"""Persistence + vector search for knowledge entries.

Embeddings are stored as float32 BLOBs in the ``knowledge_chunks`` table
and searched with a brute-force cosine in numpy. Per-project bases are
small (agent-curated, not whole-repo), so this avoids the operational
cost of a vector-index extension while staying fast enough.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from worker_shared.db import Database

Vector = NDArray[np.float32]


def vec_to_blob(vec: Vector) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob: bytes, dim: int) -> Vector:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


def _normalize(vec: Vector) -> Vector:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


async def insert_document(
    db: Database,
    *,
    project_id: int,
    title: str,
    content: str,
    source: str,
    tags: str | None,
    content_hash: str,
    chunks: list[tuple[str, Vector]],
) -> int:
    """Insert a document plus its chunk embeddings. Returns the new doc id."""
    await db.init()
    async with db.connect() as conn:
        cursor = await conn.execute(
            """INSERT INTO knowledge_documents
                   (project_id, title, content, source, tags, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, title, content, source, tags, content_hash),
        )
        doc_id = cursor.lastrowid
        assert doc_id is not None
        for ordinal, (text, vec) in enumerate(chunks):
            await conn.execute(
                """INSERT INTO knowledge_chunks
                       (document_id, project_id, ordinal, text, embedding, dim)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, project_id, ordinal, text, vec_to_blob(vec), int(vec.shape[0])),
            )
        await conn.commit()
        return int(doc_id)


async def replace_chunks(
    db: Database,
    *,
    project_id: int,
    doc_id: int,
    content: str,
    content_hash: str,
    chunks: list[tuple[str, Vector]],
) -> None:
    """Replace a document's content + chunks in place (used by update)."""
    await db.init()
    async with db.connect() as conn:
        await conn.execute(
            """UPDATE knowledge_documents
                   SET content = ?, content_hash = ?, updated_at = datetime('now')
                   WHERE id = ? AND project_id = ?""",
            (content, content_hash, doc_id, project_id),
        )
        await conn.execute(
            "DELETE FROM knowledge_chunks WHERE document_id = ?", (doc_id,)
        )
        for ordinal, (text, vec) in enumerate(chunks):
            await conn.execute(
                """INSERT INTO knowledge_chunks
                       (document_id, project_id, ordinal, text, embedding, dim)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, project_id, ordinal, text, vec_to_blob(vec), int(vec.shape[0])),
            )
        await conn.commit()


async def update_tags(
    db: Database, *, project_id: int, doc_id: int, tags: str | None
) -> bool:
    """Set/clear a document's tags in place (no chunk/embedding change)."""
    await db.init()
    async with db.connect() as conn:
        cursor = await conn.execute(
            """UPDATE knowledge_documents
                   SET tags = ?, updated_at = datetime('now')
                   WHERE id = ? AND project_id = ?""",
            (tags, doc_id, project_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_document(db: Database, *, project_id: int, doc_id: int) -> bool:
    """Delete a document and its chunks. Returns True if a row was removed."""
    await db.init()
    async with db.connect() as conn:
        await conn.execute(
            "DELETE FROM knowledge_chunks WHERE document_id = ? AND project_id = ?",
            (doc_id, project_id),
        )
        cursor = await conn.execute(
            "DELETE FROM knowledge_documents WHERE id = ? AND project_id = ?",
            (doc_id, project_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def list_documents(db: Database, project_id: int) -> list[dict[str, Any]]:
    """Return document metadata for a project, newest first (no embeddings)."""
    await db.init()
    async with db.connect() as conn:
        async with conn.execute(
            """SELECT id, title, content, source, tags, created_at, updated_at
                   FROM knowledge_documents
                   WHERE project_id = ?
                   ORDER BY updated_at DESC, id DESC""",
            (project_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "source": r["source"],
                    "tags": r["tags"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]


async def get_document(
    db: Database, project_id: int, doc_id: int
) -> dict[str, Any] | None:
    await db.init()
    async with db.connect() as conn:
        async with conn.execute(
            """SELECT id, title, content, source, tags, created_at, updated_at
                   FROM knowledge_documents WHERE id = ? AND project_id = ?""",
            (doc_id, project_id),
        ) as cursor:
            r = await cursor.fetchone()
            if not r:
                return None
            return {
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "source": r["source"],
                "tags": r["tags"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }


async def count_documents(db: Database, project_id: int) -> int:
    await db.init()
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_documents WHERE project_id = ?",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row["n"]) if row else 0


async def search_chunks(
    db: Database, project_id: int, query_vec: Vector, k: int
) -> list[dict[str, Any]]:
    """Top-k chunks by cosine similarity to *query_vec* for this project.

    Returns dicts: {doc_id, title, text, score}. One row per matching
    chunk, best score first.
    """
    await db.init()
    async with db.connect() as conn:
        async with conn.execute(
            """SELECT c.document_id AS doc_id, c.text AS text, c.embedding AS embedding,
                          c.dim AS dim, d.title AS title
                   FROM knowledge_chunks c
                   JOIN knowledge_documents d ON d.id = c.document_id
                   WHERE c.project_id = ?""",
            (project_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return []

    q = _normalize(np.asarray(query_vec, dtype=np.float32))
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        vec = _normalize(blob_to_vec(r["embedding"], int(r["dim"])))
        if vec.shape != q.shape:
            continue
        score = float(np.dot(q, vec))
        scored.append(
            (score, {"doc_id": int(r["doc_id"]), "title": r["title"], "text": r["text"]})
        )
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, item in scored[:k]:
        out.append({**item, "score": round(score, 4)})
    return out
