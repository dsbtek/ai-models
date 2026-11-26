"""Deterministic token estimation and strict context-window budget packing."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

CHARS_PER_TOKEN = 3.5
MSG_OVERHEAD_TOKENS = 4


def flatten_message_content(content: Any) -> str:
    """Extract text from OpenAI string or multimodal content-part arrays."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)


def estimate_tokens(text: str) -> int:
    """Conservative zero-dependency BPE token estimate for Llama-3 / Qwen2.5."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN) + MSG_OVERHEAD_TOKENS


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 3
    for msg in messages:
        total += estimate_tokens(flatten_message_content(msg.get("content"))) + 2
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text cleanly so estimate_tokens(result) <= max_tokens."""
    if max_tokens <= MSG_OVERHEAD_TOKENS + 2:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    char_budget = max(12, int((max_tokens - MSG_OVERHEAD_TOKENS - 3) * CHARS_PER_TOKEN))
    return text[:char_budget].rstrip() + "..."


@dataclass(frozen=True)
class BudgetPlan:
    ctx_size: int
    completion_reserve: int
    safety_margin: int
    input_budget: int
    system_cap: int
    memories_cap: int
    summary_cap: int
    clamped_max_tokens: int | None


def compute_budget_plan(
    ctx_size: int = 2048,
    requested_max_tokens: int | None = None,
    default_completion_reserve: int = 384,
    safety_margin: int = 64,
) -> BudgetPlan:
    """Compute guaranteed-safe input and output token caps for llama-server."""
    min_input_floor = min(768, ctx_size // 2)
    max_allowed_completion = max(64, ctx_size - min_input_floor - safety_margin)

    clamped_max_tokens: int | None = None
    if requested_max_tokens is not None and requested_max_tokens > 0:
        completion_reserve = min(int(requested_max_tokens), max_allowed_completion)
        clamped_max_tokens = completion_reserve
    else:
        completion_reserve = min(default_completion_reserve, max_allowed_completion)

    input_budget = max(256, ctx_size - completion_reserve - safety_margin)
    return BudgetPlan(
        ctx_size=ctx_size,
        completion_reserve=completion_reserve,
        safety_margin=safety_margin,
        input_budget=input_budget,
        system_cap=min(320, max(120, int(input_budget * 0.22))),
        memories_cap=min(240, max(96, int(input_budget * 0.16))),
        summary_cap=min(260, max(96, int(input_budget * 0.18))),
        clamped_max_tokens=clamped_max_tokens,
    )


def pack_recent_history(
    history_messages: list[dict[str, Any]],
    available_tokens: int,
) -> list[dict[str, Any]]:
    """Select the most recent chronological slice of messages fitting within available_tokens."""
    if available_tokens <= 16 or not history_messages:
        return []

    selected_reversed: list[dict[str, Any]] = []
    used = 0
    for msg in reversed(history_messages):
        content = flatten_message_content(msg.get("content"))
        cost = estimate_tokens(content) + 2
        if used + cost > available_tokens:
            break
        selected_reversed.append({"role": msg["role"], "content": content})
        used += cost

    selected_reversed.reverse()
    return selected_reversed


def extract_usage_counts(
    payload: dict[str, Any] | None,
    fallback_prompt: int = 0,
    fallback_completion: int = 0,
) -> tuple[int, int, int]:
    """Extract (prompt_tokens, completion_tokens, total_tokens) with deterministic fallback."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        p_val = max(0, int(prompt)) if isinstance(prompt, (int, float)) else max(0, int(fallback_prompt))
        c_val = max(0, int(completion)) if isinstance(completion, (int, float)) else max(0, int(fallback_completion))
        t_val = max(0, int(total)) if isinstance(total, (int, float)) else p_val + c_val
        if p_val > 0 or c_val > 0 or t_val > 0:
            if t_val < p_val + c_val:
                t_val = p_val + c_val
            return p_val, c_val, t_val

    p_val = max(0, int(fallback_prompt))
    c_val = max(0, int(fallback_completion))
    return p_val, c_val, p_val + c_val
