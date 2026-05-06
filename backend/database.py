from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

DATA_DIR = os.path.expanduser("~/.cloudbase")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR}/cloudbase.db"

from sqlalchemy import event as _sa_event
from sqlalchemy.pool import NullPool

engine = create_async_engine(DATABASE_URL, echo=False, poolclass=NullPool)

@_sa_event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    import datetime as _dt
    from env_crypto import decrypt_text, encrypt_text
    from models import Application, Node, NodeInvite, NodeCommand, AuditLog, StatsHistory, User
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

    # Seed admin user from legacy credentials file if no users exist yet
    import os as _os
    from sqlalchemy import select as _select
    _CREDENTIALS_FILE = _os.path.expanduser("~/.cloudbase/credentials")
    async with AsyncSessionLocal() as session:
        existing = await session.execute(_select(User).limit(1))
        if existing.scalar_one_or_none() is None and _os.path.exists(_CREDENTIALS_FILE):
            with open(_CREDENTIALS_FILE) as _f:
                hashed = _f.read().strip()
            if hashed:
                session.add(User(username="admin", password_hash=hashed, role="admin"))
                await session.commit()
