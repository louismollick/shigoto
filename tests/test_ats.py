from collections.abc import Callable

import pytest
from pytest import LogCaptureFixture, MonkeyPatch

from shigoto.config import SlugBoard
from shigoto.models import Job
from shigoto.sources import ats

BOARD = SlugBoard(name='Acme', slug='acme')


@pytest.mark.parametrize('fetch,rows', [
    (ats.greenhouse_board, [None, {}, {'id': 'bad', 'location': 'malformed'},
                           {'id': '1', 'title': 'Quality technician', 'location': {'name': 'Toronto'}}]),
    (ats.lever_board, [None, {}, {'id': 'bad', 'categories': 'malformed'},
                      {'id': '1', 'text': 'Quality technician', 'categories': {'allLocations': ['Toronto']}}]),
    (ats.ashby_board, [None, {}, {'id': 'bad', 'secondaryLocations': ['malformed']},
                      {'id': 'hidden', 'isListed': False}, {'id': '1', 'title': 'Quality technician', 'location': 'Toronto'}]),
])
def test_malformed_rows_keep_whole_board_survivors(monkeypatch: MonkeyPatch, caplog: LogCaptureFixture,
                                                  fetch: Callable[[SlugBoard], list[Job]], rows: list[object]) -> None:
    monkeypatch.setattr(ats._session, 'get_json', lambda url: rows if fetch == ats.lever_board else {'jobs': rows})
    [job] = fetch(BOARD)
    assert job.source_id == 'acme:1' and job.location == 'Toronto'
    assert 'malformed posting, skipping' in caplog.text


def test_malformed_smartrecruiters_row_keeps_later_jobs(monkeypatch: MonkeyPatch, caplog: LogCaptureFixture) -> None:
    monkeypatch.setattr(ats.SmartRecruitersSource, '_search', lambda *args: [
        None, {}, {'id': 'bad', 'location': 'malformed'},
        {'id': '1', 'name': 'Quality technician', 'location': {'fullLocation': 'Toronto'}},
    ])
    monkeypatch.setattr(ats._session, 'get_json', lambda url: {'jobAd': {'sections': {'description': {'text': 'Test samples.'}}}})
    [job] = list(ats.SmartRecruitersSource([BOARD], ['quality'], ['quality'], lambda s: True).fetch())
    assert job.source_id == 'acme:1' and 'Test samples.' in job.description
    assert 'malformed posting, skipping' in caplog.text
