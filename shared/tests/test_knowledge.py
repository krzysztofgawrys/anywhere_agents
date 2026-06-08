"""Tests for the knowledge module: chunking, store, and service.

Uses a deterministic feature-hashing embedder so retrieval ordering and
dedup are exercised without pulling fastembed/onnxruntime.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from numpy.typing import NDArray

from worker_shared.db import Database
from worker_shared.knowledge import service
from worker_shared.knowledge.chunk import chunk_text
from worker_shared.knowledge.config import KnowledgeConfig

Vector = NDArray[np.float32]

_DIM = 64


class StubEmbedder:
    """Deterministic feature-hashing embedder (token overlap -> cosine)."""

    def __init__(self, dim: int = _DIM) -> None:
        self._dim = dim

    def _vec(self, text: str) -> Vector:
        v = np.zeros(self._dim, dtype=np.float32)
        for token in text.lower().split():
            h = int(hashlib.sha1(token.encode()).hexdigest(), 16)
            v[h % self._dim] += 1.0
        return v

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> Vector:
        return self._vec(text)


def _cfg(**over: object) -> KnowledgeConfig:
    base = dict(
        enabled=True,
        backend="local",
        model="stub",
        topk=5,
        dedup_threshold=0.95,
        consolidate_idle_sec=600,
        voyage_api_key=None,
        embed_cache_dir=None,
    )
    base.update(over)
    return KnowledgeConfig(**base)  # type: ignore[arg-type]


@pytest.fixture
async def db(tmp_path) -> Database:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "test.sqlite")
    await database.init()
    # A project row is required (FK target).
    async with database.connect() as conn:
        await conn.execute(
            "INSERT INTO projects (path, name) VALUES (?, ?)", ("/tmp/p", "p")
        )
        await conn.commit()
    return database


# ── chunking ──────────────────────────────────────────────────────────


def test_chunk_short_text_single_chunk() -> None:
    assert chunk_text("hello world") == ["hello world"]


def test_chunk_empty() -> None:
    assert chunk_text("   ") == []


def test_chunk_long_text_splits_with_budget() -> None:
    para = " ".join(["word"] * 400)  # ~2000 chars, no blank lines
    chunks = chunk_text(para, max_chars=500, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


# ── service: save / search / dedup / update / delete ────────────────────


async def test_save_and_search_roundtrip(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    res = await service.save(
        db, 1, "Build", "The build uses docker compose up", embedder=emb, config=cfg
    )
    assert res["saved"] is True
    hits = await service.search(db, 1, "how to build with docker", embedder=emb, config=cfg)
    assert hits
    assert hits[0]["title"] == "Build"
    assert hits[0]["score"] > 0.0


async def test_search_orders_by_relevance(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    await service.save(db, 1, "DB", "database schema migrations sqlite", embedder=emb, config=cfg)
    await service.save(db, 1, "UI", "react frontend tailwind components", embedder=emb, config=cfg)
    hits = await service.search(db, 1, "database schema", embedder=emb, config=cfg)
    assert hits[0]["title"] == "DB"


async def test_exact_duplicate_skipped(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    await service.save(db, 1, "A", "same content here", embedder=emb, config=cfg)
    res = await service.save(db, 1, "A again", "same content here", embedder=emb, config=cfg)
    assert res["saved"] is False
    assert "identical" in res["reason"]


async def test_near_duplicate_skipped_by_threshold(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg(dedup_threshold=0.9)
    await service.save(db, 1, "A", "alpha beta gamma delta", embedder=emb, config=cfg)
    # Same tokens, different order -> identical feature-hash vector -> cosine 1.0
    res = await service.save(db, 1, "B", "delta gamma beta alpha", embedder=emb, config=cfg)
    assert res["saved"] is False
    assert "near-duplicate" in res["reason"]


async def test_update_and_delete(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    saved = await service.save(db, 1, "T", "original text", embedder=emb, config=cfg)
    doc_id = saved["id"]
    upd = await service.update(db, 1, doc_id, "replaced text body", embedder=emb, config=cfg)
    assert upd["updated"] is True
    entry = await service.get_entry(db, 1, doc_id)
    assert entry is not None and entry["content"] == "replaced text body"
    deleted = await service.delete(db, 1, doc_id)
    assert deleted["deleted"] is True
    assert await service.has_knowledge(db, 1) is False


async def test_update_tags_in_place(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    saved = await service.save(db, 1, "T", "content body", embedder=emb, config=cfg)
    doc_id = saved["id"]
    # Tag in place: no content, no embedder needed (no re-embed).
    res = await service.update(db, 1, doc_id, tags="build, gotcha")
    assert res["updated"] is True
    assert res["changed"] == ["tags"]
    entry = await service.get_entry(db, 1, doc_id)
    assert entry is not None
    assert entry["tags"] == "build, gotcha"
    assert entry["content"] == "content body"  # unchanged
    # list surfaces tags
    entries = await service.list_entries(db, 1)
    assert entries[0]["tags"] == "build, gotcha"


async def test_update_nothing_is_noop(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    saved = await service.save(db, 1, "T", "body", embedder=emb, config=cfg)
    res = await service.update(db, 1, saved["id"])
    assert res["updated"] is False


async def test_has_knowledge(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    assert await service.has_knowledge(db, 1) is False
    await service.save(db, 1, "X", "some durable fact", embedder=emb, config=cfg)
    assert await service.has_knowledge(db, 1) is True


async def test_empty_content_not_saved(db: Database) -> None:
    emb, cfg = StubEmbedder(), _cfg()
    res = await service.save(db, 1, "Empty", "   ", embedder=emb, config=cfg)
    assert res["saved"] is False
