"""Polite HTTP client shared by the hand-written adapters.

Each adapter owns one `PoliteSession`, so `min_interval` acts as a per-host crawl delay.
429 and 5xx responses are retried with backoff (honoring Retry-After); other errors raise.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class RateLimited(Exception):
    """Raised when a host keeps answering 429 after all retries."""


class PoliteSession:
    def __init__(self, min_interval: float = 0.5, retries: int = 3, timeout: float = 30.0) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-CA,en;q=0.9"})
        self.min_interval = min_interval
        self.retries = retries
        self.timeout = timeout
        self._last_request = 0.0

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self.retries + 1):
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                return resp
            if attempt == self.retries:
                if resp.status_code == 429:
                    raise RateLimited(url)
                resp.raise_for_status()
            backoff = _retry_after(resp) or 5.0 * 2**attempt
            log.warning("%s %s -> %s, retrying in %.0fs", method, url, resp.status_code, backoff)
            time.sleep(backoff)
        raise AssertionError("unreachable")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, body: Any, **kwargs: Any) -> Any:
        return self.request("POST", url, json=body, **kwargs).json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.request("GET", url, **kwargs).text


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After", "")
    return min(float(value), 300.0) if value.isdigit() else None
