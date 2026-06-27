"""Download ski tracks and scooter (snowmobile) trails from OpenStreetMap and
compute each listing's distance to the nearest of each.

Data: OpenStreetMap via the Overpass API (free; ODbL — attribution required).
  - Cross-country ski tracks: piste:type=nordic, route=ski
  - Scooter / snowmobile trails (skoterleder): snowmobile=*, piste:type=snowmobile
"""

from __future__ import annotations

import math
import time
from typing import Optional

import httpx
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from .config import Config
from .db import Database

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
EARTH_R = 6_371_000.0  # metres


def _overpass_query(bbox: list[float]) -> str:
    s, w, n, e = bbox
    b = f"{s},{w},{n},{e}"
    return f"""
[out:json][timeout:180];
(
  way["piste:type"="nordic"]({b});
  way["route"="ski"]({b});
  relation["route"="ski"]({b});
  way["snowmobile"]({b});
  way["piste:type"="snowmobile"]({b});
  relation["route"="snowmobile"]({b});
);
out geom;
""".strip()


def _classify(tags: dict) -> Optional[str]:
    if tags.get("snowmobile") or tags.get("piste:type") == "snowmobile" or tags.get("route") == "snowmobile":
        return "scooter"
    if tags.get("piste:type") == "nordic" or tags.get("route") == "ski":
        return "ski"
    return None


def download_trails(cfg: Config, db: Database, *, max_retries: int = 3) -> dict[str, int]:
    """Fetch trails for the configured bbox and store them. Returns counts per kind."""
    query = _overpass_query(cfg.bbox)
    # Overpass rejects browser-style UAs (406); identify as a normal script client.
    headers = {
        "User-Agent": "hemnet-search/0.1 (personal, non-commercial)",
        "Accept": "application/json",
    }
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = httpx.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=240.0)
            resp.raise_for_status()
            payload = resp.json()
            break
        except httpx.HTTPError as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 30) + 1)
    else:
        raise RuntimeError(f"Overpass request failed: {last_err}")

    counts = {"ski": 0, "scooter": 0}
    db.clear_trails()
    for el in payload.get("elements", []):
        kind = _classify(el.get("tags", {}) or {})
        if not kind:
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        coords = [[p["lat"], p["lon"]] for p in geom if "lat" in p and "lon" in p]
        if len(coords) < 2:
            continue
        name = (el.get("tags", {}) or {}).get("name")
        db.add_trail(kind, name, coords)
        counts[kind] += 1
    db.conn.commit()
    return counts


# --------------------------------------------------------------------------
# Distance computation
# --------------------------------------------------------------------------
def _projector(lat0: float):
    """Return a function mapping (lat, lon) -> (x, y) metres via local equirectangular
    projection around lat0. Accurate enough for 'within a few km' at these latitudes."""
    cos_lat0 = math.cos(math.radians(lat0))

    def project(lat: float, lon: float) -> tuple[float, float]:
        x = EARTH_R * math.radians(lon) * cos_lat0
        y = EARTH_R * math.radians(lat)
        return x, y

    return project


def _build_tree(db: Database, kind: str, project) -> Optional[STRtree]:
    lines = []
    for row in db.trails(kind):
        import json

        coords = json.loads(row["geom_json"])
        pts = [project(lat, lon) for lat, lon in coords]
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if not lines:
        return None
    return STRtree(lines)


def compute_distances(cfg: Config, db: Database) -> int:
    """For every listing with coordinates, store distance to nearest ski/scooter trail."""
    listings = db.listings_with_coords()
    if not listings:
        return 0

    lat0 = (cfg.bbox[0] + cfg.bbox[2]) / 2.0
    project = _projector(lat0)

    ski_tree = _build_tree(db, "ski", project)
    scooter_tree = _build_tree(db, "scooter", project)

    def nearest_dist(tree: Optional[STRtree], px: float, py: float) -> Optional[float]:
        if tree is None:
            return None
        pt = Point(px, py)
        idx = tree.nearest(pt)
        geom = tree.geometries[idx]
        return float(pt.distance(geom))

    n = 0
    for row in listings:
        px, py = project(row["lat"], row["lon"])
        dist_ski = nearest_dist(ski_tree, px, py)
        dist_scooter = nearest_dist(scooter_tree, px, py)
        db.set_geo(row["id"], dist_ski, dist_scooter)
        n += 1
    db.conn.commit()
    return n
