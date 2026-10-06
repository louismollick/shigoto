"""Polite HTTP client shared by the hand-written adapters.

Each adapter owns one `PoliteSession`, so `min_interval` acts as a per-host crawl delay.
429 and 5xx responses are retried with backoff (honoring Retry-After); other errors raise.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from shigoto.models import Liveness

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class RateLimited(Exception):
    """Raised when a host keeps answering 429 after all retries."""


class RobotsDisallowed(Exception):
    """The origin's robots rules prohibit fetching this URL."""


class PoliteSession:
    def __init__(self, min_interval: float = 0.5, retries: int = 3, timeout: float = 30.0,
                 *, respect_robots: bool = False, robots_http_fallback: str | None = None) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-CA,en;q=0.9"})
        self.min_interval = min_interval
        self.retries = retries
        self.timeout = timeout
        self._last_request = 0.0
        self.respect_robots = respect_robots
        self.robots_http_fallback = robots_http_fallback
        self._robots: dict[str, RobotFileParser] = {}

    def request(self, method: str, url: str, *, respect_robots: bool | None = None,
                **kwargs: Any) -> requests.Response:
        if not (self.respect_robots if respect_robots is None else respect_robots):
            return self._request(method, url, **kwargs)
        # Check each redirect before following it, including external detail links.
        follow = kwargs.pop("allow_redirects", True)
        for _ in range(10):
            self._check_robots(url)
            response = self._request(method, url, allow_redirects=False, **kwargs)
            if not follow or not response.is_redirect:
                return response
            url = urljoin(response.url, response.headers["Location"])
            kwargs.pop("params", None)
            if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                method = "GET"
                kwargs.pop("json", None)
                kwargs.pop("data", None)
        raise requests.TooManyRedirects(url)

    def _check_robots(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            rules = RobotFileParser(f"{origin}/robots.txt")
            # Failed robots retrieval blocks this origin until the next run.
            rules.parse(["User-agent: *", "Disallow: /"])
            self._robots[origin] = rules
            try:
                try:
                    response = self._request("GET", f"{origin}/robots.txt")
                except requests.ConnectionError:
                    if origin != self.robots_http_fallback or parsed.scheme != "https":
                        raise
                    response = self._request("GET", f"http://{parsed.netloc}/robots.txt")
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise RobotsDisallowed(f"Unable to read robots.txt for {origin}") from exc
                response = exc.response
            rules = RobotFileParser(f"{origin}/robots.txt")
            rules.parse(response.text.splitlines() if response.status_code != 404 else [])
            self._robots[origin] = rules
            delays = [float(value) for value in re.findall(r"(?im)^\s*Crawl-delay:\s*(\d+(?:\.\d+)?)", response.text)]
            self.min_interval = max([self.min_interval, *delays])
        if not self._robots[origin].can_fetch(BROWSER_UA, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    def reset_robots(self) -> None:
        """Retry unavailable robots files on the next scheduled run."""
        self._robots.clear()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
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


def posting_liveness(session: PoliteSession, url: str,
                     is_alive: Callable[[requests.Response], bool]) -> Liveness:
    """Only a posting's 404/410 is proof of removal; other failures are inconclusive."""
    try:
        response = session.request("GET", url, respect_robots=True)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (404, 410):
            return "gone"
        return "unknown"
    except (requests.RequestException, RateLimited, RobotsDisallowed):
        return "unknown"
    if response.status_code != 200:
        return "unknown"
    try:
        return "alive" if is_alive(response) else "unknown"
    except (ValueError, TypeError, KeyError):
        return "unknown"
