import asyncio
import json
import logging
import math
import os
import shutil
import socket
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Query, Body, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, func, or_, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, AsyncSessionLocal
import datetime as _dt
from models import Application, ApplicationReplica, Node, StatsHistory, NodeCommand, AuditLog
import process_manager as pm
import nginx_manager as nm
import token_vault
import docker_manager as dm
from routers.nodes import ensure_local_node, queue_node_command, wait_for_node_command
from env_crypto import encrypt_env, decrypt_env, encrypt_text, decrypt_text
from audit import log_audit
import auth as _auth
import config as _cfg
import system_config as _syscfg

router = APIRouter(prefix="/api/apps", tags=["applications"])
log = logging.getLogger("pdm.apps")

RESTART_READY_TIMEOUT_SECONDS = 180
RESTART_READY_POLL_SECONDS = 1
TRANSITION_HOLD_MAX_SECONDS = 300
RESTART_READY_MIN_HEALTHY_RATIO = 0.5
_active_transition_modes: dict[int, tuple[str, float]] = {}


def _set_active_transition_mode(app_id: int, mode: str, started_at: Optional[float] = None) -> float:
    ts = started_at if started_at is not None else asyncio.get_running_loop().time()
    _active_transition_modes[app_id] = (mode, ts)
    return ts


def _get_active_transition_mode(app_id: int) -> Optional[str]:
    active = _active_transition_modes.get(app_id)
    if not active:
        return None
    mode, started_at = active
    elapsed = asyncio.get_running_loop().time() - started_at
    if elapsed > TRANSITION_HOLD_MAX_SECONDS:
        _active_transition_modes.pop(app_id, None)
        return None
    return mode


def _clear_active_transition_mode(app_id: int) -> None:
    _active_transition_modes.pop(app_id, None)


class DeployRequest(BaseModel):
    name: str
    repo_url: str
    github_token: Optional[str] = None
    github_token_id: Optional[str] = None   # ID of a saved vault token
    domain: Optional[str] = None
    extra_domains: Optional[list] = None      # additional domains/subdomains
    redirect_domains: Optional[list] = None   # domains that redirect to primary
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    start_command: Optional[str] = None
    port: Optional[int] = None               # internal port (inside container)
    external_port: Optional[int] = None      # host port — auto-assigned if omitted
    docker_cpu_limit: Optional[float] = None
    docker_memory_limit_mb: Optional[int] = None
    docker_read_only_root: Optional[bool] = None
    docker_tmpfs_enabled: Optional[bool] = None
    docker_tmpfs_size_mb: Optional[int] = None
    env_vars: Optional[dict] = None
    node_id: Optional[int] = None
    auto_start: Optional[bool] = None
    restart_policy: Optional[str] = None   # no | always | on-failure
    no_web: Optional[bool] = None          # True = no web server, skip nginx + port assignment
    source_revision: Optional[str] = None
    image_revision: Optional[str] = None


class UpdateRequest(BaseModel):
    domain: Optional[str] = None
    extra_domains: Optional[list] = None      # additional domains/subdomains
    redirect_domains: Optional[list] = None   # domains that redirect to primary
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    start_command: Optional[str] = None
    port: Optional[int] = None               # internal port
    external_port: Optional[int] = None      # host port
    docker_cpu_limit: Optional[float] = None
    docker_memory_limit_mb: Optional[int] = None
    docker_read_only_root: Optional[bool] = None
    docker_tmpfs_enabled: Optional[bool] = None
    docker_tmpfs_size_mb: Optional[int] = None
    env_vars: Optional[dict] = None
    github_token: Optional[str] = None
    github_token_id: Optional[str] = None   # ID of a saved vault token
    auto_start:     Optional[bool] = None
    restart_policy: Optional[str] = None   # no | always | on-failure
    no_web:         Optional[bool] = None  # True = no web server, skip nginx + port assignment
    working_dir:    Optional[str] = None   # set by node agents after source extraction
    source_revision: Optional[str] = None
    image_revision: Optional[str] = None


class MaintenancePageConfig(BaseModel):
    title: Optional[str] = ""
    message: Optional[str] = ""
    color: Optional[str] = "#f85149"
    status_url: Optional[str] = None
    custom_html: Optional[str] = None
    logo_data: Optional[str] = None    # base64 data-URL for logo image


class ExportRequest(BaseModel):
    app_ids: Optional[list[int]] = None # If None or empty, export all


class ImportRequest(BaseModel):
    apps: list[dict]
    target_node_id: Optional[int] = None # Optional override node


class PullRequest(BaseModel):
    commit: Optional[str] = None



class MaintenanceSettings(BaseModel):
    downtime_page: MaintenancePageConfig = MaintenancePageConfig()
    update_page: MaintenancePageConfig = MaintenancePageConfig(color="#f0883e")
    restart_page: MaintenancePageConfig = MaintenancePageConfig(color="#388bfd")
    starting_page: MaintenancePageConfig = MaintenancePageConfig(color="#388bfd")


async def _assign_external_port(requested: Optional[int], node_id: int, exclude_app_id: Optional[int], db: AsyncSession) -> int:
    """Return requested port if free, else auto-pick the next free port in [8000, 8999]."""
    result = await db.execute(
        select(Application.external_port).where(
            or_(Application.node_id == node_id, Application.node_id.is_(None)),
            Application.external_port.isnot(None),
            Application.id != exclude_app_id if exclude_app_id else True,
        )
    )
    used: set[int] = {row[0] for row in result.all()}

    replica_result = await db.execute(
        select(ApplicationReplica.external_port).where(
            or_(ApplicationReplica.node_id == node_id, ApplicationReplica.node_id.is_(None)),
            ApplicationReplica.external_port.isnot(None),
        )
    )
    used |= {row[0] for row in replica_result.all()}

    if requested:
        if requested in used:
            raise HTTPException(400, f"External port {requested} is already used by another app on this node")
        return requested

    return await asyncio.to_thread(dm.pick_free_external_port, used)


async def _cleanup_app_dependencies(db: AsyncSession, app_id: int) -> None:
    """Remove or detach rows that can still reference an app in older DB schemas."""
    await db.execute(
        update(NodeCommand)
        .where(NodeCommand.app_id == app_id)
        .values(app_id=None)
    )
    await db.execute(
        update(AuditLog)
        .where(AuditLog.app_id == app_id)
        .values(app_id=None)
    )
    await db.execute(delete(StatsHistory).where(StatsHistory.app_id == app_id))
    await db.execute(delete(ApplicationReplica).where(ApplicationReplica.app_id == app_id))


def _best_effort_remove_app_nginx(app_name: str) -> None:
    """Remove app nginx config even when DB flags are stale, to avoid ghost routes."""
    try:
        nm.remove_nginx_config(app_name)
    except Exception:
        pass


async def _check_domain_conflicts(
    domains: list[str],
    db: AsyncSession,
    exclude_app_id: Optional[int] = None,
) -> None:
    """Raise 400 if any domain in *domains* is already used by another app."""
    clean = [d.strip().lower() for d in domains if d and d.strip()]
    if not clean:
        return
    result = await db.execute(
        select(Application.id, Application.name, Application.domain,
               Application.extra_domains, Application.redirect_domains).where(
            Application.id != exclude_app_id if exclude_app_id else True,
        )
    )
    for row in result.all():
        other_id, other_name, primary, extra_raw, redirect_raw = row
        other_domains = set()
        if primary:
            other_domains.add(primary.strip().lower())
        for lst_raw in (extra_raw, redirect_raw):
            try:
                for d in json.loads(lst_raw or "[]"):
                    if d:
                        other_domains.add(d.strip().lower())
            except Exception:
                pass
        clash = set(clean) & other_domains
        if clash:
            raise HTTPException(
                400,
                f"Domain '{next(iter(clash))}' is already used by app '{other_name}'",
            )


def _get_nginx_mode(app: Application) -> str:
    if app.update_mode:
        return "update"
    if app.maintenance_mode:
        return "maintenance"
    return "normal"


def _has_public_nginx_domain(app: Application) -> bool:
    """True when app traffic can be routed via custom or auto-subdomain nginx."""
    if app.no_web:
        return False
    return bool((app.nginx_enabled and app.domain) or _syscfg.get_base_domain_cached())


def _nginx_proxy_port(app: Application) -> Optional[int]:
    return app.external_port or app.port


def _validate_docker_runtime_settings(
    cpu_limit: Optional[float],
    memory_limit_mb: Optional[int],
    tmpfs_size_mb: Optional[int],
) -> None:
    if cpu_limit is not None and cpu_limit <= 0:
        raise HTTPException(400, "docker_cpu_limit must be greater than 0")
    if memory_limit_mb is not None and memory_limit_mb <= 0:
        raise HTTPException(400, "docker_memory_limit_mb must be greater than 0")
    if tmpfs_size_mb is not None and tmpfs_size_mb <= 0:
        raise HTTPException(400, "docker_tmpfs_size_mb must be greater than 0")


def _docker_runtime_options(app: Application) -> dict:
    return {
        "cpu_limit": app.docker_cpu_limit,
        "memory_limit_mb": app.docker_memory_limit_mb,
        "read_only_root": bool(app.docker_read_only_root),
        "tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "restart_policy": app.restart_policy or "no",
    }



async def _load_app_replicas(app_id: int, db: AsyncSession) -> list[ApplicationReplica]:
    result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    return result.scalars().all()


async def _preferred_replica_node(
    app: Application,
    db: AsyncSession,
    local_node: Node,
) -> Node:
    if app.node_id:
        node_result = await db.execute(
            select(Node).where(Node.id == app.node_id)
        )
        node = node_result.scalar_one_or_none()
        if node:
            return node
    return local_node


def _resolve_replica_node(
    replica: ApplicationReplica,
    node_map: dict[int, Node],
    local_node: Node,
) -> Optional[Node]:
    if replica.node_id is None or replica.node_id == local_node.id:
        return local_node
    return node_map.get(replica.node_id)




async def _best_effort_stop_legacy_app_container(app_id: int) -> None:
    if await asyncio.to_thread(pm.is_docker_app_running, app_id):
        await asyncio.to_thread(pm.stop_docker_app, app_id)


async def _write_app_nginx_config(
    app: Application,
    db: AsyncSession,
    local_node: Node,
    *,
    mode: Optional[str] = None,
) -> None:
    if app.no_web:
        app.nginx_enabled = False
        _best_effort_remove_app_nginx(app.name)
        return

    _base = _syscfg.get_base_domain_cached()
    has_custom = bool(app.nginx_enabled and app.domain)
    if not has_custom and not _base:
        return

    maint_ok, maint_msg = _ensure_maintenance_files(app, app.id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")

    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
    backends = await _get_nginx_backends(app, db, local_node)
    effective_mode = mode or _get_nginx_mode(app)
    if mode is None and effective_mode == "normal":
        active_mode = _get_active_transition_mode(app.id)
        if active_mode in ("restart", "starting"):
            log.info(
                "[nginx-transition-guard] app_id=%s forcing mode=%s (normal write suppressed while transition active)",
                app.id,
                active_mode,
            )
            effective_mode = active_mode
    log.info(
        "[nginx-debug] write app_id=%s app=%s mode=%s backends=%d values=%s has_custom=%s base_domain=%s",
        app.id,
        app.name,
        effective_mode,
        len(backends),
        backends,
        has_custom,
        bool(_base),
    )
    config = nm.generate_config(
        app.name,
        app.domain if has_custom else None,
        backends,
        ssl_cert,
        ssl_key,
        app_id=app.id,
        mode=effective_mode,
        extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
        redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
    )
    ok, msg = nm.write_nginx_config(app.name, config)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")


async def _wait_for_app_backends_ready(
    app_id: int,
    timeout_seconds: int = RESTART_READY_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_reason = "no running instance backends yet"
    stable_ready_polls = 0

    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionLocal() as db:
            app = await db.get(Application, app_id)
            if not app:
                return False, "app was removed before nginx restore"
            backends = await _get_nginx_backends(app, db)

        if not backends:
            last_reason = "no running instance backends yet"
            await asyncio.sleep(RESTART_READY_POLL_SECONDS)
            continue

        ready = 0
        checked = 0
        for backend in backends:
            try:
                port = int(str(backend).rsplit(":", 1)[1])
            except (IndexError, TypeError, ValueError):
                continue
            checked += 1
            if await asyncio.to_thread(_local_http_service_ready, port):
                ready += 1

        # Require a healthy quorum for two consecutive polls so restart pages
        # are not cleared prematurely while still allowing partial recovery.
        required = max(1, math.ceil(checked * RESTART_READY_MIN_HEALTHY_RATIO)) if checked else 0
        if checked and ready >= required:
            stable_ready_polls += 1
            if stable_ready_polls >= 2:
                return True, f"{ready}/{checked} backend(s) healthy (threshold {required})"
            last_reason = f"{ready}/{checked} backend(s) healthy (threshold {required}), waiting for stability"
            await asyncio.sleep(RESTART_READY_POLL_SECONDS)
            continue

        stable_ready_polls = 0

        last_reason = f"{ready}/{checked} backend(s) healthy (threshold {required})"
        await asyncio.sleep(RESTART_READY_POLL_SECONDS)

    return False, last_reason


async def _restore_nginx_after_transition(
    app_id: int,
    started_at: float,
    transition_label: str,
) -> None:
    ready, reason = await _wait_for_app_backends_ready(app_id)
    elapsed = max(asyncio.get_running_loop().time() - started_at, 0)

    # Do not clear start/restart pages while backends are still unhealthy.
    # Keep waiting in the background, with a hard cap to avoid infinite loops.
    if not ready and elapsed < TRANSITION_HOLD_MAX_SECONDS:
        log.info(
            "[nginx-transition-debug] app_id=%s label=%s keep_transition_page elapsed=%.1fs reason=%s",
            app_id,
            transition_label,
            elapsed,
            reason,
        )
        pm._push_line(
            app_id,
            f"{transition_label.capitalize()} page still active after {elapsed:.1f}s ({reason}); waiting for healthy backends...",
        )
        asyncio.create_task(_restore_nginx_after_transition(app_id, started_at, transition_label))
        return

    # Transition window has ended (ready or hard timeout): allow normal writes again.
    _clear_active_transition_mode(app_id)

    async with AsyncSessionLocal() as db:
        app = await db.get(Application, app_id)
        _base = _syscfg.get_base_domain_cached()
        has_custom = bool(app and app.nginx_enabled and app.domain)
        if not app or (not has_custom and not _base):
            return

        local_node = await ensure_local_node(db)
        backends = await _get_nginx_backends(app, db, local_node)
        if not backends:
            # Readiness can race with status transitions; keep the transition page
            # active until backends are still present when we restore nginx.
            if elapsed < TRANSITION_HOLD_MAX_SECONDS:
                log.info(
                    "[nginx-transition-debug] app_id=%s label=%s restore_race_no_backends elapsed=%.1fs",
                    app_id,
                    transition_label,
                    elapsed,
                )
                pm._push_line(
                    app_id,
                    f"{transition_label.capitalize()} page still active after {elapsed:.1f}s (backends temporarily unavailable); retrying...",
                )
                asyncio.create_task(_restore_nginx_after_transition(app_id, started_at, transition_label))
                return
            pm._push_line(
                app_id,
                f"{transition_label.capitalize()} page kept active after {elapsed:.1f}s; no running backends available to restore normal nginx routing.",
            )
            return

        ssl_cert = ssl_key = None
        if has_custom:
            ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
        effective_mode = _get_nginx_mode(app)
        log.info(
            "[nginx-transition-debug] app_id=%s label=%s restoring mode=%s backends=%d values=%s",
            app_id,
            transition_label,
            effective_mode,
            len(backends),
            backends,
        )
        config = nm.generate_config(
            app.name,
            app.domain if has_custom else None,
            backends,
            ssl_cert,
            ssl_key,
            app_id=app.id,
            mode=effective_mode,
            extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
            redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
        )
        ok, msg = nm.write_nginx_config(app.name, config)

    log.info(
        "[nginx-transition] app_id=%d label=%s ready=%s elapsed=%.1fs reason=%r nginx_ok=%s msg=%r",
        app_id,
        transition_label,
        ready,
        elapsed,
        reason,
        ok,
        msg,
    )
    if ok:
        status_text = "cleared" if ready else "force-cleared after hard timeout"
        pm._push_line(
            app_id,
            f"{transition_label.capitalize()} page {status_text} after {elapsed:.1f}s ({reason}).",
        )
    else:
        pm._push_line(app_id, f"Failed to restore nginx after {transition_label}: {msg}")


def _remote_replica_command_payload(app: Application, env_vars: dict, external_port: int) -> dict:
    source_revision = _refresh_app_source_revision(app)
    app_type = app.app_type or pm.detect_app_type_from_command(app.start_command or "")
    return {
        "app_id": app.id,
        "app_name": app.name,
        "repo_url": app.repo_url,
        "github_token": _decrypt_github_token(app.github_token),
        "app_type": app_type or "unknown",
        "start_command": app.start_command,
        "internal_port": app.port or 8000,
        "external_port": external_port,
        "env_vars": env_vars,
        "restart_policy": app.restart_policy,
        "docker_cpu_limit": app.docker_cpu_limit,
        "docker_memory_limit_mb": app.docker_memory_limit_mb,
        "docker_read_only_root": bool(app.docker_read_only_root),
        "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "docker_options": _docker_runtime_options(app),
        "source_revision": source_revision,
    }


def _resolve_ssl_paths(cert: str | None, key: str | None) -> tuple[str | None, str | None]:
    """Return cert/key paths only if both files actually exist on disk; otherwise None."""
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    if cert or key:
        missing = [p for p in (cert, key) if p and not os.path.isfile(p)]
        log.warning("SSL cert/key file(s) not found on disk, skipping SSL: %s", missing)
    return None, None


def _ensure_maintenance_files(app: Application, app_id: int) -> tuple[bool, str]:
    """Write maintenance HTML files from stored config (or defaults)."""
    downtime_cfg = json.loads(app.downtime_page  or "{}")
    update_cfg   = json.loads(app.update_page    or "{}")
    restart_cfg  = json.loads(app.restart_page   or "{}")
    starting_cfg = json.loads(app.starting_page  or "{}")

    def _page_meta(cfg: dict) -> dict:
        logo = cfg.get("logo_data")
        html = cfg.get("custom_html")
        return {
            "has_logo": bool(logo),
            "logo_len": len(logo) if isinstance(logo, str) else 0,
            "has_custom_html": bool(html),
            "custom_html_len": len(html) if isinstance(html, str) else 0,
            "has_status_url": bool(cfg.get("status_url")),
        }

    log.info(
        "[ensure-files] app_id=%d page_meta downtime=%s update=%s restart=%s starting=%s",
        app_id,
        _page_meta(downtime_cfg),
        _page_meta(update_cfg),
        _page_meta(restart_cfg),
        _page_meta(starting_cfg),
    )

    downtime_html = nm.generate_maintenance_html(
        downtime_cfg.get("title")       or "Down for Maintenance",
        downtime_cfg.get("message")     or "We'll be back shortly.",
        downtime_cfg.get("color")       or "#f85149",
        downtime_cfg.get("status_url"),
        downtime_cfg.get("custom_html"),
        "downtime",
        logo_data=downtime_cfg.get("logo_data"),
    )
    update_html = nm.generate_maintenance_html(
        update_cfg.get("title")         or "Updating\u2026",
        update_cfg.get("message")       or "We\u2019re deploying a new version. Check back soon.",
        update_cfg.get("color")         or "#f0883e",
        update_cfg.get("status_url"),
        update_cfg.get("custom_html"),
        "update",
        logo_data=update_cfg.get("logo_data"),
    )
    restart_html = nm.generate_maintenance_html(
        restart_cfg.get("title")        or "Restarting\u2026",
        restart_cfg.get("message")      or "The server is restarting. This only takes a moment.",
        restart_cfg.get("color")        or "#388bfd",
        restart_cfg.get("status_url"),
        restart_cfg.get("custom_html"),
        "restart",
        logo_data=restart_cfg.get("logo_data"),
    )
    starting_html = nm.generate_maintenance_html(
        starting_cfg.get("title")       or "Starting\u2026",
        starting_cfg.get("message")     or "The service is starting up. This only takes a moment.",
        starting_cfg.get("color")       or "#388bfd",
        starting_cfg.get("status_url"),
        starting_cfg.get("custom_html"),
        "starting",
        logo_data=starting_cfg.get("logo_data"),
    )
    ok, msg = nm.write_maintenance_files(app_id, downtime_html, update_html, restart_html, starting_html)
    log.info("[ensure-files] write result ok=%s msg=%r", ok, msg)
    return ok, msg


def _local_port_accepts_connections(port: int) -> bool:
    """Return True when localhost:port accepts TCP connections."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except Exception:
        return False


def _local_http_service_ready(port: int) -> bool:
    """Return True when localhost:port accepts TCP and returns a non-5xx HTTP status."""
    if not _local_port_accepts_connections(port):
        return False

    try:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=0.8)
        conn.request("GET", "/")
        res = conn.getresponse()
        status = int(res.status or 0)
        conn.close()
        return 100 <= status < 500
    except Exception:
        return False


def _resolve_token(req_token: Optional[str], req_token_id: Optional[str]) -> Optional[str]:
    """Return raw token: prefer vault lookup, fall back to inline value."""
    if req_token_id:
        resolved = token_vault.resolve(req_token_id)
        if resolved:
            return resolved
    return req_token or None


def _encrypt_github_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    return encrypt_text(token)


def _decrypt_github_token(stored: Optional[str]) -> Optional[str]:
    if not stored:
        return None
    plain = decrypt_text(stored, fallback_plaintext=True)
    return plain or None


def _request_is_admin(request: Request) -> bool:
    token = request.cookies.get("pdm_token")
    user = _auth.decode_token(token) if token else None
    return bool(user and user.get("role") == "admin")


def _build_clone_url(repo_url: str, token: Optional[str]) -> str:
    if token and "github.com" in repo_url:
        repo_url = repo_url.replace("https://", f"https://{token}@")
    return repo_url


def _friendly_git_clone_error(stderr: str) -> str:
    raw = (stderr or "").strip()
    low = raw.lower()

    if "could not read username for" in low or "authentication failed" in low:
        return "Git clone failed: authentication required for this repository. Add a valid GitHub token and try again."

    if "repository not found" in low and "github.com" in low:
        return "Git clone failed: repository not found or access denied. Check the repo URL and GitHub token permissions."

    if "permission denied" in low:
        return "Git clone failed: permission denied. Verify repository access and deploy credentials."

    if raw:
        return f"Git clone failed: {raw}"
    return "Git clone failed for an unknown reason."


def _git_app_dir_or_404(app_name: str) -> str:
    app_dir = pm.get_app_dir(app_name)
    if not os.path.exists(app_dir):
        raise HTTPException(404, "App directory does not exist on disk")
    if not os.path.exists(os.path.join(app_dir, ".git")):
        raise HTTPException(
            400,
            "Repository is not initialized for this app. Deploy it successfully first.",
        )
    return app_dir


def _fetch_origin(app_dir: str, branch: str) -> None:
    fetch = subprocess.run(["git", "fetch", "origin", branch], cwd=app_dir, capture_output=True, text=True)
    if fetch.returncode != 0:
        fetch = subprocess.run(["git", "fetch", "origin"], cwd=app_dir, capture_output=True, text=True)
    if fetch.returncode != 0:
        raise HTTPException(500, f"Git fetch failed: {fetch.stderr}")


def _current_branch(app_dir: str) -> str:
    br_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=app_dir, capture_output=True, text=True)
    return br_res.stdout.strip() if br_res.returncode == 0 else "main"


def _git_head_commit(app_dir: str) -> Optional[str]:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=app_dir, capture_output=True, text=True)
    if res.returncode != 0:
        return None
    value = (res.stdout or "").strip()
    return value or None


def _git_worktree_dirty(app_dir: str) -> bool:
    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=app_dir,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False
    return bool((res.stdout or "").strip())


def _resolve_source_revision(app_dir: str) -> Optional[str]:
    head = _git_head_commit(app_dir)
    if not head:
        return None
    if _git_worktree_dirty(app_dir):
        return f"{head}-dirty"
    return head


def _refresh_app_source_revision(app: Application) -> Optional[str]:
    app_dir = app.working_dir
    if not app_dir or not os.path.exists(os.path.join(app_dir, ".git")):
        return app.source_revision
    revision = _resolve_source_revision(app_dir)
    if revision:
        app.source_revision = revision
    return revision


def _recent_git_commits(app_dir: str, limit: int = 20, ref: Optional[str] = None) -> list[dict]:
    fmt = "%H%x1f%h%x1f%s%x1f%cr%x1f%an"
    cmd = ["git", "log"]
    if ref:
        cmd.append(ref)
    cmd.extend([f"-n{limit}", f"--format={fmt}", "--decorate"])
    res = subprocess.run(
        cmd,
        cwd=app_dir,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise HTTPException(500, f"Git log failed: {res.stderr}")

    commits = []
    for line in res.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        full_hash, short_hash, subject, relative_time, author = parts
        commits.append({
            "hash": full_hash,
            "short_hash": short_hash,
            "subject": subject,
            "relative_time": relative_time,
            "author": author,
        })
    return commits


@router.get("/{app_id}/source-archive")
async def get_source_archive(app_id: int, db: AsyncSession = Depends(get_db)):
    """Return a .tar.gz of the app's working directory for remote nodes to build from.
    Only the local primary serves this — remote nodes call this endpoint directly."""
    app = await _get_or_404(app_id, db)
    if not app.working_dir or not os.path.exists(app.working_dir):
        raise HTTPException(404, "App source directory not found — deploy the app first")

    import tarfile
    import io

    def _make_archive() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(app.working_dir, arcname=".", recursive=True,
                    filter=lambda m: None if os.path.basename(m.name) in {".git", "__pycache__", "node_modules", ".venv", "venv"} else m)
        return buf.getvalue()

    data = await asyncio.to_thread(_make_archive)
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={app.name}-source.tar.gz"},
    )


@router.get("/{app_id}/commits")
async def list_git_commits(
    app_id: int,
    limit: int = Query(20, ge=1, le=100),
    refresh: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    app = await _get_or_404(app_id, db)
    app_dir = _git_app_dir_or_404(app.name)

    def _get_commits() -> tuple[str, str, list]:
        branch = _current_branch(app_dir)
        ref = "HEAD"
        if refresh:
            _fetch_origin(app_dir, branch)
            ref = f"origin/{branch}"
        commits = _recent_git_commits(app_dir, limit, ref=ref)
        if refresh and not commits:
            commits = _recent_git_commits(app_dir, limit)
            ref = "HEAD"
        return branch, ref, commits

    branch, ref, commits = await asyncio.to_thread(_get_commits)
    return {"branch": branch, "ref": ref, "commits": commits}


def _run_install(app_dir: str) -> str:
    outputs = []
    if os.path.exists(os.path.join(app_dir, "package.json")):
        r = subprocess.run(["npm", "install"], cwd=app_dir, capture_output=True, text=True)
        outputs.append(f"--- npm install ---\n{r.stdout}\n{r.stderr}")

    if os.path.exists(os.path.join(app_dir, "requirements.txt")):
        venv_dir = os.path.join(app_dir, "venv")
        if not os.path.exists(venv_dir):
            r = subprocess.run(["python3", "-m", "venv", "venv"], cwd=app_dir, capture_output=True, text=True)
            outputs.append(f"--- python3 -m venv venv ---\n{r.stdout}\n{r.stderr}")
        
        venv_bin_name = "Scripts" if os.name == "nt" else "bin"
        pip_name = "pip.exe" if os.name == "nt" else "pip"
        pip_path = os.path.join(venv_dir, venv_bin_name, pip_name)
        if not os.path.exists(pip_path): pip_path = "pip3"
            
        r = subprocess.run([pip_path, "install", "-r", "requirements.txt"], cwd=app_dir, capture_output=True, text=True)
        outputs.append(f"--- pip install -r requirements.txt ---\n{r.stdout}\n{r.stderr}")

    if os.path.exists(os.path.join(app_dir, "Gemfile")):
        r = subprocess.run(["bundle", "install"], cwd=app_dir, capture_output=True, text=True)
        outputs.append(f"--- bundle install ---\n{r.stdout}\n{r.stderr}")

    if os.path.exists(os.path.join(app_dir, "composer.json")):
        r = subprocess.run(["composer", "install"], cwd=app_dir, capture_output=True, text=True)
        outputs.append(f"--- composer install ---\n{r.stdout}\n{r.stderr}")

    if os.path.exists(os.path.join(app_dir, "go.mod")):
        r = subprocess.run(["go", "mod", "download"], cwd=app_dir, capture_output=True, text=True)
        outputs.append(f"--- go mod download ---\n{r.stdout}\n{r.stderr}")
    
    return "\n\n".join(outputs)


async def _deploy_app(app: Application):
    app_dir = pm.get_app_dir(app.name)
    log.info("[deploy] Starting deployment for app=%s in dir=%s", app.name, app_dir)

    # local://primary means source was already extracted by the node agent — skip clone
    if app.repo_url == "local://primary":
        if not os.path.exists(app_dir):
            raise HTTPException(500, "Source directory missing for local app — re-deploy from primary")
        app.working_dir = app.working_dir or app_dir
        app.source_revision = _resolve_source_revision(app_dir)
        app.image_revision = None
    else:
        # If directory exists but is broken or empty, clean it up
        if os.path.exists(app_dir):
            if not os.path.exists(os.path.join(app_dir, ".git")):
                log.warning("[deploy] Directory %s exists but is not a git repo, cleaning up for fresh clone", app_dir)
                shutil.rmtree(app_dir)
                os.makedirs(app_dir, exist_ok=True)
        else:
            os.makedirs(app_dir, exist_ok=True)

        github_token = _decrypt_github_token(app.github_token)
        clone_url = _build_clone_url(app.repo_url, github_token)
        log.info("[deploy] Cloning from %s", app.repo_url.replace(github_token or "SECRET", "***") if github_token else app.repo_url)

        result = await asyncio.to_thread(subprocess.run,
            ["git", "clone", clone_url, "."],
            cwd=app_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            log.error("[deploy] Git clone failed: %s", result.stderr)
            raise HTTPException(400, _friendly_git_clone_error(result.stderr))

        app.working_dir = app_dir
        app.source_revision = _resolve_source_revision(app_dir)
        app.image_revision = None
    app_type, default_cmd, default_port = pm.detect_app_type(app_dir)

    if not app.start_command:
        app.start_command = default_cmd
    if not app.port and default_port:
        app.port = default_port

    app.app_type = pm.detect_app_type_from_command(app.start_command) if app.start_command else app_type

    if app.use_docker:
        log.info("[deploy] Docker mode — skipping host-side dependency install for app=%s", app.name)
        dm.ensure_dockerfile(
            app_dir,
            app.app_type or "unknown",
            app.start_command or "",
            app.port or 8000,
        )
    else:
        log.info("[deploy] Running install for app=%s", app.name)
        await asyncio.to_thread(_run_install, app_dir)
    log.info("[deploy] Deployment finished for app=%s", app.name)


@router.get("/system/certs")
async def discover_certs():
    """Scan common certificate and key locations on this machine."""
    import glob

    cert_patterns = [
        "/etc/letsencrypt/live/*/fullchain.pem",
        "/etc/letsencrypt/live/*/cert.pem",
        "/etc/ssl/certs/*.pem",
        "/etc/ssl/certs/*.crt",
        "/etc/nginx/ssl/*.pem",
        "/etc/nginx/ssl/*.crt",
        "/etc/nginx/certs/*.pem",
        "/etc/nginx/certs/*.crt",
        os.path.expanduser("~/.cloudbase/certs/*.pem"),
        os.path.expanduser("~/.cloudbase/certs/*.crt"),
    ]
    key_patterns = [
        "/etc/letsencrypt/live/*/privkey.pem",
        "/etc/ssl/private/*.pem",
        "/etc/ssl/private/*.key",
        "/etc/nginx/ssl/*.key",
        "/etc/nginx/certs/*.key",
        os.path.expanduser("~/.cloudbase/certs/*.key"),
        os.path.expanduser("~/.cloudbase/certs/*.pem"),
    ]

    certs: list[str] = []
    keys: list[str] = []

    for pattern in cert_patterns:
        try:
            certs.extend(glob.glob(pattern))
        except Exception:
            pass
    for pattern in key_patterns:
        try:
            keys.extend(glob.glob(pattern))
        except Exception:
            pass

    return {"certs": sorted(set(certs)), "keys": sorted(set(keys))}


@router.get("/{app_id}/certs")
async def discover_app_certs(app_id: int, db: AsyncSession = Depends(get_db)):
    """Scan for cert/key files inside the app's working directory only."""
    app = await _get_or_404(app_id, db)

    base = app.working_dir
    if not base or not os.path.isdir(base):
        return {"certs": [], "keys": []}

    _SKIP_DIRS = {"venv", ".venv", "node_modules", "site-packages", "certifi", ".git", "__pycache__", ".tox", ".mypy_cache"}
    _CERT_EXTS = {".pem", ".crt", ".cer"}
    _KEY_EXTS  = {".pem", ".key"}
    _CA_BUNDLE_NAMES = {"cacert.pem", "ca-bundle.crt", "ca-bundle.pem", "ca-certificates.crt"}

    certs: list[str] = []
    keys:  list[str] = []

    for dirpath, dirnames, filenames in os.walk(base):
        # Prune ignored directories in-place so os.walk skips them entirely
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname in _CA_BUNDLE_NAMES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            fpath = os.path.join(dirpath, fname)
            if ext in _CERT_EXTS:
                certs.append(fpath)
            if ext in _KEY_EXTS:
                keys.append(fpath)

    # Heuristic: files with 'key' in name are more likely private keys
    key_set  = sorted({p for p in set(keys)  if "key" in os.path.basename(p).lower() or p.endswith(".key")})
    cert_set = sorted({p for p in set(certs) if "key" not in os.path.basename(p).lower()})
    # Fallback: if no dedicated key files found, show all .pem
    if not key_set:
        key_set = sorted(set(keys))

    return {"certs": cert_set, "keys": key_set}


@router.post("/{app_id}/certs/upload")
async def upload_app_cert(app_id: int, file: UploadFile = File(...), _user: dict = Depends(_auth.require_permission("apps.manage")), db: AsyncSession = Depends(get_db)):
    """Upload a cert/key file into the app's certs subfolder and return its path."""
    app = await _get_or_404(app_id, db)

    allowed_exts = {".pem", ".crt", ".cer", ".key"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(400, "Only .pem, .crt, .cer, .key files are allowed")
    safe_name = os.path.basename(file.filename).replace("..", "").lstrip("/")
    base = app.working_dir or os.path.expanduser(f"~/.cloudbase/certs/{app.name}")
    dest_dir = os.path.join(base, "certs")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    contents = await file.read()
    with open(dest_path, "wb") as f:
        f.write(contents)
    return {"path": dest_path}



async def _sync_process_status(app, db) -> None:
    """Reconcile DB status with actual OS state. Uses port recovery as fallback."""
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        return

    # Instance-based model: sync each local replica's container status
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app.id)
    )
    replicas = replica_result.scalars().all()
    if replicas:
        changed = False
        for replica in replicas:
            # Skip non-local replicas (their status is managed by the remote node)
            if replica.node_id:
                rn_result = await db.execute(select(Node).where(Node.id == replica.node_id))
                r_node = rn_result.scalar_one_or_none()
                if r_node is not None and not r_node.is_local:
                    continue
            # Timeout stuck deploying/starting instances (> 10 min = something went wrong)
            if replica.status in ("deploying", "starting"):
                age = (_dt.datetime.utcnow() - (replica.updated_at or replica.created_at)).total_seconds()
                if age > 600:
                    replica.status = "error"
                    replica.last_error = f"Timed out after {int(age)}s in '{replica.status}' state"
                    changed = True
                continue
            if replica.status == "stopping":
                continue
            alive = await asyncio.to_thread(dm.is_replica_container_running, app.id, replica.id)
            new_status = "running" if alive else "stopped"
            if replica.status != new_status:
                replica.status = new_status
                changed = True
        new_app_status = _derive_app_status_from_instances(replicas)
        if app.status != new_app_status:
            app.status = new_app_status
            changed = True
        if changed:
            await db.commit()
        return

    # Legacy single-container model
    if app.use_docker:
        alive = await asyncio.to_thread(pm.is_docker_app_running, app.id)
        new_status = "running" if alive else "stopped"
        if app.status != new_status:
            app.status = new_status
            await db.commit()
        return
    if not app.pid:
        return
    alive = await asyncio.to_thread(pm.is_process_running, app.pid, app.id)
    if alive:
        app.status = "running"
        return
    # Stored PID is dead — try to recover via port before declaring stopped
    if app.port:
        recovered = await asyncio.to_thread(pm.find_process_by_port, app.port)
        if recovered:
            app.pid = recovered
            app.status = "running"
            await db.commit()
            return
    app.status = "stopped"
    app.pid = None
    await db.commit()


@router.get("")
async def list_apps(request: Request, db: AsyncSession = Depends(get_db)):
    await ensure_local_node(db)
    result = await db.execute(select(Application))
    apps = result.scalars().all()
    node_map = await _load_node_map(db)

    # Batch-fetch all replica rows so we can derive statuses without N+1 Docker checks
    rep_result = await db.execute(select(ApplicationReplica))
    all_replicas = rep_result.scalars().all()
    replica_map: dict[int, list] = {}
    for r in all_replicas:
        replica_map.setdefault(r.app_id, []).append(r)

    # Derive status from replica statuses — no live Docker check needed
    async def _check(app):
        app_replicas = replica_map.get(app.id)
        if app_replicas:
            return app.id, _derive_app_status_from_instances(app_replicas), None
        return app.id, app.status, app.pid

    checks = await asyncio.gather(*[_check(a) for a in apps])

    id_map = {a.id: a for a in apps}
    dirty = False
    for app_id, new_status, new_pid in checks:
        a = id_map[app_id]
        if a.status != new_status or a.pid != new_pid:
            a.status = new_status
            a.pid = new_pid
            dirty = True
    if dirty:
        await db.commit()

    include_sensitive = _request_is_admin(request)
    result_list = []
    for a in apps:
        app_replicas = replica_map.get(a.id, [])
        replica_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in app_replicas]
        result_list.append(
            _app_to_dict(
                a,
                include_sensitive=include_sensitive,
                replicas=replica_dicts,
                include_page_configs=False,
            )
        )
    return result_list


@router.post("")
async def deploy_app(
    req: DeployRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(_auth.get_current_actor),
):
    existing = await db.execute(select(Application).where(Application.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"App '{req.name}' already exists")

    all_domains = [req.domain] + (req.extra_domains or []) + (req.redirect_domains or [])
    await _check_domain_conflicts([d for d in all_domains if d], db)

    app_count_result = await db.execute(select(func.count()).select_from(Application))
    if app_count_result.scalar() >= _cfg.get_limit("max_apps"):
        raise HTTPException(400, f"App limit reached ({_cfg.get_limit('max_apps')} apps maximum). Adjust limits.max_apps in config.yaml to increase.")

    if req.restart_policy is not None and req.restart_policy not in ("no", "always", "on-failure"):
        raise HTTPException(400, "restart_policy must be one of: no, always, on-failure")
    _validate_docker_runtime_settings(req.docker_cpu_limit, req.docker_memory_limit_mb, req.docker_tmpfs_size_mb)

    local_node = await ensure_local_node(db)
    target_node = local_node
    if req.node_id is not None:
        node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        target_node = node_result.scalar_one_or_none()
        if not target_node:
            raise HTTPException(400, "Selected node is not available")

    # no_web apps (discord bots, background workers, etc.) don't need a port or nginx
    is_no_web = bool(req.no_web)
    node_id_for_port = target_node.id if target_node else local_node.id
    external_port = None if is_no_web else await _assign_external_port(req.external_port, node_id_for_port, None, db)

    app = Application(
        name=req.name,
        repo_url=req.repo_url,
        github_token=_encrypt_github_token(_resolve_token(req.github_token, req.github_token_id)),
        domain=req.domain,
        extra_domains=json.dumps(req.extra_domains or []),
        redirect_domains=json.dumps(req.redirect_domains or []),
        ssl_cert_path=req.ssl_cert_path,
        ssl_key_path=req.ssl_key_path,
        start_command=req.start_command,
        port=req.port,
        external_port=external_port,
        env_vars=encrypt_env(req.env_vars or {}),
        auto_start=bool(req.auto_start) if req.auto_start is not None else False,
        restart_policy=req.restart_policy or "no",
        no_web=is_no_web,
        use_docker=True,
        docker_cpu_limit=req.docker_cpu_limit,
        docker_memory_limit_mb=req.docker_memory_limit_mb,
        docker_read_only_root=bool(req.docker_read_only_root) if req.docker_read_only_root is not None else False,
        docker_tmpfs_enabled=bool(req.docker_tmpfs_enabled) if req.docker_tmpfs_enabled is not None else False,
        docker_tmpfs_size_mb=req.docker_tmpfs_size_mb,
        source_revision=req.source_revision,
        image_revision=req.image_revision,
        status="deploying",
        node_id=target_node.id if not target_node.is_local else None,
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    if not target_node.is_local:
        await db.commit()
        await db.refresh(app)
        return _app_to_dict(app)

    try:
        await _deploy_app(app)
        app.status = "stopped"

        if app.domain and not app.no_web:
            app.nginx_enabled = True
            await _write_app_nginx_config(app, db, local_node)

        await log_audit(db, "app.deploy", actor=actor, app_id=app.id, detail={"name": app.name})
        await db.commit()
        await db.refresh(app)
        return _app_to_dict(app)
    except Exception as e:
        app.status = "error"
        await db.commit()
        raise HTTPException(500, str(e))


@router.get("/{app_id}")
async def get_app(app_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    await _sync_process_status(app, db)
    node_map = await _load_node_map(db)
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = replica_result.scalars().all()
    replicas_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in replicas]
    return _app_to_dict(app, include_sensitive=_request_is_admin(request), replicas=replicas_dicts)


@router.get("/{app_id}/stats/history")
async def get_stats_history(
    app_id: int,
    hours: int = Query(24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
):
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    result = await db.execute(
        select(StatsHistory)
        .where(StatsHistory.app_id == app_id, StatsHistory.timestamp >= since)
        .order_by(StatsHistory.timestamp.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "timestamp":   r.timestamp.isoformat() + "Z",
            "cpu_percent": r.cpu_percent,
            "memory_mb":   r.memory_mb,
            "net_mb":      r.net_mb  or 0,
            "disk_mb":     r.disk_mb or 0,
        }
        for r in rows
    ]


@router.put("/{app_id}")
async def update_app(app_id: int, req: UpdateRequest, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    await ensure_local_node(db)
    _validate_docker_runtime_settings(req.docker_cpu_limit, req.docker_memory_limit_mb, req.docker_tmpfs_size_mb)
    dockerfile_changed = False

    if req.domain is not None or req.extra_domains is not None or req.redirect_domains is not None:
        new_primary   = req.domain        if req.domain        is not None else app.domain
        new_extra     = req.extra_domains if req.extra_domains is not None else json.loads(app.extra_domains    or "[]")
        new_redirects = req.redirect_domains if req.redirect_domains is not None else json.loads(app.redirect_domains or "[]")
        all_domains = [new_primary] + (new_extra or []) + (new_redirects or [])
        await _check_domain_conflicts([d for d in all_domains if d], db, exclude_app_id=app.id)

    if req.domain is not None:
        app.domain = req.domain
    if req.extra_domains is not None:
        app.extra_domains = json.dumps(req.extra_domains)
    if req.redirect_domains is not None:
        app.redirect_domains = json.dumps(req.redirect_domains)
    if req.ssl_cert_path is not None:
        app.ssl_cert_path = req.ssl_cert_path
    if req.ssl_key_path is not None:
        app.ssl_key_path = req.ssl_key_path
    if req.start_command is not None:
        if req.start_command != app.start_command:
            dockerfile_changed = True
        app.start_command = req.start_command
        app.app_type = pm.detect_app_type_from_command(req.start_command)
    if req.port is not None:
        if req.port != app.port:
            dockerfile_changed = True
        app.port = req.port
    if req.external_port is not None:
        app.external_port = await _assign_external_port(req.external_port, app.node_id, app.id, db)
    if req.docker_cpu_limit is not None:
        app.docker_cpu_limit = req.docker_cpu_limit
    if req.docker_memory_limit_mb is not None:
        app.docker_memory_limit_mb = req.docker_memory_limit_mb
    if req.docker_read_only_root is not None:
        app.docker_read_only_root = req.docker_read_only_root
    if req.docker_tmpfs_enabled is not None:
        app.docker_tmpfs_enabled = req.docker_tmpfs_enabled
    if req.docker_tmpfs_size_mb is not None:
        app.docker_tmpfs_size_mb = req.docker_tmpfs_size_mb
    if req.env_vars is not None:
        app.env_vars = encrypt_env(req.env_vars)
    resolved = _resolve_token(req.github_token, req.github_token_id)
    if resolved is not None:
        app.github_token = _encrypt_github_token(resolved)
    if req.auto_start is not None:
        app.auto_start = req.auto_start
    if req.restart_policy is not None and req.restart_policy in ("no", "always", "on-failure"):
        app.restart_policy = req.restart_policy
    if req.no_web is not None:
        app.no_web = req.no_web
        if app.no_web:
            app.nginx_enabled = False
            _best_effort_remove_app_nginx(app.name)
    if req.working_dir is not None:
        app.working_dir = req.working_dir
    if req.source_revision is not None:
        app.source_revision = req.source_revision
    if req.image_revision is not None:
        app.image_revision = req.image_revision

    if app.use_docker and dockerfile_changed:
        if app.working_dir and os.path.exists(app.working_dir):
            await asyncio.to_thread(
                dm.ensure_dockerfile,
                app.working_dir,
                app.app_type or "unknown",
                app.start_command or "",
                app.port or 8000,
            )
        # Force image refresh on next start/restart so config changes are applied.
        app.image_revision = None

    if app.domain and not app.no_web:
        maint_ok, maint_msg = _ensure_maintenance_files(app, app.id)
        if not maint_ok:
            raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
        backends = await _get_nginx_backends(app, db)
        config = nm.generate_config(
            app.name, app.domain, backends,
            ssl_cert, ssl_key,
            app_id=app.id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        ok, msg = nm.write_nginx_config(app.name, config)
        app.nginx_enabled = ok
        if not ok:
            raise HTTPException(500, f"Nginx config failed: {msg}")

    await log_audit(db, "app.config_update", actor=actor, app_id=app.id, detail={"name": app.name})
    await db.commit()
    return _app_to_dict(app)


@router.delete("/{app_id}")
async def delete_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status == "online":
            cmd = await queue_node_command(
                db,
                node_id=node.id,
                app_id=app.id,
                command_type="delete_app",
                payload={"app_id": app.id, "app_name": app.name},
            )
            done = await wait_for_node_command(db, cmd.id, timeout_seconds=60)
            if done.status != "done":
                raise HTTPException(500, f"Failed to delete app on node '{node.name}': {done.error_message}")
            _best_effort_remove_app_nginx(app.name)
            await _cleanup_app_dependencies(db, app.id)
            await db.delete(app)
            try:
                await db.commit()
            except IntegrityError as e:
                await db.rollback()
                raise HTTPException(409, f"App '{app.name}' cannot be deleted because it is still referenced") from e
            result_payload = json.loads(done.result or "{}") if done.result else {}
            return {"message": result_payload.get("message") or f"App '{app.name}' deleted"}
        else:
            # Node offline — remove from DB only; node cleans up its own files when it reconnects
            _best_effort_remove_app_nginx(app.name)
            await _cleanup_app_dependencies(db, app.id)
            await db.delete(app)
            try:
                await db.commit()
            except IntegrityError as e:
                await db.rollback()
                raise HTTPException(409, f"App '{app.name}' cannot be deleted because it is still referenced") from e
            return {"message": f"App '{app.name}' removed (node '{node.name}' was offline — app files may still exist on the node)"}

    # Stop and remove all replica containers before deleting
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    node_map = await _load_node_map(db)
    for replica in replica_result.scalars().all():
        r_node = node_map.get(replica.node_id)
        if r_node and r_node.is_local:
            await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
        elif r_node and not r_node.is_local and r_node.status == "online":
            await queue_node_command(
                db, node_id=r_node.id, app_id=app_id,
                command_type="stop_replica",
                payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
            )

    await _best_effort_stop_legacy_app_container(app_id)
    await asyncio.to_thread(dm.remove_image, app_id, app.name)

    _best_effort_remove_app_nginx(app.name)

    app_dir = pm.get_app_dir(app.name)
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)

    app_name = app.name
    app_id_val = app.id
    await _cleanup_app_dependencies(db, app.id)
    await db.delete(app)
    await log_audit(db, "app.delete", actor=actor, detail={"name": app_name, "app_id": app_id_val})
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, f"App '{app_name}' cannot be deleted because it is still referenced") from e
    return {"message": f"App '{app_name}' deleted"}


@router.post("/export")
async def export_apps(req: ExportRequest, _user: dict = Depends(_auth.require_permission("apps.manage")), db: AsyncSession = Depends(get_db)):
    query = select(Application)
    if req.app_ids:
        query = query.where(Application.id.in_(req.app_ids))
    result = await db.execute(query)
    apps = result.scalars().all()

    export_data = []
    for app in apps:
        export_data.append({
            "name": app.name,
            "repo_url": app.repo_url,
            "github_token": _decrypt_github_token(app.github_token),
            "domain": app.domain,
            "extra_domains": json.loads(app.extra_domains or "[]"),
            "redirect_domains": json.loads(app.redirect_domains or "[]"),
            "ssl_cert_path": app.ssl_cert_path,
            "ssl_key_path": app.ssl_key_path,
            "start_command": app.start_command,
            "port": app.port,
            "env_vars": decrypt_env(app.env_vars or ""),
            "auto_start": app.auto_start,
            "restart_policy": app.restart_policy,
            "use_docker": True,
            "docker_cpu_limit": app.docker_cpu_limit,
            "docker_memory_limit_mb": app.docker_memory_limit_mb,
            "docker_read_only_root": bool(app.docker_read_only_root),
            "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
            "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
            "source_revision": app.source_revision,
            "image_revision": app.image_revision,
            "external_port": None,
            "maintenance_mode": app.maintenance_mode,
            "update_mode": app.update_mode,
            "downtime_page": json.loads(app.downtime_page or "{}"),
            "update_page": json.loads(app.update_page or "{}"),
            "restart_page": json.loads(app.restart_page or "{}"),
            "starting_page": json.loads(app.starting_page or "{}"),
        })

    return {"exported_apps": export_data}


@router.post("/import")
async def import_apps(req: ImportRequest, background_tasks: BackgroundTasks, _user: dict = Depends(_auth.require_permission("apps.manage")), db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    
    imported_count = 0
    for app_data in req.apps:
        existing = await db.execute(select(Application).where(Application.name == app_data.get("name")))
        if existing.scalar_one_or_none():
            continue  # Skip existing apps by name

        target_node_id = req.target_node_id or app_data.get("node_id") or local_node.id
        node_result = await db.execute(select(Node).where(Node.id == target_node_id, Node.enabled == True))
        target_node = node_result.scalar_one_or_none()
        if not target_node:
            target_node = local_node

        import_external_port = await _assign_external_port(None, target_node.id, None, db)

        app = Application(
            name=app_data.get("name"),
            repo_url=app_data.get("repo_url"),
            github_token=_encrypt_github_token(app_data.get("github_token")),
            domain=app_data.get("domain"),
            extra_domains=json.dumps(app_data.get("extra_domains") or []),
            redirect_domains=json.dumps(app_data.get("redirect_domains") or []),
            ssl_cert_path=None,
            ssl_key_path=None,
            start_command=app_data.get("start_command"),
            port=app_data.get("port"),
            external_port=import_external_port,
            env_vars=encrypt_env(app_data.get("env_vars") or {}),
            auto_start=bool(app_data.get("auto_start")) if app_data.get("auto_start") is not None else False,
            restart_policy=app_data.get("restart_policy") or "no",
            use_docker=True,
            docker_cpu_limit=app_data.get("docker_cpu_limit"),
            docker_memory_limit_mb=app_data.get("docker_memory_limit_mb"),
            docker_read_only_root=bool(app_data.get("docker_read_only_root")) if app_data.get("docker_read_only_root") is not None else False,
            docker_tmpfs_enabled=bool(app_data.get("docker_tmpfs_enabled")) if app_data.get("docker_tmpfs_enabled") is not None else False,
            docker_tmpfs_size_mb=app_data.get("docker_tmpfs_size_mb"),
            source_revision=app_data.get("source_revision"),
            image_revision=app_data.get("image_revision"),
            status="deploying",
            node_id=target_node.id if not target_node.is_local else None,
            maintenance_mode=app_data.get("maintenance_mode", False),
            update_mode=app_data.get("update_mode", False),
            downtime_page=json.dumps(app_data.get("downtime_page") or {}),
            update_page=json.dumps(app_data.get("update_page") or {}),
            restart_page=json.dumps(app_data.get("restart_page") or {}),
            starting_page=json.dumps(app_data.get("starting_page") or {}),
        )
        db.add(app)
        await db.commit()
        await db.refresh(app)

        if target_node.is_local:
            try:
                await _deploy_app(app)
                app.status = "stopped"
                if app.domain:
                    app.nginx_enabled = True
                    await _write_app_nginx_config(app, db, local_node)
            except Exception as e:
                app.status = "error"
                app.last_error = str(e)
        await db.commit()
        imported_count += 1

    return {"message": f"Successfully imported {imported_count} apps."}


async def _start_instance_local(app: "Application", replica: "ApplicationReplica", env_vars: dict, app_id: int) -> str:
    """Start a local replica container, building the Docker image first if needed."""
    desired_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
    docker_opts = _docker_runtime_options(app)
    # Per-instance overrides
    if replica.docker_cpu_limit is not None:
        docker_opts["cpu_limit"] = replica.docker_cpu_limit
    if replica.docker_memory_limit_mb is not None:
        docker_opts["memory_limit_mb"] = replica.docker_memory_limit_mb
    if replica.docker_read_only_root:
        docker_opts["read_only_root"] = True
    if replica.docker_tmpfs_enabled:
        docker_opts["tmpfs_enabled"] = True
    if replica.docker_tmpfs_size_mb is not None:
        docker_opts["tmpfs_size_mb"] = replica.docker_tmpfs_size_mb

    async def _run_replica(ext_port: int) -> str:
        return await asyncio.to_thread(
            pm.start_docker_replica,
            app_id, replica.id, app.name,
            app.port or 8000,
            ext_port,
            env_vars, docker_opts,
            app_id,  # image_app_id
        )

    async def _build_image() -> None:
        if not app.working_dir:
            raise HTTPException(400, "No working directory — deploy the app first")
        def _push(_aid, line):
            pm._push_line(app_id, str(line))
        await asyncio.to_thread(
            dm.build_image,
            app_id, app.name, app.working_dir, _push,
            app.app_type or "unknown", app.start_command or "", app.port or 8000,
        )
        app.docker_image = dm.image_name(app_id, app.name)
        built_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
        if built_revision:
            app.image_revision = built_revision

    ext_port = replica.external_port or app.port or 8000
    if desired_revision and app.image_revision != desired_revision:
        await _build_image()
    try:
        return await _run_replica(ext_port)
    except Exception as first_exc:
        msg = str(first_exc)
        bind_conflict = (
            "bind for 0.0.0.0" in msg.lower()
            or "port is already allocated" in msg.lower()
            or "failed to set up container networking" in msg.lower()
        )
        if bind_conflict:
            # Port taken — grab a free one via socket scan and retry (no build needed)
            import socket as _sock
            new_port = ext_port + 1
            while True:
                with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
                    if _s.connect_ex(('127.0.0.1', new_port)) != 0:
                        break
                new_port += 1
            replica.external_port = new_port
            pm._push_line(app_id, f"[Docker] Port conflict on :{ext_port}; retrying replica on :{new_port}")
            try:
                return await _run_replica(new_port)
            except Exception as retry_exc:
                raise HTTPException(500, f"Failed to start instance: {retry_exc}") from retry_exc
        # Not a port conflict — image may not exist yet (first start after deploy)
        await _build_image()
        try:
            return await _run_replica(ext_port)
        except Exception as build_retry_exc:
            msg2 = str(build_retry_exc)
            bind2 = (
                "bind for 0.0.0.0" in msg2.lower()
                or "port is already allocated" in msg2.lower()
                or "failed to set up container networking" in msg2.lower()
            )
            if not bind2:
                raise HTTPException(500, f"Failed to start instance: {build_retry_exc}") from build_retry_exc
            import socket as _sock2
            new_port2 = ext_port + 1
            while True:
                with _sock2.socket(_sock2.AF_INET, _sock2.SOCK_STREAM) as s:
                    if s.connect_ex(('127.0.0.1', new_port2)) != 0:
                        break
                new_port2 += 1
            replica.external_port = new_port2
            pm._push_line(app_id, f"[Docker] Port conflict after build on :{ext_port}; retrying on :{new_port2}")
            return await _run_replica(new_port2)


def _derive_app_status_from_instances(replicas: list) -> str:
    """Derive the overall app status from its instance statuses."""
    statuses = {r.status for r in replicas}
    if "running" in statuses:
        return "running"
    if "starting" in statuses:
        return "starting"
    if "restarting" in statuses:
        return "restarting"
    if "stopping" in statuses:
        return "stopping"
    if "deploying" in statuses:
        return "deploying"
    if statuses and statuses - {"stopped", "error", "pending"} == set():
        if statuses == {"error"} or (statuses - {"stopped", "pending"} == {"error"}):
            return "error"
    return "stopped"


async def _start_replica_runtime(
    app: Application,
    replica: ApplicationReplica,
    replica_node: Optional[Node],
    env_vars: dict,
    db: AsyncSession,
) -> None:
    if replica_node is None:
        replica.status = "error"
        replica.last_error = "Replica node not found"
        return

    if replica_node.is_local:
        cid = await _start_instance_local(app, replica, env_vars, app.id)
        replica.status = "running"
        replica.container_id = cid
        replica.last_error = None
        return

    if replica_node.status != "online" or not replica_node.enabled:
        replica.status = "error"
        replica.last_error = f"Node '{replica_node.name}' is offline"
        return

    remote_payload = _remote_replica_command_payload(
        app,
        env_vars,
        replica.external_port or app.port or 8000,
    )
    await queue_node_command(
        db,
        node_id=replica_node.id,
        app_id=app.id,
        command_type="start_replica",
        payload={**remote_payload, "replica_id": replica.id},
    )
    replica.status = "starting"
    replica.last_error = None


async def _stop_replica_runtime(
    app: Application,
    replica: ApplicationReplica,
    replica_node: Optional[Node],
    db: AsyncSession,
) -> None:
    if replica_node is None or replica_node.is_local:
        await asyncio.to_thread(pm.stop_docker_replica, app.id, replica.id)
        replica.status = "stopped"
        replica.container_id = None
        replica.tunnel_port = None
        replica.last_error = None
        return

    if replica_node.status == "online" and replica_node.enabled:
        await queue_node_command(
            db,
            node_id=replica_node.id,
            app_id=app.id,
            command_type="stop_replica",
            payload={"app_id": app.id, "replica_id": replica.id, "app_name": app.name},
        )
        replica.status = "stopping"
        replica.last_error = None
        return

    replica.status = "stopped"
    replica.container_id = None
    replica.tunnel_port = None
    replica.last_error = None


def _has_live_replicas(replicas: list[ApplicationReplica]) -> bool:
    return any(r.status in ("running", "starting", "restarting") for r in replicas)


def _aggregate_replica_stats_frames(frames: list[dict]) -> dict:
    if not frames:
        return {"status": "stopped", "docker": True}

    def _sum(key: str) -> float:
        return round(sum((frame.get(key) or 0) for frame in frames), 2)

    def _avg(key: str) -> float:
        values = [frame.get(key) for frame in frames if frame.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else 0.0

    def _max(key: str) -> Optional[float]:
        values = [frame.get(key) for frame in frames if frame.get(key) is not None]
        return max(values) if values else None

    return {
        "status": "running",
        "docker": True,
        "cpu_percent": _avg("cpu_percent"),
        "memory_mb": _sum("memory_mb"),
        "memory_vms_mb": _sum("memory_vms_mb"),
        "net_rx_mb": _sum("net_rx_mb"),
        "net_tx_mb": _sum("net_tx_mb"),
        "disk_read_mb": _sum("disk_read_mb"),
        "disk_write_mb": _sum("disk_write_mb"),
        "uptime_seconds": _max("uptime_seconds"),
    }


async def _get_local_replica_stats(
    app: Application,
    db: AsyncSession,
    local_node: Node,
) -> dict:
    replicas = await _load_app_replicas(app.id, db)
    frames: list[dict] = []
    for replica in replicas:
        replica_node = _resolve_replica_node(replica, {}, local_node)
        if replica_node is None or not replica_node.is_local:
            continue
        if replica.status != "running":
            continue
        stats = await asyncio.to_thread(
            dm.get_container_stats_by_name,
            dm.replica_container_name(app.id, replica.id),
        )
        if stats:
            frames.append(stats)
    return _aggregate_replica_stats_frames(frames)


async def _get_local_replica_logs(
    app: Application,
    db: AsyncSession,
    local_node: Node,
    limit: int,
) -> list[str]:
    buffered = list(pm.log_buffers.get(app.id, []))
    if buffered:
        return buffered[-limit:]

    replicas = await _load_app_replicas(app.id, db)
    lines: list[str] = []
    for replica in replicas:
        replica_node = _resolve_replica_node(replica, {}, local_node)
        if replica_node is None or not replica_node.is_local:
            continue
        lines.extend(
            await asyncio.to_thread(
                dm.get_recent_container_logs_by_name,
                dm.replica_container_name(app.id, replica.id),
                limit,
            )
        )
    return lines[-limit:]


@router.post("/{app_id}/start")
async def start_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)
    if not replicas:
        raise HTTPException(400, "No instances configured. Add an instance first before starting the app.")
    had_live_replicas = _has_live_replicas(replicas)
    show_start_page = (
        not had_live_replicas
        and _has_public_nginx_domain(app)
        and not app.maintenance_mode
        and not app.update_mode
    )

    if show_start_page:
        transition_started_at = _set_active_transition_mode(app_id, "starting")
        await _write_app_nginx_config(app, db, local_node, mode="starting")
        pm._push_line(app_id, "Starting instances...")

    await _best_effort_stop_legacy_app_container(app_id)

    env_vars = decrypt_env(app.env_vars or "")
    source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
    node_map = await _load_node_map(db)
    for replica in replicas:
        if replica.status in ("running", "starting", "restarting"):
            continue
        if replica.status not in ("stopped", "error", "pending", "node_offline"):
            continue
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        await _start_replica_runtime(app, replica, replica_node, env_vars, db)

    app.status = _derive_app_status_from_instances(replicas)
    app.pid = None
    if _has_public_nginx_domain(app) and not show_start_page:
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.start", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    if show_start_page:
        asyncio.create_task(
            _restore_nginx_after_transition(
                app_id,
                transition_started_at,
                "start",
            )
        )

    return {"status": app.status, "instance_count": len(replicas)}


@router.post("/{app_id}/stop")
async def stop_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)

    await _best_effort_stop_legacy_app_container(app_id)

    if not replicas:
        app.status = "stopped"
        app.pid = None
        if _has_public_nginx_domain(app):
            await _write_app_nginx_config(app, db, local_node)
        await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
        await db.commit()
        return {"status": "stopped", "instance_count": 0}

    node_map = await _load_node_map(db)
    for replica in replicas:
        if replica.status not in ("running", "starting", "restarting", "stopping", "node_offline"):
            continue
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        await _stop_replica_runtime(app, replica, replica_node, db)

    app.status = _derive_app_status_from_instances(replicas)
    app.pid = None
    if _has_public_nginx_domain(app):
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": app.status, "instance_count": len(replicas)}


@router.post("/{app_id}/restart")
async def restart_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    replicas = await _load_app_replicas(app_id, db)
    if not replicas:
        raise HTTPException(400, "No instances configured. Add an instance first before restarting the app.")
    env_vars = decrypt_env(app.env_vars or "")
    node_map = await _load_node_map(db)

    show_restart_page = (
        _has_live_replicas(replicas)
        and _has_public_nginx_domain(app)
        and not app.maintenance_mode
        and not app.update_mode
    )
    transition_started_at: Optional[float] = None
    if show_restart_page:
        transition_started_at = _set_active_transition_mode(app_id, "restart")
        await _write_app_nginx_config(app, db, local_node, mode="restart")
        pm._push_line(app_id, "Restarting instances...")

    await _best_effort_stop_legacy_app_container(app_id)

    for replica in replicas:
        replica_node = _resolve_replica_node(replica, node_map, local_node)
        if replica_node is None:
            replica.status = "error"
            replica.last_error = "Replica node not found"
            continue

        is_live = replica.status in ("running", "starting", "restarting", "stopping", "node_offline")
        if replica_node.is_local:
            if is_live:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
            replica.status = "stopped"
            replica.container_id = None
            replica.tunnel_port = None
            await _start_replica_runtime(app, replica, replica_node, env_vars, db)
            continue

        if replica_node.status != "online" or not replica_node.enabled:
            replica.status = "error"
            replica.last_error = f"Node '{replica_node.name}' is offline"
            continue

        if is_live:
            await queue_node_command(
                db,
                node_id=replica_node.id,
                app_id=app.id,
                command_type="stop_replica",
                payload={"app_id": app.id, "replica_id": replica.id, "app_name": app.name},
            )
        remote_payload = _remote_replica_command_payload(
            app,
            env_vars,
            replica.external_port or app.port or 8000,
        )
        await queue_node_command(
            db,
            node_id=replica_node.id,
            app_id=app.id,
            command_type="start_replica",
            payload={**remote_payload, "replica_id": replica.id},
        )
        replica.status = "starting"
        replica.last_error = None

    app.status = _derive_app_status_from_instances(replicas)
    app.pid = None
    if _has_public_nginx_domain(app) and not show_restart_page:
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "app.restart", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    if show_restart_page and transition_started_at is not None:
        asyncio.create_task(
            _restore_nginx_after_transition(
                app_id,
                transition_started_at,
                "restart",
            )
        )

    return {"status": app.status, "instance_count": len(replicas)}


class ScaleRequest(BaseModel):
    node_id: Optional[int] = None
    docker_cpu_limit: Optional[float] = None
    docker_memory_limit_mb: Optional[int] = None
    docker_read_only_root: Optional[bool] = None
    docker_tmpfs_enabled: Optional[bool] = None
    docker_tmpfs_size_mb: Optional[int] = None


class RunReplicaRequest(BaseModel):
    replica_id: int
    internal_port: int
    external_port: int
    env_vars: Optional[dict] = None
    docker_options: Optional[dict] = None
    local_app_id: Optional[int] = None
    # app_name is supplied by the remote agent so this endpoint never needs a
    # local DB lookup (the app only lives in the main node's database).
    app_name: Optional[str] = None



@router.post("/{app_id}/replicas/run-remote")
async def run_replica_remote(app_id: int, req: RunReplicaRequest, db: AsyncSession = Depends(get_db)):
    """Internal endpoint called by the node agent to start a replica container locally.
    The agent guarantees the image is already built before calling this endpoint.
    Does NOT create a DB row — the main server already has it."""
    if req.local_app_id is not None:
        local_app = await _get_or_404(req.local_app_id, db)
        app_name = local_app.name
    elif req.app_name:
        app_name = req.app_name
    else:
        app = await _get_or_404(app_id, db)
        app_name = app.name

    # Image is named after local_app_id (built under that id on this node)
    build_app_id = req.local_app_id if req.local_app_id is not None else app_id

    try:
        env_vars = req.env_vars or {}
        container_id = await asyncio.to_thread(
            pm.start_docker_replica,
            app_id, req.replica_id, app_name,
            req.internal_port, req.external_port,
            env_vars, req.docker_options,
            build_app_id,
        )
        return {"container_id": container_id, "replica_id": req.replica_id}
    except Exception as e:
        import logging as _logging
        _logging.getLogger("cloudbase.apps").error(
            "run-remote failed app_id=%d replica_id=%d local_app_id=%s build_app_id=%d: %s",
            app_id, req.replica_id, req.local_app_id, build_app_id, e, exc_info=True,
        )
        raise HTTPException(500, str(e)) from e


@router.delete("/{app_id}/replicas/{replica_id}/stop-remote")
async def stop_replica_remote(app_id: int, replica_id: int):
    """Internal endpoint called by the node agent to stop a replica container locally."""
    ok = await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)
    return {"ok": ok, "replica_id": replica_id}


@router.get("/{app_id}/replicas/aggregate-stats")
async def get_replica_aggregate_stats(app_id: int):
    """Internal endpoint called by the node agent to collect stats across all local
    replica containers for app_id.  Returns aggregated cpu/memory/net/disk numbers
    plus status=running when at least one container is up."""
    import re as _re
    client = dm._get_client()
    pattern = _re.compile(rf"^cloudbase-app-{app_id}-replica-(\d+)$")
    try:
        containers = client.containers.list(filters={"name": f"cloudbase-app-{app_id}-replica-"})
    except Exception:
        return {"status": "stopped", "docker": True}

    running = [c for c in containers if pattern.match(c.name) and c.status == "running"]
    if not running:
        return {"status": "stopped", "docker": True}

    frames = []
    for c in running:
        s = await asyncio.to_thread(dm.get_container_stats_by_name, c.name)
        if s:
            frames.append(s)

    if not frames:
        return {"status": "stopped", "docker": True}

    def _sum(k):
        return round(sum(f.get(k) or 0 for f in frames), 2)

    def _avg(k):
        vals = [f[k] for f in frames if f.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    def _max(k):
        vals = [f[k] for f in frames if f.get(k) is not None]
        return max(vals) if vals else None

    return {
        "status": "running",
        "docker": True,
        "cpu_percent":    _avg("cpu_percent"),
        "memory_mb":      _sum("memory_mb"),
        "memory_vms_mb":  _sum("memory_vms_mb"),
        "net_rx_mb":      _sum("net_rx_mb"),
        "net_tx_mb":      _sum("net_tx_mb"),
        "disk_read_mb":   _sum("disk_read_mb"),
        "disk_write_mb":  _sum("disk_write_mb"),
        "uptime_seconds": _max("uptime_seconds"),
    }


@router.get("/{app_id}/replicas/{replica_id}/stats-remote")
async def get_replica_stats_remote(app_id: int, replica_id: int):
    """Internal endpoint called by node-agent to get one replica container's stats.

    This avoids app-level aggregate ambiguity when replica mapping is in transition.
    """
    import time as _time
    cname = dm.replica_container_name(app_id, replica_id)
    stats = await asyncio.to_thread(dm.get_container_stats_by_name, cname)
    if not stats or stats.get("status") != "running":
        return {"status": "stopped", "docker": True}
    stats["timestamp"] = int(_time.time() * 1000)
    stats["replica_id"] = replica_id
    return stats


@router.get("/{app_id}/replicas")
async def list_replicas(app_id: int, db: AsyncSession = Depends(get_db)):
    await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = result.scalars().all()
    return [_replica_to_dict(r, node_map.get(r.node_id)) for r in replicas]


@router.get("/{app_id}/instances")
async def list_instances(app_id: int, db: AsyncSession = Depends(get_db)):
    """Return all instances (ApplicationReplica rows) for an app."""
    await _get_or_404(app_id, db)
    node_map = await _load_node_map(db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
        .order_by(ApplicationReplica.id)
    )
    return [_replica_to_dict(r, node_map.get(r.node_id)) for r in rep_result.scalars().all()]


@router.get("/{app_id}/instances/stats-debug")
async def debug_instance_stats(app_id: int, db: AsyncSession = Depends(get_db)):
    """Debug endpoint: run one poll cycle for remote replicas and return raw results."""
    from database import AsyncSessionLocal as _ASL
    from routers.nodes import queue_node_command, wait_for_node_command, _node_ws_connections
    await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app_id,
            ApplicationReplica.status == "running",
            ApplicationReplica.node_id.isnot(None),
            ApplicationReplica.node_id != local_node.id,
        )
    )
    remote_replicas = rep_result.scalars().all()
    if not remote_replicas:
        return {"error": "no remote running replicas found", "local_node_id": local_node.id}

    results = []
    for r in remote_replicas:
        entry = {"replica_id": r.id, "node_id": r.node_id, "app_id": r.app_id,
                 "ws_connected": r.node_id in _node_ws_connections}
        if r.node_id not in _node_ws_connections:
            entry["skip_reason"] = "no agent websocket"
            results.append(entry)
            continue
        try:
            app_r = await db.execute(select(Application).where(Application.id == r.app_id))
            app_obj = app_r.scalar_one_or_none()
            async with _ASL() as cmd_db:
                cmd = await queue_node_command(
                    cmd_db, node_id=r.node_id, app_id=r.app_id,
                    command_type="get_replica_stats",
                    payload={"app_id": r.app_id, "app_name": app_obj.name if app_obj else "", "replica_id": r.id},
                    allow_existing_inflight=True,
                )
            async with _ASL() as wait_db:
                done = await wait_for_node_command(wait_db, cmd.id, timeout_seconds=12)
            entry["cmd_id"] = cmd.id
            entry["cmd_status"] = done.status
            entry["cmd_error"] = done.error_message
            entry["raw_result"] = json.loads(done.result) if done.result else None
        except Exception as e:
            entry["exception"] = str(e)
        results.append(entry)
    return results


@router.get("/{app_id}/instances/stats")
async def get_instance_stats(app_id: int, db: AsyncSession = Depends(get_db)):
    """Return the latest stats snapshot for each replica of this app."""
    await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = rep_result.scalars().all()
    replica_ids = [r.id for r in replicas]
    snapshots = pm.get_all_replica_stats(app_id, replica_ids)

    # On-demand fallback: if a remote running replica has no cached snapshot,
    # fetch its stats directly from the node agent once.
    missing_remote_running = [
        r for r in replicas
        if r.id not in snapshots and r.status in ("running", "starting") and r.node_id is not None
    ]
    if missing_remote_running:
        node_map = await _load_node_map(db)
        for r in missing_remote_running:
            node = node_map.get(r.node_id)
            if not node or node.is_local or node.status != "online":
                continue
            try:
                cmd = await queue_node_command(
                    db,
                    node_id=node.id,
                    app_id=r.app_id,
                    command_type="get_replica_stats",
                    payload={"app_id": r.app_id, "app_name": "", "replica_id": r.id},
                    allow_existing_inflight=True,
                )
                done = await wait_for_node_command(db, cmd.id, timeout_seconds=8)
                if done.status == "done" and done.result:
                    payload = json.loads(done.result or "{}")
                    if payload.get("cpu_percent") is not None:
                        pm.set_replica_stats(r.id, {"replica_id": r.id, **payload})
                        snapshots[r.id] = {"replica_id": r.id, **payload}
            except Exception:
                pass

    return {
        str(rid): {
            "cpu_percent":  round(s.get("cpu_percent", 0), 1),
            "memory_mb":    round(s.get("memory_mb", 0), 0),
            "net_rx_mb":    round(s.get("net_rx_mb", 0), 2),
            "net_tx_mb":    round(s.get("net_tx_mb", 0), 2),
            "uptime_seconds": s.get("uptime_seconds"),
            "timestamp":    s.get("timestamp"),
        }
        for rid, s in snapshots.items()
    }


@router.get("/{app_id}/replicas/{replica_id}/logs")
async def get_replica_logs(
    app_id: int,
    replica_id: int,
    lines: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """Fetch recent log lines for a specific replica container.

    For local replicas: reads directly from docker logs.
    For remote replicas: queues a get_logs_tail agent command with the container name.
    """
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == replica_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Replica not found")

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    rep_node = (node_result.scalar_one_or_none() if node_result else None)

    container_name = dm.replica_container_name(app_id, replica_id)

    if rep_node is None or rep_node.is_local:
        # Local — read directly
        try:
            raw = await asyncio.to_thread(
                lambda: subprocess.check_output(
                    ["docker", "logs", "--tail", str(lines), container_name],
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
            return {"lines": raw.splitlines(), "remote": False}
        except Exception as e:
            return {"lines": [], "remote": False, "error": str(e)}

    if rep_node.status != "online":
        return {"lines": [], "remote": True, "error": "Node is offline"}

    cmd = await queue_node_command(
        db,
        node_id=rep_node.id,
        app_id=app_id,
        command_type="get_replica_logs",
        payload={
            "app_id": app_id,
            "app_name": app.name,
            "replica_id": replica_id,
            "container_name": container_name,
            "lines": lines,
        },
    )
    done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
    if done.status != "done":
        return {"lines": [], "remote": True, "error": done.error_message}
    result_payload = json.loads(done.result or "{}") if done.result else {}
    return {"lines": result_payload.get("lines", []) or [], "remote": True}


@router.post("/{app_id}/instances/{instance_id}/restart")
async def restart_instance(
    app_id: int,
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(_auth.get_current_actor),
):
    """Restart a single application instance."""
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == instance_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Instance not found")

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    replica_node = node_result.scalar_one_or_none() if node_result else None

    env_vars = decrypt_env(app.env_vars or "")

    if replica_node is None or replica_node.is_local:
        if replica.status == "running":
            await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
        await asyncio.sleep(1)
        try:
            cid = await _start_instance_local(app, replica, env_vars, app_id)
            replica.status = "running"
            replica.container_id = cid
            replica.last_error = None
        except HTTPException:
            raise
        except Exception as e:
            replica.status = "error"
            replica.last_error = str(e)
    elif replica_node.status == "online":
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="stop_replica",
            payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
        )
        remote_payload = _remote_replica_command_payload(
            app, env_vars, replica.external_port or app.port or 8000
        )
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="start_replica",
            payload={**remote_payload, "replica_id": replica.id},
        )
        replica.status = "starting"
    else:
        raise HTTPException(400, f"Node '{replica_node.name}' is offline")

    await log_audit(db, "instance.restart", actor=actor, app_id=app_id,
                    detail={"name": app.name, "instance_id": instance_id})
    if _has_public_nginx_domain(app):
        local_node = await ensure_local_node(db)
        await _write_app_nginx_config(app, db, local_node)
    await db.commit()
    return {"status": replica.status, "instance_id": instance_id}


@router.delete("/{app_id}/instances/{instance_id}")
async def delete_instance(
    app_id: int,
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(_auth.get_current_actor),
):
    """Stop and remove a single application instance."""
    app = await _get_or_404(app_id, db)
    rep_result = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.id == instance_id,
            ApplicationReplica.app_id == app_id,
        )
    )
    replica = rep_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Instance not found")

    node_result = await db.execute(select(Node).where(Node.id == replica.node_id)) if replica.node_id else None
    replica_node = node_result.scalar_one_or_none() if node_result else None

    if replica_node is None or replica_node.is_local:
        # Always attempt stop for any non-terminal status — ignore errors if container
        # doesn't exist (deploying failed, already stopped, etc.)
        if replica.status not in ("stopped", "error"):
            try:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
            except Exception:
                pass
    elif replica.status not in ("stopped", "error", "deploying"):
        # Queue stop command even when the node is offline — it will be dispatched
        # as soon as the node reconnects, preventing orphaned containers.
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="stop_replica",
            payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
        )
    # Always delete the DB row regardless of container state or node availability

    await db.delete(replica)
    await db.flush()

    remaining_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    remaining = remaining_result.scalars().all()
    app.status = _derive_app_status_from_instances(remaining) if remaining else "stopped"

    # Regenerate nginx config without the removed backend.
    if _has_public_nginx_domain(app):
        local_node = await ensure_local_node(db)
        await _write_app_nginx_config(app, db, local_node)

    await log_audit(db, "instance.delete", actor=actor, app_id=app_id,
                    detail={"name": app.name, "instance_id": instance_id})
    await db.commit()
    return {"status": "deleted", "instance_id": instance_id}


@router.post("/{app_id}/scale")
async def scale_app(app_id: int, req: ScaleRequest, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    instance_count_result = await db.execute(select(func.count()).select_from(ApplicationReplica))
    if instance_count_result.scalar() >= _cfg.get_limit("max_instances"):
        raise HTTPException(400, f"Instance limit reached ({_cfg.get_limit('max_instances')} instances maximum). Adjust limits.max_instances in config.yaml to increase.")

    # Resolve target node for the new replica
    if req.node_id is not None:
        target_node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        target_node = target_node_result.scalar_one_or_none()
        if not target_node:
            raise HTTPException(400, "Target node not found or not enabled")
    else:
        target_node = local_node

    external_port = await _assign_external_port(None, target_node.id, None, db)

    replica = ApplicationReplica(
        app_id=app_id,
        node_id=target_node.id,
        external_port=external_port,
        status="pending",
    )
    db.add(replica)
    await db.flush()  # get replica.id before starting container

    env_vars = decrypt_env(app.env_vars or "")

    # Per-instance docker options override app-level defaults
    def _instance_docker_options() -> dict:
        base = _docker_runtime_options(app)
        if req.docker_cpu_limit is not None:
            base["cpu_limit"] = req.docker_cpu_limit
        if req.docker_memory_limit_mb is not None:
            base["memory_limit_mb"] = req.docker_memory_limit_mb
        if req.docker_read_only_root is not None:
            base["read_only_root"] = req.docker_read_only_root
        if req.docker_tmpfs_enabled is not None:
            base["tmpfs_enabled"] = req.docker_tmpfs_enabled
        if req.docker_tmpfs_size_mb is not None:
            base["tmpfs_size_mb"] = req.docker_tmpfs_size_mb
        return base

    if target_node.is_local:
        try:
            replica.docker_cpu_limit = req.docker_cpu_limit
            replica.docker_memory_limit_mb = req.docker_memory_limit_mb
            replica.docker_read_only_root = bool(req.docker_read_only_root) if req.docker_read_only_root is not None else False
            replica.docker_tmpfs_enabled = bool(req.docker_tmpfs_enabled) if req.docker_tmpfs_enabled is not None else False
            replica.docker_tmpfs_size_mb = req.docker_tmpfs_size_mb
            container_id = await _start_instance_local(app, replica, env_vars, app_id)
            replica.status = "running"
            replica.container_id = container_id
        except Exception as e:
            replica.status = "error"
            replica.last_error = str(e)
            await db.commit()
            raise HTTPException(500, f"Failed to start replica: {e}") from e

        # Regenerate nginx with new backend — flush first so the new running replica is visible.
        if _has_public_nginx_domain(app):
            await db.flush()
            await _write_app_nginx_config(app, db, local_node)
    else:
        if target_node.status != "online":
            raise HTTPException(400, f"Node '{target_node.name}' is not online")
        remote_payload = _remote_replica_command_payload(app, env_vars, external_port)
        # Merge per-instance docker overrides into the payload
        inst_opts = _instance_docker_options()
        remote_payload["docker_cpu_limit"] = inst_opts.get("cpu_limit")
        remote_payload["docker_memory_limit_mb"] = inst_opts.get("memory_limit_mb")
        remote_payload["docker_read_only_root"] = inst_opts.get("read_only_root")
        remote_payload["docker_tmpfs_enabled"] = inst_opts.get("tmpfs_enabled")
        remote_payload["docker_tmpfs_size_mb"] = inst_opts.get("tmpfs_size_mb")
        await queue_node_command(
            db, node_id=target_node.id, app_id=app_id,
            command_type="start_replica",
            payload={**remote_payload, "replica_id": replica.id},
        )
        replica.status = "starting"

    await log_audit(db, "app.scale_up", actor=actor, app_id=app_id, detail={"replica_id": replica.id, "node": target_node.name})
    await db.commit()
    await db.refresh(replica)
    return _replica_to_dict(replica, target_node)


@router.delete("/{app_id}/replicas/{replica_id}")
async def remove_replica(app_id: int, replica_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    """Alias for DELETE /instances/{id} — kept for backwards compatibility."""
    return await delete_instance(app_id, replica_id, db=db, actor=actor)


@router.post("/{app_id}/pull")
async def git_pull(app_id: int, payload: PullRequest | None = Body(default=None), db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    target_commit = (payload.commit.strip() if payload and payload.commit else None)
    app_dir = pm.get_app_dir(app.name)

    if not os.path.exists(app_dir):
        # The directory is missing! Let's try to restore it by re-deploying.
        log.warning("[git-pull] Directory %s missing for app %s, attempting re-clone", app_dir, app.name)
        try:
            await _deploy_app(app)
            await db.commit()
            return {
                "message": "App directory was missing; performed a fresh clone successfully",
                "output": "Fresh clone completed.",
                "source_revision": app.source_revision,
            }
        except Exception as e:
            raise HTTPException(500, f"App directory was missing and re-clone failed: {e}")

    github_token = _decrypt_github_token(app.github_token)

    def _git_ops() -> tuple[str, str, str, Optional[str]]:
        if github_token:
            url = _build_clone_url(app.repo_url, github_token)
            subprocess.run(["git", "remote", "set-url", "origin", url], cwd=app_dir, capture_output=True)
        branch = _current_branch(app_dir)
        _fetch_origin(app_dir, branch)
        target = target_commit or f"origin/{branch}"
        reset = subprocess.run(["git", "reset", "--hard", target], cwd=app_dir, capture_output=True, text=True)
        if reset.returncode != 0 and not target_commit:
            reset = subprocess.run(["git", "reset", "--hard", "@{u}"], cwd=app_dir, capture_output=True, text=True)
        if reset.returncode != 0:
            raise HTTPException(500, f"Git reset failed: {reset.stderr}")
        log_res = subprocess.run(["git", "log", "-1", "--format=%h - %s (%cr)"], cwd=app_dir, capture_output=True, text=True)
        commit_info = log_res.stdout.strip() if log_res.returncode == 0 else "Unknown"
        rev = _resolve_source_revision(app_dir)
        return branch, reset.stdout.strip(), commit_info, rev

    branch, reset_stdout, commit_info, source_revision = await asyncio.to_thread(_git_ops)
    if source_revision:
        app.source_revision = source_revision

    if app.use_docker:
        was_running = pm.is_docker_app_running(app_id)
        action_logs: list[str] = [f"[Git] Updated code to {target_commit or branch} ({commit_info})"]
        action_logs.append("[Docker] Rebuilding image...")

        def _push(aid, line):
            _ = aid
            action_logs.append(str(line))

        try:
            await asyncio.to_thread(
                dm.build_image,
                app_id, app.name, app_dir, _push,
                app.app_type or "unknown", app.start_command or "",
                app.port or 8000,
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to rebuild Docker image: {e}") from e

        app.docker_image = dm.image_name(app_id, app.name)
        if source_revision:
            app.image_revision = source_revision
        await log_audit(db, "app.pull", actor=actor, app_id=app_id, detail={"name": app.name, "commit": commit_info})

        # Push updated source to all remote nodes that have instances for this app.
        # Offline nodes keep the queued refresh and catch up when they reconnect.
        remote_replica_result = await db.execute(
            select(ApplicationReplica, Node)
            .join(Node, ApplicationReplica.node_id == Node.id)
            .where(ApplicationReplica.app_id == app_id, Node.is_local == False)
        )
        remote_nodes_notified: set[int] = set()
        for _, r_node in remote_replica_result.all():
            if r_node.id not in remote_nodes_notified:
                await queue_node_command(
                    db, node_id=r_node.id, app_id=app_id,
                    command_type="refresh_source",
                    payload={
                        "app_id": app_id,
                        "app_name": app.name,
                        "commit": commit_info,
                        "source_revision": source_revision,
                    },
                )
                action_logs.append(f"[Remote] Queued source refresh on node '{r_node.name}'.")
                remote_nodes_notified.add(r_node.id)

        await db.commit()
        if was_running:
            action_logs.append("[Docker] Image rebuilt. Running container left untouched (no stop/restart). Restart manually to use the new image.")
        else:
            action_logs.append("[Docker] Image rebuilt. Start the app to apply changes.")
        return {
            "message": f"Updated and rebuilt Docker image from {target_commit or branch}",
            "output": (
                f"Latest commit: {commit_info}\n\nImage rebuilt. Running container was not stopped or restarted. "
                "Restart manually when you want to switch to the new image."
                if was_running
                else f"Latest commit: {commit_info}\n\nImage rebuilt. Start the app to apply changes."
            ),
            "commit": commit_info,
            "source_revision": source_revision,
            "action_logs": action_logs,
        }

    app.image_revision = None
    await db.commit()
    return {
        "message": f"Updated code to {target_commit or branch}",
        "output": f"{reset_stdout}\nLatest commit: {commit_info}\n\nNote: You may need to RESTART the app to apply changes.",
        "commit": commit_info,
        "source_revision": source_revision,
    }


@router.post("/{app_id}/rebuild")
async def rebuild_docker_image(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)

    if not app.use_docker:
        raise HTTPException(400, "Rebuild is only available for Docker apps")
    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    was_running = pm.is_docker_app_running(app_id)
    action_logs: list[str] = ["[Docker] Rebuilding image..."]

    def _push(aid, line):
        _ = aid
        action_logs.append(str(line))

    try:
        img = await asyncio.to_thread(
            dm.build_image,
            app_id, app.name, app.working_dir, _push,
            app.app_type or "unknown", app.start_command or "", app.port or 8000,
        )
        source_revision = await asyncio.to_thread(_refresh_app_source_revision, app)
        app.status = "running" if was_running else "stopped"
        app.docker_image = img
        if source_revision:
            app.image_revision = source_revision
        await log_audit(db, "app.rebuild", actor=actor, app_id=app_id, detail={"name": app.name, "image": img})
        await db.commit()
        if was_running:
            action_logs.append("[Docker] Image rebuilt. Running container left untouched (no restart). Restart manually to use the new image.")
            return {
                "status": "running",
                "message": "Image rebuilt. Running container was not restarted.",
                "output": "Image rebuilt. Running container was not restarted. Restart manually to switch to the new image.",
                "source_revision": source_revision,
                "action_logs": action_logs,
            }
        action_logs.append("[Docker] Image rebuilt. Start the app to run it.")
        return {
            "status": "rebuilt",
            "message": "Image rebuilt. Start the app to run it.",
            "output": "Image rebuilt. Start the app to run it.",
            "source_revision": source_revision,
            "action_logs": action_logs,
        }
    except RuntimeError as e:
        msg = str(e)
        if "already in progress" in msg.lower():
            action_logs.append(f"[Docker] {msg}.")
            return {
                "status": "in_progress",
                "message": "A rebuild is already running for this app.",
                "output": "A rebuild is already running for this app. Wait for it to finish.",
                "action_logs": action_logs,
            }
        raise HTTPException(500, f"Failed to rebuild Docker image: {e}") from e
    except Exception as e:
        raise HTTPException(500, f"Failed to rebuild Docker image: {e}") from e


@router.post("/{app_id}/deploy-zero-downtime")
async def deploy_zero_downtime(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    from database import AsyncSessionLocal
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    if not app.use_docker:
        raise HTTPException(400, "Zero-downtime deploy is only available for Docker apps")
    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Zero-downtime deploy requires a custom app domain or a configured base domain")

    pm._push_line(app_id, "[ZD] Zero-downtime deploy starting…")

    # ── Determine target nodes from currently running replicas ─────────────────
    # Use replica node_id instead of app.node_id (which is null for replica-model apps).
    # Keep one entry per *running replica* so that if node A has 3 replicas, we start
    # 3 new replacements on that same node (not just 1).
    running_res = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app_id,
            ApplicationReplica.status == "running",
        )
    )
    running_replicas = running_res.scalars().all()

    if running_replicas:
        # Build list of (node, old_replica) pairs — one entry per replica to replace
        unique_nids = list({r.node_id for r in running_replicas if r.node_id})
        nodes_res = await db.execute(select(Node).where(Node.id.in_(unique_nids)))
        node_map: dict[int, Node] = {n.id: n for n in nodes_res.scalars().all()}
        # target_slots: one entry per running replica we intend to replace
        target_slots: list[Node] = [
            node_map[r.node_id] for r in running_replicas if r.node_id and r.node_id in node_map
        ]
        # Deduplicate unique nodes for the build/refresh step
        target_nodes: list[Node] = list({n.id: n for n in target_slots}.values())
    else:
        # No running replicas yet — fall back to app's assigned node, create one replica
        fallback = await _get_app_node(app, db, local_node)
        target_nodes = [fallback]
        target_slots = [fallback]

    env_vars = decrypt_env(app.env_vars or "")
    source_revision = _refresh_app_source_revision(app)

    # ── Step 1: Refresh / build image on every target node ───────────────────
    new_img: str | None = None
    for tnode in target_nodes:
        if tnode.is_local:
            if not app.working_dir:
                raise HTTPException(400, "No working directory — deploy the app first")
            try:
                def _push(_aid, line):
                    pm._push_line(app_id, str(line))
                new_img = await asyncio.to_thread(
                    dm.build_image,
                    app_id, app.name, app.working_dir, _push,
                    app.app_type or "unknown", app.start_command or "", app.port or 8000,
                )
            except Exception as e:
                raise HTTPException(500, f"Failed to build image: {e}")
            app.docker_image = new_img
            if source_revision:
                app.image_revision = source_revision
        else:
            if tnode.status != "online":
                raise HTTPException(400, f"Node '{tnode.name}' is offline")
            pm._push_line(app_id, f"[ZD] Refreshing source on node '{tnode.name}'…")
            refresh_cmd = await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="refresh_source",
                payload={
                    "app_name": app.name,
                    "app_id": app_id,
                    "app_type": app.app_type or "unknown",
                    "start_command": app.start_command or "",
                    "internal_port": app.port or 8000,
                    "env_vars": env_vars,
                    "docker_options": _docker_runtime_options(app),
                    "source_revision": source_revision,
                },
            )
            refresh_done = await wait_for_node_command(db, refresh_cmd.id, timeout_seconds=300)
            if refresh_done.status != "done":
                raise HTTPException(500, f"Source refresh failed on node '{tnode.name}': {refresh_done.error_message}")
            pm._push_line(app_id, f"[ZD] Source refreshed on node '{tnode.name}'.")

    # ── Step 2: Create new replica rows and start containers on each slot ────
    # new_entries: list of (node, replica_id) — kept for rollback and nginx assembly
    new_entries: list[tuple[Node, int]] = []

    for tnode in target_slots:
        new_ext_port = await _assign_external_port(None, tnode.id, None, db)
        new_replica = ApplicationReplica(
            app_id=app_id,
            node_id=tnode.id,
            external_port=new_ext_port,
            status="starting",
        )
        db.add(new_replica)
        await db.flush()
        await db.commit()
        new_entries.append((tnode, new_replica.id))

        if tnode.is_local:
            pm._push_line(app_id, f"[ZD] Starting new local instance (id={new_replica.id}) on port {new_ext_port}…")
            try:
                cid = await _start_instance_local(app, new_replica, env_vars, app_id)
                new_replica.container_id = cid
                await db.commit()
            except Exception as e:
                await _zd_rollback(app_id, local_node.id, new_entries, db)
                raise HTTPException(500, f"Failed to start new local instance: {e}")
        else:
            pm._push_line(app_id, f"[ZD] Queuing start on node '{tnode.name}' for instance {new_replica.id}…")
            remote_payload = _remote_replica_command_payload(app, env_vars, new_ext_port)
            await queue_node_command(
                db, node_id=tnode.id, app_id=app_id,
                command_type="start_replica",
                payload={**remote_payload, "replica_id": new_replica.id},
            )
            await db.commit()

    # ── Step 3: Health-check every new replica ────────────────────────────────
    for tnode, new_rid in new_entries:
        if tnode.is_local:
            async with AsyncSessionLocal() as _poll_db:
                r = await _poll_db.get(ApplicationReplica, new_rid)
                check_port = r.external_port if r else None
            label = f"local port {check_port}"
        else:
            # Poll until the agent establishes the reverse tunnel (max 120 s)
            pm._push_line(app_id, f"[ZD] Waiting for tunnel from '{tnode.name}' instance {new_rid} (max 120s)…")
            deadline = asyncio.get_running_loop().time() + 120
            tunnel_port = None
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(3)
                async with AsyncSessionLocal() as _poll_db:
                    r = await _poll_db.execute(
                        select(ApplicationReplica).where(ApplicationReplica.id == new_rid)
                    )
                    fresh = r.scalar_one_or_none()
                if fresh is None:
                    break
                if fresh.status == "error":
                    await _zd_rollback(app_id, local_node.id, new_entries, db)
                    raise HTTPException(500, f"Instance {new_rid} on '{tnode.name}' failed: {fresh.last_error}")
                if fresh.tunnel_port:
                    tunnel_port = fresh.tunnel_port
                    break

            if not tunnel_port:
                await _zd_rollback(app_id, local_node.id, new_entries, db)
                raise HTTPException(502, f"Tunnel for instance {new_rid} on '{tnode.name}' did not connect within 120s — rolled back")

            check_port = tunnel_port
            label = f"tunnel port {check_port} (node '{tnode.name}')"

        pm._push_line(app_id, f"[ZD] Health checking instance {new_rid} on {label} (max 60s)…")
        deadline = asyncio.get_running_loop().time() + 60
        healthy = False
        while asyncio.get_running_loop().time() < deadline:
            if await asyncio.to_thread(_local_http_service_ready, check_port):
                healthy = True
                break
            await asyncio.sleep(2)

        if not healthy:
            await _zd_rollback(app_id, local_node.id, new_entries, db)
            raise HTTPException(502, f"Instance {new_rid} on {label} failed health check after 60s — rolled back")

        pm._push_line(app_id, f"[ZD] Instance {new_rid} healthy.")

    pm._push_line(app_id, "[ZD] All instances healthy. Switching nginx to new instances…")

    # ── Step 4: Mark new replicas running and build nginx backend list ────────
    new_backends: list[str] = []
    new_ids: set[int] = set()
    for tnode, new_rid in new_entries:
        async with AsyncSessionLocal() as _upd_db:
            r = await _upd_db.get(ApplicationReplica, new_rid)
            if r:
                r.status = "running"
                await _upd_db.commit()
                if tnode.is_local:
                    if r.external_port:
                        new_backends.append(f"127.0.0.1:{r.external_port}")
                else:
                    if r.tunnel_port:
                        new_backends.append(f"127.0.0.1:{r.tunnel_port}")
        new_ids.add(new_rid)

    app.status = "running"
    if new_img:
        app.docker_image = new_img
    else:
        app.docker_image = dm.image_name(app_id, app.name)
    if source_revision:
        app.image_revision = source_revision
    await db.flush()

    has_custom = bool(app.nginx_enabled and app.domain)
    ssl_cert = ssl_key = None
    if has_custom:
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
    cfg = nm.generate_config(
        app.name, app.domain if has_custom else None, new_backends,
        ssl_cert, ssl_key,
        app_id=app_id, mode="normal",
        extra_domains=json.loads(app.extra_domains or "[]") if has_custom else [],
        redirect_domains=json.loads(app.redirect_domains or "[]") if has_custom else [],
    )
    ok, msg = nm.write_nginx_config(app.name, cfg)
    if not ok:
        pm._push_line(app_id, f"[ZD] Warning: nginx config update failed: {msg}")
    else:
        pm._push_line(app_id, "[ZD] Nginx switched to new instances. Stopping old instances…")

    # ── Step 5: Stop all previously-running replicas ──────────────────────────
    old_res = await db.execute(
        select(ApplicationReplica).where(
            ApplicationReplica.app_id == app_id,
            ApplicationReplica.id.not_in(new_ids),
            ApplicationReplica.status == "running",
        )
    )
    for old_r in old_res.scalars().all():
        if old_r.node_id is None or old_r.node_id == local_node.id:
            await asyncio.to_thread(dm.stop_replica_container, app_id, old_r.id)
            await db.delete(old_r)
            pm._push_line(app_id, f"[ZD] Stopped old local instance {old_r.id}.")
        else:
            await queue_node_command(
                db, node_id=old_r.node_id, app_id=app_id,
                command_type="stop_replica",
                payload={"app_id": app_id, "replica_id": old_r.id, "app_name": app.name},
            )
            await db.delete(old_r)
            pm._push_line(app_id, f"[ZD] Queued stop for old remote instance {old_r.id}.")

    first_new_id = new_entries[0][1] if new_entries else None
    await log_audit(db, "app.zero_downtime_deploy", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()

    pm._push_line(app_id, f"[ZD] Zero-downtime deploy complete. New instance(s): {[nid for _, nid in new_entries]}.")
    return {"status": "ok", "image": app.docker_image, "instance_id": first_new_id}


async def _zd_rollback(app_id: int, local_node_id: int, new_entries: list[tuple["Node", int]], db: "AsyncSession") -> None:
    """Best-effort cleanup of new replicas created during a failed ZD deploy."""
    from database import AsyncSessionLocal
    for tnode, new_rid in new_entries:
        try:
            async with AsyncSessionLocal() as _rb_db:
                victim = await _rb_db.get(ApplicationReplica, new_rid)
                if victim:
                    if tnode.is_local:
                        await asyncio.to_thread(dm.stop_replica_container, app_id, new_rid)
                    else:
                        await queue_node_command(
                            _rb_db, node_id=tnode.id, app_id=app_id,
                            command_type="stop_replica",
                            payload={"app_id": app_id, "replica_id": new_rid, "app_name": victim.app_id},
                        )
                    await _rb_db.delete(victim)
                    await _rb_db.commit()
        except Exception as _e:
            log.warning("[ZD rollback] failed to clean up replica %d: %s", new_rid, _e)


def _sse_line(data: str) -> str:
    """Format a single SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/{app_id}/pull/stream")
async def git_pull_stream(app_id: int, payload: PullRequest | None = Body(default=None), _user: dict = Depends(_auth.require_permission("apps.create")), db: AsyncSession = Depends(get_db)):
    """Streaming SSE variant of git_pull. Each build log line is emitted as it happens."""
    app = await _get_or_404(app_id, db)
    target_commit = (payload.commit.strip() if payload and payload.commit else None)
    app_dir = pm.get_app_dir(app.name)

    if not os.path.exists(app_dir):
        async def _reclone_gen():
            yield _sse_line("[Git] App directory missing — attempting fresh clone…")
            try:
                await _deploy_app(app)
                yield _sse_line("[Git] Fresh clone completed.")
                yield f"event: result\ndata: {json.dumps({'message': 'Fresh clone completed', 'output': 'Fresh clone completed.'})}\n\n"
                yield "data: __DONE__\n\n"
            except Exception as exc:
                yield _sse_line(f"[Error] Re-clone failed: {exc}")
                yield "data: __FAILED__\n\n"
        return StreamingResponse(_reclone_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    queue: asyncio.Queue = asyncio.Queue()
    result_holder: dict = {}
    loop = asyncio.get_running_loop()

    def _q(line: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, str(line))

    async def _do_pull() -> None:
        try:
            github_token = _decrypt_github_token(app.github_token)
            if github_token:
                url = _build_clone_url(app.repo_url, github_token)
                subprocess.run(["git", "remote", "set-url", "origin", url], cwd=app_dir, capture_output=True)

            branch = _current_branch(app_dir)
            _q(f"[Git] Fetching from origin ({branch})…")
            await asyncio.to_thread(_fetch_origin, app_dir, branch)

            target = target_commit or f"origin/{branch}"
            _q(f"[Git] Resetting to {target}…")
            reset = await asyncio.to_thread(
                subprocess.run,
                ["git", "reset", "--hard", target],
                cwd=app_dir,
                capture_output=True,
                text=True,
            )
            if reset.returncode != 0 and not target_commit:
                reset = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "reset", "--hard", "@{u}"],
                    cwd=app_dir,
                    capture_output=True,
                    text=True,
                )
            if reset.returncode != 0:
                raise HTTPException(500, f"Git reset failed: {reset.stderr}")

            log_res = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "-1", "--format=%h - %s (%cr)"],
                cwd=app_dir,
                capture_output=True,
                text=True,
            )
            commit_info = log_res.stdout.strip() if log_res.returncode == 0 else "Unknown"
            _q(f"[Git] Updated to: {commit_info}")
            source_revision = _resolve_source_revision(app_dir)
            if source_revision:
                app.source_revision = source_revision

            if app.use_docker:
                was_running = pm.is_docker_app_running(app_id)
                _q("[Docker] Rebuilding image…")

                def _docker_push(aid, line):
                    _q(str(line))

                await asyncio.to_thread(
                    dm.build_image,
                    app_id, app.name, app_dir, _docker_push,
                    app.app_type or "unknown", app.start_command or "",
                    app.port or 8000,
                )
                app.docker_image = dm.image_name(app_id, app.name)
                if source_revision:
                    app.image_revision = source_revision
                await db.commit()

                if was_running:
                    _q("[Docker] Image rebuilt. Running container left untouched. Restart manually to use the new image.")
                else:
                    _q("[Docker] Image rebuilt. Start the app to apply changes.")

                result_holder["result"] = {
                    "message": f"Updated and rebuilt Docker image from {target_commit or branch}",
                    "commit": commit_info,
                    "source_revision": source_revision,
                    "output": (
                        "Image rebuilt. Restart manually to switch to the new image."
                        if was_running else
                        "Image rebuilt. Start the app to apply changes."
                    ),
                }
            else:
                app.image_revision = None
                await db.commit()
                result_holder["result"] = {
                    "message": f"Updated code to {target_commit or branch}",
                    "output": f"Latest commit: {commit_info}\n\nRestart the app to apply changes.",
                    "commit": commit_info,
                    "source_revision": source_revision,
                }
        except HTTPException as exc:
            result_holder["error"] = exc.detail
        except Exception as exc:
            result_holder["error"] = str(exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(_do_pull())

    async def _generate():
        while True:
            item = await queue.get()
            if item is None:
                if "error" in result_holder:
                    yield _sse_line(f"[Error] {result_holder['error']}")
                    yield "data: __FAILED__\n\n"
                else:
                    yield f"event: result\ndata: {json.dumps(result_holder.get('result', {}))}\n\n"
                    yield "data: __DONE__\n\n"
                break
            yield _sse_line(item)

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/{app_id}/rebuild/stream")
async def rebuild_docker_image_stream(app_id: int, _user: dict = Depends(_auth.require_permission("apps.create")), db: AsyncSession = Depends(get_db)):
    """Streaming SSE variant of rebuild_docker_image."""
    app = await _get_or_404(app_id, db)

    if not app.use_docker:
        raise HTTPException(400, "Rebuild is only available for Docker apps")
    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    queue: asyncio.Queue = asyncio.Queue()
    result_holder: dict = {}
    loop = asyncio.get_running_loop()
    was_running = pm.is_docker_app_running(app_id)

    def _q(line: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, str(line))

    async def _do_rebuild() -> None:
        try:
            _q("[Docker] Rebuilding image…")

            def _push(aid, line):
                _q(str(line))

            img = await asyncio.to_thread(
                dm.build_image,
                app_id, app.name, app.working_dir, _push,
                app.app_type or "unknown", app.start_command or "", app.port or 8000,
            )
            source_revision = _refresh_app_source_revision(app)
            app.status = "running" if was_running else "stopped"
            app.docker_image = img
            if source_revision:
                app.image_revision = source_revision
            await db.commit()

            if was_running:
                _q("[Docker] Image rebuilt. Running container left untouched. Restart manually to use the new image.")
                result_holder["result"] = {
                    "status": "running",
                    "message": "Image rebuilt. Running container was not restarted.",
                    "output": "Image rebuilt. Restart manually to switch to the new image.",
                    "source_revision": source_revision,
                }
            else:
                _q("[Docker] Image rebuilt. Start the app to run it.")
                result_holder["result"] = {
                    "status": "rebuilt",
                    "message": "Image rebuilt. Start the app to run it.",
                    "output": "Image rebuilt. Start the app to run it.",
                    "source_revision": source_revision,
                }
        except Exception as exc:
            result_holder["error"] = str(exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(_do_rebuild())

    async def _generate():
        while True:
            item = await queue.get()
            if item is None:
                if "error" in result_holder:
                    yield _sse_line(f"[Error] {result_holder['error']}")
                    yield "data: __FAILED__\n\n"
                else:
                    yield f"event: result\ndata: {json.dumps(result_holder.get('result', {}))}\n\n"
                    yield "data: __DONE__\n\n"
                break
            yield _sse_line(item)

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)




@router.get("/{app_id}/nginx-config")
async def get_nginx_config(app_id: int, db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)

    safe = nm._safe_name(app.name)
    config_path = os.path.join(nm.NGINX_SITES_DIR, safe)
    if not os.path.exists(config_path):
        generated = None
        if app.domain:
            backends = await _get_nginx_backends(app, db)
            generated = nm.generate_config(
                app.name, app.domain, backends,
                app.ssl_cert_path, app.ssl_key_path,
                app_id=app.id, mode=_get_nginx_mode(app),
                extra_domains=json.loads(app.extra_domains or "[]"),
                redirect_domains=json.loads(app.redirect_domains or "[]"),
            )
        return {"exists": False, "path": config_path, "content": generated, "active": False}
    with open(config_path) as f:
        content = f.read()
    enabled_path = os.path.join(nm.NGINX_ENABLED_DIR, safe)
    return {"exists": True, "path": config_path, "content": content, "active": os.path.exists(enabled_path)}


@router.put("/{app_id}/nginx-config")
async def save_nginx_config(app_id: int, payload: dict, _user: dict = Depends(_auth.require_permission("apps.manage")), db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)

    content = payload.get("content", "")
    ok, msg = nm.write_nginx_config(app.name, content)
    if ok:
        app.nginx_enabled = True
        await db.commit()
    return {"ok": ok, "message": msg}


@router.get("/{app_id}/stats")
async def get_stats(app_id: int, db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        if node.status != "online":
            return {
                "status": "stopped",
                "remote": True,
                "node": {
                    "id": node.id,
                    "name": node.name,
                    "status": node.status,
                    "is_local": bool(node.is_local),
                },
            }
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="get_stats",
            payload={"app_id": app.id, "app_name": app.name},
            allow_existing_inflight=True,
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            return {
                "status": "stopped",
                "remote": True,
                "error": done.error_message,
                "node": {
                    "id": node.id,
                    "name": node.name,
                    "status": node.status,
                    "is_local": bool(node.is_local),
                },
            }
        data = json.loads(done.result or "{}") if done.result else {}
        data["remote"] = True
        data["node"] = {
            "id": node.id,
            "name": node.name,
            "status": node.status,
            "is_local": bool(node.is_local),
        }
        return data

    replica_stats = await _get_local_replica_stats(app, db, local_node)
    if replica_stats.get("status") == "running":
        return replica_stats

    if app.use_docker:
        if pm.is_docker_app_running(app_id):
            stats = await asyncio.to_thread(pm.get_docker_stats, app_id)
            return {"status": "running", "docker": True, **stats}
        return {"status": "stopped", "docker": True}
    if app.pid and pm.is_process_running(app.pid, app.id):
        stats = await asyncio.to_thread(pm.get_process_stats, app.pid)
        return {"status": "running", **stats}
    return {"status": "stopped"}


@router.get("/{app_id}/logs/tail")
async def get_logs_tail(app_id: int, limit: int = Query(200, ge=1, le=2000), db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status != "online":
            return {"lines": [], "remote": True, "error": "Node is offline"}
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="get_logs_tail",
            payload={"app_id": app.id, "app_name": app.name, "limit": limit},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            return {"lines": [], "remote": True, "error": done.error_message}
        result_payload = json.loads(done.result or "{}") if done.result else {}
        return {"lines": result_payload.get("lines", []) or [], "remote": True}

    replica_lines = await _get_local_replica_logs(app, db, local_node, limit)
    if replica_lines:
        return {"lines": replica_lines, "remote": False}

    if app.use_docker:
        lines = pm.get_recent_docker_logs(app_id, limit)
    else:
        lines = pm.get_recent_logs(app_id, app.name)[-limit:]
    return {"lines": lines, "remote": False}


async def _get_or_404(app_id: int, db: AsyncSession) -> Application:
    result = await db.execute(select(Application).where(Application.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "App not found")
    return app


def _app_to_dict(
    app: Application,
    include_sensitive: bool = True,
    replicas: Optional[list] = None,
    include_page_configs: bool = True,
) -> dict:
    import re as _re
    downtime_page = json.loads(app.downtime_page or "{}") if include_page_configs else None
    update_page = json.loads(app.update_page or "{}") if include_page_configs else None
    restart_page = json.loads(app.restart_page or "{}") if include_page_configs else None
    starting_page = json.loads(app.starting_page or "{}") if include_page_configs else None
    # Compute the app's accessible URL: custom domain first, then auto-subdomain
    _app_url: Optional[str] = None
    if not app.no_web:
        if app.domain:
            _proto = "https" if (app.ssl_cert_path and app.ssl_key_path) else "http"
            _app_url = f"{_proto}://{app.domain}"
        else:
            _base = _syscfg.get_base_domain_cached()
            if _base:
                _slug = _re.sub(r"[^a-z0-9]+", "-", (app.name or "").lower()).strip("-")
                if _slug:
                    _app_url = f"https://{_slug}.{_base}"
    return {
        "id": app.id,
        "name": app.name,
        "repo_url": app.repo_url,
        "domain": app.domain,
        "extra_domains": json.loads(app.extra_domains or "[]"),
        "redirect_domains": json.loads(app.redirect_domains or "[]"),
        "no_web": bool(app.no_web),
        "app_type": app.app_type,
        "start_command": app.start_command,
        "port": app.port,
        "status": app.status,
        "working_dir": app.working_dir,
        "last_error": app.last_error,
        "env_vars": decrypt_env(app.env_vars or "") if include_sensitive else {},
        "nginx_enabled": app.nginx_enabled,
        "auto_start":     app.auto_start,
        "restart_policy": app.restart_policy or "no",
        "use_docker":     True,
        "docker_image":   app.docker_image,
        "source_revision": app.source_revision,
        "image_revision": app.image_revision,
        "docker_cpu_limit": app.docker_cpu_limit,
        "docker_memory_limit_mb": app.docker_memory_limit_mb,
        "docker_read_only_root": bool(app.docker_read_only_root),
        "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "maintenance_mode": app.maintenance_mode or False,
        "update_mode":      app.update_mode or False,
        "downtime_page":    downtime_page,
        "update_page":      update_page,
        "restart_page":     restart_page,
        "starting_page":    starting_page,
        "ssl_cert_path": app.ssl_cert_path,
        "ssl_key_path": app.ssl_key_path,
        "github_token": "***" if app.github_token else None,
        "created_at": app.created_at.isoformat() if app.created_at else None,
        "updated_at": app.updated_at.isoformat() if app.updated_at else None,
        "app_url": _app_url,
        "replicas": replicas if replicas is not None else [],
        "replica_count": len(replicas) if replicas is not None else 0,
    }


def _replica_to_dict(replica: ApplicationReplica, node: Optional[Node] = None) -> dict:
    return {
        "id": replica.id,
        "app_id": replica.app_id,
        "node_id": replica.node_id,
        "node_name": node.name if node else None,
        "node_is_local": bool(node.is_local) if node else False,
        "external_port": replica.external_port,
        "tunnel_port": replica.tunnel_port,
        "tunnel_connected": replica.tunnel_port is not None,
        "container_id": replica.container_id,
        "status": replica.status,
        "last_error": replica.last_error,
        "docker_cpu_limit": replica.docker_cpu_limit,
        "docker_memory_limit_mb": replica.docker_memory_limit_mb,
        "docker_read_only_root": bool(replica.docker_read_only_root),
        "docker_tmpfs_enabled": bool(replica.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": replica.docker_tmpfs_size_mb,
        "created_at": replica.created_at.isoformat() if replica.created_at else None,
        "updated_at": replica.updated_at.isoformat() if replica.updated_at else None,
    }


async def _get_nginx_backends(app: Application, db: AsyncSession, local_node: "Node" = None) -> "list[str]":
    """Return nginx backend addresses for an app as a list of 'host:port' strings.

    Local replicas use 127.0.0.1:{external_port}.
    Remote replicas use 127.0.0.1:{tunnel_port} (reverse WebSocket tunnel on main node).
    Replicas that are running but have no address yet are omitted.
    Returns an empty list when no running replicas exist.
    """
    result = await db.execute(
        select(ApplicationReplica, Node)
        .join(Node, ApplicationReplica.node_id == Node.id, isouter=True)
        .where(
            ApplicationReplica.app_id == app.id,
            ApplicationReplica.status == "running",
        )
    )
    backends: list[str] = []
    for replica, r_node in result.all():
        is_remote = r_node is not None and not r_node.is_local
        if is_remote:
            # Remote replica: traffic reaches it via the reverse tunnel on the main node
            if replica.tunnel_port:
                backends.append(f"127.0.0.1:{replica.tunnel_port}")
        else:
            # Local replica (node_id is None, or node is local): direct port on this machine
            if replica.external_port:
                backends.append(f"127.0.0.1:{replica.external_port}")
    return backends


async def _get_app_node(app: Application, db: AsyncSession, local_node: Optional[Node] = None) -> Node:
    """Return the node that should handle single-node operations (logs, git, rebuild).

    Prefers the node of the first running replica, then any replica, then local.
    """
    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app.id)
        .order_by(ApplicationReplica.status.desc())  # running > stopped
        .limit(5)
    )
    replicas = rep_result.scalars().all()
    running = next((r for r in replicas if r.status in ("running", "starting") and r.node_id), None)
    candidate = running or next((r for r in replicas if r.node_id), None)
    if candidate and candidate.node_id:
        node_result = await db.execute(select(Node).where(Node.id == candidate.node_id))
        node = node_result.scalar_one_or_none()
        if node:
            return node
    if local_node is not None:
        return local_node
    return await ensure_local_node(db)


async def _load_node_map(db: AsyncSession) -> dict[int, Node]:
    result = await db.execute(select(Node))
    nodes = result.scalars().all()
    return {n.id: n for n in nodes}


# ── Maintenance page endpoints ─────────────────────────────────────────────

@router.get("/{app_id}/maintenance-pages")
async def get_maintenance_pages(app_id: int, db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    return {
        "maintenance_mode": app.maintenance_mode or False,
        "update_mode":      app.update_mode or False,
        "downtime_page":    json.loads(app.downtime_page  or "{}"),
        "update_page":      json.loads(app.update_page    or "{}"),
        "restart_page":     json.loads(app.restart_page   or "{}"),
        "starting_page":    json.loads(app.starting_page  or "{}"),
    }


@router.put("/{app_id}/maintenance-pages")
async def save_maintenance_pages(
    app_id: int,
    req: MaintenanceSettings,
    _user: dict = Depends(_auth.require_permission("apps.manage")),
    db: AsyncSession = Depends(get_db),
):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    app.downtime_page = json.dumps(req.downtime_page.model_dump())
    app.update_page   = json.dumps(req.update_page.model_dump())
    app.restart_page  = json.dumps(req.restart_page.model_dump())
    app.starting_page = json.dumps(req.starting_page.model_dump())
    downtime_html = nm.generate_maintenance_html(
        req.downtime_page.title   or "Down for Maintenance",
        req.downtime_page.message or "We'll be back shortly.",
        req.downtime_page.color   or "#f85149",
        req.downtime_page.status_url,
        req.downtime_page.custom_html,
        "downtime",
        logo_data=req.downtime_page.logo_data,
    )
    update_html = nm.generate_maintenance_html(
        req.update_page.title   or "Updating\u2026",
        req.update_page.message or "We\u2019re deploying a new version. Check back soon.",
        req.update_page.color   or "#f0883e",
        req.update_page.status_url,
        req.update_page.custom_html,
        "update",
        logo_data=req.update_page.logo_data,
    )
    restart_html = nm.generate_maintenance_html(
        req.restart_page.title   or "Restarting\u2026",
        req.restart_page.message or "The server is restarting. This only takes a moment.",
        req.restart_page.color   or "#388bfd",
        req.restart_page.status_url,
        req.restart_page.custom_html,
        "restart",
        logo_data=req.restart_page.logo_data,
    )
    starting_html = nm.generate_maintenance_html(
        req.starting_page.title   or "Starting\u2026",
        req.starting_page.message or "The service is starting up. This only takes a moment.",
        req.starting_page.color   or "#388bfd",
        req.starting_page.status_url,
        req.starting_page.custom_html,
        "starting",
        logo_data=req.starting_page.logo_data,
    )
    ok, msg = nm.write_maintenance_files(app_id, downtime_html, update_html, restart_html, starting_html)
    if not ok:
        await db.commit()
        return {"ok": False, "message": msg}

    # Regenerate and reload nginx if configured, so changes take effect immediately
    if _has_public_nginx_domain(app):
        mode = _get_nginx_mode(app)
        await _write_app_nginx_config(app, db, local_node, mode=mode)
        nginx_ok, nginx_msg = True, "ok"
        if not nginx_ok:
            await db.commit()
            return {"ok": False, "message": f"Files saved but nginx reload failed: {nginx_msg}"}

    await db.commit()
    return {"ok": True, "message": "Saved"}


@router.post("/{app_id}/maintenance-mode/toggle")
async def toggle_maintenance_mode(app_id: int, _user: dict = Depends(_auth.require_permission("apps.manage")), db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    previous_maintenance_mode = bool(app.maintenance_mode)
    previous_update_mode = bool(app.update_mode)
    
    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Configure a custom app domain or base domain to use maintenance mode")

    app.maintenance_mode = not (app.maintenance_mode or False)
    if app.maintenance_mode:
        app.update_mode = False  # mutex: only one mode at a time

    mode = _get_nginx_mode(app)
    log.info("[toggle-maintenance] app_id=%d new_mode=%r nginx_mode=%r domain=%r",
             app_id, app.maintenance_mode, mode, app.domain)

    maint_ok, maint_msg = _ensure_maintenance_files(app, app_id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
    await _write_app_nginx_config(app, db, local_node, mode=mode)
    ok, msg = True, "ok"
    log.info("[toggle-maintenance] write_nginx_config ok=%s msg=%r", ok, msg)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    await db.commit()
    return _app_to_dict(app)


@router.post("/{app_id}/update-mode/toggle")
async def toggle_update_mode(app_id: int, _user: dict = Depends(_auth.require_permission("apps.manage")), db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    previous_maintenance_mode = bool(app.maintenance_mode)
    previous_update_mode = bool(app.update_mode)

    if not _has_public_nginx_domain(app):
        raise HTTPException(400, "Configure a custom app domain or base domain to use update mode")

    app.update_mode = not (app.update_mode or False)
    if app.update_mode:
        app.maintenance_mode = False  # mutex: only one mode at a time

    mode = _get_nginx_mode(app)
    log.info("[toggle-update] app_id=%d new_mode=%r nginx_mode=%r domain=%r",
             app_id, app.update_mode, mode, app.domain)

    maint_ok, maint_msg = _ensure_maintenance_files(app, app_id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
    await _write_app_nginx_config(app, db, local_node, mode=mode)
    ok, msg = True, "ok"
    log.info("[toggle-update] write_nginx_config ok=%s msg=%r", ok, msg)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    await db.commit()
    return _app_to_dict(app)


@router.get("/{app_id}/maintenance-pages/preview/{page_type}")
async def preview_maintenance_page(
    app_id: int,
    page_type: str,
    db: AsyncSession = Depends(get_db),
):
    """Return the rendered HTML for a maintenance page — opens directly in the browser."""
    from fastapi.responses import HTMLResponse

    if page_type not in ("downtime", "update", "restart", "starting"):
        raise HTTPException(400, "page_type must be 'downtime', 'update', 'restart', or 'starting'")

    app = await _get_or_404(app_id, db)
    if page_type == "downtime":
        raw = app.downtime_page
    elif page_type == "update":
        raw = app.update_page
    elif page_type == "restart":
        raw = app.restart_page
    else:
        raw = app.starting_page
    cfg = json.loads(raw or "{}")

    if page_type == "downtime":
        html = nm.generate_maintenance_html(
            cfg.get("title")      or "Down for Maintenance",
            cfg.get("message")    or "We'll be back shortly.",
            cfg.get("color")      or "#f85149",
            cfg.get("status_url"),
            cfg.get("custom_html"),
            "downtime",
            logo_data=cfg.get("logo_data"),
        )
    elif page_type == "restart":
        html = nm.generate_maintenance_html(
            cfg.get("title")      or "Restarting\u2026",
            cfg.get("message")    or "The server is restarting. This only takes a moment.",
            cfg.get("color")      or "#388bfd",
            cfg.get("status_url"),
            cfg.get("custom_html"),
            "restart",
            logo_data=cfg.get("logo_data"),
        )
    elif page_type == "starting":
        html = nm.generate_maintenance_html(
            cfg.get("title")      or "Starting\u2026",
            cfg.get("message")    or "The service is starting up. This only takes a moment.",
            cfg.get("color")      or "#388bfd",
            cfg.get("status_url"),
            cfg.get("custom_html"),
            "starting",
            logo_data=cfg.get("logo_data"),
        )
    else:
        html = nm.generate_maintenance_html(
            cfg.get("title")      or "Updating\u2026",
            cfg.get("message")    or "We\u2019re deploying a new version. Check back soon.",
            cfg.get("color")      or "#f0883e",
            cfg.get("status_url"),
            cfg.get("custom_html"),
            "update",
            logo_data=cfg.get("logo_data"),
        )
    return HTMLResponse(content=html)


@router.post("/{app_id}/nginx-refresh")
async def nginx_refresh(app_id: int, db: AsyncSession = Depends(get_db), _actor: str = Depends(_auth.get_current_actor)):
    """Regenerate the nginx config from the current set of running replicas."""
    app = await _get_or_404(app_id, db)
    if not _has_public_nginx_domain(app):
        return {"ok": False, "message": "Nginx not configured for this app"}
    local_node = await ensure_local_node(db)
    await _write_app_nginx_config(app, db, local_node, mode=_get_nginx_mode(app))
    backends = await _get_nginx_backends(app, db, local_node)
    ok, msg = True, "ok"
    return {"ok": ok, "message": msg, "backends": backends}


@router.get("/{app_id}/nginx-debug")
async def nginx_debug(app_id: int, db: AsyncSession = Depends(get_db)):
    """Return a full diagnostic snapshot for nginx + maintenance config of this app."""
    import subprocess as sp
    app = await _get_or_404(app_id, db)

    safe_name = nm._safe_name(app.name)
    config_path   = f"{nm.NGINX_SITES_DIR}/{safe_name}"
    enabled_path  = f"{nm.NGINX_ENABLED_DIR}/{safe_name}"
    maint_dir     = f"{nm.MAINTENANCE_DIR}/{app_id}"

    def _read_file(path: str) -> str:
        r = sp.run(["sudo", "cat", path], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        return f"ERROR ({r.returncode}): {r.stderr.strip()}"

    def _ls(path: str) -> list:
        r = sp.run(["sudo", "ls", "-la", path], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip().splitlines()
        return [f"ERROR: {r.stderr.strip()}"]

    nginx_test  = sp.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
    nginx_status = sp.run(["sudo", "systemctl", "is-active", "nginx"], capture_output=True, text=True)

    return {
        "app": {
            "id":               app.id,
            "name":             app.name,
            "domain":           app.domain,
            "port":             app.port,
            "nginx_enabled":    app.nginx_enabled,
            "maintenance_mode": app.maintenance_mode,
            "update_mode":      app.update_mode,
            "computed_mode":    _get_nginx_mode(app),
        },
        "nginx": {
            "status":           nginx_status.stdout.strip(),
            "config_test":      nginx_test.stderr.strip() or nginx_test.stdout.strip(),
            "config_test_ok":   nginx_test.returncode == 0,
        },
        "files": {
            "sites_available_exists": sp.run(["sudo", "test", "-f", config_path], capture_output=True).returncode == 0,
            "sites_enabled_exists":   sp.run(["sudo", "test", "-L", enabled_path], capture_output=True).returncode == 0,
            "maintenance_dir_ls":     _ls(maint_dir),
            "nginx_config_content":   _read_file(config_path),
        },
        "conflicts": {
            "description": "Other enabled nginx configs that also define this domain (should be empty)",
            "files": [
                line for line in
                sp.run(
                    ["sudo", "grep", "-rl", app.domain or "", nm.NGINX_ENABLED_DIR],
                    capture_output=True, text=True,
                ).stdout.strip().splitlines()
                if line and not line.endswith("/" + safe_name)
            ] if app.domain else [],
        },
        "generated_config": nm.generate_config(
            app.name, app.domain or "(no domain)", await _get_nginx_backends(app, db),
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        ),
    }
