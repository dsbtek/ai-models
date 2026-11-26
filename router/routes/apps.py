"""Integrated application registration, self-inspection, key rotation, and revocation endpoints."""

from __future__ import annotations

from typing import Any

# pyrefly: ignore[missing-import]
from fastapi import APIRouter, HTTPException, Query, Request
# pyrefly: ignore[missing-import]
from pydantic import BaseModel, Field

from auth import require_admin, resolve_request_scope
from db.repositories import AppRepository

router = APIRouter(prefix="/v1/apps", tags=["apps"])


class CreateAppRequest(BaseModel):
    app_id: str = Field(..., min_length=2, max_length=63)
    name: str = Field(default="", max_length=120)
    settings: dict[str, Any] = Field(default_factory=dict)


@router.get("/me")
async def get_current_app(request: Request) -> dict[str, Any]:
    """Inspect the resolved application identity and memory statistics."""
    scope = await resolve_request_scope(request)
    app_repo: AppRepository = request.app.state.app_repo
    app_info = None
    if request.app.state.db.is_configured:
        try:
            app_info = await app_repo.get_app(scope.app_id)
        except Exception:
            app_info = None
    return {
        "app_id": scope.app_id,
        "authenticated": scope.authenticated,
        "auth_mode": request.app.state.config.auth_mode,
        "app": app_info,
    }


@router.post("", status_code=201)
async def register_app(payload: CreateAppRequest, request: Request) -> dict[str, Any]:
    """Register an integrated app and return its one-time plaintext ak_... API key."""
    require_admin(request)
    app_repo: AppRepository = request.app.state.app_repo
    try:
        app_record, raw_key = await app_repo.create_app(
            app_id=payload.app_id.strip(),
            name=payload.name.strip() or payload.app_id.strip(),
            settings=payload.settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "app": app_record,
        "api_key": raw_key,
        "note": "Save this api_key securely; only its SHA-256 hash is stored on the server.",
    }


@router.get("")
async def list_apps(request: Request) -> dict[str, Any]:
    """List all registered and sandbox apps with session and memory counts."""
    require_admin(request)
    app_repo: AppRepository = request.app.state.app_repo
    try:
        apps = await app_repo.list_apps()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"object": "list", "data": apps}


@router.post("/{app_id}/rotate-key")
async def rotate_app_key(app_id: str, request: Request) -> dict[str, Any]:
    """Issue a new ak_... API key for an existing app, invalidating the previous key immediately."""
    require_admin(request)
    app_repo: AppRepository = request.app.state.app_repo
    try:
        result = await app_repo.rotate_key(app_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")
    app_record, raw_key = result
    return {
        "app": app_record,
        "api_key": raw_key,
        "note": "Previous API key has been invalidated.",
    }


@router.delete("/{app_id}")
async def delete_or_revoke_app(
    app_id: str,
    request: Request,
    hard: bool = Query(default=False, description="Cascade delete app, sessions, and memories"),
) -> dict[str, Any]:
    """Deactivate an app's API key (default) or permanently delete all its isolated tenant data."""
    require_admin(request)
    app_repo: AppRepository = request.app.state.app_repo
    try:
        ok = await app_repo.delete_or_deactivate(app_id, hard=hard)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not ok:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")
    return {
        "app_id": app_id,
        "action": "deleted" if hard else "deactivated",
    }
