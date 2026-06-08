"""Embedding backends: local (fastembed/ONNX, CPU) or hosted (Voyage).

Selected at runtime via :func:`get_embedder` based on
:class:`KnowledgeConfig`. Heavy deps (``fastembed``, ``httpx``) are
imported lazily so the chosen backend pulls only what it needs.

The public interface is async: local embedding (sync, CPU-bound) is run
in a thread so it never blocks the event loop; the Voyage backend uses
async httpx directly.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import numpy as np
import structlog
from numpy.typing import NDArray

from worker_shared.knowledge.config import KnowledgeConfig

logger = structlog.get_logger()

Vector = NDArray[np.float32]


class Embedder(Protocol):
    """Async embedding provider."""

    @property
    def dim(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[Vector]: ...

    async def embed_query(self, text: str) -> Vector: ...


class LocalEmbedder:
    """fastembed (ONNX) embedder. Runs on CPU, no torch."""

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding  # lazy: only when local backend used

        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self._dim = 0

    def _embed_sync(self, texts: list[str]) -> list[Vector]:
        return [np.asarray(v, dtype=np.float32) for v in self._model.embed(texts)]

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        vecs = await asyncio.to_thread(self._embed_sync, list(texts))
        if not self._dim and vecs:
            self._dim = int(vecs[0].shape[0])
        return vecs

    async def embed_query(self, text: str) -> Vector:
        vecs = await self.embed_documents([text])
        return vecs[0]

    @property
    def dim(self) -> int:
        return self._dim


class VoyageEmbedder:
    """Hosted Voyage embeddings. Opt-in: sends text to api.voyageai.com."""

    _URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self._dim = 0

    async def _call(self, texts: list[str], input_type: str) -> list[Vector]:
        import httpx  # lazy: only when voyage backend used

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"input": texts, "model": self.model, "input_type": input_type},
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [np.asarray(d["embedding"], dtype=np.float32) for d in ordered]

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        vecs = await self._call(list(texts), "document")
        if not self._dim and vecs:
            self._dim = int(vecs[0].shape[0])
        return vecs

    async def embed_query(self, text: str) -> Vector:
        vecs = await self._call([text], "query")
        return vecs[0]

    @property
    def dim(self) -> int:
        return self._dim


_cache: dict[tuple[str, str], Embedder] = {}


def get_embedder(config: KnowledgeConfig) -> Embedder:
    """Return a process-cached embedder for *config* (keyed by backend+model)."""
    key = (config.backend, config.model)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    embedder: Embedder
    if config.backend == "voyage":
        if not config.voyage_api_key:
            raise RuntimeError(
                "KNOWLEDGE_EMBEDDINGS=voyage but VOYAGE_API_KEY is not set"
            )
        embedder = VoyageEmbedder(config.voyage_api_key, config.model)
    else:
        embedder = LocalEmbedder(config.model, config.embed_cache_dir)
    logger.info("knowledge_embedder_ready", backend=config.backend, model=config.model)
    _cache[key] = embedder
    return embedder
