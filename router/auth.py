"""Authentication, anti-spoofing app guard, and multi-tenant request scope resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import secrets
from typing import Any

# pyrefly: ignore[missing-import]
from fastapi import HTTPException, Request

from config import RouterConfig
from db.repositories import AppRepository

PLACEHOLDER_BEARER_TOKENS = frozenset(
    {
        "",
        "not-needed",
        "sk-local",
        "sk-no-key-required",
        "test",
        "demo",
        "none",
        "null",
        "ollama",
        "lm-studio",
    }
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
VALID_MEMORY_MODES = frozenset({"auto", "read_only", "session_only", "off"})

ROUTER_EXTRA_BODY_KEYS = (
    "app_id",
    "session_id",
    "memory_mode",
    "session_ttl_hours",
)


@dataclass
class RequestScope:
    app_id: str
    user_id: str | None = None
    session_id: str | None = None
    memory_mode: str = "off"
    authenticated: bool = False
    app_name: str = ""
    app_settings: dict[str, Any] = field(default_factory=dict)
    session_ttl_hours: int | None = None


def extract_bearer_token(request: Request) -> str:
    auth_header = (request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _clean_opt_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


async def resolve_request_scope(
    request: Request,
    body: dict[str, Any] | None = None,
    *,
    require_user: bool = False,
    strip_router_fields: bool = False,
) -> RequestScope:
    """Resolve and validate (app_id, user_id, session_id, memory_mode) for an incoming request."""
    config: RouterConfig = request.app.state.config
    app_repo: AppRepository = request.app.state.app_repo
    db_ready = request.app.state.db.is_configured

    metadata = (body or {}).get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    requested_app_id = (
        _clean_opt_str(request.headers.get("x-app-id"))
        or _clean_opt_str(request.query_params.get("app_id"))
        or _clean_opt_str((body or {}).get("app_id"))
        or _clean_opt_str(metadata.get("app_id"))
    )
    user_id = (
        _clean_opt_str(request.headers.get("x-user-id"))
        or _clean_opt_str(request.query_params.get("user_id"))
        or _clean_opt_str((body or {}).get("user"))
        or _clean_opt_str(metadata.get("user_id"))
    )
    session_id = (
        _clean_opt_str(request.headers.get("x-session-id"))
        or _clean_opt_str(request.query_params.get("session_id"))
        or _clean_opt_str((body or {}).get("session_id"))
        or _clean_opt_str(metadata.get("session_id"))
    )
    raw_mode = (
        _clean_opt_str(request.headers.get("x-memory-mode"))
        or _clean_opt_str(request.query_params.get("memory_mode"))
        or _clean_opt_str((body or {}).get("memory_mode"))
        or _clean_opt_str(metadata.get("memory_mode"))
        or ""
    ).lower()

    ttl_raw = (
        request.headers.get("x-session-ttl-hours")
        or (body or {}).get("session_ttl_hours")
        or metadata.get("session_ttl_hours")
    )
    session_ttl_hours: int | None = None
    if ttl_raw is not None and str(ttl_raw).strip() != "":
        try:
            session_ttl_hours = max(0, int(ttl_raw))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="session_ttl_hours must be an integer"
            ) from exc

    if strip_router_fields and isinstance(body, dict):
        for k in ROUTER_EXTRA_BODY_KEYS:
            body.pop(k, None)

    token = extract_bearer_token(request)
    is_placeholder = token.lower() in PLACEHOLDER_BEARER_TOKENS

    app_id = "dev-sandbox"
    app_name = "Dev Sandbox"
    authenticated = False
    app_settings: dict[str, Any] = {}

    # Case 1: Caller sent an explicit app API key (ak_...) or non-placeholder key
    if token and not is_placeholder:
        if not db_ready:
            raise HTTPException(
                status_code=503,
                detail="Memory database is unavailable to verify API key",
            )
        app_record = await app_repo.verify_api_key(token)
        if app_record is None or not app_record.get("is_active", False):
            # In dev mode, only reject if it looks like a real ak_ key or strict mode is active;
            # actually any non-placeholder wrong key should fail clearly with 401!
            raise HTTPException(
                status_code=401,
                detail="Invalid or deactivated app API key",
            )
        if requested_app_id and requested_app_id != app_record["app_id"]:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"X-App-Id '{requested_app_id}' does not match "
                    f"authenticated app '{app_record['app_id']}'"
                ),
            )
        app_id = app_record["app_id"]
        app_name = app_record["name"]
        authenticated = True
        app_settings = app_record.get("settings") or {}

    # Case 2: No API key (or SDK dummy placeholder like "not-needed")
    else:
        if config.auth_mode == "strict":
            raise HTTPException(
                status_code=401,
                detail="Authentication required: provide a registered Bearer ak_... API key",
            )
        app_id = requested_app_id or "dev-sandbox"
        needs_tenant_store = bool(
            db_ready and (user_id or session_id or require_user or requested_app_id)
        )
        if needs_tenant_store:
            try:
                sandbox_app = await app_repo.ensure_sandbox_app(app_id)
                app_name = sandbox_app["name"]
                app_settings = sandbox_app.get("settings") or {}
            except PermissionError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except RuntimeError:
                # If DB is unreachable during stateless call without memory, allow graceful fallback
                if require_user:
                    raise HTTPException(
                        status_code=503, detail="Memory database is unreachable"
                    )

    # User & session validation
    if session_id and not user_id:
        if config.auth_mode == "dev":
            user_id = "dev-user"
        else:
            raise HTTPException(
                status_code=400,
                detail="user_id (X-User-Id header or 'user' field) is required when session_id is set",
            )

    if require_user and not user_id:
        raise HTTPException(
            status_code=400,
            detail="user_id is required (pass X-User-Id header or ?user_id= query parameter)",
        )

    if raw_mode in VALID_MEMORY_MODES:
        memory_mode = raw_mode
    elif user_id or session_id:
        memory_mode = "auto"
    else:
        memory_mode = "off"

    if session_ttl_hours is None:
        app_ttl = app_settings.get("session_ttl_hours")
        if isinstance(app_ttl, int) and app_ttl > 0:
            session_ttl_hours = app_ttl
        elif config.session_default_ttl_hours > 0:
            session_ttl_hours = config.session_default_ttl_hours

    return RequestScope(
        app_id=app_id,
        user_id=user_id,
        session_id=session_id,
        memory_mode=memory_mode,
        authenticated=authenticated,
        app_name=app_name,
        app_settings=app_settings,
        session_ttl_hours=session_ttl_hours,
    )


def require_admin(request: Request) -> None:
    """Protect /v1/apps registration & rotation endpoints."""
    config: RouterConfig = request.app.state.config
    provided = (
        (request.headers.get("x-admin-key") or "").strip()
        or extract_bearer_token(request)
    )

    if config.admin_api_key:
        if not provided or not secrets.compare_digest(provided, config.admin_api_key):
            raise HTTPException(
                status_code=401,
                detail="Valid X-Admin-Key or Bearer ADMIN_API_KEY is required",
            )
        return

    # No ADMIN_API_KEY configured: permit loopback & Docker-gateway/LAN private IPs in dev mode
    client_host = request.client.host if request.client else ""
    if config.auth_mode == "dev":
        if client_host in LOOPBACK_HOSTS:
            return
        try:
            ip = ipaddress.ip_address(client_host)
            if ip.is_loopback or ip.is_private:
                return
        except ValueError:
            pass

    raise HTTPException(
        status_code=403,
        detail="Set ADMIN_API_KEY in .env to manage apps remotely or in strict mode",
    )
