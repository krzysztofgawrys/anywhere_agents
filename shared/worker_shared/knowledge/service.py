"""Knowledge service facade - the surface workers expose as MCP tools.

Orchestrates chunking + embedding + persistence. All functions take an
explicit :class:`Database` (testable) and accept optional ``config`` /
``embedder`` overrides so tests can inject deterministic stubs.
"""

from __future__ import annotations

import hashlib
from typing import Any

from worker_shared.db import Database
from worker_shared.knowledge import store
from worker_shared.knowledge.chunk import chunk_text
from worker_shared.knowledge.config import KnowledgeConfig, load_config
from worker_shared.knowledge.embed import Embedder, get_embedder


def _resolve(
    config: KnowledgeConfig | None, embedder: Embedder | None
) -> tuple[KnowledgeConfig, Embedder]:
    cfg = config or load_config()
    emb = embedder or get_embedder(cfg)
    return cfg, emb


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def has_knowledge(db: Database, project_id: int) -> bool:
    """True if the project has at least one stored knowledge entry."""
    return await store.count_documents(db, project_id) > 0


async def save(
    db: Database,
    project_id: int,
    title: str,
    content: str,
    *,
    tags: str | None = None,
    source: str = "agent",
    config: KnowledgeConfig | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Store a knowledge entry, skipping near-duplicates.

    Returns ``{"saved": bool, "id"?: int, "reason"?: str, "title": str}``.
    """
    title = (title or "").strip() or "Untitled"
    content = (content or "").strip()
    if not content:
        return {"saved": False, "reason": "empty content", "title": title}

    cfg, emb = _resolve(config, embedder)
    content_hash = _hash(content)

    # Exact-duplicate short circuit.
    for doc in await store.list_documents(db, project_id):
        if doc.get("content") and _hash(doc["content"]) == content_hash:
            return {
                "saved": False,
                "reason": "identical entry already exists",
                "id": doc["id"],
                "title": title,
            }

    # Semantic-duplicate guard: if the new content is near-identical to
    # something already indexed, skip to keep retrieval clean.
    query_vec = await emb.embed_query(f"{title}\n\n{content}"[:2000])
    existing = await store.search_chunks(db, project_id, query_vec, k=1)
    if existing and existing[0]["score"] >= cfg.dedup_threshold:
        return {
            "saved": False,
            "reason": f"near-duplicate of '{existing[0]['title']}' "
            f"(score {existing[0]['score']})",
            "id": existing[0]["doc_id"],
            "title": title,
        }

    chunks = chunk_text(content)
    if not chunks:
        return {"saved": False, "reason": "no chunkable content", "title": title}
    vectors = await emb.embed_documents(chunks)
    pairs = list(zip(chunks, vectors, strict=True))
    doc_id = await store.insert_document(
        db,
        project_id=project_id,
        title=title,
        content=content,
        source=source,
        tags=tags,
        content_hash=content_hash,
        chunks=pairs,
    )
    return {"saved": True, "id": doc_id, "title": title}


async def search(
    db: Database,
    project_id: int,
    query: str,
    *,
    k: int | None = None,
    config: KnowledgeConfig | None = None,
    embedder: Embedder | None = None,
) -> list[dict[str, Any]]:
    """Return the top knowledge chunks relevant to *query*."""
    query = (query or "").strip()
    if not query:
        return []
    cfg, emb = _resolve(config, embedder)
    query_vec = await emb.embed_query(query)
    return await store.search_chunks(db, project_id, query_vec, k or cfg.topk)


# Sentinel: distinguishes "tags not provided" (leave as-is) from
# "tags=None" (clear them).
_UNSET: Any = object()


async def update(
    db: Database,
    project_id: int,
    doc_id: int,
    content: str | None = None,
    *,
    tags: Any = _UNSET,
    config: KnowledgeConfig | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Update a document's content and/or tags by id.

    - ``content`` given -> re-chunk + re-embed (replaces the content).
    - ``tags`` given (string or None) -> set/clear tags in place, no
      re-embed (cheap; ideal for tagging existing entries).
    Omit both -> nothing to do.
    """
    doc = await store.get_document(db, project_id, doc_id)
    if doc is None:
        return {"updated": False, "reason": "not found"}

    changed: list[str] = []
    norm_content = content.strip() if isinstance(content, str) else None
    if norm_content:
        _, emb = _resolve(config, embedder)
        chunks = chunk_text(norm_content)
        vectors = await emb.embed_documents(chunks)
        await store.replace_chunks(
            db,
            project_id=project_id,
            doc_id=doc_id,
            content=norm_content,
            content_hash=_hash(norm_content),
            chunks=list(zip(chunks, vectors, strict=True)),
        )
        changed.append("content")

    if tags is not _UNSET:
        norm_tags = (str(tags).strip() or None) if tags is not None else None
        await store.update_tags(
            db, project_id=project_id, doc_id=doc_id, tags=norm_tags
        )
        changed.append("tags")

    if not changed:
        return {"updated": False, "reason": "nothing to update"}
    return {"updated": True, "id": doc_id, "changed": changed}


async def delete(db: Database, project_id: int, doc_id: int) -> dict[str, Any]:
    removed = await store.delete_document(db, project_id=project_id, doc_id=doc_id)
    return {"deleted": removed, "id": doc_id}


async def list_entries(db: Database, project_id: int) -> list[dict[str, Any]]:
    """Document metadata for UI / the agent's `list` tool (no embeddings)."""
    return await store.list_documents(db, project_id)


async def get_entry(
    db: Database, project_id: int, doc_id: int
) -> dict[str, Any] | None:
    return await store.get_document(db, project_id, doc_id)
