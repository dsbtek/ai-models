"""Tests for SSE streaming delta capture, memory deduplication/supersede, compaction, and Playground UI."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app import SSEContentAccumulator
from tests.conftest import build_test_app


def test_sse_accumulator_handles_split_chunks_and_utf8():
    acc = SSEContentAccumulator()
    raw = (
        'data: {"choices":[{"delta":{"role":"assistant","content":"Héllo "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"world 🚀"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode("utf-8")
    # Feed byte-by-byte to stress arbitrary TCP/chunk boundaries
    for i in range(0, len(raw), 5):
        acc.feed(raw[i : i + 5])
    assert acc.finish() == "Héllo world 🚀"


@pytest.mark.asyncio
async def test_streaming_chat_persists_turns_and_compacts_and_serves_playground():
    app, _ = build_test_app(auth_mode="dev")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Playground routes
            r_root = await client.get("/", follow_redirects=False)
            assert r_root.status_code == 307
            assert r_root.headers["location"] == "/playground"

            r_ui = await client.get("/playground/")
            assert r_ui.status_code == 200
            assert "llama.cpp Studio" in r_ui.text

            # 2. Streaming SSE chat turn persists both user & reconstructed assistant message
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={
                    "X-App-Id": "stream-test",
                    "X-User-Id": "u1",
                    "X-Session-Id": "s1",
                    "X-Memory-Mode": "session_only",
                },
                json={
                    "model": "coder",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Stream a greeting please"}],
                },
            ) as resp:
                assert resp.status_code == 200
                body_bytes = await resp.aread()
                assert b"streaming world!" in body_bytes

            # Add one more turn so manual compaction has >2 messages
            await client.post(
                "/v1/chat/completions",
                headers={
                    "X-App-Id": "stream-test",
                    "X-User-Id": "u1",
                    "X-Session-Id": "s1",
                    "X-Memory-Mode": "session_only",
                },
                json={
                    "model": "coder",
                    "messages": [{"role": "user", "content": "Second turn in session"}],
                },
            )

            s_detail = await client.get(
                "/v1/sessions/s1",
                headers={"X-App-Id": "stream-test", "X-User-Id": "u1"},
            )
            assert s_detail.status_code == 200
            msgs = s_detail.json()["messages"]
            assert len(msgs) == 4
            assert msgs[1]["role"] == "assistant"
            assert msgs[1]["content"] == "Hello streaming world!"

            # 3. Manual session compaction
            r_comp = await client.post(
                "/v1/sessions/s1/compact",
                headers={"X-App-Id": "stream-test", "X-User-Id": "u1"},
            )
            assert r_comp.status_code == 200
            assert r_comp.json()["compacted"] is True
            assert "FastAPI + PostgreSQL" in r_comp.json()["rolling_summary"]

            # 4. Semantic memory exact deduplication & user wipe
            r_m1 = await client.post(
                "/v1/memories",
                headers={"X-App-Id": "stream-test", "X-User-Id": "u1"},
                json={"category": "preference", "importance": 3, "content": "Prefers dark mode in IDEs"},
            )
            assert r_m1.json()["action"] == "created"

            r_m2 = await client.post(
                "/v1/memories",
                headers={"X-App-Id": "stream-test", "X-User-Id": "u1"},
                json={"category": "preference", "importance": 5, "content": "prefers dark mode in ides"},
            )
            assert r_m2.json()["action"] == "deduplicated"
            assert r_m2.json()["memory"]["importance"] == 5

            r_wipe = await client.delete(
                "/v1/users/u1/data",
                headers={"X-App-Id": "stream-test"},
            )
            assert r_wipe.status_code == 200
            assert r_wipe.json()["deleted_memories"] == 1
            assert r_wipe.json()["deleted_sessions"] == 1
