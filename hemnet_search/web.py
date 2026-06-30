"""Local web UI (FastAPI) for browsing search results — in Swedish, with a map.

Run with: hemnet-search serve   (open http://127.0.0.1:8000)
Everything runs locally; the page queries the local SQLite DB. The map uses
Leaflet (vendored) + OpenStreetMap tiles (free, no key). Type your searches in
Swedish, or click the fuzzy-word chips.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .db import Database
from .embed import Embedder
from .search import Filters, search

STATIC_DIR = Path(__file__).resolve().parent / "static"
NEAR_DEFAULT_M = 2000.0  # "near a trail" when no distance filter is set

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


# --------------------------------------------------------------------------
# Map marker helpers
# --------------------------------------------------------------------------
def _popup_html(l: dict) -> str:
    """A rich info-card popup for a map marker."""
    title = html.escape(l.get("title") or l.get("type") or "Bostad")
    url = html.escape(l.get("url") or "#")
    img = ""
    if l.get("image_url"):
        img = (f'<img class="pop-img" src="{html.escape(l["image_url"])}" alt="" '
               f'loading="lazy">')
    facts = " · ".join(x for x in [
        _price(l.get("price")),
        f"{int(l['living_area'])} m²" if l.get("living_area") else None,
        f"tomt {int(l['plot_area'])} m²" if l.get("plot_area") else None,
        f"{l['rooms']:g} rok" if l.get("rooms") else None,
    ] if x)
    line2 = " · ".join(x for x in [
        html.escape(l.get("type") or ""),
        html.escape(l.get("municipality") or ""),
        f"byggår {l['build_year']}" if l.get("build_year") else None,
        f"energiklass {html.escape(str(l['energy_class']))}" if l.get("energy_class") else None,
    ] if x)
    dist = f'⛷ {_km(l.get("dist_ski_m"))} &nbsp; 🛷 {_km(l.get("dist_scooter_m"))}'
    tax = ""
    if l.get("taxeringsvarde"):
        tax = f'taxeringsvärde {_price(l["taxeringsvarde"])}'
        if l.get("price"):
            d = l["price"] - l["taxeringsvarde"]
            tax += f' · pris − tax {"+" if d >= 0 else "−"}{abs(int(d)):,} kr'.replace(",", " ")
    fast = f'<div class="pop-sub">{html.escape(str(l["fastighet"]))}</div>' if l.get("fastighet") else ""
    links = [f'<a href="{url}" target="_blank" rel="noopener">Hemnet ↗</a>']
    if l.get("broker_url"):
        links.append(f'<a href="{html.escape(l["broker_url"])}" target="_blank" rel="noopener">mäklarsida ↗</a>')
    return (
        f'<div class="pop">{img}'
        f'<div class="pop-body">'
        f'<div class="pop-title">{title}</div>'
        f'<div class="pop-facts">{facts}</div>'
        f'<div class="pop-sub">{line2}</div>'
        f'<div class="pop-dist">{dist}</div>'
        + (f'<div class="pop-sub">{tax}</div>' if tax else "")
        + fast
        + f'<div class="pop-links">{" · ".join(links)}</div>'
        f'<div class="pop-note">ungefärligt läge</div>'
        f'</div></div>'
    )


def _marker(l: dict, thr_ski: float, thr_scooter: float) -> dict:
    ds, dsc = l.get("dist_ski_m"), l.get("dist_scooter_m")
    near = (ds is not None and ds <= thr_ski) or (dsc is not None and dsc <= thr_scooter)
    return {
        "id": l["id"],
        "lat": l["lat"],
        "lon": l["lon"],
        "near": near,
        "popup": _popup_html(l),
    }


MAP_JS = """
<script>
(function () {
  L.Icon.Default.imagePath = "/static/images/";
  var data = JSON.parse(document.getElementById("markers").textContent || "[]");
  var map = L.map("map", { scrollWheelZoom: true });
  window.hemnetMap = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);

  var byId = {}, latlngs = [];
  data.forEach(function (m) {
    var mk = L.circleMarker([m.lat, m.lon], {
      radius: 7, weight: 2, color: "#ffffff",
      fillColor: m.near ? "#2e9e4f" : "#2b6cb0", fillOpacity: 0.9
    }).addTo(map);
    mk.bindPopup(m.popup, { maxWidth: 300, minWidth: 250, className: "objpop" });
    mk.on("click", function () { highlight(m.id); });
    byId[m.id] = mk;
    latlngs.push([m.lat, m.lon]);
  });
  if (latlngs.length) map.fitBounds(latlngs, { padding: [30, 30] });
  else map.setView([62.5, 15.5], 6);

  document.querySelectorAll(".card").forEach(function (c) {
    c.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      var mk = byId[c.dataset.id];
      if (mk) { map.panTo(mk.getLatLng()); mk.openPopup(); }
    });
  });
  function highlight(id) {
    var c = document.querySelector('.card[data-id="' + id + '"]');
    if (!c) return;
    document.querySelectorAll(".card.hl").forEach(function (x) { x.classList.remove("hl"); });
    c.classList.add("hl");
    c.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // Trails overlay (loaded on demand for the current view)
  var trailLayer = L.layerGroup(), trailsOn = false;
  function loadTrails() {
    if (!trailsOn) return;
    var b = map.getBounds();
    fetch("/api/trails?s=" + b.getSouth() + "&w=" + b.getWest() +
          "&n=" + b.getNorth() + "&e=" + b.getEast())
      .then(function (r) { return r.json(); })
      .then(function (t) {
        trailLayer.clearLayers();
        (t.ski || []).forEach(function (ln) {
          L.polyline(ln, { color: "#2b6cb0", weight: 2, opacity: 0.6 }).addTo(trailLayer);
        });
        (t.scooter || []).forEach(function (ln) {
          L.polyline(ln, { color: "#c0392b", weight: 2, opacity: 0.6 }).addTo(trailLayer);
        });
        (t.downhill || []).forEach(function (ln) {
          L.polyline(ln, { color: "#e67e22", weight: 3, opacity: 0.8 }).addTo(trailLayer);
        });
        (t.lift || []).forEach(function (ln) {
          L.polyline(ln, { color: "#000000", weight: 2, opacity: 0.8, dashArray: "4 4" }).addTo(trailLayer);
        });
      });
  }
  map.on("moveend", loadTrails);

  var Toggle = L.Control.extend({
    options: { position: "topright" },
    onAdd: function () {
      var d = L.DomUtil.create("div", "leaflet-bar trail-toggle");
      d.innerHTML = '<label><input type="checkbox" id="trailchk"> visa leder &amp; liftar</label>' +
        '<div class="trail-legend">' +
        '<span style="color:#2b6cb0">— skidspår</span> ' +
        '<span style="color:#c0392b">— skoterled</span> ' +
        '<span style="color:#e67e22">— utförsåkning</span> ' +
        '<span style="color:#000">--- skidlift</span></div>';
      L.DomEvent.disableClickPropagation(d);
      return d;
    }
  });
  map.addControl(new Toggle());
  document.getElementById("trailchk").addEventListener("change", function (e) {
    trailsOn = e.target.checked;
    if (trailsOn) { trailLayer.addTo(map); loadTrails(); }
    else { map.removeLayer(trailLayer); }
  });
})();
</script>
"""


PAGE = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hemnet-sök (lokal)</title>
<link rel="stylesheet" href="/static/leaflet.css">
<script src="/static/leaflet.js"></script>
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
  #map {{ height: 440px; border-radius: 12px; margin: 4px 0 18px; z-index: 0; }}
  .trail-toggle {{ background: #fff; padding: 5px 8px; font-size: 12px; }}
  .trail-legend {{ margin-top: 4px; font-size: 11px; font-weight: 600; line-height: 1.5; }}
  .trail-legend span {{ margin-right: 6px; white-space: nowrap; }}
  .objpop .leaflet-popup-content {{ margin: 0; width: 258px !important; }}
  .pop-img {{ width: 100%; height: 140px; object-fit: cover; display: block; border-radius: 12px 12px 0 0; }}
  .pop-body {{ padding: 9px 12px 11px; }}
  .pop-title {{ font-weight: 700; font-size: 14px; margin-bottom: 3px; line-height: 1.25; }}
  .pop-facts {{ font-size: 13px; color: #222; }}
  .pop-sub {{ font-size: 12px; color: #555; margin-top: 2px; }}
  .pop-dist {{ font-size: 12px; color: #1e4527; margin-top: 6px; font-weight: 600; }}
  .pop-links {{ margin-top: 8px; font-size: 13px; }}
  .pop-links a {{ color: #2b5d34; font-weight: 600; text-decoration: none; }}
  .pop-note {{ font-size: 11px; color: #999; margin-top: 6px; }}
  .meta {{ color: #555; font-size: 14px; margin-bottom: 14px; }}
  .card {{ background: #fff; border: 1px solid #e3e3e3; border-radius: 12px; padding: 15px 17px;
       margin-bottom: 13px; cursor: pointer; }}
  .card.hl {{ outline: 2px solid #2b5d34; }}
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
  a.broker {{ display: inline-block; margin-top: 9px; font-size: 13px; color: #2b5d34; font-weight: 600; text-decoration: none; }}
  a.broker:hover {{ text-decoration: underline; }}
  .rel {{ float: right; font-size: 12px; color: #888; }}
  footer {{ padding: 16px 24px; color: #888; font-size: 12px; }}
</style></head>
<body>
<header>
  <h1>🏡 Hemnet-sök</h1>
  <p>Lokal, gratis sökning med karta — skriv på svenska, eller klicka på orden nedan.</p>
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
  {map_block}
  {cards}
</main>
<footer>Bostadsdata från Hemnets publika sidor (personligt bruk). Lägen är ungefärliga.
  Karta &amp; leder &copy; OpenStreetMap-bidragsgivare.</footer>
<script>
  function syncChips() {{
    var q = document.getElementById('q').value.toLowerCase();
    document.querySelectorAll('.chip').forEach(function (c) {{
      c.classList.toggle('active', q.indexOf(c.dataset.word.toLowerCase()) !== -1);
    }});
  }}
  document.querySelectorAll('.chip').forEach(function (c) {{
    c.addEventListener('click', function () {{
      var box = document.getElementById('q'); var w = c.dataset.word; var cur = box.value.trim();
      if (cur.toLowerCase().indexOf(w.toLowerCase()) !== -1) {{
        box.value = cur.replace(new RegExp(w, 'i'), '').replace(/\\s{{2,}}/g, ' ').replace(/^[,\\s]+|[,\\s]+$/g, '');
      }} else {{ box.value = cur ? (cur + ', ' + w) : w; }}
      syncChips(); box.focus();
    }});
  }});
  function updRange(which) {{
    var v = parseFloat(document.getElementById('near_' + which).value);
    document.getElementById(which + 'val').textContent = v <= 0 ? 'valfritt' : ('≤ ' + v.toString().replace('.', ',') + ' km');
  }}
  syncChips();
</script>
{map_js}
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
    gap = None
    if l.get("price") and l.get("taxeringsvarde"):
        d = l["price"] - l["taxeringsvarde"]
        gap = ("pris − taxeringsvärde " + ("+" if d >= 0 else "−")
               + f"{abs(int(d)):,} kr".replace(",", " "))
    extra = " · ".join(x for x in [
        f"byggår {l['build_year']}" if l.get("build_year") else None,
        f"energiklass {html.escape(str(l['energy_class']))}" if l.get("energy_class") else None,
        f"taxeringsvärde {_price(l['taxeringsvarde'])}" if l.get("taxeringsvarde") else None,
        gap,
        f"fastighet {html.escape(str(l['fastighet']))}" if l.get("fastighet") else None,
    ] if x)
    extra_html = f'<div class="facts" style="color:#555">{extra}</div>' if extra else ""
    broker = (f'<a class="broker" href="{html.escape(l["broker_url"])}" target="_blank" '
              f'rel="noopener">mäklarsida ↗</a>') if l.get("broker_url") else ""
    snippet = html.escape((l.get("description") or "")[:300])
    llm = f'<div class="snippet">🤖 {html.escape(r.llm_answer)}</div>' if r.llm_answer else ""
    return f"""<div class="card" data-id="{html.escape(str(l.get('id')))}">{rel}
      <h3><a class="listing" href="{html.escape(l.get('url') or '#')}" target="_blank" rel="noopener">{title}</a></h3>
      <div class="facts">{facts}</div>
      {extra_html}
      {dist}
      <p class="snippet">{snippet}</p>
      {llm}
      {broker}
    </div>"""


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="Hemnet-sök")
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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

    @app.get("/api/trails")
    def trails(s: float, w: float, n: float, e: float):
        with Database(cfg.db_path) as db:
            rows = db.trails_in_bbox(s, w, n, e)
        out: dict[str, list] = {"ski": [], "scooter": [], "downhill": [], "lift": []}
        for t in rows[:4000]:  # cap payload size
            if t["kind"] in out:
                out[t["kind"]].append(t["coords"])
        return out

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
        thr_ski = ski_km * 1000 if ski_km > 0 else NEAR_DEFAULT_M
        thr_scooter = scooter_km * 1000 if scooter_km > 0 else NEAR_DEFAULT_M
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
        markers = []
        if has_criteria:
            with Database(cfg.db_path) as db:
                results = search(cfg, db, q, filters, limit=80, embedder=embedder)
            markers = [_marker(r.listing, thr_ski, thr_scooter)
                       for r in results if r.listing.get("lat") is not None]
            meta = (f"{len(results)} träffar" + (f' för “{html.escape(q)}”' if q else "")
                    + f" · {len(markers)} på kartan")
        else:
            with Database(cfg.db_path) as db:
                rows = [dict(x) for x in db.listings_for_map()]
            markers = [_marker(x, thr_ski, thr_scooter) for x in rows]
            meta = (f"Alla {len(markers)} objekt på kartan — sök eller filtrera ovan "
                    "för att smalna av.")

        cards = "".join(_card(r) for r in results)
        if has_criteria and not results:
            cards = ('<div class="meta">Inga träffar. Har du kört '
                     '<code>hemnet-search ingest</code>? Prova att ta bort några filter.</div>')

        map_block = ('<script id="markers" type="application/json">'
                     + json.dumps(markers) + '</script>\n<div id="map"></div>')

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
            map_block=map_block,
            cards=cards,
            map_js=MAP_JS,
        )

    return app
