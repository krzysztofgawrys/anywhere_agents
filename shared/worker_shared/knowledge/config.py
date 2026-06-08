"""Knowledge feature configuration, read from environment.

All knobs are env-driven so an operator can flip embeddings backend or
disable the feature without code changes. Defaults are chosen so the
feature works out-of-the-box with a local embedding model (no vendor in
the data path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Local model default. English-leaning but always loadable via fastembed.
# For non-English projects set KNOWLEDGE_EMBED_MODEL to a multilingual
# model from fastembed's supported list (e.g. a multilingual e5/MiniLM).
_DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
_DEFAULT_VOYAGE_MODEL = "voyage-3"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class KnowledgeConfig:
    """Resolved knowledge settings for this worker process."""

    enabled: bool
    backend: str  # "local" | "voyage"
    model: str
    topk: int
    dedup_threshold: float
    consolidate_idle_sec: int
    voyage_api_key: str | None
    # Local-embedding model cache dir (fastembed). None -> library default.
    # Point at a persistent volume so the model downloads once.
    embed_cache_dir: str | None


def load_config() -> KnowledgeConfig:
    """Build a :class:`KnowledgeConfig` from the current environment."""
    backend = (os.getenv("KNOWLEDGE_EMBEDDINGS", "local") or "local").strip().lower()
    if backend not in ("local", "voyage"):
        backend = "local"
    default_model = _DEFAULT_LOCAL_MODEL if backend == "local" else _DEFAULT_VOYAGE_MODEL
    model = (os.getenv("KNOWLEDGE_EMBED_MODEL") or default_model).strip()
    return KnowledgeConfig(
        enabled=_env_bool("KNOWLEDGE_ENABLED", True),
        backend=backend,
        model=model,
        topk=max(1, _env_int("KNOWLEDGE_TOPK", 5)),
        dedup_threshold=_env_float("KNOWLEDGE_DEDUP_THRESHOLD", 0.95),
        consolidate_idle_sec=_env_int("KNOWLEDGE_CONSOLIDATE_IDLE_SEC", 600),
        voyage_api_key=(os.getenv("VOYAGE_API_KEY") or None),
        embed_cache_dir=(os.getenv("KNOWLEDGE_EMBED_CACHE") or None),
    )
