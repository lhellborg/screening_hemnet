"""Load and access the YAML configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class FetchConfig:
    user_agent: str = "hemnet-search/0.1 (personal)"
    min_delay_seconds: float = 3.0
    jitter_seconds: float = 2.0
    max_retries: int = 4
    respect_robots: bool = True
    max_pages_per_search: int = 50
    backend: str = "httpx"            # "httpx" (fast) or "playwright" (passes JS challenges)
    browser_wait_seconds: float = 20.0  # max wait for an anti-bot challenge to clear


@dataclass
class Config:
    location_ids: list[int] = field(default_factory=list)
    item_types: list[str] = field(default_factory=list)
    bbox: list[float] = field(default_factory=lambda: [60.6, 12.5, 64.4, 19.5])
    fetch: FetchConfig = field(default_factory=FetchConfig)
    embeddings_model: str = "intfloat/multilingual-e5-base"
    ollama_enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    db_path: Path = PROJECT_ROOT / "data" / "hemnet.sqlite"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if cfg_path.exists():
            raw = yaml.safe_load(cfg_path.read_text()) or {}

        fetch_raw = raw.get("fetch", {}) or {}
        paths_raw = raw.get("paths", {}) or {}
        emb_raw = raw.get("embeddings", {}) or {}
        ollama_raw = raw.get("ollama", {}) or {}

        def resolve(p: str, default: Path) -> Path:
            if not p:
                return default
            pp = Path(p)
            return pp if pp.is_absolute() else (PROJECT_ROOT / pp)

        return cls(
            location_ids=[int(x) for x in (raw.get("location_ids") or [])],
            item_types=list(raw.get("item_types") or []),
            bbox=[float(x) for x in (raw.get("bbox") or [60.6, 12.5, 64.4, 19.5])],
            fetch=FetchConfig(
                user_agent=fetch_raw.get("user_agent", FetchConfig.user_agent),
                min_delay_seconds=float(fetch_raw.get("min_delay_seconds", 3.0)),
                jitter_seconds=float(fetch_raw.get("jitter_seconds", 2.0)),
                max_retries=int(fetch_raw.get("max_retries", 4)),
                respect_robots=bool(fetch_raw.get("respect_robots", True)),
                max_pages_per_search=int(fetch_raw.get("max_pages_per_search", 50)),
                backend=str(fetch_raw.get("backend", "httpx")),
                browser_wait_seconds=float(fetch_raw.get("browser_wait_seconds", 20.0)),
            ),
            embeddings_model=emb_raw.get("model", cls.embeddings_model),
            ollama_enabled=bool(ollama_raw.get("enabled", False)),
            ollama_url=ollama_raw.get("url", "http://localhost:11434"),
            ollama_model=ollama_raw.get("model", "llama3.1"),
            db_path=resolve(paths_raw.get("db", ""), PROJECT_ROOT / "data" / "hemnet.sqlite"),
            cache_dir=resolve(paths_raw.get("cache", ""), PROJECT_ROOT / "data" / "cache"),
        )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
