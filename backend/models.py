from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, Float
from database import Base


class User(Base):
    __tablename__ = "users"

    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role         = Column(String(10), nullable=False, default="viewer")  # "admin" or "viewer"
    created_at   = Column(DateTime, default=datetime.utcnow)


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    repo_url = Column(String(500), nullable=False)
    github_token = Column(String(200), nullable=True)
    domain = Column(String(200), nullable=True)
    extra_domains = Column(Text, nullable=True)     # JSON list of additional domains/subdomains
    redirect_domains = Column(Text, nullable=True)  # JSON list of domains that redirect to primary
    ssl_cert_path = Column(String(500), nullable=True)
    ssl_key_path = Column(String(500), nullable=True)
    app_type = Column(String(50), nullable=True)
    start_command = Column(String(500), nullable=True)
    port          = Column(Integer, nullable=True)   # internal port (inside container)
    external_port = Column(Integer, nullable=True)   # host port (auto-assigned 8000–8999)
    status = Column(String(20), default="stopped")
    pid = Column(Integer, nullable=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=True, index=True)
    working_dir = Column(String(500), nullable=True)
    last_error = Column(Text, nullable=True)
    env_vars = Column(Text, nullable=True)
    nginx_enabled  = Column(Boolean, default=False)
    auto_start     = Column(Boolean, default=False)
    restart_policy = Column(String(20), default="no")   # no | always | on-failure
    use_docker     = Column(Boolean, default=True)
    docker_image   = Column(String(500), nullable=True)
    source_revision = Column(String(120), nullable=True)
    image_revision  = Column(String(120), nullable=True)
    docker_cpu_limit = Column(Float, nullable=True)
    docker_memory_limit_mb = Column(Integer, nullable=True)
    docker_read_only_root = Column(Boolean, default=False)
    docker_tmpfs_enabled = Column(Boolean, default=False)
    docker_tmpfs_size_mb = Column(Integer, nullable=True)
    maintenance_mode = Column(Boolean, default=False)
    update_mode      = Column(Boolean, default=False)
    downtime_page    = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    update_page      = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    restart_page     = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    starting_page    = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False)
    role = Column(String(20), default="node")  # main | node | hybrid
    status = Column(String(20), default="online")  # online | offline | unknown
    api_base_url = Column(String(500), nullable=True)
    public_host = Column(String(255), nullable=True)
    auth_token = Column(String(200), nullable=True, index=True)
    is_local = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    capabilities = Column(Text, nullable=True)  # JSON dict
    metadata_json = Column(Text, nullable=True) # JSON dict
    heartbeat_interval = Column(Integer, default=15)
    last_seen = Column(DateTime, nullable=True)
    offline_since = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    connection_type     = Column(String(20), nullable=True)   # "websocket" | "http_polling"
    agent_version       = Column(String(40), nullable=True)
    node_cpu_percent    = Column(Float, nullable=True)
    node_memory_percent = Column(Float, nullable=True)
    node_disk_percent   = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NodeInvite(Base):
    __tablename__ = "node_invites"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(80), unique=True, nullable=False, index=True)
    note = Column(String(200), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class NodeCommand(Base):
    __tablename__ = "node_commands"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=False, index=True)
    app_id = Column(Integer, ForeignKey("applications.id"), nullable=True, index=True)
    command_type = Column(String(80), nullable=False)
    payload = Column(Text, nullable=True)  # JSON dict
    status = Column(String(20), default="queued")  # queued | in_progress | done | failed
    priority = Column(Integer, default=5)           # 1=high, 5=normal, 10=low
    idempotency_key = Column(String(100), nullable=False, unique=True, index=True)
    result = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    dispatched_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class ApplicationReplica(Base):
    __tablename__ = "application_replicas"

    id            = Column(Integer, primary_key=True, index=True)
    app_id        = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id       = Column(Integer, ForeignKey("nodes.id"), nullable=True, index=True)
    external_port = Column(Integer, nullable=True)
    tunnel_port   = Column(Integer, nullable=True)   # localhost port on main node (reverse tunnel)
    container_id  = Column(String(200), nullable=True)
    status        = Column(String(20), default="stopped")  # pending|starting|running|stopping|stopped|error
    last_error    = Column(Text, nullable=True)
    # Per-instance Docker resource overrides (null = use app-level defaults)
    docker_cpu_limit        = Column(Float, nullable=True)
    docker_memory_limit_mb  = Column(Integer, nullable=True)
    docker_read_only_root   = Column(Boolean, default=False)
    docker_tmpfs_enabled    = Column(Boolean, default=False)
    docker_tmpfs_size_mb    = Column(Integer, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id        = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    action    = Column(String(80), nullable=False)
    actor     = Column(String(80), default="admin", nullable=False)
    app_id    = Column(Integer, ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True)
    detail    = Column(Text, nullable=True)  # JSON string


class StatsHistory(Base):
    __tablename__ = "stats_history"

    id          = Column(Integer, primary_key=True, index=True)
    app_id      = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    timestamp   = Column(DateTime, default=datetime.utcnow, nullable=False)
    cpu_percent = Column(Float, nullable=False)
    memory_mb   = Column(Float, nullable=False)
    net_mb      = Column(Float, nullable=True)
    disk_mb     = Column(Float, nullable=True)


class SystemConfig(Base):
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
