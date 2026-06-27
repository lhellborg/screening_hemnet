"""A small local web UI (FastAPI) for browsing search results.

Run with: hemnet-search serve   (then open http://127.0.0.1:8000)
Everything runs locally; the page just queries the local SQLite DB.
"""

from __future__ import annotations

import html
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from .config import Config
from .db import Database
from .embed import Embedder
from .search import Filters, search


def _fmt_price(p) -> str:
    return f"{int(p):,} kr".replace(",", " ") if p else "—"


def _fmt_dist(d) -> str:
    if d is None:
        return "—"
    return f"{d/1000:.1f} km" if d >= 1000 else f"{int(d)} m"


PAGE = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hemnet-sök (lokal)</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #fafafa; color: #1a1a1a; }}
  header {{ background: #2b5d34; color: #fff; padding: 16px 24px; }}
  header h1 {{ margin: 0; font-size: 20px; font-weight: 600; }}
  form {{ display: grid; gap: 10px; padding: 16px 24px; background: #fff;
          border-bottom: 1px solid #e3e3e3; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); align-items: end; }}
  .full {{ grid-column: 1 / -1; }}
  label {{ display: block; font-size: 12px; color: #555; margin-bottom: 3px; }}
  input, select {{ width: 100%; box-sizing: border-box; padding: 7px 8px; border: 1px solid #ccc;
                   border-radius: 6px; font-size: 14px; }}
  button {{ background: #2b5d34; color: #fff; border: 0; padding: 9px 18px; border-radius: 6px;
            font-size: 14px; cursor: pointer; }}
  main {{ padding: 16px 24px; max-width: 980px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 14px; }}
  .card {{ background: #fff; border: 1px solid #e3e3e3; border-radius: 10px; padding: 14px 16px;
           margin-bottom: 12px; }}
  .card h3 {{ margin: 0 0 4px; font-size: 16px; }}
  .facts {{ color: #333; font-size: 14px; margin: 2px 0; }}
  .badges span {{ display: inline-block; background: #eef4ee; color: #2b5d34; border-radius: 999px;
                  padding: 2px 9px; font-size: 12px; margin-right: 6px; }}
  .snippet {{ color: #444; font-size: 13px; margin: 8px 0 0; line-height: 1.45; }}
  .llm {{ background: #fff7e6; border-left: 3px solid #d9a300; padding: 6px 10px; font-size: 13px; margin-top: 8px; }}
  a.listing {{ color: #2b5d34; text-decoration: none; font-weight: 600; }}
  footer {{ padding: 14px 24px; color: #888; font-size: 12px; }}
</style></head>
<body>
<header><h1>🏡 Hemnet-sök — lokal, gratis fuzzy-sökning</h1></header>
<form method="get" action="/">
  <div class="full">
    <label>Fritext (fuzzy) — t.ex. "jakt och vacker utsikt", "avskilt fritidshus"</label>
    <input name="q" value="{q}" placeholder="vad letar du efter?" autofocus>
  </div>
  <div><label>Max pris (kr)</label><input name="max_price" value="{max_price}" inputmode="numeric"></div>
  <div><label>Min boarea (m²)</label><input name="min_area" value="{min_area}" inputmode="numeric"></div>
  <div><label>Min tomt (m²)</label><input name="min_plot" value="{min_plot}" inputmode="numeric"></div>
  <div><label>Län innehåller</label><input name="county" value="{county}"></div>
  <div><label>Max avstånd skidspår (km)</label><input name="near_ski" value="{near_ski}" inputmode="numeric"></div>
  <div><label>Max avstånd skoterled (km)</label><input name="near_scooter" value="{near_scooter}" inputmode="numeric"></div>
  <div><label>&nbsp;</label><button type="submit">Sök</button></div>
</form>
<main>
  <div class="meta">{meta}</div>
  {cards}
</main>
<footer>Bostadsdata från Hemnet (publika sidor). Skid- &amp; skoterleder från © OpenStreetMap-bidragsgivare (ODbL). Endast personligt bruk.</footer>
</body></html>"""


def _card(r) -> str:
    l = r.listing
    badges = []
    if r.dist_ski_m is not None:
        badges.append(f"<span>⛷ {_fmt_dist(r.dist_ski_m)}</span>")
    if r.dist_scooter_m is not None:
        badges.append(f"<span>🛷 {_fmt_dist(r.dist_scooter_m)}</span>")
    if r.score:
        badges.append(f"<span>relevans {r.score:.2f}</span>")
    desc = (l.get("description") or "")[:280]
    title = html.escape(l.get("title") or l.get("type") or "Bostad")
    facts = " · ".join(
        x for x in [
            _fmt_price(l.get("price")),
            f"{l.get('living_area')} m²" if l.get("living_area") else None,
            f"tomt {l.get('plot_area')} m²" if l.get("plot_area") else None,
            html.escape(f"{l.get('municipality') or ''} {l.get('county') or ''}".strip()) or None,
        ] if x
    )
    llm = f'<div class="llm">🤖 {html.escape(r.llm_answer)}</div>' if r.llm_answer else ""
    return f"""<div class="card">
      <h3><a class="listing" href="{html.escape(l.get('url') or '#')}" target="_blank" rel="noopener">{title}</a></h3>
      <div class="facts">{facts}</div>
      <div class="badges">{''.join(badges)}</div>
      <p class="snippet">{html.escape(desc)}</p>
      {llm}
    </div>"""


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="Hemnet-sök")
    # One shared embedder so the model loads once.
    embedder = Embedder(cfg.embeddings_model)

    def _int(v: Optional[str]):
        try:
            return int(v) if v not in (None, "") else None
        except ValueError:
            return None

    def _float(v: Optional[str]):
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    @app.get("/", response_class=HTMLResponse)
    def home(
        q: str = Query(""),
        max_price: str = Query(""),
        min_area: str = Query(""),
        min_plot: str = Query(""),
        county: str = Query(""),
        near_ski: str = Query(""),
        near_scooter: str = Query(""),
    ) -> str:
        filters = Filters(
            max_price=_int(max_price),
            min_living_area=_float(min_area),
            min_plot_area=_float(min_plot),
            county=county or None,
            max_dist_ski_m=(_float(near_ski) * 1000) if _float(near_ski) is not None else None,
            max_dist_scooter_m=(_float(near_scooter) * 1000) if _float(near_scooter) is not None else None,
        )
        results = []
        meta = "Ange sökkriterier ovan."
        if q or any([filters.max_price, filters.min_living_area, filters.min_plot_area,
                     filters.county, filters.max_dist_ski_m, filters.max_dist_scooter_m]):
            with Database(cfg.db_path) as db:
                results = search(cfg, db, q, filters, limit=40, embedder=embedder)
            meta = f"{len(results)} träffar" + (f' för "{html.escape(q)}"' if q else "")

        cards = "".join(_card(r) for r in results) or (
            '<div class="meta">Inga träffar ännu. Kör <code>hemnet-search ingest</code> först.</div>'
            if (q or filters.county) else ""
        )
        return PAGE.format(
            q=html.escape(q), max_price=html.escape(max_price), min_area=html.escape(min_area),
            min_plot=html.escape(min_plot), county=html.escape(county),
            near_ski=html.escape(near_ski), near_scooter=html.escape(near_scooter),
            meta=meta, cards=cards,
        )

    return app
