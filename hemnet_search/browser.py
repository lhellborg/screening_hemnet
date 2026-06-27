"""A headless-browser fetch backend (Playwright/Chromium).

Hemnet's public pages sit behind an anti-bot JS challenge that a plain HTTP client
cannot pass. A real browser executes that challenge like any visitor would, then we
read the resulting public HTML. This stays polite (robots.txt, rate limiting, cache)
and only reads pages that are public to any browser — it does not bypass logins.

Requires: `pip install playwright` and `playwright install chromium`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .config import FetchConfig
from .http import FetchBlocked, _BaseFetcher

# Markers that indicate an unsolved anti-bot interstitial rather than real content.
_CHALLENGE_MARKERS = (
    "Just a moment",
    "cf-challenge",
    "challenge-platform",
    "Checking your browser",
    "Attention Required",
)


class BrowserClient(_BaseFetcher):
    def __init__(self, fetch: FetchConfig, cache_dir: Path):
        super().__init__(fetch, cache_dir)
        self._pw = None
        self._browser = None
        self._context = None

    # -- browser lifecycle --------------------------------------------------
    def _ensure(self):
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise FetchBlocked(
                "Playwright is not installed. Run:\n"
                "  pip install playwright && playwright install chromium"
            ) from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception as exc:  # browser binary missing
            raise FetchBlocked(
                f"Could not launch Chromium ({exc}). Run: playwright install chromium"
            ) from exc
        self._context = self._browser.new_context(
            user_agent=self.fetch.user_agent,
            locale="sv-SE",
            viewport={"width": 1280, "height": 900},
        )

    # -- robots -------------------------------------------------------------
    def _robots_text(self, robots_url: str) -> Optional[str]:
        self._ensure()
        page = self._context.new_page()
        try:
            resp = page.goto(robots_url, wait_until="domcontentloaded", timeout=30000)
            if resp and resp.status == 200:
                # robots.txt is plain text; read the body element text.
                body = page.inner_text("body")
                return body or None
            return None
        except Exception:
            return None
        finally:
            page.close()

    # -- fetch --------------------------------------------------------------
    def _looks_like_challenge(self, html: str) -> bool:
        head = html[:4000]
        return any(m in head for m in _CHALLENGE_MARKERS)

    def _fetch_raw(self, url: str) -> tuple[int, str]:
        self._ensure()
        page = self._context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else 0

            # Give an anti-bot challenge time to resolve, polling for real content.
            deadline = time.monotonic() + self.fetch.browser_wait_seconds
            html = page.content()
            while self._looks_like_challenge(html) and time.monotonic() < deadline:
                page.wait_for_timeout(1000)
                html = page.content()
                # Once the challenge clears, the navigation usually succeeds (200).
                status = 200 if not self._looks_like_challenge(html) else status

            if self._looks_like_challenge(html):
                return 403, html
            # Treat a resolved page as success even if the original status was a challenge code.
            return (200 if status in (0, 403, 503) else status), html
        finally:
            page.close()

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = self._browser = self._pw = None
