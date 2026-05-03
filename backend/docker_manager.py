import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Optional

log = logging.getLogger("pdm.docker")

# ── Lazy Docker client ────────────────────────────────────────────────────────

_docker_client = None
_docker_lock = threading.Lock()
_build_locks: dict[int, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _get_client():
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    with _docker_lock:
        if _docker_client is not None:
            return _docker_client
        import docker
        _docker_client = docker.from_env()
        return _docker_client


def _assert_image_local(client, img: str) -> None:
    """Raise RuntimeError if the image is not present locally, preventing docker-py
    from silently falling through to a Docker Hub pull attempt."""
    import docker
    try:
        client.images.get(img)
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"Image '{img}' not found locally. Deploy the app first to build the image."
        )


def _get_build_lock(app_id: int) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(app_id)
        if lock is None:
            lock = threading.Lock()
            _build_locks[app_id] = lock
        return lock


def is_docker_available() -> bool:
    try:
        client = _get_client()
        client.ping()
        return True
    except Exception:
        return False


# ── Container naming ──────────────────────────────────────────────────────────

def container_name(app_id: int) -> str:
    return f"cloudbase-app-{app_id}"


def image_name(app_id: int, app_name: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "-", app_name.lower())
    return f"cloudbase/{safe}-{app_id}:latest"


def _stringify_build_error(error) -> str:
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or error.get("detail")
        if message:
            return str(message)
        try:
            return json.dumps(error, ensure_ascii=False)
        except Exception:
            return str(error)
    try:
        return str(error)
    except Exception:
        return "Unknown Docker build error"


def _iter_build_events(log_stream):
    decoder = json.JSONDecoder()
    buffer = ""

    for raw in log_stream:
        if raw is None:
            continue
        if isinstance(raw, dict):
            yield raw
            continue

        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)

        if not text:
            continue

        buffer += text
        while buffer:
            buffer = buffer.lstrip()
            if not buffer:
                break
            try:
                chunk, idx = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                newline_idx = buffer.find("\n")
                if newline_idx == -1:
                    break
                line = buffer[:newline_idx].strip()
                buffer = buffer[newline_idx + 1 :]
                if line:
                    yield {"stream": line}
                continue
            yield chunk
            buffer = buffer[idx:]

    trailing = buffer.strip()
    if trailing:
        try:
            yield decoder.raw_decode(trailing)[0]
        except Exception:
            yield {"stream": trailing}


def _emit_build_event(app_id: int, event: dict, push_line_fn) -> None:
    if "stream" in event:
        text = str(event["stream"]).rstrip()
        if text:
            push_line_fn(app_id, f"[Docker build] {text}")
        return

    if "error" in event or "errorDetail" in event:
        error = event.get("error") or event.get("errorDetail")
        raise RuntimeError(_stringify_build_error(error))

    status = event.get("status")
    if status:
        parts = [str(status)]
        if event.get("id"):
            parts.append(str(event["id"]))
        if event.get("progress"):
            parts.append(str(event["progress"]))
        push_line_fn(app_id, f"[Docker build] {' '.join(parts)}")


def _is_loopback_bind_log(line: str, internal_port: Optional[int]) -> bool:
    low = (line or "").lower()
    if "127.0.0.1" not in low and "localhost" not in low:
        return False

    markers = (
        "running on",
        "listening on",
        "started server",
        "server running",
        "uvicorn running",
    )
    if not any(m in low for m in markers):
        return False

    if internal_port is None:
        return True
    return f":{internal_port}" in low


# ── Dockerfile templates ──────────────────────────────────────────────────────

_DOCKERFILES: dict[str, str] = {
    "nodejs": """\
FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --production
COPY . .
EXPOSE {port}
CMD {cmd_json}
""",
    "python": """\
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
CMD {cmd_json}
""",
    "ruby": """\
FROM ruby:3.2-slim
WORKDIR /app
COPY Gemfile* ./
RUN bundle install
COPY . .
EXPOSE {port}
CMD {cmd_json}
""",
    "go": """\
FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o app .

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/app .
EXPOSE {port}
CMD ["./app"]
""",
    "php": """\
FROM php:8.2-cli
WORKDIR /app
COPY . .
EXPOSE {port}
CMD {cmd_json}
""",
    "unknown": """\
FROM ubuntu:22.04
WORKDIR /app
RUN apt-get update && apt-get install -y curl wget && rm -rf /var/lib/apt/lists/*
COPY . .
EXPOSE {port}
CMD {cmd_json}
""",
}


def _cmd_to_json(cmd: str) -> str:
    """Convert a shell command string to a JSON array for Dockerfile CMD."""
    import shlex
    try:
        parts = shlex.split(cmd)
        return json.dumps(parts)
    except Exception:
        return json.dumps(["/bin/sh", "-c", cmd])


def _normalize_docker_start_command(app_type: str, start_command: str, port: int) -> str:
    """Best-effort normalization so containerized web apps bind on 0.0.0.0."""
    cmd = (start_command or "").strip()
    if not cmd:
        return cmd

    if app_type == "python":
        lower = cmd.lower()

        if "uvicorn" in lower:
            cmd = re.sub(r"--host(?:=|\s+)127\.0\.0\.1", "--host 0.0.0.0", cmd, flags=re.IGNORECASE)
            cmd = re.sub(r"--host(?:=|\s+)localhost", "--host 0.0.0.0", cmd, flags=re.IGNORECASE)
            if "--host" not in cmd:
                cmd += " --host 0.0.0.0"

        if "flask run" in lower and "--host" not in lower:
            cmd += " --host=0.0.0.0"

    return cmd


def generate_dockerfile(app_type: str, start_command: str, port: int) -> str:
    template = _DOCKERFILES.get(app_type, _DOCKERFILES["unknown"])
    port = port or 8000
    start_command = _normalize_docker_start_command(app_type, start_command, port)
    return template.format(
        port=port,
        cmd_json=_cmd_to_json(start_command),
    )


def ensure_dockerfile(app_dir: str, app_type: str, start_command: str, port: int) -> str:
    """Write a Dockerfile to app_dir if one doesn't exist. Returns the path."""
    dockerfile_path = os.path.join(app_dir, "Dockerfile")
    if not os.path.exists(dockerfile_path):
        content = generate_dockerfile(app_type, start_command, port)
        with open(dockerfile_path, "w") as f:
            f.write(content)
        log.info("[docker] Generated Dockerfile for %s app at %s", app_type, dockerfile_path)
    else:
        log.info("[docker] Using existing Dockerfile at %s", dockerfile_path)
    return dockerfile_path


# ── Build ─────────────────────────────────────────────────────────────────────

def build_image(
    app_id: int,
    app_name: str,
    app_dir: str,
    push_line_fn,
    app_type: str = "unknown",
    start_command: str = "",
    port: int = 8000,
) -> str:
    """Build Docker image, streaming build output via push_line_fn. Returns image tag."""
    build_lock = _get_build_lock(app_id)
    if not build_lock.acquire(blocking=False):
        msg = "Docker build already in progress for this app"
        push_line_fn(app_id, f"[Docker] {msg}.")
        raise RuntimeError(msg)

    img = image_name(app_id, app_name)
    try:
        ensure_dockerfile(app_dir, app_type, start_command, port)

        push_line_fn(app_id, f"[Docker] Building image {img} …")
        client = _get_client()
        # Use the low-level API here: it streams build output incrementally and
        # avoids the docker-py 7.x high-level decode issues around dict chunks.
        log_stream = client.api.build(
            path=app_dir,
            tag=img,
            rm=True,
            forcerm=True,
            decode=False,
        )
        for event in _iter_build_events(log_stream):
            _emit_build_event(app_id, event, push_line_fn)
    except RuntimeError:
        raise
    except Exception as e:
        push_line_fn(app_id, f"[Docker] Build failed: {e}")
        raise RuntimeError(str(e)) from e
    finally:
        build_lock.release()

    push_line_fn(app_id, f"[Docker] Image built: {img}")
    return img


def _restart_policy_config(policy: str | None) -> dict:
    policy = (policy or "no").strip().lower()
    if policy == "always":
        return {"Name": "always"}
    if policy == "on-failure":
        return {"Name": "on-failure", "MaximumRetryCount": 5}
    return {"Name": "no"}


# ── Port allocation ───────────────────────────────────────────────────────────

EXTERNAL_PORT_START = 8000
EXTERNAL_PORT_END   = 9999


def pick_free_external_port(used_ports: set[int]) -> int:
    """Return the lowest unused host port in [8000, 9999] that is also free on the OS."""
    import psutil
    try:
        active = {c.laddr.port for c in psutil.net_connections(kind="inet") if c.laddr}
    except Exception:
        active = set()

    for p in range(EXTERNAL_PORT_START, EXTERNAL_PORT_END + 1):
        if p not in used_ports and p not in active:
            return p
    raise RuntimeError(
        f"No free external port available in range {EXTERNAL_PORT_START}–{EXTERNAL_PORT_END}"
    )


# ── Run / start ───────────────────────────────────────────────────────────────

def run_container(
    app_id: int,
    app_name: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Create and start a container. Maps external_port → internal_port. Returns container ID."""
    client = _get_client()
    cname = container_name(app_id)
    docker_options = docker_options or {}

    # Remove any leftover container with the same name
    try:
        old = client.containers.get(cname)
        old.remove(force=True)
        push_line_fn(app_id, "[Docker] Removed old container.")
    except Exception:
        pass

    port_bindings = {}
    if internal_port and external_port:
        port_bindings[f"{internal_port}/tcp"] = external_port

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        "environment": env_vars,
        "restart_policy": _restart_policy_config(docker_options.get("restart_policy")),
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.app_name": app_name,
            "cloudbase.internal_port": str(internal_port),
            "cloudbase.external_port": str(external_port),
        },
    }

    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)

    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024

    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True

    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        tmpfs_size_mb = docker_options.get("tmpfs_size_mb")
        if tmpfs_size_mb:
            tmpfs_opts.append(f"size={int(tmpfs_size_mb)}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}

    _assert_image_local(client, img)
    container = client.containers.run(
        img,
        **run_kwargs,
    )
    push_line_fn(app_id, f"[Docker] Container started: {container.short_id} (:{external_port} → :{internal_port})")
    return container.id


# ── Stop / remove ─────────────────────────────────────────────────────────────

def stop_container(app_id: int, push_line_fn=None) -> bool:
    client = _get_client()
    cname = container_name(app_id)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
        if push_line_fn:
            push_line_fn(app_id, "[Docker] Container stopped and removed.")
        return True
    except Exception as e:
        if push_line_fn:
            push_line_fn(app_id, f"[Docker] Stop error: {e}")
        return False


def remove_image(app_id: int, app_name: str) -> None:
    client = _get_client()
    img = image_name(app_id, app_name)
    try:
        client.images.remove(img, force=True)
        log.info("[docker] Removed image %s", img)
    except Exception:
        pass


# ── Status ────────────────────────────────────────────────────────────────────

def is_container_running(app_id: int) -> bool:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        c.reload()
        return c.status == "running"
    except Exception:
        return False


def get_container_id(app_id: int) -> Optional[str]:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        return c.id
    except Exception:
        return None


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_container_stats(app_id: int) -> dict:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        raw = c.stats(stream=False)

        # CPU %
        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
        cpu_percent = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        # Memory — cgroups v2: usage may be 0; fall back to anon+file from stats dict
        mem_stats = raw.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_inner = mem_stats.get("stats", {})
        if mem_usage == 0 and mem_inner:
            # cgroups v2: anon = anonymous pages (true RSS), file = page cache
            rss = mem_inner.get("anon", 0)
            mem_usage = rss + mem_inner.get("file", 0)
            log.info("app_id=%d cgroups-v2 fallback: anon=%d file=%d rss=%d",
                     app_id, rss, mem_inner.get("file", 0), rss)
        else:
            mem_cache = mem_inner.get("inactive_file", mem_inner.get("cache", 0))
            rss = max(mem_usage - mem_cache, 0)

        c.reload()
        started_at = c.attrs.get("State", {}).get("StartedAt", "")
        uptime = 0
        if started_at:
            import datetime
            try:
                started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception:
                pass

        # Network I/O (cumulative since container start)
        networks = raw.get("networks") or {}
        net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
        net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

        # Disk I/O (cumulative block bytes)
        blkio = raw.get("blkio_stats", {})
        io_list = blkio.get("io_service_bytes_recursive") or []
        disk_read  = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "read")
        disk_write = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "write")

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(rss / 1024 / 1024, 2),
            "memory_vms_mb": round(mem_usage / 1024 / 1024, 2),
            "uptime_seconds": uptime,
            "status": c.status,
            "num_threads": 0,
            "num_connections": 0,
            "net_rx_mb": round(net_rx / 1024 / 1024, 2),
            "net_tx_mb": round(net_tx / 1024 / 1024, 2),
            "disk_read_mb": round(disk_read / 1024 / 1024, 2),
            "disk_write_mb": round(disk_write / 1024 / 1024, 2),
        }
    except Exception:
        log.warning("get_container_stats failed for app_id=%d", app_id, exc_info=True)
        return {}


# ── Log streaming ─────────────────────────────────────────────────────────────

def attach_container_log_tailer(
    app_id: int,
    log_buffers: dict,
    push_line_fn,
    main_loop,
) -> None:
    """Stream container logs to the in-memory buffer in a background thread."""
    if app_id not in log_buffers:
        log_buffers[app_id] = deque(maxlen=5000)

    def _reader():
        try:
            for _ in range(40):
                if is_container_running(app_id):
                    break
                time.sleep(0.25)

            client = _get_client()
            c = client.containers.get(container_name(app_id))
            labels = (c.attrs.get("Config", {}) or {}).get("Labels", {}) or {}
            raw_internal_port = labels.get("cloudbase.internal_port")
            internal_port = int(raw_internal_port) if str(raw_internal_port or "").isdigit() else None
            warned_loopback_bind = False
            for raw in c.logs(stream=True, follow=True, timestamps=False):
                line = raw.decode("utf-8", errors="replace").rstrip()
                log_buffers[app_id].append(line)
                if main_loop and not main_loop.is_closed():
                    main_loop.call_soon_threadsafe(
                        lambda l=line: None  # push_line_fn called below
                    )
                push_line_fn(app_id, line)
                if not warned_loopback_bind and _is_loopback_bind_log(line, internal_port):
                    warned_loopback_bind = True
                    target = f"port {internal_port}" if internal_port else "the exposed app port"
                    push_line_fn(
                        app_id,
                        f"[Docker] Warning: app appears to bind to localhost/127.0.0.1 inside the container for {target}. "
                        "Use host 0.0.0.0 so Docker port mapping and nginx can reach it.",
                    )
        except Exception as e:
            log.debug("[docker] Log tailer ended for app %d: %s", app_id, e)

    threading.Thread(target=_reader, daemon=True).start()


def get_recent_container_logs(app_id: int, lines: int = 300) -> list[str]:
    try:
        client = _get_client()
        c = client.containers.get(container_name(app_id))
        raw = c.logs(tail=lines, timestamps=False)
        return [l.decode("utf-8", errors="replace").rstrip() for l in raw.splitlines()]
    except Exception:
        return []


# ── Blue-green / zero-downtime helpers ───────────────────────────────────────

def slot_image_name(app_id: int, app_name: str, slot: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]", "-", app_name.lower())
    return f"cloudbase/{safe}-{app_id}:{slot}"


def slot_container_name(app_id: int, slot: str) -> str:
    return f"cloudbase-app-{app_id}-{slot}"


def build_image_for_slot(
    app_id: int,
    app_name: str,
    app_dir: str,
    push_line_fn,
    app_type: str,
    start_command: str,
    port: int,
    slot: str,
) -> str:
    """Build image tagged with the given slot name. Returns image tag."""
    build_lock = _get_build_lock(app_id)
    if not build_lock.acquire(blocking=False):
        raise RuntimeError("Docker build already in progress for this app")
    img = slot_image_name(app_id, app_name, slot)
    try:
        ensure_dockerfile(app_dir, app_type, start_command, port)
        push_line_fn(app_id, f"[ZD] Building image {img} for slot {slot}…")
        client = _get_client()
        log_stream = client.api.build(path=app_dir, tag=img, rm=True, forcerm=True, decode=False)
        for event in _iter_build_events(log_stream):
            _emit_build_event(app_id, event, push_line_fn)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(str(e)) from e
    finally:
        build_lock.release()
    push_line_fn(app_id, f"[ZD] Image built: {img}")
    return img


def run_container_for_slot(
    app_id: int,
    app_name: str,
    slot: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Start a slot container on a temporary port. Returns container ID."""
    client = _get_client()
    cname = slot_container_name(app_id, slot)
    docker_options = docker_options or {}
    try:
        old = client.containers.get(cname)
        old.remove(force=True)
    except Exception:
        pass
    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": {f"{internal_port}/tcp": external_port},
        "environment": env_vars,
        "restart_policy": {"Name": "no"},
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.app_name": app_name,
            "cloudbase.slot": slot,
        },
    }
    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)
    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024
    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True
    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        if docker_options.get("tmpfs_size_mb"):
            tmpfs_opts.append(f"size={int(docker_options['tmpfs_size_mb'])}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}
    _assert_image_local(client, img)
    container = client.containers.run(img, **run_kwargs)
    push_line_fn(app_id, f"[ZD] Slot container started: {container.short_id} on :{external_port}")
    return container.id


def stop_slot_container(app_id: int, slot: str) -> None:
    client = _get_client()
    cname = slot_container_name(app_id, slot)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
    except Exception:
        pass


# ── Pull (rebuild on git pull) ────────────────────────────────────────────────

def rebuild_image(
    app_id: int,
    app_name: str,
    app_dir: str,
    push_line_fn,
    app_type: str = "unknown",
    start_command: str = "",
    port: int = 8000,
) -> str:
    """Remove old image and build fresh. Returns new image tag."""
    remove_image(app_id, app_name)
    return build_image(app_id, app_name, app_dir, push_line_fn, app_type, start_command, port)


# ── Replica helpers ───────────────────────────────────────────────────────────

def replica_container_name(app_id: int, replica_id: int) -> str:
    return f"cloudbase-app-{app_id}-replica-{replica_id}"


def run_replica_container(
    app_id: int,
    replica_id: int,
    app_name: str,
    img: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None,
    push_line_fn,
) -> str:
    """Start a replica container. Returns container ID."""
    client = _get_client()
    cname = replica_container_name(app_id, replica_id)
    docker_options = docker_options or {}
    try:
        old = client.containers.get(cname)
        old.remove(force=True)
        push_line_fn(app_id, f"[Replica] Removed old container for replica {replica_id}.")
    except Exception:
        pass

    port_bindings = {}
    if internal_port and external_port:
        port_bindings[f"{internal_port}/tcp"] = external_port

    run_kwargs = {
        "detach": True,
        "name": cname,
        "ports": port_bindings,
        "environment": env_vars,
        "restart_policy": _restart_policy_config(docker_options.get("restart_policy")),
        "labels": {
            "cloudbase.app_id": str(app_id),
            "cloudbase.replica_id": str(replica_id),
            "cloudbase.app_name": app_name,
            "cloudbase.internal_port": str(internal_port),
            "cloudbase.external_port": str(external_port),
        },
    }

    cpu_limit = docker_options.get("cpu_limit")
    if cpu_limit:
        run_kwargs["nano_cpus"] = int(float(cpu_limit) * 1_000_000_000)

    memory_limit_mb = docker_options.get("memory_limit_mb")
    if memory_limit_mb:
        run_kwargs["mem_limit"] = int(memory_limit_mb) * 1024 * 1024

    if docker_options.get("read_only_root"):
        run_kwargs["read_only"] = True

    if docker_options.get("tmpfs_enabled"):
        tmpfs_opts = ["rw", "nosuid", "nodev", "noexec"]
        tmpfs_size_mb = docker_options.get("tmpfs_size_mb")
        if tmpfs_size_mb:
            tmpfs_opts.append(f"size={int(tmpfs_size_mb)}m")
        run_kwargs["tmpfs"] = {"/tmp": ",".join(tmpfs_opts)}

    _assert_image_local(client, img)
    container = client.containers.run(img, **run_kwargs)
    push_line_fn(app_id, f"[Replica] Container {replica_id} started: {container.short_id} (:{external_port} → :{internal_port})")
    return container.id


def stop_replica_container(app_id: int, replica_id: int, push_line_fn=None) -> bool:
    client = _get_client()
    cname = replica_container_name(app_id, replica_id)
    try:
        c = client.containers.get(cname)
        c.stop(timeout=10)
        c.remove()
        if push_line_fn:
            push_line_fn(app_id, f"[Replica] Container {replica_id} stopped and removed.")
        return True
    except Exception as e:
        if push_line_fn:
            push_line_fn(app_id, f"[Replica] Stop error for replica {replica_id}: {e}")
        return False


def is_replica_container_running(app_id: int, replica_id: int) -> bool:
    try:
        client = _get_client()
        c = client.containers.get(replica_container_name(app_id, replica_id))
        c.reload()
        return c.status == "running"
    except Exception:
        return False


def get_container_stats_by_name(container_name_str: str) -> dict:
    """Like get_container_stats but accepts an explicit container name instead of app_id."""
    try:
        client = _get_client()
        c = client.containers.get(container_name_str)
        raw = c.stats(stream=False)

        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
        cpu_percent = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        mem_stats = raw.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_inner = mem_stats.get("stats", {})
        if mem_usage == 0 and mem_inner:
            rss = mem_inner.get("anon", 0)
            mem_usage = rss + mem_inner.get("file", 0)
        else:
            mem_cache = mem_inner.get("inactive_file", mem_inner.get("cache", 0))
            rss = max(mem_usage - mem_cache, 0)

        c.reload()
        started_at = c.attrs.get("State", {}).get("StartedAt", "")
        uptime = 0
        if started_at:
            import datetime
            try:
                started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception:
                pass

        networks = raw.get("networks") or {}
        net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
        net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

        blkio = raw.get("blkio_stats", {})
        io_list = blkio.get("io_service_bytes_recursive") or []
        disk_read  = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "read")
        disk_write = sum(e.get("value", 0) for e in io_list if e.get("op", "").lower() == "write")

        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(rss / 1024 / 1024, 2),
            "memory_vms_mb": round(mem_usage / 1024 / 1024, 2),
            "uptime_seconds": uptime,
            "status": c.status,
            "num_threads": 0,
            "num_connections": 0,
            "net_rx_mb": round(net_rx / 1024 / 1024, 2),
            "net_tx_mb": round(net_tx / 1024 / 1024, 2),
            "disk_read_mb": round(disk_read / 1024 / 1024, 2),
            "disk_write_mb": round(disk_write / 1024 / 1024, 2),
        }
    except Exception:
        log.warning("get_container_stats_by_name failed for %s", container_name_str, exc_info=True)
        return {}
