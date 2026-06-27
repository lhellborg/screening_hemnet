"""Local, free semantic embeddings of listing descriptions.

Uses a multilingual sentence-transformer that handles Swedish well. The model
downloads once and runs offline on CPU. Vectors are stored in SQLite as float32
blobs; only new or changed descriptions are re-embedded.

The model is `intfloat/multilingual-e5-*` by default, which expects inputs to be
prefixed with "passage: " (documents) or "query: " (search queries).
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional, Sequence

import numpy as np

from .config import Config
from .db import Database

EncodeFn = Callable[[Sequence[str]], np.ndarray]


def listing_text(row: dict) -> str:
    """Build the text we embed for a listing."""
    parts = [
        row.get("title") or "",
        row.get("type") or "",
        row.get("municipality") or "",
        row.get("county") or "",
        row.get("description") or "",
    ]
    return " — ".join(p for p in parts if p).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _pack(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def _unpack(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4", count=dim).astype(np.float32)


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class Embedder:
    """Wraps a SentenceTransformer, prefixing inputs per the e5 convention."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None  # lazy

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _prefix(self, texts: Sequence[str], kind: str) -> list[str]:
        # e5 models want "passage: " / "query: " prefixes; harmless for others.
        if "e5" in self.model_name.lower():
            return [f"{kind}: {t}" for t in texts]
        return list(texts)

    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        model = self._ensure()
        vecs = model.encode(self._prefix(texts, "passage"), normalize_embeddings=True)
        return _normalize(np.asarray(vecs, dtype=np.float32))

    def encode_query(self, text: str) -> np.ndarray:
        model = self._ensure()
        vec = model.encode(self._prefix([text], "query"), normalize_embeddings=True)
        return _normalize(np.asarray(vec, dtype=np.float32))[0]


def embed_new(
    cfg: Config,
    db: Database,
    *,
    encode: Optional[EncodeFn] = None,
    model_name: Optional[str] = None,
    batch_size: int = 32,
) -> int:
    """Embed listings whose text is new or changed. Returns the number embedded.

    `encode` lets tests inject a fake encoder; otherwise the configured model is used.
    """
    model_name = model_name or cfg.embeddings_model
    embedder = Embedder(model_name) if encode is None else None
    encode_fn: EncodeFn = encode or embedder.encode_passages  # type: ignore[assignment]

    todo: list[tuple[str, str, str]] = []  # (listing_id, text, hash)
    for row in db.all_listings():
        text = listing_text(dict(row))
        if not text:
            continue
        h = text_hash(text)
        meta = db.get_embedding_meta(row["id"])
        if meta and meta["model"] == model_name and meta["source_hash"] == h:
            continue  # unchanged
        todo.append((row["id"], text, h))

    embedded = 0
    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        vecs = encode_fn([t for _, t, _ in batch])
        vecs = np.asarray(vecs, dtype=np.float32)
        for (listing_id, _text, h), vec in zip(batch, vecs):
            db.set_embedding(listing_id, model_name, int(vec.shape[0]), _pack(vec), h)
            embedded += 1
        db.conn.commit()
    return embedded


def load_matrix(db: Database, model_name: str) -> tuple[list[str], np.ndarray]:
    """Load all stored vectors for a model as (ids, matrix[n, dim])."""
    rows = db.all_embeddings(model_name)
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [r["listing_id"] for r in rows]
    dim = rows[0]["dim"]
    mat = np.vstack([_unpack(r["vector"], r["dim"]) for r in rows]).astype(np.float32)
    return ids, _normalize(mat)
