"""Polite fetchers: robots.txt, rate limiting with jitter, retries, disk cache.

`_BaseFetcher` holds the shared politeness logic. `PoliteClient` fetches with plain
httpx (fast, but can't pass anti-bot JS challenges). `BrowserClient` (in browser.py)
drives a real headless browser to read the same public pages when a JS challenge is
in the way. Neither bypasses logins; both honor robots.txt and rate limits.
"""

from __future__ import annotations

import hashlib
import random
import time
import urllib.robotparser
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from .config import FetchConfig


class FetchBlocked(RuntimeError):
    """Raised when the site refuses the request (e.g. anti-bot challenge / 403)."""


class _BaseFetcher:
    """Shared cache / robots / rate-limit / retry logic.

    Subclasses implement `_fetch_raw(url) -> (status_code, text)` and
    `_robots_text(base_url) -> Optional[str]`.
    """

    def __init__(self, fetch: FetchConfig, cache_dir: Path):
        self.fetch = fetch
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request = 0.0
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    # -- robots -------------------------------------------------------------
    def _robots_text(self, base_url: str) -> Optional[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base in self._robots:
            return self._robots[base]
        rp: Optional[urllib.robotparser.RobotFileParser] = urllib.robotparser.RobotFileParser()
        text = self._robots_text(urljoin(base, "/robots.txt"))
        if text is None:
            rp = None
        else:
            rp.parse(text.splitlines())
        self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.fetch.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            # Could not read robots.txt — proceed conservatively (public page, low rate).
            return True
        return rp.can_fetch(self.fetch.user_agent, url)

    # -- rate limiting ------------------------------------------------------
    def _throttle(self) -> None:
        delay = self.fetch.min_delay_seconds + random.uniform(0, self.fetch.jitter_seconds)
        elapsed = time.monotonic() - self._last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    # -- cache --------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.html"

    # -- fetch --------------------------------------------------------------
    def _fetch_raw(self, url: str) -> tuple[int, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def get(self, url: str, *, use_cache: bool = True, max_age_seconds: Optional[float] = None) -> str:
        """Fetch a URL's text, using the on-disk cache when available."""
        cache_path = self._cache_path(url)
        if use_cache and cache_path.exists():
            if max_age_seconds is None or (time.time() - cache_path.stat().st_mtime) < max_age_seconds:
                return cache_path.read_text(encoding="utf-8")

        if not self.allowed(url):
            raise FetchBlocked(f"robots.txt disallows fetching {url}")

        last_err: Optional[Exception] = None
        for attempt in range(self.fetch.max_retries):
            self._throttle()
            try:
                status, text = self._fetch_raw(url)
            except FetchBlocked as exc:
                last_err = exc
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            except Exception as exc:  # network/browser error
                last_err = exc
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue

            if status == 200 and text:
                cache_path.write_text(text, encoding="utf-8")
                return text
            if status in (403, 429, 503):
                last_err = FetchBlocked(f"HTTP {status} for {url}")
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            if 500 <= status < 600:
                last_err = RuntimeError(f"server error {status}")
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            raise FetchBlocked(f"HTTP {status} for {url}")

        if isinstance(last_err, FetchBlocked):
            raise last_err
        raise FetchBlocked(f"Failed to fetch {url} after {self.fetch.max_retries} attempts: {last_err}")

    def close(self) -> None:  # pragma: no cover - overridden
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PoliteClient(_BaseFetcher):
    """Plain-httpx fetcher. Fast, but cannot pass anti-bot JS challenges."""

    def __init__(self, fetch: FetchConfig, cache_dir: Path):
        super().__init__(fetch, cache_dir)
        self._client = httpx.Client(
            headers={
                "User-Agent": fetch.user_agent,
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=30.0,
        )

    def _robots_text(self, robots_url: str) -> Optional[str]:
        try:
            resp = self._client.get(robots_url)
            return resp.text if resp.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def _fetch_raw(self, url: str) -> tuple[int, str]:
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(str(exc))
        return resp.status_code, resp.text

    def close(self) -> None:
        self._client.close()


def make_client(fetch: FetchConfig, cache_dir: Path) -> _BaseFetcher:
    """Return the fetch backend selected by config (`httpx` or `playwright`)."""
    backend = (fetch.backend or "httpx").lower()
    if backend == "playwright":
        from .browser import BrowserClient

        return BrowserClient(fetch, cache_dir)
    return PoliteClient(fetch, cache_dir)
