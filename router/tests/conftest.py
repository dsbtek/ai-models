"""Shared fixtures and fake tenant repositories for deterministic router unit & API tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any
import uuid

import httpx
import pytest

ROUTER_DIR = Path(__file__).resolve().parent.parent
if str(ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(ROUTER_DIR))

from app import create_app  # noqa: E402
from config import RouterConfig  # noqa: E402
from db.repositories import generate_api_key, hash_api_key, hash_memory_content  # noqa: E402


def deterministic_embed(text: str, dim: int = 384) -> list[float]:
    """Deterministic unit-norm embedding based on token hashing so related texts have high cosine sim."""
    vec = [0.0] * dim
    tokens = [t.strip(".,!?;:()[]\"'").lower() for t in text.split()]
    for tok in tokens:
        if not tok:
            continue
        idx = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [round(x / norm, 6) for x in vec]


class FakeDBManager:
    is_configured = True
    pool = object()
    last_error = None

    async def connect(self) -> bool:
        return True

    async def health_status(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def close(self) -> None:
        pass


class FakeUsageRepo:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str, str, str], dict[str, int]] = {}

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
        p_tok = max(0, int(prompt_tokens))
        c_tok = max(0, int(completion_tokens))
        t_tok = max(p_tok + c_tok, int(total_tokens)) if total_tokens is not None else p_tok + c_tok
        if p_tok == 0 and c_tok == 0 and t_tok == 0:
            return
        d_str = (usage_date or datetime.now(timezone.utc).date()).isoformat()
        key = (d_str, app_id, (user_id or "").strip(), model_id, source)
        cur = self.records.setdefault(
            key,
            {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        cur["request_count"] += 1
        cur["prompt_tokens"] += p_tok
        cur["completion_tokens"] += c_tok
        cur["total_tokens"] += t_tok

    def _filtered(
        self,
        app_id: str,
        user_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        model_id: str | None = None,
        source: str | None = None,
    ) -> list[tuple[tuple[str, str, str, str, str], dict[str, int]]]:
        out = []
        for k, v in self.records.items():
            d_str, a_id, u_id, m_id, src = k
            if a_id != app_id:
                continue
            if user_id is not None and u_id != user_id.strip():
                continue
            d_val = date.fromisoformat(d_str)
            if start_date is not None and d_val < start_date:
                continue
            if end_date is not None and d_val > end_date:
                continue
            if model_id is not None and m_id != model_id:
                continue
            if source is not None and src != source:
                continue
            out.append((k, v))
        return out

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
        rows = self._filtered(app_id, user_id, start_date, end_date, model_id, source)
        totals = {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model: dict[str, dict[str, int]] = {}
        by_source: dict[str, dict[str, int]] = {}
        by_day: dict[str, dict[str, int]] = {}
        for (d_str, _, _, m_id, src), vals in rows:
            for k in totals:
                totals[k] += vals[k]
            for bucket, b_key in ((by_model, m_id), (by_source, src), (by_day, d_str)):
                b = bucket.setdefault(
                    b_key,
                    {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )
                for k in b:
                    b[k] += vals[k]
        return {
            "totals": totals,
            "by_model": sorted(
                [{"model_id": k, **v} for k, v in by_model.items()],
                key=lambda x: x["total_tokens"],
                reverse=True,
            ),
            "by_source": sorted(
                [{"source": k, **v} for k, v in by_source.items()],
                key=lambda x: x["total_tokens"],
                reverse=True,
            ),
            "by_day": sorted(
                [{"date": k, **v} for k, v in by_day.items()],
                key=lambda x: x["date"],
                reverse=True,
            ),
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
        rows = self._filtered(app_id, None, start_date, end_date, None, None)
        by_user: dict[str, dict[str, Any]] = {}
        for (d_str, _, u_id, _, _), vals in rows:
            b = by_user.setdefault(
                u_id,
                {
                    "user_id": u_id if u_id else None,
                    "request_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "last_active_date": d_str,
                },
            )
            b["request_count"] += vals["request_count"]
            b["prompt_tokens"] += vals["prompt_tokens"]
            b["completion_tokens"] += vals["completion_tokens"]
            b["total_tokens"] += vals["total_tokens"]
            if d_str > b["last_active_date"]:
                b["last_active_date"] = d_str
        ordered = sorted(by_user.values(), key=lambda x: x["total_tokens"], reverse=True)
        return ordered[offset : offset + limit]


class FakeAppRepo:
    def __init__(self) -> None:
        self.apps: dict[str, dict[str, Any]] = {}
        self.sessions_ref: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.memories_ref: list[dict[str, Any]] = []

    async def create_app(
        self, app_id: str, name: str, settings: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str]:
        if app_id in self.apps and self.apps[app_id]["api_key_hash"] is not None:
            raise ValueError(f"App '{app_id}' is already registered")
        raw_key, key_hash, prefix = generate_api_key(app_id)
        now = datetime.now(timezone.utc).isoformat()
        rec = {
            "app_id": app_id,
            "name": name or app_id,
            "api_key_hash": key_hash,
            "has_api_key": True,
            "api_key_prefix": prefix,
            "is_active": True,
            "settings": settings or {},
            "created_at": now,
            "updated_at": now,
        }
        self.apps[app_id] = rec
        return {k: v for k, v in rec.items() if k != "api_key_hash"}, raw_key

    async def ensure_sandbox_app(self, app_id: str) -> dict[str, Any]:
        existing = self.apps.get(app_id)
        if existing:
            if existing["api_key_hash"] is not None:
                raise PermissionError(
                    f"App '{app_id}' is a registered key-protected app; provide its Bearer ak_... API key"
                )
            if not existing["is_active"]:
                raise PermissionError(f"App '{app_id}' is deactivated")
            return existing
        now = datetime.now(timezone.utc).isoformat()
        rec = {
            "app_id": app_id,
            "name": f"Dev Sandbox ({app_id})",
            "api_key_hash": None,
            "has_api_key": False,
            "api_key_prefix": None,
            "is_active": True,
            "settings": {"sandbox": True},
            "created_at": now,
            "updated_at": now,
        }
        self.apps[app_id] = rec
        return rec

    async def get_app(self, app_id: str) -> dict[str, Any] | None:
        rec = self.apps.get(app_id)
        if not rec:
            return None
        sc = sum(1 for (a, _, _) in self.sessions_ref if a == app_id)
        mc = sum(1 for m in self.memories_ref if m["app_id"] == app_id and m["is_active"])
        return {**{k: v for k, v in rec.items() if k != "api_key_hash"}, "session_count": sc, "memory_count": mc}

    async def verify_api_key(self, raw_key: str) -> dict[str, Any] | None:
        h = hash_api_key(raw_key)
        for rec in self.apps.values():
            if rec["api_key_hash"] == h:
                return {k: v for k, v in rec.items() if k != "api_key_hash"}
        return None

    async def list_apps(self) -> list[dict[str, Any]]:
        out = []
        for aid in self.apps:
            info = await self.get_app(aid)
            if info:
                out.append(info)
        return out

    async def rotate_key(self, app_id: str) -> tuple[dict[str, Any], str] | None:
        rec = self.apps.get(app_id)
        if not rec:
            return None
        raw_key, key_hash, prefix = generate_api_key(app_id)
        rec["api_key_hash"] = key_hash
        rec["api_key_prefix"] = prefix
        rec["has_api_key"] = True
        rec["is_active"] = True
        return {k: v for k, v in rec.items() if k != "api_key_hash"}, raw_key

    async def delete_or_deactivate(self, app_id: str, hard: bool = False) -> bool:
        if app_id not in self.apps:
            return False
        if hard:
            del self.apps[app_id]
            for k in [k for k in self.sessions_ref if k[0] == app_id]:
                del self.sessions_ref[k]
            self.memories_ref[:] = [m for m in self.memories_ref if m["app_id"] != app_id]
        else:
            self.apps[app_id]["is_active"] = False
        return True


class FakeSessionRepo:
    def __init__(self, sessions_ref: dict[tuple[str, str, str], dict[str, Any]]) -> None:
        self.sessions = sessions_ref
        self.messages: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

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
        key = (app_id, user_id, session_id)
        now = datetime.now(timezone.utc).isoformat()
        if key not in self.sessions:
            self.sessions[key] = {
                "app_id": app_id,
                "user_id": user_id,
                "session_id": session_id,
                "title": title,
                "model_id": model_id,
                "rolling_summary": "",
                "summary_up_to_seq": 0,
                "summary_token_est": 0,
                "message_count": 0,
                "metadata": metadata or {},
                "expires_at": None,
                "created_at": now,
                "updated_at": now,
                "last_active_at": now,
            }
        else:
            if title:
                self.sessions[key]["title"] = title
            if metadata:
                self.sessions[key]["metadata"].update(metadata)
        return dict(self.sessions[key])

    async def get_session(self, app_id: str, user_id: str, session_id: str) -> dict[str, Any] | None:
        s = self.sessions.get((app_id, user_id, session_id))
        return dict(s) if s else None

    async def list_sessions(self, app_id: str, user_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        matched = [
            dict(s)
            for (a, u, _), s in self.sessions.items()
            if a == app_id and u == user_id
        ]
        return matched[offset : offset + limit]

    async def get_messages(
        self, app_id: str, user_id: str, session_id: str, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        msgs = [
            dict(m)
            for m in self.messages.get((app_id, user_id, session_id), [])
            if m["seq"] > after_seq
        ]
        return msgs[-limit:]

    async def append_messages(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        model_id: str | None = None,
        ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        await self.upsert_session(app_id, user_id, session_id, model_id=model_id, ttl_hours=ttl_hours)
        key = (app_id, user_id, session_id)
        bucket = self.messages.setdefault(key, [])
        base = self.sessions[key]["message_count"]
        now = datetime.now(timezone.utc).isoformat()
        for idx, m in enumerate(messages, start=1):
            bucket.append(
                {
                    "seq": base + idx,
                    "role": m["role"],
                    "content": m["content"],
                    "token_est": int(m["token_est"]),
                    "model_id": m.get("model_id") or model_id,
                    "metadata": m.get("metadata") or {},
                    "created_at": now,
                }
            )
        self.sessions[key]["message_count"] = base + len(messages)
        if not self.sessions[key]["title"] and messages:
            self.sessions[key]["title"] = messages[0]["content"][:60]
        return dict(self.sessions[key])

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
        key = (app_id, user_id, session_id)
        s = self.sessions.get(key)
        if not s or s["summary_up_to_seq"] != expected_up_to_seq:
            return False
        s["rolling_summary"] = summary
        s["summary_up_to_seq"] = new_up_to_seq
        s["summary_token_est"] = summary_token_est
        return True

    async def delete_session(self, app_id: str, user_id: str, session_id: str) -> bool:
        key = (app_id, user_id, session_id)
        if key not in self.sessions:
            return False
        del self.sessions[key]
        self.messages.pop(key, None)
        return True


class FakeMemoryRepo:
    def __init__(self, memories_ref: list[dict[str, Any]], sessions_ref: dict[tuple[str, str, str], dict[str, Any]]) -> None:
        self.memories = memories_ref
        self.sessions = sessions_ref

    async def has_active_memories(self, app_id: str, user_id: str) -> bool:
        return any(
            m["app_id"] == app_id and m["user_id"] == user_id and m["is_active"]
            for m in self.memories
        )

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        denom = (na * nb) or 1.0
        return float(dot / denom)

    async def search_memories(
        self,
        app_id: str,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.35,
        bump_access: bool = True,
    ) -> list[dict[str, Any]]:
        scored = []
        for m in self.memories:
            if m["app_id"] != app_id or m["user_id"] != user_id or not m["is_active"]:
                continue
            sim = self._cos(m["_vec"], query_embedding)
            if sim >= min_similarity:
                if bump_access:
                    m["access_count"] += 1
                copy = {k: v for k, v in m.items() if k != "_vec"}
                copy["similarity"] = round(sim, 4)
                copy["hybrid_score"] = round(0.65 * sim + 0.20 * (m["importance"] / 5.0) + 0.15, 4)
                scored.append(copy)
        scored.sort(key=lambda x: x["hybrid_score"], reverse=True)
        return scored[:top_k]

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
        c_hash = hash_memory_content(content)
        active = [
            m for m in self.memories
            if m["app_id"] == app_id and m["user_id"] == user_id and m["is_active"]
        ]
        for m in active:
            if m["content_hash"] == c_hash:
                m["importance"] = max(m["importance"], importance)
                return {k: v for k, v in m.items() if k != "_vec"}, "deduplicated"

        best = None
        best_sim = -1.0
        for m in active:
            sim = self._cos(m["_vec"], embedding)
            if sim > best_sim:
                best_sim = sim
                best = m

        if best is not None and best_sim >= 0.90:
            best["importance"] = max(best["importance"], importance)
            return {k: v for k, v in best.items() if k != "_vec"}, "deduplicated"

        now = datetime.now(timezone.utc).isoformat()
        new_id = str(uuid.uuid4())
        rec = {
            "memory_id": new_id,
            "app_id": app_id,
            "user_id": user_id,
            "category": category,
            "content": content.strip(),
            "content_hash": c_hash,
            "_vec": embedding,
            "importance": importance,
            "confidence": confidence,
            "source_session_id": source_session_id,
            "access_count": 0,
            "last_accessed_at": None,
            "expires_at": None,
            "is_active": True,
            "superseded_by": None,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        action = "created"
        if best is not None and 0.78 <= best_sim < 0.90 and best["category"] == category:
            best["is_active"] = False
            best["superseded_by"] = new_id
            action = "superseded"

        self.memories.append(rec)
        return {k: v for k, v in rec.items() if k != "_vec"}, action

    async def list_memories(
        self,
        app_id: str,
        user_id: str,
        category: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        out = [
            {k: v for k, v in m.items() if k != "_vec"}
            for m in self.memories
            if m["app_id"] == app_id
            and m["user_id"] == user_id
            and (include_inactive or m["is_active"])
            and (category is None or m["category"] == category)
        ]
        return out[offset : offset + limit]

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
        uuid.UUID(memory_id)
        for m in self.memories:
            if m["app_id"] == app_id and m["user_id"] == user_id and m["memory_id"] == memory_id:
                if content is not None:
                    m["content"] = content.strip()
                    m["content_hash"] = hash_memory_content(content)
                if embedding is not None:
                    m["_vec"] = embedding
                if category is not None:
                    m["category"] = category
                if importance is not None:
                    m["importance"] = importance
                if is_active is not None:
                    m["is_active"] = is_active
                return {k: v for k, v in m.items() if k != "_vec"}
        return None

    async def delete_memory(self, app_id: str, user_id: str, memory_id: str) -> bool:
        uuid.UUID(memory_id)
        before = len(self.memories)
        self.memories[:] = [
            m
            for m in self.memories
            if not (m["app_id"] == app_id and m["user_id"] == user_id and m["memory_id"] == memory_id)
        ]
        return len(self.memories) < before

    async def wipe_user_data(self, app_id: str, user_id: str) -> dict[str, int]:
        m_before = len(self.memories)
        self.memories[:] = [
            m for m in self.memories if not (m["app_id"] == app_id and m["user_id"] == user_id)
        ]
        s_keys = [k for k in self.sessions if k[0] == app_id and k[1] == user_id]
        for k in s_keys:
            del self.sessions[k]
        return {"deleted_memories": m_before - len(self.memories), "deleted_sessions": len(s_keys)}

    async def purge_expired(self) -> dict[str, int]:
        return {"expired_memories": 0, "expired_sessions": 0}


def build_test_app(auth_mode: str = "dev", admin_api_key: str = "") -> tuple[Any, list[dict[str, Any]]]:
    """Build a FastAPI app wired with mock llama.cpp backends and fake repositories."""
    captured_upstream_calls: list[dict[str, Any]] = []

    def mock_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(req.content.decode("utf-8")) if req.content else {}
        captured_upstream_calls.append({"url": str(req.url), "body": body})

        if path == "/v1/embeddings":
            vec = deterministic_embed(str(body.get("input", "")))
            return httpx.Response(200, json={"object": "list", "data": [{"embedding": vec}]})

        if path == "/v1/chat/completions":
            msgs = body.get("messages") or []
            sys_txt = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
            # Handle background worker summarization or fact extraction prompts
            if "Update the conversation summary" in sys_txt:
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"role": "assistant", "content": "User builds FastAPI + PostgreSQL apps and discussed deployment."}}]},
                )
            if "extract durable, long-term facts" in sys_txt:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": '[{"category":"preference","content":"User prefers FastAPI and PostgreSQL","importance":4}]',
                                }
                            }
                        ]
                    },
                )
            if body.get("stream"):
                chunks = (
                    b'data: {"choices":[{"delta":{"role":"assistant","content":"Hello "}}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"streaming world!"}}]}\n\n'
                    b"data: [DONE]\n\n"
                )
                return httpx.Response(
                    200,
                    content=chunks,
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Mock assistant reply."}}]},
            )

        return httpx.Response(200, json={"choices": [{"text": "raw completion"}]})

    cfg = RouterConfig(
        default_model="llama-3.2-1b",
        models={
            "llama-3.2-1b": "http://mock-llama:8080",
            "qwen2.5-1.5b": "http://mock-qwen:8080",
            "all-minilm-l6-v2": "http://mock-embed:8080",
        },
        embedding_models={"all-minilm-l6-v2"},
        agents={
            "coder": {
                "backend": "qwen2.5-1.5b",
                "description": "Code agent",
                "system_prompt": "You are a concise software engineer.",
                "defaults": {"temperature": 0.2},
            }
        },
        database_url="",  # Prevent real DB connect during unit lifespan
        auth_mode=auth_mode,
        admin_api_key=admin_api_key,
        ctx_size=2048,
    )

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    fastapi_app = create_app(custom_config=cfg, custom_http_client=mock_http)

    fake_db = FakeDBManager()
    app_repo = FakeAppRepo()
    session_repo = FakeSessionRepo(app_repo.sessions_ref)
    memory_repo = FakeMemoryRepo(app_repo.memories_ref, app_repo.sessions_ref)
    usage_repo = FakeUsageRepo()

    # Override repositories after lifespan starts via middleware-like startup hook
    orig_lifespan = fastapi_app.router.lifespan_context

    from contextlib import asynccontextmanager
    from memory.assembler import ContextAssembler
    from memory.embeddings import EmbeddingClient
    from memory.worker import MemoryWorker

    @asynccontextmanager
    async def patched_lifespan(a):
        async with orig_lifespan(a):
            a.state.db = fake_db
            a.state.app_repo = app_repo
            a.state.session_repo = session_repo
            a.state.memory_repo = memory_repo
            a.state.usage_repo = usage_repo
            a.state.embedder = EmbeddingClient(cfg, mock_http)
            a.state.assembler = ContextAssembler(
                cfg, session_repo, memory_repo, a.state.embedder, usage_repo
            )
            await a.state.worker.stop()
            a.state.worker = MemoryWorker(
                cfg, mock_http, session_repo, memory_repo, a.state.embedder, usage_repo
            )
            a.state.worker.start()
            yield
            await mock_http.aclose()

    fastapi_app.router.lifespan_context = patched_lifespan
    return fastapi_app, captured_upstream_calls


@pytest.fixture
def dev_app_bundle():
    return build_test_app(auth_mode="dev")
