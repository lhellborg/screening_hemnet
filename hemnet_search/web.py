"""Local web UI (FastAPI) for browsing search results — in Swedish.

Run with: hemnet-search serve   (open http://127.0.0.1:8000)
Everything runs locally; the page queries the local SQLite DB. Type your searches
in Swedish (the embedding model is multilingual / Swedish-capable), or click the
fuzzy-word chips.
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

# Clickable fuzzy-word chips (Swedish). Clicking one adds/removes the word from the
# free-text box; the semantic model matches meaning, not just the exact word.
CHIPS = [
    "vacker utsikt", "sjönära", "avskilt", "jakt", "fiske", "fjällnära",
    "ski-in ski-out", "nära skidspår", "nära skoterled", "öppen spis",
    "bastu", "timmerhus", "renoveringsobjekt", "nyproducerat", "garage", "naturskönt",
]


def _km(d) -> str:
    if d is None:
        return "okänt"
    return f"{d/1000:.1f}".replace(".", ",") + " km"


def _price(p) -> str:
    return f"{int(p):,} kr".replace(",", " ") if p else "—"


PAGE = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hemnet-sök (lokal)</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f6f4; color: #1a1a1a; }}
  header {{ background: #2b5d34; color: #fff; padding: 18px 24px; }}
  header h1 {{ margin: 0; font-size: 20px; font-weight: 600; }}
  header p {{ margin: 4px 0 0; font-size: 13px; opacity: .85; }}
  form {{ background: #fff; padding: 18px 24px; border-bottom: 1px solid #e3e3e3; }}
  .searchrow {{ display: flex; gap: 10px; }}
  .searchrow input[type=text] {{ flex: 1; padding: 11px 12px; font-size: 15px;
       border: 1px solid #bcc; border-radius: 8px; }}
  button.go {{ background: #2b5d34; color: #fff; border: 0; padding: 0 22px; border-radius: 8px;
       font-size: 15px; font-weight: 600; cursor: pointer; }}
  .chips {{ margin: 12px 0 4px; display: flex; flex-wrap: wrap; gap: 7px; }}
  .chip {{ border: 1px solid #c4d3c4; background: #eef4ee; color: #2b5d34; border-radius: 999px;
       padding: 5px 12px; font-size: 13px; cursor: pointer; user-select: none; }}
  .chip.active {{ background: #2b5d34; color: #fff; border-color: #2b5d34; }}
  .filters {{ display: grid; gap: 14px 18px; margin-top: 14px;
       grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); align-items: end; }}
  label {{ display: block; font-size: 12px; color: #555; margin-bottom: 4px; }}
  input[type=number], select {{ width: 100%; padding: 8px; border: 1px solid #ccc;
       border-radius: 6px; font-size: 14px; }}
  input[type=range] {{ width: 100%; accent-color: #2b5d34; }}
  .rangeval {{ font-weight: 600; color: #2b5d34; }}
  main {{ padding: 18px 24px; max-width: 1000px; }}
  .meta {{ color: #555; font-size: 14px; margin-bottom: 14px; }}
  .card {{ background: #fff; border: 1px solid #e3e3e3; border-radius: 12px; padding: 15px 17px;
       margin-bottom: 13px; }}
  .card h3 {{ margin: 0 0 5px; font-size: 17px; }}
  a.listing {{ color: #2b5d34; text-decoration: none; }}
  a.listing:hover {{ text-decoration: underline; }}
  .facts {{ color: #222; font-size: 14px; margin: 3px 0 9px; }}
  .dist {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 8px 0; }}
  .dist span {{ background: #eef4ee; color: #1e4527; border-radius: 8px; padding: 5px 11px;
       font-size: 13px; font-weight: 600; }}
  .dist span.far {{ background: #f3eeee; color: #7a5a5a; }}
  .dist span.unknown {{ background: #f0f0f0; color: #888; font-weight: 400; }}
  .snippet {{ color: #444; font-size: 13px; line-height: 1.5; margin: 8px 0 0; }}
  .rel {{ float: right; font-size: 12px; color: #888; }}
  footer {{ padding: 16px 24px; color: #888; font-size: 12px; }}
</style></head>
<body>
<header>
  <h1>🏡 Hemnet-sök</h1>
  <p>Lokal, gratis sökning — skriv på svenska, eller klicka på orden nedan.</p>
</header>
<form method="get" action="/" id="searchform">
  <div class="searchrow">
    <input type="text" name="q" id="q" value="{q}" placeholder="t.ex. avskilt fritidshus med utsikt och egen jaktmark">
    <button class="go" type="submit">Sök</button>
  </div>
  <div class="chips" id="chips">{chips}</div>
  <div class="filters">
    <div><label>Max pris (kr)</label><input type="number" name="max_price" value="{max_price}" step="50000" min="0"></div>
    <div><label>Min boarea (m²)</label><input type="number" name="min_area" value="{min_area}" step="5" min="0"></div>
    <div><label>Min tomt (m²)</label><input type="number" name="min_plot" value="{min_plot}" step="100" min="0"></div>
    <div><label>Typ</label><select name="typ">{typeopts}</select></div>
    <div>
      <label>Max avstånd till <b>skidspår</b>: <span class="rangeval" id="skival">{ski_label}</span></label>
      <input type="range" name="near_ski" id="near_ski" min="0" max="15" step="0.5" value="{near_ski}" oninput="updRange('ski')">
    </div>
    <div>
      <label>Max avstånd till <b>skoterled</b>: <span class="rangeval" id="scooterval">{scooter_label}</span></label>
      <input type="range" name="near_scooter" id="near_scooter" min="0" max="15" step="0.5" value="{near_scooter}" oninput="updRange('scooter')">
    </div>
  </div>
</form>
<main>
  <div class="meta">{meta}</div>
  {cards}
</main>
<footer>Bostadsdata från Hemnets publika sidor (personligt bruk). Avstånd är ungefärliga.
  Skid- &amp; skoterleder © OpenStreetMap-bidragsgivare (ODbL).</footer>
<script>
  function tokens() {{
    return document.getElementById('q').value.split(/\\s*,\\s*|\\s{{2,}}/).map(s=>s.trim()).filter(Boolean);
  }}
  function syncChips() {{
    var q = document.getElementById('q').value.toLowerCase();
    document.querySelectorAll('.chip').forEach(function(c) {{
      c.classList.toggle('active', q.indexOf(c.dataset.word.toLowerCase()) !== -1);
    }});
  }}
  document.querySelectorAll('.chip').forEach(function(c) {{
    c.addEventListener('click', function() {{
      var box = document.getElementById('q');
      var w = c.dataset.word;
      var cur = box.value.trim();
      if (cur.toLowerCase().indexOf(w.toLowerCase()) !== -1) {{
        // remove the word (and tidy separators)
        box.value = cur.replace(new RegExp(w, 'i'), '').replace(/\\s{{2,}}/g,' ').replace(/^[,\\s]+|[,\\s]+$/g,'');
      }} else {{
        box.value = cur ? (cur + ', ' + w) : w;
      }}
      syncChips();
      box.focus();
    }});
  }});
  function updRange(which) {{
    var v = parseFloat(document.getElementById('near_'+which).value);
    document.getElementById(which+'val').textContent = v <= 0 ? 'valfritt' : ('≤ ' + v.toString().replace('.',',') + ' km');
  }}
  syncChips();
</script>
</body></html>"""


def _chip_html(active_query: str) -> str:
    ql = active_query.lower()
    out = []
    for w in CHIPS:
        cls = "chip active" if w.lower() in ql else "chip"
        out.append(f'<span class="{cls}" data-word="{html.escape(w)}">{html.escape(w)}</span>')
    return "".join(out)


def _type_options(selected: str) -> str:
    opts = [("", "Alla typer"), ("Villa", "Villa"), ("Fritidshus", "Fritidshus"),
            ("Tomt", "Tomt"), ("Gård", "Gård"), ("Lägenhet", "Lägenhet")]
    out = []
    for val, label in opts:
        sel = " selected" if val and val.lower() in selected.lower() else ""
        out.append(f'<option value="{html.escape(val)}"{sel}>{label}</option>')
    return "".join(out)


def _range_label(v: float) -> str:
    return "valfritt" if v <= 0 else f"≤ {str(v).rstrip('0').rstrip('.').replace('.', ',')} km"


def _card(r) -> str:
    l = r.listing
    title = html.escape(l.get("title") or l.get("type") or "Bostad")
    facts = " · ".join(
        x for x in [
            _price(l.get("price")),
            f"{int(l['living_area'])} m²" if l.get("living_area") else None,
            f"tomt {int(l['plot_area'])} m²" if l.get("plot_area") else None,
            f"{l['rooms']:g} rok" if l.get("rooms") else None,
            html.escape(l.get("type") or ""),
            html.escape(f"{l.get('municipality') or ''}".strip()),
        ] if x
    )

    def dist_span(label: str, icon: str, d) -> str:
        if d is None:
            return f'<span class="unknown">{icon} {label}: okänt</span>'
        cls = "" if d <= 5000 else "far"
        return f'<span class="{cls}">{icon} {label}: {_km(d)}</span>'

    dist = (f'<div class="dist">{dist_span("skidspår", "⛷", r.dist_ski_m)}'
            f'{dist_span("skoterled", "🛷", r.dist_scooter_m)}</div>')
    rel = f'<span class="rel">relevans {r.score:.2f}</span>' if r.score else ""
    snippet = html.escape((l.get("description") or "")[:300])
    llm = f'<div class="snippet">🤖 {html.escape(r.llm_answer)}</div>' if r.llm_answer else ""
    return f"""<div class="card">{rel}
      <h3><a class="listing" href="{html.escape(l.get('url') or '#')}" target="_blank" rel="noopener">{title}</a></h3>
      <div class="facts">{facts}</div>
      {dist}
      <p class="snippet">{snippet}</p>
      {llm}
    </div>"""


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="Hemnet-sök")
    embedder = Embedder(cfg.embeddings_model)  # load model once

    def _int(v: Optional[str]):
        try:
            return int(float(v)) if v not in (None, "") else None
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
        typ: str = Query(""),
        near_ski: str = Query("0"),
        near_scooter: str = Query("0"),
    ) -> str:
        ski_km = _float(near_ski) or 0.0
        scooter_km = _float(near_scooter) or 0.0
        filters = Filters(
            max_price=_int(max_price),
            min_living_area=_float(min_area),
            min_plot_area=_float(min_plot),
            types=[typ] if typ else [],
            max_dist_ski_m=ski_km * 1000 if ski_km > 0 else None,
            max_dist_scooter_m=scooter_km * 1000 if scooter_km > 0 else None,
        )
        has_criteria = bool(q) or any([
            filters.max_price, filters.min_living_area, filters.min_plot_area,
            filters.types, filters.max_dist_ski_m, filters.max_dist_scooter_m,
        ])
        results = []
        meta = "Skriv en sökning på svenska eller klicka på orden ovan."
        if has_criteria:
            with Database(cfg.db_path) as db:
                results = search(cfg, db, q, filters, limit=50, embedder=embedder)
            meta = f"{len(results)} träffar" + (f' för “{html.escape(q)}”' if q else "")

        cards = "".join(_card(r) for r in results)
        if has_criteria and not results:
            cards = ('<div class="meta">Inga träffar. Har du kört '
                     '<code>hemnet-search ingest</code>? Prova att ta bort några filter.</div>')

        return PAGE.format(
            q=html.escape(q),
            chips=_chip_html(q),
            max_price=html.escape(max_price),
            min_area=html.escape(min_area),
            min_plot=html.escape(min_plot),
            typeopts=_type_options(typ),
            near_ski=html.escape(near_ski or "0"),
            near_scooter=html.escape(near_scooter or "0"),
            ski_label=_range_label(ski_km),
            scooter_label=_range_label(scooter_km),
            meta=meta,
            cards=cards,
        )

    return app
