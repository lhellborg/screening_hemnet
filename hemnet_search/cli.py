"""Command-line interface for the personal Hemnet search tool."""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .db import Database


def _fmt_price(p) -> str:
    return f"{int(p):,} kr".replace(",", " ") if p else "—"


def _fmt_signed(n) -> str:
    sign = "+" if n >= 0 else "−"
    return f"{sign}{abs(int(n)):,} kr".replace(",", " ")


def _fmt_dist(d) -> str:
    if d is None:
        return "—"
    return f"{d/1000:.1f} km" if d >= 1000 else f"{int(d)} m"


def cmd_locations(args, cfg: Config) -> int:
    from .locations import resolve

    hits = resolve(cfg, args.query)
    if not hits:
        print(
            f"No locations found for {args.query!r} (the endpoint may be unavailable or "
            "its response shape changed). You can also find a location_id by searching on "
            "hemnet.se and copying the `location_ids[]=` value from the URL."
        )
        return 1
    print(f"location_id  type            name")
    for loc_id, name, loc_type in hits:
        print(f"{loc_id:<11}  {str(loc_type or ''):<14}  {name}")
    print("\nAdd the ids you want to `location_ids:` in config.yaml.")
    return 0


def cmd_fetch(args, cfg: Config) -> int:
    from .fetch_hemnet import ingest

    with Database(cfg.db_path) as db:
        counts = ingest(cfg, db, max_listings=args.max, refresh=args.refresh)
    print(f"\nfetch done: {counts}")
    return 0


def cmd_geo(args, cfg: Config) -> int:
    from . import osm_trails

    with Database(cfg.db_path) as db:
        if not args.skip_download:
            print("Downloading ski/scooter trails from OpenStreetMap (Overpass)...")
            counts = osm_trails.download_trails(cfg, db)
            print(f"trails: {counts}")
        n = osm_trails.compute_distances(cfg, db)
        print(f"distances computed for {n} listings")
    return 0


def cmd_embed(args, cfg: Config) -> int:
    from .embed import embed_new

    with Database(cfg.db_path) as db:
        print(f"Embedding with {cfg.embeddings_model} (first run downloads the model)...")
        n = embed_new(cfg, db)
    print(f"embedded {n} listings")
    return 0


def cmd_enrich(args, cfg: Config) -> int:
    from .broker_enrich import enrich

    with Database(cfg.db_path) as db:
        print("Fetching broker pages for taxeringsvärde / fastighetsbeteckning...")
        counts = enrich(cfg, db, max_listings=args.max)
    print(f"enrich done: {counts}")
    return 0


def cmd_ingest(args, cfg: Config) -> int:
    """Run the full pipeline: fetch -> geo -> embed."""
    rc = cmd_fetch(args, cfg)
    if rc != 0:
        return rc
    cmd_geo(args, cfg)
    cmd_embed(args, cfg)
    return 0


def cmd_search(args, cfg: Config) -> int:
    from .search import Filters, search

    filters = Filters(
        max_price=args.max_price,
        min_price=args.min_price,
        min_living_area=args.min_area,
        min_plot_area=args.min_plot,
        types=args.type or [],
        county=args.county,
        max_dist_ski_m=args.near_ski * 1000 if args.near_ski is not None else None,
        max_dist_scooter_m=args.near_scooter * 1000 if args.near_scooter is not None else None,
    )
    with Database(cfg.db_path) as db:
        results = search(cfg, db, args.query or "", filters, limit=args.limit, deep_read=args.deep)

    if not results:
        print("No matches. (Have you run `ingest`? Are there listings in the DB?)")
        return 0

    for i, r in enumerate(results, 1):
        l = r.listing
        print(f"\n{i}. {l.get('title') or l.get('type') or 'Listing'}  "
              f"[{l.get('type') or '?'}]  score={r.score:.3f}")
        print(f"   {_fmt_price(l.get('price'))} · "
              f"{l.get('living_area') or '—'} m² · "
              f"tomt {l.get('plot_area') or '—'} m² · "
              f"{l.get('municipality') or ''} {l.get('county') or ''}".rstrip())
        extra = []
        if l.get('build_year'):
            extra.append(f"byggår {l['build_year']}")
        if l.get('energy_class'):
            extra.append(f"energiklass {l['energy_class']}")
        if l.get('taxeringsvarde'):
            extra.append(f"taxeringsvärde {_fmt_price(l['taxeringsvarde'])}")
        if l.get('price') and l.get('taxeringsvarde'):
            diff = l['price'] - l['taxeringsvarde']
            extra.append(f"pris − taxeringsvärde {_fmt_signed(diff)}")
        if l.get('fastighet'):
            extra.append(f"fastighet {l['fastighet']}")
        if extra:
            print("   " + " · ".join(extra))
        print(f"   ski {_fmt_dist(r.dist_ski_m)} · scooter {_fmt_dist(r.dist_scooter_m)}")
        if l.get('broker_url'):
            print(f"   mäklarsida: {l['broker_url'][:80]}")
        if r.llm_answer:
            print(f"   LLM: {r.llm_answer}")
        print(f"   {l.get('url')}")
    return 0


def cmd_serve(args, cfg: Config) -> int:
    import uvicorn

    from .web import create_app

    app = create_app(cfg)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hemnet-search", description=__doc__)
    p.add_argument("--config", help="Path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("locations", help="Resolve a place name to Hemnet location_ids")
    sp.add_argument("query")
    sp.set_defaults(func=cmd_locations)

    sp = sub.add_parser("fetch", help="Fetch + parse listings into the DB")
    sp.add_argument("--max", type=int, help="Cap number of listings (for testing)")
    sp.add_argument("--refresh", action="store_true", help="Ignore cache, refetch")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("geo", help="Download OSM trails and compute distances")
    sp.add_argument("--skip-download", action="store_true", help="Reuse stored trails")
    sp.set_defaults(func=cmd_geo)

    sp = sub.add_parser("embed", help="Embed listing descriptions (local model)")
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("enrich", help="Fetch broker pages for taxeringsvärde / fastighetsbeteckning")
    sp.add_argument("--max", type=int, help="Cap number of broker pages to fetch")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("ingest", help="Full pipeline: fetch -> geo -> embed")
    sp.add_argument("--max", type=int, help="Cap number of listings (for testing)")
    sp.add_argument("--refresh", action="store_true")
    sp.add_argument("--skip-download", action="store_true")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("search", help="Search listings")
    sp.add_argument("query", nargs="?", help="Free-text fuzzy query, e.g. 'jakt och utsikt'")
    sp.add_argument("--max-price", type=int)
    sp.add_argument("--min-price", type=int)
    sp.add_argument("--min-area", type=float, help="Min living area m²")
    sp.add_argument("--min-plot", type=float, help="Min plot area m²")
    sp.add_argument("--type", action="append", help="Filter by type (repeatable)")
    sp.add_argument("--county", help="County contains...")
    sp.add_argument("--near-ski", type=float, help="Within N km of a ski track")
    sp.add_argument("--near-scooter", type=float, help="Within N km of a scooter trail")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--deep", action="store_true", help="Local-LLM deep read (needs Ollama)")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("serve", help="Run the local web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    cfg.ensure_dirs()
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
