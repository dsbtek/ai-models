"""Async client for the local all-minilm-l6-v2 llama.cpp embedding server."""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any

# pyrefly: ignore[missing-import]
import httpx

from config import RouterConfig

logger = logging.getLogger("router.memory.embeddings")


class EmbeddingClient:
    """Generates 384-dimensional embeddings via the configured embedding backend with an in-memory LRU cache."""

    def __init__(
        self,
        config: RouterConfig,
        http_client: httpx.AsyncClient,
        cache_size: int = 256,
    ) -> None:
        self.config = config
        self.http_client = http_client
        self.cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self.last_cache_hit: bool = False

    @property
    def backend_url(self) -> str | None:
        return self.config.models.get(self.config.embedding_model_id)

    async def embed_text(
        self, text: str, *, raise_on_error: bool = False, timeout: float = 4.0
    ) -> list[float] | None:
        cleaned = text.strip()
        if not cleaned:
            self.last_cache_hit = False
            return None

        bounded_text = cleaned[:1400]
        cache_key = (self.config.embedding_model_id, bounded_text)
        if self.cache_size > 0 and cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            self.last_cache_hit = True
            return list(self._cache[cache_key])

        self.last_cache_hit = False
        base_url = self.backend_url
        if not base_url:
            if raise_on_error:
                raise RuntimeError(
                    f"Embedding model '{self.config.embedding_model_id}' is not registered"
                )
            return None

        try:
            resp = await self.http_client.post(
                f"{base_url}/v1/embeddings",
                json={"model": self.config.embedding_model_id, "input": bounded_text},
                timeout=timeout,
            )
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            data = payload.get("data") or []
            if not data:
                raise ValueError("Empty embedding data returned by backend")
            vec = data[0].get("embedding")
            if isinstance(vec, list) and vec and isinstance(vec[0], list):
                vec = vec[0]
            if not isinstance(vec, list) or len(vec) != self.config.embedding_dim:
                raise ValueError(
                    f"Expected {self.config.embedding_dim}-d embedding, got {len(vec) if isinstance(vec, list) else type(vec)}"
                )
            result = [float(x) for x in vec]
            if self.cache_size > 0:
                self._cache[cache_key] = result
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return list(result)
        except Exception as exc:
            logger.warning("Embedding generation failed: %s", exc)
            if raise_on_error:
                raise
            return None
