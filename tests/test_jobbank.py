from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from pytest import MonkeyPatch

from shigoto.sources import jobbank as jb

FEED = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Laboratory Technician</title>
<link href="https://www.jobbank.gc.ca/jobsearch/jobposting/50431084"/>
<updated>2026-10-05</updated><summary><![CDATA[<strong>Employer:</strong> Acme &amp; Co<br />
<strong>Location:</strong> Toronto (ON)<br /><strong>Salary:</strong> $28.00 hourly]]></summary></entry></feed>'''


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


def test_feed_and_paginated_search_dedupe_stable_ids(monkeypatch: MonkeyPatch) -> None:
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
    monkeypatch.setattr(jb, 'html_search_due', lambda: True)
    jobs = list(jb.JobBankSource(['laboratory technician']).fetch())
    assert [job.source_id for job in jobs] == ['50431084', '2', '3', '4']
    assert len(calls) == 4  # Short/partially malformed pages do not truncate the search.


def test_parse_failures_leave_remaining_pages_and_terms(monkeypatch: MonkeyPatch) -> None:
    def get_text(url: str) -> str:
        query = parse_qs(urlparse(url).query)
        if '/feed/' in url:
            return 'bad XML' if query['searchstring'] == ['bad'] else FEED
        if query['searchstring'] == ['bad'] and query['page'] == ['1']:
            return '<html>Bad page</html>'
        return search_page([int(query['page'][0])], 40 if query['searchstring'] == ['bad'] else 1)

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    monkeypatch.setattr(jb, 'html_search_due', lambda: True)
    assert {j.source_id for j in jb.JobBankSource(['bad', 'good']).fetch()} == {'50431084', '1', '2'}


def test_repeated_search_page_stops(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        return search_page([1], 100)

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['quality'])._search())) == 1
    assert len(calls) == 2


@pytest.mark.parametrize('markup', ['<main>Try again</main>', '<div class="job-posting-detail-requirements"></div>'])
def test_unknown_empty_detail_is_retryable(monkeypatch: MonkeyPatch, markup: str) -> None:
    monkeypatch.setattr(jb._session, 'get_text', lambda url: markup)
    with pytest.raises(ValueError, match='requirements'):
        jb.fetch_description('https://www.jobbank.gc.ca/jobsearch/jobposting/1')


def test_external_empty_and_native_detail(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(jb._session, 'get_text', lambda url: '''<section class="job-posting-content">
        <a id="externalJobLink" href="https://example.com/job">View full posting</a></section>''')
    assert jb.fetch_description('https://www.jobbank.gc.ca/jobsearch/jobposting/1') == ''
    monkeypatch.setattr(jb._session, 'get_text', lambda url: '<div class="job-posting-detail-requirements"><p>Test samples.</p></div>')
    assert jb.fetch_description('https://www.jobbank.gc.ca/jobsearch/jobposting/1') == 'Test samples.'


def test_feed_fetch_error_keeps_other_terms(monkeypatch: MonkeyPatch) -> None:
    def get_text(url: str) -> str:
        if parse_qs(urlparse(url).query)['searchstring'] == ['bad']:
            raise requests.ConnectionError('unavailable')
        return FEED

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['bad', 'good'])._feed())) == 1


def test_html_runs_in_one_utc_slot_and_feed_always_runs(monkeypatch: MonkeyPatch) -> None:

    assert jb.html_search_due(datetime(2026, 10, 13, 0, tzinfo=timezone.utc))
    assert jb.html_search_due(datetime(2026, 10, 13, 5, 59, tzinfo=timezone.utc))
    assert not jb.html_search_due(datetime(2026, 10, 13, 6, tzinfo=timezone.utc))
    assert not jb.html_search_due(datetime(2026, 10, 13, 23, tzinfo=timezone.utc))
    monkeypatch.setattr(jb, 'html_search_due', lambda: False)
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        return FEED

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['laboratory']).fetch())) == 1
    assert len(calls) == 1 and '/feed/' in calls[0]


def test_consecutive_page_failures_do_not_crawl_page_cap(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        if parse_qs(urlparse(url).query)['searchstring'] == ['bad']:
            raise requests.ConnectionError('unavailable')
        return search_page([1], 1)

    monkeypatch.setattr(jb._session, 'get_text', get_text)
    assert len(list(jb.JobBankSource(['bad', 'good'])._search())) == 1
    assert len(calls) == 3
