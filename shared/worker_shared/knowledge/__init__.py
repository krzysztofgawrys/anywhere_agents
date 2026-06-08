"""Project Knowledge - agent-curated RAG memory.

SDK-agnostic core reused by every worker (like ``db``, ``files``,
``terminal``). A worker wires this into its agent SDK by exposing the
:mod:`worker_shared.knowledge.service` functions as MCP tools and
injecting a short directive into the system prompt; the data (documents
+ chunk embeddings) lives in the worker's SQLite DB.

Public surface is :mod:`worker_shared.knowledge.service`.
"""

from __future__ import annotations

from worker_shared.knowledge.config import KnowledgeConfig, load_config

__all__ = ["KnowledgeConfig", "load_config"]
