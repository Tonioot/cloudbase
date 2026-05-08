import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import psutil
from collections import deque
from typing import Optional

import docker_manager as dm

APPS_BASE_DIR = os.path.expanduser("~/.cloudbase/apps")
REGISTRY_PATH = os.path.expanduser("~/.cloudbase/pid_registry.json")
DEBUG_LOG_PATH = os.path.expanduser("~/.cloudbase/cloudbase-debug.log")
os.makedirs(APPS_BASE_DIR, exist_ok=True)

# Recent lines for history (capped, no tracking issues)
log_buffers: dict[int, deque] = {}

# Real-time subscribers: app_id -> list of asyncio.Queue
_log_queues: dict[int, list[asyncio.Queue]] = {}
_queues_lock = threading.Lock()

# Main event loop — set once at startup
_main_loop: Optional[asyncio.AbstractEventLoop] = None

running_processes: dict[int, subprocess.Popen] = {}

# Persistent PID registry: {app_id: {pid, shell_pid, create_time}}
# Survives PDManager restarts so we can recover orphaned processes
_pid_registry: dict[int, dict] = {}

# Stats history: last 60 snapshots per app (~2 min at 2s interval)
_stats_history: dict[int, deque] = {}
_stats_queues: dict[int, list[asyncio.Queue]] = {}
_stats_queues_lock = threading.Lock()

# Latest stats snapshot per replica_id (for the instances table)
_replica_stats: dict[int, dict] = {}


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def subscribe_stats(app_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _stats_queues_lock:
        _stats_queues.setdefault(app_id, []).append(q)
    return q


def unsubscribe_stats(app_id: int, q: asyncio.Queue) -> None:
    with _stats_queues_lock:
        queues = _stats_queues.get(app_id, [])
        try:
            queues.remove(q)
        except ValueError:
            pass


def _push_stat(app_id: int, data: dict) -> None:
    if _main_loop is None or _main_loop.is_closed():
        return
    with _stats_queues_lock:
        queues = list(_stats_queues.get(app_id, []))
    for q in queues:
        _main_loop.call_soon_threadsafe(q.put_nowait, data)


def get_recent_stats(app_id: int) -> list[dict]:
    return list(_stats_history.get(app_id, []))


def set_replica_stats(replica_id: int, data: dict) -> None:
    _replica_stats[replica_id] = data


def get_replica_stats(replica_id: int) -> dict | None:
    return _replica_stats.get(replica_id)


def get_all_replica_stats(app_id: int, replica_ids: list[int]) -> dict[int, dict]:
    return {rid: _replica_stats[rid] for rid in replica_ids if rid in _replica_stats}


def load_registry() -> None:
    global _pid_registry
    try:
        with open(REGISTRY_PATH) as f:
            _pid_registry = {int(k): v for k, v in json.load(f).items()}
    except Exception:
        _pid_registry = {}


def _save_registry() -> None:
    try:
        with open(REGISTRY_PATH, "w") as f:
            json.dump(_pid_registry, f)
    except Exception:
        pass


def subscribe_logs(app_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _queues_lock:
        _log_queues.setdefault(app_id, []).append(q)
    return q


def unsubscribe_logs(app_id: int, q: asyncio.Queue) -> None:
    with _queues_lock:
        queues = _log_queues.get(app_id, [])
        try:
            queues.remove(q)
        except ValueError:
            pass


def _push_line(app_id: int, line: str) -> None:
    if _main_loop is None or _main_loop.is_closed():
        return
    with _queues_lock:
        queues = list(_log_queues.get(app_id, []))
    for q in queues:
        _main_loop.call_soon_threadsafe(q.put_nowait, line)


def _safe_dir_name(name: str) -> str:
    """Strip/replace characters that are invalid or problematic in file paths."""
    import re
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name)


def get_app_dir(app_name: str) -> str:
    return os.path.join(APPS_BASE_DIR, _safe_dir_name(app_name))


def detect_app_type_from_command(cmd: str) -> str:
    """Infer app type from the start command."""
    cmd = cmd.strip().lower()
    if cmd.startswith("node ") or "npm " in cmd or cmd == "npm start" or cmd.startswith("npx "):
        return "nodejs"
    if cmd.startswith("python") or cmd.startswith("python3") or cmd.startswith("uvicorn") or cmd.startswith("gunicorn") or cmd.startswith("flask"):
        return "python"
    if cmd.startswith("ruby") or cmd.startswith("bundle exec ruby") or cmd.startswith("rails"):
        return "ruby"
    if cmd.startswith("go run") or cmd.startswith("go build") or cmd.startswith("./ "):
        return "go"
    if cmd.startswith("php") or cmd.startswith("composer"):
        return "php"
    if cmd.startswith("java") or cmd.startswith("mvn") or cmd.startswith("gradle"):
        return "java"
    if cmd.startswith("dotnet") or cmd.endswith(".exe"):
        return "dotnet"
    return "unknown"


def detect_app_type(app_dir: str) -> tuple[str, str, Optional[int]]:
    if os.path.exists(os.path.join(app_dir, "package.json")):
        import json as _json
        pkg = _json.load(open(os.path.join(app_dir, "package.json")))
        scripts = pkg.get("scripts", {})
        if "start" in scripts:
            cmd = "npm start"
        elif os.path.exists(os.path.join(app_dir, "index.js")):
            cmd = "node index.js"
        elif os.path.exists(os.path.join(app_dir, "server.js")):
            cmd = "node server.js"
        elif os.path.exists(os.path.join(app_dir, "app.js")):
            cmd = "node app.js"
        else:
            cmd = "npm start"
        return "nodejs", cmd, 3000

    if os.path.exists(os.path.join(app_dir, "requirements.txt")):
        for entry in ["main.py", "app.py", "server.py", "run.py", "wsgi.py"]:
            if os.path.exists(os.path.join(app_dir, entry)):
                name = entry.replace(".py", "")
                try:
                    content = open(os.path.join(app_dir, entry)).read()
                    if any(kw in content for kw in ["FastAPI", "Flask", "Starlette"]):
                        return "python", f"python3 -m uvicorn {name}:app --host 0.0.0.0 --port 8000", 8000
                except Exception:
                    pass
                return "python", f"python3 {entry}", None
        return "python", "python3 main.py", None

    if os.path.exists(os.path.join(app_dir, "Gemfile")):
        return "ruby", "bundle exec ruby app.rb", 4567

    if os.path.exists(os.path.join(app_dir, "go.mod")):
        return "go", "go run .", None

    if os.path.exists(os.path.join(app_dir, "composer.json")):
        return "php", "php -S 0.0.0.0:8080", 8080

    return "unknown", "", None


def _pid_alive(pid: int, expected_create_time: Optional[float] = None) -> bool:
    """Check if a PID is alive, optionally verifying it's the same process."""
    try:
        proc = psutil.Process(pid)
        if not (proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE):
            return False
        if expected_create_time is not None:
            if abs(proc.create_time() - expected_create_time) > 2.0:
                return False  # PID was reused by a different process
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def prepare_app_env(start_command: str, working_dir: Optional[str], custom_env: Optional[dict] = None) -> tuple[str, dict]:
    """
    Sets up the environment for an app, including virtualenv and node_modules/.bin in PATH.
    Returns (resolved_command, env_dict).
    """
    env = os.environ.copy()
    if custom_env:
        env.update(custom_env)
    
    final_command = start_command
    if working_dir:
        # Detect venv directory based on OS (Scripts on Windows, bin on Unix)
        venv_bin = os.path.join(working_dir, "venv", "Scripts" if os.name == "nt" else "bin")
        if os.path.exists(venv_bin):
            # Add venv/bin to PATH so 'python3', 'uvicorn', etc. are found there first
            env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
            # Also handle common prefixes directly for extra robustness
            py_name = "python.exe" if os.name == "nt" else "python3"
            if final_command.startswith("python3 "):
                final_command = final_command.replace("python3 ", f'"{os.path.join(venv_bin, py_name)}" ', 1)
            elif final_command.startswith("python "):
                final_command = final_command.replace("python ", f'"{os.path.join(venv_bin, py_name)}" ', 1)
        
        # Node.js support: automatically add node_modules/.bin to PATH
        node_bin = os.path.join(working_dir, "node_modules", ".bin")
        if os.path.exists(node_bin):
            existing_path = env.get("PATH", "")
            env["PATH"] = f"{node_bin}{os.pathsep}{existing_path}"

    return final_command, env


def is_process_running(pid: int, app_id: Optional[int] = None) -> bool:
    reg = _pid_registry.get(app_id) if app_id is not None else None
    create_time = reg.get("create_time") if reg else None

    if _pid_alive(pid, create_time):
        return True

    # Fallback: if we know the shell PID, check if its children are alive
    if reg:
        shell_pid = reg.get("shell_pid")
        if shell_pid and shell_pid != pid:
            try:
                children = psutil.Process(shell_pid).children(recursive=True)
                if any(c.is_running() for c in children):
                    return True
            except Exception:
                pass

    return False


def find_process_by_port(port: int) -> Optional[int]:
    """Find PID of process listening on a given port (port-based recovery)."""
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception:
        pass
    return None


def get_process_stats(pid: int) -> dict:
    try:
        proc = psutil.Process(pid)
        # interval=None is non-blocking: returns delta since last call (or 0.0 on first call).
        # The background stats loop calls this periodically, so values stay fresh without blocking.
        cpu = proc.cpu_percent(interval=None)
        mem = proc.memory_info()
        uptime = int(time.time() - proc.create_time())

        try:
            num_threads = proc.num_threads()
        except Exception:
            num_threads = 0

        try:
            conns = proc.connections() if hasattr(proc, 'connections') else proc.net_connections()
            num_connections = len(conns)
        except Exception:
            num_connections = 0

        try:
            io = proc.io_counters()
            disk_read_mb  = round(io.read_bytes  / 1024 / 1024, 2)
            disk_write_mb = round(io.write_bytes / 1024 / 1024, 2)
        except Exception:
            disk_read_mb  = 0
            disk_write_mb = 0

        return {
            "cpu_percent": cpu,
            "memory_mb": round(mem.rss / 1024 / 1024, 2),
            "memory_vms_mb": round(mem.vms / 1024 / 1024, 2),
            "uptime_seconds": uptime,
            "status": proc.status(),
            "num_threads": num_threads,
            "num_connections": num_connections,
            "disk_read_mb": disk_read_mb,
            "disk_write_mb": disk_write_mb,
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}


def get_log_path(app_name: str) -> str:
    return os.path.join(os.path.expanduser("~/.cloudbase/logs"), f"{_safe_dir_name(app_name)}.log")


def attach_log_tailer(
    app_id: int,
    app_name: str,
    proc: Optional[subprocess.Popen] = None,
    seek_to_end: bool = False,
) -> None:
    """Tail a log file and push new lines to log_buffers / subscribers.

    Uses the log file directly so the child process is not coupled to
    pdmanager via a pipe — pdmanager restarts no longer send SIGPIPE to apps.
    """
    log_path = get_log_path(app_name)
    # Ensure the buffer exists before the thread starts appending to it
    if app_id not in log_buffers:
        log_buffers[app_id] = deque(maxlen=5000)

    def _reader():
        try:
            # Wait briefly if the file hasn't been created yet (fast start)
            for _ in range(20):
                if os.path.exists(log_path):
                    break
                time.sleep(0.05)
            else:
                return

            with open(log_path, "r") as f:
                if seek_to_end:
                    f.seek(0, 2)
                while True:
                    raw_line = f.readline()
                    if raw_line:
                        line = raw_line.rstrip()
                        log_buffers[app_id].append(line)
                        _push_line(app_id, line)
                    else:
                        # Determine whether the process is still alive
                        if proc is not None:
                            if proc.poll() is not None:
                                # Read any last bytes the OS may have buffered
                                for raw in f:
                                    l = raw.rstrip()
                                    log_buffers[app_id].append(l)
                                    _push_line(app_id, l)
                                break
                        else:
                            reg = _pid_registry.get(app_id)
                            if not reg:
                                break
                            pid = reg.get("pid")
                            ct  = reg.get("create_time")
                            if pid and not _pid_alive(pid, ct):
                                for raw in f:
                                    l = raw.rstrip()
                                    log_buffers[app_id].append(l)
                                    _push_line(app_id, l)
                                break
                        time.sleep(0.05)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()


def _systemd_run() -> str | None:
    """Return path to systemd-run if available, else None."""
    return shutil.which("systemd-run")


def _debug(msg: str) -> None:
    """Append a timestamped line to the PDManager debug log."""
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG_PATH), exist_ok=True)
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def start_docker_app(
    app_id: int,
    app_name: str,
    app_dir: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    app_type: str,
    start_command: str,
    docker_options: dict | None = None,
    build: bool = True,
) -> str:
    """Build (if needed) and start a Docker container. Returns container ID."""
    log_buffers[app_id] = deque(maxlen=5000)
    _stats_history.pop(app_id, None)

    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    try:
        if build:
            img = dm.build_image(app_id, app_name, app_dir, _push, app_type, start_command, internal_port)
        else:
            img = dm.image_name(app_id, app_name)

        container_id = dm.run_container(
            app_id, app_name, img, internal_port, external_port, env_vars or {}, docker_options, _push
        )
    except Exception as e:
        message = str(e) or "Unknown Docker error"
        _push(app_id, f"[Docker] Start failed: {message}")
        _debug(f"Docker app {app_id} failed to start: {message}")
        raise RuntimeError(message) from e

    if _main_loop is not None and not _main_loop.is_closed():
        dm.attach_container_log_tailer(app_id, log_buffers, _push_line, _main_loop)

    _debug(f"Docker app {app_id} started, container_id={container_id[:12]}, :{external_port}→:{internal_port}")
    return container_id


def stop_docker_app(app_id: int) -> bool:
    """Stop and remove a Docker container."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    ok = dm.stop_container(app_id, push_line_fn=_push)
    _debug(f"Docker app {app_id} stopped, ok={ok}")
    return ok


def start_docker_replica(
    app_id: int,
    replica_id: int,
    app_name: str,
    internal_port: int,
    external_port: int,
    env_vars: dict,
    docker_options: dict | None = None,
    image_app_id: int | None = None,
) -> str:
    """Start a replica container using the existing image (no rebuild). Returns container ID.
    image_app_id: if provided, use this app id for the Docker image name instead of app_id.
    Useful when the image was built under a different (local) app id on remote nodes."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    img = dm.image_name(image_app_id if image_app_id is not None else app_id, app_name)
    try:
        container_id = dm.run_replica_container(
            app_id, replica_id, app_name, img,
            internal_port, external_port, env_vars or {}, docker_options, _push,
        )
    except Exception as e:
        message = str(e) or "Unknown Docker error"
        _push(app_id, f"[Replica] Start failed for replica {replica_id}: {message}")
        raise RuntimeError(message) from e

    if _main_loop is not None and not _main_loop.is_closed():
        dm.attach_container_log_tailer(
            app_id, log_buffers, _push_line, _main_loop,
            cname=dm.replica_container_name(app_id, replica_id),
        )

    _debug(f"Replica {replica_id} for app {app_id} started, container_id={container_id[:12]}")
    return container_id


def stop_docker_replica(app_id: int, replica_id: int) -> bool:
    """Stop and remove a replica container."""
    def _push(aid, line):
        log_buffers.setdefault(aid, deque(maxlen=5000)).append(str(line))
        _push_line(aid, line)

    ok = dm.stop_replica_container(app_id, replica_id, push_line_fn=_push)
    _debug(f"Replica {replica_id} for app {app_id} stopped, ok={ok}")
    return ok


def is_docker_app_running(app_id: int) -> bool:
    return dm.is_container_running(app_id)


def get_docker_stats(app_id: int) -> dict:
    return dm.get_container_stats(app_id)


def get_recent_docker_logs(app_id: int, lines: int = 300) -> list[str]:
    buf = log_buffers.get(app_id)
    if buf:
        return list(buf)[-lines:]
    return dm.get_recent_container_logs(app_id, lines)


def start_app(app_id: int, app_name: str, command: str, working_dir: str, env_vars: dict = None) -> int:
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    log_buffers[app_id] = deque(maxlen=5000)
    _stats_history.pop(app_id, None)  # fresh process = fresh history
    log_path = get_log_path(app_name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # Open for writing; pass the fd directly so the child owns it.
    # No pipe means pdmanager restarts never send SIGPIPE to the child.
    log_file = open(log_path, "w")

    systemd_run = _systemd_run()
    use_systemd = False
    if systemd_run:
        # Quick probe: can we actually use systemd-run --user?
        try:
            result = subprocess.run(
                [systemd_run, "--user", "--scope", "--collect", "--", "/bin/true"],
                capture_output=True, timeout=5,
            )
            use_systemd = result.returncode == 0
            _debug(f"systemd-run probe for app {app_id}: returncode={result.returncode} use_systemd={use_systemd}")
            if not use_systemd:
                _debug(f"  stderr: {result.stderr.decode(errors='replace').strip()}")
        except Exception as e:
            _debug(f"systemd-run probe failed for app {app_id}: {e}")
            use_systemd = False
    else:
        _debug(f"systemd-run not found on PATH for app {app_id} — using start_new_session fallback")

    if use_systemd:
        # Wrap in a transient user-scope unit so the app lives in its own
        # cgroup and is NOT killed when the PDManager service stops.
        # --user     : use the user's own systemd manager (no root/polkit needed)
        # --scope    : transient scope, not a service
        # --collect  : auto-remove unit after exit
        unit_name = f"pdm-app-{app_id}"
        
        setsid_bin = shutil.which("setsid")
        final_shell_cmd = ["/bin/sh", "-c", command]
        if setsid_bin and os.name != 'nt':
             final_shell_cmd = [setsid_bin] + final_shell_cmd

        cmd = [
            systemd_run,
            "--user",
            "--scope",
            "--collect",
            f"--unit={unit_name}",
            "--",
        ] + final_shell_cmd

        creationflags = 0
        if os.name == 'nt':
            creationflags = 0x00000008

        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            creationflags=creationflags,
        )
        _debug(f"app {app_id} started via systemd-run --user --scope, shell_pid={proc.pid}")
    else:
        # Fallback: no systemd-run — start_new_session + setsid prevents SIGHUP/termination
        creationflags = 0
        final_cmd = command
        if os.name == 'nt':
            creationflags = 0x00000008
        else:
            setsid_bin = shutil.which("setsid")
            if setsid_bin:
                final_cmd = f"setsid {command}"

        proc = subprocess.Popen(
            final_cmd,
            shell=True,
            cwd=working_dir,
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            creationflags=creationflags,
        )
        _debug(f"app {app_id} started via start_new_session fallback, shell_pid={proc.pid}")
    # Parent no longer needs the fd; child has its own copy
    log_file.close()

    running_processes[app_id] = proc
    shell_pid = proc.pid

    # Find the actual app PID (child of shell) after a brief moment
    actual_pid = shell_pid
    try:
        time.sleep(0.25)
        children = psutil.Process(shell_pid).children(recursive=True)
        if children:
            actual_pid = children[-1].pid
    except Exception:
        pass

    # Persist to registry for recovery after PDManager restarts
    try:
        create_time = psutil.Process(actual_pid).create_time()
        _pid_registry[app_id] = {
            "pid": actual_pid,
            "shell_pid": shell_pid,
            "create_time": create_time,
        }
        _save_registry()
        _debug(f"app {app_id} registry saved: pid={actual_pid} shell_pid={shell_pid} create_time={create_time:.2f}")
    except Exception as e:
        _debug(f"app {app_id} registry save error: {e}")
        _pid_registry[app_id] = {"pid": actual_pid, "shell_pid": shell_pid}
        _save_registry()

    attach_log_tailer(app_id, app_name, proc=proc, seek_to_end=False)
    return actual_pid


def stop_app(app_id: int, pid: int) -> bool:
    proc = running_processes.pop(app_id, None)
    reg  = _pid_registry.pop(app_id, {})
    _save_registry()

    killed = False

    # Kill entire process group (shell + all children)
    shell_pid = reg.get("shell_pid") or (proc.pid if proc else None)
    if shell_pid:
        if os.name == 'nt':
            # Windows: use taskkill to kill the tree (/T) forcefully (/F)
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(shell_pid)], capture_output=True, check=False)
                killed = True
            except Exception:
                pass
        else:
            try:
                os.killpg(os.getpgid(shell_pid), signal.SIGTERM)
                killed = True
            except Exception:
                pass

    # Also terminate via Popen object
    if proc:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            killed = True
        except Exception:
            pass

    # Kill by actual PID and its children
    for target_pid in {pid, reg.get("pid")} - {None}:
        if target_pid and _pid_alive(target_pid):
            try:
                parent = psutil.Process(target_pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                parent.terminate()
                killed = True
            except Exception:
                pass

    return killed


def get_recent_logs(app_id: int, app_name: str, lines: int = 300) -> list[str]:
    buf = log_buffers.get(app_id)
    if buf:
        return list(buf)[-lines:]

    log_path = os.path.join(os.path.expanduser("~/.cloudbase/logs"), f"{_safe_dir_name(app_name)}.log")
    if os.path.exists(log_path):
        with open(log_path) as f:
            all_lines = f.readlines()
        return [l.rstrip() for l in all_lines[-lines:]]
    return []
