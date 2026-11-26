"""Tenant-scoped session management and manual rolling-summary compaction endpoints."""

from __future__ import annotations

import secrets
from typing import Any

# pyrefly: ignore[missing-import]
from fastapi import APIRouter, HTTPException, Query, Request
# pyrefly: ignore[missing-import]
from pydantic import BaseModel, Field

from auth import resolve_request_scope
from db.repositories import SessionRepository
from memory.worker import MemoryWorker

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=200)
    model_id: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_hours: int | None = Field(default=None, ge=0, le=8760)


@router.post("", status_code=201)
async def create_or_update_session(
    payload: CreateSessionRequest, request: Request
) -> dict[str, Any]:
    body_hint = {"user": payload.user_id} if payload.user_id else None
    scope = await resolve_request_scope(request, body_hint, require_user=True)
    assert scope.user_id is not None

    session_id = (payload.session_id or "").strip() or f"sess_{secrets.token_hex(6)}"
    ttl = payload.ttl_hours if payload.ttl_hours is not None else scope.session_ttl_hours

    session_repo: SessionRepository = request.app.state.session_repo
    try:
        session = await session_repo.upsert_session(
            app_id=scope.app_id,
            user_id=scope.user_id,
            session_id=session_id,
            title=payload.title,
            model_id=payload.model_id,
            metadata=payload.metadata,
            ttl_hours=ttl,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"session": session}


@router.get("")
async def list_sessions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None
    session_repo: SessionRepository = request.app.state.session_repo
    try:
        sessions = await session_repo.list_sessions(
            scope.app_id, scope.user_id, limit=limit, offset=offset
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "object": "list",
        "app_id": scope.app_id,
        "user_id": scope.user_id,
        "data": sessions,
    }


@router.get("/{session_id}")
async def get_session_detail(
    session_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None
    session_repo: SessionRepository = request.app.state.session_repo
    try:
        session = await session_repo.get_session(scope.app_id, scope.user_id, session_id)
        if session is None:
            raise HTTPException(
                status_code=404, detail=f"Session '{session_id}' not found"
            )
        messages = await session_repo.get_messages(
            scope.app_id, scope.user_id, session_id, after_seq=0, limit=limit
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"session": session, "messages": messages}


@router.post("/{session_id}/compact")
async def compact_session_endpoint(session_id: str, request: Request) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None
    worker: MemoryWorker = request.app.state.worker
    try:
        result = await worker.compact_session(
            scope.app_id, scope.user_id, session_id, force=True
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Compaction failed: {exc}") from exc

    if result.get("reason") == "session_not_found":
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return result


@router.delete("/{session_id}")
async def delete_session_endpoint(session_id: str, request: Request) -> dict[str, Any]:
    scope = await resolve_request_scope(request, require_user=True)
    assert scope.user_id is not None
    session_repo: SessionRepository = request.app.state.session_repo
    try:
        deleted = await session_repo.delete_session(
            scope.app_id, scope.user_id, session_id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"deleted": True, "session_id": session_id}
