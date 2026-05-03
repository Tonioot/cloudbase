import asyncio
import logging
import logging.handlers
import os
import time as _time
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

import psutil

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from database import AsyncSessionLocal, init_db, get_db
from models import Application, User
from routers import applications, files, logs, stats, nodes, audit as audit_router
from env_crypto import decrypt_env
from audit import log_audit
import auth
import nginx_manager as nm
import process_manager as pm
import docker_manager as dm
import token_vault
import node_agent

_LOG_DIR  = os.path.expanduser("~/.cloudbase/logs")
_LOG_FILE = os.path.join(_LOG_DIR, "server.log")
os.makedirs(_LOG_DIR, exist_ok=True)

_log_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_log_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), _log_file_handler],
)

PORT = 7823
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
_COOKIE_NAME = "pdm_token"
_COOKIE_OPTS = dict(httponly=True, samesite="strict", path="/")

# Restart-loop protection: max 5 restarts per 60s per app
_restart_history: dict[int, list[float]] = {}
MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_SECONDS = 60


async def _node_health_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await nodes.ensure_local_node(db)
                await nodes.mark_stale_nodes_offline(db)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)


def _restore_stuck_restart_configs(apps: list[Application]) -> None:
    for app in apps:
        proxy_port = app.external_port or app.port
        if not (app.nginx_enabled and app.domain and proxy_port):
            continue
        if app.maintenance_mode or app.update_mode:
            continue

        config_path = nm.get_config_path(app.name)
        try:
            if not os.path.exists(config_path):
                continue
            with open(config_path, encoding="utf-8") as f:
                current_config = f.read()
        except Exception:
            continue

        if not nm.config_uses_restart_page(current_config):
            continue

        pm._debug(
            f"STARTUP nginx recovery for app {app.id} ({app.name}): "
            "restart page config detected, restoring normal proxy"
        )
        normal_cfg = nm.generate_config(
            app.name,
            app.domain,
            proxy_port,
            app.ssl_cert_path,
            app.ssl_key_path,
            app_id=app.id,
            mode="normal",
        )
        ok, msg = nm.write_nginx_config(app.name, normal_cfg)
        pm._debug(f"STARTUP nginx recovery result for app {app.id} ({app.name}): ok={ok} msg={msg!r}")
        if ok:
            pm._push_line(app.id, "Recovered a stale restart page after Cloudbase startup.")


def _docker_runtime_options(app: Application) -> dict:
    return {
        "cpu_limit": app.docker_cpu_limit,
        "memory_limit_mb": app.docker_memory_limit_mb,
        "read_only_root": bool(app.docker_read_only_root),
        "tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "restart_policy": app.restart_policy or "no",
    }


# ── Background stats collector ────────────────────────────────────────────────
async def _stats_collector():
    """Collect process stats for all running apps every 2 s, push to subscribers."""
    await asyncio.sleep(4)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Application).where(Application.status == "running")
                )
                apps = result.scalars().all()
                from models import Node as _Node
                local_node_result = await db.execute(
                    select(_Node).where(_Node.is_local == True)
                )
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None

            async def _one(a):
                try:
                    if a.node_id and a.node_id != local_node_id:
                        return  # remote node apps stream their own stats via the agent
                    import time as _time
                    timestamp = int(_time.time() * 1000)  # milliseconds
                    if a.use_docker:
                        s = await asyncio.to_thread(pm.get_docker_stats, a.id)
                        if not s:
                            return
                        mem = psutil.virtual_memory()
                        data = {
                            "status": "running",
                            "pid": None,
                            "docker": True,
                            "timestamp": timestamp,
                            **s,
                            "system_cpu_percent": psutil.cpu_percent(interval=None),
                            "system_memory_total_mb": round(mem.total / 1024 / 1024),
                            "system_memory_used_mb":  round(mem.used  / 1024 / 1024),
                            "system_memory_percent":  mem.percent,
                        }
                    else:
                        if not a.pid:
                            return
                        s = await asyncio.to_thread(pm.get_process_stats, a.pid)
                        if not s:
                            return
                        mem = psutil.virtual_memory()
                        data = {
                            "status": "running",
                            "pid": a.pid,
                            "timestamp": timestamp,
                            **s,
                            "system_cpu_percent": psutil.cpu_percent(interval=None),
                            "system_memory_total_mb": round(mem.total / 1024 / 1024),
                            "system_memory_used_mb":  round(mem.used  / 1024 / 1024),
                            "system_memory_percent":  mem.percent,
                        }
                    pm._stats_history.setdefault(a.id, deque(maxlen=60)).append(data)
                    pm._push_stat(a.id, data)
                except Exception:
                    pass

            # Collect all apps concurrently — cpu_percent(interval=0.5) runs in threads
            await asyncio.gather(*[_one(a) for a in apps])

            # Collect stats for local running replicas and push to parent app stream
            async with AsyncSessionLocal() as db:
                from models import ApplicationReplica as _AppReplica
                rep_result = await db.execute(
                    select(_AppReplica).where(
                        _AppReplica.status == "running",
                        (_AppReplica.node_id == local_node_id) | (_AppReplica.node_id.is_(None)),
                    )
                )
                running_replicas = rep_result.scalars().all()

            async def _one_replica(replica):
                try:
                    import time as _time
                    cname = dm.replica_container_name(replica.app_id, replica.id)
                    s = await asyncio.to_thread(dm.get_container_stats_by_name, cname)
                    if not s:
                        return
                    timestamp = int(_time.time() * 1000)
                    data = {
                        "replica_id": replica.id,
                        "timestamp": timestamp,
                        **s,
                    }
                    pm._push_stat(replica.app_id, data)
                except Exception:
                    pass

            await asyncio.gather(*[_one_replica(r) for r in running_replicas])

        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(2)


# ── Historical stats writer ───────────────────────────────────────────────────
async def _stats_history_writer():
    """Every 30s write averaged cpu/mem from the in-memory deque to the DB for long-term history."""
    import datetime as _dt
    from models import StatsHistory
    from sqlalchemy import delete as _delete
    await asyncio.sleep(30)
    _cleanup_counter = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                from models import Node as _Node
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None
                result = await db.execute(
                    select(Application).where(Application.status == "running")
                )
                apps = result.scalars().all()
                for a in apps:
                    if a.node_id and a.node_id != local_node_id:
                        continue
                    recent = pm.get_recent_stats(a.id)
                    if not recent:
                        continue
                    window = recent[-15:]
                    avg_cpu  = sum(s.get("cpu_percent", 0) for s in window) / len(window)
                    avg_mem  = sum(s.get("memory_mb",   0) for s in window) / len(window)
                    avg_net  = sum((s.get("net_rx_mb",  0) or 0) + (s.get("net_tx_mb",   0) or 0) for s in window) / len(window)
                    avg_disk = sum((s.get("disk_read_mb",0) or 0) + (s.get("disk_write_mb",0) or 0) for s in window) / len(window)
                    db.add(StatsHistory(
                        app_id=a.id,
                        timestamp=_dt.datetime.utcnow(),
                        cpu_percent=round(avg_cpu, 2),
                        memory_mb=round(avg_mem, 2),
                        net_mb=round(avg_net, 2),
                        disk_mb=round(avg_disk, 2),
                    ))
                _cleanup_counter += 1
                if _cleanup_counter >= 120:
                    _cleanup_counter = 0
                    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=7)
                    await db.execute(_delete(StatsHistory).where(StatsHistory.timestamp < cutoff))
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(30)


# ── Crash monitor ─────────────────────────────────────────────────────────────
async def _crash_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                from models import Node as _Node
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None
                result = await db.execute(select(Application))
                apps = result.scalars().all()
                for a in apps:
                    if a.status != "running":
                        continue
                    if a.node_id and a.node_id != local_node_id:
                        continue  # remote node apps are managed by their own agent

                    if a.use_docker:
                        alive = pm.is_docker_app_running(a.id)
                    else:
                        if not a.pid:
                            continue
                        alive = pm.is_process_running(a.pid, a.id)

                    if alive:
                        continue

                    policy = a.restart_policy or "no"
                    if policy == "no":
                        a.status = "stopped"
                        a.pid = None
                        pm._push_line(a.id, "⚠ Process exited.")
                        await db.commit()
                        continue

                    now = _time.time()
                    history = _restart_history.setdefault(a.id, [])
                    history[:] = [t for t in history if now - t < RESTART_WINDOW_SECONDS]

                    if len(history) >= MAX_RESTARTS_PER_WINDOW:
                        a.status = "error"
                        a.pid = None
                        pm._push_line(a.id, f"✖ Crashed {MAX_RESTARTS_PER_WINDOW}× in {RESTART_WINDOW_SECONDS}s — giving up.")
                        await db.commit()
                        continue

                    history.append(now)
                    attempt = len(history)
                    pm._push_line(a.id, f"⟳ Process exited — restarting (attempt {attempt}/{MAX_RESTARTS_PER_WINDOW})…")
                    await asyncio.sleep(min(2 ** attempt, 30))

                    try:
                        env_vars = decrypt_env(a.env_vars or "")
                        if a.use_docker:
                            container_id = await asyncio.to_thread(
                                pm.start_docker_app,
                                a.id, a.name, a.working_dir,
                                a.port or 8000, a.external_port or a.port or 8000,
                                env_vars, a.app_type or "unknown", a.start_command or "",
                                _docker_runtime_options(a),
                                False,
                            )
                            a.pid = None
                            a.status = "running"
                            pm._push_line(a.id, f"✓ Container restarted ({container_id[:12]}).")
                        else:
                            final_cmd, env_vars = pm.prepare_app_env(a.start_command, a.working_dir, env_vars)
                            new_pid = pm.start_app(a.id, a.name, final_cmd, a.working_dir, env_vars)
                            a.pid = new_pid
                            a.status = "running"
                            pm._push_line(a.id, f"✓ Restarted (pid {new_pid}).")
                    except Exception as e:
                        a.status = "error"
                        a.pid = None
                        pm._push_line(a.id, f"✖ Restart failed: {e}")
                    await db.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    pm.set_main_loop(asyncio.get_event_loop())
    pm.load_registry()   # restore PID + shell_pid from disk before any process checks

    # First-run: generate a password if none exists
    if not auth.load_hashed_password():
        import secrets
        import string
        alphabet = string.ascii_letters + string.digits
        password = ''.join(secrets.choice(alphabet) for _ in range(16))
        auth.save_hashed_password(auth.hash_password(password))
        print("\n" + "=" * 60)
        print("  Cloudbase — FIRST RUN")
        print(f"  Admin password: {password}")
        print("  Save this — it will not be shown again.")
        print("=" * 60 + "\n")

    # Ensure internal agent token exists (used by node_agent.py)
    auth.get_or_create_agent_token()

    # Recover running apps and auto-start
    async with AsyncSessionLocal() as db:
        local_node = await nodes.ensure_local_node(db)
        local_node_id = local_node.id if local_node else None
        result = await db.execute(select(Application))
        apps = result.scalars().all()
        for a in apps:
            if a.node_id and a.node_id != local_node_id:
                continue  # remote node apps — don't touch their status on the main server
            if a.use_docker:
                alive = pm.is_docker_app_running(a.id)
                if alive:
                    a.status = "running"
                    pm._debug(f"RECOVERY Docker app {a.id} ({a.name}): container running → running")
                    dm.attach_container_log_tailer(a.id, pm.log_buffers, pm._push_line, asyncio.get_event_loop())
                else:
                    a.status = "stopped"
                    pm._debug(f"RECOVERY Docker app {a.id} ({a.name}): container not found → stopped")
            elif a.pid:
                if pm.is_process_running(a.pid, a.id):
                    a.status = "running"
                    pm._debug(f"RECOVERY app {a.id} ({a.name}): pid={a.pid} still alive → running")
                    # Re-attach a log tailer so live streaming works after restart
                    pm.attach_log_tailer(a.id, a.name, proc=None, seek_to_end=True)
                else:
                    recovered = pm.find_process_by_port(a.port) if a.port else None
                    if recovered:
                        pm._debug(f"RECOVERY app {a.id} ({a.name}): pid={a.pid} dead but found port-match pid={recovered}")
                        a.pid = recovered
                        a.status = "running"
                        pm.attach_log_tailer(a.id, a.name, proc=None, seek_to_end=True)
                    else:
                        pm._debug(f"RECOVERY app {a.id} ({a.name}): pid={a.pid} dead, no port match → stopped")
                        a.status = "stopped"
                        a.pid = None

            if a.auto_start and a.status == "stopped" and a.start_command and a.working_dir:
                try:
                    env_vars = decrypt_env(a.env_vars or "")
                    if a.use_docker:
                        container_id = pm.start_docker_app(
                            a.id, a.name, a.working_dir,
                            a.port or 8000, a.external_port or a.port or 8000,
                            env_vars, a.app_type or "unknown", a.start_command,
                            _docker_runtime_options(a),
                            False,
                        )
                        a.pid = None
                        a.status = "running"
                        pm._debug(f"AUTO-START Docker app {a.id} ({a.name}): container {container_id[:12]}")
                    else:
                        final_cmd, env_vars = pm.prepare_app_env(a.start_command, a.working_dir, env_vars)
                        pid = pm.start_app(a.id, a.name, final_cmd, a.working_dir, env_vars)
                        a.pid = pid
                        a.status = "running"
                        pm._debug(f"AUTO-START app {a.id} ({a.name}): new pid={pid}")
                except Exception as exc:
                    pm._debug(f"AUTO-START app {a.id} ({a.name}): FAILED — {exc}")

        await db.commit()
        await asyncio.to_thread(_restore_stuck_restart_configs, apps)

    monitor_task  = asyncio.create_task(_crash_monitor())
    stats_task    = asyncio.create_task(_stats_collector())
    node_task     = asyncio.create_task(_node_health_monitor())
    history_task  = asyncio.create_task(_stats_history_writer())

    # Start node agent if configured (as an integrated background task)
    agent_task = None
    if node_agent._load_state() is not None:
        pm._debug("INIT: Node state detected, starting integrated agent task")
        agent_task = asyncio.create_task(node_agent.start_agent())

    yield
    for task in (monitor_task, stats_task, node_task, history_task, agent_task):
        if not task: continue
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Cloudbase", version="1.0.0", lifespan=lifespan)

# Auth middleware — blocks all /api/ and /ws/ except public paths
_PUBLIC = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
}

# Paths that require admin role for non-GET requests
_ADMIN_WRITE_PREFIXES = ("/api/apps", "/api/nodes", "/api/system", "/api/users")


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/nodes/agent/") or path.startswith("/api/nodes/ws/agent"):
            return await call_next(request)
        if path in _PUBLIC:
            return await call_next(request)
        # Remote node agents may fetch the image export endpoint using their node token
        if path.endswith("/image/export"):
            node_token = request.headers.get("X-Node-Token", "")
            if node_token:
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import select as _select
                    from models import Node as _Node
                    result = await db.execute(
                        _select(_Node).where((_Node.auth_token == node_token) & (_Node.enabled == True))
                    )
                    if result.scalar_one_or_none():
                        return await call_next(request)
        # Browser-facing node WebSocket endpoints — auth checked via cookie below
        # (BaseHTTPMiddleware can't block WS upgrades cleanly; the endpoints themselves are read-only)
        if path.startswith("/api/nodes/") and any(path.endswith(s) for s in ("/events", "/stats", "/commands/live")):
            token = request.cookies.get(_COOKIE_NAME)
            if token and auth.decode_token(token):
                return await call_next(request)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if (path.startswith("/api/") and path not in _PUBLIC) or path.startswith("/ws/"):
            token = request.cookies.get(_COOKIE_NAME)
            if token:
                user = auth.decode_token(token)
                if user:
                    # Viewer role may not perform mutations on protected paths
                    if (request.method not in ("GET", "HEAD", "OPTIONS")
                            and user["role"] != "admin"
                            and any(path.startswith(p) for p in _ADMIN_WRITE_PREFIXES)):
                        return JSONResponse({"detail": "Admin access required"}, status_code=403)
                    return await call_next(request)
            # Allow node_agent.py running locally via X-Agent-Token header
            agent_token = request.headers.get("X-Agent-Token", "")
            if agent_token and auth.verify_agent_token(agent_token):
                return await call_next(request)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)


app.add_middleware(_AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth endpoints (public) ───────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "port": PORT}


@app.get("/api/auth/check")
async def auth_check(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get(_COOKIE_NAME)
    user = auth.decode_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Always return the live role from the database so UI reflects immediate role changes
    result = await db.execute(select(User).where(User.username == user["username"]))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"authenticated": True, "username": db_user.username, "role": db_user.role, "is_superadmin": db_user.username == "admin"}


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    auth._check_rate_limit(request.client.host if request.client else "unknown")
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user or not auth.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.create_access_token(user.username, user.role)
    response.set_cookie(key=_COOKIE_NAME, value=token, max_age=auth.TOKEN_EXPIRE_SECONDS, **_COOKIE_OPTS)
    await log_audit(db, "auth.login", actor=user.username, detail={"ip": request.client.host if request.client else "unknown"})
    await db.commit()
    return {"ok": True, "expires_in": auth.TOKEN_EXPIRE_SECONDS, "role": user.role}


@app.get("/api/auth/session")
async def session_info(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    expires_in = auth.get_token_expires_in(token)
    if expires_in is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"expires_in": expires_in}


@app.post("/api/auth/logout")
async def logout(response: Response, request: Request):
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    actor = auth.get_current_actor(request.cookies.get(_COOKIE_NAME))
    async with AsyncSessionLocal() as db:
        await log_audit(db, "auth.logout", actor=actor)
        await db.commit()
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    password: str


@app.post("/api/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get(_COOKIE_NAME)
    user_info = auth.decode_token(token) if token else None
    if not user_info:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    result = await db.execute(select(User).where(User.username == user_info["username"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = auth.hash_password(req.password)
    # Keep legacy credentials file in sync for the admin user
    if user.username == "admin":
        auth.save_hashed_password(user.password_hash)
    await log_audit(db, "auth.change_password", actor=user.username, detail={"username": user.username})
    await db.commit()
    return {"ok": True}


# ── User management endpoints (admin only) ───────────────────────────────────
class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"


@app.get("/api/users")
async def list_users(_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at.isoformat() if u.created_at else None}
        for u in users
    ]


@app.post("/api/users")
async def create_user(req: CreateUserRequest, admin_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    if req.role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'")
    if len(req.username.strip()) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    existing = await db.execute(select(User).where(User.username == req.username.strip()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    new_user = User(username=req.username.strip(), password_hash=auth.hash_password(req.password), role=req.role)
    db.add(new_user)
    await log_audit(db, "user.create", actor=admin_user["username"], detail={"username": req.username, "role": req.role})
    await db.commit()
    await db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username, "role": new_user.role}


class UpdateUserRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None


@app.put("/api/users/{user_id}")
async def update_user(user_id: int, req: UpdateUserRequest, current_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    detail: dict = {"username": user.username}
    if req.username is not None:
        new_name = req.username.strip()
        if len(new_name) < 2:
            raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Cannot rename the superadmin account")
        existing = await db.execute(select(User).where(User.username == new_name))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already taken")
        detail["old_username"] = user.username
        user.username = new_name
        detail["new_username"] = new_name
    if req.role is not None:
        if req.role not in ("admin", "viewer"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'")
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Cannot change the superadmin role")
        user.role = req.role
        detail["role"] = req.role
    if req.password is not None:
        if len(req.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        user.password_hash = auth.hash_password(req.password)
        if user.username == "admin":
            auth.save_hashed_password(user.password_hash)
        detail["password_changed"] = True
    await log_audit(db, "user.update", actor=current_user["username"], detail=detail)
    await db.commit()
    return {"id": user.id, "username": user.username, "role": user.role}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, current_user: dict = Depends(auth.require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the superadmin account")
    await log_audit(db, "user.delete", actor=current_user["username"], detail={"username": user.username})
    await db.delete(user)
    await db.commit()
    return {"ok": True}


# ── System endpoints ──────────────────────────────────────────────────────────
class CloudbaseNginxRequest(BaseModel):
    domain: str
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None


@app.get("/api/system/nginx-config")
async def get_cloudbase_nginx(_: dict = Depends(auth.require_admin)):
    config_path = os.path.join(nm.NGINX_SITES_DIR, "cloudbase")
    if not os.path.exists(config_path):
        return {"exists": False, "content": None}
    with open(config_path) as f:
        content = f.read()
    return {"exists": True, "content": content, "path": config_path}


@app.post("/api/system/nginx-config")
async def apply_cloudbase_nginx(req: CloudbaseNginxRequest, _: dict = Depends(auth.require_admin)):
    config = nm.generate_config("cloudbase", req.domain, PORT, req.ssl_cert_path, req.ssl_key_path)
    ok, msg = nm.write_nginx_config("cloudbase", config)
    return {"ok": ok, "message": msg, "preview": config}


@app.post("/api/system/nginx-default-catchall")
async def apply_nginx_default_catchall(_: dict = Depends(auth.require_admin)):
    ok, msg = nm.write_default_catch_all()
    return {"ok": ok, "message": msg}


@app.post("/api/system/certs/upload")
async def upload_system_cert(file: UploadFile = File(...), _: dict = Depends(auth.require_admin)):
    allowed_exts = {".pem", ".crt", ".cer", ".key"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(400, "Only .pem, .crt, .cer, .key files are allowed")
    safe_name = os.path.basename(file.filename or "cert").replace("..", "").lstrip("/")
    dest_dir = os.path.expanduser("~/.cloudbase/certs")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    with open(dest_path, "wb") as f:
        f.write(await file.read())
    return {"path": dest_path}


# ── GitHub token vault ────────────────────────────────────────────────────────
class SaveTokenRequest(BaseModel):
    label: str
    token: str


@app.get("/api/system/github-tokens")
async def list_github_tokens(_: dict = Depends(auth.require_admin)):
    return token_vault.list_hints()


@app.post("/api/system/github-tokens")
async def save_github_token(req: SaveTokenRequest, _: dict = Depends(auth.require_admin)):
    if not req.label.strip():
        raise HTTPException(400, "Label is required")
    if not req.token.strip():
        raise HTTPException(400, "Token is required")
    token_vault.add(req.label.strip(), req.token.strip())
    return {"ok": True}


@app.delete("/api/system/github-tokens/{token_id}")
async def delete_github_token(token_id: str, _: dict = Depends(auth.require_admin)):
    token_vault.remove(token_id)
    return {"ok": True}


# /value endpoint intentionally omitted — raw tokens are resolved server-side only.


@app.get("/api/system/debug-log")
async def get_debug_log(lines: int = 200, _: dict = Depends(auth.require_admin)):
    try:
        with open(pm.DEBUG_LOG_PATH) as f:
            all_lines = f.readlines()
        return {"lines": [l.rstrip() for l in all_lines[-lines:]]}
    except FileNotFoundError:
        return {"lines": ["(debug log is empty — no events recorded yet)"]}


@app.get("/api/system/logs")
async def get_server_logs(lines: int = 500, _: dict = Depends(auth.require_admin)):
    try:
        with open(_LOG_FILE, encoding="utf-8") as f:
            all_lines = f.readlines()
        return {"lines": [l.rstrip() for l in all_lines[-lines:]]}
    except FileNotFoundError:
        return {"lines": []}


from fastapi import WebSocket as _WS, WebSocketDisconnect as _WSD

@app.websocket("/ws/system/server-logs")
async def stream_server_logs(websocket: _WS):
    await websocket.accept()
    try:
        # Send existing lines first
        try:
            with open(_LOG_FILE, encoding="utf-8") as f:
                existing = f.readlines()[-300:]
            for line in existing:
                await websocket.send_text(line.rstrip())
        except FileNotFoundError:
            pass

        # Then tail the file for new lines
        with open(_LOG_FILE, encoding="utf-8") as f:
            f.seek(0, 2)  # seek to end
            while True:
                line = f.readline()
                if line:
                    await websocket.send_text(line.rstrip())
                else:
                    await asyncio.sleep(0.5)
    except (_WSD, Exception):
        pass








# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(applications.router)
app.include_router(nodes.router)
app.include_router(files.router)
app.include_router(logs.router)
app.include_router(stats.router)
app.include_router(audit_router.router)

# ── Static / SPA ──────────────────────────────────────────────────────────────
if os.path.isdir(FRONTEND_DIR):
    app.mount("/css", StaticFiles(directory=os.path.join(FRONTEND_DIR, "css")), name="css")
    app.mount("/js",  StaticFiles(directory=os.path.join(FRONTEND_DIR, "js")),  name="js")

    @app.get("/favicon.png", include_in_schema=False)
    async def favicon():
        return FileResponse(os.path.join(FRONTEND_DIR, "cloudbase.png"), media_type="image/svg+xml")

    @app.get("/cloudbase.png", include_in_schema=False)
    async def logo():
        return FileResponse(os.path.join(FRONTEND_DIR, "cloudbase.png"), media_type="image/png")

    @app.get("/login", include_in_schema=False)
    @app.get("/login.html", include_in_schema=False)
    async def login_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))

    @app.get("/app.html", include_in_schema=False)
    async def app_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "app.html"))

    @app.get("/node.html", include_in_schema=False)
    async def node_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "node.html"))

    @app.get("/audit.html", include_in_schema=False)
    async def audit_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "audit.html"))

    @app.get("/", include_in_schema=False)
    async def index_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def catch_all(full_path: str):
        # Never catch API or WebSocket paths
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Paths with a file extension that aren't known static assets → 404
        # (prevents hosting panel paths like /cgi-bin/, /wp-admin/, etc. from redirecting)
        _, ext = os.path.splitext(full_path)
        if ext and ext not in {".html", ".htm"}:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Known SPA routes — serve the shell
        _SPA_PREFIXES = ("", "app", "node", "login", "settings", "nodes")
        root = full_path.split("/")[0]
        if root not in _SPA_PREFIXES:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
