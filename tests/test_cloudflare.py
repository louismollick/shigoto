import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import jobspy.ziprecruiter
import pandas as pd
import pytest
import requests
from curl_cffi.requests import Cookies
from curl_cffi.requests.exceptions import HTTPError
from pytest import MonkeyPatch

from shigoto.config import City, JobSpySite
from shigoto.db import Store
from shigoto.sources import cloudflare
from shigoto.sources import ziprecruiter_detail as z
from shigoto.sources.jobspy_source import JobSpySource

URL = "https://www.ziprecruiter.com/c/Acme/Job/Lab/-in-Toronto,ON?jid=1"
HTML = '<script type="application/ld+json">{"@type": "JobPosting", "description": "<p>Analyze samples.</p>"}</script>'
PROXY = "http://login__cr.ca:secret@gw.dataimpulse.com:823"


def solved(cookies: dict[str, str] | None = None, status: int = 200) -> Mock:
    reply = Mock()
    reply.json.return_value = {"status": "ok", "solution": {
        "status": status, "userAgent": "Firefox/159.0",
        "cookies": [{"name": k, "value": v} for k, v in (cookies or {"cf_clearance": "ok"}).items()]}}
    return reply


@pytest.fixture
def byparr(monkeypatch: MonkeyPatch) -> Iterator[Mock]:
    monkeypatch.setenv("SHIGOTO_PROXY", PROXY)
    monkeypatch.setenv("SHIGOTO_BYPARR_URL", "http://byparr:8191")
    monkeypatch.setattr(cloudflare, "_current", None)
    monkeypatch.setattr(cloudflare, "_solves", [])
    post = Mock(return_value=solved())
    monkeypatch.setattr("shigoto.sources.cloudflare.requests.post", post)
    yield post


def test_sticky_proxy_adds_sessid_to_existing_parameters() -> None:
    assert cloudflare.sticky_proxy(PROXY, "abc") == "http://login__cr.ca;sessid.abc:secret@gw.dataimpulse.com:823"
    assert cloudflare.sticky_proxy("http://login:pw@host:823", "abc") == "http://login__sessid.abc:pw@host:823"


def test_unconfigured_never_solves(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("SHIGOTO_BYPARR_URL", raising=False)
    monkeypatch.setenv("SHIGOTO_PROXY", PROXY)
    monkeypatch.setattr(cloudflare, "_current", None)
    post = Mock()
    monkeypatch.setattr("shigoto.sources.cloudflare.requests.post", post)
    assert cloudflare.clearance() is None
    post.assert_not_called()


def test_solve_reuses_until_invalidated_then_uses_a_new_ip(byparr: Mock) -> None:
    first = cloudflare.clearance()
    assert first and first.cookies == {"cf_clearance": "ok"} and first.user_agent == "Firefox/159.0"
    assert cloudflare.clearance() is first
    headers = byparr.call_args.kwargs["headers"]
    assert headers["X-Proxy-Server"] == "http://gw.dataimpulse.com:823"
    assert headers["X-Proxy-Username"].startswith("login__cr.ca;sessid.") and headers["X-Proxy-Password"] == "secret"
    cloudflare.invalidate(first)
    second = cloudflare.clearance()
    assert second and second is not first and second.proxy != first.proxy
    assert byparr.call_count == 2


def test_failed_solves_are_budgeted(byparr: Mock) -> None:
    byparr.return_value = solved({"__cf_bm": "x"})  # challenge never cleared
    assert [cloudflare.clearance() for _ in range(8)] == [None] * 8
    assert byparr.call_count == cloudflare.MAX_SOLVES_PER_HOUR
    byparr.side_effect = requests.ConnectionError("down")
    cloudflare._solves.clear()
    assert cloudflare.clearance() is None


@pytest.mark.parametrize("error, solves", [
    ("response status code 403 (blocked: use a proxy)", 2),
    ("Failed to perform, curl: (56) CONNECT tunnel failed", 2),
    ("response status code 429 (blocked: use a proxy)", 1),
    ("response status code 500", 1),
])
def test_search_uses_clearance_and_resolves_only_after_a_challenge(
        error: str, solves: int, byparr: Mock, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    session = Mock(cookies=Cookies(), headers={})

    def scrape(**kwargs: object) -> pd.DataFrame:
        calls.append(kwargs)
        # JobSpy builds its own session through this factory.
        getattr(jobspy.ziprecruiter, "create_session")(session, Mock(ca_cert=None, user_agent=None))
        if len(calls) == 1:
            logging.getLogger("jobspy.ziprecruiter").error(error)
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    monkeypatch.setattr("shigoto.sources.jobspy_source.time.sleep", lambda seconds: None)
    original = getattr(jobspy.ziprecruiter, "create_session")
    store = Store(tmp_path / "t.db")
    site = JobSpySite(site="zip_recruiter", use_proxy=True, delay_seconds=0)
    list(JobSpySource(site, ["lab"], [City(name="Toronto", jobspy_location="Toronto, ON")], store).fetch())
    store.close()
    # A challenge or dead exit re-solves from a new IP; rate limits and server errors keep the clearance.
    assert len(calls) == 2 and byparr.call_count == solves
    assert all(";sessid." in str(c["proxies"]) for c in calls)
    assert (calls[0]["proxies"] != calls[1]["proxies"]) == (solves == 2)
    assert session.cookies.get("cf_clearance", domain=".ziprecruiter.com") == "ok"
    assert session.headers["User-Agent"] == "Firefox/159.0"
    assert session.impersonate == "firefox"
    assert getattr(jobspy.ziprecruiter, "create_session") is original


def test_detail_fetch_uses_clearance_and_drops_it_on_challenge(byparr: Mock, monkeypatch: MonkeyPatch) -> None:
    challenge = Mock(status_code=403, url=URL)
    challenge.raise_for_status.side_effect = HTTPError("challenge", response=challenge)
    page = Mock(status_code=200, url=URL, text=HTML, headers={"Content-Type": "text/html"})
    clients = [Mock(cookies=Cookies(), headers={}) for _ in range(2)]
    for client, reply in zip(clients, [challenge, page]):
        client.get.return_value = reply
    monkeypatch.setattr(z, "create_session", Mock(side_effect=clients))
    monkeypatch.setattr("shigoto.sources.ziprecruiter_detail.time.sleep", lambda seconds: None)
    assert z.fetch_description(URL) == ("Analyze samples.", "")
    proxies = [call.args[0] for call in getattr(z, "create_session").call_args_list]
    assert all(";sessid." in p for p in proxies) and proxies[0] != proxies[1]
    assert all(c.cookies.get("cf_clearance", domain=".ziprecruiter.com") == "ok" for c in clients)
    assert byparr.call_count == 2
