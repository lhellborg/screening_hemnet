"""Fetch and parse public Hemnet pages.

Hemnet is a Next.js app: pages embed their data as JSON in a
`<script id="__NEXT_DATA__">` tag (and sometimes a normalized Apollo cache).
We read the public HTML and pull listing fields out of that JSON. The full
payload is kept in `raw_json` so nothing is lost if a field name shifts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlencode

from .config import Config
from .db import Database
from .http import FetchBlocked, make_client

BASE = "https://www.hemnet.se"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
# Listing detail URLs look like /bostad/<slug>-<id>
_LISTING_HREF_RE = re.compile(r'href="(/bostad/[^"#?]+)"')
_TRAILING_ID_RE = re.compile(r"-(\d+)/?$")


# --------------------------------------------------------------------------
# JSON extraction helpers
# --------------------------------------------------------------------------
def extract_next_data(html: str) -> Optional[dict[str, Any]]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _iter_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict nested anywhere inside obj."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _build_store(data: Any) -> dict[str, Any]:
    """Find Hemnet's Apollo normalized cache (a flat 'TypeName:id' -> object map)."""
    for node in _iter_dicts(data):
        hits = sum(
            1
            for k in node.keys()
            if isinstance(k, str) and ":" in k and k.split(":", 1)[0][:1].isupper()
        )
        if hits >= 5:
            return node
    return {}


def _resolve(store: dict[str, Any], value: Any) -> Any:
    """Follow an Apollo {'__ref': 'Type:id'} reference into the store."""
    if isinstance(value, dict) and "__ref" in value:
        return store.get(value["__ref"])
    return value


def _geometry_points(data: Any) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for node in _iter_dicts(data):
        if str(node.get("__typename", "")) == "GeometryPoint":
            lat = to_number(node.get("lat"))
            lon = to_number(node.get("long"))
            if lat is not None and lon is not None:
                pts.append((lat, lon))
    return pts


def _location_names(store: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Pull municipality and county fullNames from Location objects in the store."""
    municipality = county = None
    for v in store.values():
        if not (isinstance(v, dict) and v.get("__typename") == "Location"):
            continue
        loc_type = str(v.get("type", "")).upper()
        name = v.get("fullName") or v.get("name")
        if not name:
            continue
        low = name.lower()
        # `type` is sometimes absent from the page's GraphQL selection; Swedish
        # place names are self-identifying ("... kommun" / "... län").
        if (loc_type == "MUNICIPALITY" or low.endswith("kommun")) and not municipality:
            municipality = name
        elif (loc_type == "COUNTY" or low.endswith("län")) and not county:
            county = name
    return municipality, county


def _first_key(node: dict[str, Any], *names: str) -> Any:
    """Return the value of the first present key (case-insensitive)."""
    lower = {k.lower(): k for k in node.keys()}
    for name in names:
        if name.lower() in lower:
            return node[lower[name.lower()]]
    return None


def to_number(value: Any) -> Optional[float]:
    """Coerce Hemnet's various numeric shapes to a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for k in ("amount", "value", "raw", "formatted"):
            if k in value:
                return to_number(value[k])
        return None
    if isinstance(value, str):
        digits = re.sub(r"[^\d.,]", "", value).replace(",", ".")
        # keep only the first number-looking run
        m = re.search(r"\d+(?:\.\d+)?", digits)
        return float(m.group()) if m else None
    return None


def _looks_like_listing(node: dict[str, Any]) -> bool:
    typename = str(node.get("__typename", "")).lower()
    if "listing" in typename and ("activeproperty" in typename or "property" in typename or typename == "listing"):
        return True
    has_desc = _first_key(node, "description", "descriptionFormatted") is not None
    has_coord = (
        _first_key(node, "coordinate", "coordinates") is not None
        or _first_key(node, "latitude") is not None
    )
    return bool(has_desc and has_coord)


def _extract_coords(
    node: dict[str, Any], data: Any, store: dict[str, Any]
) -> tuple[Optional[float], Optional[float]]:
    coord = _resolve(store, _first_key(node, "coordinate", "coordinates"))
    if isinstance(coord, dict):
        lat = to_number(_first_key(coord, "lat", "latitude"))
        lon = to_number(_first_key(coord, "long", "lng", "lon", "longitude"))
        if lat is not None and lon is not None:
            return lat, lon
    lat = to_number(_first_key(node, "latitude"))
    lon = to_number(_first_key(node, "longitude"))
    if lat is not None and lon is not None:
        return lat, lon
    # Hemnet doesn't publish the listing's own point in this payload, but the page
    # carries the coordinates of nearby sold comparables ("SaleCard"s) that cluster
    # tightly around it. Use their median as an APPROXIMATE listing location
    # (good enough for km-scale "near ski/scooter trail" filtering). Median is
    # robust to the occasional far comparable.
    pts = _geometry_points(data)
    if len(pts) >= 3:
        lats = sorted(p[0] for p in pts)
        lons = sorted(p[1] for p in pts)
        mid = len(pts) // 2
        return lats[mid], lons[mid]
    if len(pts) == 1:
        return pts[0]
    return None, None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = _first_key(value, "text", "html", "formatted", "name", "label", "value") or ""
    text = str(value)
    # strip simple HTML tags if present
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_listing(html: str, url: str) -> Optional[dict[str, Any]]:
    """Parse a listing detail page into a listing row dict, or None if not found."""
    data = extract_next_data(html)
    if not data:
        return None

    store = _build_store(data)

    # Find the most complete listing-like node.
    candidates = [n for n in _iter_dicts(data) if _looks_like_listing(n)]
    if not candidates:
        return None
    node = max(candidates, key=lambda n: len(json.dumps(n)))

    lat, lon = _extract_coords(node, data, store)
    listing_id = _first_key(node, "id")
    if listing_id is not None:
        listing_id = str(listing_id)
    else:
        m = _TRAILING_ID_RE.search(url)
        listing_id = m.group(1) if m else None
    if not listing_id:
        return None

    municipality, county = _location_names(store)
    municipality = _clean_text(municipality) or _clean_text(_resolve(store, _first_key(node, "municipality")))
    county = _clean_text(county) or _clean_text(_resolve(store, _first_key(node, "county", "region")))
    area_name = _clean_text(_first_key(node, "area"))  # e.g. "Siksjön"
    if area_name and municipality and area_name not in municipality:
        municipality = f"{area_name}, {municipality}"

    return {
        "id": listing_id,
        "url": url if url.startswith("http") else BASE + url,
        "type": _clean_text(_first_key(node, "housingForm", "type", "propertyType")),
        "price": _to_int(to_number(_first_key(node, "askingPrice", "price"))),
        "fee": _to_int(to_number(_first_key(node, "fee", "monthlyFee"))),
        "living_area": to_number(_first_key(node, "livingArea", "formattedLivingArea", "boarea")),
        "plot_area": to_number(_first_key(node, "landArea", "formattedLandArea", "plotArea", "tomtarea")),
        "rooms": to_number(_first_key(node, "numberOfRooms", "rooms")),
        "lat": lat,
        "lon": lon,
        "municipality": municipality,
        "county": county,
        "title": _clean_text(_first_key(node, "streetAddress", "title", "heading")),
        "description": _clean_text(_first_key(node, "description", "descriptionFormatted")),
        "build_year": _to_int(to_number(_first_key(node, "legacyConstructionYear", "constructionYear", "yearBuilt"))),
        "energy_class": _energy_class(_resolve(store, _first_key(node, "energyClassification"))),
        "broker_url": _first_key(node, "listingBrokerUrl"),
        "image_url": _image_url(node),
        "raw": node,
    }


def _image_url(node: dict[str, Any]) -> Optional[str]:
    """First listing photo URL from Hemnet's image CDN (prefer a larger format)."""
    blob = json.dumps(node, ensure_ascii=False)
    for pat in (
        r"https://bilder\.hemnet\.se/images/itemgallery_L/[^\"\\ ]+\.(?:jpg|jpeg|png|webp)",
        r"https://bilder\.hemnet\.se/images/[^\"\\ ]+\.(?:jpg|jpeg|png|webp)",
    ):
        m = re.search(pat, blob)
        if m:
            return m.group(0)
    return None


def _energy_class(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("classification")
    return value if isinstance(value, str) else None


def _to_int(value: Optional[float]) -> Optional[int]:
    return int(value) if value is not None else None


# --------------------------------------------------------------------------
# Search pagination
# --------------------------------------------------------------------------
def search_url(location_ids: Iterable[int], item_types: Iterable[str], page: int = 1) -> str:
    params: list[tuple[str, str]] = []
    for lid in location_ids:
        params.append(("location_ids[]", str(lid)))
    for it in item_types:
        params.append(("item_types[]", str(it)))
    if page > 1:
        params.append(("page", str(page)))
    return f"{BASE}/bostader?{urlencode(params)}"


def listing_urls_from_search(html: str) -> list[str]:
    """Extract listing detail URLs from a search results page."""
    urls: dict[str, None] = {}
    # Prefer structured data when available.
    data = extract_next_data(html)
    if data:
        for node in _iter_dicts(data):
            href = _first_key(node, "url", "slug", "linkUrl")
            if isinstance(href, str) and "/bostad/" in href and _TRAILING_ID_RE.search(href):
                full = href if href.startswith("http") else BASE + href
                urls[full] = None
    # Fall back to scraping anchors.
    for href in _LISTING_HREF_RE.findall(html):
        urls[BASE + href] = None
    return list(urls)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def ingest(
    cfg: Config,
    db: Database,
    *,
    location_ids: Optional[list[int]] = None,
    max_listings: Optional[int] = None,
    refresh: bool = False,
) -> dict[str, int]:
    """Fetch search pages, then each listing detail page, into the database.

    Returns counts: {'urls': N, 'parsed': N, 'blocked': N}.
    """
    location_ids = location_ids if location_ids is not None else cfg.location_ids
    if not location_ids:
        raise ValueError(
            "No location_ids configured. Run `hemnet-search locations <name>` to find "
            "them and add them to config.yaml."
        )

    item_types = cfg.item_types or ["villa"]
    counts = {"urls": 0, "parsed": 0, "blocked": 0}
    seen: set[str] = set()
    max_age = None if refresh else 24 * 3600  # reuse cache <24h unless --refresh

    with make_client(cfg.fetch, cfg.cache_dir) as client:
        # 1) collect listing URLs across paginated search results
        for page in range(1, cfg.fetch.max_pages_per_search + 1):
            url = search_url(location_ids, item_types, page)
            try:
                html = client.get(url, max_age_seconds=max_age)
            except FetchBlocked as exc:
                print(f"[search p{page}] blocked: {exc}")
                counts["blocked"] += 1
                break
            page_urls = listing_urls_from_search(html)
            new = [u for u in page_urls if u not in seen]
            for u in new:
                seen.add(u)
            counts["urls"] = len(seen)
            print(f"[search p{page}] {len(new)} new listing urls (total {len(seen)})")
            if not new:
                break  # no more results
            if max_listings and len(seen) >= max_listings:
                break

        # 2) fetch + parse each listing detail page
        targets = list(seen)
        if max_listings:
            targets = targets[:max_listings]
        for i, url in enumerate(targets, 1):
            try:
                html = client.get(url, max_age_seconds=max_age)
            except FetchBlocked as exc:
                print(f"[listing {i}/{len(targets)}] blocked: {exc}")
                counts["blocked"] += 1
                continue
            row = parse_listing(html, url)
            if not row:
                print(f"[listing {i}/{len(targets)}] could not parse {url}")
                continue
            db.upsert_listing(row)
            db.conn.commit()
            counts["parsed"] += 1
            if i % 10 == 0:
                print(f"[listing {i}/{len(targets)}] parsed so far: {counts['parsed']}")

    return counts
