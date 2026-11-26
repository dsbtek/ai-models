"""AsyncPG connection pool, pgvector codec registration, and schema bootstrapper."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

# pyrefly: ignore[missing-import]
import asyncpg
# pyrefly: ignore[missing-import]
from pgvector.asyncpg import register_vector

logger = logging.getLogger("router.db")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK_KEY = "ai_models_router_migrations"


async def apply_migrations(
    conn: Any, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending *.sql migrations in sorted order under a PostgreSQL advisory lock."""
    await conn.execute("SELECT pg_advisory_lock(hashtext($1))", MIGRATION_LOCK_KEY)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        applied = {str(r["version"]) for r in rows}

        newly_applied: list[str] = []
        if not migrations_dir.exists():
            return newly_applied

        for path in sorted(migrations_dir.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                if sql.strip():
                    await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT (version) DO NOTHING",
                    version,
                )
            newly_applied.append(version)
            logger.info("Applied database migration: %s", version)

        return newly_applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", MIGRATION_LOCK_KEY)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register pgvector and JSON/JSONB codecs on every pooled connection."""
    await register_vector(conn)
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class DatabaseManager:
    """Manages the lifecycle of the PostgreSQL asyncpg pool and schema migrations."""

    def __init__(self, dsn: str, seed_keys: dict[str, str] | None = None) -> None:
        self.dsn = dsn
        self.seed_keys = seed_keys or {}
        self.pool: asyncpg.Pool | None = None
        self.last_error: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.dsn)

    async def connect(self) -> bool:
        """Initialize extensions, apply schema, and open the connection pool."""
        if not self.dsn:
            self.last_error = "DATABASE_URL is not configured"
            logger.warning("DATABASE_URL is empty; running without persistent memory store.")
            return False

        if self.pool is not None:
            return True

        try:
            bootstrap_conn = await asyncpg.connect(self.dsn, timeout=8.0)
            try:
                has_vector = await bootstrap_conn.fetchval(
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                )
                if not has_vector:
                    try:
                        await bootstrap_conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                    except asyncpg.InsufficientPrivilegeError as exc:
                        db_name = bootstrap_conn.get_settings().get(" dbname", "ai_models_db")
                        hint = (
                            f"pgvector extension is not enabled yet and DB user lacks superuser privileges. "
                            f"Run once on host: sudo -u postgres psql -d {db_name} -c 'CREATE EXTENSION IF NOT EXISTS vector;'"
                        )
                        self.last_error = hint
                        logger.error(hint)
                        raise RuntimeError(hint) from exc

                await apply_migrations(bootstrap_conn)
            finally:
                await bootstrap_conn.close()

            self.pool = await asyncpg.create_pool(
                self.dsn,
                min_size=2,
                max_size=10,
                command_timeout=20.0,
                init=_init_connection,
            )
            await self._seed_apps()
            self.last_error = None
            logger.info("Connected to PostgreSQL + pgvector memory store.")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Failed to initialize PostgreSQL memory store: %s", exc)
            self.pool = None
            return False

    async def ensure_pool(self) -> asyncpg.Pool:
        """Return active pool or attempt lazy reconnection."""
        if self.pool is None:
            await self.connect()
        if self.pool is None:
            raise RuntimeError(self.last_error or "Memory database is unavailable")
        return self.pool

    async def _seed_apps(self) -> None:
        """Seed preconfigured APP_API_KEYS_JSON entries (idempotent)."""
        if not self.seed_keys or self.pool is None:
            return
        async with self.pool.acquire() as conn:
            for app_id, raw_key in self.seed_keys.items():
                if not app_id or not raw_key:
                    continue
                key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
                prefix = f"{raw_key[:14]}..." if len(raw_key) > 14 else raw_key
                await conn.execute(
                    """
                    INSERT INTO apps (app_id, name, api_key_hash, api_key_prefix, is_active)
                    VALUES ($1, $2, $3, $4, TRUE)
                    ON CONFLICT (app_id) DO UPDATE
                    SET api_key_hash = EXCLUDED.api_key_hash,
                        api_key_prefix = EXCLUDED.api_key_prefix,
                        updated_at = NOW()
                    """,
                    app_id,
                    app_id,
                    key_hash,
                    prefix,
                )

    async def health_status(self) -> dict[str, Any]:
        """Return non-throwing health check dictionary for GET /health."""
        if not self.dsn:
            return {"status": "disabled", "detail": "DATABASE_URL not set"}
        try:
            pool = await self.ensure_pool()
            async with pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1")
                applied_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM schema_migrations"
                )
            return {
                "status": "ok" if val == 1 else "degraded",
                "applied_migrations": int(applied_count or 0),
            }
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
