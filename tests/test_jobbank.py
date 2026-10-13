from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from pytest import MonkeyPatch

from shigoto.db import Store
from shigoto.sources import jobbank as jb

FEED = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Laboratory Technician</title>
<link href="https://www.jobbank.gc.ca/jobsearch/jobposting/50431084"/>
<updated>2026-10-05</updated><summary><![CDATA[<strong>Employer:</strong> Acme &amp; Co<br />
<strong>Location:</strong> Toronto (ON)<br /><strong>Salary:</strong> $28.00 hourly]]></summary></entry></feed>'''


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / 'jobs.db')


def search_page(ids: list[int], total: int = 60) -> str:
    return f'<span id="results-count">{total:,}</span>' + ''.join(
        f'''<article id="article-{i}"><a class="resultJobItem" href="/jobsearch/jobposting/{i};jsessionid=abc?source=searchresults">
        <h3><span class="flag">New Direct Apply</span><span class="noctitle">Laboratory Technician</span></h3>
        <li class="business">Acme &amp; Co</li><li class="location"><span class="wb-inv">Location</span>Toronto (ON)</li>
        <li class="salary">Salary $28.00 hourly</li><li class="date">October 05, 2026</li></a></article>'''
        for i in ids
    )


def test_html_shape_matches_feed_and_skips_malformed_row() -> None:
    jobs, total = jb.parse_search(search_page([50431084]) + '<article id="article-bad">Broken row</article>')
    [job] = jobs
    assert total == 60
    assert job == jb.parse_feed(FEED)[0]
    assert job.posted_date == date(2026, 10, 5)
    assert jb.parse_search(search_page([], 0)) == ([], 0)
    with pytest.raises(ValueError, match="results count"):
        jb.parse_search('<main>Temporarily unavailable</main>')


def test_feed_and_paginated_search_dedupe_stable_ids(monkeypatch: MonkeyPatch, store: Store) -> None:
    calls: list[dict[str, list[str]]] = []

    def get_text(url: str) -> str:
        query = parse_qs(urlparse(url).query)
        calls.append(query)
        if '/feed/' in url:
            return FEED
        page = int(query['page'][0])
        assert query['searchstring'] == ['laboratory technician'] and query['sort'] == ['D']
        assert 'locationstring' not in query
        return search_page({1: [50431084, 2], 2: [3], 3: [4]}[page])

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    jobs = list(jb.JobBankSource(['laboratory technician'], store).fetch())
    assert [job.source_id for job in jobs] == ['50431084', '2', '3', '4']
    assert len(calls) == 4  # Short/partially malformed pages do not truncate the search.


def test_parse_failures_leave_remaining_pages_and_terms(monkeypatch: MonkeyPatch, store: Store) -> None:
    def get_text(url: str) -> str:
        query = parse_qs(urlparse(url).query)
        if '/feed/' in url:
            return 'bad XML' if query['searchstring'] == ['bad'] else FEED
        if query['searchstring'] == ['bad'] and query['page'] == ['1']:
            return '<html>Bad page</html>'
        return search_page([int(query['page'][0])], 40 if query['searchstring'] == ['bad'] else 1)

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert {j.source_id for j in jb.JobBankSource(['bad', 'good'], store).fetch()} == {'50431084', '1', '2'}
    assert store.get_meta(jb.HTML_SEARCH_LAST_SUCCESS) is None


def test_repeated_search_page_stops(monkeypatch: MonkeyPatch, store: Store) -> None:
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        return search_page([1], 100)

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['quality'], store)._search())) == 1
    assert len(calls) == 2
    assert store.get_meta(jb.HTML_SEARCH_LAST_SUCCESS) is None


@pytest.mark.parametrize('markup', ['<main>Try again</main>', '<div class="job-posting-detail-requirements"></div>'])
def test_unknown_empty_detail_is_retryable(monkeypatch: MonkeyPatch, markup: str) -> None:
    monkeypatch.setattr(jb._session, 'get_text', lambda url: markup)
    with pytest.raises(ValueError, match='requirements'):
        jb.fetch_description('https://www.jobbank.gc.ca/jobsearch/jobposting/1')


def test_external_empty_and_native_detail(monkeypatch: MonkeyPatch, store: Store) -> None:
    monkeypatch.setattr(jb._session, 'get_text', lambda url: '''<section class="job-posting-content">
        <a id="externalJobLink" href="https://example.com/job">View full posting</a></section>''')
    assert jb.fetch_description('https://www.jobbank.gc.ca/jobsearch/jobposting/1') == ''
    monkeypatch.setattr(jb._session, 'get_text', lambda url: '<div class="job-posting-detail-requirements"><p>Test samples.</p></div>')
    assert jb.fetch_description('https://www.jobbank.gc.ca/jobsearch/jobposting/1') == 'Test samples.'


def test_feed_fetch_error_keeps_other_terms(monkeypatch: MonkeyPatch, store: Store) -> None:
    def get_text(url: str) -> str:
        if parse_qs(urlparse(url).query)['searchstring'] == ['bad']:
            raise requests.ConnectionError('unavailable')
        return FEED

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['bad', 'good'], store)._feed())) == 1


def test_html_gate_uses_last_success_and_feed_always_runs(monkeypatch: MonkeyPatch, store: Store) -> None:
    now = datetime(2026, 10, 13, 17, tzinfo=timezone.utc)
    assert jb.html_search_due(store, now)
    store.set_meta(jb.HTML_SEARCH_LAST_SUCCESS, (now - timedelta(hours=20)).isoformat())
    assert jb.html_search_due(store, now)
    store.set_meta(jb.HTML_SEARCH_LAST_SUCCESS, (now - timedelta(hours=20) + timedelta(seconds=1)).isoformat())
    assert not jb.html_search_due(store, now)
    store.set_meta(jb.HTML_SEARCH_LAST_SUCCESS, datetime.now(timezone.utc).isoformat())
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        return FEED

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['laboratory'], store).fetch())) == 1
    assert len(calls) == 1 and '/feed/' in calls[0]


def test_success_records_pass_start_and_skips_next_html_pass(monkeypatch: MonkeyPatch, store: Store) -> None:
    started = datetime(2026, 10, 13, 17, tzinfo=timezone.utc)
    current = started

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> 'Clock':
            return cls.fromtimestamp(current.timestamp(), timezone.utc)

    calls: list[str] = []

    def get_text(url: str) -> str:
        nonlocal current
        calls.append(url)
        if '/feed/' in url:
            return FEED
        current = started + timedelta(hours=2)
        return search_page([1], 1)

    monkeypatch.setattr(jb, 'datetime', Clock)
    monkeypatch.setattr(jb._session, 'get_text', get_text)
    source = jb.JobBankSource(['laboratory'], store)
    assert len(list(source.fetch())) == 2
    assert store.get_meta(jb.HTML_SEARCH_LAST_SUCCESS) == started.isoformat()
    calls.clear()
    assert len(list(source.fetch())) == 1
    assert len(calls) == 1 and '/feed/' in calls[0]


def test_consecutive_page_failures_do_not_crawl_page_cap(monkeypatch: MonkeyPatch, store: Store) -> None:
    last_success = '2026-10-11T17:00:00+00:00'
    store.set_meta(jb.HTML_SEARCH_LAST_SUCCESS, last_success)
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        if parse_qs(urlparse(url).query)['searchstring'] == ['bad']:
            raise requests.ConnectionError('unavailable')
        return search_page([1], 1)

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['bad', 'good'], store)._search())) == 1
    assert len(calls) == 3
    assert store.get_meta(jb.HTML_SEARCH_LAST_SUCCESS) == last_success
