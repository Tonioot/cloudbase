import json
import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, AsyncSessionLocal
from models import Node, NodeCommand, NodeInvite
from audit import log_audit
import auth as _auth

log = logging.getLogger("cloudbase.nodes")

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

# Connected node-agent websockets: node_id -> websocket
_node_ws_connections: dict[int, WebSocket] = {}
# Per-node event set when a new command is queued.
_node_command_events: dict[int, asyncio.Event] = {}
# Per-command event set when a result arrives.
_command_done_events: dict[int, asyncio.Event] = {}

# --- Streaming infrastructure ---
# stream_id -> list of queues fed by agent stream_data frames
_node_stream_queues: dict[str, list[asyncio.Queue]] = {}
# node_id -> list of queues for browser-facing event subscribers
_node_event_subscribers: dict[int, list[asyncio.Queue]] = {}
# ping_id -> asyncio.Future awaiting pong from agent
_pending_pings: dict[str, asyncio.Future] = {}
# node_id -> deque of last 60 stats snapshots (same pattern as _stats_history for apps)
from collections import deque
_node_stats_history: dict[int, deque] = {}


def subscribe_node_stream(stream_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    _node_stream_queues.setdefault(stream_id, []).append(q)
    return q


def unsubscribe_node_stream(stream_id: str, q: asyncio.Queue) -> None:
    lst = _node_stream_queues.get(stream_id, [])
    try:
        lst.remove(q)
    except ValueError:
        pass
    if not lst:
        _node_stream_queues.pop(stream_id, None)


def push_node_stream_data(stream_id: str, data: str) -> None:
    listeners = _node_stream_queues.get(stream_id, [])
    if not listeners:
        log.debug("stream_data received for stream_id=%s but no listeners", stream_id)
    for q in listeners:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("stream queue full for stream_id=%s, dropping frame", stream_id)


def _record_node_stats(node_id: int, raw: str) -> None:
    """Buffer the last 60 stats snapshots for a node (used to replay history on WS connect)."""
    _node_stats_history.setdefault(node_id, deque(maxlen=60)).append(raw)


def _subscribe_node_events(node_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _node_event_subscribers.setdefault(node_id, []).append(q)
    return q


def _unsubscribe_node_events(node_id: int, q: asyncio.Queue) -> None:
    lst = _node_event_subscribers.get(node_id, [])
    try:
        lst.remove(q)
    except ValueError:
        pass
    if not lst:
        _node_event_subscribers.pop(node_id, None)


def _push_node_event(node_id: int, event: dict) -> None:
    for q in _node_event_subscribers.get(node_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _get_node_event(node_id: int) -> asyncio.Event:
    ev = _node_command_events.get(node_id)
    if ev is None:
        ev = asyncio.Event()
        _node_command_events[node_id] = ev
    return ev


def _get_command_event(command_id: int) -> asyncio.Event:
    ev = _command_done_events.get(command_id)
    if ev is None:
        ev = asyncio.Event()
        _command_done_events[command_id] = ev
    return ev


async def _claim_pending_commands(db: AsyncSession, node_id: int, limit: int = 100) -> list[dict]:
    capped_limit = min(max(limit, 1), 100)
    result = await db.execute(
        select(NodeCommand)
        .where(and_(NodeCommand.node_id == node_id, NodeCommand.status == "queued"))
        .order_by(NodeCommand.created_at.asc())
        .limit(capped_limit)
    )
    commands = result.scalars().all()
    now = _utcnow()
    items = []
    for cmd in commands:
        claim_res = await db.execute(
            update(NodeCommand)
            .where(and_(NodeCommand.id == cmd.id, NodeCommand.status == "queued"))
            .values(status="in_progress", dispatched_at=now)
        )
        if claim_res.rowcount != 1:
            # Another concurrent claimer already took this command.
            continue
        items.append(
            {
                "id": cmd.id,
                "app_id": cmd.app_id,
                "command_type": cmd.command_type,
                "payload": json.loads(cmd.payload or "{}"),
                "idempotency_key": cmd.idempotency_key,
                "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
            }
        )
    await db.commit()
    return items


async def _apply_command_result(
    db: AsyncSession,
    *,
    node: Node,
    command_id: int,
    status: str,
    result_payload: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> None:
    result = await db.execute(select(NodeCommand).where(NodeCommand.id == command_id))
    cmd = result.scalar_one_or_none()
    if not cmd or cmd.node_id != node.id:
        raise HTTPException(404, "Command not found")

    if status not in ("done", "failed"):
        raise HTTPException(400, "Status must be 'done' or 'failed'")

    cmd.status = status
    cmd.result = json.dumps(result_payload or {}) if result_payload is not None else None
    cmd.completed_at = _utcnow()
    cmd.error_message = error_message
    try:
        payload = json.loads(cmd.payload or "{}")
    except Exception:
        payload = {}

    if cmd.app_id:
        from models import Application

        app_result = await db.execute(select(Application).where(Application.id == cmd.app_id))
        app = app_result.scalar_one_or_none()
        if app:
            if cmd.command_type == "deploy_app":
                if status == "done":
                    if result_payload:
                        app.working_dir = result_payload.get("working_dir", app.working_dir)
                        if "nginx_enabled" in result_payload:
                            app.nginx_enabled = bool(result_payload.get("nginx_enabled"))
                        # Use the actual status from the agent (auto_start may have started it)
                        remote_status = result_payload.get("status")
                        if remote_status in ("running", "stopped", "error"):
                            app.status = remote_status
                        else:
                            app.status = "stopped"
                    else:
                        app.status = "stopped"
                    app.last_error = None
                else:
                    app.status = "error"
                    if error_message:
                        app.last_error = error_message
                # Update the first ApplicationReplica status if it was created pre-deploy
                first_replica_id = payload.get("first_replica_id")
                if first_replica_id:
                    from models import ApplicationReplica
                    frep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.id == int(first_replica_id))
                    )
                    first_replica = frep_result.scalar_one_or_none()
                    if first_replica:
                        if status == "done":
                            deployed_status = (result_payload or {}).get("status", "stopped")
                            first_replica.status = deployed_status if deployed_status in ("running", "stopped", "error") else "stopped"
                            first_replica.last_error = None
                        else:
                            first_replica.status = "error"
                            if error_message:
                                first_replica.last_error = error_message
            elif cmd.command_type == "update_app":
                if status == "done":
                    if result_payload and "nginx_enabled" in result_payload:
                        app.nginx_enabled = bool(result_payload.get("nginx_enabled"))
                    app.last_error = None
                else:
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "git_pull":
                if status == "done":
                    app.last_error = None
                elif error_message:
                    app.last_error = error_message
            elif cmd.command_type in ("start_app", "stop_app", "restart_app"):
                if status == "done":
                    action = cmd.command_type.replace("_app", "")
                    reported_status = (result_payload or {}).get("status")
                    if action == "start":
                        if reported_status in ("running", "starting", "stopped", "error"):
                            app.status = reported_status
                        else:
                            app.status = "running"
                    elif action == "restart":
                        if reported_status in ("running", "restarting", "stopped", "error"):
                            app.status = reported_status
                        else:
                            app.status = "running"
                    else:
                        if reported_status in ("stopped", "stopping", "running", "error"):
                            app.status = reported_status
                        else:
                            app.status = "stopped"
                    app.last_error = None
                else:
                    app.status = "error"
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "delete_app":
                if status == "done":
                    app.status = "stopped"
                    app.last_error = None
                else:
                    app.status = "error"
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "save_nginx_config":
                if status == "done":
                    if (result_payload or {}).get("ok"):
                        app.nginx_enabled = True
                        app.last_error = None
                    else:
                        app.last_error = (result_payload or {}).get("message")
                elif error_message:
                    app.last_error = error_message
            elif cmd.command_type == "save_maintenance_pages":
                if status == "done":
                    if (result_payload or {}).get("ok", True):
                        app.last_error = None
                    else:
                        app.last_error = (result_payload or {}).get("message")
                elif error_message:
                    app.last_error = error_message
            elif cmd.command_type == "toggle_maintenance_mode":
                if status == "done":
                    app.last_error = None
                else:
                    if "previous_maintenance_mode" in payload:
                        app.maintenance_mode = bool(payload.get("previous_maintenance_mode"))
                    if "previous_update_mode" in payload:
                        app.update_mode = bool(payload.get("previous_update_mode"))
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type == "toggle_update_mode":
                if status == "done":
                    app.last_error = None
                else:
                    if "previous_maintenance_mode" in payload:
                        app.maintenance_mode = bool(payload.get("previous_maintenance_mode"))
                    if "previous_update_mode" in payload:
                        app.update_mode = bool(payload.get("previous_update_mode"))
                    if error_message:
                        app.last_error = error_message
            elif cmd.command_type in ("start_replica", "stop_replica"):
                from models import ApplicationReplica
                replica_id = payload.get("replica_id")
                if replica_id:
                    rep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.id == int(replica_id))
                    )
                    replica = rep_result.scalar_one_or_none()
                    if replica:
                        if cmd.command_type == "start_replica":
                            if status == "done":
                                # Container is up on the remote node; stay in "starting"
                                # until the reverse tunnel connects (which sets "running").
                                replica.status = "starting"
                                replica.container_id = (result_payload or {}).get("container_id")
                                replica.last_error = None
                            else:
                                replica.status = "error"
                                replica.last_error = error_message
                        else:  # stop_replica
                            replica.status = "stopped" if status == "done" else "error"
                            if status != "done" and error_message:
                                replica.last_error = error_message
            app.updated_at = _utcnow()

    await db.commit()
    _get_command_event(command_id).set()

    # Broadcast completion event to browser subscribers
    new_app_status = None
    if cmd.app_id:
        from models import Application
        app_result2 = await db.execute(select(Application).where(Application.id == cmd.app_id))
        app2 = app_result2.scalar_one_or_none()
        if app2:
            new_app_status = app2.status
    _push_node_event(node.id, {
        "type": "command_update",
        "command_id": command_id,
        "status": status,
        "app_id": cmd.app_id,
        "app_status": new_app_status,
        "error_message": error_message,
    })


class InviteRequest(BaseModel):
    note: Optional[str] = None
    ttl_minutes: int = Field(default=30, ge=1, le=1440)


class RegisterNodeRequest(BaseModel):
    invite_code: str
    name: str
    role: str = "node"
    api_base_url: Optional[str] = None
    public_host: Optional[str] = None
    capabilities: Optional[dict] = None
    metadata_json: Optional[dict] = None
    heartbeat_interval: int = Field(default=15, ge=5, le=300)


class CommandResultRequest(BaseModel):
    status: str
    result: Optional[dict] = None
    error_message: Optional[str] = None


def _utcnow() -> datetime:
    return datetime.utcnow()


async def ensure_local_node(db: AsyncSession) -> Node:
    result = await db.execute(select(Node).where(Node.is_local == True))
    local = result.scalar_one_or_none()
    if local:
        updated = False
        if local.status != "online":
            local.status = "online"
            updated = True
        local.last_seen = _utcnow()
        if local.offline_since is not None:
            local.offline_since = None
            updated = True
        if local.name in ("local-cloudbase", "local_cloudbase", "local-cloudbae", "local_cloudbae", "") or "local" in local.name.lower():
            local.name = "Primary Node"
            updated = True
        # Always refresh local system info
        local.metadata_json = json.dumps(_local_system_info())
        updated = True
        if updated:
            local.updated_at = _utcnow()
            await db.commit()
            await db.refresh(local)
        return local

    local = Node(
        name="Primary Node",
        role="hybrid",
        status="online",
        is_local=True,
        enabled=True,
        heartbeat_interval=15,
        capabilities=json.dumps({"local_execution": True}),
        metadata_json=json.dumps(_local_system_info()),
        last_seen=_utcnow(),
    )
    db.add(local)
    await db.commit()
    await db.refresh(local)
    return local


async def get_node_or_404(node_id: int, db: AsyncSession) -> Node:
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    return node


async def get_node_by_token_or_401(token: str, db: AsyncSession) -> Node:
    result = await db.execute(select(Node).where(and_(Node.auth_token == token, Node.enabled == True)))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(401, "Invalid node token")
    return node


async def queue_node_command(
    db: AsyncSession,
    *,
    node_id: int,
    command_type: str,
    payload: dict,
    app_id: Optional[int] = None,
) -> NodeCommand:
    cmd = NodeCommand(
        node_id=node_id,
        app_id=app_id,
        command_type=command_type,
        payload=json.dumps(payload or {}),
        status="queued",
        idempotency_key=secrets.token_hex(16),
    )
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)
    ws_connected = node_id in _node_ws_connections
    log.info("queue_command: id=%d type=%s node_id=%d ws_connected=%s",
             cmd.id, command_type, node_id, ws_connected)
    _get_node_event(node_id).set()
    return cmd


async def wait_for_node_command(
    _db: AsyncSession,
    command_id: int,
    *,
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.5,
) -> NodeCommand:
    # Each poll uses a fresh session so SQLite's snapshot isolation doesn't hide
    # status updates committed by _claim_pending_commands / _apply_command_result.
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    event = _get_command_event(command_id)
    log.debug("wait_for_command: id=%d timeout=%.0fs", command_id, timeout_seconds)
    cmd = None
    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionLocal() as fresh_db:
            result = await fresh_db.execute(select(NodeCommand).where(NodeCommand.id == command_id))
            cmd = result.scalar_one_or_none()
        if not cmd:
            raise HTTPException(404, "Node command not found")
        if cmd.status in ("done", "failed"):
            log.debug("wait_for_command: id=%d completed with status=%s", command_id, cmd.status)
            return cmd
        remaining = max(deadline - asyncio.get_running_loop().time(), 0)
        wait_for = min(poll_seconds, remaining)
        if wait_for <= 0:
            break
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_for)
        except asyncio.TimeoutError:
            pass
        finally:
            event.clear()
    log.warning("wait_for_command: id=%d TIMED OUT after %.0fs (status still %s)",
                command_id, timeout_seconds, cmd.status if cmd else '?')
    raise HTTPException(504, "Timed out waiting for node command result")


def _local_system_info() -> dict:
    import socket as _socket
    info: dict = {}
    try:
        import psutil, platform, subprocess
        info["hostname"]  = _socket.gethostname()
        info["os"]        = platform.platform()
        info["os_short"]  = f"{platform.system()} {platform.release()}"
        info["arch"]      = platform.machine()
        
        import time as _time
        try:
            info["uptime_secs"] = round(_time.time() - psutil.boot_time())
        except Exception: pass

        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        info["ram_total_mb"]  = round(mem.total / 1024 / 1024)
        info["disk_total_gb"] = round(disk.total / 1024 / 1024 / 1024, 1)
        
        info["cpu_count"]         = psutil.cpu_count(logical=False) or 0
        info["cpu_count_logical"] = psutil.cpu_count(logical=True) or 0
        
        try:
            freq = psutil.cpu_freq()
            if freq:
                info["cpu_freq_mhz"] = round(freq.max or freq.current)
        except Exception:
            pass

        # Try to get CPU model
        try:
            import cpuinfo
            c = cpuinfo.get_cpu_info()
            info["cpu_model"] = c.get("brand_raw", "")
        except Exception:
            if platform.system() == "Windows":
                try:
                    info["cpu_model"] = subprocess.check_output(["wmic", "cpu", "get", "name"]).decode().split("\n")[1].strip()
                except Exception: pass
            elif platform.system() == "Darwin":
                try:
                    info["cpu_model"] = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
                except Exception: pass
            else:
                try:
                    with open("/proc/cpuinfo") as f:
                        for line in f:
                            if line.startswith("model name"):
                                info["cpu_model"] = line.split(":", 1)[1].strip()
                                break
                except Exception: pass

        # Try to get GPU info
        try:
            if platform.system() == "Windows":
                try:
                    gpu_out = subprocess.check_output(["wmic", "path", "win32_VideoController", "get", "name"]).decode()
                    gpus = [line.strip() for line in gpu_out.split("\n") if line.strip() and "Name" not in line]
                    if gpus: info["gpu_model"] = ", ".join(gpus)
                except Exception: pass
            else:
                try:
                    gpu_out = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).decode()
                    gpus = [line.strip() for line in gpu_out.split("\n") if line.strip()]
                    if gpus: info["gpu_model"] = ", ".join(gpus)
                except Exception: pass
        except Exception:
            pass

        try:
            addrs = psutil.net_if_addrs()
            ips = []
            for iface, addr_list in addrs.items():
                if iface.startswith("lo") or "loopback" in iface.lower():
                    continue
                for a in addr_list:
                    if a.family == _socket.AF_INET:
                        ips.append(a.address)
            if ips:
                info["ip"] = ips[0]
                info["ip_all"] = ips
        except Exception:
            pass
    except Exception:
        info.setdefault("hostname", _socket.gethostname())
    return info


def _local_metrics() -> dict:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "disk_percent": disk.percent,
            "memory_total_mb": round(mem.total / 1024 / 1024),
            "memory_used_mb": round(mem.used / 1024 / 1024),
        }
    except Exception:
        return {}


def _as_node_dict(node: Node) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "role": node.role,
        "status": node.status,
        "api_base_url": node.api_base_url,
        "public_host": node.public_host,
        "is_local": bool(node.is_local),
        "enabled": bool(node.enabled),
        "heartbeat_interval": node.heartbeat_interval,
        "capabilities": json.loads(node.capabilities or "{}"),
        "metadata": json.loads(node.metadata_json or "{}"),
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        "offline_since": node.offline_since.isoformat() + "Z" if node.offline_since else None,
        "last_error": node.last_error,
        "connection_type": node.connection_type,
        "agent_version": node.agent_version,
        "node_metrics": _local_metrics() if node.is_local else (
            {
                "cpu_percent": node.node_cpu_percent,
                "memory_percent": node.node_memory_percent,
                "disk_percent": node.node_disk_percent,
            } if node.node_cpu_percent is not None else None
        ),
        "websocket_connected": node.id in _node_ws_connections if node.id else False,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


@router.get("")
async def list_nodes(db: AsyncSession = Depends(get_db)):
    await ensure_local_node(db)
    result = await db.execute(select(Node).order_by(Node.is_local.desc(), Node.name.asc()))
    nodes = result.scalars().all()
    return [_as_node_dict(n) for n in nodes]


@router.post("/invites")
async def create_invite(req: InviteRequest, db: AsyncSession = Depends(get_db)):
    code = secrets.token_urlsafe(24)
    invite = NodeInvite(
        code=code,
        note=req.note,
        expires_at=_utcnow() + timedelta(minutes=req.ttl_minutes),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return {
        "code": invite.code,
        "note": invite.note,
        "expires_at": invite.expires_at.isoformat() + "Z",  # Append Z to indicate UTC
    }


@router.post("/agent/register")
async def register_node(req: RegisterNodeRequest, db: AsyncSession = Depends(get_db)):
    now = _utcnow()
    result = await db.execute(select(NodeInvite).where(NodeInvite.code == req.invite_code))
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(400, "Invalid invite code")
    if invite.used_at is not None:
        raise HTTPException(400, "Invite code already used")
    if invite.expires_at < now:
        raise HTTPException(400, "Invite code expired")

    existing = await db.execute(select(Node).where(Node.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Node '{req.name}' already exists")

    auth_token = secrets.token_urlsafe(40)
    node = Node(
        name=req.name,
        role=req.role if req.role in ("main", "node", "hybrid") else "node",
        status="online",
        api_base_url=req.api_base_url,
        public_host=req.public_host,
        auth_token=auth_token,
        is_local=False,
        enabled=True,
        heartbeat_interval=req.heartbeat_interval,
        capabilities=json.dumps(req.capabilities or {}),
        metadata_json=json.dumps(req.metadata_json or {}),
        last_seen=now,
    )
    db.add(node)
    await db.flush()

    invite.used_at = now
    invite.node_id = node.id

    await log_audit(db, "node.connect", actor="agent", detail={"name": node.name, "node_id": node.id})
    await db.commit()
    await db.refresh(node)

    # Broadcast to all browser subscribers that a new node connected
    for qlist in list(_node_event_subscribers.values()):
        for q in qlist:
            try:
                q.put_nowait({"type": "node_connected", "node_id": node.id, "node_name": node.name})
            except asyncio.QueueFull:
                pass

    return {
        "node": _as_node_dict(node),
        "auth_token": auth_token,
    }


@router.get("/agent/commands")
async def get_pending_commands(
    limit: int = 20,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    items = await _claim_pending_commands(db, node.id, limit=limit)
    return {"commands": items}


@router.post("/agent/commands/{command_id}/result")
async def report_command_result(
    command_id: int,
    req: CommandResultRequest,
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    await _apply_command_result(
        db,
        node=node,
        command_id=command_id,
        status=req.status,
        result_payload=req.result,
        error_message=req.error_message,
    )
    return {"ok": True}


@router.get("/agent/my-replicas")
async def get_node_replicas(
    x_node_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Return all live replica IDs assigned to this node so the agent can detect orphan containers."""
    if not x_node_token:
        raise HTTPException(401, "Missing node token")
    node = await get_node_by_token_or_401(x_node_token, db)
    from models import ApplicationReplica
    result = await db.execute(
        select(ApplicationReplica.id, ApplicationReplica.app_id).where(
            ApplicationReplica.node_id == node.id,
            ApplicationReplica.status.not_in(["stopped", "error"]),
        )
    )
    replicas = [{"id": row[0], "app_id": row[1]} for row in result.all()]
    return {"replicas": replicas}


async def _dispatch_queued_commands_ws(node_id: int, websocket: WebSocket, *, limit: int = 100) -> int:
    async with AsyncSessionLocal() as db:
        items = await _claim_pending_commands(db, node_id, limit=limit)
    if items:
        log.info("dispatch %d command(s) to node id=%d: %s",
                 len(items), node_id, [i["command_type"] for i in items])
    for item in items:
        await websocket.send_json({"type": "command", "command": item})
    return len(items)


@router.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket):
    await websocket.accept()
    node: Optional[Node] = None
    node_id: Optional[int] = None
    client = websocket.client
    client_addr = f"{client.host}:{client.port}" if client else "unknown"

    log.info("WS agent connection accepted from %s", client_addr)

    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if (hello or {}).get("type") != "auth":
            log.warning("WS agent from %s: first message was not auth, closing", client_addr)
            await websocket.send_json({"type": "error", "error": "First message must be auth"})
            await websocket.close(code=1008)
            return
        token = (hello or {}).get("token")
        if not token:
            log.warning("WS agent from %s: missing token, closing", client_addr)
            await websocket.send_json({"type": "error", "error": "Missing node token"})
            await websocket.close(code=1008)
            return

        async with AsyncSessionLocal() as db:
            node = await get_node_by_token_or_401(token, db)
            was_offline = node.status == "offline"
            node.status = "online"
            node.last_seen = _utcnow()
            node.offline_since = None
            await db.commit()

        node_id = int(node.id)
        old_ws = _node_ws_connections.get(node_id)
        if old_ws is not None and old_ws is not websocket:
            try:
                await old_ws.close(code=1012)
            except Exception:
                pass
        _node_ws_connections[node_id] = websocket
        _get_node_event(node_id).set()

        if was_offline:
            asyncio.create_task(recover_node_replicas(node_id))

        log.info("WS agent authenticated: node '%s' (id=%d) from %s", node.name, node_id, client_addr)

        await websocket.send_json({
            "type": "auth_ok",
            "node_id": node_id,
            "heartbeat_interval": node.heartbeat_interval,
            "server_time": _utcnow().isoformat(),
        })

        event = _get_node_event(node_id)
        receive_task: asyncio.Task = asyncio.create_task(websocket.receive_json())
        event_task: asyncio.Task = asyncio.create_task(event.wait())

        try:
            while True:
                done, _ = await asyncio.wait(
                    {receive_task, event_task},
                    timeout=3,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Only mark offline when this websocket is still the active one.
                if _node_ws_connections.get(node_id) is None:
                    async with AsyncSessionLocal() as db:
                        from models import ApplicationReplica
                        result = await db.execute(select(Node).where(Node.id == node_id))
                        offline_node = result.scalar_one_or_none()
                        if offline_node and offline_node.status != "offline":
                            offline_node.status = "offline"
                            if offline_node.offline_since is None:
                                offline_node.offline_since = _utcnow()
                            offline_node.connection_type = None
                            # Mark running replicas so they get recovered on reconnect
                            rep_res = await db.execute(
                                select(ApplicationReplica).where(
                                    and_(ApplicationReplica.node_id == node_id, ApplicationReplica.status == "running")
                                )
                            )
                            for _r in rep_res.scalars().all():
                                _r.status = "node_offline"
                                _r.tunnel_port = None
                            await db.commit()
                            log.info("Node id=%d marked offline after WS disconnect", node_id)
                    _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})

                if event_task in done:
                    await _dispatch_queued_commands_ws(node_id, websocket)
                    event_task = asyncio.create_task(event.wait())

                if receive_task in done:
                    try:
                        incoming = receive_task.result()
                    except Exception:
                        break  # WebSocket closed
                    receive_task = asyncio.create_task(websocket.receive_json())

                    msg_type = (incoming or {}).get("type")
                    if msg_type == "heartbeat":
                        log.debug("WS heartbeat from node id=%d", node_id)
                        async with AsyncSessionLocal() as db:
                            live_node = await get_node_by_token_or_401(token, db)
                            live_node.status = "online"
                            live_node.last_seen = _utcnow()
                            live_node.offline_since = None
                            live_node.connection_type = "websocket"
                            caps = incoming.get("capabilities") or {}
                            if caps:
                                live_node.capabilities = json.dumps(caps)
                                if caps.get("agent_version"):
                                    live_node.agent_version = caps["agent_version"]
                            if incoming.get("metadata_json") is not None:
                                live_node.metadata_json = json.dumps(incoming.get("metadata_json") or {})
                            metrics = incoming.get("node_metrics") or {}
                            if metrics:
                                live_node.node_cpu_percent = metrics.get("cpu_percent")
                                live_node.node_memory_percent = metrics.get("memory_percent")
                                live_node.node_disk_percent = metrics.get("disk_percent")
                            await db.commit()
                        _push_node_event(node_id, {
                            "type": "node_health",
                            "cpu_percent": incoming.get("node_metrics", {}).get("cpu_percent"),
                            "memory_percent": incoming.get("node_metrics", {}).get("memory_percent"),
                            "disk_percent": incoming.get("node_metrics", {}).get("disk_percent"),
                            "connection_type": "websocket",
                        })
                    elif msg_type == "status_update":
                        app_id = (incoming or {}).get("app_id")
                        new_status = (incoming or {}).get("status")
                        if app_id and new_status:
                            async with AsyncSessionLocal() as db:
                                from models import Application
                                res = await db.execute(select(Application).where(Application.id == int(app_id)))
                                target_app = res.scalar_one_or_none()
                                if target_app and target_app.node_id == node_id:
                                    target_app.status = new_status
                                    if new_status != "running":
                                        target_app.pid = None
                                    await db.commit()
                    elif msg_type == "command_result":
                        command_id = int(incoming.get("command_id") or -1)
                        if command_id == -1:
                            pass  # streaming commands use id=-1, no DB record to update
                        else:
                            async with AsyncSessionLocal() as db:
                                live_node = await get_node_by_token_or_401(token, db)
                                await _apply_command_result(
                                    db,
                                    node=live_node,
                                    command_id=command_id,
                                    status=(incoming.get("status") or "failed"),
                                    result_payload=incoming.get("result"),
                                    error_message=incoming.get("error_message"),
                                )
                    elif msg_type == "stream_data":
                        stream_id = incoming.get("stream_id")
                        data = incoming.get("data")
                        if stream_id and data is not None:
                            push_node_stream_data(stream_id, data)
                    elif msg_type == "pong":
                        ping_id = incoming.get("ping_id", "")
                        fut = _pending_pings.get(ping_id)
                        if fut and not fut.done():
                            fut.set_result(True)
                    elif msg_type == "ping":
                        await websocket.send_json({"type": "pong", "server_time": _utcnow().isoformat()})
        finally:
            receive_task.cancel()
            event_task.cancel()

    except WebSocketDisconnect as e:
        log.info("WS agent disconnected: node id=%s code=%s", node_id, getattr(e, 'code', '?'))
    except Exception as e:
        log.exception("Unhandled error in agent_ws for node id=%s: %s", node_id, e)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        should_mark_offline = False
        if node_id is not None:
            current_ws = _node_ws_connections.get(node_id)
            if current_ws is websocket:
                _node_ws_connections.pop(node_id, None)
                log.info("WS agent removed from active connections: node id=%d", node_id)
                should_mark_offline = True
            elif current_ws is None:
                should_mark_offline = True

        if node_id is not None and should_mark_offline:
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(Node).where(Node.id == node_id))
                    offline_node = result.scalar_one_or_none()
                    if offline_node and offline_node.status != "offline":
                        offline_node.status = "offline"
                        if offline_node.offline_since is None:
                            offline_node.offline_since = _utcnow()
                        offline_node.connection_type = None
                        await db.commit()
                        log.info("Node id=%d marked offline after WS disconnect", node_id)
                _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})
            except Exception as e:
                log.error("Error marking node id=%s offline: %s", node_id, e)


@router.post("/{node_id}/enable")
async def enable_node(node_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    node.enabled = True
    await log_audit(db, "node.enable", actor=actor, detail={"name": node.name, "node_id": node_id})
    await db.commit()
    return _as_node_dict(node)


@router.post("/{node_id}/disable")
async def disable_node(node_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    if node.is_local:
        raise HTTPException(400, "Local node cannot be disabled")
    node.enabled = False
    node.status = "offline"
    if node.offline_since is None:
        node.offline_since = _utcnow()
    await log_audit(db, "node.disable", actor=actor, detail={"name": node.name, "node_id": node_id})
    await db.commit()
    return _as_node_dict(node)


class NodeUpdateRequest(BaseModel):
    name: Optional[str] = None


@router.patch("/{node_id}")
async def update_node(node_id: int, req: NodeUpdateRequest, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    old_name = node.name
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "Name cannot be empty")
        existing = await db.execute(select(Node).where(Node.name == name, Node.id != node_id))
        if existing.scalar_one_or_none():
            raise HTTPException(409, "A node with that name already exists")
        node.name = name
    await log_audit(db, "node.rename", actor=actor, detail={"old_name": old_name, "new_name": node.name, "node_id": node_id})
    await db.commit()
    await db.refresh(node)
    return _as_node_dict(node)


@router.delete("/{node_id}")
async def delete_node(node_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    node = await get_node_or_404(node_id, db)
    if node.is_local:
        raise HTTPException(400, "Local node cannot be deleted")
    from models import Application
    app_count_result = await db.execute(
        select(func.count()).where(Application.node_id == node_id)
    )
    app_count = app_count_result.scalar() or 0
    if app_count > 0:
        raise HTTPException(
            400,
            f"Cannot remove node: {app_count} app{'s' if app_count != 1 else ''} still assigned to it. "
            "Move or delete all apps first."
        )
    node_name = node.name
    await db.execute(delete(NodeCommand).where(NodeCommand.node_id == node_id))
    await db.execute(delete(NodeInvite).where(NodeInvite.node_id == node_id))
    await db.delete(node)
    await log_audit(db, "node.delete", actor=actor, detail={"name": node_name, "node_id": node_id})
    await db.commit()
    return {"ok": True}


@router.get("/{node_id}/commands")
async def list_node_commands(node_id: int, db: AsyncSession = Depends(get_db)):
    await get_node_or_404(node_id, db)
    result = await db.execute(
        select(NodeCommand)
        .where(NodeCommand.node_id == node_id)
        .order_by(NodeCommand.created_at.desc())
        .limit(200)
    )
    commands = result.scalars().all()
    return [
        {
            "id": c.id,
            "node_id": c.node_id,
            "app_id": c.app_id,
            "command_type": c.command_type,
            "payload": json.loads(c.payload or "{}"),
            "status": c.status,
            "idempotency_key": c.idempotency_key,
            "result": json.loads(c.result or "{}") if c.result else None,
            "error_message": c.error_message,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "dispatched_at": c.dispatched_at.isoformat() if c.dispatched_at else None,
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        }
        for c in commands
    ]


@router.get("/{node_id}/commands/{command_id}")
async def get_node_command_status(node_id: int, command_id: int, db: AsyncSession = Depends(get_db)):
    await get_node_or_404(node_id, db)
    result = await db.execute(
        select(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.id == command_id)
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Command not found")
    return {
        "id": c.id,
        "status": c.status,
        "result": json.loads(c.result or "{}") if c.result else None,
        "error_message": c.error_message,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


async def recover_node_replicas(node_id: int) -> None:
    """Queue start_replica for every replica marked node_offline on this node."""
    from models import Application, ApplicationReplica
    from env_crypto import decrypt_env
    from routers.applications import _remote_replica_command_payload
    import process_manager as pm

    async with AsyncSessionLocal() as db:
        rep_result = await db.execute(
            select(ApplicationReplica).where(
                and_(ApplicationReplica.node_id == node_id, ApplicationReplica.status == "node_offline")
            )
        )
        replicas = rep_result.scalars().all()
        if not replicas:
            return

        log.info("Node id=%d reconnected — recovering %d replica(s)", node_id, len(replicas))

        for replica in replicas:
            app_result = await db.execute(select(Application).where(Application.id == replica.app_id))
            app = app_result.scalar_one_or_none()
            if not app:
                replica.status = "error"
                replica.last_error = "App not found during node recovery"
                await db.commit()
                continue

            try:
                env_vars = decrypt_env(app.env_vars or "")
            except Exception:
                env_vars = {}

            ext_port = replica.external_port or app.port or 8000
            remote_payload = _remote_replica_command_payload(app, env_vars, ext_port)

            replica.status = "starting"
            replica.tunnel_port = None
            replica.last_error = None
            await db.commit()

            await queue_node_command(
                db,
                node_id=node_id,
                app_id=app.id,
                command_type="start_replica",
                payload={**remote_payload, "replica_id": replica.id},
            )
            pm._push_line(app.id, f"⟳ Node came back online — recovering instance {replica.id}…")
            await db.commit()


async def mark_stale_nodes_offline(db: AsyncSession) -> None:
    from models import ApplicationReplica
    result = await db.execute(select(Node).where(and_(Node.enabled == True, Node.is_local == False)))
    nodes = result.scalars().all()
    now = _utcnow()
    changed = False
    for node in nodes:
        if not node.last_seen:
            continue
        threshold = max(node.heartbeat_interval * 2, 20)
        delta = (now - node.last_seen).total_seconds()
        if delta > threshold and node.status != "offline":
            log.warning("Node '%s' (id=%d) stale: last seen %.0fs ago (threshold %ds) — marking offline", node.name, node.id, delta, threshold)
            node.status = "offline"
            if node.offline_since is None:
                node.offline_since = now
            node.connection_type = None
            changed = True
            _push_node_event(node.id, {"type": "node_offline", "node_id": node.id})
            # Mark running replicas as node_offline so they get recovered when the node reconnects
            rep_result = await db.execute(
                select(ApplicationReplica).where(
                    and_(ApplicationReplica.node_id == node.id, ApplicationReplica.status == "running")
                )
            )
            for replica in rep_result.scalars().all():
                replica.status = "node_offline"
                replica.tunnel_port = None
    if changed:
        await db.commit()


# --- New endpoints ---

@router.post("/{node_id}/ping")
async def ping_node(node_id: int, db: AsyncSession = Depends(get_db)):
    node = await get_node_or_404(node_id, db)

    if node.is_local:
        return {"reachable": True, "latency_ms": 0, "transport": "local"}

    if node.status == "offline" or not node.enabled:
        return {"reachable": False, "latency_ms": None, "transport": "none", "error": "Node is offline"}

    ws = _node_ws_connections.get(node_id)
    if ws:
        ping_id = secrets.token_hex(4)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending_pings[ping_id] = fut
        t0 = time.monotonic()
        try:
            await ws.send_json({"type": "ping", "ping_id": ping_id})
            await asyncio.wait_for(fut, timeout=5.0)
            latency_ms = round((time.monotonic() - t0) * 1000)
            return {"reachable": True, "latency_ms": latency_ms, "transport": "websocket"}
        except asyncio.TimeoutError:
            # WS is stale — evict it and mark node offline immediately
            _node_ws_connections.pop(node_id, None)
            async with AsyncSessionLocal() as wdb:
                stale = await wdb.get(Node, node_id)
                if stale:
                    stale.status = "offline"
                    stale.connection_type = None
                    if stale.offline_since is None:
                        stale.offline_since = _utcnow()
                    await wdb.commit()
            _push_node_event(node_id, {"type": "node_offline", "node_id": node_id})
            return {"reachable": False, "latency_ms": None, "transport": "websocket", "error": "Ping timed out — node marked offline"}
        except Exception as exc:
            return {"reachable": False, "latency_ms": None, "transport": "websocket", "error": str(exc)}
        finally:
            _pending_pings.pop(ping_id, None)

    if node.api_base_url:
        import httpx
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient() as client:
                resp = await asyncio.wait_for(
                    client.get(f"{node.api_base_url}/api/health", timeout=5),
                    timeout=6,
                )
            latency_ms = round((time.monotonic() - t0) * 1000)
            return {"reachable": resp.status_code == 200, "latency_ms": latency_ms, "transport": "http"}
        except Exception as exc:
            return {"reachable": False, "latency_ms": None, "transport": "http", "error": str(exc)}

    return {"reachable": False, "latency_ms": None, "transport": "none", "error": "No active connection"}


@router.get("/{node_id}/connection-status")
async def get_node_connection_status(node_id: int, db: AsyncSession = Depends(get_db)):
    node = await get_node_or_404(node_id, db)
    pending_count_result = await db.execute(
        select(func.count()).where(
            and_(NodeCommand.node_id == node_id, NodeCommand.status.in_(["queued", "in_progress"]))
        )
    )
    pending_count = pending_count_result.scalar() or 0
    ws_connected = node_id in _node_ws_connections
    return {
        "node_id": node_id,
        "websocket_connected": ws_connected,
        "connection_type": node.connection_type or ("websocket" if ws_connected else "http_polling"),
        "agent_version": node.agent_version,
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        "node_metrics": {
            "cpu_percent": node.node_cpu_percent,
            "memory_percent": node.node_memory_percent,
            "disk_percent": node.node_disk_percent,
        } if node.node_cpu_percent is not None else None,
        "pending_commands": pending_count,
    }


# ── Reverse tunnel WebSocket ──────────────────────────────────────────────────
# The remote node agent connects here to expose a replica container without
# opening any inbound firewall ports on the remote node.

async def _regen_nginx_for_app(app_id: int) -> None:
    """Rebuild nginx config for an app after a tunnel state change.

    Intentionally self-contained (no import from routers.applications) to avoid
    circular imports.  Replicates the backend-list logic of _get_nginx_backends.
    """
    import json as _json
    import nginx_manager as _nm
    from models import Application, ApplicationReplica

    async with AsyncSessionLocal() as db:
        app_result = await db.execute(select(Application).where(Application.id == app_id))
        app = app_result.scalar_one_or_none()
        if not app or not app.nginx_enabled or not app.domain:
            return

        local_node_result = await db.execute(select(Node).where(Node.is_local == True))
        local_node = local_node_result.scalar_one_or_none()
        if not local_node:
            return

        rep_result = await db.execute(
            select(ApplicationReplica, Node)
            .join(Node, ApplicationReplica.node_id == Node.id, isouter=True)
            .where(
                ApplicationReplica.app_id == app_id,
                ApplicationReplica.status == "running",
            )
        )
        running_replicas = rep_result.all()

        app_node_result = await db.execute(select(Node).where(Node.id == app.node_id))
        app_node = app_node_result.scalar_one_or_none() or local_node
        main_port = app.external_port or app.port

        if not running_replicas:
            backends: "int | list" = main_port
        else:
            def _addr(node: Optional[Node], external_port: Optional[int], tunnel_port: Optional[int]) -> Optional[str]:
                if node is None or node.is_local:
                    return f"127.0.0.1:{external_port}" if external_port else None
                # Remote node: use the reverse tunnel port on the main node
                return f"127.0.0.1:{tunnel_port}" if tunnel_port else None

            blist: list[str] = []
            if main_port:
                if app_node.is_local:
                    blist.append(f"127.0.0.1:{main_port}")
                elif app_node.public_host:
                    blist.append(f"{app_node.public_host}:{main_port}")
            for replica, r_node in running_replicas:
                addr = _addr(r_node, replica.external_port, replica.tunnel_port)
                if addr:
                    blist.append(addr)
            backends = blist if len(blist) > 1 else main_port

        if not backends:
            return

        nginx_mode = (
            "update" if app.update_mode
            else "maintenance" if app.maintenance_mode
            else "normal"
        )
        extra_domains   = _json.loads(app.extra_domains   or "[]")
        redirect_domains = _json.loads(app.redirect_domains or "[]")

        config = _nm.generate_config(
            app.name, app.domain, backends,
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app.id, mode=nginx_mode,
            extra_domains=extra_domains,
            redirect_domains=redirect_domains,
        )
        try:
            _nm.write_nginx_config(app.name, config)
        except Exception as e:
            log.warning("[tunnel] nginx reload failed for app=%d: %s", app_id, e)


@router.websocket("/ws/tunnel/{replica_id}")
async def replica_tunnel_ws(replica_id: int, websocket: WebSocket):
    """Reverse-tunnel endpoint — the node agent connects here to expose its
    replica container on a localhost port on the main node.  Nginx then
    load-balances to 127.0.0.1:{tunnel_port} instead of the remote node's
    public IP, eliminating the need for open inbound firewall ports.
    """
    import tunnel_server
    from models import ApplicationReplica

    # Authenticate: accept token from header OR query param (websocket libraries
    # cannot always set custom headers — query param is the fallback).
    token = (
        websocket.headers.get("x-node-token")
        or websocket.query_params.get("token")
    )
    if not token:
        await websocket.close(code=1008, reason="Missing node token")
        return

    async with AsyncSessionLocal() as db:
        try:
            node = await get_node_by_token_or_401(token, db)
        except HTTPException:
            await websocket.close(code=1008, reason="Invalid node token")
            return

        # Verify the replica belongs to this node
        rep_result = await db.execute(
            select(ApplicationReplica).where(ApplicationReplica.id == replica_id)
        )
        replica = rep_result.scalar_one_or_none()
        if not replica or replica.node_id != node.id:
            await websocket.close(code=1008, reason="Replica not found or not owned by this node")
            return
        app_id = replica.app_id

    await websocket.accept()
    log.info("[tunnel-ws] replica=%d node=%s connected", replica_id, node.name)

    # Allocate a free localhost port and start the TCP listener
    local_port = await tunnel_server.open_tunnel(replica_id, websocket)
    if local_port is None:
        await websocket.close(code=1011, reason="No tunnel ports available")
        return

    # Persist tunnel_port and promote status → running
    async with AsyncSessionLocal() as db:
        rep_result = await db.execute(
            select(ApplicationReplica).where(ApplicationReplica.id == replica_id)
        )
        replica = rep_result.scalar_one_or_none()
        if replica:
            replica.tunnel_port = local_port
            replica.status = "running"
            replica.last_error = None
            replica.updated_at = _utcnow()
            await db.commit()

    log.info("[tunnel-ws] replica=%d allocated 127.0.0.1:%d", replica_id, local_port)

    # Regenerate nginx to include the new backend
    await _regen_nginx_for_app(app_id)

    # Capture node_id for later use in finally (node object may be detached)
    _tunnel_node_id = node.id

    # Relay messages until the agent disconnects
    try:
        async for message in websocket.iter_text():
            await tunnel_server.dispatch_message(replica_id, message)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("[tunnel-ws] replica=%d error: %s", replica_id, e)
    finally:
        log.info("[tunnel-ws] replica=%d disconnected, tearing down tunnel", replica_id)

        # Guard against race condition: if the agent reconnected and opened a new
        # tunnel for this replica before our cleanup runs, leave the new tunnel alone.
        was_active = tunnel_server.is_active_tunnel(replica_id, websocket)
        if was_active:
            await tunnel_server.close_tunnel(replica_id)

        # Only update DB state if we were still the active tunnel
        if was_active:
            async with AsyncSessionLocal() as db:
                rep_result = await db.execute(
                    select(ApplicationReplica).where(ApplicationReplica.id == replica_id)
                )
                replica = rep_result.scalar_one_or_none()
                if replica:
                    was_running = replica.status == "running"
                    replica.tunnel_port = None
                    # If the node agent WS is no longer connected, the tunnel dropped
                    # due to a node outage — mark the replica for automatic recovery.
                    # If the agent is still connected, the container stopped on its own.
                    agent_online = _tunnel_node_id in _node_ws_connections
                    replica.status = "stopped" if agent_online else "node_offline"
                    replica.updated_at = _utcnow()
                    await db.commit()
                    if was_running:
                        # Regenerate nginx to remove the dropped backend
                        await _regen_nginx_for_app(app_id)


@router.get("/{node_id}/agent-logs")
async def get_node_agent_logs(node_id: int, limit: int = 200, db: AsyncSession = Depends(get_db)):
    node = await get_node_or_404(node_id, db)
    if node.is_local:
        import os
        log_path = os.path.expanduser("~/.cloudbase/logs/node-agent.log")
        try:
            with open(log_path, encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            return {"lines": [l.rstrip() for l in lines], "source": "local"}
        except FileNotFoundError:
            return {"lines": [], "source": "local", "error": "Log file not found"}
        except Exception as exc:
            return {"lines": [], "source": "local", "error": str(exc)}

    # Remote: queue a command and wait for result
    cmd = await queue_node_command(
        db,
        node_id=node_id,
        command_type="get_agent_logs",
        payload={"limit": max(1, min(limit, 2000))},
    )
    try:
        cmd = await wait_for_node_command(db, cmd.id, timeout_seconds=20.0)
        result = json.loads(cmd.result or "{}") if cmd.result else {}
        return {"lines": result.get("lines", []), "source": "remote"}
    except HTTPException:
        return {"lines": [], "source": "remote", "error": "Timed out waiting for agent logs"}


@router.websocket("/{node_id}/events")
async def node_events_ws(node_id: int, websocket: WebSocket):
    """Browser-facing WebSocket: streams command updates and node health events."""
    await websocket.accept()
    q = _subscribe_node_events(node_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _unsubscribe_node_events(node_id, q)


@router.websocket("/{node_id}/stats")
async def node_stats_ws(node_id: int, websocket: WebSocket):
    """Browser-facing WebSocket: streams the remote node's system stats."""
    await websocket.accept()
    agent_ws_conn = _node_ws_connections.get(node_id)
    if not agent_ws_conn:
        await websocket.send_json({"error": "Node not connected via WebSocket"})
        await websocket.close(code=1011)
        return

    # Replay buffered history so the chart populates immediately
    for snapshot in list(_node_stats_history.get(node_id, [])):
        try:
            await websocket.send_text(snapshot)
        except Exception:
            return

    stream_id = secrets.token_hex(8)
    q = subscribe_node_stream(stream_id)
    try:
        await agent_ws_conn.send_json({
            "type": "command",
            "command": {
                "id": -1,
                "command_type": "node_stats_stream",
                "payload": {"stream_id": stream_id},
            },
        })
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=30.0)
                _record_node_stats(node_id, data)
                await websocket.send_text(data)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        unsubscribe_node_stream(stream_id, q)
        try:
            if _node_ws_connections.get(node_id) is agent_ws_conn:
                await agent_ws_conn.send_json({"type": "cancel_stream", "stream_id": stream_id})
        except Exception:
            pass


@router.websocket("/{node_id}/commands/live")
async def node_commands_live_ws(node_id: int, websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    """Browser-facing WebSocket: streams live command queue updates."""
    await websocket.accept()

    # Send initial snapshot
    result = await db.execute(
        select(NodeCommand)
        .where(and_(NodeCommand.node_id == node_id, NodeCommand.status.in_(["queued", "in_progress"])))
        .order_by(NodeCommand.created_at.asc())
        .limit(50)
    )
    active_cmds = result.scalars().all()
    await websocket.send_json({
        "type": "snapshot",
        "commands": [
            {
                "id": c.id,
                "command_type": c.command_type,
                "app_id": c.app_id,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in active_cmds
        ],
    })

    q = _subscribe_node_events(node_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                if event.get("type") == "command_update":
                    await websocket.send_json({"type": "command_updated", "command": event})
                elif event.get("type") == "ping":
                    await websocket.send_json({"type": "ping"})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _unsubscribe_node_events(node_id, q)
