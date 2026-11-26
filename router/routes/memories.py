"""Tenant-scoped long-term semantic user memory CRUD, search, and GDPR user wipe endpoints."""

from __future__ import annotations

from typing import Any

# pyrefly: ignore[missing-import]
from fastapi import APIRouter, HTTPException, Query, Request
# pyrefly: ignore[missing-import]
from pydantic import BaseModel, Field

from auth import resolve_request_scope
from db.repositories import MemoryRepository, VALID_CATEGORIES
from memory.embeddings import EmbeddingClient

router = APIRouter(tags=["memories"])


class CreateMemoryRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=128)
    content: str = Field(..., min_length=2, max_length=1000)
    category: str = Field(default="fact")
    importance: int = Field(default=3, ge=1, le=5)
    source_session_id: str | None = Field(default=None, max_length=128)
    ttl_hours: int | None = Field(default=None, ge=0, le=87600)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchMemoryRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=128)
    query: str = Field(..., min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=50)
    min_similarity: float = Field(default=0.25, ge=-1.0, le=1.0)


class UpdateMemoryRequest(BaseModel):
    content: str | None = Field(default=None, min_length=2, max_length=1000)
    category: str | None = Field(default=None)
    importance: int | None = Field(default=None, ge=1, le=5)
    is_active: bool | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default=None)


@router.post("/v1/memories", status_code=201)
async def create_memory(payload: CreateMemoryRequest, request: Request) -> dict[str, Any]:
    body_hint = {"user": payload.user_id} if payload.user_id else None
    scope = await resolve_request_scope(request, body_hint, require_user=True)
    assert scope.user_id is not None

    category = payload.category.strip().lower()
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{category}'. Choose from: {sorted(VALID_CATEGORIES)}",
        )

    embedder: EmbeddingClient = request.app.state.embedder
    try:
        vec = await embedder.embed_text(payload.content, raise_on_error=True)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Embedding service error: {exc}"
        ) from exc
    assert vec is not None

    memory_repo: MemoryRepository = request.app.state.memory_repo
    try:
        memory, action = await memory_repo.upsert_semantic_memory(
            app_id=scope.app_id,
            user_id=scope.user_id,
            content=payload.content,
            embedding=vec,
            category=category,
            importance=payload.importance,
            confidence=1.0,
            source_session_id=payload.source_session_id,
            metadata=payload.metadata,
            ttl_hours=payload.ttl_hours,
            max_memories=request.app.state.config.max_memories_per_user,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"action": action, "memory": memory}


@router.get("/v1/memories")
async def list_memories(
    request: Request,
    category: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None
    memory_repo: MemoryRepository = request.app.state.memory_repo
    try:
        items = await memory_repo.list_memories(
            app_id=scope.app_id,
            user_id=scope.user_id,
            category=category,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "object": "list",
        "app_id": scope.app_id,
        "user_id": scope.user_id,
        "data": items,
    }


@router.post("/v1/memories/search")
async def search_memories(payload: SearchMemoryRequest, request: Request) -> dict[str, Any]:
    body_hint = {"user": payload.user_id} if payload.user_id else None
    scope = await resolve_request_scope(request, body_hint, require_user=True)
    assert scope.user_id is not None

    embedder: EmbeddingClient = request.app.state.embedder
    try:
        vec = await embedder.embed_text(payload.query, raise_on_error=True)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Embedding service error: {exc}"
        ) from exc
    assert vec is not None

    memory_repo: MemoryRepository = request.app.state.memory_repo
    try:
        results = await memory_repo.search_memories(
            app_id=scope.app_id,
            user_id=scope.user_id,
            query_embedding=vec,
            top_k=payload.top_k,
            min_similarity=payload.min_similarity,
            bump_access=False,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "object": "list",
        "app_id": scope.app_id,
        "user_id": scope.user_id,
        "query": payload.query,
        "data": results,
    }


@router.patch("/v1/memories/{memory_id}")
async def update_memory(
    memory_id: str, payload: UpdateMemoryRequest, request: Request
) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None

    if payload.category is not None and payload.category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{payload.category}'. Choose from: {sorted(VALID_CATEGORIES)}",
        )

    vec: list[float] | None = None
    if payload.content is not None:
        embedder: EmbeddingClient = request.app.state.embedder
        try:
            vec = await embedder.embed_text(payload.content, raise_on_error=True)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Embedding service error: {exc}"
            ) from exc

    memory_repo: MemoryRepository = request.app.state.memory_repo
    try:
        updated = await memory_repo.update_memory(
            app_id=scope.app_id,
            user_id=scope.user_id,
            memory_id=memory_id,
            content=payload.content,
            embedding=vec,
            category=payload.category,
            importance=payload.importance,
            is_active=payload.is_active,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid memory UUID") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if updated is None:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"memory": updated}


@router.delete("/v1/memories/{memory_id}")
async def delete_memory(memory_id: str, request: Request) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None
    memory_repo: MemoryRepository = request.app.state.memory_repo
    try:
        deleted = await memory_repo.delete_memory(
            scope.app_id, scope.user_id, memory_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid memory UUID") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return {"deleted": True, "memory_id": memory_id}


@router.delete("/v1/users/{user_id}/data")
async def wipe_user_tenant_data(user_id: str, request: Request) -> dict[str, Any]:
    """Delete all sessions, messages, and long-term memories for (app_id, user_id)."""
    scope = await resolve_request_scope(request, {"user": user_id}, require_user=True)
    if scope.user_id and scope.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Header X-User-Id '{scope.user_id}' conflicts with path user_id '{user_id}'",
        )
    memory_repo: MemoryRepository = request.app.state.memory_repo
    try:
        counts = await memory_repo.wipe_user_data(scope.app_id, user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"app_id": scope.app_id, "user_id": user_id, **counts}
