from __future__ import annotations

import pytest
import requests
from pytest import MonkeyPatch

from shigoto.http import PoliteSession, RobotsDisallowed, posting_liveness


def response(url: str, text: str = "", status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = text.encode()
    return result


def test_robots_cache_delay_and_disallow(monkeypatch: MonkeyPatch) -> None:
    session = PoliteSession(min_interval=0, respect_robots=True)
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(url, "User-agent: *\nCrawl-delay: 7.5\nDisallow: /private/\n" if url.endswith("robots.txt") else "posting")

    monkeypatch.setattr(session.session, "request", request)
    monkeypatch.setattr("shigoto.http.time.sleep", lambda _: None)
    assert session.get_text("https://example.com/public") == "posting"
    assert session.min_interval == 7.5
    assert session.get_text("https://example.com/other") == "posting"
    with pytest.raises(RobotsDisallowed, match="disallows"):
        session.get_text("https://example.com/private/poster")
    assert calls == ["https://example.com/robots.txt", "https://example.com/public", "https://example.com/other"]


def test_robots_404_allows_and_cache_is_per_origin(monkeypatch: MonkeyPatch) -> None:
    session = PoliteSession(min_interval=0, respect_robots=True)
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(url, status=404 if url.endswith("robots.txt") else 200)

    monkeypatch.setattr(session.session, "request", request)
    session.get_text("https://example.com/a")
    session.get_text("https://other.com/a")
    assert calls == ["https://example.com/robots.txt", "https://example.com/a",
                     "https://other.com/robots.txt", "https://other.com/a"]


@pytest.mark.parametrize("status", [401, 403, 410, 429, 500])
def test_unavailable_robots_never_means_posting_gone(monkeypatch: MonkeyPatch, status: int) -> None:
    session = PoliteSession(min_interval=0, retries=0)
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(url, status=status)

    monkeypatch.setattr(session.session, "request", request)
    assert posting_liveness(session, "https://example.com/post", lambda _: True) == "unknown"
    assert posting_liveness(session, "https://example.com/post", lambda _: True) == "unknown"
    assert calls == ["https://example.com/robots.txt"]
    session.reset_robots()
    assert posting_liveness(session, "https://example.com/post", lambda _: True) == "unknown"
    assert len(calls) == 2


@pytest.mark.parametrize("fallback", [False, True])
def test_robots_http_fallback_is_opt_in(monkeypatch: MonkeyPatch, fallback: bool) -> None:
    session = PoliteSession(min_interval=0, respect_robots=True, robots_http_fallback="https://example.com" if fallback else None)
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        if url == "https://example.com/robots.txt":
            raise requests.ConnectionError("reset")
        return response(url, "User-agent: *\nDisallow: /private/\n")

    monkeypatch.setattr(session.session, "request", request)
    if fallback:
        session.get_text("https://example.com/public")
        assert calls == ["https://example.com/robots.txt", "http://example.com/robots.txt", "https://example.com/public"]
    else:
        with pytest.raises(requests.ConnectionError):
            session.get_text("https://example.com/public")
        assert calls == ["https://example.com/robots.txt"]


def test_redirect_target_is_checked_before_fetch(monkeypatch: MonkeyPatch) -> None:
    session = PoliteSession(min_interval=0, respect_robots=True)
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        if url.endswith("robots.txt"):
            return response(url, "User-agent: *\nDisallow: /error")
        result = response(url, status=302)
        result.headers["Location"] = "/errorpage/"
        return result

    monkeypatch.setattr(session.session, "request", request)
    assert posting_liveness(session, "https://example.com/job/1", lambda _: True) == "unknown"
    assert calls == ["https://example.com/robots.txt", "https://example.com/job/1"]


def test_http_robots_fallback_stays_on_configured_origin(monkeypatch: MonkeyPatch) -> None:
    session = PoliteSession(min_interval=0, respect_robots=True, robots_http_fallback="https://gc.example")
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        raise requests.ConnectionError("reset")

    monkeypatch.setattr(session.session, "request", request)
    assert posting_liveness(session, "https://external.example/posting", lambda _: True) == "unknown"
    assert calls == ["https://external.example/robots.txt"]
