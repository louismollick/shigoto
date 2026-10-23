import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from pytest import MonkeyPatch
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from shigoto import enrich
from shigoto.config import Config
from shigoto.db import DescriptionTarget, Store
from shigoto.http import RateLimited, RobotsDisallowed
from shigoto.models import Job
from shigoto.sources import jobbank, ziprecruiter_detail

NOW = '2026-10-13T00:00:00+00:00'


@pytest.fixture(autouse=True)
def no_real_waits(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr('shigoto.enrich.time.sleep', lambda seconds: None)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / 'jobs.db')


def add_job(store: Store, source: str, source_id: str, description: str = '') -> str:
    url = f'https://www.ziprecruiter.com/job/{source_id}' if source == 'zip_recruiter' else f'https://example.com/{source_id}'
    store.upsert(Job(source=source, source_id=source_id, url=url,
                     title=f'Lab technician {source_id}', company='Acme', location='Toronto',
                     city='Toronto', description=description), NOW)
    store.commit()
    row = store.conn.execute('SELECT job_id FROM job_sources WHERE source=? AND source_id=?', (source, source_id)).fetchone()
    job_id: str = row['job_id']
    return job_id


def enrich_jobbank(store: Store) -> int:
    config = Config.model_validate({'search_terms': [], 'cities': [], 'title_keywords': [], 'sheet': {},
                                   'descriptions': {'linkedin_max_per_run': 0, 'jobbank_max_per_run': 25}})
    return enrich.enrich_descriptions(store, config)['described_jobbank']


def test_jobbank_enrichment_only_retries_unchecked_empty_postings(store: Store, monkeypatch: MonkeyPatch) -> None:
    empty = add_job(store, 'jobbank', 'empty')
    complete = add_job(store, 'jobbank', 'complete', 'Analyze samples.')
    checked = add_job(store, 'jobbank', 'checked')
    store.set_description(checked, '', NOW)
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return 'Duties\nAnalyze samples.'

    monkeypatch.setattr(jobbank, 'fetch_description', fetch)
    assert enrich_jobbank(store) == 1
    assert calls == ['https://example.com/empty']
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[empty]['description'] == 'Duties\nAnalyze samples.'
    assert rows[empty]['description_checked_at']
    assert rows[complete]['description_checked_at'] is None
    assert enrich_jobbank(store) == 0


def test_jobbank_parse_failure_leaves_target_pending_and_continues(store: Store, monkeypatch: MonkeyPatch) -> None:
    bad = add_job(store, 'jobbank', 'bad')
    add_job(store, 'jobbank', 'good')

    def fetch(url: str) -> str:
        if url.endswith('/bad'):
            raise ValueError('parse failed')
        return 'Analyze samples.'

    monkeypatch.setattr(jobbank, 'fetch_description', fetch)
    assert enrich_jobbank(store) == 1
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (bad,)).fetchone()
    assert row['description'] == '' and row['description_checked_at'] is None


def test_jobbank_retry_cap(store: Store, monkeypatch: MonkeyPatch) -> None:
    for i in range(26):
        add_job(store, 'jobbank', str(i))
    monkeypatch.setattr(jobbank, 'fetch_description', lambda url: 'Analyze samples.')
    assert enrich_jobbank(store) == 25
    assert len(store.missing_descriptions('jobbank', 30)) == 1


@pytest.mark.parametrize('status', [404, 410])
def test_jobbank_gone_posting_marks_checked_and_continues(store: Store, monkeypatch: MonkeyPatch, status: int) -> None:
    job_id = add_job(store, 'jobbank', 'gone')
    add_job(store, 'jobbank', 'good')
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        if url.endswith('/gone'):
            response = requests.Response()
            response.status_code = status
            raise requests.HTTPError(response=response)
        return 'Analyze samples.'

    monkeypatch.setattr(jobbank, 'fetch_description', fetch)
    assert enrich_jobbank(store) == 2 and len(calls) == 2
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    assert row['description'] == '' and row['description_checked_at']


def test_jobbank_rate_limit_stops_run(store: Store, monkeypatch: MonkeyPatch) -> None:
    add_job(store, 'jobbank', '1')
    add_job(store, 'jobbank', '2')
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        raise RateLimited('blocked')

    monkeypatch.setattr(jobbank, 'fetch_description', fetch)
    assert enrich_jobbank(store) == 0 and len(calls) == 1


def test_jobbank_unrecognized_detail_stays_pending_and_next_target_survives(store: Store, monkeypatch: MonkeyPatch) -> None:
    pending = add_job(store, 'jobbank', 'pending')
    external = add_job(store, 'jobbank', 'external')

    def fetch(url: str) -> str:
        if url.endswith('/pending'):
            raise ValueError('no recognized posting content')
        return ''

    assert enrich._enrich(store, 'jobbank', 60, lambda target: (fetch(target.url), ''), delay=0) == 1
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[pending]['description_checked_at'] is None
    assert rows[external]['description_checked_at']


def test_jobbank_robots_failure_leaves_targets_pending(store: Store) -> None:
    add_job(store, 'jobbank', '1')
    add_job(store, 'jobbank', '2')

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        raise RobotsDisallowed('disallowed')

    assert enrich._enrich(store, 'jobbank', 60, fetch, delay=0) == 0
    assert len(store.missing_descriptions('jobbank', 60)) == 2


def test_linkedin_salary_fills_empty_salary_only(store: Store) -> None:
    empty = add_job(store, 'linkedin', 'empty')
    known = add_job(store, 'linkedin', 'known')
    store.conn.execute("UPDATE jobs SET salary='$60,000' WHERE job_id=?", (known,))
    assert enrich._enrich(store, 'linkedin', 10, lambda t: ('QA duties.', 'CA$20.55/hr - CA$26.00/hr'),
                          delay=0) == 2
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[empty]['salary'] == 'CA$20.55/hr - CA$26.00/hr'
    assert rows[known]['salary'] == '$60,000'


def test_proxied_linkedin_failure_skips_to_next_job(store: Store) -> None:
    first = add_job(store, 'linkedin', 'first')
    second = add_job(store, 'linkedin', 'second')

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        if target.job_id == first:
            raise RateLimited('429')
        return 'QA duties.', ''

    assert enrich._enrich(store, 'linkedin', 10, fetch, delay=0, stop_on_failure=False) == 1
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[first]['description_checked_at'] is None  # retried next run
    assert rows[second]['description'] == 'QA duties.'


@pytest.mark.parametrize('failure', ['http', 'transport', 'parse', 'rate_limit'])
def test_failed_jobs_are_paced_before_the_next_fetch(store: Store, monkeypatch: MonkeyPatch, failure: str) -> None:
    for i in range(3):
        add_job(store, 'zip_recruiter', str(i))
    events: list[str | float] = []
    monkeypatch.setattr('shigoto.enrich.time.sleep', lambda delay: events.append(delay))
    monkeypatch.setattr('shigoto.enrich.random.uniform', lambda low, high: 0)

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        events.append('fetch')
        if failure == 'http':
            response = requests.Response()
            response.status_code = 403
            raise requests.HTTPError(response=response)
        if failure == 'transport':
            raise requests.RequestException('failed')
        if failure == 'rate_limit':
            raise RateLimited('blocked')
        raise ValueError('unrecognized')

    assert enrich._enrich(store, 'zip_recruiter', 3, fetch, delay=2, stop_on_failure=False) == 0
    assert events == ['fetch', 2, 'fetch', 2, 'fetch']


def test_linkedin_detail_reads_description_and_pay_box(monkeypatch: MonkeyPatch) -> None:
    from shigoto.sources import linkedin_detail
    page = """<div class="compensation__salary-range"><h3 class="compensation__heading">Base pay range</h3>
      <div class="salary compensation__salary">
        CA$20.55/hr - CA$26.00/hr
      </div></div>
      <div class="show-more-less-html__markup"><p>Perform QA checks.</p></div>"""
    monkeypatch.setattr(linkedin_detail._session, 'get_text', lambda url: page)
    assert linkedin_detail.fetch_description('li-1') == ('Perform QA checks.', 'CA$20.55/hr - CA$26.00/hr')
    monkeypatch.setattr(linkedin_detail._session, 'get_text',
                        lambda url: '<div class="show-more-less-html__markup">No pay.</div>')
    assert linkedin_detail.fetch_description('li-1') == ('No pay.', '')


@pytest.mark.parametrize('fallback', [False, True])
def test_ziprecruiter_description(monkeypatch: MonkeyPatch, fallback: bool) -> None:
    if fallback:
        data = json.dumps({'jobDetails': {'htmlFullDescription': '<p>Perform QA checks.</p>'}}, separators=(',', ':'))
        page = f'<script>self.__next_f.push([1,{json.dumps(data)}])</script>'
    else:
        page = ('<script type="application/ld+json">'
                '{"@type":"JobPosting","description":"<p>Perform QA checks.</p>"}</script>')
    session = Mock()
    session.get.return_value.url = 'https://www.ziprecruiter.com/job/1'
    session.get.return_value.text = page
    session.get.return_value.status_code = 200
    session.get.return_value.headers = {}
    factory = Mock(return_value=session)
    monkeypatch.setattr(ziprecruiter_detail, 'create_session', factory)
    monkeypatch.setenv('SHIGOTO_PROXY', 'http://proxy.test')
    url = 'https://www.ziprecruiter.com/c/Acme/Job/QA/-in-Toronto,ON?jid=1'
    assert ziprecruiter_detail.fetch_description(url) == ('Perform QA checks.', '')
    factory.assert_called_once_with('http://proxy.test')
    session.get.assert_called_once_with(url, timeout=30, allow_redirects=False, headers={})
    session.close.assert_called_once()


@pytest.mark.parametrize('status', [200, 403, 404, 410])
def test_ziprecruiter_failure_continues(store: Store, monkeypatch: MonkeyPatch, status: int) -> None:
    first = add_job(store, 'zip_recruiter', 'first')
    second = add_job(store, 'zip_recruiter', 'second')
    store.conn.execute('UPDATE jobs SET first_seen=? WHERE job_id=?', ('2026-10-14', first))
    store.commit()
    failed_attempts = 1 if status in (404, 410) else ziprecruiter_detail.MAX_REQUESTS
    sessions = [Mock() for _ in range(failed_attempts + 1)]
    for session in sessions:
        session.get.return_value.url = 'https://www.ziprecruiter.com/job/1'
        session.get.return_value.status_code = status
        session.get.return_value.headers = {}
        session.get.return_value.text = '<title>Just a moment...</title>'
        if status != 200:
            session.get.return_value.raise_for_status.side_effect = CurlHTTPError(
                'blocked or gone', response=session.get.return_value)
    sessions[-1].get.return_value.status_code = 200
    sessions[-1].get.return_value.raise_for_status.side_effect = None
    sessions[-1].get.return_value.text = ('<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"<p>QA duties.</p>"}</script>')
    factory = Mock(side_effect=sessions)
    monkeypatch.setattr(ziprecruiter_detail, 'create_session', factory)
    monkeypatch.setenv('SHIGOTO_PROXY', 'http://proxy.test')
    config = Config.model_validate({'search_terms': [], 'cities': [], 'title_keywords': [], 'sheet': {},
                                   'descriptions': {'ziprecruiter_delay_seconds': 0}})
    assert enrich.enrich_descriptions(store, config)['described_zip_recruiter'] == (2 if status in (404, 410) else 1)
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert bool(rows[first]['description_checked_at']) == (status in (404, 410))
    assert rows[second]['description'] == 'QA duties.'
    assert factory.call_count == failed_attempts + 1
    for session in sessions:
        session.close.assert_called_once()


def test_ziprecruiter_without_proxy(store: Store, monkeypatch: MonkeyPatch) -> None:
    add_job(store, 'zip_recruiter', 'pending')
    monkeypatch.delenv('SHIGOTO_PROXY', raising=False)
    factory = Mock()
    monkeypatch.setattr(ziprecruiter_detail, 'create_session', factory)
    config = Config.model_validate({'search_terms': [], 'cities': [], 'title_keywords': [], 'sheet': {}})
    assert enrich.enrich_descriptions(store, config)['described_zip_recruiter'] == 0
    factory.assert_not_called()
    assert len(store.missing_descriptions('zip_recruiter', 25)) == 1


@pytest.mark.parametrize('source', ['zip_recruiter', 'linkedin'])
@pytest.mark.parametrize('failure', ['parse', 'http', 'transport', 'rate_limit', 'robots'])
def test_failed_descriptions_rotate_across_runs(
    store: Store, monkeypatch: MonkeyPatch, source: str, failure: str,
) -> None:
    ids = [add_job(store, source, str(i)) for i in range(3)]
    for i, job_id in enumerate(ids):
        store.conn.execute('UPDATE jobs SET first_seen=? WHERE job_id=?', (f'2026-10-{10 + i}', job_id))
    store.commit()
    calls: list[str] = []

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        calls.append(target.job_id)
        if failure == 'http':
            response = requests.Response()
            response.status_code = 503
            raise requests.HTTPError(response=response)
        if failure == 'transport':
            raise requests.RequestException('connection failed')
        if failure == 'rate_limit':
            raise RateLimited('blocked')
        if failure == 'robots':
            raise RobotsDisallowed('disallowed')
        raise ValueError('unrecognized page')

    monkeypatch.setattr(enrich, 'now_iso', lambda: NOW)
    assert enrich._enrich(store, source, 2, fetch, delay=0, stop_on_failure=False) == 0
    # Robots rules still stop the source after one request.
    assert calls == ([ids[2]] if failure == 'robots' else [ids[2], ids[1]])
    monkeypatch.setattr(enrich, 'now_iso', lambda: '2026-10-14T00:00:00+00:00')
    assert enrich._enrich(store, source, 2, fetch, delay=0, stop_on_failure=False) == 0
    if failure == 'robots':
        assert calls == [ids[2], ids[1]]
    else:
        assert calls[2] == ids[0]  # The job beyond the first run's cap gets its turn.
        assert [t.job_id for t in store.missing_descriptions(source, 3)] == [ids[1], ids[2], ids[0]]
    for row in store.conn.execute('SELECT * FROM jobs'):
        assert row['description'] == '' and row['description_checked_at'] is None
        assert row['updated_at'] == NOW


@pytest.mark.parametrize('host', ['external.example', 'ziprecruiter.com.external.example', 'evilziprecruiter.com'])
@pytest.mark.parametrize('status', [200, 404, 410])
def test_ziprecruiter_external_redirect_stays_pending(
    store: Store, monkeypatch: MonkeyPatch, host: str, status: int,
) -> None:
    job_id = add_job(store, 'zip_recruiter', 'redirect')
    session = Mock()
    response = session.get.return_value
    response.url = f'https://{host}/job/1'
    response.status_code = status
    response.text = ('<script type="application/ld+json">'
                     '{"@type":"JobPosting","description":"External duties."}</script>')
    if status != 200:
        response.raise_for_status.side_effect = CurlHTTPError('gone', response=response)
    monkeypatch.setattr(ziprecruiter_detail, 'create_session', Mock(return_value=session))
    monkeypatch.setenv('SHIGOTO_PROXY', 'http://proxy.test')
    config = Config.model_validate({'search_terms': [], 'cities': [], 'title_keywords': [], 'sheet': {}})
    assert enrich.enrich_descriptions(store, config)['described_zip_recruiter'] == 0
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    assert row['description'] == '' and row['description_checked_at'] is None
    assert row['description_attempted_at']
    response.raise_for_status.assert_not_called()
    session.close.assert_called_once()


@pytest.mark.parametrize('status', [None, 0])
def test_ziprecruiter_curl_error_without_status(monkeypatch: MonkeyPatch, status: int | None) -> None:
    session = Mock()
    response = None if status is None else Mock(status_code=status)
    session.get.side_effect = CurlRequestException('connection failed', response=response)
    monkeypatch.setattr(ziprecruiter_detail, 'create_session', Mock(return_value=session))
    monkeypatch.setenv('SHIGOTO_PROXY', 'http://proxy.test')
    with pytest.raises(requests.RequestException) as exc:
        ziprecruiter_detail.fetch_description('https://ziprecruiter.com/job/1')
    assert not isinstance(exc.value, requests.HTTPError)
    assert session.close.call_count == ziprecruiter_detail.MAX_REQUESTS


def add_zip_listing(store: Store, source_id: str, jid: str, description: str = '') -> str:
    """One ZipRecruiter search listing; repeated searches give one posting (`jid`) new listing keys."""
    store.upsert(Job(source='zip_recruiter', source_id=source_id,
                     url=f'https://www.ziprecruiter.com/c/Acme/Job/QA/-in-Toronto,ON?jid={jid}',
                     title='QA technician', company='Acme', location='Toronto', city='Toronto',
                     description=description), NOW)
    store.commit()
    row = store.conn.execute('SELECT job_id FROM job_sources WHERE source=? AND source_id=?',
                             ('zip_recruiter', source_id)).fetchone()
    job_id: str = row['job_id']
    return job_id


def test_ziprecruiter_posting_key() -> None:
    url = 'https://www.ziprecruiter.com/c/Cheo/Job/Lab-Tech/-in-Ottawa,ON?jid=7993fcc84826898c'
    renamed = 'https://www.ziprecruiter.com/c/childrens-hospital/Job/Lab-Tech/-in-Ottawa,ON?jid=7993fcc84826898c'
    assert ziprecruiter_detail.posting_key(url) == ziprecruiter_detail.posting_key(renamed) == 'jid:7993fcc84826898c'
    assert ziprecruiter_detail.posting_key('https://www.ziprecruiter.com/job/1') == 'https://www.ziprecruiter.com/job/1'


def test_duplicate_listings_share_one_fetch(store: Store) -> None:
    first, second = add_zip_listing(store, 'zr-a', 'j1'), add_zip_listing(store, 'zr-b', 'j1')
    other = add_zip_listing(store, 'zr-c', 'j2')
    calls: list[str] = []

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        calls.append(target.url)
        return f'Duties for {target.url[-2:]}', '$30 hourly'

    assert enrich._enrich(store, 'zip_recruiter', 1, fetch, delay=0,
                          posting_key=ziprecruiter_detail.posting_key) == 2
    assert len(calls) == 1  # The cap counts postings, and both listings of j1 got the one fetch.
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[first]['description'] == rows[second]['description'] == 'Duties for j1'
    assert rows[first]['salary'] == rows[second]['salary'] == '$30 hourly'
    assert rows[other]['description'] == ''
    assert enrich._enrich(store, 'zip_recruiter', 1, fetch, delay=0,
                          posting_key=ziprecruiter_detail.posting_key) == 1
    assert calls[1].endswith('jid=j2')


def test_new_listing_copies_described_sibling_without_fetching(store: Store) -> None:
    add_zip_listing(store, 'zr-a', 'j1', 'Perform QA checks.')
    copy = add_zip_listing(store, 'zr-b', 'j1')
    fetch = Mock()
    assert enrich._enrich(store, 'zip_recruiter', 25, fetch, delay=0,
                          posting_key=ziprecruiter_detail.posting_key) == 1
    fetch.assert_not_called()
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (copy,)).fetchone()
    assert row['description'] == 'Perform QA checks.' and row['description_checked_at']


def test_failed_posting_moves_every_listing_back(store: Store, monkeypatch: MonkeyPatch) -> None:
    listings = [add_zip_listing(store, 'zr-a', 'j1'), add_zip_listing(store, 'zr-b', 'j1')]
    pending = add_zip_listing(store, 'zr-c', 'j2')

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        raise ValueError('challenge page')

    monkeypatch.setattr(enrich, 'now_iso', lambda: NOW)
    enrich._enrich(store, 'zip_recruiter', 2, fetch, delay=0, stop_on_failure=False,
                   posting_key=ziprecruiter_detail.posting_key)
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert all(rows[job_id]['description_attempted_at'] == NOW for job_id in [*listings, pending])
    assert all(rows[job_id]['description_checked_at'] is None for job_id in [*listings, pending])


def test_new_alias_of_failed_posting_waits_behind_untried_postings(store: Store, monkeypatch: MonkeyPatch) -> None:
    add_zip_listing(store, 'zr-a', 'failed')
    add_zip_listing(store, 'zr-b', 'untried')
    store.conn.execute("UPDATE jobs SET description_attempted_at=? WHERE job_id IN "
                       "(SELECT job_id FROM job_sources WHERE source_id='zr-a')", (NOW,))
    store.commit()
    add_zip_listing(store, 'zr-c', 'failed')  # A fresh search lists the failed posting again.
    store.conn.execute("UPDATE jobs SET first_seen='2026-10-10' WHERE job_id IN "
                       "(SELECT job_id FROM job_sources WHERE source_id IN ('zr-a', 'zr-b'))")
    store.commit()  # The alias is the newest untried job, which alone would put it first.
    calls: list[str] = []

    def fetch(target: DescriptionTarget) -> tuple[str, str]:
        calls.append(target.url)
        raise ValueError('challenge page')

    enrich._enrich(store, 'zip_recruiter', 1, fetch, delay=0, stop_on_failure=False,
                   posting_key=ziprecruiter_detail.posting_key)
    assert calls[0].endswith('jid=untried')


def test_copied_sibling_description_brings_its_salary(store: Store) -> None:
    donor = add_zip_listing(store, 'zr-a', 'j1', 'Perform QA checks.')
    store.conn.execute("UPDATE jobs SET salary='$60,000 yearly' WHERE job_id=?", (donor,))
    store.commit()
    copy = add_zip_listing(store, 'zr-b', 'j1')
    enrich._enrich(store, 'zip_recruiter', 25, Mock(), delay=0, posting_key=ziprecruiter_detail.posting_key)
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (copy,)).fetchone()
    assert row['salary'] == '$60,000 yearly'
