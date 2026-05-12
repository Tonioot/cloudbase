from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

DATA_DIR = os.path.expanduser("~/.cloudbase")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR}/cloudbase.db"

from sqlalchemy import event as _sa_event
from sqlalchemy.pool import NullPool

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    poolclass=NullPool,
    connect_args={"timeout": 30},
)

@_sa_event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    import datetime as _dt
    from env_crypto import decrypt_text, encrypt_text
    from models import Application, Node, NodeInvite, NodeCommand, AuditLog, StatsHistory, User, SystemConfig, Role, Permission, role_permissions
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrate existing DBs: add columns introduced after initial schema.
        result = await conn.exec_driver_sql("PRAGMA table_info(applications)")
        existing_columns = {row[1] for row in result.fetchall()}

        for col, definition in [
            ("auto_start",        "BOOLEAN NOT NULL DEFAULT 0"),
            ("restart_policy",    "VARCHAR(20) NOT NULL DEFAULT 'no'"),
            ("docker_cpu_limit",  "FLOAT"),
            ("docker_memory_limit_mb", "INTEGER"),
            ("docker_read_only_root", "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_size_mb", "INTEGER"),
            ("maintenance_mode",  "BOOLEAN NOT NULL DEFAULT 0"),
            ("update_mode",       "BOOLEAN NOT NULL DEFAULT 0"),
            ("downtime_page",     "TEXT"),
            ("update_page",       "TEXT"),
            ("restart_page",      "TEXT"),
            ("starting_page",     "TEXT"),
            ("extra_domains",     "TEXT"),
            ("redirect_domains",  "TEXT"),
            ("node_id",           "INTEGER"),
            ("last_error",        "TEXT"),
            ("source_revision",   "VARCHAR(120)"),
            ("image_revision",    "VARCHAR(120)"),
            ("no_web",            "BOOLEAN NOT NULL DEFAULT 0"),
        ]:
            if col in existing_columns:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE applications ADD COLUMN {col} {definition}"
            )

        # Migrate legacy plaintext GitHub tokens to encrypted values at rest.
        token_rows = await conn.exec_driver_sql(
            "SELECT id, github_token FROM applications WHERE github_token IS NOT NULL AND github_token != ''"
        )
        for app_id, stored_token in token_rows.fetchall():
            if not stored_token:
                continue
            plain = decrypt_text(stored_token, fallback_plaintext=True)
            # If decrypting returns the same value, this is legacy plaintext.
            if plain == stored_token:
                encrypted = encrypt_text(plain)
                await conn.exec_driver_sql(
                    "UPDATE applications SET github_token = ? WHERE id = ?",
                    (encrypted, app_id),
                )

        # Cloudbase now runs applications in Docker-only mode.
        await conn.exec_driver_sql("UPDATE applications SET use_docker = 1 WHERE use_docker IS NULL OR use_docker = 0")

        result = await conn.exec_driver_sql("PRAGMA table_info(nodes)")
        existing_node_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("connection_type",     "VARCHAR(20)"),
            ("agent_version",       "VARCHAR(40)"),
            ("node_cpu_percent",    "FLOAT"),
            ("node_memory_percent", "FLOAT"),
            ("node_disk_percent",   "FLOAT"),
        ]:
            if col in existing_node_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE nodes ADD COLUMN {col} {definition}"
            )

        result = await conn.exec_driver_sql("PRAGMA table_info(node_commands)")
        existing_cmd_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("priority", "INTEGER NOT NULL DEFAULT 5"),
        ]:
            if col in existing_cmd_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE node_commands ADD COLUMN {col} {definition}"
            )

        # nodes — index auth_token for fast per-request token lookups
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_nodes_auth_token ON nodes (auth_token)"
        )

        # audit_logs — new table, created by create_all above; ensure composite index
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_app_ts "
            "ON audit_logs (app_id, timestamp)"
        )

        # stats_history — new table; ensure composite index and purge old rows on startup
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_stats_history_app_ts "
            "ON stats_history (app_id, timestamp)"
        )
        cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=7)).isoformat()
        await conn.exec_driver_sql(
            f"DELETE FROM stats_history WHERE timestamp < '{cutoff}'"
        )

        # stats_history column migrations (added after initial schema)
        result = await conn.exec_driver_sql("PRAGMA table_info(stats_history)")
        existing_sh_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("net_mb",  "FLOAT"),
            ("disk_mb", "FLOAT"),
        ]:
            if col in existing_sh_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE stats_history ADD COLUMN {col} {definition}"
            )

        # application_replicas — add tunnel_port (reverse WebSocket tunnel)
        result = await conn.exec_driver_sql("PRAGMA table_info(application_replicas)")
        existing_replica_cols = {row[1] for row in result.fetchall()}
        for col, definition in [
            ("tunnel_port",              "INTEGER"),
            ("docker_cpu_limit",         "FLOAT"),
            ("docker_memory_limit_mb",   "INTEGER"),
            ("docker_read_only_root",    "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_enabled",     "BOOLEAN NOT NULL DEFAULT 0"),
            ("docker_tmpfs_size_mb",     "INTEGER"),
        ]:
            if col in existing_replica_cols:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE application_replicas ADD COLUMN {col} {definition}"
            )

        # users table — add role_id column if missing
        result = await conn.exec_driver_sql("PRAGMA table_info(users)")
        existing_user_cols = {row[1] for row in result.fetchall()}
        if "role_id" not in existing_user_cols:
            await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL")

    # Seed default permissions, roles, and admin user
    import os as _os
    from sqlalchemy import select as _select
    _CREDENTIALS_FILE = _os.path.expanduser("~/.cloudbase/credentials")

    # All known permissions in the system
    ALL_PERMISSIONS = [
        ("apps.view",         "View applications and their status"),
        ("apps.manage",       "Create, edit and delete applications"),
        ("apps.deploy",       "Deploy, start, stop and restart applications"),
        ("nodes.view",        "View nodes"),
        ("nodes.manage",      "Add, edit and delete nodes"),
        ("logs.view",         "View application logs"),
        ("stats.view",        "View application statistics"),
        ("system.view",       "View system settings"),
        ("system.manage",     "Manage system settings and nginx config"),
        ("users.manage",      "Create, edit and delete users (superadmin only)"),
        ("roles.manage",      "Create, edit and delete roles (superadmin only)"),
        ("audit.view",        "View audit logs"),
        ("github.manage",     "Manage GitHub tokens"),
    ]

    async with AsyncSessionLocal() as session:
        # Ensure all permissions exist
        perm_map: dict[str, int] = {}
        for pname, pdesc in ALL_PERMISSIONS:
            res = await session.execute(_select(Permission).where(Permission.name == pname))
            perm = res.scalar_one_or_none()
            if perm is None:
                perm = Permission(name=pname, description=pdesc)
                session.add(perm)
                await session.flush()
            perm_map[pname] = perm.id

        # Seed default roles: "viewer" (read-only) and "admin" (full access) if they don't exist
        viewer_perms = ["apps.view", "nodes.view", "logs.view", "stats.view", "audit.view"]
        admin_perms  = [p for p, _ in ALL_PERMISSIONS]

        for role_name, role_desc, perms in [
            ("viewer", "Read-only access", viewer_perms),
            ("admin",  "Full access to all features", admin_perms),
        ]:
            res = await session.execute(_select(Role).where(Role.name == role_name))
            role_obj = res.scalar_one_or_none()
            if role_obj is None:
                role_obj = Role(name=role_name, description=role_desc)
                session.add(role_obj)
                await session.flush()
                # Assign permissions via raw insert (avoid ORM relationship overhead)
                for pname in perms:
                    pid = perm_map.get(pname)
                    if pid:
                        await session.execute(
                            role_permissions.insert().values(role_id=role_obj.id, permission_id=pid)
                        )

        await session.commit()

        # Re-fetch role IDs after commit
        res = await session.execute(_select(Role).where(Role.name == "admin"))
        admin_role = res.scalar_one_or_none()
        res = await session.execute(_select(Role).where(Role.name == "viewer"))
        viewer_role = res.scalar_one_or_none()

        # Seed admin user from legacy credentials file if no users exist yet
        existing = await session.execute(_select(User).limit(1))
        if existing.scalar_one_or_none() is None and _os.path.exists(_CREDENTIALS_FILE):
            with open(_CREDENTIALS_FILE) as _f:
                hashed = _f.read().strip()
            if hashed:
                session.add(User(
                    username="admin",
                    password_hash=hashed,
                    role="admin",
                    role_id=admin_role.id if admin_role else None,
                ))
                await session.commit()
        else:
            # Migrate existing users that have no role_id yet
            res = await session.execute(_select(User).where(User.role_id == None))  # noqa: E711
            for u in res.scalars().all():
                if u.role == "admin":
                    u.role_id = admin_role.id if admin_role else None
                else:
                    u.role_id = viewer_role.id if viewer_role else None
            await session.commit()
