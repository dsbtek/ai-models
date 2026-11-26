"""Pre-inference context assembly: recalls semantic memories, merges session state, and packs tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from auth import RequestScope
from config import RouterConfig
from db.repositories import MemoryRepository, SessionRepository
from .embeddings import EmbeddingClient
from .tokens import (
    BudgetPlan,
    compute_budget_plan,
    estimate_messages_tokens,
    estimate_tokens,
    flatten_message_content,
    pack_recent_history,
    truncate_to_tokens,
)

logger = logging.getLogger("router.memory.assembler")


@dataclass
class AssembledContext:
    messages: list[dict[str, Any]]
    new_messages_to_persist: list[dict[str, Any]]
    latest_user_text: str
    input_tokens: int
    budget: BudgetPlan
    retrieved_memories: list[dict[str, Any]] = field(default_factory=list)
    used_rolling_summary: bool = False


def reconcile_client_messages(
    stored_messages: list[dict[str, Any]],
    client_conv_messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Reconcile stored session history with incoming client conversational messages.

    Supports both:
      1. Server-stateful callers sending only the latest user message: [{"role": "user", "content": "..."}]
      2. Client-history callers re-sending prior turns alongside the new message(s)

    Returns (prior_history_messages, new_messages_to_persist).
    """
    normalized_client = [
        {
            "role": str(m.get("role") or "user"),
            "content": flatten_message_content(m.get("content")),
        }
        for m in client_conv_messages
        if flatten_message_content(m.get("content")).strip()
    ]
    if not normalized_client:
        return stored_messages, []

    if not stored_messages:
        return [], normalized_client

    # Common server-stateful case: client sent a single turn
    if len(normalized_client) == 1:
        msg = normalized_client[0]
        last_stored = stored_messages[-1]
        if (
            last_stored["role"] == msg["role"]
            and last_stored["content"].strip() == msg["content"].strip()
        ):
            return stored_messages[:-1], [msg]
        return stored_messages, [msg]

    # Multi-message client payload: find longest overlap between suffix of stored_messages
    # and prefix of normalized_client to avoid duplicating already-persisted turns.
    stored_pairs = [(m["role"], m["content"].strip()) for m in stored_messages]
    client_pairs = [(m["role"], m["content"].strip()) for m in normalized_client]

    max_k = min(len(stored_pairs), len(client_pairs))
    overlap = 0
    for k in range(max_k, 0, -1):
        if stored_pairs[-k:] == client_pairs[:k]:
            overlap = k
            break

    if overlap > 0:
        new_slice = normalized_client[overlap:]
        if new_slice:
            return stored_messages, new_slice
        # Client resent exact stored tail; treat trailing message as active turn without re-saving
        return stored_messages[:-1], []

    # Check if stored_pairs is a subsequence prefix inside client_pairs (client kept full history)
    last_stored_pair = stored_pairs[-1]
    for idx in range(len(client_pairs) - 1, -1, -1):
        if client_pairs[idx] == last_stored_pair:
            new_slice = normalized_client[idx + 1 :]
            return stored_messages, new_slice

    # Fallback: persist only trailing user message(s) after the last assistant message in client array
    trailing: list[dict[str, Any]] = []
    for m in reversed(normalized_client):
        if m["role"] == "assistant":
            break
        trailing.append(m)
    trailing.reverse()
    return stored_messages, trailing or [normalized_client[-1]]


class ContextAssembler:
    """Builds the token-bounded message array combining agent prompt, memories, summary, and turns."""

    def __init__(
        self,
        config: RouterConfig,
        session_repo: SessionRepository,
        memory_repo: MemoryRepository,
        embedder: EmbeddingClient,
    ) -> None:
        self.config = config
        self.session_repo = session_repo
        self.memory_repo = memory_repo
        self.embedder = embedder

    async def assemble(
        self,
        scope: RequestScope,
        payload: dict[str, Any],
        agent_system_prompt: str | None = None,
    ) -> AssembledContext:
        raw_messages = list(payload.get("messages") or [])
        caller_system_parts: list[str] = []
        caller_conv: list[dict[str, Any]] = []

        for m in raw_messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            text = flatten_message_content(m.get("content")).strip()
            if not text:
                continue
            if role == "system":
                caller_system_parts.append(text)
            else:
                caller_conv.append({"role": role, "content": text})

        latest_user_text = next(
            (m["content"] for m in reversed(caller_conv) if m["role"] == "user"),
            caller_conv[-1]["content"] if caller_conv else "",
        )

        req_max_tokens = payload.get("max_tokens")
        try:
            req_max_int = int(req_max_tokens) if req_max_tokens is not None else None
        except (TypeError, ValueError):
            req_max_int = None

        budget = compute_budget_plan(
            ctx_size=self.config.ctx_size,
            requested_max_tokens=req_max_int,
            default_completion_reserve=self.config.default_completion_reserve,
            safety_margin=self.config.safety_margin_tokens,
        )
        if budget.clamped_max_tokens is not None:
            payload["max_tokens"] = budget.clamped_max_tokens

        # 1. Load session state & unsummarized messages (if session_id is active)
        rolling_summary = ""
        stored_unsummarized: list[dict[str, Any]] = []
        if scope.session_id and scope.user_id and scope.memory_mode != "off":
            try:
                session = await self.session_repo.get_session(
                    scope.app_id, scope.user_id, scope.session_id
                )
                if session:
                    rolling_summary = (session.get("rolling_summary") or "").strip()
                    up_to_seq = int(session.get("summary_up_to_seq") or 0)
                    stored_unsummarized = await self.session_repo.get_messages(
                        scope.app_id,
                        scope.user_id,
                        scope.session_id,
                        after_seq=up_to_seq,
                        limit=120,
                    )
            except Exception as exc:
                logger.warning("Session lookup failed, continuing gracefully: %s", exc)

        prior_history, new_to_persist = reconcile_client_messages(
            stored_unsummarized, caller_conv
        )

        # Active turn messages that MUST appear at the end of the prompt
        active_turn = (
            new_to_persist
            if new_to_persist
            else ([caller_conv[-1]] if caller_conv else [])
        )

        # 2. Retrieve top-K semantic user memories (if user_id is present and mode permits)
        retrieved_memories: list[dict[str, Any]] = []
        if (
            scope.user_id
            and latest_user_text
            and scope.memory_mode in {"auto", "read_only"}
        ):
            query_vec = await self.embedder.embed_text(
                latest_user_text, raise_on_error=False
            )
            if query_vec is not None:
                try:
                    retrieved_memories = await self.memory_repo.search_memories(
                        app_id=scope.app_id,
                        user_id=scope.user_id,
                        query_embedding=query_vec,
                        top_k=self.config.memory_top_k,
                        min_similarity=self.config.memory_min_similarity,
                        bump_access=(scope.memory_mode == "auto"),
                    )
                except Exception as exc:
                    logger.warning("Semantic memory recall failed: %s", exc)

        # 3. Pack slots strictly within budget.input_budget
        remaining = budget.input_budget - 6  # message framing slack

        base_sys_pieces = [p for p in [agent_system_prompt, *caller_system_parts] if p]
        base_sys_text = truncate_to_tokens(
            "\n\n".join(base_sys_pieces).strip(), budget.system_cap
        )

        memory_block = ""
        if retrieved_memories:
            lines = ["[Known User Context]"]
            for mem in retrieved_memories:
                bullet = f"- ({mem['category']}) {mem['content']}"
                candidate = "\n".join([*lines, bullet])
                if estimate_tokens(candidate) > budget.memories_cap:
                    break
                lines.append(bullet)
            if len(lines) > 1:
                memory_block = "\n".join(lines)

        summary_block = ""
        used_rolling_summary = False
        if rolling_summary:
            bounded_sum = truncate_to_tokens(rolling_summary, budget.summary_cap - 10)
            if bounded_sum:
                summary_block = f"[Earlier Conversation Summary]\n{bounded_sum}"
                used_rolling_summary = True

        # Place stable blocks (base prompt + summary) before dynamic per-query memories
        # to maximize llama-server Longest Common Prefix (LCP) KV-cache reuse across turns.
        combined_system = "\n\n".join(
            p for p in (base_sys_text, summary_block, memory_block) if p
        ).strip()

        final_messages: list[dict[str, Any]] = []
        if combined_system:
            sys_cost = estimate_tokens(combined_system) + 2
            remaining -= sys_cost
            final_messages.append({"role": "system", "content": combined_system})

        # Reserve space for active_turn first (truncating single huge message only if it exceeds remaining)
        bounded_active_turn: list[dict[str, Any]] = []
        active_cost = 0
        max_for_active = max(128, remaining - 32)
        per_msg_cap = max(96, max_for_active // max(1, len(active_turn)))
        for m in active_turn:
            bounded_text = truncate_to_tokens(m["content"], per_msg_cap)
            active_cost += estimate_tokens(bounded_text) + 2
            bounded_active_turn.append({"role": m["role"], "content": bounded_text})

        remaining_for_history = max(0, remaining - active_cost)
        packed_history = pack_recent_history(prior_history, remaining_for_history)

        final_messages.extend(packed_history)
        final_messages.extend(bounded_active_turn)

        payload["messages"] = final_messages
        total_input_est = estimate_messages_tokens(final_messages)

        persist_records = [
            {
                "role": m["role"],
                "content": m["content"],
                "token_est": estimate_tokens(m["content"]),
            }
            for m in new_to_persist
        ]

        return AssembledContext(
            messages=final_messages,
            new_messages_to_persist=persist_records,
            latest_user_text=latest_user_text,
            input_tokens=total_input_est,
            budget=budget,
            retrieved_memories=retrieved_memories,
            used_rolling_summary=used_rolling_summary,
        )
