"""Tests for DSN normalization, deterministic token budgeting, and client message reconciliation."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from auth import RequestScope
from config import RouterConfig, normalize_database_url
from memory.assembler import ContextAssembler, reconcile_client_messages
from memory.embeddings import EmbeddingClient
from memory.tokens import compute_budget_plan, estimate_messages_tokens
from tests.conftest import FakeAppRepo, FakeMemoryRepo, FakeSessionRepo, deterministic_embed


def test_normalize_database_url_variants():
    assert (
        normalize_database_url("postgresql+psycopg2://dsbtek:secret@localhost/ai_models_db")
        == "postgresql://dsbtek:secret@localhost/ai_models_db"
    )
    assert (
        normalize_database_url("postgresql+asyncpg://u:p@127.0.0.1:5432/db")
        == "postgresql://u:p@127.0.0.1:5432/db"
    )
    assert (
        normalize_database_url("postgresql://u:p@localhost/db")
        == "postgresql://u:p@localhost/db"
    )
    assert (
        normalize_database_url(
            "postgresql+psycopg2://dsbtek:secret@localhost/ai_models_db",
            in_docker=True,
        )
        == "postgresql://dsbtek:secret@host.docker.internal/ai_models_db"
    )
    assert normalize_database_url("") == ""


def test_reconcile_client_messages_prevents_duplicates():
    stored = [
        {"role": "user", "content": "Hi, I am Alice."},
        {"role": "assistant", "content": "Hello Alice!"},
    ]
    # 1. Server-stateful caller sending only the new message
    prior, to_save = reconcile_client_messages(
        stored, [{"role": "user", "content": "What is my name?"}]
    )
    assert len(prior) == 2
    assert to_save == [{"role": "user", "content": "What is my name?"}]

    # 2. SDK client-history caller re-sending full history + 1 new message
    prior2, to_save2 = reconcile_client_messages(
        stored,
        [
            {"role": "user", "content": "Hi, I am Alice."},
            {"role": "assistant", "content": "Hello Alice!"},
            {"role": "user", "content": "What is my name?"},
        ],
    )
    assert len(prior2) == 2
    assert to_save2 == [{"role": "user", "content": "What is my name?"}]


@pytest.mark.asyncio
async def test_context_assembler_never_exceeds_2048_ctx_budget():
    cfg = RouterConfig(
        default_model="llama-3.2-1b",
        models={"all-minilm-l6-v2": "http://mock-embed:8080"},
        embedding_models={"all-minilm-l6-v2"},
        agents={},
        database_url="",
        ctx_size=2048,
    )
    app_repo = FakeAppRepo()
    session_repo = FakeSessionRepo(app_repo.sessions_ref)
    memory_repo = FakeMemoryRepo(app_repo.memories_ref, app_repo.sessions_ref)

    # Populate 5 long memories + rolling summary + 40 long conversation turns
    for i in range(5):
        txt = f"Durable architecture preference {i}: " + ("PostgreSQL pgvector FastAPI " * 12)
        await memory_repo.upsert_semantic_memory(
            "app1", "u1", txt, deterministic_embed(txt), category="preference", importance=5
        )

    long_turns = []
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        long_turns.append(
            {
                "role": role,
                "content": f"Turn {i}: " + ("Detailed microservices discussion about caching and latency. " * 6),
                "token_est": 110,
            }
        )
    await session_repo.append_messages("app1", "u1", "s1", long_turns)
    await session_repo.update_rolling_summary(
        "app1",
        "u1",
        "s1",
        expected_up_to_seq=0,
        new_up_to_seq=10,
        summary="Summary: " + ("Earlier architectural decisions and constraints. " * 25),
        summary_token_est=220,
    )

    mock_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"data": [{"embedding": deterministic_embed("PostgreSQL FastAPI architecture")}]},
            )
        )
    )
    try:
        embedder = EmbeddingClient(cfg, mock_http)
        assembler = ContextAssembler(cfg, session_repo, memory_repo, embedder)
        scope = RequestScope(
            app_id="app1",
            user_id="u1",
            session_id="s1",
            memory_mode="auto",
            authenticated=True,
        )
        payload = {
            "model": "llama-3.2-1b",
            "max_tokens": 900,  # Excessively high max_tokens should be safely clamped
            "messages": [
                {"role": "system", "content": "Custom system rules. " * 40},
                {"role": "user", "content": "Given our PostgreSQL FastAPI architecture, what next?"},
            ],
        }
        assembled = await assembler.assemble(
            scope, payload, agent_system_prompt="Agent base instructions. " * 25
        )

        budget = compute_budget_plan(ctx_size=2048, requested_max_tokens=900)
        assert payload["max_tokens"] == budget.clamped_max_tokens
        total_est = estimate_messages_tokens(assembled.messages)
        assert total_est <= budget.input_budget
        assert total_est + payload["max_tokens"] + budget.safety_margin <= 2048
        assert assembled.messages[-1]["content"].startswith("Given our PostgreSQL")
        assert assembled.used_rolling_summary is True
        assert len(assembled.retrieved_memories) >= 1
    finally:
        await mock_http.aclose()
