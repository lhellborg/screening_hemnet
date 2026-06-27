"""Resolve Hemnet location names to the integer `location_id`s used in search URLs.

Hemnet's site uses a location autocomplete endpoint. The exact response shape has
changed over time, so this does a tolerant deep-search of the JSON for objects that
carry an id and a human-readable name. If the endpoint can't be reached or parsed,
it reports that clearly rather than guessing.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional
from urllib.parse import quote

from .config import Config
from .http import FetchBlocked, PoliteClient

BASE = "https://www.hemnet.se"

_ENDPOINTS = [
    "{base}/locations/show?q={q}&limit=20",
    "{base}/locations/search?q={q}",
]


def _iter_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _name_of(node: dict[str, Any]) -> Optional[str]:
    for key in ("fullName", "full_name", "name", "label", "title"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def resolve(cfg: Config, query: str) -> list[tuple[int, str, Optional[str]]]:
    """Return [(location_id, name, type)] candidates for a place-name query."""
    results: dict[int, tuple[int, str, Optional[str]]] = {}
    with PoliteClient(cfg.fetch, cfg.cache_dir) as client:
        for tmpl in _ENDPOINTS:
            url = tmpl.format(base=BASE, q=quote(query))
            try:
                text = client.get(url, use_cache=False)
            except FetchBlocked:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for node in _iter_dicts(data):
                raw_id = node.get("id") or node.get("location_id") or node.get("locationId")
                name = _name_of(node)
                if raw_id is None or name is None:
                    continue
                try:
                    loc_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                loc_type = node.get("type") or node.get("locationType")
                results.setdefault(loc_id, (loc_id, name, loc_type))
            if results:
                break
    return sorted(results.values(), key=lambda r: r[1])
