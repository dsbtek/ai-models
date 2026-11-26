"""OpenAI-compatible multi-model, virtual-agent & multi-tenant memory router for llama.cpp."""

from __future__ import annotations

from contextlib import asynccontextmanager
import codecs
import json
import logging
from pathlib import Path
import time
from typing import Any

# pyrefly: ignore[missing-import]
from fastapi import FastAPI, HTTPException, Request, Response
# pyrefly: ignore[missing-import]
from fastapi.middleware.cors import CORSMiddleware
# pyrefly: ignore[missing-import]
from fastapi.responses import RedirectResponse, StreamingResponse
# pyrefly: ignore[missing-import]
from fastapi.staticfiles import StaticFiles
# pyrefly: ignore[missing-import]
import httpx

from auth import RequestScope, resolve_request_scope
from config import RouterConfig, load_config
from db.pool import DatabaseManager
from db.repositories import (
    AppRepository,
    MemoryRepository,
    SessionRepository,
    UsageRepository,
)
from memory.assembler import AssembledContext, ContextAssembler
from memory.embeddings import EmbeddingClient
from memory.tokens import (
    estimate_messages_tokens,
    estimate_tokens,
    extract_usage_counts,
    flatten_message_content,
)
from memory.worker import MemoryWorker, PostTurnJob
from routes.apps import router as apps_router
from routes.memories import router as memories_router
from routes.sessions import router as sessions_router
from routes.usage import router as usage_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("router.app")

UI_DIR = Path(__file__).resolve().parent / "ui"


class SSEContentAccumulator:
    """Zero-buffering SSE parser that reconstructs assistant delta content on the fly."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._tail = ""
        self.parts: list[str] = []
        self.usage: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        self._tail += self._decoder.decode(chunk, final=False)
        while "\n" in self._tail:
            line, self._tail = self._tail.split("\n", 1)
            self._process_line(line.strip())

    def finish(self) -> str:
        self._tail += self._decoder.decode(b"", final=True)
        if self._tail.strip():
            self._process_line(self._tail.strip())
            self._tail = ""
        return "".join(self.parts)

    def _process_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            return
        try:
            event = json.loads(data_str)
            if isinstance(event.get("usage"), dict):
                self.usage = event["usage"]
            choices = event.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    self.parts.append(content)
        except json.JSONDecodeError:
            pass


def _resolve_model_or_agent(
    config: RouterConfig,
    payload: dict[str, Any],
    upstream_path: str,
    allowed_models: list[str] | None = None,
) -> tuple[str, str, str | None]:
    """
    Resolve target backend URL, requested ID, and optional agent system_prompt.
    Does NOT mutate messages yet if ContextAssembler will assemble them.
    """
    requested_id = str(payload.get("model") or config.default_model)
    if allowed_models and requested_id not in allowed_models:
        raise HTTPException(
            status_code=403,
            detail=f"Model/agent '{requested_id}' is not permitted for this app",
        )

    if requested_id in config.models:
        return f"{config.models[requested_id]}{upstream_path}", requested_id, None

    if requested_id in config.agents:
        agent = config.agents[requested_id]
        backend_id = agent["backend"]
        payload["model"] = backend_id
        for key, value in agent.get("defaults", {}).items():
            payload.setdefault(key, value)
        system_prompt = (
            agent.get("system_prompt")
            if upstream_path == "/v1/chat/completions"
            else None
        )
        return f"{config.models[backend_id]}{upstream_path}", requested_id, system_prompt

    raise HTTPException(
        status_code=404,
        detail={
            "error": f"Unknown model or agent '{requested_id}'",
            "available_models": list(config.models.keys()),
            "available_agents": list(config.agents.keys()),
        },
    )


def _inject_stateless_system_prompt(
    payload: dict[str, Any], system_prompt: str | None
) -> None:
    if not system_prompt:
        return
    messages = list(payload.get("messages") or [])
    if messages and messages[0].get("role") == "system":
        existing = flatten_message_content(messages[0].get("content"))
        messages[0] = {
            **messages[0],
            "content": f"{system_prompt}\n\n{existing}".strip(),
        }
    else:
        messages.insert(0, {"role": "system", "content": system_prompt})
    payload["messages"] = messages


async def _safe_record_usage(
    app: FastAPI,
    *,
    app_id: str,
    user_id: str | None,
    model_id: str,
    source: str,
    resp_json: dict[str, Any] | None,
    fallback_prompt: int,
    fallback_completion: int,
) -> None:
    if not app.state.db.is_configured:
        return
    usage_repo: UsageRepository | None = getattr(app.state, "usage_repo", None)
    if usage_repo is None:
        return
    p_tok, c_tok, t_tok = extract_usage_counts(
        resp_json,
        fallback_prompt=fallback_prompt,
        fallback_completion=fallback_completion,
    )
    try:
        await usage_repo.record_usage(
            app_id=app_id,
            user_id=user_id,
            model_id=model_id,
            source=source,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
        )
    except Exception as exc:
        logger.warning("Failed to record %s token usage: %s", source, exc)


async def _record_completed_turn(
    app: FastAPI,
    scope: RequestScope,
    assembled: AssembledContext,
    assistant_text: str,
    requested_id: str,
) -> None:
    """Persist new user + assistant messages and enqueue background compaction/extraction."""
    cleaned_reply = assistant_text.strip()
    if not cleaned_reply:
        return

    if scope.session_id and scope.user_id and scope.memory_mode in {"auto", "session_only"}:
        to_save = [
            *assembled.new_messages_to_persist,
            {
                "role": "assistant",
                "content": cleaned_reply,
                "token_est": estimate_tokens(cleaned_reply),
                "model_id": requested_id,
            },
        ]
        try:
            await app.state.session_repo.append_messages(
                app_id=scope.app_id,
                user_id=scope.user_id,
                session_id=scope.session_id,
                messages=to_save,
                model_id=requested_id,
                ttl_hours=scope.session_ttl_hours,
            )
        except Exception as exc:
            logger.error("Failed to persist session messages: %s", exc)

    if scope.user_id and scope.memory_mode in {"auto", "session_only"}:
        app_extract_pref = scope.app_settings.get("extract_memories", True)
        should_extract = bool(scope.memory_mode == "auto" and app_extract_pref)
        should_compact = bool(scope.session_id)
        if should_extract or should_compact:
            app.state.worker.enqueue(
                PostTurnJob(
                    app_id=scope.app_id,
                    user_id=scope.user_id,
                    session_id=scope.session_id,
                    user_text=assembled.latest_user_text,
                    assistant_text=cleaned_reply,
                    requested_model_or_agent=requested_id,
                    extract_memories=should_extract,
                    compact_session=should_compact,
                )
            )


def create_app(
    custom_config: RouterConfig | None = None,
    custom_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    config = custom_config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        owns_client = custom_http_client is None
        if custom_http_client is not None:
            app.state.client = custom_http_client
        else:
            timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            app.state.client = httpx.AsyncClient(timeout=timeout)

        db = DatabaseManager(config.database_url, seed_keys=config.app_api_keys_seed)
        await db.connect()

        app.state.db = db
        app.state.app_repo = AppRepository(db)
        app.state.session_repo = SessionRepository(db)
        app.state.memory_repo = MemoryRepository(db)
        app.state.usage_repo = UsageRepository(db)
        app.state.embedder = EmbeddingClient(config, app.state.client)
        app.state.assembler = ContextAssembler(
            config,
            app.state.session_repo,
            app.state.memory_repo,
            app.state.embedder,
            app.state.usage_repo,
        )
        app.state.worker = MemoryWorker(
            config,
            app.state.client,
            app.state.session_repo,
            app.state.memory_repo,
            app.state.embedder,
            app.state.usage_repo,
        )
        app.state.worker.start()

        try:
            yield
        finally:
            await app.state.worker.stop()
            await db.close()
            if owns_client:
                await app.state.client.aclose()

    fastapi_app = FastAPI(
        title="llama.cpp Multi-Model, Agent & Memory Router",
        version="2.0.0",
        lifespan=lifespan,
    )

    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-App-Id",
            "X-Session-Id",
            "X-Memories-Recalled",
            "X-Context-Tokens",
        ],
    )

    fastapi_app.include_router(apps_router)
    fastapi_app.include_router(sessions_router)
    fastapi_app.include_router(memories_router)
    fastapi_app.include_router(usage_router)

    @fastapi_app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/playground")

    @fastapi_app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Check health of llama-server backends, PostgreSQL memory store, and background worker."""
        client: httpx.AsyncClient = request.app.state.client
        cfg: RouterConfig = request.app.state.config
        statuses: dict[str, str] = {}
        backends_ok = True

        for model_id, base_url in cfg.models.items():
            try:
                resp = await client.get(f"{base_url}/health", timeout=3.0)
                statuses[model_id] = (
                    "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
                )
                if resp.status_code != 200:
                    backends_ok = False
            except httpx.HTTPError:
                statuses[model_id] = "unreachable"
                backends_ok = False

        db_health = await request.app.state.db.health_status()
        db_ok = db_health.get("status") in {"ok", "disabled"}

        return {
            "status": "ok" if (backends_ok and db_ok) else "degraded",
            "auth_mode": cfg.auth_mode,
            "ctx_size": cfg.ctx_size,
            "default_model": cfg.default_model,
            "backends": statuses,
            "agents": list(cfg.agents.keys()),
            "memory_db": db_health,
            "worker_queue_depth": request.app.state.worker.queue_depth,
        }

    @fastapi_app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        """Return OpenAI-compatible list of base models and virtual agents with preset metadata."""
        await resolve_request_scope(request)
        cfg: RouterConfig = request.app.state.config
        now = int(time.time())
        data: list[dict[str, Any]] = []

        for model_id in cfg.models:
            m_type = "embedding" if model_id in cfg.embedding_models else "base"
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "llama.cpp",
                    "type": m_type,
                }
            )

        for agent_id, agent_cfg in cfg.agents.items():
            data.append(
                {
                    "id": agent_id,
                    "object": "model",
                    "created": now,
                    "owned_by": f"agent:{agent_cfg['backend']}",
                    "type": "agent",
                    "backend": agent_cfg["backend"],
                    "description": agent_cfg.get("description", ""),
                    "system_prompt": agent_cfg.get("system_prompt", ""),
                    "defaults": agent_cfg.get("defaults", {}),
                    "extract_memories": agent_cfg.get("extract_memories", True),
                }
            )

        return {"object": "list", "data": data}

    @fastapi_app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")

        scope = await resolve_request_scope(
            request, payload, strip_router_fields=True
        )
        cfg: RouterConfig = request.app.state.config
        allowed_models = scope.app_settings.get("allowed_models")
        target_url, requested_id, agent_sys_prompt = _resolve_model_or_agent(
            cfg,
            payload,
            "/v1/chat/completions",
            allowed_models if isinstance(allowed_models, list) else None,
        )

        use_memory = bool(
            scope.memory_mode != "off"
            and (scope.user_id or scope.session_id)
            and request.app.state.db.is_configured
        )

        assembled: AssembledContext | None = None
        if use_memory:
            assembled = await request.app.state.assembler.assemble(
                scope, payload, agent_sys_prompt
            )
        else:
            _inject_stateless_system_prompt(payload, agent_sys_prompt)

        extra_headers: dict[str, str] = {"X-App-Id": scope.app_id}
        if scope.session_id:
            extra_headers["X-Session-Id"] = scope.session_id
        if assembled is not None:
            extra_headers["X-Memories-Recalled"] = str(len(assembled.retrieved_memories))
            extra_headers["X-Context-Tokens"] = str(assembled.input_tokens)

        client: httpx.AsyncClient = request.app.state.client
        worker: MemoryWorker = request.app.state.worker
        is_stream = bool(payload.get("stream", False))

        if not is_stream:
            worker.notify_inference_start()
            try:
                resp = await client.post(target_url, json=payload)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"Backend error: {exc}") from exc
            finally:
                worker.notify_inference_end()

            if resp.status_code == 200:
                body_json: dict[str, Any] | None = None
                assistant_text = ""
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        body_json = parsed
                        choices = parsed.get("choices") or []
                        assistant_text = (
                            (choices[0].get("message") or {}).get("content", "")
                            if choices
                            else ""
                        )
                except Exception:
                    assistant_text = ""

                fallback_prompt = (
                    assembled.input_tokens
                    if assembled is not None
                    else estimate_messages_tokens(list(payload.get("messages") or []))
                )
                await _safe_record_usage(
                    request.app,
                    app_id=scope.app_id,
                    user_id=scope.user_id,
                    model_id=requested_id,
                    source="chat",
                    resp_json=body_json,
                    fallback_prompt=fallback_prompt,
                    fallback_completion=estimate_tokens(assistant_text),
                )
                if assembled is not None and assistant_text:
                    await _record_completed_turn(
                        request.app, scope, assembled, assistant_text, requested_id
                    )

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=extra_headers,
            )

        worker.notify_inference_start()
        req = client.build_request("POST", target_url, json=payload)
        try:
            upstream_resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            worker.notify_inference_end()
            raise HTTPException(status_code=502, detail=f"Backend error: {exc}") from exc

        accumulator = SSEContentAccumulator()

        async def stream_body():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    if upstream_resp.status_code == 200:
                        accumulator.feed(chunk)
                    yield chunk
            finally:
                await upstream_resp.aclose()
                worker.notify_inference_end()
                if upstream_resp.status_code == 200:
                    full_reply = accumulator.finish()
                    fallback_prompt = (
                        assembled.input_tokens
                        if assembled is not None
                        else estimate_messages_tokens(list(payload.get("messages") or []))
                    )
                    await _safe_record_usage(
                        request.app,
                        app_id=scope.app_id,
                        user_id=scope.user_id,
                        model_id=requested_id,
                        source="chat",
                        resp_json={"usage": accumulator.usage} if accumulator.usage else None,
                        fallback_prompt=fallback_prompt,
                        fallback_completion=estimate_tokens(full_reply),
                    )
                    if assembled is not None and full_reply:
                        await _record_completed_turn(
                            request.app, scope, assembled, full_reply, requested_id
                        )

        return StreamingResponse(
            stream_body(),
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **extra_headers,
            },
        )

    async def _proxy_simple(
        request: Request, upstream_path: str, source: str
    ) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")

        scope = await resolve_request_scope(request, payload, strip_router_fields=True)
        cfg: RouterConfig = request.app.state.config
        allowed_models = scope.app_settings.get("allowed_models")
        target_url, requested_id, _ = _resolve_model_or_agent(
            cfg,
            payload,
            upstream_path,
            allowed_models if isinstance(allowed_models, list) else None,
        )
        client: httpx.AsyncClient = request.app.state.client
        try:
            resp = await client.post(target_url, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Backend error: {exc}") from exc

        if resp.status_code == 200:
            body_json: dict[str, Any] | None = None
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body_json = parsed
            except Exception:
                body_json = None
            raw_input = payload.get("prompt") if source == "completion" else payload.get("input")
            fallback_prompt = estimate_tokens(flatten_message_content(raw_input))
            fallback_completion = 0
            if source == "completion" and body_json:
                choices = body_json.get("choices") or []
                text = str(choices[0].get("text") or "") if choices else ""
                fallback_completion = estimate_tokens(text)
            await _safe_record_usage(
                request.app,
                app_id=scope.app_id,
                user_id=scope.user_id,
                model_id=requested_id,
                source=source,
                resp_json=body_json,
                fallback_prompt=fallback_prompt,
                fallback_completion=fallback_completion,
            )

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={"X-App-Id": scope.app_id},
        )

    @fastapi_app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        return await _proxy_simple(request, "/v1/completions", "completion")

    @fastapi_app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await _proxy_simple(request, "/v1/embeddings", "embedding")

    UI_DIR.mkdir(parents=True, exist_ok=True)
    fastapi_app.mount(
        "/playground",
        StaticFiles(directory=str(UI_DIR), html=True),
        name="playground",
    )

    return fastapi_app


app = create_app()
