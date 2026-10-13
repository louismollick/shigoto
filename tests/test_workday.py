import requests
from pytest import MonkeyPatch

from shigoto.config import WorkdayBoard
from shigoto.sources import workday as wd

BOARD = WorkdayBoard(name='Acme', url='https://acme.wd5.myworkdayjobs.com/Careers')
FACETS = [{'facetParameter': 'country', 'values': [{'descriptor': 'Canada', 'id': 'ca'}]}]


def summary(i: int) -> dict[str, object]:
    return {'externalPath': f'/job/Toronto/Quality_R{i}', 'title': 'Quality technician',
            'locationsText': 'Toronto, ON', 'bulletFields': [f'R{i}']}


def test_later_zero_totals_and_more_than_five_pages(monkeypatch: MonkeyPatch) -> None:
    offsets: list[int] = []

    def post(url: str, body: dict[str, object]) -> dict[str, object]:
        if body['limit'] == 1:
            return {'facets': FACETS}
        offset = body['offset']
        assert isinstance(offset, int)
        offsets.append(offset)
        return {'total': 121 if offset == 0 else 0,
                'jobPostings': [summary(i) for i in range(offset, min(offset + 20, 121))]}

    def get(url: str) -> dict[str, object]:
        req = url.rsplit('_', 1)[-1]
        return {'jobPostingInfo': {'title': 'Quality technician', 'jobReqId': req, 'location': 'Toronto',
                                   'jobDescription': '<p>Test samples.</p>'}}

    monkeypatch.setattr(wd._session, 'post_json', post)
    monkeypatch.setattr(wd._session, 'get_json', get)
    jobs = list(wd.WorkdaySource([BOARD], ['quality'], ['quality'], lambda s: 'Toronto' in s).fetch())
    assert len(jobs) == 121 and len({job.source_id for job in jobs}) == 121
    assert offsets == [0, 20, 40, 60, 80, 100, 120]
    assert all(job.description == 'Test samples.' for job in jobs)


def test_search_error_keeps_collected_jobs_and_next_term(monkeypatch: MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    def post(url: str, body: dict[str, object]) -> dict[str, object]:
        if body['limit'] == 1:
            return {'facets': FACETS}
        term, offset = body['searchText'], body['offset']
        assert isinstance(term, str) and isinstance(offset, int)
        calls.append((term, offset))
        if term == 'broken' and offset == 20:
            raise requests.ConnectionError('unavailable')
        return {'total': 40 if term == 'broken' else 1, 'jobPostings': [summary(1 if term == 'broken' else 2)]}

    def get_detail(url: str) -> dict[str, object]:
        return {'jobPostingInfo': {'jobReqId': url.rsplit('_', 1)[-1], 'title': 'Quality technician',
                                   'location': 'Toronto', 'jobDescription': '<p>Test samples.</p>'}}

    monkeypatch.setattr(wd._session, 'post_json', post)
    monkeypatch.setattr(wd._session, 'get_json', get_detail)
    jobs = list(wd.WorkdaySource([BOARD], ['broken', 'quality'], ['quality'], lambda s: True).fetch())
    assert [j.source_id for j in jobs] == ['acme.wd5.myworkdayjobs.com:R1', 'acme.wd5.myworkdayjobs.com:R2']
    assert all(j.location == 'Toronto' and j.description == 'Test samples.' for j in jobs)
    assert jobs[0].url == 'https://acme.wd5.myworkdayjobs.com/Careers/job/Toronto/Quality_R1'
    assert calls == [('broken', 0), ('broken', 20), ('quality', 0)]


def test_unknown_total_stops_on_repeated_ids(monkeypatch: MonkeyPatch) -> None:
    offsets: list[int] = []

    def post(url: str, body: dict[str, object]) -> dict[str, object]:
        if body['limit'] == 1:
            return {'facets': FACETS}
        offset = body['offset']
        assert isinstance(offset, int)
        offsets.append(offset)
        return {'total': 0, 'jobPostings': [summary(1)]}

    monkeypatch.setattr(wd._session, 'post_json', post)
    monkeypatch.setattr(wd._session, 'get_json', lambda url: {'jobPostingInfo': {'jobReqId': 'R1', 'location': 'Toronto'}})
    assert len(list(wd.WorkdaySource([BOARD], ['quality'], ['quality'], lambda s: True).fetch())) == 1
    assert offsets == [0, 20]


def test_detail_failure_skips_summary_and_keeps_next_posting(monkeypatch: MonkeyPatch) -> None:
    def post(url: str, body: dict[str, object]) -> dict[str, object]:
        if body['limit'] == 1:
            return {'facets': FACETS}
        bad = {**summary(1), 'locationsText': '2 Locations', 'bulletFields': ['Full time']}
        return {'total': 2, 'jobPostings': [bad, summary(2)]}

    def get(url: str) -> dict[str, object]:
        if url.endswith('_R1'):
            raise requests.ConnectionError('detail unavailable')
        return {'jobPostingInfo': {'jobReqId': 'R2', 'location': 'Toronto'}}

    monkeypatch.setattr(wd._session, 'post_json', post)
    monkeypatch.setattr(wd._session, 'get_json', get)
    jobs = list(wd.WorkdaySource([BOARD], ['quality'], ['quality'], lambda s: True).fetch())
    assert [job.source_id for job in jobs] == ['acme.wd5.myworkdayjobs.com:R2']
