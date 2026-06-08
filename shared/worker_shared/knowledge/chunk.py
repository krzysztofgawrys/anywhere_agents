"""Text chunking for knowledge ingestion.

Deliberately simple: split into paragraphs (blank-line separated), then
greedily pack paragraphs into chunks of at most ``max_chars`` with a
small character overlap between consecutive chunks for context bleed.
Agent-curated entries are short, so this is plenty - no token-aware
splitter or markdown AST needed.
"""

from __future__ import annotations

import re

_PARA_SPLIT = re.compile(r"\n\s*\n")


def chunk_text(text: str, *, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """Split *text* into overlapping chunks no longer than ``max_chars``."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    paragraphs = [p.strip() for p in _PARA_SPLIT.split(cleaned) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        # A single paragraph larger than the budget gets hard-split.
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(para, max_chars, overlap))
            continue
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    """Window a long, paragraph-less string into overlapping slices."""
    step = max(1, max_chars - overlap)
    out: list[str] = []
    for start in range(0, len(text), step):
        piece = text[start : start + max_chars]
        if piece:
            out.append(piece)
        if start + max_chars >= len(text):
            break
    return out
