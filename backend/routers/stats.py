import asyncio
import collections
import json
import logging
import secrets
import time as _time
from collections import deque
import psutil
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

import process_manager as pm
from database import AsyncSessionLocal
from models import Application, ApplicationReplica, Node
from routers.nodes import (
    ensure_local_node, queue_node_command, wait_for_node_command,
    subscribe_node_stream, unsubscribe_node_stream,
    _node_ws_connections,
)

log = logging.getLogger("cloudbase.stats")
router = APIRouter(tags=["stats"])

_local_stats_history: deque = deque(maxlen=60)


def _aggregate_frames(frames: list[dict]) -> dict:
    """Merge stats from multiple nodes/replicas into one aggregated frame.

    CPU → average across nodes (each node already reports % of its own cores).
    Memory / network / disk → sum (additive resources).
    """
    n = len(frames)
    if n == 0:
        return {"status": "stopped"}
    if n == 1:
        return frames[0]

    def _sum(key):
        return round(sum(f.get(key) or 0 for f in frames), 2)

    def _avg(key):
        vals = [f.get(key) for f in frames if f.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _max(key):
        vals = [f.get(key) for f in frames if f.get(key) is not None]
        return max(vals) if vals else None

    agg = {
        "status":       "running",
        "docker":       any(f.get("docker") for f in frames),
        "timestamp":    frames[-1].get("timestamp"),
        "cpu_percent":  _avg("cpu_percent"),
        "memory_mb":    _sum("memory_mb"),
        "memory_vms_mb": _sum("memory_vms_mb"),
        "net_rx_mb":    _sum("net_rx_mb"),
        "net_tx_mb":    _sum("net_tx_mb"),
        "disk_read_mb": _sum("disk_read_mb"),
        "disk_write_mb": _sum("disk_write_mb"),
        "uptime_seconds": _max("uptime_seconds"),
        "system_cpu_percent": _avg("system_cpu_percent"),
        # Aggregation metadata consumed by the frontend
        "_instance_count": n,
        "_aggregated": True,
    }
    return {k: v for k, v in agg.items() if v is not None}


async def _open_remote_stream(node: Node, app_id: int, app_name: str, out_q: asyncio.Queue) -> asyncio.Task:
    """Start a background task that reads stats from one remote node and puts frames on out_q."""

    async def _run():
        caps = json.loads(node.capabilities or "{}")
        features = caps.get("features") or []
        agent_ws = _node_ws_connections.get(node.id)

        if "streaming_stats" in features and agent_ws is not None:
            stream_id = secrets.token_hex(8)
            q = subscribe_node_stream(stream_id)
            try:
                await agent_ws.send_json({
                    "type": "command",
                    "command": {
                        "id": -1,
                        "command_type": "stream_stats",
                        "payload": {"app_id": app_id, "app_name": app_name, "stream_id": stream_id},
                    },
                })
                while True:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    parsed = json.loads(data) if isinstance(data, str) else data
                    await out_q.put((node.id, parsed))
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.debug("remote stream node %d ended: %s", node.id, e)
            finally:
                unsubscribe_node_stream(stream_id, q)
                try:
                    if _node_ws_connections.get(node.id) is agent_ws:
                        await agent_ws.send_json({"type": "cancel_stream", "stream_id": stream_id})
                except Exception:
                    pass
        else:
            # Poll fallback
            while True:
                try:
                    async with AsyncSessionLocal() as poll_db:
                        cmd = await queue_node_command(
                            poll_db,
                            node_id=node.id,
                            app_id=app_id,
                            command_type="get_stats",
                            payload={"app_id": app_id, "app_name": app_name},
                        )
                        done = await wait_for_node_command(poll_db, cmd.id, timeout_seconds=20)
                    if done.status == "done":
                        payload = json.loads(done.result or "{}") if done.result else {}
                        await out_q.put((node.id, payload))
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.debug("remote poll node %d error: %s", node.id, e)
                    await asyncio.sleep(2)

    return asyncio.create_task(_run())


@router.websocket("/ws/apps/{app_id}/stats")
async def stream_stats(app_id: int, websocket: WebSocket):
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Application).where(Application.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await websocket.send_json({"status": "stopped", "error": "App not found"})
            await websocket.close()
            return
        local_node = await ensure_local_node(db)

        # Collect all distinct nodes that have running replicas for this app
        rep_result = await db.execute(
            select(ApplicationReplica).where(
                ApplicationReplica.app_id == app_id,
                ApplicationReplica.status.in_(["running", "starting"]),
            )
        )
        replicas = rep_result.scalars().all()

        # Unique node IDs; None / local node are treated as local
        remote_node_ids = {r.node_id for r in replicas if r.node_id}
        nodes_result = await db.execute(select(Node).where(Node.id.in_(remote_node_ids)))
        all_nodes: dict[int, Node] = {n.id: n for n in nodes_result.scalars().all()}
        remote_nodes = [n for n in all_nodes.values() if not n.is_local]
        has_local = any(r.node_id is None or all_nodes.get(r.node_id, local_node).is_local for r in replicas)

    app_id_val = app.id
    app_name_val = app.name
    instance_count = len(replicas) if replicas else 1

    # ── Pure local case (fast path) ──────────────────────────────────────────
    if not remote_nodes and has_local:
        q = pm.subscribe_stats(app_id_val)
        for point in pm.get_recent_stats(app_id_val):
            if "cpu_percent" in point:
                point.setdefault("_instance_count", instance_count)
            try:
                await websocket.send_json(point)
            except Exception:
                pm.unsubscribe_stats(app_id_val, q)
                return
        try:
            while True:
                data = await q.get()
                if "cpu_percent" in data:
                    data.setdefault("_instance_count", instance_count)
                await websocket.send_json(data)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            pm.unsubscribe_stats(app_id_val, q)
        return

    # ── Mixed / remote case — aggregate across nodes ─────────────────────────
    # latest frame per node; local node uses key None
    latest: dict = {}
    remote_q: asyncio.Queue = asyncio.Queue()
    tasks: list[asyncio.Task] = []

    for rn in remote_nodes:
        tasks.append(await _open_remote_stream(rn, app_id_val, app_name_val, remote_q))

    # Local node stats subscriber (if any local replicas)
    local_q = pm.subscribe_stats(app_id_val) if has_local else None

    # Flush local history to client immediately
    if local_q:
        for point in pm.get_recent_stats(app_id_val):
            if "cpu_percent" in point:
                latest[None] = point
        if latest:
            merged = _aggregate_frames(list(latest.values()))
            merged["_instance_count"] = instance_count
            pm._stats_history.setdefault(app_id_val, collections.deque(maxlen=60)).append(merged)
            try:
                await websocket.send_json(merged)
            except Exception:
                pass

    async def _drain_local():
        """Feed local stats queue into remote_q under key None."""
        if not local_q:
            return
        try:
            while True:
                data = await local_q.get()
                await remote_q.put((None, data))
        except asyncio.CancelledError:
            pass

    drain_task = asyncio.create_task(_drain_local())

    try:
        while True:
            try:
                node_id, frame = await asyncio.wait_for(remote_q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                continue

            if frame.get("status") == "stopped":
                # Only propagate stopped if ALL nodes report stopped
                latest.pop(node_id, None)
                if not latest:
                    await websocket.send_json({"status": "stopped"})
                continue

            if "cpu_percent" not in frame:
                # Per-replica frame or metadata — pass through as-is
                await websocket.send_json(frame)
                continue

            latest[node_id] = frame
            merged = _aggregate_frames(list(latest.values()))
            merged["_instance_count"] = instance_count
            # Buffer for history writer
            pm._stats_history.setdefault(app_id_val, collections.deque(maxlen=60)).append(merged)
            await websocket.send_json(merged)

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        drain_task.cancel()
        for t in tasks:
            t.cancel()
        if local_q:
            pm.unsubscribe_stats(app_id_val, local_q)


async def _get_app_node(app: Application, db, local_node: Node) -> Node:
    """Return the node of the first running replica, falling back to local."""
    result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app.id,
            ApplicationReplica.status == "running",
            ApplicationReplica.node_id.isnot(None),
        ).limit(1)
    )
    replica = result.scalar_one_or_none()
    if replica and replica.node_id:
        node_result = await db.execute(select(Node).where(Node.id == replica.node_id))
        node = node_result.scalar_one_or_none()
        if node:
            return node
    return local_node


@router.websocket("/ws/system/stats")
async def stream_system_stats(websocket: WebSocket):
    await websocket.accept()
    try:
        # Replay history so charts populate immediately on connect
        for snapshot in list(_local_stats_history):
            await websocket.send_json(snapshot)

        psutil.cpu_percent(interval=None)
        while True:
            import time
            timestamp = int(time.time() * 1000)  # milliseconds
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
            payload = {
                "timestamp": timestamp,
                "cpu_percent": cpu,
                "memory_total_mb": round(mem.total / 1024 / 1024),
                "memory_used_mb": round(mem.used / 1024 / 1024),
                "memory_percent": mem.percent,
                "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
                "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
                "disk_percent": disk.percent,
            }
            _local_stats_history.append(payload)
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
