"""Best-effort taxeringsvärde (and fastighetsbeteckning) from the broker's own page.

Hemnet doesn't publish taxeringsvärde, but it does link to the broker's own listing
page (`listingBrokerUrl`). Many brokers show taxeringsvärde and fastighetsbeteckning
there. Broker sites are all different, so we fetch the page and look for those labels
generically. This is opportunistic: it fills the value where a broker shows it, and
leaves it blank otherwise.
"""

from __future__ import annotations

import re
from typing import Optional

from .browser import BrowserClient
from .config import Config
from .db import Database
from .http import FetchBlocked, make_client

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[\s ]+")

# "Taxeringsvärde ... 1 234 000 kr"  (allow label/value separated by markup/words)
_TAX_RE = re.compile(
    r"taxeringsv[äa]rde\D{0,40}?(\d[\d  \.]{3,})", re.IGNORECASE
)
# A Swedish fastighetsbeteckning looks like "Trakt[ Block] N:M" (e.g. "Rännberg 1:21",
# "Berg Lockåsen 4:18"). Capture up to and including the "N:M" so we don't grab trailing
# label text from the page.
_FASTIGHET_RE = re.compile(
    r"fastighetsbeteckning\W{0,20}?([A-ZÅÄÖ][\wÅÄÖåäö]*(?:[ \-][A-ZÅÄÖ0-9][\wÅÄÖåäö]*){0,3}[ \-]\d+:\d+)",
    re.IGNORECASE,
)


def _text(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html))


def parse_taxering(html: str) -> Optional[int]:
    text = _text(html)
    m = _TAX_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"[  \.]", "", m.group(1))
    if not digits.isdigit():
        return None
    val = int(digits)
    # Plausibility: taxeringsvärde is typically 100k–50M SEK.
    return val if 50_000 <= val <= 100_000_000 else None


def parse_fastighet(html: str) -> Optional[str]:
    m = _FASTIGHET_RE.search(_text(html))
    if not m:
        return None
    val = m.group(1).strip(" .:-")
    return val or None


def enrich(
    cfg: Config, db: Database, *, max_listings: Optional[int] = None, render: bool = False
) -> dict[str, int]:
    """Fetch broker pages and fill taxeringsvärde / fastighetsbeteckning where present.

    render=True fully renders each page (slower, re-fetches) so values that brokers
    load via JavaScript are captured.
    """
    todo = db.listings_needing_taxering()
    if max_listings:
        todo = todo[:max_listings]
    counts = {"checked": 0, "taxering": 0, "fastighet": 0, "blocked": 0}

    client = BrowserClient(cfg.fetch, cfg.cache_dir, render=True) if render else make_client(cfg.fetch, cfg.cache_dir)
    with client:
        for i, row in enumerate(todo, 1):
            url = row["broker_url"]
            try:
                # When rendering, bypass the (non-rendered) cache to get JS-populated values.
                html = client.get(url, use_cache=not render, max_age_seconds=30 * 24 * 3600)
            except FetchBlocked:
                counts["blocked"] += 1
                continue
            except Exception:
                continue
            counts["checked"] += 1
            tax = parse_taxering(html)
            fast = parse_fastighet(html)
            if tax is not None or fast is not None:
                db.set_taxering(row["id"], tax, fast)
                db.conn.commit()
                if tax is not None:
                    counts["taxering"] += 1
                if fast is not None:
                    counts["fastighet"] += 1
            if i % 10 == 0:
                print(f"[broker {i}/{len(todo)}] taxering so far: {counts['taxering']}")
    return counts
