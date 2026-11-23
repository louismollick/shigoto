"""Cloudflare clearance for ZipRecruiter, solved by a Byparr browser sidecar.

ZipRecruiter answers nearly every plain request with a Cloudflare challenge, even from the
residential proxy (2 of 40 got through on 2026-11-22). Byparr solves the challenge once in a real
browser from a sticky proxy IP; plain curl_cffi requests then reuse its cookies and User-Agent from
that same IP until Cloudflare challenges again. Without SHIGOTO_BYPARR_URL or SHIGOTO_PROXY,
`clearance` returns None and callers keep making plain rotating-proxy requests.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import requests
from curl_cffi.requests import Session

from shigoto.http import proxy_url

log = logging.getLogger(__name__)

SOLVE_URL = "https://www.ziprecruiter.com/jobs-search?search=laboratory&location=Toronto%2C+ON"
SOLVE_TIMEOUT_SECONDS = 90
# DataImpulse holds a sessid's exit IP for about 30 minutes; Cloudflare ties clearance to that IP.
MAX_AGE_SECONDS = 20 * 60
# A solve opens a browser for up to 90s, so a challenge loop can't stall a run for long.
# Counted per process: `serve` is one long-lived process, and a manual run gets its own budget.
MAX_SOLVES_PER_HOUR = 6


@dataclass(frozen=True)
class Clearance:
    proxy: str
    user_agent: str
    cookies: dict[str, str]
    solved_at: float


_current: Clearance | None = None
_solves: list[float] = []


def sticky_proxy(proxy: str, session: str) -> str:
    """The proxy URL pinned to one exit IP with DataImpulse's `sessid` username parameter.

    Parameters follow `__` in the username and are separated by `;`, e.g. `login__cr.ca;sessid.abc`.
    """
    parts = urlsplit(proxy)
    user = unquote(parts.username or "")
    user += f"{';' if '__' in user else '__'}sessid.{session}"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(user, safe=';')}:{parts.password or ''}@{parts.hostname}{port}"
    return parts._replace(netloc=netloc).geturl()


def clearance() -> Clearance | None:
    """The current clearance, solving a new one from a fresh sticky IP when none is fresh.

    Returns None when Byparr or the proxy isn't configured, the hourly solve budget is spent,
    or the solve fails.
    """
    global _current
    now = time.monotonic()
    if _current and now - _current.solved_at < MAX_AGE_SECONDS:
        return _current
    _current = None
    byparr, proxy = os.environ.get("SHIGOTO_BYPARR_URL"), proxy_url()
    if not (byparr and proxy):
        return None
    _solves[:] = [at for at in _solves if now - at < 3600]
    if len(_solves) >= MAX_SOLVES_PER_HOUR:
        return None
    _solves.append(now)
    try:
        sticky = sticky_proxy(proxy, secrets.token_hex(6))
        parts = urlsplit(sticky)
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
    log.info("Byparr solved ZipRecruiter's challenge in %.0fs", time.monotonic() - now)
    _current = Clearance(sticky, user_agent, cookies, now)
    return _current


def invalidate(cleared: Clearance) -> None:
    """Drop a clearance that was challenged again or whose sticky IP stopped answering.

    The next request solves from a new IP.
    """
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
