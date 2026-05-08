"""Load and expose Cloudbase configuration from config.yaml."""
import os
import yaml

_REPO_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
_USER_CONFIG_PATH = os.path.expanduser("~/.cloudbase/config.yaml")

# User config takes precedence over the repo default; if it doesn't exist yet
# we copy the repo config there on first load so it survives updates.
def _resolve_config_path() -> str:
    if os.path.exists(_USER_CONFIG_PATH):
        return _USER_CONFIG_PATH
    # Bootstrap: copy repo config to user directory so future saves go there.
    import shutil
    os.makedirs(os.path.dirname(_USER_CONFIG_PATH), exist_ok=True)
    if os.path.exists(_REPO_CONFIG_PATH):
        shutil.copy2(_REPO_CONFIG_PATH, _USER_CONFIG_PATH)
    return _USER_CONFIG_PATH

_CONFIG_PATH = _resolve_config_path()

_DEFAULTS = {
    "server": {
        "port": 7823,
    },
    "auth": {
        "token_expire_seconds": 3600,
    },
    "ports": {
        "instance_min": 8000,
        "instance_max": 17999,
        "tunnel_min": 18000,
        "tunnel_max": 27999,
    },
    "limits": {
        "max_apps": 1000,
        "max_instances": 10000,
        "max_nodes": 100,
        "max_restarts_per_window": 5,
        "restart_window_seconds": 60,
    },
}


def _load() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Merge with defaults so missing keys always have a value
        result = {section: dict(values) for section, values in _DEFAULTS.items()}
        for section, values in data.items():
            if isinstance(values, dict) and section in result:
                result[section] = {**result[section], **values}
            else:
                result[section] = values
        return result
    except FileNotFoundError:
        return {section: dict(values) for section, values in _DEFAULTS.items()}


_config = _load()


def get_auth(key: str):
    """Return an auth value from the config, e.g. get_auth('token_expire_seconds')."""
    default = _DEFAULTS["auth"].get(key)
    val = _config.get("auth", {}).get(key, default)
    if isinstance(default, int):
        return int(val)
    return val


def get_limit(key: str) -> int:
    """Return a limit value from the config, e.g. get_limit('max_apps')."""
    return int(_config.get("limits", {}).get(key, _DEFAULTS["limits"].get(key, 0)))


def get_port(key: str) -> int:
    """Return a port/range value from the config, e.g. get_port('instance_min')."""
    return int(_config.get("ports", {}).get(key, _DEFAULTS["ports"].get(key, 0)))


def get_server_port() -> int:
    """Return the main Cloudbase server port."""
    return int(_config.get("server", {}).get("port", _DEFAULTS["server"]["port"]))


def get_base_domain() -> str:
    """Return the base domain for auto-subdomains, or empty string if not configured."""
    return str(_config.get("server", {}).get("base_domain", "")).strip()


def get_base_ssl_cert() -> str:
    """Return the wildcard SSL cert path for app auto-subdomains, or ''."""
    return str(_config.get("server", {}).get("base_ssl_cert", "")).strip()


def get_base_ssl_key() -> str:
    """Return the wildcard SSL key path for app auto-subdomains, or ''."""
    return str(_config.get("server", {}).get("base_ssl_key", "")).strip()


def save_server_config(fields: dict) -> None:
    """Update one or more fields under server: in config.yaml and reload."""
    global _config
    try:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        data.setdefault("server", {}).update(fields)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        raise RuntimeError(f"Failed to update config.yaml: {e}")
    _config = _load()


def save_config_sections(sections: dict) -> None:
    """Update arbitrary config sections and reload. E.g. {'limits': {'max_apps': 500}}."""
    global _config
    try:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        for section, values in sections.items():
            if isinstance(values, dict):
                data.setdefault(section, {}).update(values)
            else:
                data[section] = values
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        raise RuntimeError(f"Failed to update config.yaml: {e}")
    _config = _load()


def set_base_domain(value: str) -> None:
    """Update base_domain in config.yaml and reload the in-memory config."""
    save_server_config({"base_domain": (value or "").strip()})


def validate() -> None:
    """Log warnings for any configuration problems detected at startup."""
    import logging
    log = logging.getLogger("cloudbase.config")

    inst_min = get_port("instance_min")
    inst_max = get_port("instance_max")
    tun_min  = get_port("tunnel_min")
    tun_max  = get_port("tunnel_max")
    max_inst = get_limit("max_instances")
    max_apps = get_limit("max_apps")

    inst_range = inst_max - inst_min + 1
    tun_range  = tun_max  - tun_min  + 1

    if inst_range < max_inst:
        log.warning(
            "CONFIG WARNING: instance port range %d–%d has only %d ports, "
            "but limits.max_instances is %d. "
            "Increase instance_max or reduce max_instances in config.yaml.",
            inst_min, inst_max, inst_range, max_inst,
        )
    elif inst_range < max_apps:
        log.warning(
            "CONFIG WARNING: instance port range %d–%d has only %d ports, "
            "but limits.max_apps is %d. Each app needs at least one port. "
            "Consider increasing instance_max in config.yaml.",
            inst_min, inst_max, inst_range, max_apps,
        )

    if tun_range < max_inst:
        log.warning(
            "CONFIG WARNING: tunnel port range %d–%d has only %d ports, "
            "but limits.max_instances is %d. "
            "Increase tunnel_max or reduce max_instances in config.yaml.",
            tun_min, tun_max, tun_range, max_inst,
        )

    # Overlap check
    inst_set = range(inst_min, inst_max + 1)
    tun_set  = range(tun_min,  tun_max  + 1)
    if inst_min <= tun_max and tun_min <= inst_max:
        log.warning(
            "CONFIG WARNING: instance port range %d–%d overlaps with tunnel port range %d–%d. "
            "This will cause port conflicts. Adjust the ranges in config.yaml so they do not overlap.",
            inst_min, inst_max, tun_min, tun_max,
        )

    server_port = get_server_port()
    if inst_min <= server_port <= inst_max or tun_min <= server_port <= tun_max:
        log.warning(
            "CONFIG WARNING: server port %d falls inside a port range "
            "(instance %d–%d, tunnel %d–%d). "
            "Change server.port in config.yaml to avoid conflicts.",
            server_port, inst_min, inst_max, tun_min, tun_max,
        )
