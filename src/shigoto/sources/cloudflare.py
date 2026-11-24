"""Cloudflare clearance for ZipRecruiter, solved by a Byparr browser sidecar.

ZipRecruiter answers nearly every plain request with a Cloudflare challenge, even from residential
proxies. Byparr solves the challenge once in a real browser through SHIGOTO_ZIPRECRUITER_PROXY, a
static residential IP; plain curl_cffi requests then reuse its cookies and User-Agent from that same
IP until Cloudflare challenges again. Every ZipRecruiter request goes through that clearance, so
without SHIGOTO_BYPARR_URL and SHIGOTO_ZIPRECRUITER_PROXY, ZipRecruiter only gets direct searches.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import requests
from curl_cffi.requests import Session

log = logging.getLogger(__name__)

SOLVE_URL = "https://www.ziprecruiter.com/jobs-search?search=laboratory&location=Toronto%2C+ON"
# Most solves finish in 20-35s. A failed solve keeps the browser loading ZipRecruiter until the
# timeout (about 78 MB at 90s), so a short timeout matters more than the rare slow success.
SOLVE_TIMEOUT_SECONDS = 45
# About half of solves fail. Runs are 6 hours apart, so this is a per-run budget in practice;
# a manual run is its own process and gets its own.
MAX_SOLVES = 4
SOLVE_WINDOW_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Clearance:
    proxy: str
    user_agent: str
    cookies: dict[str, str]


_current: Clearance | None = None
_solves: list[float] = []


def proxy() -> str | None:
    """Static residential proxy URL (`http://user:pass@host:port`) for every ZipRecruiter request."""
    return os.environ.get("SHIGOTO_ZIPRECRUITER_PROXY") or None


def configured() -> bool:
    return bool(os.environ.get("SHIGOTO_BYPARR_URL") and proxy())


def clearance() -> Clearance | None:
    """The current clearance, solving until one works when there is none.

    A clearance is kept until ZipRecruiter challenges a request made with it (see `invalidate`).
    Returns None when Byparr or the proxy isn't configured or every solve the budget allows failed.
    """
    global _current
    byparr, static = os.environ.get("SHIGOTO_BYPARR_URL"), proxy()
    if not _current and byparr and static:
        while not _current and _spend_solve():
            _current = _solve(byparr, static)
    return _current


def _spend_solve() -> bool:
    now = time.monotonic()
    _solves[:] = [at for at in _solves if now - at < SOLVE_WINDOW_SECONDS]
    if len(_solves) >= MAX_SOLVES:
        return False
    _solves.append(now)
    return True


def _solve(byparr: str, static: str) -> Clearance | None:
    started = time.monotonic()
    parts = urlsplit(static)
    try:
        response = requests.post(
            f"{byparr.rstrip('/')}/v1",
            json={"cmd": "request.get", "url": SOLVE_URL, "maxTimeout": SOLVE_TIMEOUT_SECONDS,
                  "returnOnlyCookies": True},
            # Byparr's per-request proxy override; the credentials stay in Shigoto's environment.
            headers={"X-Proxy-Server": f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}",
                     "X-Proxy-Username": unquote(parts.username or ""),
                     "X-Proxy-Password": unquote(parts.password or "")},
            timeout=SOLVE_TIMEOUT_SECONDS + 30,
            allow_redirects=False,  # never forward the proxy credentials elsewhere
        )
        response.raise_for_status()
        solution = response.json()["solution"]
        cookies = {str(c["name"]): str(c["value"]) for c in solution["cookies"]}
        user_agent = str(solution["userAgent"])
        status = solution["status"]
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        log.warning("Byparr could not solve ZipRecruiter's challenge: %s", exc)
        return None
    if status != 200 or "cf_clearance" not in cookies or not user_agent:
        log.warning("Byparr returned no ZipRecruiter clearance (HTTP %s, %d cookies)", status, len(cookies))
        return None
    log.info("Byparr solved ZipRecruiter's challenge in %.0fs", time.monotonic() - started)
    return Clearance(static, user_agent, cookies)


def invalidate(cleared: Clearance) -> None:
    """Drop a clearance that ZipRecruiter challenged again; the next request solves a new one."""
    global _current
    if _current is cleared:
        log.info("ZipRecruiter rejected a cleared request; solving again on the next request")
        _current = None


def apply(session: Session, cleared: Clearance) -> None:  # type: ignore[type-arg]
    """Present the solving browser's cookies and identity. Byparr's browser is Firefox."""
    for name, value in cleared.cookies.items():
        # Scoped so a redirect to an employer site doesn't receive ZipRecruiter's cookies.
        session.cookies.set(name, value, domain=".ziprecruiter.com")
    session.headers["User-Agent"] = cleared.user_agent
    session.impersonate = "firefox"
