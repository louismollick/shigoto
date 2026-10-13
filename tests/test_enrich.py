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


def test_gc_enrichment_retries_old_empty_and_metadata_only_postings(store: Store, monkeypatch: MonkeyPatch) -> None:
    empty = add_job(store, 'gcjobs', 'empty')
    metadata = add_job(store, 'gcjobs', 'metadata', 'Closing date: 2026-10-30\nEnglish\n$60,000')
    complete = add_job(store, 'gcjobs', 'complete', 'Closing date: 2026-10-30\n\nDuties\nAnalyze samples.')
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return 'Duties\nAnalyze samples.'

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    config = Config.model_validate({'search_terms': [], 'cities': [], 'title_keywords': [], 'sheet': {},
                                   'descriptions': {'linkedin_max_per_run': 0, 'jobbank_max_per_run': 0}})
    assert enrich.enrich_descriptions(store, config)['described_gcjobs'] == 2
    assert set(calls) == {'https://example.com/empty', 'https://example.com/metadata'}
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert 'Analyze samples.' in rows[empty]['description']
    assert rows[metadata]['description'] == 'Closing date: 2026-10-30\nEnglish\n$60,000\n\nDuties\nAnalyze samples.'
    assert rows[metadata]['description_checked_at']
    assert rows[complete]['description_checked_at'] is None
    assert enrich._enrich_gcjobs(store) == 0


@pytest.mark.parametrize('error', [requests.ConnectionError('unavailable'), ValueError('parse failed'),
                                 RobotsDisallowed('disallowed')])
def test_gc_failed_retry_preserves_metadata_and_other_targets(store: Store, monkeypatch: MonkeyPatch, error: Exception) -> None:
    bad = add_job(store, 'gcjobs', 'bad', 'Closing date: 2026-10-30')
    add_job(store, 'gcjobs', 'good')

    def fetch(url: str) -> str:
        if url.endswith('/bad'):
            raise error
        return 'Analyze samples.'

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich._enrich_gcjobs(store) == 1
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (bad,)).fetchone()
    assert row['description'] == 'Closing date: 2026-10-30' and row['description_checked_at'] is None


def test_gc_retry_cap(store: Store, monkeypatch: MonkeyPatch) -> None:
    for i in range(3):
        add_job(store, 'gcjobs', str(i))
    monkeypatch.setattr(enrich, 'GCJOBS_MAX_PER_RUN', 2)
    monkeypatch.setattr(gcjobs, 'fetch_description', lambda url: 'Analyze samples.')
    assert enrich._enrich_gcjobs(store) == 2
    assert len(store.missing_descriptions('gcjobs', 10)) == 1


def test_gc_gone_posting_marks_checked_without_erasing_metadata(store: Store, monkeypatch: MonkeyPatch) -> None:
    job_id = add_job(store, 'gcjobs', 'gone', 'Closing date: 2026-10-30')

    def fetch(url: str) -> str:
        response = requests.Response()
        response.status_code = 410
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich._enrich_gcjobs(store) == 1
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    assert row['description'] == 'Closing date: 2026-10-30' and row['description_checked_at']


def test_gc_rate_limit_stops_run(store: Store, monkeypatch: MonkeyPatch) -> None:
    add_job(store, 'gcjobs', '1')
    add_job(store, 'gcjobs', '2')
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        raise RateLimited('blocked')

    monkeypatch.setattr(gcjobs, 'fetch_description', fetch)
    assert enrich._enrich_gcjobs(store) == 0 and len(calls) == 1


def test_jobbank_unrecognized_detail_stays_pending_and_next_target_survives(store: Store, monkeypatch: MonkeyPatch) -> None:
    pending = add_job(store, 'jobbank', 'pending')
    external = add_job(store, 'jobbank', 'external')

    def fetch(url: str) -> str:
        if url.endswith('/pending'):
            raise ValueError('no recognized posting content')
        return ''

    assert enrich._enrich(store, 'jobbank', 60, lambda target: fetch(target.url), delay=0) == 1
    rows = {r['job_id']: r for r in store.conn.execute('SELECT * FROM jobs')}
    assert rows[pending]['description_checked_at'] is None
    assert rows[external]['description_checked_at']


def test_gc_metadata_reintroduced_by_later_fetch_is_retried(store: Store, monkeypatch: MonkeyPatch) -> None:
    job_id = add_job(store, 'gcjobs', 'retry', 'Closing date: 2026-10-30')
    store.set_description(job_id, 'Closing date: 2026-10-30\n\nAnalyze samples.', NOW)
    store.upsert(Job(source='gcjobs', source_id='retry', url='https://example.com/retry', title='Lab technician retry',
                     company='Acme', city='Toronto', location='Toronto', description='Closing date: 2026-10-30'),
                 '2026-10-14T00:00:00+00:00')
    monkeypatch.setattr(gcjobs, 'fetch_description', lambda url: 'Analyze samples.')
    assert enrich._enrich_gcjobs(store) == 1
    row = store.conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    assert row['description'] == 'Closing date: 2026-10-30\n\nAnalyze samples.'


def test_jobbank_robots_failure_leaves_targets_pending(store: Store) -> None:
    add_job(store, 'jobbank', '1')
    add_job(store, 'jobbank', '2')

    def fetch(target: DescriptionTarget) -> str:
        raise RobotsDisallowed('disallowed')

    assert enrich._enrich(store, 'jobbank', 60, fetch, delay=0) == 0
    assert len(store.missing_descriptions('jobbank', 60)) == 2
