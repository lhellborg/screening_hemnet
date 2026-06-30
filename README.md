# Hemnet-sök — personal, local, zero-cost property search

A personal tool to search **Hemnet** listings in a fixed region of central/northern
Sweden — not just on price/area/rooms, but on **fuzzy qualities** in the description
("jakt", "vacker utsikt", "avskilt") and on **how close each object is to ski tracks
and scooter trails**.

Everything runs **locally and free**: data is saved in one SQLite file, fuzzy search uses
a local multilingual embedding model, and the trail data comes from OpenStreetMap. No paid
APIs, no subscriptions, no per-search cost.

## How it works

```
fetch (public Hemnet pages) ─┐
OSM trails (Overpass)        ├─► SQLite (data/hemnet.sqlite) ─► local search (CLI / web UI)
local embeddings             ─┘
```

- **Fetch** — reads Hemnet's *public* listing pages politely (honors `robots.txt`,
  low request rate, disk cache) and parses the JSON embedded in each page.
- **Geo** — downloads ski tracks (`piste:type=nordic`, `route=ski`) and scooter trails
  (`snowmobile=*`) from OpenStreetMap and computes each listing's distance to the nearest.
- **Embed** — a local `sentence-transformers` model (multilingual, Swedish-capable) turns
  each description into a vector so "panorama" ≈ "utsikt", "jaktmark" ≈ "jakt", etc.
- **Search** — structured SQL filters (price, area, distances) + semantic ranking. An
  optional local LLM (Ollama) can re-read top candidates to answer the query directly.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .            # installs deps incl. playwright
.venv/bin/playwright install chromium # one-time browser download (for the fetch backend)
```

The fetcher uses a **headless browser** (`fetch.backend: playwright` in `config.yaml`) because
Hemnet's pages sit behind a Cloudflare JS challenge that a plain HTTP client can't pass. The
browser executes that challenge like any visitor, then we read the public HTML. Set
`fetch.backend: httpx` if you ever need the faster plain-HTTP path (it will be blocked on Hemnet).

## Quickstart

1. **Find the region's `location_id`s** and put them in `config.yaml`:
   ```bash
   .venv/bin/hemnet-search locations "Jämtland"
   .venv/bin/hemnet-search locations "Härjedalen"
   ```
   (If the autocomplete endpoint is unavailable, search on hemnet.se and copy the
   `location_ids[]=` value from the URL.) Add the ids under `location_ids:` in `config.yaml`,
   and tighten `bbox:` to roughly cover them.

2. **Ingest** (fetch → geo → embed). Start small to confirm it works:
   ```bash
   .venv/bin/hemnet-search ingest --max 20
   ```
   The first run downloads the embedding model (~1 GB) once.

3. **Search** from the terminal:
   ```bash
   .venv/bin/hemnet-search search "jakt och vacker utsikt" --max-price 2000000 --near-scooter 3
   ```

4. or **browse** in the local web UI:
   ```bash
   .venv/bin/hemnet-search serve     # open http://127.0.0.1:8000
   ```

Re-run `ingest` (e.g. daily, via cron) to pick up new listings — only new/changed ones are
re-fetched and re-embedded.

## Scheduled updates (launchd — no Full Disk Access needed)

`scripts/update.sh` runs the pipeline (`fetch → geo → embed → enrich`) with a lock so runs can't
overlap, logs to `data/update.log`, and re-downloads the OSM trails once a week (Sundays). Tune
the per-run volume with `HEMNET_MAX` / `HEMNET_ENRICH_MAX`.

It is scheduled with **four user LaunchAgents — one per county** — instead of one big run, so
each fetch is small and they never overlap (a single giant crawl tends to get blocked by
Hemnet's Cloudflare). They run in your login session and — unlike the cron daemon — do **not**
require Full Disk Access. Each fires weekly at **12:30** on its own weekday (a time the Mac is
likely awake; launchd runs a missed slot once on the next wake):

| County | location_id | Day |
|---|---|---|
| Dalarna | 17759 | Mon |
| Gävleborg | 17760 | Tue |
| Västernorrland | 17761 | Wed |
| Jämtland | 17762 | Thu |

Each plist sets `HEMNET_LOCATION_ID` (the county) and `HEMNET_MAX` (per-run cap). Install/manage:

```bash
for f in scripts/com.hemnet-search.update.*.plist; do
  cp "$f" ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/"$(basename "$f")"
done
launchctl list | grep hemnet                              # verify (4 agents)
tail -f data/update.log                                    # watch runs
launchctl start com.hemnet-search.update.dalarna           # trigger one now (optional)
```
Fetch one county by hand any time: `hemnet-search fetch --location-id 17759 --max 120`. The
running web app reflects new data automatically — it queries the DB per request, no restart
needed.

## Optional: local LLM "deep read" (free, needs Ollama)

Install [Ollama](https://ollama.com), `ollama pull llama3.1`, then set `ollama.enabled: true`
in `config.yaml`. Add `--deep` to a CLI search to have the model judge whether each top
candidate actually matches your question, with a one-line motivation. The tool works fully
without it.

## Map

The web UI shows the objects on a map (**Leaflet** + **OpenStreetMap** tiles — free, no API
key; Leaflet is vendored under `hemnet_search/static/`). With no search it plots **all** objects;
with a search it plots just those, kept in sync with the cards (click a card to fly to its
marker, click a marker for a popup with price/distances/taxeringsvärde and a Hemnet link).
Markers are **green when near a ski/scooter trail**, blue otherwise. A top-right toggle overlays,
for the current view: **cross-country ski tracks (blue), scooter trails (red), downhill/alpine
pistes (orange), and ski lifts (black dashed)** — so you can see resorts and lift systems. Map tiles need internet (true
of any map); everything else is local. Locations are **approximate** (see below).

## Notes & caveats

- **Anti-bot:** Hemnet uses Cloudflare bot protection. The headless-browser backend passes the
  challenge, but the fetcher is still deliberately slow and polite and backs off on `403/429`.
  Keep the request rate low (`fetch.min_delay_seconds`) and run small, scheduled batches.
- **Approximate coordinates:** Hemnet does not publish a listing's exact point in the page data,
  but each page carries the coordinates of nearby *sold comparables*. We use the **median of
  those comparables** as the listing's approximate location — good enough for km-scale "near a
  ski/scooter trail" filtering, but not a house-level pin. Listings with too few comparables get
  no distance (they still appear in search).
- **Legal / personal use:** public pages only, no login bypass, no redistribution of Hemnet
  content. Intended for personal, non-commercial use.
- **Attribution:** trail data is © OpenStreetMap contributors, ODbL.
- Not affiliated with Hemnet. The official `integration.hemnet.se` API is a *broker
  publishing* API and is not usable for reading all listings as an individual.

## Project layout

```
config.yaml                 region (location_ids, bbox), politeness, model, ollama
hemnet_search/
  config.py  http.py        config loading; polite HTTP client (robots, rate-limit, cache)
  fetch_hemnet.py locations.py   fetch + parse listings; resolve location ids
  osm_trails.py             OSM trail download + nearest-distance
  embed.py  search.py       local embeddings; hybrid search (+ optional Ollama)
  db.py  cli.py  web.py     SQLite storage; CLI; FastAPI web UI
data/hemnet.sqlite          everything is stored here
```
