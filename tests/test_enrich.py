from pathlib import Path

import pytest
import requests
from pytest import MonkeyPatch

from shigoto import enrich
from shigoto.config import Config
from shigoto.db import DescriptionTarget, Store
from shigoto.http import RateLimited, RobotsDisallowed
from shigoto.models import Job
from shigoto.sources import gcjobs

NOW = '2026-10-13T00:00:00+00:00'


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / 'jobs.db')


def add_job(store: Store, source: str, source_id: str, description: str = '') -> str:
    store.upsert(Job(source=source, source_id=source_id, url=f'https://example.com/{source_id}',
                     title=f'Lab technician {source_id}', company='Acme', location='Toronto',
                     city='Toronto', description=description), NOW)
    store.commit()
    row = store.conn.execute('SELECT job_id FROM job_sources WHERE source=? AND source_id=?', (source, source_id)).fetchone()
    job_id: str = row['job_id']
    return job_id


def enrich_gcjobs(store: Store) -> int:
    config = Config.model_validate({'search_terms': [], 'cities': [], 'title_keywords': [], 'sheet': {},
                                   'descriptions': {'linkedin_max_per_run': 0, 'jobbank_max_per_run': 0}})
    return enrich.enrich_descriptions(store, config)['described_gcjobs']


def test_gc_enrichment_only_retries_unchecked_empty_postings(store: Store, monkeypatch: MonkeyPatch) -> None:
    empty = add_job(store, 'gcjobs', 'empty')
    complete = add_job(store, 'gcjobs', 'complete', 'Analyze samples.')
    checked = add_job(store, 'gcjobs', 'checked')
    store.set_description(checked, '', NOW)
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return 'Duties\nAnalyze samples.'

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich_gcjobs(store) == 1
    assert calls == ['https://example.com/empty']
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[empty]['description'] == 'Duties\nAnalyze samples.'
    assert rows[empty]['description_checked_at']
    assert rows[complete]['description_checked_at'] is None
    assert enrich_gcjobs(store) == 0


def test_gc_parse_failure_leaves_target_pending_and_continues(store: Store, monkeypatch: MonkeyPatch) -> None:
    bad = add_job(store, 'gcjobs', 'bad')
    add_job(store, 'gcjobs', 'good')

    def fetch(url: str) -> str:
        if url.endswith('/bad'):
            raise ValueError('parse failed')
        return 'Analyze samples.'

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich_gcjobs(store) == 1
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (bad,)).fetchone()
    assert row['description'] == '' and row['description_checked_at'] is None


def test_gc_retry_cap(store: Store, monkeypatch: MonkeyPatch) -> None:
    for i in range(26):
        add_job(store, 'gcjobs', str(i))
    monkeypatch.setattr(gcjobs, 'fetch_description', lambda url: 'Analyze samples.')
    assert enrich_gcjobs(store) == 25
    assert len(store.missing_descriptions('gcjobs', 30)) == 1


@pytest.mark.parametrize('status', [404, 410])
def test_gc_gone_posting_marks_checked_and_continues(store: Store, monkeypatch: MonkeyPatch, status: int) -> None:
    job_id = add_job(store, 'gcjobs', 'gone')
    add_job(store, 'gcjobs', 'good')
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        if url.endswith('/gone'):
            response = requests.Response()
            response.status_code = status
            raise requests.HTTPError(response=response)
        return 'Analyze samples.'

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich_gcjobs(store) == 2 and len(calls) == 2
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    assert row['description'] == '' and row['description_checked_at']


def test_gc_rate_limit_stops_run(store: Store, monkeypatch: MonkeyPatch) -> None:
    add_job(store, 'gcjobs', '1')
    add_job(store, 'gcjobs', '2')
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        raise RateLimited('blocked')

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich_gcjobs(store) == 0 and len(calls) == 1


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
