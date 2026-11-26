"""Tests for app/user token usage tracking, background worker attribution, and DB migrations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from asgi_lifespan import LifespanManager
import httpx
import pytest

from db.pool import apply_migrations
from memory.tokens import extract_usage_counts
from memory.worker import PostTurnJob
from tests.conftest import build_test_app


def test_extract_usage_counts_prefers_upstream_and_falls_back():
    assert extract_usage_counts(
        {"usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}},
        fallback_prompt=10,
        fallback_completion=5,
    ) == (120, 45, 165)

    assert extract_usage_counts(None, fallback_prompt=33, fallback_completion=17) == (33, 17, 50)


@pytest.mark.asyncio
async def test_chat_completions_embeddings_and_tenant_usage_isolation():
    app, _ = build_test_app(auth_mode="dev")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. App A / Alice non-streaming chat
            r1 = await client.post(
                "/v1/chat/completions",
                headers={"X-App-Id": "app-alpha", "X-User-Id": "alice"},
                json={"model": "coder", "messages": [{"role": "user", "content": "Hello from Alice"}]},
            )
            assert r1.status_code == 200

            # 2. App A / Bob streaming chat
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"X-App-Id": "app-alpha", "X-User-Id": "bob"},
                json={
                    "model": "llama-3.2-1b",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Stream for Bob"}],
                },
            ) as r2:
                assert r2.status_code == 200
                await r2.aread()

            # 3. App B / Alice raw completion + embedding
            r3 = await client.post(
                "/v1/completions",
                headers={"X-App-Id": "app-beta", "X-User-Id": "alice"},
                json={"model": "llama-3.2-1b", "prompt": "Complete this line"},
            )
            assert r3.status_code == 200

            r4 = await client.post(
                "/v1/embeddings",
                headers={"X-App-Id": "app-beta", "X-User-Id": "alice"},
                json={"model": "all-minilm-l6-v2", "input": "Embed this sentence"},
            )
            assert r4.status_code == 200

            # 4. Query App A / Alice usage (isolated from Bob and App B)
            u_alpha_alice = await client.get(
                "/v1/usage",
                headers={"X-App-Id": "app-alpha", "X-User-Id": "alice"},
            )
            assert u_alpha_alice.status_code == 200
            body = u_alpha_alice.json()
            assert body["app_id"] == "app-alpha"
            assert body["user_id"] == "alice"
            assert body["totals"]["total_tokens"] > 0
            assert any(m["model_id"] == "coder" for m in body["by_model"])
            assert not any(m["model_id"] == "llama-3.2-1b" for m in body["by_model"])

            # 5. Query App A / all users
            u_alpha_users = await client.get(
                "/v1/usage/users",
                headers={"X-App-Id": "app-alpha"},
            )
            assert u_alpha_users.status_code == 200
            user_ids = {u["user_id"] for u in u_alpha_users.json()["data"]}
            assert user_ids == {"alice", "bob"}

            # 6. Query /v1/apps/me includes token_usage
            me = await client.get("/v1/apps/me", headers={"X-App-Id": "app-alpha"})
            assert me.status_code == 200
            assert me.json()["token_usage"]["total_tokens"] > 0

            # 7. Admin endpoint for app-beta filtered by source=embedding
            adm = await client.get("/v1/apps/app-beta/usage?source=embedding")
            assert adm.status_code == 200
            assert adm.json()["app_id"] == "app-beta"
            assert adm.json()["totals"]["request_count"] == 1
            assert adm.json()["by_source"][0]["source"] == "embedding"


@pytest.mark.asyncio
async def test_background_worker_records_compaction_and_extraction_usage():
    app, _ = build_test_app(auth_mode="dev")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            for idx in range(3):
                await client.post(
                    "/v1/chat/completions",
                    headers={
                        "X-App-Id": "worker-app",
                        "X-User-Id": "u-worker",
                        "X-Session-Id": "s-worker",
                        "X-Memory-Mode": "session_only",
                    },
                    json={"model": "coder", "messages": [{"role": "user", "content": f"Turn {idx}"}]},
                )

            comp = await client.post(
                "/v1/sessions/s-worker/compact",
                headers={"X-App-Id": "worker-app", "X-User-Id": "u-worker"},
            )
            assert comp.status_code == 200
            assert comp.json()["compacted"] is True

            await app.state.worker.extract_and_store_facts(
                PostTurnJob(
                    app_id="worker-app",
                    user_id="u-worker",
                    session_id="s-worker",
                    user_text="I build backend APIs with FastAPI and PostgreSQL.",
                    assistant_text="Great stack!",
                    requested_model_or_agent="coder",
                )
            )

            usage_resp = await client.get(
                "/v1/usage",
                headers={"X-App-Id": "worker-app", "X-User-Id": "u-worker"},
            )
            assert usage_resp.status_code == 200
            sources = {s["source"] for s in usage_resp.json()["by_source"]}
            assert {"chat", "memory_compact", "memory_extract", "memory_embed"}.issubset(sources)


@pytest.mark.asyncio
async def test_apply_migrations_runs_pending_in_order_and_skips_applied(tmp_path: Path):
    (tmp_path / "002_second.sql").write_text("CREATE TABLE t2 (id INT);", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("CREATE TABLE t1 (id INT);", encoding="utf-8")

    class FakeConn:
        def __init__(self) -> None:
            self.applied = {"001_first"}
            self.executed: list[str] = []

        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, sql: str, *args):
            self.executed.append(sql.strip())
            if sql.startswith("INSERT INTO schema_migrations"):
                self.applied.add(args[0])

        async def fetch(self, sql: str):
            return [{"version": v} for v in sorted(self.applied)]

    conn = FakeConn()
    newly = await apply_migrations(conn, migrations_dir=tmp_path)
    assert newly == ["002_second"]
    assert "CREATE TABLE t2 (id INT);" in conn.executed
    assert "CREATE TABLE t1 (id INT);" not in conn.executed
    assert conn.executed[-1].startswith("SELECT pg_advisory_unlock")


@pytest.mark.asyncio
async def test_zero_memory_skips_embedding_and_uses_lru_cache_and_cache_prompt():
    app, upstream_calls = build_test_app(auth_mode="dev")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            upstream_calls.clear()

            # 1. User has 0 memories -> should NOT hit /v1/embeddings during chat recall
            r1 = await client.post(
                "/v1/chat/completions",
                headers={"X-App-Id": "fast-app", "X-User-Id": "new-user"},
                json={"model": "coder", "messages": [{"role": "user", "content": "Fast turn"}]},
            )
            assert r1.status_code == 200
            assert "x-router-prep-ms" in r1.headers
            embed_calls = [c for c in upstream_calls if c["path"] == "/v1/embeddings"]
            chat_calls = [c for c in upstream_calls if c["path"] == "/v1/chat/completions"]
            assert len(embed_calls) == 0
            assert len(chat_calls) == 1
            assert chat_calls[0]["body"].get("cache_prompt") is True

            # 2. Store a memory for new-user (1 embedding call)
            r_mem = await client.post(
                "/v1/memories",
                headers={"X-App-Id": "fast-app", "X-User-Id": "new-user"},
                json={"category": "preference", "content": "Uses Python 3.12 and FastAPI"},
            )
            assert r_mem.status_code == 201
            upstream_calls.clear()

            # 3. Chat turn with same text twice -> first embeds, second hits EmbeddingClient LRU cache
            for _ in range(2):
                r_chat = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "X-App-Id": "fast-app",
                        "X-User-Id": "new-user",
                        "X-Memory-Mode": "read_only",
                    },
                    json={"model": "coder", "messages": [{"role": "user", "content": "Repeated query text"}]},
                )
                assert r_chat.status_code == 200

            embed_calls_after = [c for c in upstream_calls if c["path"] == "/v1/embeddings"]
            assert len(embed_calls_after) == 1
