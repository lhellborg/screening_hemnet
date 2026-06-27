"""SQLite storage: schema and access helpers. Everything lives in one file on disk."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            TEXT PRIMARY KEY,         -- Hemnet listing id
    url           TEXT NOT NULL,
    type          TEXT,                     -- villa, fritidshus, ...
    price         INTEGER,                  -- asking price (SEK)
    fee           INTEGER,                  -- monthly fee (SEK), if any
    living_area   REAL,                     -- boarea (m2)
    plot_area     REAL,                     -- tomtarea (m2)
    rooms         REAL,
    lat           REAL,
    lon           REAL,
    municipality  TEXT,
    county        TEXT,
    title         TEXT,
    description   TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    raw_json      TEXT                       -- full original payload
);

CREATE TABLE IF NOT EXISTS geo (
    listing_id     TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    dist_ski_m     REAL,
    dist_scooter_m REAL,
    computed_at    TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    listing_id   TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,             -- float32 little-endian
    source_hash  TEXT NOT NULL,             -- hash of embedded text, to detect changes
    embedded_at  TEXT
);

CREATE TABLE IF NOT EXISTS trails (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,                 -- 'ski' or 'scooter'
    name      TEXT,
    geom_json TEXT NOT NULL                  -- JSON list of [lat, lon] points
);

CREATE TABLE IF NOT EXISTS saved (
    listing_id TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    note       TEXT,
    saved_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_county ON listings(county);
CREATE INDEX IF NOT EXISTS idx_trails_kind ON trails(kind);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- listings -----------------------------------------------------------
    def upsert_listing(self, row: dict[str, Any]) -> None:
        """Insert or update a listing. Preserves first_seen, refreshes last_seen."""
        existing = self.conn.execute(
            "SELECT first_seen FROM listings WHERE id = ?", (row["id"],)
        ).fetchone()
        first_seen = existing["first_seen"] if existing else now_iso()
        payload = {
            "id": row["id"],
            "url": row["url"],
            "type": row.get("type"),
            "price": row.get("price"),
            "fee": row.get("fee"),
            "living_area": row.get("living_area"),
            "plot_area": row.get("plot_area"),
            "rooms": row.get("rooms"),
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "municipality": row.get("municipality"),
            "county": row.get("county"),
            "title": row.get("title"),
            "description": row.get("description"),
            "first_seen": first_seen,
            "last_seen": now_iso(),
            "raw_json": json.dumps(row.get("raw") or {}, ensure_ascii=False),
        }
        cols = ", ".join(payload.keys())
        placeholders = ", ".join(f":{k}" for k in payload)
        updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "id")
        self.conn.execute(
            f"INSERT INTO listings ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            payload,
        )

    def all_listings(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM listings").fetchall()

    def listings_with_coords(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, lat, lon FROM listings WHERE lat IS NOT NULL AND lon IS NOT NULL"
        ).fetchall()

    # -- geo ----------------------------------------------------------------
    def set_geo(self, listing_id: str, dist_ski_m: Optional[float], dist_scooter_m: Optional[float]) -> None:
        self.conn.execute(
            "INSERT INTO geo (listing_id, dist_ski_m, dist_scooter_m, computed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(listing_id) DO UPDATE SET "
            "dist_ski_m=excluded.dist_ski_m, dist_scooter_m=excluded.dist_scooter_m, "
            "computed_at=excluded.computed_at",
            (listing_id, dist_ski_m, dist_scooter_m, now_iso()),
        )

    # -- trails -------------------------------------------------------------
    def clear_trails(self) -> None:
        self.conn.execute("DELETE FROM trails")

    def add_trail(self, kind: str, name: Optional[str], coords: list[list[float]]) -> None:
        self.conn.execute(
            "INSERT INTO trails (kind, name, geom_json) VALUES (?, ?, ?)",
            (kind, name, json.dumps(coords)),
        )

    def trails(self, kind: Optional[str] = None) -> list[sqlite3.Row]:
        if kind:
            return self.conn.execute("SELECT * FROM trails WHERE kind = ?", (kind,)).fetchall()
        return self.conn.execute("SELECT * FROM trails").fetchall()

    # -- embeddings ---------------------------------------------------------
    def get_embedding_meta(self, listing_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT listing_id, model, dim, source_hash FROM embeddings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()

    def set_embedding(self, listing_id: str, model: str, dim: int, vector: bytes, source_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO embeddings (listing_id, model, dim, vector, source_hash, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(listing_id) DO UPDATE SET "
            "model=excluded.model, dim=excluded.dim, vector=excluded.vector, "
            "source_hash=excluded.source_hash, embedded_at=excluded.embedded_at",
            (listing_id, model, dim, vector, source_hash, now_iso()),
        )

    def all_embeddings(self, model: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT listing_id, dim, vector FROM embeddings WHERE model = ?", (model,)
        ).fetchall()

    # -- saved --------------------------------------------------------------
    def save_listing(self, listing_id: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO saved (listing_id, note, saved_at) VALUES (?, ?, ?) "
            "ON CONFLICT(listing_id) DO UPDATE SET note=excluded.note",
            (listing_id, note, now_iso()),
        )
        self.conn.commit()
