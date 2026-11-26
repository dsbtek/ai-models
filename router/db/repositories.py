"""Strictly tenant-isolated repositories for apps, sessions, messages, and semantic memories."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import re
import secrets
from typing import Any
import uuid

from .pool import DatabaseManager

VALID_APP_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,62}$")
VALID_CATEGORIES = frozenset(
    {"preference", "profile", "fact", "goal", "constraint", "episodic"}
)
VALID_USAGE_SOURCES = frozenset(
    {
        "chat",
        "completion",
        "embedding",
        "memory_extract",
        "memory_compact",
        "memory_embed",
    }
)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(app_id: str) -> tuple[str, str, str]:
    """Generate (plaintext_key, sha256_hash, display_prefix) for an integrated app."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", app_id)[:24].strip("-") or "app"
    raw_key = f"ak_{slug}_{secrets.token_hex(16)}"
    key_hash = hash_api_key(raw_key)
    prefix = f"{raw_key[:min(len(raw_key), 15)]}..."
    return raw_key, key_hash, prefix


def hash_memory_content(content: str) -> str:
    normalized = " ".join(content.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _record_to_app(row: Any) -> dict[str, Any]:
    data = {
        "app_id": row["app_id"],
        "name": row["name"],
        "has_api_key": row["api_key_hash"] is not None,
        "api_key_prefix": row["api_key_prefix"],
        "is_active": row["is_active"],
        "settings": row["settings"] or {},
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }
    if "session_count" in row:
        data["session_count"] = int(row["session_count"] or 0)
    if "memory_count" in row:
        data["memory_count"] = int(row["memory_count"] or 0)
    return data


def _record_to_session(row: Any) -> dict[str, Any]:
    return {
        "app_id": row["app_id"],
        "user_id": row["user_id"],
        "session_id": row["session_id"],
        "title": row["title"],
        "model_id": row["model_id"],
        "rolling_summary": row["rolling_summary"] or "",
        "summary_up_to_seq": int(row["summary_up_to_seq"] or 0),
        "summary_token_est": int(row["summary_token_est"] or 0),
        "message_count": int(row["message_count"] or 0),
        "metadata": row["metadata"] or {},
        "expires_at": _iso(row["expires_at"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "last_active_at": _iso(row["last_active_at"]),
    }


def _record_to_message(row: Any) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "role": row["role"],
        "content": row["content"],
        "token_est": int(row["token_est"]),
        "model_id": row["model_id"],
        "metadata": row["metadata"] or {},
        "created_at": _iso(row["created_at"]),
    }


def _record_to_memory(row: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "memory_id": str(row["memory_id"]),
        "app_id": row["app_id"],
        "user_id": row["user_id"],
        "category": row["category"],
        "content": row["content"],
        "importance": int(row["importance"]),
        "confidence": round(float(row["confidence"]), 4),
        "source_session_id": row["source_session_id"],
        "access_count": int(row["access_count"] or 0),
        "last_accessed_at": _iso(row["last_accessed_at"]),
        "expires_at": _iso(row["expires_at"]),
        "is_active": bool(row["is_active"]),
        "superseded_by": str(row["superseded_by"]) if row["superseded_by"] else None,
        "metadata": row["metadata"] or {},
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }
    if "similarity" in row and row["similarity"] is not None:
        item["similarity"] = round(float(row["similarity"]), 4)
    if "hybrid_score" in row and row["hybrid_score"] is not None:
        item["hybrid_score"] = round(float(row["hybrid_score"]), 4)
    return item


class AppRepository:
    """Manage integrated application registrations and SHA-256 API keys."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def create_app(
        self,
        app_id: str,
        name: str,
        settings: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        if not VALID_APP_ID_RE.match(app_id):
            raise ValueError(
                "Invalid app_id: use 2-63 alphanumeric, dot, underscore, or hyphen characters"
            )
        pool = await self.db.ensure_pool()
        raw_key, key_hash, prefix = generate_api_key(app_id)
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT app_id, api_key_hash FROM apps WHERE app_id = $1", app_id
            )
            if existing is not None and existing["api_key_hash"] is not None:
                raise ValueError(f"App '{app_id}' is already registered")
            row = await conn.fetchrow(
                """
                INSERT INTO apps (app_id, name, api_key_hash, api_key_prefix, is_active, settings)
                VALUES ($1, $2, $3, $4, TRUE, $5::jsonb)
                ON CONFLICT (app_id) DO UPDATE
                SET name = EXCLUDED.name,
                    api_key_hash = EXCLUDED.api_key_hash,
                    api_key_prefix = EXCLUDED.api_key_prefix,
                    is_active = TRUE,
                    settings = EXCLUDED.settings,
                    updated_at = NOW()
                RETURNING *
                """,
                app_id,
                name.strip() or app_id,
                key_hash,
                prefix,
                settings or {},
            )
        return _record_to_app(row), raw_key

    async def ensure_sandbox_app(self, app_id: str) -> dict[str, Any]:
        """Auto-provision an unauthenticated dev-mode app ONLY if not claimed by a registered key."""
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT * FROM apps WHERE app_id = $1", app_id)
            if existing is not None:
                if existing["api_key_hash"] is not None:
                    raise PermissionError(
                        f"App '{app_id}' is a registered key-protected app; provide its Bearer ak_... API key"
                    )
                if not existing["is_active"]:
                    raise PermissionError(f"App '{app_id}' is deactivated")
                return _record_to_app(existing)

            row = await conn.fetchrow(
                """
                INSERT INTO apps (app_id, name, api_key_hash, api_key_prefix, is_active, settings)
                VALUES ($1, $2, NULL, NULL, TRUE, '{"sandbox": true}'::jsonb)
                ON CONFLICT (app_id) DO NOTHING
                RETURNING *
                """,
                app_id,
                f"Dev Sandbox ({app_id})",
            )
            if row is None:
                # Concurrent insert fallback
                row = await conn.fetchrow("SELECT * FROM apps WHERE app_id = $1", app_id)
                if row["api_key_hash"] is not None:
                    raise PermissionError(
                        f"App '{app_id}' is a registered key-protected app"
                    )
            return _record_to_app(row)

    async def get_app(self, app_id: str) -> dict[str, Any] | None:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM sessions s WHERE s.app_id = a.app_id) AS session_count,
                       (SELECT COUNT(*) FROM user_memories m WHERE m.app_id = a.app_id AND m.is_active = TRUE) AS memory_count
                FROM apps a
                WHERE a.app_id = $1
                """,
                app_id,
            )
            return _record_to_app(row) if row else None

    async def verify_api_key(self, raw_key: str) -> dict[str, Any] | None:
        key_hash = hash_api_key(raw_key)
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM apps WHERE api_key_hash = $1",
                key_hash,
            )
            return _record_to_app(row) if row else None

    async def list_apps(self) -> list[dict[str, Any]]:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM sessions s WHERE s.app_id = a.app_id) AS session_count,
                       (SELECT COUNT(*) FROM user_memories m WHERE m.app_id = a.app_id AND m.is_active = TRUE) AS memory_count
                FROM apps a
                ORDER BY a.created_at DESC
                """
            )
            return [_record_to_app(r) for r in rows]

    async def rotate_key(self, app_id: str) -> tuple[dict[str, Any], str] | None:
        pool = await self.db.ensure_pool()
        raw_key, key_hash, prefix = generate_api_key(app_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE apps
                SET api_key_hash = $2,
                    api_key_prefix = $3,
                    is_active = TRUE,
                    updated_at = NOW()
                WHERE app_id = $1
                RETURNING *
                """,
                app_id,
                key_hash,
                prefix,
            )
            if row is None:
                return None
            return _record_to_app(row), raw_key

    async def delete_or_deactivate(self, app_id: str, hard: bool = False) -> bool:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            if hard:
                status = await conn.execute("DELETE FROM apps WHERE app_id = $1", app_id)
                return status.endswith("1")
            row = await conn.fetchrow(
                """
                UPDATE apps
                SET is_active = FALSE, updated_at = NOW()
                WHERE app_id = $1
                RETURNING app_id
                """,
                app_id,
            )
            return row is not None


class SessionRepository:
    """Strictly (app_id, user_id)-scoped session and message history persistence."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def upsert_session(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        title: str | None = None,
        model_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        pool = await self.db.ensure_pool()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            if ttl_hours and ttl_hours > 0
            else None
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sessions (
                    app_id, user_id, session_id, title, model_id, metadata, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, COALESCE($6::jsonb, '{}'::jsonb), $7)
                ON CONFLICT (app_id, user_id, session_id) DO UPDATE
                SET title = COALESCE($4, sessions.title),
                    model_id = COALESCE($5, sessions.model_id),
                    metadata = CASE
                        WHEN $6::jsonb IS NOT NULL THEN sessions.metadata || $6::jsonb
                        ELSE sessions.metadata
                    END,
                    expires_at = COALESCE($7, sessions.expires_at),
                    updated_at = NOW(),
                    last_active_at = NOW()
                RETURNING *
                """,
                app_id,
                user_id,
                session_id,
                title,
                model_id,
                metadata,
                expires_at,
            )
            return _record_to_session(row)

    async def get_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM sessions
                WHERE app_id = $1
                  AND user_id = $2
                  AND session_id = $3
                  AND (expires_at IS NULL OR expires_at > NOW())
                """,
                app_id,
                user_id,
                session_id,
            )
            return _record_to_session(row) if row else None

    async def list_sessions(
        self, app_id: str, user_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM sessions
                WHERE app_id = $1
                  AND user_id = $2
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY last_active_at DESC
                LIMIT $3 OFFSET $4
                """,
                app_id,
                user_id,
                max(1, min(limit, 200)),
                max(0, offset),
            )
            return [_record_to_session(r) for r in rows]

    async def get_messages(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            # Fetch the newest `limit` messages after `after_seq`, then return in chronological order
            rows = await conn.fetch(
                """
                SELECT * FROM (
                    SELECT seq, role, content, token_est, model_id, metadata, created_at
                    FROM session_messages
                    WHERE app_id = $1
                      AND user_id = $2
                      AND session_id = $3
                      AND seq > $4
                    ORDER BY seq DESC
                    LIMIT $5
                ) sub
                ORDER BY seq ASC
                """,
                app_id,
                user_id,
                session_id,
                after_seq,
                max(1, min(limit, 500)),
            )
            return [_record_to_message(r) for r in rows]

    async def append_messages(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        model_id: str | None = None,
        ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        """Atomically append ordered messages to a session using row-level locking."""
        if not messages:
            existing = await self.get_session(app_id, user_id, session_id)
            if existing:
                return existing
            return await self.upsert_session(
                app_id, user_id, session_id, model_id=model_id, ttl_hours=ttl_hours
            )

        pool = await self.db.ensure_pool()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            if ttl_hours and ttl_hours > 0
            else None
        )
        first_user_text = next(
            (
                str(m.get("content", "")).strip()
                for m in messages
                if m.get("role") == "user" and str(m.get("content", "")).strip()
            ),
            None,
        )
        auto_title = first_user_text[:60] if first_user_text else None

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO sessions (app_id, user_id, session_id, title, model_id, expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (app_id, user_id, session_id) DO NOTHING
                    """,
                    app_id,
                    user_id,
                    session_id,
                    auto_title,
                    model_id,
                    expires_at,
                )
                locked = await conn.fetchrow(
                    """
                    SELECT message_count, title, expires_at
                    FROM sessions
                    WHERE app_id = $1 AND user_id = $2 AND session_id = $3
                    FOR UPDATE
                    """,
                    app_id,
                    user_id,
                    session_id,
                )
                base_seq = int(locked["message_count"] or 0)
                records = []
                for idx, msg in enumerate(messages, start=1):
                    records.append(
                        (
                            app_id,
                            user_id,
                            session_id,
                            base_seq + idx,
                            msg["role"],
                            msg["content"],
                            int(msg["token_est"]),
                            msg.get("model_id") or model_id,
                            msg.get("metadata") or {},
                        )
                    )
                await conn.executemany(
                    """
                    INSERT INTO session_messages (
                        app_id, user_id, session_id, seq, role, content, token_est, model_id, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                    """,
                    records,
                )
                new_count = base_seq + len(records)
                updated_session = await conn.fetchrow(
                    """
                    UPDATE sessions
                    SET message_count = $4,
                        title = COALESCE(sessions.title, $5),
                        model_id = COALESCE($6, sessions.model_id),
                        expires_at = COALESCE($7, sessions.expires_at),
                        updated_at = NOW(),
                        last_active_at = NOW()
                    WHERE app_id = $1 AND user_id = $2 AND session_id = $3
                    RETURNING *
                    """,
                    app_id,
                    user_id,
                    session_id,
                    new_count,
                    auto_title,
                    model_id,
                    expires_at,
                )
                return _record_to_session(updated_session)

    async def update_rolling_summary(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        expected_up_to_seq: int,
        new_up_to_seq: int,
        summary: str,
        summary_token_est: int,
    ) -> bool:
        """Optimistic CAS update so concurrent compaction never regresses summary_up_to_seq."""
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE sessions
                SET rolling_summary = $5,
                    summary_up_to_seq = $6,
                    summary_token_est = $7,
                    updated_at = NOW()
                WHERE app_id = $1
                  AND user_id = $2
                  AND session_id = $3
                  AND summary_up_to_seq = $4
                RETURNING session_id
                """,
                app_id,
                user_id,
                session_id,
                expected_up_to_seq,
                summary,
                new_up_to_seq,
                summary_token_est,
            )
            return row is not None

    async def delete_session(
        self, app_id: str, user_id: str, session_id: str
    ) -> bool:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM sessions
                WHERE app_id = $1 AND user_id = $2 AND session_id = $3
                """,
                app_id,
                user_id,
                session_id,
            )
            return status.endswith("1")


class MemoryRepository:
    """Strictly (app_id, user_id)-scoped long-term semantic user memory store."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def search_memories(
        self,
        app_id: str,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.35,
        bump_access: bool = True,
    ) -> list[dict[str, Any]]:
        pool = await self.db.ensure_pool()
        vec = [float(x) for x in query_embedding]
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *,
                       (1 - (embedding <=> $3)) AS similarity,
                       (
                           0.65 * (1 - (embedding <=> $3))
                         + 0.20 * (importance / 5.0)
                         + 0.15 * EXP(-GREATEST(0.0, EXTRACT(EPOCH FROM (NOW() - updated_at))) / 2592000.0)
                       ) AS hybrid_score
                FROM user_memories
                WHERE app_id = $1
                  AND user_id = $2
                  AND is_active = TRUE
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND (1 - (embedding <=> $3)) >= $4
                ORDER BY hybrid_score DESC
                LIMIT $5
                """,
                app_id,
                user_id,
                vec,
                min_similarity,
                max(1, min(top_k, 50)),
            )
            results = [_record_to_memory(r) for r in rows]
            if results and bump_access:
                ids = [uuid.UUID(r["memory_id"]) for r in results]
                await conn.execute(
                    """
                    UPDATE user_memories
                    SET access_count = access_count + 1,
                        last_accessed_at = NOW()
                    WHERE app_id = $1 AND user_id = $2 AND memory_id = ANY($3::uuid[])
                    """,
                    app_id,
                    user_id,
                    ids,
                )
            return results

    async def upsert_semantic_memory(
        self,
        app_id: str,
        user_id: str,
        content: str,
        embedding: list[float],
        category: str = "fact",
        importance: int = 3,
        confidence: float = 0.85,
        source_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_hours: int | None = None,
        max_memories: int = 200,
    ) -> tuple[dict[str, Any], str]:
        """Insert or deduplicate/supersede a durable user fact within (app_id, user_id)."""
        clean_content = content.strip()
        if not clean_content:
            raise ValueError("Memory content cannot be empty")
        cat = category if category in VALID_CATEGORIES else "fact"
        imp = max(1, min(int(importance), 5))
        c_hash = hash_memory_content(clean_content)
        vec = [float(x) for x in embedding]
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            if ttl_hours and ttl_hours > 0
            else None
        )

        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Exact content hash check
                exact = await conn.fetchrow(
                    """
                    SELECT * FROM user_memories
                    WHERE app_id = $1 AND user_id = $2 AND is_active = TRUE AND content_hash = $3
                    LIMIT 1
                    """,
                    app_id,
                    user_id,
                    c_hash,
                )
                if exact is not None:
                    updated = await conn.fetchrow(
                        """
                        UPDATE user_memories
                        SET importance = GREATEST(importance, $4),
                            confidence = GREATEST(confidence, $5),
                            updated_at = NOW()
                        WHERE memory_id = $1 AND app_id = $2 AND user_id = $3
                        RETURNING *
                        """,
                        exact["memory_id"],
                        app_id,
                        user_id,
                        imp,
                        confidence,
                    )
                    return _record_to_memory(updated), "deduplicated"

                # 2. Nearest semantic neighbor check within (app_id, user_id)
                nearest = await conn.fetchrow(
                    """
                    SELECT *, (1 - (embedding <=> $3)) AS similarity
                    FROM user_memories
                    WHERE app_id = $1
                      AND user_id = $2
                      AND is_active = TRUE
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY embedding <=> $3 ASC
                    LIMIT 1
                    """,
                    app_id,
                    user_id,
                    vec,
                )
                if nearest is not None:
                    sim = float(nearest["similarity"] or 0.0)
                    if sim >= 0.90:
                        updated = await conn.fetchrow(
                            """
                            UPDATE user_memories
                            SET importance = GREATEST(importance, $4),
                                confidence = GREATEST(confidence, $5),
                                updated_at = NOW()
                            WHERE memory_id = $1 AND app_id = $2 AND user_id = $3
                            RETURNING *
                            """,
                            nearest["memory_id"],
                            app_id,
                            user_id,
                            imp,
                            confidence,
                        )
                        return _record_to_memory(updated), "deduplicated"

                    if 0.78 <= sim < 0.90 and nearest["category"] == cat:
                        inserted = await conn.fetchrow(
                            """
                            INSERT INTO user_memories (
                                app_id, user_id, category, content, content_hash, embedding,
                                importance, confidence, source_session_id, expires_at, metadata
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                            RETURNING *
                            """,
                            app_id,
                            user_id,
                            cat,
                            clean_content,
                            c_hash,
                            vec,
                            imp,
                            confidence,
                            source_session_id,
                            expires_at,
                            metadata or {},
                        )
                        await conn.execute(
                            """
                            UPDATE user_memories
                            SET is_active = FALSE,
                                superseded_by = $4,
                                updated_at = NOW()
                            WHERE memory_id = $1 AND app_id = $2 AND user_id = $3
                            """,
                            nearest["memory_id"],
                            app_id,
                            user_id,
                            inserted["memory_id"],
                        )
                        return _record_to_memory(inserted), "superseded"

                inserted = await conn.fetchrow(
                    """
                    INSERT INTO user_memories (
                        app_id, user_id, category, content, content_hash, embedding,
                        importance, confidence, source_session_id, expires_at, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                    RETURNING *
                    """,
                    app_id,
                    user_id,
                    cat,
                    clean_content,
                    c_hash,
                    vec,
                    imp,
                    confidence,
                    source_session_id,
                    expires_at,
                    metadata or {},
                )

                # Enforce per-(app_id, user_id) active memory cap
                await conn.execute(
                    """
                    UPDATE user_memories
                    SET is_active = FALSE, updated_at = NOW()
                    WHERE memory_id IN (
                        SELECT memory_id FROM user_memories
                        WHERE app_id = $1 AND user_id = $2 AND is_active = TRUE
                        ORDER BY importance DESC, updated_at DESC
                        OFFSET $3
                    )
                    """,
                    app_id,
                    user_id,
                    max_memories,
                )
                return _record_to_memory(inserted), "created"

    async def list_memories(
        self,
        app_id: str,
        user_id: str,
        category: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM user_memories
                WHERE app_id = $1
                  AND user_id = $2
                  AND ($3::boolean OR is_active = TRUE)
                  AND ($4::text IS NULL OR category = $4)
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY importance DESC, updated_at DESC
                LIMIT $5 OFFSET $6
                """,
                app_id,
                user_id,
                include_inactive,
                category,
                max(1, min(limit, 300)),
                max(0, offset),
            )
            return [_record_to_memory(r) for r in rows]

    async def get_memory(
        self, app_id: str, user_id: str, memory_id: str
    ) -> dict[str, Any] | None:
        pool = await self.db.ensure_pool()
        mem_uuid = uuid.UUID(memory_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_memories WHERE app_id = $1 AND user_id = $2 AND memory_id = $3",
                app_id,
                user_id,
                mem_uuid,
            )
            return _record_to_memory(row) if row else None

    async def update_memory(
        self,
        app_id: str,
        user_id: str,
        memory_id: str,
        content: str | None = None,
        embedding: list[float] | None = None,
        category: str | None = None,
        importance: int | None = None,
        is_active: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pool = await self.db.ensure_pool()
        mem_uuid = uuid.UUID(memory_id)
        vec = [float(x) for x in embedding] if embedding is not None else None
        c_hash = hash_memory_content(content) if content is not None else None
        cat = category if (category and category in VALID_CATEGORIES) else None
        imp = max(1, min(int(importance), 5)) if importance is not None else None

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE user_memories
                SET content = COALESCE($4, content),
                    content_hash = COALESCE($5, content_hash),
                    embedding = COALESCE($6, embedding),
                    category = COALESCE($7, category),
                    importance = COALESCE($8, importance),
                    is_active = COALESCE($9, is_active),
                    metadata = CASE
                        WHEN $10::jsonb IS NOT NULL THEN user_memories.metadata || $10::jsonb
                        ELSE user_memories.metadata
                    END,
                    updated_at = NOW()
                WHERE app_id = $1 AND user_id = $2 AND memory_id = $3
                RETURNING *
                """,
                app_id,
                user_id,
                mem_uuid,
                content.strip() if content is not None else None,
                c_hash,
                vec,
                cat,
                imp,
                is_active,
                metadata,
            )
            return _record_to_memory(row) if row else None

    async def delete_memory(self, app_id: str, user_id: str, memory_id: str) -> bool:
        pool = await self.db.ensure_pool()
        mem_uuid = uuid.UUID(memory_id)
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM user_memories WHERE app_id = $1 AND user_id = $2 AND memory_id = $3",
                app_id,
                user_id,
                mem_uuid,
            )
            return status.endswith("1")

    async def wipe_user_data(self, app_id: str, user_id: str) -> dict[str, int]:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                mem_status = await conn.execute(
                    "DELETE FROM user_memories WHERE app_id = $1 AND user_id = $2",
                    app_id,
                    user_id,
                )
                sess_status = await conn.execute(
                    "DELETE FROM sessions WHERE app_id = $1 AND user_id = $2",
                    app_id,
                    user_id,
                )
                deleted_memories = int(mem_status.split()[-1]) if mem_status else 0
                deleted_sessions = int(sess_status.split()[-1]) if sess_status else 0
                return {
                    "deleted_memories": deleted_memories,
                    "deleted_sessions": deleted_sessions,
                }

    async def purge_expired(self) -> dict[str, int]:
        pool = await self.db.ensure_pool()
        async with pool.acquire() as conn:
            m_stat = await conn.execute(
                "DELETE FROM user_memories WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            s_stat = await conn.execute(
                "DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            return {
                "expired_memories": int(m_stat.split()[-1]) if m_stat else 0,
                "expired_sessions": int(s_stat.split()[-1]) if s_stat else 0,
            }


class UsageRepository:
    """Tenant-isolated daily token usage aggregation per (app_id, user_id, model_id, source)."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def record_usage(
        self,
        *,
        app_id: str,
        user_id: str | None,
        model_id: str,
        source: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
        usage_date: date | None = None,
    ) -> None:
        if source not in VALID_USAGE_SOURCES:
            raise ValueError(f"Invalid usage source: {source}")
        p_tok = max(0, int(prompt_tokens))
        c_tok = max(0, int(completion_tokens))
        t_tok = max(p_tok + c_tok, int(total_tokens)) if total_tokens is not None else p_tok + c_tok
        if t_tok == 0 and p_tok == 0 and c_tok == 0:
            return

        pool = await self.db.ensure_pool()
        target_date = usage_date or datetime.now(timezone.utc).date()
        norm_user = (user_id or "").strip()

        async with pool.acquire() as conn:
            # Ensure sandbox app row exists for stateless calls so FK on apps(app_id) succeeds.
            await conn.execute(
                """
                INSERT INTO apps (app_id, name, api_key_hash, api_key_prefix, is_active, settings)
                VALUES ($1, $2, NULL, NULL, TRUE, '{"sandbox": true}'::jsonb)
                ON CONFLICT (app_id) DO NOTHING
                """,
                app_id,
                f"Dev Sandbox ({app_id})",
            )
            await conn.execute(
                """
                INSERT INTO token_usage_daily (
                    usage_date, app_id, user_id, model_id, source,
                    request_count, prompt_tokens, completion_tokens, total_tokens
                )
                VALUES ($1, $2, $3, $4, $5, 1, $6, $7, $8)
                ON CONFLICT (usage_date, app_id, user_id, model_id, source) DO UPDATE
                SET request_count = token_usage_daily.request_count + 1,
                    prompt_tokens = token_usage_daily.prompt_tokens + EXCLUDED.prompt_tokens,
                    completion_tokens = token_usage_daily.completion_tokens + EXCLUDED.completion_tokens,
                    total_tokens = token_usage_daily.total_tokens + EXCLUDED.total_tokens,
                    updated_at = NOW()
                """,
                target_date,
                app_id,
                norm_user,
                model_id,
                source,
                p_tok,
                c_tok,
                t_tok,
            )

    @staticmethod
    def _build_filters(
        app_id: str,
        user_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        model_id: str | None = None,
        source: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses = ["app_id = $1"]
        params: list[Any] = [app_id]
        if user_id is not None:
            params.append(user_id.strip())
            clauses.append(f"user_id = ${len(params)}")
        if start_date is not None:
            params.append(start_date)
            clauses.append(f"usage_date >= ${len(params)}")
        if end_date is not None:
            params.append(end_date)
            clauses.append(f"usage_date <= ${len(params)}")
        if model_id is not None:
            params.append(model_id)
            clauses.append(f"model_id = ${len(params)}")
        if source is not None:
            params.append(source)
            clauses.append(f"source = ${len(params)}")
        return " AND ".join(clauses), params

    async def get_usage_summary(
        self,
        *,
        app_id: str,
        user_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        model_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        pool = await self.db.ensure_pool()
        where_sql, params = self._build_filters(
            app_id=app_id,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
            model_id=model_id,
            source=source,
        )
        async with pool.acquire() as conn:
            totals_row = await conn.fetchrow(
                f"""
                SELECT COALESCE(SUM(request_count), 0) AS request_count,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM token_usage_daily
                WHERE {where_sql}
                """,
                *params,
            )
            by_model_rows = await conn.fetch(
                f"""
                SELECT model_id,
                       SUM(request_count) AS request_count,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens
                FROM token_usage_daily
                WHERE {where_sql}
                GROUP BY model_id
                ORDER BY total_tokens DESC
                """,
                *params,
            )
            by_source_rows = await conn.fetch(
                f"""
                SELECT source,
                       SUM(request_count) AS request_count,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens
                FROM token_usage_daily
                WHERE {where_sql}
                GROUP BY source
                ORDER BY total_tokens DESC
                """,
                *params,
            )
            by_day_rows = await conn.fetch(
                f"""
                SELECT usage_date,
                       SUM(request_count) AS request_count,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens
                FROM token_usage_daily
                WHERE {where_sql}
                GROUP BY usage_date
                ORDER BY usage_date DESC
                LIMIT 90
                """,
                *params,
            )

        def _agg(row: Any) -> dict[str, int]:
            return {
                "request_count": int(row["request_count"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }

        return {
            "totals": _agg(totals_row),
            "by_model": [{"model_id": r["model_id"], **_agg(r)} for r in by_model_rows],
            "by_source": [{"source": r["source"], **_agg(r)} for r in by_source_rows],
            "by_day": [{"date": r["usage_date"].isoformat(), **_agg(r)} for r in by_day_rows],
        }

    async def list_user_usage(
        self,
        *,
        app_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        pool = await self.db.ensure_pool()
        where_sql, params = self._build_filters(
            app_id=app_id, start_date=start_date, end_date=end_date
        )
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT user_id,
                       SUM(request_count) AS request_count,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       MAX(usage_date) AS last_active_date
                FROM token_usage_daily
                WHERE {where_sql}
                GROUP BY user_id
                ORDER BY total_tokens DESC
                LIMIT ${limit_idx} OFFSET ${offset_idx}
                """,
                *params,
                limit,
                offset,
            )
        return [
            {
                "user_id": r["user_id"] if r["user_id"] else None,
                "request_count": int(r["request_count"] or 0),
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "last_active_date": r["last_active_date"].isoformat()
                if r["last_active_date"]
                else None,
            }
            for r in rows
        ]
