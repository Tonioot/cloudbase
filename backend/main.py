import asyncio
import json
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
from fastapi.middleware.gzip import GZipMiddleware
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

                    # Fetch running local replicas alongside apps so _one() can aggregate them
            async with AsyncSessionLocal() as _rep_db:
                from models import ApplicationReplica as _AppReplica
                rep_result = await _rep_db.execute(
                    select(_AppReplica).where(
                        _AppReplica.status == "running",
                        (_AppReplica.node_id == local_node_id) | (_AppReplica.node_id.is_(None)),
                    )
                )
                running_replicas = rep_result.scalars().all()

            replicas_by_app: dict[int, list] = {}
            for _r in running_replicas:
                replicas_by_app.setdefault(_r.app_id, []).append(_r)

            async def _one(a):
                try:
                    if a.node_id and a.node_id != local_node_id:
                        return  # remote node apps stream their own stats via the agent
                    import time as _time
                    timestamp = int(_time.time() * 1000)  # milliseconds
                    if a.use_docker:
                        app_replicas = replicas_by_app.get(a.id, [])
                        if app_replicas:
                            # Instance-based: aggregate stats from all running replicas
                            stats_list = await asyncio.gather(*[
                                asyncio.to_thread(
                                    dm.get_container_stats_by_name,
                                    dm.replica_container_name(a.id, r.id),
                                )
                                for r in app_replicas
                            ])
                            stats_list = [s for s in stats_list if s]
                            if not stats_list:
                                return
                            n = len(stats_list)
                            s = {
                                "cpu_percent":    round(sum(s.get("cpu_percent",    0) for s in stats_list) / n, 2),
                                "memory_mb":      round(sum(s.get("memory_mb",      0) for s in stats_list), 2),
                                "memory_vms_mb":  round(sum(s.get("memory_vms_mb",  0) for s in stats_list), 2),
                                "net_rx_mb":      round(sum(s.get("net_rx_mb",      0) for s in stats_list), 2),
                                "net_tx_mb":      round(sum(s.get("net_tx_mb",      0) for s in stats_list), 2),
                                "disk_read_mb":   round(sum(s.get("disk_read_mb",   0) for s in stats_list), 2),
                                "disk_write_mb":  round(sum(s.get("disk_write_mb",  0) for s in stats_list), 2),
                                "uptime_seconds": max((s.get("uptime_seconds", 0) for s in stats_list), default=0),
                            }
                        else:
                            # Legacy single-container model
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

            # Per-replica stats: store latest snapshot for local replicas
            async def _one_replica(replica):
                try:
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
                    pm.set_replica_stats(replica.id, data)
                    pm._push_stat(replica.app_id, data)
                except Exception:
                    pass

            await asyncio.gather(*[_one_replica(r) for r in running_replicas])

        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(2)


# ── Remote replica stats poller ───────────────────────────────────────────────
async def _remote_replica_stats_poller():
    """Poll remote nodes every 15s to keep per-replica stats fresh for the instances tab.

    Only fires when no stats WebSocket relay is already streaming for a given app
    (the relay in stats.py feeds _replica_stats in real-time when the stats tab is open).
    Uses one get_stats command per (node, app) pair — not per replica — to keep load low.
    """
    await asyncio.sleep(20)
    while True:
        try:
            from routers.nodes import queue_node_command, wait_for_node_command, _node_ws_connections
            from models import ApplicationReplica as _AR, Application as _App, Node as _Node

            async with AsyncSessionLocal() as db:
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None

                rep_result = await db.execute(
                    select(_AR).where(
                        _AR.status.in_(["running", "starting"]),
                        _AR.node_id.isnot(None),
                        _AR.node_id != local_node_id,
                    )
                )
                remote_replicas = rep_result.scalars().all()

            if not remote_replicas:
                await asyncio.sleep(15)
                continue

            # Group by (node_id, app_id) — one command per pair
            from collections import defaultdict
            groups: dict[tuple[int, int], list] = defaultdict(list)
            for r in remote_replicas:
                groups[(r.node_id, r.app_id)].append(r)

            async def _poll_group(node_id: int, app_id: int, replicas: list):
                # Skip if no agent WS — command would just queue forever
                if node_id not in _node_ws_connections:
                    return
                try:
                    async with AsyncSessionLocal() as db:
                        app_r = await db.execute(select(_App).where(_App.id == app_id))
                        app_obj = app_r.scalar_one_or_none()
                        if not app_obj:
                            return
                        app_name = app_obj.name
                        cmd = await queue_node_command(
                            db,
                            node_id=node_id,
                            app_id=app_id,
                            command_type="get_stats",
                            payload={"app_id": app_id, "app_name": app_name},
                            allow_existing_inflight=True,
                        )
                        cmd_id = cmd.id
                    # wait_for_node_command uses its own sessions internally
                    async with AsyncSessionLocal() as wait_db:
                        done = await wait_for_node_command(wait_db, cmd_id, timeout_seconds=20)
                    _rlog = logging.getLogger("cloudbase.remote_stats")
                    _rlog.info("poll node=%d app=%d cmd=%d status=%s result_len=%s",
                               node_id, app_id, cmd_id, done.status,
                               len(done.result) if done.result else 0)
                    if done.status == "done" and done.result:
                        s = json.loads(done.result)
                        _rlog.info("poll node=%d app=%d result keys=%s cpu=%s",
                                   node_id, app_id, list(s.keys()), s.get("cpu_percent"))
                        if s.get("cpu_percent") is not None:
                            snap = {
                                k: v for k, v in s.items()
                                if k not in ("status", "remote", "docker",
                                             "system_cpu_percent", "system_memory_total_mb",
                                             "system_memory_used_mb", "system_memory_percent")
                            }
                            snap["timestamp"] = int(_time.time() * 1000)
                            for replica in replicas:
                                pm.set_replica_stats(replica.id, {"replica_id": replica.id, **snap})
                            _rlog.info("poll stored stats for %d replicas on node=%d", len(replicas), node_id)
                        elif s.get("status") == "stopped":
                            _rlog.info("poll node=%d app=%d temporarily stopped during restart", node_id, app_id)
                            # Fallback to per-replica stats to avoid losing metrics when
                            # app-level aggregate briefly reports stopped.
                            for replica in replicas:
                                try:
                                    async with AsyncSessionLocal() as rdb:
                                        rcmd = await queue_node_command(
                                            rdb,
                                            node_id=node_id,
                                            app_id=app_id,
                                            command_type="get_replica_stats",
                                            payload={"app_id": app_id, "app_name": app_name, "replica_id": replica.id},
                                            allow_existing_inflight=True,
                                        )
                                        rdone = await wait_for_node_command(rdb, rcmd.id, timeout_seconds=12)
                                    if rdone.status == "done" and rdone.result:
                                        rs = json.loads(rdone.result)
                                        if rs.get("cpu_percent") is not None:
                                            pm.set_replica_stats(replica.id, {"replica_id": replica.id, **rs})
                                except Exception:
                                    continue
                except Exception as _e:
                    logging.getLogger("cloudbase.remote_stats").warning(
                        "remote stats poll failed node=%d app=%d: %s", node_id, app_id, _e)

            await asyncio.gather(*[_poll_group(nid, aid, reps) for (nid, aid), reps in groups.items()])

        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(15)


# ── Historical stats writer ───────────────────────────────────────────────────
async def _stats_history_writer():
    """Every 30s write averaged stats from the in-memory deque to the DB for long-term history.

    Works for both local and remote apps — remote apps buffer their stats into pm._stats_history
    via the stats WebSocket relay in routers/stats.py, so we just write whatever has accumulated.
    """
    import datetime as _dt
    from models import StatsHistory
    from sqlalchemy import delete as _delete
    await asyncio.sleep(30)
    _cleanup_counter = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Application).where(Application.status == "running")
                )
                apps = result.scalars().all()
                for a in apps:
                    recent = pm.get_recent_stats(a.id)
                    if not recent:
                        continue
                    # Skip per-replica frames (they have replica_id, not cpu_percent at top level)
                    agg = [s for s in recent if "cpu_percent" in s]
                    if not agg:
                        continue
                    window = agg[-15:]
                    n = len(window)
                    avg_cpu  = sum(s.get("cpu_percent", 0) for s in window) / n
                    avg_mem  = sum(s.get("memory_mb",   0) for s in window) / n
                    avg_net  = sum((s.get("net_rx_mb",  0) or 0) + (s.get("net_tx_mb",   0) or 0) for s in window) / n
                    avg_disk = sum((s.get("disk_read_mb",0) or 0) + (s.get("disk_write_mb",0) or 0) for s in window) / n
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
                from models import Node as _Node, ApplicationReplica
                local_node_result = await db.execute(select(_Node).where(_Node.is_local == True))
                local_node_obj = local_node_result.scalar_one_or_none()
                local_node_id = local_node_obj.id if local_node_obj else None

                # Monitor local replica containers
                rep_result = await db.execute(
                    select(ApplicationReplica).where(ApplicationReplica.status == "running")
                )
                replicas = rep_result.scalars().all()
                node_map: dict = {}
                for replica in replicas:
                    r_node_id = replica.node_id
                    if r_node_id not in node_map:
                        nr = await db.execute(select(_Node).where(_Node.id == r_node_id))
                        node_map[r_node_id] = nr.scalar_one_or_none()
                    r_node = node_map.get(r_node_id)
                    if r_node and not r_node.is_local:
                        continue  # remote replicas managed by their agent

                    alive = await asyncio.to_thread(dm.is_replica_container_running, replica.app_id, replica.id)
                    if alive:
                        continue

                    app_result = await db.execute(select(Application).where(Application.id == replica.app_id))
                    a = app_result.scalar_one_or_none()
                    if not a:
                        continue

                    policy = a.restart_policy or "no"
                    if policy == "no":
                        replica.status = "stopped"
                        pm._push_line(a.id, f"⚠ Instance {replica.id} exited.")
                        await db.commit()
                        continue

                    now = _time.time()
                    key = (a.id, replica.id)
                    history = _restart_history.setdefault(key, [])
                    history[:] = [t for t in history if now - t < RESTART_WINDOW_SECONDS]

                    if len(history) >= MAX_RESTARTS_PER_WINDOW:
                        replica.status = "error"
                        replica.last_error = f"Crashed {MAX_RESTARTS_PER_WINDOW}× in {RESTART_WINDOW_SECONDS}s"
                        pm._push_line(a.id, f"✖ Instance {replica.id} crashed {MAX_RESTARTS_PER_WINDOW}× — giving up.")
                        await db.commit()
                        continue

                    history.append(now)
                    attempt = len(history)
                    pm._push_line(a.id, f"⟳ Instance {replica.id} exited — restarting (attempt {attempt}/{MAX_RESTARTS_PER_WINDOW})…")
                    await asyncio.sleep(min(2 ** attempt, 30))

                    try:
                        env_vars = decrypt_env(a.env_vars or "")
                        cid = await asyncio.to_thread(
                            pm.start_docker_replica,
                            a.id, replica.id, a.name,
                            a.port or 8000, replica.external_port or a.port or 8000,
                            env_vars, _docker_runtime_options(a),
                        )
                        replica.status = "running"
                        replica.container_id = cid
                        replica.last_error = None
                        pm._push_line(a.id, f"✓ Instance {replica.id} restarted ({cid[:12]}).")
                    except Exception as e:
                        replica.status = "error"
                        replica.last_error = str(e)
                        pm._push_line(a.id, f"✖ Instance {replica.id} restart failed: {e}")

                    # Sync app status from all its replicas
                    all_rep_result = await db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.app_id == a.id)
                    )
                    from routers.applications import _derive_app_status_from_instances
                    a.status = _derive_app_status_from_instances(all_rep_result.scalars().all())
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

    # Recover running instances and auto-start
    async with AsyncSessionLocal() as db:
        from models import ApplicationReplica, Node as _Node
        local_node = await nodes.ensure_local_node(db)

        # Recover replica container statuses
        rep_result = await db.execute(select(ApplicationReplica))
        all_replicas = rep_result.scalars().all()
        node_map: dict = {}
        for replica in all_replicas:
            r_node_id = replica.node_id
            if r_node_id not in node_map:
                nr = await db.execute(select(_Node).where(_Node.id == r_node_id))
                node_map[r_node_id] = nr.scalar_one_or_none()
            r_node = node_map.get(r_node_id)
            if r_node and not r_node.is_local:
                continue  # remote replicas managed by their own agent
            if replica.status in ("starting", "stopping", "deploying"):
                continue
            alive = dm.is_replica_container_running(replica.app_id, replica.id)
            new_status = "running" if alive else "stopped"
            if new_status != replica.status:
                replica.status = new_status
                pm._debug(f"RECOVERY replica {replica.id} app={replica.app_id}: → {new_status}")
            if alive:
                dm.attach_container_log_tailer(replica.app_id, pm.log_buffers, pm._push_line, asyncio.get_event_loop())

        # Kill any orphan legacy containers (cloudbase-app-{id} without replica row)
        app_ids_with_replicas = {r.app_id for r in all_replicas}
        for app_id in list(app_ids_with_replicas):
            if pm.is_docker_app_running(app_id):
                pm.stop_docker_app(app_id)
                pm._debug(f"RECOVERY killed orphan legacy container for app {app_id}")

        # Sync app statuses from their replicas
        result = await db.execute(select(Application))
        apps = result.scalars().all()
        replica_map: dict[int, list] = {}
        for r in all_replicas:
            replica_map.setdefault(r.app_id, []).append(r)

        from routers.applications import _derive_app_status_from_instances
        for a in apps:
            app_replicas = replica_map.get(a.id)
            if app_replicas:
                a.status = _derive_app_status_from_instances(app_replicas)
            # auto_start: start all stopped local replicas
            if a.auto_start and a.start_command and a.working_dir and app_replicas:
                env_vars = decrypt_env(a.env_vars or "")
                for replica in app_replicas:
                    r_node = node_map.get(replica.node_id)
                    if r_node and not r_node.is_local:
                        continue
                    if replica.status != "stopped":
                        continue
                    try:
                        cid = pm.start_docker_replica(
                            a.id, replica.id, a.name,
                            a.port or 8000, replica.external_port or a.port or 8000,
                            env_vars, _docker_runtime_options(a),
                        )
                        replica.status = "running"
                        replica.container_id = cid
                        pm._debug(f"AUTO-START replica {replica.id} app={a.id}: {cid[:12]}")
                    except Exception as exc:
                        pm._debug(f"AUTO-START replica {replica.id} app={a.id}: FAILED — {exc}")
                a.status = _derive_app_status_from_instances(app_replicas)

        await db.commit()
        await asyncio.to_thread(_restore_stuck_restart_configs, apps)

    # Regenerate nginx configs from live instance state so stale configs on disk
    # (written before the instance-based model) are replaced with correct upstreams.
    async with AsyncSessionLocal() as db:
        from models import ApplicationReplica as _AR
        from routers.applications import _get_nginx_backends, _get_nginx_mode, _derive_app_status_from_instances
        from env_crypto import decrypt_env as _dec
        result = await db.execute(select(Application))
        for a in result.scalars().all():
            if not a.nginx_enabled or not a.domain:
                continue
            try:
                backends = await _get_nginx_backends(a, db)
                ssl_cert = a.ssl_cert_path
                ssl_key  = a.ssl_key_path
                config   = nm.generate_config(
                    a.name, a.domain, backends,
                    ssl_cert, ssl_key,
                    app_id=a.id, mode=_get_nginx_mode(a),
                    extra_domains=json.loads(a.extra_domains or "[]"),
                    redirect_domains=json.loads(a.redirect_domains or "[]"),
                )
                nm.write_nginx_config(a.name, config)
                pm._debug(f"STARTUP nginx regenerated for app {a.id} ({a.name}): {len(backends)} backends")
            except Exception as exc:
                pm._debug(f"STARTUP nginx regen failed for app {a.id}: {exc}")

    monitor_task       = asyncio.create_task(_crash_monitor())
    stats_task         = asyncio.create_task(_stats_collector())
    node_task          = asyncio.create_task(_node_health_monitor())
    history_task       = asyncio.create_task(_stats_history_writer())
    remote_stats_task  = asyncio.create_task(_remote_replica_stats_poller())

    # Start node agent if configured (as an integrated background task)
    agent_task = None
    if node_agent._load_state() is not None:
        pm._debug("INIT: Node state detected, starting integrated agent task")
        agent_task = asyncio.create_task(node_agent.start_agent())

    yield
    for task in (monitor_task, stats_task, node_task, history_task, remote_stats_task, agent_task):
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
            # Allow remote node agents via X-Node-Token (e.g. fetching source archives)
            node_token = request.headers.get("X-Node-Token", "")
            if node_token:
                from sqlalchemy import select as _select, and_ as _and
                from models import Node as _Node
                async with AsyncSessionLocal() as _db:
                    _res = await _db.execute(
                        _select(_Node).where(_and(_Node.auth_token == node_token, _Node.enabled == True))
                    )
                    if _res.scalar_one_or_none():
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
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


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
    unavailable_html = nm.generate_cloudbase_unavailable_html(req.domain)
    page_ok, page_msg = nm.write_cloudbase_unavailable_page(unavailable_html)
    if not page_ok:
        raise HTTPException(status_code=500, detail=f"Failed to write Cloudbase unavailable page: {page_msg}")

    config = nm.generate_config("cloudbase", req.domain, PORT, req.ssl_cert_path, req.ssl_key_path)
    ok, msg = nm.write_nginx_config("cloudbase", config)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to apply nginx config: {msg}")
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
