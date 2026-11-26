"""Tenant-isolated token usage reporting endpoints per app and user."""

from __future__ import annotations

from datetime import date
from typing import Any

# pyrefly: ignore[missing-import]
from fastapi import APIRouter, HTTPException, Query, Request

from auth import require_admin, resolve_request_scope
from db.repositories import UsageRepository, VALID_USAGE_SOURCES

router = APIRouter(tags=["usage"])


def _validate_source(source: str | None) -> str | None:
    if source is None:
        return None
    cleaned = source.strip().lower()
    if not cleaned:
        return None
    if cleaned not in VALID_USAGE_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source '{source}'. Choose from: {sorted(VALID_USAGE_SOURCES)}",
        )
    return cleaned


@router.get("/v1/usage")
async def get_usage(
    request: Request,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    model_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return token usage totals and breakdowns for the resolved app (and optional user)."""
    scope = await resolve_request_scope(request)
    valid_source = _validate_source(source)
    usage_repo: UsageRepository = request.app.state.usage_repo

    try:
        summary = await usage_repo.get_usage_summary(
            app_id=scope.app_id,
            user_id=scope.user_id,
            start_date=start_date,
            end_date=end_date,
            model_id=model_id.strip() if model_id else None,
            source=valid_source,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "app_id": scope.app_id,
        "user_id": scope.user_id,
        **summary,
    }


@router.get("/v1/usage/users")
async def list_app_user_usage(
    request: Request,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List per-user token usage totals within the resolved app."""
    scope = await resolve_request_scope(request)
    usage_repo: UsageRepository = request.app.state.usage_repo
    try:
        users = await usage_repo.list_user_usage(
            app_id=scope.app_id,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "object": "list",
        "app_id": scope.app_id,
        "data": users,
    }


@router.get("/v1/apps/{app_id}/usage")
async def admin_get_app_usage(
    app_id: str,
    request: Request,
    user_id: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    model_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
) -> dict[str, Any]:
    """Admin endpoint to inspect token usage for any registered or sandbox app."""
    require_admin(request)
    valid_source = _validate_source(source)
    usage_repo: UsageRepository = request.app.state.usage_repo
    try:
        summary = await usage_repo.get_usage_summary(
            app_id=app_id,
            user_id=user_id.strip() if user_id is not None else None,
            start_date=start_date,
            end_date=end_date,
            model_id=model_id.strip() if model_id else None,
            source=valid_source,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "app_id": app_id,
        "user_id": user_id.strip() if user_id is not None else None,
        **summary,
    }
