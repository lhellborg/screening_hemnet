"""Local hybrid search: structured SQL filters + semantic ranking, all free.

Optionally, a local LLM (Ollama) can re-read the top candidates to answer the
fuzzy question directly. That step is skipped automatically if Ollama isn't running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .config import Config
from .db import Database
from .embed import Embedder, load_matrix


@dataclass
class Filters:
    max_price: Optional[int] = None
    min_price: Optional[int] = None
    min_living_area: Optional[float] = None
    min_plot_area: Optional[float] = None
    types: list[str] = field(default_factory=list)
    county: Optional[str] = None
    max_dist_ski_m: Optional[float] = None
    max_dist_scooter_m: Optional[float] = None


@dataclass
class Result:
    listing_id: str
    score: float
    listing: dict
    dist_ski_m: Optional[float]
    dist_scooter_m: Optional[float]
    llm_answer: Optional[str] = None


def _apply_filters(db: Database, f: Filters) -> set[str]:
    """Return the set of listing ids passing the structured filters."""
    clauses = ["1=1"]
    params: list = []
    if f.max_price is not None:
        clauses.append("l.price IS NOT NULL AND l.price <= ?")
        params.append(f.max_price)
    if f.min_price is not None:
        clauses.append("l.price IS NOT NULL AND l.price >= ?")
        params.append(f.min_price)
    if f.min_living_area is not None:
        clauses.append("l.living_area IS NOT NULL AND l.living_area >= ?")
        params.append(f.min_living_area)
    if f.min_plot_area is not None:
        clauses.append("l.plot_area IS NOT NULL AND l.plot_area >= ?")
        params.append(f.min_plot_area)
    if f.types:
        placeholders = ",".join("?" for _ in f.types)
        clauses.append(f"l.type IN ({placeholders})")
        params.extend(f.types)
    if f.county:
        clauses.append("l.county LIKE ?")
        params.append(f"%{f.county}%")
    if f.max_dist_ski_m is not None:
        clauses.append("g.dist_ski_m IS NOT NULL AND g.dist_ski_m <= ?")
        params.append(f.max_dist_ski_m)
    if f.max_dist_scooter_m is not None:
        clauses.append("g.dist_scooter_m IS NOT NULL AND g.dist_scooter_m <= ?")
        params.append(f.max_dist_scooter_m)

    sql = (
        "SELECT l.id FROM listings l LEFT JOIN geo g ON g.listing_id = l.id "
        f"WHERE {' AND '.join(clauses)}"
    )
    rows = db.conn.execute(sql, params).fetchall()
    return {r["id"] for r in rows}


def _ollama_judge(cfg: Config, question: str, description: str) -> Optional[str]:
    """Ask a local LLM whether a listing matches the query. Returns None if unavailable."""
    import httpx

    prompt = (
        "Du bedömer en bostadsannons mot en sökfråga. Svara mycket kort: "
        "JA/NEJ/OSÄKER och en kort motivering (max en mening).\n\n"
        f"Sökfråga: {question}\n\nAnnonsbeskrivning: {description}\n\nSvar:"
    )
    try:
        resp = httpx.post(
            f"{cfg.ollama_url}/api/generate",
            json={"model": cfg.ollama_model, "prompt": prompt, "stream": False},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip() or None
    except Exception:
        return None


def search(
    cfg: Config,
    db: Database,
    query: str,
    filters: Optional[Filters] = None,
    *,
    limit: int = 20,
    embedder: Optional[Embedder] = None,
    deep_read: bool = False,
) -> list[Result]:
    filters = filters or Filters()
    allowed = _apply_filters(db, filters)
    if not allowed:
        return []

    ids, matrix = load_matrix(db, cfg.embeddings_model)
    have_embeddings = matrix.shape[0] > 0

    if query and have_embeddings:
        embedder = embedder or Embedder(cfg.embeddings_model)
        qvec = embedder.encode_query(query)
        sims = matrix @ qvec  # cosine, both normalized
        order = np.argsort(-sims)
        ranked = [(ids[i], float(sims[i])) for i in order if ids[i] in allowed]
    else:
        # No query (or no embeddings yet): just list filtered results.
        ranked = [(lid, 0.0) for lid in allowed]

    ranked = ranked[:limit]
    results: list[Result] = []
    for listing_id, score in ranked:
        row = db.conn.execute(
            "SELECT l.*, g.dist_ski_m, g.dist_scooter_m FROM listings l "
            "LEFT JOIN geo g ON g.listing_id = l.id WHERE l.id = ?",
            (listing_id,),
        ).fetchone()
        if row is None:
            continue
        listing = dict(row)
        llm_answer = None
        if deep_read and cfg.ollama_enabled and query and listing.get("description"):
            llm_answer = _ollama_judge(cfg, query, listing["description"])
        results.append(
            Result(
                listing_id=listing_id,
                score=score,
                listing=listing,
                dist_ski_m=listing.get("dist_ski_m"),
                dist_scooter_m=listing.get("dist_scooter_m"),
                llm_answer=llm_answer,
            )
        )
    return results
