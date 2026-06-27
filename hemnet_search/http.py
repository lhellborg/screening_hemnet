"""A polite HTTP client: robots.txt, rate limiting with jitter, retries, disk cache.

Designed to be a respectful, low-rate reader of public pages only. It never bypasses
logins or anti-bot challenges; if a request is blocked it backs off and reports it.
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


class PoliteClient:
    def __init__(self, fetch: FetchConfig, cache_dir: Path):
        self.fetch = fetch
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._client = httpx.Client(
            headers={
                "User-Agent": fetch.user_agent,
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=30.0,
        )

    # -- robots -------------------------------------------------------------
    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base in self._robots:
            return self._robots[base]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(base, "/robots.txt")
        try:
            resp = self._client.get(robots_url)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # type: ignore[assignment]
        except httpx.HTTPError:
            rp = None  # type: ignore[assignment]
        self._robots[base] = rp  # type: ignore[assignment]
        return rp

    def allowed(self, url: str) -> bool:
        if not self.fetch.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            # Could not read robots.txt — be conservative but proceed (public page,
            # low rate). Caller still rate-limits.
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
    def get(self, url: str, *, use_cache: bool = True, max_age_seconds: Optional[float] = None) -> str:
        """Fetch a URL's text, using the on-disk cache when available.

        max_age_seconds: if set, cached copies older than this are refetched.
        """
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
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                last_err = exc
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue

            if resp.status_code == 200:
                cache_path.write_text(resp.text, encoding="utf-8")
                return resp.text
            if resp.status_code in (403, 429, 503):
                # Anti-bot / rate limit / unavailable — back off and retry a few times.
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
                last_err = FetchBlocked(f"HTTP {resp.status_code} for {url}")
                time.sleep(wait + random.uniform(0, 1))
                continue
            if 500 <= resp.status_code < 600:
                last_err = httpx.HTTPStatusError("server error", request=resp.request, response=resp)
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                continue
            # Other 4xx — not retryable.
            resp.raise_for_status()

        if isinstance(last_err, FetchBlocked):
            raise last_err
        raise FetchBlocked(f"Failed to fetch {url} after {self.fetch.max_retries} attempts: {last_err}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
