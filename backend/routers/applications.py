import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Query, Body, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
import datetime as _dt
from models import Application, ApplicationReplica, Node, StatsHistory
import process_manager as pm
import nginx_manager as nm
import token_vault
import docker_manager as dm
from routers.nodes import ensure_local_node, queue_node_command, wait_for_node_command
from env_crypto import encrypt_env, decrypt_env, encrypt_text, decrypt_text
from audit import log_audit
import auth as _auth

router = APIRouter(prefix="/api/apps", tags=["applications"])
log = logging.getLogger("pdm.apps")

RESTART_READY_TIMEOUT_SECONDS = 180
RESTART_READY_POLL_SECONDS = 1


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
            Application.node_id == node_id,
            Application.external_port.isnot(None),
            Application.id != exclude_app_id if exclude_app_id else True,
        )
    )
    used: set[int] = {row[0] for row in result.all()}

    replica_result = await db.execute(
        select(ApplicationReplica.external_port).where(
            ApplicationReplica.node_id == node_id,
            ApplicationReplica.external_port.isnot(None),
        )
    )
    used |= {row[0] for row in replica_result.all()}

    if requested:
        if requested in used:
            raise HTTPException(400, f"External port {requested} is already used by another app on this node")
        return requested

    return await asyncio.to_thread(dm.pick_free_external_port, used)


def _get_nginx_mode(app: Application) -> str:
    if app.update_mode:
        return "update"
    if app.maintenance_mode:
        return "maintenance"
    return "normal"


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


def _remote_replica_command_payload(app: Application, env_vars: dict, external_port: int) -> dict:
    return {
        "app_id": app.id,
        "app_name": app.name,
        "repo_url": app.repo_url,
        "github_token": _decrypt_github_token(app.github_token),
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
    log.info("[ensure-files] app_id=%d downtime_page=%r update_page=%r restart_page=%r starting_page=%r",
             app_id, app.downtime_page, app.update_page, app.restart_page, app.starting_page)
    downtime_cfg = json.loads(app.downtime_page  or "{}")
    update_cfg   = json.loads(app.update_page    or "{}")
    restart_cfg  = json.loads(app.restart_page   or "{}")
    starting_cfg = json.loads(app.starting_page  or "{}")

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


async def _wait_for_restart_ready(app_id: int, pid: Optional[int], port: Optional[int], use_docker: bool = False) -> tuple[bool, str]:
    deadline = asyncio.get_running_loop().time() + RESTART_READY_TIMEOUT_SECONDS

    while asyncio.get_running_loop().time() < deadline:
        if use_docker:
            if not pm.is_docker_app_running(app_id):
                return False, "container exited before becoming ready"

            if port:
                ready = await asyncio.to_thread(_local_http_service_ready, port)
                if ready:
                    return True, f"host port {port} responds to HTTP requests"
            else:
                return True, "container is running"
        else:
            if pid and not pm.is_process_running(pid, app_id):
                return False, "process exited before becoming ready"

            if port:
                listening_pid = await asyncio.to_thread(pm.find_process_by_port, port)
                if listening_pid:
                    return True, f"port {port} is accepting connections"
            elif pid and pm.is_process_running(pid, app_id):
                return True, "process is running"

        await asyncio.sleep(RESTART_READY_POLL_SECONDS)

    if use_docker:
        if port:
            return False, f"timed out waiting for host port {port}"
        return False, "timed out waiting for container to start"
    if port:
        return False, f"timed out waiting for port {port}"
    return False, "timed out waiting for process readiness"


async def _wait_for_host_port(port: Optional[int], timeout_seconds: int = 10) -> bool:
    if not port:
        return True

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(_local_http_service_ready, port):
            return True
        await asyncio.sleep(0.5)
    return False


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


async def _restore_nginx_after_restart(
    app_id: int,
    app_name: str,
    domain: str,
    port: int,
    ssl_cert_path: Optional[str],
    ssl_key_path: Optional[str],
    pid: Optional[int],
    started_at: float,
    extra_domains: list = None,
    redirect_domains: list = None,
    use_docker: bool = False,
) -> None:
    ready, reason = await _wait_for_restart_ready(app_id, pid, port, use_docker=use_docker)
    elapsed = max(asyncio.get_running_loop().time() - started_at, 0)

    normal_cfg = nm.generate_config(
        app_name, domain, port,
        ssl_cert_path, ssl_key_path,
        app_id=app_id, mode="normal",
        extra_domains=extra_domains,
        redirect_domains=redirect_domains,
    )
    ok, msg = nm.write_nginx_config(app_name, normal_cfg)
    log.info(
        "[restart-restore] app_id=%d ready=%s elapsed=%.1fs reason=%r nginx_ok=%s msg=%r",
        app_id, ready, elapsed, reason, ok, msg,
    )

    if ok:
        if ready:
            pm._push_line(app_id, f"Restart page cleared after {elapsed:.1f}s ({reason}).")
        else:
            pm._push_line(app_id, f"Restart page timed out after {elapsed:.1f}s; switched back to normal proxy ({reason}).")
    else:
        pm._push_line(app_id, f"Failed to restore nginx after restart: {msg}")


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


def _recent_git_commits(app_dir: str, limit: int = 20) -> list[dict]:
    fmt = "%H%x1f%h%x1f%s%x1f%cr%x1f%an"
    res = subprocess.run(
        ["git", "log", f"-n{limit}", f"--format={fmt}", "--decorate"],
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


@router.get("/{app_id}/commits")
async def list_git_commits(app_id: int, limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="list_git_commits",
            payload={"app_id": app.id, "app_name": app.name, "limit": limit},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=30)
        if done.status != "done":
            raise HTTPException(500, f"Failed to load commits from node '{node.name}': {done.error_message}")
        return json.loads(done.result or "{}")

    app_dir = _git_app_dir_or_404(app.name)
    branch = _current_branch(app_dir)
    _fetch_origin(app_dir, branch)
    return {"branch": branch, "commits": _recent_git_commits(app_dir, limit)}


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
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if node.status != "online":
            raise HTTPException(503, "Node is offline")
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="discover_app_certs",
            payload={"app_id": app.id, "app_name": app.name},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            raise HTTPException(500, f"Remote cert scan failed: {done.error_message}")
        return json.loads(done.result or "{}")

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
async def upload_app_cert(app_id: int, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Upload a cert/key file into the app's certs subfolder and return its path."""
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        import base64
        contents = await file.read()
        content_b64 = base64.b64encode(contents).decode()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="upload_cert",
            payload={"app_id": app.id, "app_name": app.name, "filename": file.filename, "content_b64": content_b64},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=40)
        if done.status != "done":
            raise HTTPException(500, f"Remote upload failed: {done.error_message}")
        return json.loads(done.result or "{}")

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


@router.get("/system/service-file")
async def get_service_file():
    """Return a systemd unit file for auto-starting Cloudbase on boot."""
    import getpass
    script_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "start.sh"))
    user = getpass.getuser()
    content = f"""[Unit]
Description=Cloudbase
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={os.path.dirname(os.path.dirname(script_dir))}
ExecStart=/bin/bash {script_dir} run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    return {
        "content": content,
        "path": "/etc/systemd/system/cloudbase.service",
    }


async def _sync_process_status(app, db) -> None:
    """Reconcile DB status with actual OS state. Uses port recovery as fallback."""
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        return

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
    local_node = await ensure_local_node(db)
    result = await db.execute(select(Application))
    apps = result.scalars().all()
    node_map = await _load_node_map(db)

    # Check all process statuses in parallel (each check runs blocking psutil calls in threads)
    async def _check(app):
        node = node_map.get(app.node_id) or local_node
        if not node.is_local:
            return app.id, app.status, app.pid

        if app.use_docker:
            alive = await asyncio.to_thread(pm.is_docker_app_running, app.id)
            return app.id, ("running" if alive else "stopped"), None
        if not app.pid:
            return app.id, app.status, app.pid
        alive = await asyncio.to_thread(pm.is_process_running, app.pid, app.id)
        if alive:
            return app.id, "running", app.pid
        if app.port:
            recovered = await asyncio.to_thread(pm.find_process_by_port, app.port)
            if recovered:
                return app.id, "running", recovered
        return app.id, "stopped", None

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

    # Load replica counts in one query
    replica_counts_result = await db.execute(
        select(ApplicationReplica.app_id, func.count(ApplicationReplica.id).label("cnt"))
        .group_by(ApplicationReplica.app_id)
    )
    replica_count_map = {row[0]: row[1] for row in replica_counts_result.all()}

    include_sensitive = _request_is_admin(request)
    result_list = []
    for a in apps:
        count = replica_count_map.get(a.id, 0)
        # Pass a placeholder list of the right length so replica_count is populated
        replica_stubs = [{}] * count
        result_list.append(_app_to_dict(a, node_map.get(a.node_id) or local_node, include_sensitive=include_sensitive, replicas=replica_stubs))
    return result_list


@router.post("")
async def deploy_app(req: DeployRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    existing = await db.execute(select(Application).where(Application.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"App '{req.name}' already exists")

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

    # Auto-assign external port (conflict check is inside _assign_external_port)
    node_id_for_port = target_node.id if target_node else local_node.id
    external_port = await _assign_external_port(req.external_port, node_id_for_port, None, db)

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
        use_docker=True,
        docker_cpu_limit=req.docker_cpu_limit,
        docker_memory_limit_mb=req.docker_memory_limit_mb,
        docker_read_only_root=bool(req.docker_read_only_root) if req.docker_read_only_root is not None else False,
        docker_tmpfs_enabled=bool(req.docker_tmpfs_enabled) if req.docker_tmpfs_enabled is not None else False,
        docker_tmpfs_size_mb=req.docker_tmpfs_size_mb,
        status="deploying",
        node_id=target_node.id,
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    if not target_node.is_local:
        await queue_node_command(
            db,
            node_id=target_node.id,
            app_id=app.id,
            command_type="deploy_app",
            payload={
                "name": req.name,
                "repo_url": req.repo_url,
                "github_token": _resolve_token(req.github_token, req.github_token_id),
                "domain": req.domain,
                "extra_domains": req.extra_domains or [],
                "redirect_domains": req.redirect_domains or [],
                "ssl_cert_path": req.ssl_cert_path,
                "ssl_key_path": req.ssl_key_path,
                "start_command": req.start_command,
                "port": req.port,
                "external_port": external_port,
                "docker_cpu_limit": req.docker_cpu_limit,
                "docker_memory_limit_mb": req.docker_memory_limit_mb,
                "docker_read_only_root": req.docker_read_only_root,
                "docker_tmpfs_enabled": req.docker_tmpfs_enabled,
                "docker_tmpfs_size_mb": req.docker_tmpfs_size_mb,
                "env_vars": req.env_vars or {},
                "auto_start": req.auto_start,
                "restart_policy": req.restart_policy,
                "use_docker": True,
            },
        )
        await db.refresh(app)
        return _app_to_dict(app, target_node)

    try:
        await _deploy_app(app)
        app.status = "stopped"

        if app.domain and _nginx_proxy_port(app):
            maint_ok, maint_msg = _ensure_maintenance_files(app, app.id)
            if not maint_ok:
                raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
            ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
            config = nm.generate_config(
                app.name, app.domain, _nginx_proxy_port(app),
                ssl_cert, ssl_key,
                app_id=app.id, mode=_get_nginx_mode(app),
                extra_domains=json.loads(app.extra_domains or "[]"),
                redirect_domains=json.loads(app.redirect_domains or "[]"),
            )
            ok, msg = nm.write_nginx_config(app.name, config)
            app.nginx_enabled = ok
            if not ok:
                raise HTTPException(500, f"Nginx config failed: {msg}")

        await log_audit(db, "app.deploy", actor=actor, app_id=app.id, detail={"name": app.name})
        await db.commit()
        await db.refresh(app)
        return _app_to_dict(app, target_node)
    except Exception as e:
        app.status = "error"
        await db.commit()
        raise HTTPException(500, str(e))


@router.get("/{app_id}")
async def get_app(app_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    await _sync_process_status(app, db)
    node = await _get_app_node(app, db, local_node)
    node_map = await _load_node_map(db)
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    replicas = replica_result.scalars().all()
    replicas_dicts = [_replica_to_dict(r, node_map.get(r.node_id)) for r in replicas]
    return _app_to_dict(app, node, include_sensitive=_request_is_admin(request), replicas=replicas_dicts)


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
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    _validate_docker_runtime_settings(req.docker_cpu_limit, req.docker_memory_limit_mb, req.docker_tmpfs_size_mb)

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
        app.start_command = req.start_command
        app.app_type = pm.detect_app_type_from_command(req.start_command)
    if req.port is not None:
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

    if not node.is_local:
        # If domain and port are provided, we assume the user wants Nginx enabled.
        # The node will attempt to configure it and return the result.
        if app.domain and _nginx_proxy_port(app):
            app.nginx_enabled = True

        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="update_app",
            payload={
                "app_id": app.id,
                "app_name": app.name,
                "domain": req.domain,
                "extra_domains": req.extra_domains,
                "redirect_domains": req.redirect_domains,
                "ssl_cert_path": req.ssl_cert_path,
                "ssl_key_path": req.ssl_key_path,
                "start_command": req.start_command,
                "port": req.port,
                "external_port": req.external_port,
                "docker_cpu_limit": req.docker_cpu_limit,
                "docker_memory_limit_mb": req.docker_memory_limit_mb,
                "docker_read_only_root": req.docker_read_only_root,
                "docker_tmpfs_enabled": req.docker_tmpfs_enabled,
                "docker_tmpfs_size_mb": req.docker_tmpfs_size_mb,
                "env_vars": req.env_vars,
                "auto_start": req.auto_start,
                "restart_policy": req.restart_policy,
            },
        )
        await db.refresh(app)

        # When a node is offline/unknown, keep the update queued and apply it when it reconnects.
        if node.status != "online":
            queued = _app_to_dict(app, node)
            queued["queued"] = True
            queued["pending_sync"] = True
            queued["command_id"] = cmd.id
            queued["message"] = f"Node '{node.name}' is offline. Settings saved and queued for sync."
            return queued

        try:
            done = await wait_for_node_command(db, cmd.id, timeout_seconds=30)
        except HTTPException as exc:
            if exc.status_code == 504:
                queued = _app_to_dict(app, node)
                queued["queued"] = True
                queued["pending_sync"] = True
                queued["command_id"] = cmd.id
                queued["message"] = f"Node '{node.name}' did not respond in time. Update remains queued."
                return queued
            raise

        await db.refresh(app)
        if done.status != "done":
            raise HTTPException(500, f"Failed to update remote app settings: {done.error_message}")
        return _app_to_dict(app, node)

    if app.domain and _nginx_proxy_port(app):
        maint_ok, maint_msg = _ensure_maintenance_files(app, app.id)
        if not maint_ok:
            raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
        ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
        config = nm.generate_config(
            app.name, app.domain, _nginx_proxy_port(app),
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
    return _app_to_dict(app, node)


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
            await db.delete(app)
            await db.commit()
            result_payload = json.loads(done.result or "{}") if done.result else {}
            return {"message": result_payload.get("message") or f"App '{app.name}' deleted"}
        else:
            # Node offline — remove from DB only; node cleans up its own files when it reconnects
            await db.delete(app)
            await db.commit()
            return {"message": f"App '{app.name}' removed (node '{node.name}' was offline — app files may still exist on the node)"}

    # Stop replica containers before deleting (DB rows cascade-delete with the app row)
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    node_map = await _load_node_map(db)
    for replica in replica_result.scalars().all():
        if replica.status in ("running", "starting"):
            r_node = node_map.get(replica.node_id)
            if r_node and r_node.is_local:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
            elif r_node and r_node.status == "online":
                await queue_node_command(
                    db, node_id=r_node.id, app_id=app_id,
                    command_type="stop_replica",
                    payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
                )

    if app.use_docker:
        await asyncio.to_thread(pm.stop_docker_app, app_id)
        await asyncio.to_thread(dm.remove_image, app_id, app.name)
    elif app.status == "running" and app.pid:
        pm.stop_app(app_id, app.pid)

    if app.nginx_enabled:
        nm.remove_nginx_config(app.name)

    app_dir = pm.get_app_dir(app.name)
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)

    app_name = app.name
    app_id_val = app.id
    await db.delete(app)
    await log_audit(db, "app.delete", actor=actor, detail={"name": app_name, "app_id": app_id_val})
    await db.commit()
    return {"message": f"App '{app_name}' deleted"}


@router.post("/export")
async def export_apps(req: ExportRequest, db: AsyncSession = Depends(get_db)):
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
            "external_port": None,
            "node_id": app.node_id,
            "maintenance_mode": app.maintenance_mode,
            "update_mode": app.update_mode,
            "downtime_page": json.loads(app.downtime_page or "{}"),
            "update_page": json.loads(app.update_page or "{}"),
            "restart_page": json.loads(app.restart_page or "{}"),
            "starting_page": json.loads(app.starting_page or "{}"),
        })

    return {"exported_apps": export_data}


@router.post("/import")
async def import_apps(req: ImportRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
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
            status="deploying",
            node_id=target_node.id,
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

        if not target_node.is_local:
            await queue_node_command(
                db,
                node_id=target_node.id,
                app_id=app.id,
                command_type="deploy_app",
                payload={
                    "name": app.name,
                    "repo_url": app.repo_url,
                    "github_token": app_data.get("github_token"),
                    "domain": app.domain,
                    "extra_domains": app_data.get("extra_domains") or [],
                    "redirect_domains": app_data.get("redirect_domains") or [],
                    "ssl_cert_path": None,
                    "ssl_key_path": None,
                    "start_command": app.start_command,
                    "port": app.port,
                    "external_port": import_external_port,
                    "docker_cpu_limit": app.docker_cpu_limit,
                    "docker_memory_limit_mb": app.docker_memory_limit_mb,
                    "docker_read_only_root": app.docker_read_only_root,
                    "docker_tmpfs_enabled": app.docker_tmpfs_enabled,
                    "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
                    "env_vars": app_data.get("env_vars") or {},
                    "auto_start": app.auto_start,
                    "restart_policy": app.restart_policy,
                    "use_docker": True,
                },
            )
        else:
            try:
                # Deploy it locally (async logic wait handled simply here)
                await _deploy_app(app)
                app.status = "stopped"
                if app.domain and app.port:
                    _ensure_maintenance_files(app, app.id)
                    config = nm.generate_config(
                        app.name, app.domain, app.external_port or app.port,
                        app.ssl_cert_path, app.ssl_key_path,
                        app_id=app.id, mode=_get_nginx_mode(app),
                        extra_domains=json.loads(app.extra_domains or "[]"),
                        redirect_domains=json.loads(app.redirect_domains or "[]"),
                    )
                    ok, _ = nm.write_nginx_config(app.name, config)
                    app.nginx_enabled = ok
                await db.commit()
            except Exception as e:
                app.status = "error"
                app.last_error = str(e)
                await db.commit()
        imported_count += 1

    return {"message": f"Successfully imported {imported_count} apps."}



class MoveRequest(BaseModel):
    target_node_id: int
    port: Optional[int] = None


@router.post("/{app_id}/move")
async def move_app(app_id: int, req: MoveRequest, db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    old_node = await _get_app_node(app, db, local_node)

    if old_node.id == req.target_node_id:
        raise HTTPException(400, "App is already on that node")

    target_node_result = await db.execute(
        select(Node).where(Node.id == req.target_node_id, Node.enabled == True)
    )
    target_node = target_node_result.scalar_one_or_none()
    if not target_node:
        raise HTTPException(400, "Target node not found or disabled")

    chosen_port = req.port if req.port is not None else app.port
    if chosen_port is not None and (chosen_port < 1 or chosen_port > 65535):
        raise HTTPException(400, "Port must be between 1 and 65535")

    # Check port availability on target node
    if chosen_port:
        conflict_result = await db.execute(
            select(Application).where(
                Application.node_id == req.target_node_id,
                Application.port == chosen_port,
                Application.id != app.id,
            )
        )
        conflict = conflict_result.scalar_one_or_none()
        if conflict:
            raise HTTPException(
                409,
                f"Port {chosen_port} is already used by app '{conflict.name}' on the target node",
            )
        if target_node.is_local:
            existing_pid = pm.find_process_by_port(chosen_port)
            if existing_pid:
                raise HTTPException(
                    409,
                    f"Port {chosen_port} is already occupied by process {existing_pid} on the local node",
                )

    was_running = app.status == "running"
    if old_node.is_local:
        if app.use_docker:
            was_running = await asyncio.to_thread(pm.is_docker_app_running, app_id)
        elif app.pid:
            was_running = await asyncio.to_thread(pm.is_process_running, app.pid, app.id)

    # Stop app on old node first
    if was_running:
        if old_node.is_local:
            if app.use_docker:
                await asyncio.to_thread(pm.stop_docker_app, app_id)
            else:
                pm.stop_app(app_id, app.pid)
            app.pid = None
        elif old_node.status == "online":
            stop_cmd = await queue_node_command(
                db, node_id=old_node.id, app_id=app.id,
                command_type="stop_app", payload={"app_id": app.id, "app_name": app.name},
            )
            try:
                await wait_for_node_command(db, stop_cmd.id, timeout_seconds=30)
            except Exception:
                pass  # Continue with migration even if stop times out

    # Remove from old node — wait so the old container is gone before we deploy on the new one
    if not old_node.is_local and old_node.status == "online":
        del_cmd = await queue_node_command(
            db, node_id=old_node.id, app_id=app.id,
            command_type="delete_app", payload={"app_id": app.id, "app_name": app.name},
        )
        try:
            await wait_for_node_command(db, del_cmd.id, timeout_seconds=30)
        except Exception:
            pass
    elif old_node.is_local:
        if app.use_docker:
            await asyncio.to_thread(pm.stop_docker_app, app_id)
        app_dir = pm.get_app_dir(app.name)
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir, ignore_errors=True)
        if app.nginx_enabled:
            nm.remove_nginx_config(app.name)
            app.nginx_enabled = False

    # Reassign to target node and reset state
    app.node_id = target_node.id
    app.port = chosen_port
    app.status = "stopped"
    app.pid = None
    app.working_dir = None
    await db.commit()

    # Deploy on new node
    # --- Transfer SSL certs to the target node if they exist on the source ---
    # Certs are only readable locally when the old node was the local node.
    import base64 as _b64
    _cert_content: bytes | None = None
    _key_content:  bytes | None = None
    _cert_filename: str | None  = None
    _key_filename:  str | None  = None

    if old_node.is_local and app.ssl_cert_path and app.ssl_key_path:
        try:
            with open(app.ssl_cert_path, "rb") as f:
                _cert_content = f.read()
            _cert_filename = os.path.basename(app.ssl_cert_path)
        except Exception:
            pass
        try:
            with open(app.ssl_key_path, "rb") as f:
                _key_content = f.read()
            _key_filename = os.path.basename(app.ssl_key_path)
        except Exception:
            pass

    if target_node.is_local:
        try:
            await _deploy_app(app)
            app.status = "stopped"

            if was_running:
                env_vars = decrypt_env(app.env_vars or "")
                if app.use_docker:
                    try:
                        await asyncio.to_thread(
                            pm.start_docker_app,
                            app_id,
                            app.name,
                            app.working_dir,
                            app.port or 8000,
                            app.external_port or app.port or 8000,
                            env_vars,
                            app.app_type or "unknown",
                            app.start_command or "",
                            _docker_runtime_options(app),
                            False,
                        )
                    except Exception:
                        await asyncio.to_thread(
                            pm.start_docker_app,
                            app_id,
                            app.name,
                            app.working_dir,
                            app.port or 8000,
                            app.external_port or app.port or 8000,
                            env_vars,
                            app.app_type or "unknown",
                            app.start_command or "",
                            _docker_runtime_options(app),
                            True,
                        )
                    app.status = "running"
                    app.pid = None
                else:
                    final_cmd, prepared_env = pm.prepare_app_env(app.start_command or "", app.working_dir, env_vars)
                    pid = pm.start_app(app_id, app.name, final_cmd, app.working_dir, prepared_env)
                    app.pid = pid
                    app.status = "running"

            await db.commit()
        except Exception as exc:
            app.status = "error"
            app.last_error = str(exc)
            await db.commit()
    else:
        # Queue cert uploads before deploy so nginx config can reference them
        new_cert_path: str | None = None
        new_key_path:  str | None = None
        if _cert_content and _cert_filename:
            cert_cmd = await queue_node_command(
                db, node_id=target_node.id, app_id=app.id,
                command_type="upload_cert",
                payload={
                    "app_id": app.id, "app_name": app.name,
                    "filename": _cert_filename,
                    "content_b64": _b64.b64encode(_cert_content).decode(),
                },
            )
            try:
                cert_done = await wait_for_node_command(db, cert_cmd.id, timeout_seconds=30)
                if cert_done.status == "done":
                    new_cert_path = (json.loads(cert_done.result or "{}") or {}).get("path")
            except Exception:
                pass
        if _key_content and _key_filename:
            key_cmd = await queue_node_command(
                db, node_id=target_node.id, app_id=app.id,
                command_type="upload_cert",
                payload={
                    "app_id": app.id, "app_name": app.name,
                    "filename": _key_filename,
                    "content_b64": _b64.b64encode(_key_content).decode(),
                },
            )
            try:
                key_done = await wait_for_node_command(db, key_cmd.id, timeout_seconds=30)
                if key_done.status == "done":
                    new_key_path = (json.loads(key_done.result or "{}") or {}).get("path")
            except Exception:
                pass

        # Update db with new cert paths on target (or clear them if transfer failed)
        app.ssl_cert_path = new_cert_path
        app.ssl_key_path  = new_key_path
        await db.commit()

        deploy_cmd = await queue_node_command(
            db,
            node_id=target_node.id,
            app_id=app.id,
            command_type="deploy_app",
            payload={
                "name": app.name,
                "repo_url": app.repo_url,
                "github_token": _decrypt_github_token(app.github_token),
                "domain": app.domain,
                "extra_domains": json.loads(app.extra_domains or "[]"),
                "redirect_domains": json.loads(app.redirect_domains or "[]"),
                "ssl_cert_path": new_cert_path,
                "ssl_key_path": new_key_path,
                "start_command": app.start_command,
                "port": app.port,
                "env_vars": decrypt_env(app.env_vars or ""),
                "auto_start": False,
                "restart_policy": app.restart_policy,
                "use_docker": True,
            },
        )
        app.status = "deploying"
        await db.commit()

        # If the app was running before the move, start it on the new node after deploy
        if was_running:
            try:
                deploy_done = await wait_for_node_command(db, deploy_cmd.id, timeout_seconds=180)
                if deploy_done.status != "done":
                    app.status = "error"
                    app.last_error = f"Move failed during deploy on target node: {deploy_done.error_message or 'unknown error'}"
                else:
                    await queue_node_command(
                        db,
                        node_id=target_node.id,
                        app_id=app.id,
                        command_type="start_app",
                        payload={"app_id": app.id, "app_name": app.name},
                    )
                    app.status = "starting"
                    app.last_error = None
            except Exception as exc:
                app.status = "error"
                app.last_error = f"Move failed while waiting for target deploy: {exc}"
            await db.commit()

    await db.refresh(app)
    return _app_to_dict(app, target_node)


@router.post("/{app_id}/start")
async def start_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        app.status = "starting"
        await db.commit()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="start_app",
            payload={"app_id": app.id, "app_name": app.name},
        )
        return {"status": "queued", "command_id": cmd.id, "message": f"Start queued on node '{node.name}'"}

    if app.use_docker:
        if pm.is_docker_app_running(app_id):
            raise HTTPException(400, "App is already running")
        if not app.working_dir:
            raise HTTPException(400, "No working directory — deploy the app first")

        start_started_at = asyncio.get_running_loop().time()
        show_starting_page = (
            app.nginx_enabled and app.domain and (app.external_port or app.port)
            and not app.maintenance_mode and not app.update_mode
        )
        if show_starting_page:
            _ensure_maintenance_files(app, app_id)
            starting_cfg = nm.generate_config(
                app.name, app.domain, app.external_port or app.port,
                app.ssl_cert_path, app.ssl_key_path,
                app_id=app_id, mode="starting",
                extra_domains=json.loads(app.extra_domains or "[]"),
                redirect_domains=json.loads(app.redirect_domains or "[]"),
            )
            nm.write_nginx_config(app.name, starting_cfg)
            pm._push_line(app_id, "Starting page enabled while the container comes online.")

        env_vars = decrypt_env(app.env_vars or "")
        try:
            container_id = await asyncio.to_thread(
                pm.start_docker_app,
                app_id, app.name, app.working_dir,
                app.port or 8000, app.external_port or app.port or 8000,
                env_vars, app.app_type or "unknown", app.start_command or "",
                _docker_runtime_options(app),
                False,
            )
        except Exception as e:
            pm._push_line(app_id, f"[Docker] Existing image start failed ({e}). Building image and retrying…")
            try:
                container_id = await asyncio.to_thread(
                    pm.start_docker_app,
                    app_id, app.name, app.working_dir,
                    app.port or 8000, app.external_port or app.port or 8000,
                    env_vars, app.app_type or "unknown", app.start_command or "",
                    _docker_runtime_options(app),
                    True,
                )
            except Exception as retry_e:
                msg = str(retry_e)
                if "already in progress" in msg.lower():
                    app.status = "starting"
                    await db.commit()
                    return {
                        "status": "starting",
                        "message": "A Docker build/start is already in progress for this app.",
                    }
                raise HTTPException(500, f"Failed to start Docker app: {retry_e}") from retry_e
        app.pid = None
        app.docker_image = dm.image_name(app_id, app.name)
        app.status = "running"

        proxy_port = app.external_port or app.port or 8000
        if not await _wait_for_host_port(proxy_port):
            pm._push_line(
                app_id,
                f"[Docker] Warning: container is running but host port {proxy_port} is not returning a healthy HTTP response yet. "
                "Possible causes: app still starting, wrong internal/external port mapping, or bind to localhost inside the container.",
            )

        if show_starting_page:
            asyncio.create_task(_restore_nginx_after_restart(
                app_id,
                app.name, app.domain, app.external_port or app.port,
                app.ssl_cert_path, app.ssl_key_path,
                None, start_started_at,
                json.loads(app.extra_domains or "[]"),
                json.loads(app.redirect_domains or "[]"),
                use_docker=True,
            ))

        # Start any stopped replicas
        replica_result = await db.execute(
            select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
        )
        for replica in replica_result.scalars().all():
            if replica.status in ("stopped", "error"):
                replica_node_result = await db.execute(select(Node).where(Node.id == replica.node_id))
                replica_node = replica_node_result.scalar_one_or_none()
                if replica_node and replica_node.is_local:
                    try:
                        rid = await asyncio.to_thread(
                            pm.start_docker_replica,
                            app_id, replica.id, app.name,
                            app.port or 8000, replica.external_port or app.port or 8000,
                            env_vars, _docker_runtime_options(app),
                        )
                        replica.status = "running"
                        replica.container_id = rid
                    except Exception:
                        replica.status = "error"
                elif replica_node and replica_node.status == "online":
                    remote_payload = _remote_replica_command_payload(
                        app,
                        env_vars,
                        replica.external_port or app.port or 8000,
                    )
                    await queue_node_command(
                        db, node_id=replica_node.id, app_id=app_id,
                        command_type="start_replica",
                        payload={**remote_payload, "replica_id": replica.id},
                    )
                    replica.status = "starting"

        await log_audit(db, "app.start", actor=actor, app_id=app_id, detail={"name": app.name})
        await db.commit()
        remote_payload = _remote_replica_command_payload(app, env_vars, external_port)
        return {"status": "running", "container_id": container_id[:12]}

    if app.status == "running" and app.pid and pm.is_process_running(app.pid, app.id):
            payload={**remote_payload, "replica_id": replica.id},
        app.nginx_enabled and app.domain and (app.external_port or app.port)
        and not app.maintenance_mode and not app.update_mode
    )
    if show_starting_page:
        _ensure_maintenance_files(app, app_id)
        starting_cfg = nm.generate_config(
            app.name, app.domain, _nginx_proxy_port(app),
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode="starting",
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        nm.write_nginx_config(app.name, starting_cfg)
        pm._push_line(app_id, "Starting page enabled while the app comes online.")

    env_vars = decrypt_env(app.env_vars or "")
    final_command, env_vars = pm.prepare_app_env(app.start_command, app.working_dir, env_vars)

    pid = pm.start_app(app_id, app.name, final_command, app.working_dir, env_vars)

    app.pid = pid
    app.status = "running"

    if show_starting_page:
        asyncio.create_task(_restore_nginx_after_restart(
            app_id,
            app.name, app.domain, app.external_port or app.port,
            app.ssl_cert_path, app.ssl_key_path,
            pid,
            start_started_at,
            json.loads(app.extra_domains or "[]"),
            json.loads(app.redirect_domains or "[]"),
        ))

    await log_audit(db, "app.start", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": "running", "pid": pid}


@router.post("/{app_id}/stop")
async def stop_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        if app.status not in ("running", "stopping"):
            return {"status": "stopped", "message": "App is not running"}
        app.status = "stopping"
        await db.commit()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="stop_app",
            payload={"app_id": app.id, "app_name": app.name},
        )
        return {"status": "queued", "command_id": cmd.id, "message": f"Stop queued on node '{node.name}'"}

    if app.use_docker:
        await asyncio.to_thread(pm.stop_docker_app, app_id)
    else:
        pm.stop_app(app_id, app.pid)
    app.status = "stopped"
    app.pid = None

    # Stop running replicas (keep rows so they can be restarted later)
    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
    )
    for replica in replica_result.scalars().all():
        if replica.status == "running":
            replica_node_result = await db.execute(select(Node).where(Node.id == replica.node_id))
            replica_node = replica_node_result.scalar_one_or_none()
            if replica_node and replica_node.is_local:
                await asyncio.to_thread(pm.stop_docker_replica, app_id, replica.id)
                replica.status = "stopped"
            elif replica_node and replica_node.status == "online":
                await queue_node_command(
                    db, node_id=replica_node.id, app_id=app_id,
                    command_type="stop_replica",
                    payload={"app_id": app_id, "replica_id": replica.id, "app_name": app.name},
                )
                replica.status = "stopping"

    await log_audit(db, "app.stop", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": "stopped"}


@router.post("/{app_id}/restart")
async def restart_app(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        app.status = "restarting"
        await db.commit()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="restart_app",
            payload={"app_id": app.id, "app_name": app.name},
        )
        return {"status": "queued", "command_id": cmd.id, "message": f"Restart queued on node '{node.name}'"}
    restart_started_at = asyncio.get_running_loop().time()

    # Temporarily show restart page via nginx (only when in normal mode)
    show_restart_page = (
        app.nginx_enabled and app.domain and (app.external_port or app.port)
        and not app.maintenance_mode and not app.update_mode
    )
    if show_restart_page:
        _ensure_maintenance_files(app, app_id)
        restart_cfg = nm.generate_config(
            app.name, app.domain, _nginx_proxy_port(app),
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode="restart",
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        nm.write_nginx_config(app.name, restart_cfg)
        pm._push_line(app_id, "Restart page enabled while the app comes back online.")

    if app.use_docker:
        await asyncio.to_thread(pm.stop_docker_app, app_id)
        await asyncio.sleep(1)
        env_vars = decrypt_env(app.env_vars or "")
        try:
            container_id = await asyncio.to_thread(
                pm.start_docker_app,
                app_id, app.name, app.working_dir,
                app.port or 8000, app.external_port or app.port or 8000,
                env_vars, app.app_type or "unknown", app.start_command or "",
                _docker_runtime_options(app),
                False,
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to restart Docker app: {e}") from e
        app.pid = None
        app.status = "running"

        proxy_port = app.external_port or app.port or 8000
        if not await _wait_for_host_port(proxy_port):
            pm._push_line(
                app_id,
                f"[Docker] Warning: container restarted, but host port {proxy_port} is not returning a healthy HTTP response yet. "
                "Possible causes: app still starting, wrong internal/external port mapping, or bind to localhost inside the container.",
            )

        if show_restart_page:
            asyncio.create_task(_restore_nginx_after_restart(
                app_id,
                app.name, app.domain, app.external_port or app.port,
                app.ssl_cert_path, app.ssl_key_path,
                None, restart_started_at,
                json.loads(app.extra_domains or "[]"),
                json.loads(app.redirect_domains or "[]"),
                use_docker=True,
            ))

        await log_audit(db, "app.restart", actor=actor, app_id=app_id, detail={"name": app.name})
        await db.commit()
        return {"status": "running", "container_id": container_id[:12]}

    if app.pid:
        pm.stop_app(app_id, app.pid)

    await asyncio.sleep(1)

    env_vars = decrypt_env(app.env_vars or "")
    final_command, env_vars = pm.prepare_app_env(app.start_command, app.working_dir, env_vars)

    pid = pm.start_app(app_id, app.name, final_command, app.working_dir, env_vars)

    app.pid = pid
    app.status = "running"

    if show_restart_page:
        asyncio.create_task(_restore_nginx_after_restart(
            app_id,
            app.name, app.domain, app.external_port or app.port,
            app.ssl_cert_path, app.ssl_key_path,
            pid,
            restart_started_at,
            json.loads(app.extra_domains or "[]"),
            json.loads(app.redirect_domains or "[]"),
        ))

    await log_audit(db, "app.restart", actor=actor, app_id=app_id, detail={"name": app.name})
    await db.commit()
    return {"status": "running", "pid": pid}


class ScaleRequest(BaseModel):
    node_id: Optional[int] = None


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
    Does NOT create a DB row — the main server already has it."""
    local_app = None
    if req.local_app_id is not None:
        local_app = await _get_or_404(req.local_app_id, db)
        app_name = local_app.name
    elif req.app_name:
        app_name = req.app_name
    else:
        app = await _get_or_404(app_id, db)
        app_name = app.name

    def _push(_aid, line):
        pm._push_line(app_id, str(line))

    try:
        env_vars = req.env_vars or {}
        container_id = await asyncio.to_thread(
            pm.start_docker_replica,
            app_id, req.replica_id, app_name,
            req.internal_port, req.external_port,
            env_vars, req.docker_options,
        )
        return {"container_id": container_id, "replica_id": req.replica_id}
    except Exception as e:
        if local_app and local_app.working_dir:
            try:
                await asyncio.to_thread(
                    dm.build_image,
                    app_id,
                    app_name,
                    local_app.working_dir,
                    _push,
                    local_app.app_type or "unknown",
                    local_app.start_command or "",
                    req.internal_port or local_app.port or 8000,
                )
                container_id = await asyncio.to_thread(
                    pm.start_docker_replica,
                    app_id,
                    req.replica_id,
                    app_name,
                    req.internal_port,
                    req.external_port,
                    env_vars,
                    req.docker_options,
                )
                return {"container_id": container_id, "replica_id": req.replica_id, "rebuilt": True}
            except Exception as rebuild_error:
                raise HTTPException(500, str(rebuild_error)) from rebuild_error
        raise HTTPException(500, str(e)) from e


@router.delete("/{app_id}/replicas/{replica_id}/stop-remote")
async def stop_replica_remote(app_id: int, replica_id: int):
    """Internal endpoint called by the node agent to stop a replica container locally."""
    ok = await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)
    return {"ok": ok, "replica_id": replica_id}


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
    """Return ALL instances: the primary container (instance 0) plus all replicas.

    This is the unified view used by the Instances tab — no "main vs extras" distinction.
    """
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node_map = await _load_node_map(db)

    app_node = node_map.get(app.node_id) or local_node
    primary = {
        "id": 0,          # sentinel: primary container
        "app_id": app.id,
        "node_id": app.node_id or app_node.id,
        "node_name": app_node.name if app_node else None,
        "node_is_local": bool(app_node.is_local) if app_node else True,
        "external_port": app.external_port,
        "tunnel_port": None,
        "tunnel_connected": False,
        "container_id": None,   # primary doesn't store container_id on app row
        "status": app.status,
        "last_error": app.last_error,
        "is_primary": True,
        "created_at": app.created_at.isoformat() if app.created_at else None,
    }

    rep_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.app_id == app_id)
        .order_by(ApplicationReplica.id)
    )
    replicas = rep_result.scalars().all()
    replica_dicts = [
        {**_replica_to_dict(r, node_map.get(r.node_id)), "is_primary": False}
        for r in replicas
    ]

    return [primary] + replica_dicts


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


@router.post("/{app_id}/scale")
async def scale_app(app_id: int, req: ScaleRequest, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    # Resolve target node for the new replica
    if req.node_id is not None:
        target_node_result = await db.execute(select(Node).where(Node.id == req.node_id, Node.enabled == True))
        target_node = target_node_result.scalar_one_or_none()
        if not target_node:
            raise HTTPException(400, "Target node not found or not enabled")
    else:
        target_node = await _get_app_node(app, db, local_node)

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

    if target_node.is_local:
        try:
            container_id = await asyncio.to_thread(
                pm.start_docker_replica,
                app_id, replica.id, app.name,
                app.port or 8000, external_port,
                env_vars, _docker_runtime_options(app),
            )
            replica.status = "running"
            replica.container_id = container_id
        except Exception as e:
            replica.status = "error"
            replica.last_error = str(e)
            await db.commit()
            raise HTTPException(500, f"Failed to start replica: {e}") from e

        # Regenerate nginx with new backend
        if app.nginx_enabled and app.domain:
            backends = await _get_nginx_backends(app, db, local_node)
            _ensure_maintenance_files(app, app_id)
            config = nm.generate_config(
                app.name, app.domain, backends,
                app.ssl_cert_path, app.ssl_key_path,
                app_id=app_id, mode=_get_nginx_mode(app),
                extra_domains=json.loads(app.extra_domains or "[]"),
                redirect_domains=json.loads(app.redirect_domains or "[]"),
            )
            nm.write_nginx_config(app.name, config)
    else:
        if target_node.status != "online":
            raise HTTPException(400, f"Node '{target_node.name}' is not online")
        _ensure_replica_arch_compatible(local_node, target_node)
        await queue_node_command(
            db, node_id=target_node.id, app_id=app_id,
            command_type="start_replica",
            payload={
                "app_id": app_id, "replica_id": replica.id,
                "app_name": app.name,
                "internal_port": app.port or 8000,
                "external_port": external_port,
                "env_vars": env_vars,
                "docker_options": _docker_runtime_options(app),
            },
        )
        replica.status = "starting"

    await log_audit(db, "app.scale_up", actor=actor, app_id=app_id, detail={"replica_id": replica.id, "node": target_node.name})
    await db.commit()
    await db.refresh(replica)
    return _replica_to_dict(replica, target_node)


@router.delete("/{app_id}/replicas/{replica_id}")
async def remove_replica(app_id: int, replica_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)

    replica_result = await db.execute(
        select(ApplicationReplica).where(ApplicationReplica.id == replica_id, ApplicationReplica.app_id == app_id)
    )
    replica = replica_result.scalar_one_or_none()
    if not replica:
        raise HTTPException(404, "Replica not found")

    replica_node = None
    if replica.node_id:
        node_result = await db.execute(select(Node).where(Node.id == replica.node_id))
        replica_node = node_result.scalar_one_or_none()

    if replica_node and replica_node.is_local:
        await asyncio.to_thread(pm.stop_docker_replica, app_id, replica_id)
    elif replica_node and replica_node.status == "online":
        await queue_node_command(
            db, node_id=replica_node.id, app_id=app_id,
            command_type="stop_replica",
            payload={"app_id": app_id, "replica_id": replica_id, "app_name": app.name},
        )

    await db.delete(replica)

    # Regenerate nginx without the removed backend
    if app.nginx_enabled and app.domain:
        await db.flush()
        backends = await _get_nginx_backends(app, db, local_node)
        _ensure_maintenance_files(app, app_id)
        config = nm.generate_config(
            app.name, app.domain, backends,
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        nm.write_nginx_config(app.name, config)

    await log_audit(db, "app.scale_down", actor=actor, app_id=app_id, detail={"replica_id": replica_id})
    await db.commit()
    return {"ok": True}


@router.post("/{app_id}/pull")
async def git_pull(app_id: int, payload: PullRequest | None = Body(default=None), db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    target_commit = (payload.commit.strip() if payload and payload.commit else None)

    if not node.is_local:
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="git_pull",
            payload={"app_id": app.id, "app_name": app.name, "commit": target_commit},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=180)
        if done.status != "done":
            raise HTTPException(500, f"Failed to pull on node '{node.name}': {done.error_message}")
        return json.loads(done.result or "{}")

    app_dir = pm.get_app_dir(app.name)

    if not os.path.exists(app_dir):
        # The directory is missing! Let's try to restore it by re-deploying.
        log.warning("[git-pull] Directory %s missing for app %s, attempting re-clone", app_dir, app.name)
        try:
            await _deploy_app(app)
            return {"message": "App directory was missing; performed a fresh clone successfully", "output": "Fresh clone completed."}
        except Exception as e:
            raise HTTPException(500, f"App directory was missing and re-clone failed: {e}")

    github_token = _decrypt_github_token(app.github_token)
    if github_token:
        url = _build_clone_url(app.repo_url, github_token)
        subprocess.run(["git", "remote", "set-url", "origin", url], cwd=app_dir, capture_output=True)

    branch = _current_branch(app_dir)
    _fetch_origin(app_dir, branch)

    # 3. Reset to selected commit or latest origin branch
    target = target_commit or f"origin/{branch}"
    reset = subprocess.run(["git", "reset", "--hard", target], cwd=app_dir, capture_output=True, text=True)
    if reset.returncode != 0 and not target_commit:
        # Final fallback to @{u}
        reset = subprocess.run(["git", "reset", "--hard", "@{u}"], cwd=app_dir, capture_output=True, text=True)
    
    if reset.returncode != 0:
        raise HTTPException(500, f"Git reset failed: {reset.stderr}")

    # 4. Get latest commit info for confirmation
    log_res = subprocess.run(["git", "log", "-1", "--format=%h - %s (%cr)"], cwd=app_dir, capture_output=True, text=True)
    commit_info = log_res.stdout.strip() if log_res.returncode == 0 else "Unknown"

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
        await log_audit(db, "app.pull", actor=actor, app_id=app_id, detail={"name": app.name, "commit": commit_info})
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
            "action_logs": action_logs,
        }

    return {
        "message": f"Updated code to {target_commit or branch}",
        "output": f"{reset.stdout.strip()}\nLatest commit: {commit_info}\n\nNote: You may need to RESTART the app to apply changes.",
        "commit": commit_info
    }


@router.post("/{app_id}/rebuild")
async def rebuild_docker_image(app_id: int, db: AsyncSession = Depends(get_db), actor: str = Depends(_auth.get_current_actor)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not app.use_docker:
        raise HTTPException(400, "Rebuild is only available for Docker apps")
    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    if not node.is_local:
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="rebuild_app",
            payload={"app_id": app.id, "app_name": app.name},
        )
        return {"status": "queued", "command_id": cmd.id, "message": f"Rebuild queued on node '{node.name}'"}

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
        app.status = "running" if was_running else "stopped"
        app.docker_image = img
        await log_audit(db, "app.rebuild", actor=actor, app_id=app_id, detail={"name": app.name, "image": img})
        await db.commit()
        if was_running:
            action_logs.append("[Docker] Image rebuilt. Running container left untouched (no restart). Restart manually to use the new image.")
            return {
                "status": "running",
                "message": "Image rebuilt. Running container was not restarted.",
                "output": "Image rebuilt. Running container was not restarted. Restart manually to switch to the new image.",
                "action_logs": action_logs,
            }
        action_logs.append("[Docker] Image rebuilt. Start the app to run it.")
        return {
            "status": "rebuilt",
            "message": "Image rebuilt. Start the app to run it.",
            "output": "Image rebuilt. Start the app to run it.",
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
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        raise HTTPException(400, "Zero-downtime deploy is only available for local apps")
    if not app.use_docker:
        raise HTTPException(400, "Zero-downtime deploy is only available for Docker apps")
    if not app.nginx_enabled or not app.domain:
        raise HTTPException(400, "Zero-downtime deploy requires nginx to be enabled with a domain")
    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    current_slot = app.active_slot or "blue"
    new_slot = "green" if current_slot == "blue" else "blue"
    internal_port = app.port or 8000
    current_ext_port = app.external_port or internal_port

    result_port_q = await db.execute(select(Application.external_port).where(Application.external_port.isnot(None)))
    used_ports = {row[0] for row in result_port_q.all()}
    used_ports.add(current_ext_port)
    temp_port = await asyncio.to_thread(dm.pick_free_external_port, used_ports)

    pm._push_line(app_id, f"[ZD] Zero-downtime deploy starting: active={current_slot} → new={new_slot}")
    pm._push_line(app_id, f"[ZD] Temp port for new {new_slot} slot: {temp_port}")

    try:
        img = await asyncio.to_thread(
            dm.build_image_for_slot,
            app_id, app.name, app.working_dir, pm._push_line,
            app.app_type or "unknown", app.start_command or "", internal_port, new_slot,
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to build {new_slot} image: {e}")

    env_vars = decrypt_env(app.env_vars or "")
    try:
        await asyncio.to_thread(
            dm.run_container_for_slot,
            app_id, app.name, new_slot, img,
            internal_port, temp_port,
            env_vars, _docker_runtime_options(app), pm._push_line,
        )
    except Exception as e:
        await asyncio.to_thread(dm.stop_slot_container, app_id, new_slot)
        raise HTTPException(500, f"Failed to start {new_slot} container: {e}")

    pm._push_line(app_id, f"[ZD] Waiting for health check on port {temp_port} (max 60s)…")
    deadline = asyncio.get_running_loop().time() + 60
    healthy = False
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(_local_http_service_ready, temp_port):
            healthy = True
            break
        await asyncio.sleep(2)

    if not healthy:
        pm._push_line(app_id, f"[ZD] Health check failed — rolling back, stopping {new_slot} slot")
        await asyncio.to_thread(dm.stop_slot_container, app_id, new_slot)
        raise HTTPException(502, f"New {new_slot} slot failed health check after 60s — rolled back")

    pm._push_line(app_id, "[ZD] Health check passed. Swapping nginx to new slot…")
    ssl_cert, ssl_key = _resolve_ssl_paths(app.ssl_cert_path, app.ssl_key_path)
    swap_cfg = nm.generate_config(
        app.name, app.domain, temp_port,
        ssl_cert, ssl_key,
        app_id=app_id, mode="normal",
        extra_domains=json.loads(app.extra_domains or "[]"),
        redirect_domains=json.loads(app.redirect_domains or "[]"),
    )
    ok, msg = nm.write_nginx_config(app.name, swap_cfg)
    if not ok:
        await asyncio.to_thread(dm.stop_slot_container, app_id, new_slot)
        raise HTTPException(500, f"Nginx swap failed: {msg}")

    pm._push_line(app_id, f"[ZD] Nginx now routing to {new_slot} on port {temp_port}. Stopping old {current_slot} container…")
    await asyncio.to_thread(dm.stop_container, app_id)

    try:
        client = dm._get_client()
        slot_cname = dm.slot_container_name(app_id, new_slot)
        canonical = dm.container_name(app_id)
        c = client.containers.get(slot_cname)
        c.rename(canonical)
        pm._push_line(app_id, f"[ZD] Container renamed to {canonical}")
    except Exception as e:
        pm._push_line(app_id, f"[ZD] Warning: could not rename container: {e}")

    # The new container runs on temp_port — persist that as the new external_port.
    # Nginx already points to temp_port from the swap above; no second write needed.
    app.active_slot = new_slot
    app.docker_image = img
    app.external_port = temp_port
    app.status = "running"
    await log_audit(db, "app.zero_downtime_deploy", actor=actor, app_id=app_id, detail={"name": app.name, "slot": new_slot})
    await db.commit()

    pm._push_line(app_id, f"[ZD] Zero-downtime deploy complete. Active slot: {new_slot} on port {temp_port}")
    return {"status": "ok", "active_slot": new_slot, "image": img}


def _sse_line(data: str) -> str:
    """Format a single SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/{app_id}/pull/stream")
async def git_pull_stream(app_id: int, payload: PullRequest | None = Body(default=None), db: AsyncSession = Depends(get_db)):
    """Streaming SSE variant of git_pull. Each build log line is emitted as it happens."""
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)
    target_commit = (payload.commit.strip() if payload and payload.commit else None)

    if not node.is_local:
        # Remote: queue command, wait, then replay action_logs
        async def _remote_gen():
            yield _sse_line(f"[Remote] Queueing pull on node '{node.name}'…")
            cmd = await queue_node_command(
                db, node_id=node.id, app_id=app.id,
                command_type="git_pull",
                payload={"app_id": app.id, "app_name": app.name, "commit": target_commit},
            )
            yield _sse_line(f"[Remote] Command queued (id={cmd.id}), waiting…")
            done = await wait_for_node_command(db, cmd.id, timeout_seconds=180)
            if done.status != "done":
                yield _sse_line(f"[Error] {done.error_message or 'Pull failed on remote node'}")
                yield "data: __FAILED__\n\n"
                return
            result = json.loads(done.result or "{}")
            for line in result.get("action_logs", []):
                yield _sse_line(str(line))
            yield f"event: result\ndata: {json.dumps(result)}\n\n"
            yield "data: __DONE__\n\n"
        return StreamingResponse(_remote_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

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
                await db.commit()

                if was_running:
                    _q("[Docker] Image rebuilt. Running container left untouched. Restart manually to use the new image.")
                else:
                    _q("[Docker] Image rebuilt. Start the app to apply changes.")

                result_holder["result"] = {
                    "message": f"Updated and rebuilt Docker image from {target_commit or branch}",
                    "commit": commit_info,
                    "output": (
                        "Image rebuilt. Restart manually to switch to the new image."
                        if was_running else
                        "Image rebuilt. Start the app to apply changes."
                    ),
                }
            else:
                result_holder["result"] = {
                    "message": f"Updated code to {target_commit or branch}",
                    "output": f"Latest commit: {commit_info}\n\nRestart the app to apply changes.",
                    "commit": commit_info,
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
async def rebuild_docker_image_stream(app_id: int, db: AsyncSession = Depends(get_db)):
    """Streaming SSE variant of rebuild_docker_image."""
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not app.use_docker:
        raise HTTPException(400, "Rebuild is only available for Docker apps")
    if not app.working_dir:
        raise HTTPException(400, "No working directory — deploy the app first")

    if not node.is_local:
        async def _remote_rebuild_gen():
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                yield _sse_line(f"[Remote] Queueing rebuild on node '{node.name}' (attempt {attempt}/{max_attempts})…")
                cmd = await queue_node_command(
                    db, node_id=node.id, app_id=app.id,
                    command_type="rebuild_app",
                    payload={"app_id": app.id, "app_name": app.name},
                )
                yield _sse_line(f"[Remote] Command queued (id={cmd.id}), waiting…")
                done = await wait_for_node_command(db, cmd.id, timeout_seconds=900)
                if done.status == "done":
                    result = json.loads(done.result or "{}")
                    for line in result.get("action_logs", []):
                        yield _sse_line(str(line))
                    yield f"event: result\ndata: {json.dumps(result)}\n\n"
                    yield "data: __DONE__\n\n"
                    return

                err = (done.error_message or "").strip()
                retryable = (
                    "500 internal server error" in err.lower()
                    or "build already in progress" in err.lower()
                )
                if retryable and attempt < max_attempts:
                    yield _sse_line(f"[Remote] Temporary failure from node: {err or 'unknown error'}. Retrying…")
                    await asyncio.sleep(0.7 * attempt)
                    continue

                yield _sse_line(f"[Error] {err or 'Rebuild failed on remote node'}")
                yield "data: __FAILED__\n\n"
                return
        return StreamingResponse(_remote_rebuild_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

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
            app.status = "running" if was_running else "stopped"
            app.docker_image = img
            await db.commit()

            if was_running:
                _q("[Docker] Image rebuilt. Running container left untouched. Restart manually to use the new image.")
                result_holder["result"] = {
                    "status": "running",
                    "message": "Image rebuilt. Running container was not restarted.",
                    "output": "Image rebuilt. Restart manually to switch to the new image.",
                }
            else:
                _q("[Docker] Image rebuilt. Start the app to run it.")
                result_holder["result"] = {
                    "status": "rebuilt",
                    "message": "Image rebuilt. Start the app to run it.",
                    "output": "Image rebuilt. Start the app to run it.",
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


@router.post("/{app_id}/install-deps")
async def install_deps(app_id: int, db: AsyncSession = Depends(get_db)):
    raise HTTPException(
        400,
        "Install Dependencies is not available in Docker-only mode. Rebuild the image instead.",
    )


@router.get("/{app_id}/nginx-config")
async def get_nginx_config(app_id: int, db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="get_nginx_config",
            payload={"app_id": app.id, "app_name": app.name},
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            raise HTTPException(500, f"Failed to get remote nginx config: {done.error_message}")
        return json.loads(done.result or "{}")

    safe = nm._safe_name(app.name)
    config_path = os.path.join(nm.NGINX_SITES_DIR, safe)
    if not os.path.exists(config_path):
        generated = None
        if app.domain and app.port:
            generated = nm.generate_config(
                app.name, app.domain, app.external_port or app.port,
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
async def save_nginx_config(app_id: int, payload: dict, db: AsyncSession = Depends(get_db)):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    if not node.is_local:
        remote_payload = dict(payload or {})
        remote_payload["app_id"] = app.id
        remote_payload["app_name"] = app.name
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="save_nginx_config",
            payload=remote_payload,
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=20)
        if done.status != "done":
            raise HTTPException(500, f"Failed to save remote nginx config: {done.error_message}")

        result_payload = json.loads(done.result or "{}") if done.result else {}
        await db.refresh(app)
        return result_payload

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
    if app.use_docker:
        if pm.is_docker_app_running(app_id):
            stats = await asyncio.to_thread(pm.get_docker_stats, app_id)
            return {"status": "running", "docker": True, **stats}
        return {"status": "stopped", "docker": True}
    if app.pid and pm.is_process_running(app.pid, app.id):
        stats = pm.get_process_stats(app.pid)
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


def _app_to_dict(app: Application, node: Optional[Node] = None, include_sensitive: bool = True, replicas: Optional[list] = None) -> dict:
    return {
        "id": app.id,
        "name": app.name,
        "repo_url": app.repo_url,
        "domain": app.domain,
        "extra_domains": json.loads(app.extra_domains or "[]"),
        "redirect_domains": json.loads(app.redirect_domains or "[]"),
        "app_type": app.app_type,
        "start_command": app.start_command,
        "port": app.port,
        "external_port": app.external_port,
        "status": app.status,
        "pid": app.pid,
        "working_dir": app.working_dir,
        "last_error": app.last_error,
        "env_vars": decrypt_env(app.env_vars or "") if include_sensitive else {},
        "nginx_enabled": app.nginx_enabled,
        "node_id": app.node_id,
        "node": {
            "id": node.id,
            "name": node.name,
            "status": node.status,
            "is_local": bool(node.is_local),
            "public_host": node.public_host,
        } if node else None,
        "auto_start":     app.auto_start,
        "restart_policy": app.restart_policy or "no",
        "use_docker":     True,
        "docker_image":   app.docker_image,
        "docker_cpu_limit": app.docker_cpu_limit,
        "docker_memory_limit_mb": app.docker_memory_limit_mb,
        "docker_read_only_root": bool(app.docker_read_only_root),
        "docker_tmpfs_enabled": bool(app.docker_tmpfs_enabled),
        "docker_tmpfs_size_mb": app.docker_tmpfs_size_mb,
        "maintenance_mode": app.maintenance_mode or False,
        "update_mode":      app.update_mode or False,
        "downtime_page":    json.loads(app.downtime_page or "{}"),
        "update_page":      json.loads(app.update_page   or "{}"),
        "restart_page":     json.loads(app.restart_page  or "{}"),
        "starting_page":    json.loads(app.starting_page or "{}"),
        "ssl_cert_path": app.ssl_cert_path,
        "ssl_key_path": app.ssl_key_path,
        "github_token": "***" if app.github_token else None,
        "created_at": app.created_at.isoformat() if app.created_at else None,
        "updated_at": app.updated_at.isoformat() if app.updated_at else None,
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
        "created_at": replica.created_at.isoformat() if replica.created_at else None,
    }


async def _get_nginx_backends(app: Application, db: AsyncSession, local_node: "Node") -> "int | list[str]":
    """Return nginx backend(s) for an app.

    Returns a single int (legacy direct proxy) when there are no running replicas.
    Returns a list[str] of "host:port" strings for upstream load balancing when
    one or more replicas are running.

    Remote replicas are reached via the reverse WebSocket tunnel: their backend
    address is 127.0.0.1:{replica.tunnel_port} on the main node, not the remote
    node's public IP.  Replicas that are running but have no tunnel_port yet
    (tunnel still connecting) are omitted from the upstream list.
    """
    result = await db.execute(
        select(ApplicationReplica, Node).join(Node, ApplicationReplica.node_id == Node.id, isouter=True).where(
            ApplicationReplica.app_id == app.id,
            ApplicationReplica.status == "running",
        )
    )
    running_replicas = result.all()

    if not running_replicas:
        return app.external_port or app.port

    app_node_result = await db.execute(select(Node).where(Node.id == app.node_id))
    app_node = app_node_result.scalar_one_or_none() or local_node

    def _main_addr() -> Optional[str]:
        port = app.external_port or app.port
        if not port:
            return None
        if app_node.is_local:
            return f"127.0.0.1:{port}"
        # App itself is on a remote node — use its public host directly
        # (only the replicas use the reverse tunnel)
        return f"{app_node.public_host}:{port}" if app_node.public_host else None

    def _replica_addr(replica: ApplicationReplica, r_node: Optional[Node]) -> Optional[str]:
        if r_node is None or r_node.is_local:
            # Local replica — connect directly
            return f"127.0.0.1:{replica.external_port}" if replica.external_port else None
        # Remote replica — use the reverse tunnel port on the main node
        return f"127.0.0.1:{replica.tunnel_port}" if replica.tunnel_port else None

    backends: list[str] = []
    main_addr = _main_addr()
    if main_addr:
        backends.append(main_addr)
    for replica, r_node in running_replicas:
        addr = _replica_addr(replica, r_node)
        if addr:
            backends.append(addr)

    if len(backends) <= 1:
        return app.external_port or app.port
    return backends


async def _get_app_node(app: Application, db: AsyncSession, local_node: Optional[Node] = None) -> Node:
    if app.node_id:
        result = await db.execute(select(Node).where(Node.id == app.node_id))
        node = result.scalar_one_or_none()
        if node:
            return node
    if local_node is not None:
        if app.node_id is None:
            app.node_id = local_node.id
            await db.commit()
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
    db: AsyncSession = Depends(get_db),
):
    app = await _get_or_404(app_id, db)
    local_node = await ensure_local_node(db)
    node = await _get_app_node(app, db, local_node)

    app.downtime_page = json.dumps(req.downtime_page.model_dump())
    app.update_page   = json.dumps(req.update_page.model_dump())
    app.restart_page  = json.dumps(req.restart_page.model_dump())
    app.starting_page = json.dumps(req.starting_page.model_dump())

    if not node.is_local:
        remote_payload = req.model_dump()
        remote_payload["app_id"] = app.id
        remote_payload["app_name"] = app.name
        await db.commit()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="save_maintenance_pages",
            payload=remote_payload,
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=60)
        if done.status != "done":
            raise HTTPException(500, f"Failed to save maintenance settings on node '{node.name}': {done.error_message}")
        return json.loads(done.result or "{}")

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
    if app.nginx_enabled and app.domain:
        mode   = _get_nginx_mode(app)
        config = nm.generate_config(
            app.name, app.domain, _nginx_proxy_port(app),
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode=mode,
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        )
        nginx_ok, nginx_msg = nm.write_nginx_config(app.name, config)
        if not nginx_ok:
            await db.commit()
            return {"ok": False, "message": f"Files saved but nginx reload failed: {nginx_msg}"}

    await db.commit()
    return {"ok": True, "message": "Saved"}


@router.post("/{app_id}/maintenance-mode/toggle")
async def toggle_maintenance_mode(app_id: int, db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    node = await _get_app_node(app, db, local_node)
    previous_maintenance_mode = bool(app.maintenance_mode)
    previous_update_mode = bool(app.update_mode)
    
    if not app.domain:
        raise HTTPException(400, "A domain must be configured to use maintenance mode")
    
    if node.is_local and not app.nginx_enabled:
        raise HTTPException(400, "Nginx must be configured to use maintenance mode")

    app.maintenance_mode = not (app.maintenance_mode or False)
    if app.maintenance_mode:
        app.update_mode = False  # mutex: only one mode at a time

    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        await db.commit()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="toggle_maintenance_mode",
            payload={
                "app_id": app.id,
                "app_name": app.name,
                "maintenance_mode": bool(app.maintenance_mode),
                "update_mode": bool(app.update_mode),
                "previous_maintenance_mode": previous_maintenance_mode,
                "previous_update_mode": previous_update_mode,
            },
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=30)
        await db.refresh(app)
        if done.status != "done":
            raise HTTPException(500, f"Failed to toggle maintenance mode on node '{node.name}': {done.error_message}")
        return _app_to_dict(app, node)

    mode = _get_nginx_mode(app)
    log.info("[toggle-maintenance] app_id=%d new_mode=%r nginx_mode=%r domain=%r port=%r",
             app_id, app.maintenance_mode, mode, app.domain, _nginx_proxy_port(app))

    maint_ok, maint_msg = _ensure_maintenance_files(app, app_id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
    config = nm.generate_config(
        app.name, app.domain, _nginx_proxy_port(app),
        app.ssl_cert_path, app.ssl_key_path,
        app_id=app_id, mode=mode,
        extra_domains=json.loads(app.extra_domains or "[]"),
        redirect_domains=json.loads(app.redirect_domains or "[]"),
    )
    ok, msg = nm.write_nginx_config(app.name, config)
    log.info("[toggle-maintenance] write_nginx_config ok=%s msg=%r", ok, msg)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    await db.commit()
    return _app_to_dict(app, node)


@router.post("/{app_id}/update-mode/toggle")
async def toggle_update_mode(app_id: int, db: AsyncSession = Depends(get_db)):
    local_node = await ensure_local_node(db)
    app = await _get_or_404(app_id, db)
    node = await _get_app_node(app, db, local_node)
    previous_maintenance_mode = bool(app.maintenance_mode)
    previous_update_mode = bool(app.update_mode)

    if not app.domain:
        raise HTTPException(400, "A domain must be configured to use update mode")
        
    if node.is_local and not app.nginx_enabled:
        raise HTTPException(400, "Nginx must be configured to use update mode")

    app.update_mode = not (app.update_mode or False)
    if app.update_mode:
        app.maintenance_mode = False  # mutex: only one mode at a time

    node = await _get_app_node(app, db, local_node)
    if not node.is_local:
        await db.commit()
        cmd = await queue_node_command(
            db,
            node_id=node.id,
            app_id=app.id,
            command_type="toggle_update_mode",
            payload={
                "app_id": app.id,
                "app_name": app.name,
                "maintenance_mode": bool(app.maintenance_mode),
                "update_mode": bool(app.update_mode),
                "previous_maintenance_mode": previous_maintenance_mode,
                "previous_update_mode": previous_update_mode,
            },
        )
        done = await wait_for_node_command(db, cmd.id, timeout_seconds=30)
        await db.refresh(app)
        if done.status != "done":
            raise HTTPException(500, f"Failed to toggle update mode on node '{node.name}': {done.error_message}")
        return _app_to_dict(app, node)

    mode = _get_nginx_mode(app)
    log.info("[toggle-update] app_id=%d new_mode=%r nginx_mode=%r domain=%r port=%r",
             app_id, app.update_mode, mode, app.domain, _nginx_proxy_port(app))

    maint_ok, maint_msg = _ensure_maintenance_files(app, app_id)
    if not maint_ok:
        raise HTTPException(500, f"Maintenance files failed: {maint_msg}")
    config = nm.generate_config(
        app.name, app.domain, _nginx_proxy_port(app),
        app.ssl_cert_path, app.ssl_key_path,
        app_id=app_id, mode=mode,
        extra_domains=json.loads(app.extra_domains or "[]"),
        redirect_domains=json.loads(app.redirect_domains or "[]"),
    )
    ok, msg = nm.write_nginx_config(app.name, config)
    log.info("[toggle-update] write_nginx_config ok=%s msg=%r", ok, msg)
    if not ok:
        raise HTTPException(500, f"Nginx config failed: {msg}")

    await db.commit()
    return _app_to_dict(app, node)


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
            app.name, app.domain or "(no domain)", _nginx_proxy_port(app) or 0,
            app.ssl_cert_path, app.ssl_key_path,
            app_id=app_id, mode=_get_nginx_mode(app),
            extra_domains=json.loads(app.extra_domains or "[]"),
            redirect_domains=json.loads(app.redirect_domains or "[]"),
        ),
    }
