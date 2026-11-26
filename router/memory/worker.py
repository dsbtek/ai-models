"""Single-worker background queue for rolling session compaction, fact extraction, and TTL cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any

# pyrefly: ignore[missing-import]
import httpx

from config import RouterConfig
from db.repositories import MemoryRepository, SessionRepository, UsageRepository, VALID_CATEGORIES
from .embeddings import EmbeddingClient
from .tokens import estimate_tokens, extract_usage_counts, flatten_message_content, truncate_to_tokens

logger = logging.getLogger("router.memory.worker")

TRIVIAL_INPUTS = frozenset(
    {
        "ok",
        "okay",
        "k",
        "thanks",
        "thank you",
        "thx",
        "hi",
        "hello",
        "hey",
        "yes",
        "no",
        "cool",
        "got it",
        "sure",
        "bye",
        "continue",
    }
)

FACT_EXTRACTION_SYSTEM_PROMPT = """You extract durable, long-term facts about the USER from a conversation turn.
Return ONLY a valid JSON array (no markdown fences, no prose).
Each element must be an object:
{"category": "preference|profile|fact|goal|constraint|episodic", "content": "concise third-person statement", "importance": 1-5}

Rules:
- Extract ONLY stable facts, preferences, tech stack choices, role/profile details, ongoing goals, or explicit constraints stated by the USER.
- Ignore greetings, temporary one-off math/translation questions, generic coding questions without personal/project context, and assistant-invented examples.
- Extract at most 3 items. If there are NO durable user facts in the user message, return []"""

ROLLING_SUMMARY_SYSTEM_PROMPT = """Update the conversation summary concisely in under 140 words.
Preserve key user goals, decisions, named entities, technical choices, and unresolved questions.
Return ONLY the plain summary text without headings or preamble."""


@dataclass
class PostTurnJob:
    app_id: str
    user_id: str
    session_id: str | None
    user_text: str
    assistant_text: str
    requested_model_or_agent: str
    extract_memories: bool = True
    compact_session: bool = True


def _parse_json_array(raw_text: str) -> list[dict[str, Any]]:
    """Extract a JSON array cleanly even if a small model wraps it in code fences."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(cleaned[start : end + 1])
        return [x for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def should_extract_facts(user_text: str) -> bool:
    stripped = user_text.strip()
    if len(stripped) < 15:
        return False
    if stripped.lower().rstrip(".!?") in TRIVIAL_INPUTS:
        return False
    return True


class MemoryWorker:
    """Bounded single-concurrency background worker to prevent CPU contention with live chats."""

    def __init__(
        self,
        config: RouterConfig,
        http_client: httpx.AsyncClient,
        session_repo: SessionRepository,
        memory_repo: MemoryRepository,
        embedder: EmbeddingClient,
        usage_repo: UsageRepository | None = None,
    ) -> None:
        self.config = config
        self.http_client = http_client
        self.session_repo = session_repo
        self.memory_repo = memory_repo
        self.embedder = embedder
        self.usage_repo = usage_repo
        self.queue: asyncio.Queue[PostTurnJob] = asyncio.Queue(maxsize=256)
        self._worker_task: asyncio.Task[None] | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._compact_lock = asyncio.Lock()
        self._active_inferences = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    async def _record_usage(
        self,
        *,
        app_id: str,
        user_id: str | None,
        model_id: str,
        source: str,
        resp_json: dict[str, Any] | None,
        fallback_prompt: int,
        fallback_completion: int,
    ) -> None:
        if self.usage_repo is None:
            return
        p_tok, c_tok, t_tok = extract_usage_counts(
            resp_json,
            fallback_prompt=fallback_prompt,
            fallback_completion=fallback_completion,
        )
        try:
            await self.usage_repo.record_usage(
                app_id=app_id,
                user_id=user_id,
                model_id=model_id,
                source=source,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
            )
        except Exception as exc:
            logger.warning("Failed to record background %s token usage: %s", source, exc)

    def notify_inference_start(self) -> None:
        self._active_inferences += 1
        self._idle_event.clear()

    def notify_inference_end(self) -> None:
        self._active_inferences = max(0, self._active_inferences - 1)
        if self._active_inferences == 0:
            self._idle_event.set()

    async def _wait_for_cpu_idle(self, quiet_seconds: float = 0.8) -> None:
        """Yield CPU completely to active user chats before running background LLM jobs."""
        while True:
            await self._idle_event.wait()
            if quiet_seconds <= 0:
                return
            await asyncio.sleep(quiet_seconds)
            if self._idle_event.is_set():
                return

    @property
    def queue_depth(self) -> int:
        return self.queue.qsize()

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._run_worker(), name="memory-worker")
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._run_ttl_reaper(), name="ttl-reaper")

    async def stop(self) -> None:
        for task in (self._worker_task, self._reaper_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._worker_task = None
        self._reaper_task = None

    def enqueue(self, job: PostTurnJob) -> bool:
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            logger.warning("Memory worker queue full; dropping background job for %s/%s", job.app_id, job.user_id)
            return False

    async def _run_worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._wait_for_cpu_idle(quiet_seconds=0.8)
                if job.compact_session and job.session_id:
                    await self.compact_session(
                        job.app_id, job.user_id, job.session_id, force=False
                    )
                if job.extract_memories:
                    await self._wait_for_cpu_idle(quiet_seconds=0.3)
                    await self.extract_and_store_facts(job)
            except Exception as exc:
                logger.warning("Background memory job failed: %s", exc)
            finally:
                self.queue.task_done()

    async def _run_ttl_reaper(self) -> None:
        while True:
            await asyncio.sleep(900)
            try:
                purged = await self.memory_repo.purge_expired()
                if purged["expired_memories"] or purged["expired_sessions"]:
                    logger.info("TTL reaper purged %s", purged)
            except Exception as exc:
                logger.debug("TTL reaper skipped: %s", exc)

    async def compact_session(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Summarize older unsummarized turns into sessions.rolling_summary."""
        async with self._compact_lock:
            session = await self.session_repo.get_session(app_id, user_id, session_id)
            if session is None:
                return {"compacted": False, "reason": "session_not_found"}

            up_to_seq = int(session["summary_up_to_seq"] or 0)
            unsummarized = await self.session_repo.get_messages(
                app_id, user_id, session_id, after_seq=up_to_seq, limit=200
            )
            total_unsumm_tokens = sum(int(m["token_est"]) for m in unsummarized)

            if not force:
                if (
                    len(unsummarized) < self.config.compact_min_messages
                    and total_unsumm_tokens < self.config.compact_min_tokens
                ):
                    return {
                        "compacted": False,
                        "reason": "below_threshold",
                        "unsummarized_messages": len(unsummarized),
                        "unsummarized_tokens": total_unsumm_tokens,
                    }
                keep_tail = self.config.keep_recent_messages
            else:
                if len(unsummarized) <= 2:
                    return {
                        "compacted": False,
                        "reason": "too_few_messages",
                        "unsummarized_messages": len(unsummarized),
                    }
                keep_tail = min(2, len(unsummarized) - 1)

            to_fold = unsummarized[:-keep_tail] if keep_tail > 0 else unsummarized
            if not to_fold:
                return {"compacted": False, "reason": "nothing_to_fold"}

            new_up_to_seq = int(to_fold[-1]["seq"])
            transcript_lines = []
            for m in to_fold:
                role = m["role"].upper()
                snippet = truncate_to_tokens(flatten_message_content(m["content"]), 140)
                transcript_lines.append(f"{role}: {snippet}")
            transcript_block = truncate_to_tokens("\n".join(transcript_lines), 950)

            prior_summary = (session["rolling_summary"] or "").strip()
            user_prompt = (
                f"Existing summary:\n{prior_summary or '(none)'}\n\n"
                f"New turns to fold in:\n{transcript_block}"
            )

            summary_backend = self.config.models.get(self.config.summary_model_id)
            if not summary_backend:
                return {"compacted": False, "reason": "summary_backend_unavailable"}

            resp = await self.http_client.post(
                f"{summary_backend}/v1/chat/completions",
                json={
                    "model": self.config.summary_model_id,
                    "messages": [
                        {"role": "system", "content": ROLLING_SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
                timeout=45.0,
            )
            resp.raise_for_status()
            resp_json = resp.json()
            await self._record_usage(
                app_id=app_id,
                user_id=user_id,
                model_id=self.config.summary_model_id,
                source="memory_compact",
                resp_json=resp_json,
                fallback_prompt=estimate_tokens(ROLLING_SUMMARY_SYSTEM_PROMPT) + estimate_tokens(user_prompt),
                fallback_completion=estimate_tokens(
                    ((resp_json.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                ),
            )
            choices = resp_json.get("choices") or []
            summary_text = (
                (choices[0].get("message") or {}).get("content", "").strip()
                if choices
                else ""
            )
            if not summary_text:
                return {"compacted": False, "reason": "empty_summary_output"}

            summary_tokens = estimate_tokens(summary_text)
            updated = await self.session_repo.update_rolling_summary(
                app_id=app_id,
                user_id=user_id,
                session_id=session_id,
                expected_up_to_seq=up_to_seq,
                new_up_to_seq=new_up_to_seq,
                summary=summary_text,
                summary_token_est=summary_tokens,
            )
            return {
                "compacted": updated,
                "summary_up_to_seq": new_up_to_seq,
                "rolling_summary": summary_text,
                "summary_token_est": summary_tokens,
            }

    async def extract_and_store_facts(self, job: PostTurnJob) -> list[dict[str, Any]]:
        """Extract 0..3 durable user facts via qwen2.5-1.5b, embed, and deduplicate."""
        if not should_extract_facts(job.user_text):
            return []

        agent_cfg = self.config.agents.get(job.requested_model_or_agent) or {}
        if agent_cfg.get("extract_memories") is False:
            return []

        extractor_backend = self.config.models.get(self.config.extractor_model_id)
        if not extractor_backend:
            return []

        bounded_user = truncate_to_tokens(job.user_text, 400)
        bounded_assistant = truncate_to_tokens(job.assistant_text, 220)
        extraction_input = f"USER: {bounded_user}\nASSISTANT: {bounded_assistant}"

        resp = await self.http_client.post(
            f"{extractor_backend}/v1/chat/completions",
            json={
                "model": self.config.extractor_model_id,
                "messages": [
                    {"role": "system", "content": FACT_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": extraction_input},
                ],
                "temperature": 0.0,
                "max_tokens": 220,
            },
            timeout=45.0,
        )
        resp.raise_for_status()
        resp_json = resp.json()
        choices = resp_json.get("choices") or []
        raw_output = (
            (choices[0].get("message") or {}).get("content", "").strip()
            if choices
            else ""
        )
        await self._record_usage(
            app_id=job.app_id,
            user_id=job.user_id,
            model_id=self.config.extractor_model_id,
            source="memory_extract",
            resp_json=resp_json,
            fallback_prompt=estimate_tokens(FACT_EXTRACTION_SYSTEM_PROMPT) + estimate_tokens(extraction_input),
            fallback_completion=estimate_tokens(raw_output),
        )
        items = _parse_json_array(raw_output)[:3]
        stored: list[dict[str, Any]] = []

        for item in items:
            content = str(item.get("content") or "").strip()
            if len(content) < 6 or len(content) > 400:
                continue
            category = str(item.get("category") or "fact").strip().lower()
            if category not in VALID_CATEGORIES:
                category = "fact"
            try:
                importance = max(1, min(int(item.get("importance", 3)), 5))
            except (TypeError, ValueError):
                importance = 3

            vec = await self.embedder.embed_text(content, raise_on_error=False)
            if vec is None:
                continue
            await self._record_usage(
                app_id=job.app_id,
                user_id=job.user_id,
                model_id=self.config.embedding_model_id,
                source="memory_embed",
                resp_json=None,
                fallback_prompt=estimate_tokens(content),
                fallback_completion=0,
            )

            record, action = await self.memory_repo.upsert_semantic_memory(
                app_id=job.app_id,
                user_id=job.user_id,
                content=content,
                embedding=vec,
                category=category,
                importance=importance,
                confidence=0.85,
                source_session_id=job.session_id,
                max_memories=self.config.max_memories_per_user,
            )
            stored.append({"action": action, "memory": record})

        return stored
