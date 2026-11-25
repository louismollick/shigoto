"""Cloudflare clearance for ZipRecruiter, solved by a Byparr browser sidecar.

ZipRecruiter answers nearly every plain request with a Cloudflare challenge, even from residential
proxies. Byparr solves the challenge once in a real browser through SHIGOTO_PROXY, a static
residential IP; plain curl_cffi requests then reuse its cookies and User-Agent from that same IP
until Cloudflare challenges again. Every ZipRecruiter request goes through that clearance, so
without SHIGOTO_BYPARR_URL and SHIGOTO_PROXY, ZipRecruiter sends nothing.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import requests
from curl_cffi.requests import Session

from shigoto.http import proxy_url

log = logging.getLogger(__name__)

SOLVE_URL = "https://www.ziprecruiter.com/jobs-search?search=laboratory&location=Toronto%2C+ON"
# Most solves finish in 20-35s. A failed solve keeps the browser loading ZipRecruiter until the
# timeout (about 78 MB at 90s), so a short timeout matters more than the rare slow success.
SOLVE_TIMEOUT_SECONDS = 45
# About half of solves fail. Only a run's searches solve (see `clearance`), so this caps a run.
MAX_SOLVES = 4


@dataclass(frozen=True)
class Clearance:
    proxy: str
    user_agent: str
    cookies: dict[str, str]


_current: Clearance | None = None
_solves_left = MAX_SOLVES


def configured() -> bool:
    return bool(os.environ.get("SHIGOTO_BYPARR_URL") and proxy_url())


def start_run() -> None:
    """Give a run its solve budget. The clearance itself carries over between runs."""
    global _solves_left
    _solves_left = MAX_SOLVES


def current() -> Clearance | None:
    """The clearance searches left, without solving. Descriptions and closure checks use this,
    so the 10-minute liveness ticks between runs never spend a run's solves."""
    return _current


def clearance() -> Clearance | None:
    """The current clearance, solving until one works when there is none. Only searches call this.

    A clearance is kept until ZipRecruiter challenges a request made with it or the proxy stops
    answering (see `invalidate`). Returns None when Byparr or the proxy isn't configured or every
    solve left in the run's budget failed.
    """
    global _current, _solves_left
    byparr, static = os.environ.get("SHIGOTO_BYPARR_URL"), proxy_url()
    while not _current and byparr and static and _solves_left > 0:
        _solves_left -= 1
        _current = _solve(byparr, static)
    return _current


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
    """Drop a clearance that ZipRecruiter challenged again or whose proxy stopped answering.
    The next search solves again, within the run's budget."""
    global _current
    if _current is cleared:
        log.info("ZipRecruiter rejected a cleared request or its proxy failed; dropping the clearance")
        _current = None


def apply(session: Session, cleared: Clearance) -> None:  # type: ignore[type-arg]
    """Present the solving browser's cookies and identity. Byparr's browser is Firefox."""
    for name, value in cleared.cookies.items():
        # Scoped so a redirect to an employer site doesn't receive ZipRecruiter's cookies.
        session.cookies.set(name, value, domain=".ziprecruiter.com")
    session.headers["User-Agent"] = cleared.user_agent
    session.impersonate = "firefox"
