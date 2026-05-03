import os
import sys
import json
import time
import socket
import secrets
import asyncio
import argparse
import platform
import subprocess
import logging
from typing import Optional, Any, Tuple, Dict, Callable
from dataclasses import dataclass, field

import httpx
import websockets
import psutil

# Configuration
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_BASE_DIR, "agent_state.json")
AGENT_LOG_FILE = os.path.expanduser("~/.cloudbase/logs/node-agent.log")
_LOCAL_API_BASE = "http://127.0.0.1:7823"

os.makedirs(os.path.dirname(AGENT_LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(AGENT_LOG_FILE), logging.StreamHandler()],
)

def _agent_log(message: str) -> None:
    logging.info(message)

@dataclass
class AgentState:
    main_url: str
    auth_token: str
    node_id: int
    node_name: str
    heartbeat_interval: int = 15
    app_id_map: dict[str, int] = field(default_factory=dict)

def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    return url

def _ws_url(main_url: str) -> str:
    url = _normalize_url(main_url)
    if url.startswith("https"):
        return url.replace("https://", "wss://") + "/api/nodes/ws/agent"
    return url.replace("http://", "ws://") + "/api/nodes/ws/agent"

def _local_ws_url(path: str) -> str:
    # Assuming local Cloudbase runs on 7823
    return f"ws://127.0.0.1:7823{path}"

def _load_agent_token() -> Optional[str]:
    token = os.environ.get("AGENT_TOKEN")
    if token:
        return token
    token_file = os.path.expanduser("~/.cloudbase/agent_token")
    if os.path.exists(token_file):
        with open(token_file) as f:
            tok = f.read().strip()
        if tok:
            return tok
    return None

def _load_state() -> Optional[AgentState]:
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
            return AgentState(**data)
    except Exception as e:
        _agent_log(f"[agent] Error loading state: {e}")
        return None

def _save_state(state: AgentState) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state.__dict__, f)
    except Exception as e:
        _agent_log(f"[agent] Error saving state: {e}")

def _collect_node_metrics() -> dict:
    try:
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent,
        }
    except Exception:
        return {}

def _collect_system_info() -> dict:
    import socket as _socket
    info: dict = {}
    try:
        info["hostname"]  = _socket.gethostname()
        info["os"]        = platform.platform()
        info["os_short"]  = f"{platform.system()} {platform.release()}"
        info["arch"]      = platform.machine()
        
        try:
            info["uptime_secs"] = round(time.time() - psutil.boot_time())
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
        except Exception: pass

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

        try:
            addrs = psutil.net_if_addrs()
            ips = []
            for iface, addr_list in addrs.items():
                if iface.startswith("lo") or "loopback" in iface.lower(): continue
                for a in addr_list:
                    if a.family == _socket.AF_INET: ips.append(a.address)
            if ips:
                info["ip"] = ips[0]
                info["ip_all"] = ips
        except Exception: pass
    except Exception:
        info.setdefault("hostname", _socket.gethostname())
    return info

def _build_capabilities() -> dict:
    return {
        "agent_version": "1.1.0",
        "features": ["streaming_logs", "streaming_stats", "file_management", "nginx_management", "hybrid_mode"],
        "platform": platform.system(),
        "arch": platform.machine(),
    }

async def _register(
    client: httpx.AsyncClient,
    main_url: str,
    invite_code: str,
    node_name: str,
    public_host: Optional[str] = None,
    heartbeat_interval: int = 15,
) -> AgentState:
    payload = {
        "invite_code": invite_code,
        "name": node_name,
        "public_host": public_host,
        "api_base_url": _LOCAL_API_BASE,
        "heartbeat_interval": heartbeat_interval,
        "capabilities": _build_capabilities(),
        "metadata_json": _collect_system_info(),
    }
    response = await client.post(f"{main_url}/api/nodes/agent/register", json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    return AgentState(
        main_url=main_url,
        auth_token=data["auth_token"],
        node_id=data["node"]["id"],
        node_name=data["node"]["name"],
        heartbeat_interval=data["node"]["heartbeat_interval"],
    )

async def _report_result(
    client: httpx.AsyncClient,
    state: AgentState,
    command_id: int,
    *,
    status: str,
    result: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    headers = {"X-Node-Token": state.auth_token}
    payload = {
        "status": status,
        "result": result,
        "error_message": error_message,
    }
    response = await client.post(
        f"{state.main_url}/api/nodes/agent/commands/{command_id}/result",
        json=payload,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()

# ─── Orphan cleanup ───────────────────────────────────────────────────────────

async def _cleanup_orphaned_apps(client: httpx.AsyncClient, state: AgentState) -> None:
    """Delete local apps that no longer exist on the main server for this node."""
    agent_token = _load_agent_token()
    local_headers = {"X-Agent-Token": agent_token}
    node_headers = {"X-Node-Token": state.auth_token}
    try:
        local_resp = await client.get(f"{_LOCAL_API_BASE}/api/apps", headers=local_headers, timeout=10)
        main_resp = await client.get(f"{state.main_url}/api/nodes/agent/my-apps", headers=node_headers, timeout=10)
        if local_resp.status_code != 200 or main_resp.status_code != 200:
            return
        local_apps = {a["name"]: a["id"] for a in local_resp.json()}
        main_names = {a["name"] for a in main_resp.json().get("apps", [])}

        # Safety: if main returned an empty list but we have local apps, the main
        # server may have just restarted and not yet restored state — skip cleanup.
        if not main_names and local_apps:
            _agent_log("[cleanup] Main returned no apps but local apps exist — skipping orphan cleanup (main may be restarting)")
            return

        orphans = {name: lid for name, lid in local_apps.items() if name not in main_names}
        if not orphans:
            return
        _agent_log(f"[cleanup] Found {len(orphans)} orphaned app(s): {list(orphans)}")
        for name, local_id in orphans.items():
            try:
                del_resp = await client.delete(f"{_LOCAL_API_BASE}/api/apps/{local_id}", headers=local_headers, timeout=30)
                if del_resp.status_code in (200, 204):
                    _agent_log(f"[cleanup] Removed orphaned app '{name}' (local_id={local_id})")
                else:
                    _agent_log(f"[cleanup] Failed to remove '{name}': HTTP {del_resp.status_code}")
            except Exception as e:
                _agent_log(f"[cleanup] Error removing '{name}': {e}")
    except Exception as e:
        _agent_log(f"[cleanup] Orphan check failed: {e}")


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def _resolve_local_id(client, state, main_app_id, payload, headers) -> int:
    local_id = state.app_id_map.get(str(main_app_id))
    if local_id: return local_id
    app_name = payload.get("name") or payload.get("app_name")
    if app_name:
        resp = await client.get(f"{_LOCAL_API_BASE}/api/apps", headers=headers, timeout=10)
        if resp.status_code == 200:
            for a in resp.json():
                if a.get("name") == app_name:
                    found = int(a["id"])
                    state.app_id_map[str(main_app_id)] = found
                    _save_state(state)
                    return found
    raise ValueError(f"Unknown app (id={main_app_id}, name={app_name or '?'})")

async def cmd_get_nginx_config(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/nginx-config", headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_save_nginx_config(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.put(f"{_LOCAL_API_BASE}/api/apps/{local_id}/nginx-config", json=payload, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_update_app(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    update_payload = dict(payload or {})
    update_payload.pop("app_id", None)
    update_payload.pop("app_name", None)
    resp = await client.put(f"{_LOCAL_API_BASE}/api/apps/{local_id}", json=update_payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()

async def cmd_git_pull(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    body = {"commit": payload.get("commit")} if payload and payload.get("commit") else None
    resp = await client.post(f"{_LOCAL_API_BASE}/api/apps/{local_id}/pull", json=body, headers=headers, timeout=180)
    resp.raise_for_status()
    return resp.json()

async def cmd_list_git_commits(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    limit = payload.get("limit") or 20
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/commits", params={"limit": limit}, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

async def cmd_rebuild_app(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    url = f"{_LOCAL_API_BASE}/api/apps/{local_id}/rebuild"
    # Rebuild can briefly return 5xx during overlapping local operations; retry a few times.
    retry_delays = (0.2, 0.5, 1.0)
    last_error: Optional[str] = None

    for attempt in range(len(retry_delays) + 1):
        resp = await client.post(url, headers=headers, timeout=900)
        if 200 <= resp.status_code < 300:
            return resp.json()

        body_text = (resp.text or "").strip()
        last_error = f"HTTP {resp.status_code}: {body_text[:400]}"

        is_retryable = 500 <= resp.status_code < 600 and attempt < len(retry_delays)
        if is_retryable:
            await asyncio.sleep(retry_delays[attempt])
            continue

        raise RuntimeError(f"Rebuild request failed: {last_error}")

    raise RuntimeError(f"Rebuild request failed: {last_error or 'unknown error'}")

async def cmd_discover_app_certs(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/certs", headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_save_maintenance_pages(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.put(f"{_LOCAL_API_BASE}/api/apps/{local_id}/maintenance-pages", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

async def cmd_toggle_maintenance_mode(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.post(f"{_LOCAL_API_BASE}/api/apps/{local_id}/maintenance-mode/toggle", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

async def cmd_toggle_update_mode(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.post(f"{_LOCAL_API_BASE}/api/apps/{local_id}/update-mode/toggle", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

async def cmd_list_files(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    path = payload.get("path") or ""
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/files", params={"path": path}, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_get_file_content(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    path = payload.get("path") or ""
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/files/content", params={"path": path}, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_app_lifecycle(client, state, main_id, payload, headers, action):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    url = f"{_LOCAL_API_BASE}/api/apps/{local_id}/{action}"
    retry_delays = (0.2, 0.5, 1.0)

    for attempt in range(len(retry_delays) + 1):
        resp = await client.post(url, headers=headers, timeout=120)
        if 200 <= resp.status_code < 300:
            body: dict[str, Any] = {}
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}

            result = {"local_app_id": local_id, "action": action}
            result.update(body)
            return result

        body_text = (resp.text or "").strip().lower()

        # Treat idempotent lifecycle conflicts as success.
        if resp.status_code == 400:
            if action == "start" and "already running" in body_text:
                return {"local_app_id": local_id, "action": action, "note": "already running"}
            if action == "stop" and "not running" in body_text:
                return {"local_app_id": local_id, "action": action, "note": "already stopped"}

        # Retry transient server-side failures.
        if 500 <= resp.status_code < 600 and attempt < len(retry_delays):
            await asyncio.sleep(retry_delays[attempt])
            continue

        raise RuntimeError(f"{action} request failed: HTTP {resp.status_code}: {(resp.text or '').strip()[:400]}")

    raise RuntimeError(f"{action} request failed after retries")

async def cmd_delete_app(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.delete(f"{_LOCAL_API_BASE}/api/apps/{local_id}", headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()

async def cmd_get_logs_tail(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    limit = payload.get("limit") or 200
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/logs/tail", params={"limit": limit}, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_get_stats(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    resp = await client.get(f"{_LOCAL_API_BASE}/api/apps/{local_id}/stats", headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

async def cmd_deploy_app(client, state, main_id, payload, headers):
    resp = await client.post(f"{_LOCAL_API_BASE}/api/apps", json=payload, headers=headers, timeout=300)
    if resp.status_code == 400 and "already exists" in resp.text:
        list_resp = await client.get(f"{_LOCAL_API_BASE}/api/apps", headers=headers, timeout=20)
        local_app = next((a for a in list_resp.json() if a.get("name") == payload.get("name")), None)
        if not local_app: resp.raise_for_status()
    else:
        resp.raise_for_status()
        local_app = resp.json()
    
    local_id = int(local_app["id"])
    if main_id:
        state.app_id_map[str(main_id)] = local_id
        _save_state(state)
    return {
        "local_app_id": local_id,
        "status": local_app.get("status"),
        "working_dir": local_app.get("working_dir"),
        "nginx_enabled": local_app.get("nginx_enabled"),
    }

async def cmd_upload_cert(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    import base64
    content = base64.b64decode(payload["content_b64"])
    files = {"file": (payload["filename"], content)}
    multipart_headers = {"X-Agent-Token": headers["X-Agent-Token"]}
    resp = await client.post(f"{_LOCAL_API_BASE}/api/apps/{local_id}/certs/upload", files=files, headers=multipart_headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

async def cmd_get_agent_logs(client, state, main_id, payload, headers):
    log_file = os.path.expanduser("~/.cloudbase/logs/node-agent.log")
    lines = payload.get("lines") or 100
    if not os.path.exists(log_file):
        return {"lines": []}
    with open(log_file) as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip() for l in all_lines[-lines:]]}

async def cmd_start_replica(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    body = {
        "replica_id": payload["replica_id"],
        "internal_port": payload.get("internal_port", 8000),
        "external_port": payload["external_port"],
        "env_vars": payload.get("env_vars") or {},
        "docker_options": payload.get("docker_options"),
    }
    resp = await client.post(
        f"{_LOCAL_API_BASE}/api/apps/{local_id}/replicas/run-remote",
        json=body, headers=headers, timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


async def cmd_stop_replica(client, state, main_id, payload, headers):
    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
    replica_id = payload["replica_id"]
    resp = await client.delete(
        f"{_LOCAL_API_BASE}/api/apps/{local_id}/replicas/{replica_id}/stop-remote",
        headers=headers, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ─── Command Dispatcher ───────────────────────────────────────────────────────

COMMAND_HANDLERS: Dict[str, Callable] = {
    "get_nginx_config": cmd_get_nginx_config,
    "save_nginx_config": cmd_save_nginx_config,
    "update_app": cmd_update_app,
    "git_pull": cmd_git_pull,
    "list_git_commits": cmd_list_git_commits,
    "rebuild_app": cmd_rebuild_app,
    "discover_app_certs": cmd_discover_app_certs,
    "save_maintenance_pages": cmd_save_maintenance_pages,
    "toggle_maintenance_mode": cmd_toggle_maintenance_mode,
    "toggle_update_mode": cmd_toggle_update_mode,
    "list_files": cmd_list_files,
    "get_file_content": cmd_get_file_content,
    "start_app": lambda c, s, m, p, h: cmd_app_lifecycle(c, s, m, p, h, "start"),
    "stop_app": lambda c, s, m, p, h: cmd_app_lifecycle(c, s, m, p, h, "stop"),
    "restart_app": lambda c, s, m, p, h: cmd_app_lifecycle(c, s, m, p, h, "restart"),
    "get_logs_tail": cmd_get_logs_tail,
    "get_stats": cmd_get_stats,
    "deploy_app": cmd_deploy_app,
    "upload_cert": cmd_upload_cert,
    "get_agent_logs": cmd_get_agent_logs,
    "delete_app": cmd_delete_app,
    "start_replica": cmd_start_replica,
    "stop_replica": cmd_stop_replica,
}

async def _execute_command(
    client: httpx.AsyncClient,
    state: AgentState,
    command: dict[str, Any],
    ws_main=None,
    ws_send_fn=None,
) -> tuple[str, Optional[dict[str, Any]], Optional[str]]:
    command_type = command.get("command_type") or "unknown"
    payload = command.get("payload") or {}
    main_id = str(command.get("app_id") or payload.get("app_id") or "")
    
    agent_token = _load_agent_token()
    headers = {"X-Agent-Token": agent_token, "Content-Type": "application/json"}

    try:
        # Handle streaming separately as they need ws_main
        if command_type in ("stream_logs", "stream_stats", "node_stats_stream"):
            if not ws_main: return "failed", None, "Streaming requires WebSocket"

            if command_type == "node_stats_stream":
                local_path = "/ws/system/stats"
            else:
                agent_token = _load_agent_token()
                headers = {"X-Agent-Token": agent_token, "Content-Type": "application/json"}
                try:
                    local_id = await _resolve_local_id(client, state, main_id, payload, headers)
                except Exception as e:
                    _agent_log(f"[agent] stream {command_type}: cannot resolve app {main_id}: {e}")
                    return "failed", None, f"Cannot resolve local app: {e}"
                suffix = "logs" if command_type == "stream_logs" else "stats"
                local_path = f"/ws/apps/{local_id}/{suffix}"

            stream_id = payload.get("stream_id") or secrets.token_hex(8)
            _agent_log(f"[agent] starting {command_type} relay: local={local_path} stream_id={stream_id}")
            # Use ws_send_fn (lock-protected) when available, fall back to raw ws_main.send
            send_fn = ws_send_fn if ws_send_fn is not None else ws_main.send
            task = asyncio.create_task(_stream_relay(send_fn, stream_id, local_path))
            _active_streams[stream_id] = task
            return "streaming", {"stream_id": stream_id}, None

        # Standard commands
        handler = COMMAND_HANDLERS.get(command_type)
        if not handler:
            _agent_log(f"[agent] Unsupported command: {command_type}")
            return "failed", None, f"Unsupported command type: {command_type!r}"
        
        result = await handler(client, state, main_id, payload, headers)
        return "done", result, None

    except Exception as e:
        _agent_log(f"[agent] Command {command_type} failed: {e}")
        return "failed", None, str(e)

# ─── WebSocket / Loops ────────────────────────────────────────────────────────

_active_streams: dict[str, asyncio.Task] = {}
_pending_pings: dict[str, asyncio.Future] = {}

async def _stream_relay(send_fn, stream_id: str, local_ws_path: str) -> None:
    local_url = _local_ws_url(local_ws_path)
    agent_token = _load_agent_token()
    extra_headers = {"X-Agent-Token": agent_token} if agent_token else {}
    frames = 0
    try:
        _agent_log(f"[stream] {stream_id} connecting to {local_url}")
        async with websockets.connect(local_url, max_size=2**22, additional_headers=extra_headers) as local_ws:
            _agent_log(f"[stream] {stream_id} connected, relaying to main WS")
            async for raw in local_ws:
                frames += 1
                await send_fn(json.dumps({
                    "type": "stream_data",
                    "stream_id": stream_id,
                    "data": raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace"),
                }))
                if frames == 1:
                    _agent_log(f"[stream] {stream_id} first frame sent")
    except Exception as e:
        _agent_log(f"[stream] {stream_id} error after {frames} frames: {type(e).__name__}: {e}")
    finally:
        _active_streams.pop(stream_id, None)
        _agent_log(f"[stream] {stream_id} relay ended ({frames} frames total)")

async def _run_websocket_loop(client: httpx.AsyncClient, state: AgentState):
    ws_url = _ws_url(state.main_url)
    attempt = 0
    while True:
        _agent_log(f"[agent] Connecting websocket to {ws_url} (attempt {attempt})")
        try:
            async with websockets.connect(ws_url, max_size=2**22, ping_interval=20, ping_timeout=20) as ws:
                attempt = 0
                _agent_log(f"[agent] WebSocket connected to {ws_url}")
                # Serialise all ws.send() calls — concurrent sends corrupt the connection
                _ws_lock = asyncio.Lock()

                async def _ws_send(payload: str):
                    async with _ws_lock:
                        await ws.send(payload)

                await _ws_send(json.dumps({"type": "auth", "token": state.auth_token}))

                async def _heartbeat_task():
                    _known_statuses: dict[int, str] = {}
                    
                    async def _check_and_report_statuses():
                        try:
                            agent_token = _load_agent_token()
                            headers = {"X-Agent-Token": agent_token}
                            resp = await client.get(f"{_LOCAL_API_BASE}/api/apps", headers=headers, timeout=5)
                            if resp.status_code == 200:
                                for app_info in resp.json():
                                    local_id = int(app_info["id"])
                                    new_status = app_info.get("status") or "stopped"
                                    main_id = next(
                                        (int(k) for k, v in state.app_id_map.items() if v == local_id),
                                        None,
                                    )
                                    if not main_id:
                                        continue
                                    old_status = _known_statuses.get(local_id)
                                    _known_statuses[local_id] = new_status
                                    if old_status != new_status:
                                        _agent_log(f"[agent] app local_id={local_id} main_id={main_id} status {old_status}→{new_status}")
                                        await _ws_send(json.dumps({
                                            "type": "status_update",
                                            "app_id": main_id,
                                            "status": new_status,
                                        }))
                        except Exception as e:
                            _agent_log(f"[agent] app status monitor error: {e}")

                    # Initial check immediately on connect
                    await _check_and_report_statuses()
                    await _cleanup_orphaned_apps(client, state)

                    while True:
                        await asyncio.sleep(state.heartbeat_interval)
                        await _ws_send(json.dumps({
                            "type": "heartbeat",
                            "node_metrics": _collect_node_metrics(),
                            "metadata_json": _collect_system_info(),
                            "capabilities": _build_capabilities()
                        }))
                        await _check_and_report_statuses()

                hb_task = asyncio.create_task(_heartbeat_task())
                try:
                    async for message in ws:
                        data = json.loads(message)
                        mtype = data.get("type")

                        if mtype == "command":
                            cmd = data["command"]
                            status, res, err = await _execute_command(client, state, cmd, ws_main=ws, ws_send_fn=_ws_send)
                            # Streaming commands use id=-1 and have no DB record — never ACK them
                            if status != "streaming" and cmd.get("id", -1) != -1:
                                await _ws_send(json.dumps({
                                    "type": "command_result",
                                    "command_id": cmd["id"],
                                    "status": status,
                                    "result": res,
                                    "error_message": err
                                }))
                        elif mtype == "cancel_stream":
                            sid = data.get("stream_id")
                            if sid and sid in _active_streams:
                                _active_streams[sid].cancel()
                                _active_streams.pop(sid, None)
                        elif mtype == "ping":
                            await _ws_send(json.dumps({"type": "pong", "ping_id": data.get("ping_id", "")}))
                finally:
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass
        except Exception as e:
            backoff = min(0.5 * (2 ** attempt), 60)
            _agent_log(f"[agent] websocket loop error (attempt {attempt}): {type(e).__name__}: {e} — retrying in {backoff:.1f}s")
            attempt += 1
            await asyncio.sleep(backoff)

async def start_agent(main_url=None, invite_code=None, node_name=None, public_host=None, heartbeat_interval=15, exit_after_registration=False):
    state = _load_state()
    if not state:
        # No saved state — must register
        if not main_url or not invite_code:
            _agent_log("[agent] No saved state and no registration args provided — cannot start")
            return
        async with httpx.AsyncClient() as client:
            state = await _register(client, _normalize_url(main_url), invite_code, node_name or socket.gethostname(), public_host, heartbeat_interval)
        _save_state(state)
        _agent_log(f"[agent] Registered node '{state.node_name}' (id={state.node_id})")
        if exit_after_registration:
            _agent_log("[agent] Registration complete, exiting as requested.")
            return
    elif exit_after_registration:
        # Already registered — nothing to do
        _agent_log(f"[agent] Already registered as '{state.node_name}' (id={state.node_id}), skipping re-registration")
        return
    
    async with httpx.AsyncClient() as client:
        await _drain_stale_commands(client, state)
        await _run_websocket_loop(client, state)

async def _drain_stale_commands(client: httpx.AsyncClient, state: AgentState) -> None:
    """Fail any queued commands that were left over from a previous session."""
    headers = {"X-Node-Token": state.auth_token}
    try:
        resp = await client.get(
            f"{state.main_url}/api/nodes/agent/commands",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return
        commands = resp.json().get("commands") or []
        for cmd in commands:
            _agent_log(f"[agent] Draining stale command {cmd['id']} ({cmd.get('command_type')})")
            await _report_result(client, state, cmd["id"], status="failed", error_message="Agent restarted — command discarded")
    except Exception as e:
        _agent_log(f"[agent] Could not drain stale commands: {e}")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-url")
    parser.add_argument("--invite-code")
    parser.add_argument("--node-name")
    parser.add_argument("--public-host")
    parser.add_argument("--heartbeat-interval", type=int, default=15)
    parser.add_argument("--exit-after-registration", action="store_true")
    args = parser.parse_args()
    await start_agent(
        args.main_url, 
        args.invite_code, 
        args.node_name, 
        args.public_host, 
        args.heartbeat_interval,
        exit_after_registration=args.exit_after_registration
    )

if __name__ == "__main__":
    asyncio.run(main())
