"""Typed configuration and model registry loader for the llama.cpp API & Memory Router."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any


def normalize_database_url(raw_url: str, *, in_docker: bool = False) -> str:
    """Convert SQLAlchemy-style PostgreSQL URLs to asyncpg DSNs and map localhost inside Docker."""
    url = (raw_url or "").strip()
    if not url:
        return ""
    dsn = re.sub(r"^postgres(?:ql)?\+[a-zA-Z0-9_]+://", "postgresql://", url, count=1)
    if in_docker:
        dsn = re.sub(
            r"@(localhost|127\.0\.0\.1)(?=[:/])",
            "@host.docker.internal",
            dsn,
            count=1,
        )
    return dsn


def _load_dotenv_fallback() -> None:
    """Load root .env into os.environ when running locally outside Docker if vars aren't set."""
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if env_path.is_file():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
            break


@dataclass
class RouterConfig:
    default_model: str
    models: dict[str, str]
    embedding_models: set[str]
    agents: dict[str, dict[str, Any]]
    database_url: str
    auth_mode: str = "dev"  # "dev" | "strict"
    admin_api_key: str = ""
    app_api_keys_seed: dict[str, str] = field(default_factory=dict)
    ctx_size: int = 2048
    default_completion_reserve: int = 384
    safety_margin_tokens: int = 64
    embedding_model_id: str = "all-minilm-l6-v2"
    summary_model_id: str = "llama-3.2-1b"
    extractor_model_id: str = "qwen2.5-1.5b"
    session_default_ttl_hours: int = 0
    memory_top_k: int = 5
    memory_min_similarity: float = 0.35
    max_memories_per_user: int = 200
    compact_min_messages: int = 8
    compact_min_tokens: int = 650
    keep_recent_messages: int = 4
    embedding_dim: int = 384


def load_config(config_path: Path | None = None) -> RouterConfig:
    """Load model registry JSON and environment configuration."""
    _load_dotenv_fallback()

    resolved_path = config_path or Path(
        os.getenv("MODELS_CONFIG_PATH", str(Path(__file__).resolve().parent / "models.json"))
    )
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))

    models: dict[str, str] = {
        k: v.rstrip("/") for k, v in raw.get("models", {}).items()
    }
    # Allow per-model port/URL overrides from env
    port_overrides = {
        "llama-3.2-1b": os.getenv("LLAMA_MODEL_1_PORT"),
        "qwen2.5-1.5b": os.getenv("LLAMA_MODEL_2_PORT"),
        "all-minilm-l6-v2": os.getenv("LLAMA_EMBED_PORT"),
    }
    for model_id, port in port_overrides.items():
        if port and model_id in models and "127.0.0.1:" in models[model_id]:
            models[model_id] = f"http://127.0.0.1:{port}"

    for model_id in list(models):
        env_key = "MODEL_URL_" + re.sub(r"[^A-Za-z0-9]+", "_", model_id).upper()
        if os.getenv(env_key):
            models[model_id] = os.environ[env_key].rstrip("/")

    embedding_models = set(raw.get("embedding_models", ["all-minilm-l6-v2"]))
    agents: dict[str, dict[str, Any]] = raw.get("agents", {})
    for agent_id, cfg in agents.items():
        backend = cfg.get("backend")
        if backend not in models:
            raise ValueError(f"Agent '{agent_id}' references unknown backend '{backend}'")

    default_model = os.getenv("DEFAULT_MODEL") or raw.get("default_model", "")
    if not default_model and models:
        default_model = next(iter(models))

    auth_mode = (os.getenv("AUTH_MODE") or "dev").strip().lower()
    if auth_mode not in {"dev", "strict"}:
        auth_mode = "dev"

    seed_raw = os.getenv("APP_API_KEYS_JSON", "{}").strip() or "{}"
    try:
        app_api_keys_seed = json.loads(seed_raw)
        if not isinstance(app_api_keys_seed, dict):
            app_api_keys_seed = {}
    except json.JSONDecodeError:
        app_api_keys_seed = {}

    in_docker = os.getenv("IN_DOCKER", "").strip().lower() in {"1", "true", "yes"}

    return RouterConfig(
        default_model=default_model,
        models=models,
        embedding_models=embedding_models,
        agents=agents,
        database_url=normalize_database_url(os.getenv("DATABASE_URL", ""), in_docker=in_docker),
        auth_mode=auth_mode,
        admin_api_key=os.getenv("ADMIN_API_KEY", "").strip(),
        app_api_keys_seed={str(k): str(v) for k, v in app_api_keys_seed.items()},
        ctx_size=max(512, int(os.getenv("LLAMA_CTX_SIZE", "2048"))),
        embedding_model_id=os.getenv("EMBEDDING_MODEL_ID", "all-minilm-l6-v2"),
        summary_model_id=os.getenv("SUMMARY_MODEL_ID", "llama-3.2-1b"),
        extractor_model_id=os.getenv("EXTRACTOR_MODEL_ID", "qwen2.5-1.5b"),
        session_default_ttl_hours=max(0, int(os.getenv("SESSION_DEFAULT_TTL_HOURS", "0"))),
        memory_top_k=max(1, int(os.getenv("MEMORY_TOP_K", "5"))),
        memory_min_similarity=float(os.getenv("MEMORY_MIN_SIMILARITY", "0.35")),
        max_memories_per_user=max(10, int(os.getenv("MAX_MEMORIES_PER_USER", "200"))),
        compact_min_messages=max(4, int(os.getenv("COMPACT_MIN_MESSAGES", "8"))),
        compact_min_tokens=max(200, int(os.getenv("COMPACT_MIN_TOKENS", "650"))),
        keep_recent_messages=max(2, int(os.getenv("KEEP_RECENT_MESSAGES", "4"))),
    )
