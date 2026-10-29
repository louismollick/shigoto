from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import requests
from pytest import MonkeyPatch

from shigoto.config import Config, SheetConfig
from shigoto.db import SourceLink, Store
from shigoto.http import PoliteSession, RateLimited, RobotsDisallowed
from shigoto.liveness import check_liveness, check_source
from shigoto.models import Job, Liveness
from shigoto.sources import ats, indeed, jobbank, linkedin_detail, successfactors, workday

CHECKS: list[tuple[Callable[[str, str], Liveness], PoliteSession, str, str, object]] = [
    (workday.check_liveness, workday._session,
     "https://saputo.wd5.myworkdayjobs.com/en-US/Saputo_External_Careers/job/Toronto/QA_R1", "saputo:R1",
     {"jobPostingInfo": {"title": "QA", "jobReqId": "R1"}}),
    (ats.greenhouse_liveness, ats._session, "https://example.com/1", "board:1", {"id": 1, "title": "QA"}),
    (ats.lever_liveness, ats._session, "https://example.com/1", "board:1", {"id": "1", "text": "QA"}),
    (ats.smartrecruiters_liveness, ats._session, "https://example.com/1", "board:1", {"id": "1", "name": "QA"}),
    (successfactors.check_liveness, successfactors._session, "https://example.com/job/1", "board:1",
     '<div class="jobdescription">Test samples.</div>'),
    (jobbank.check_liveness, jobbank._session, "https://www.jobbank.gc.ca/jobsearch/jobposting/1", "1",
     '<div class="job-posting-detail-requirements">Test samples.</div>'),
]


def response(url: str, data: object, status: int) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = (data if isinstance(data, str) else json.dumps(data)).encode()
    return result


@pytest.mark.parametrize("check,session,url,source_id,data", CHECKS)
@pytest.mark.parametrize("status,expected", [(200, "alive"), (404, "gone"), (410, "gone"), (403, "unknown"),
                                             (429, "unknown"), (500, "unknown"), (204, "unknown")])
def test_posting_classification(monkeypatch: MonkeyPatch, check: Callable[[str, str], Liveness],
                                session: PoliteSession, url: str, source_id: str, data: object,
                                status: int, expected: Liveness) -> None:
    def request(method: str, api: str, **kwargs: object) -> requests.Response:
        result = response(api, data, status)
        result.raise_for_status()
        return result

    monkeypatch.setattr(session, "request", request)
    assert check(url, source_id) == expected


@pytest.mark.parametrize("check,session,url,source_id,data", CHECKS)
@pytest.mark.parametrize("bad_data", [{}, [], "<html>Unexpected page</html>"])
def test_unexpected_200_is_unknown(monkeypatch: MonkeyPatch, check: Callable[[str, str], Liveness],
                                  session: PoliteSession, url: str, source_id: str, data: object,
                                  bad_data: object) -> None:
    monkeypatch.setattr(session, "request", lambda method, api, **kwargs: response(api, bad_data, 200))
    assert check(url, source_id) == "unknown"


@pytest.mark.parametrize("check,session,url,source_id,data", CHECKS)
@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError(), RateLimited(), RobotsDisallowed()])
def test_request_failure_is_unknown(monkeypatch: MonkeyPatch, check: Callable[[str, str], Liveness],
                                    session: PoliteSession, url: str, source_id: str, data: object,
                                    error: Exception) -> None:
    def request(method: str, api: str, **kwargs: object) -> requests.Response:
        raise error

    monkeypatch.setattr(session, "request", request)
    assert check(url, source_id) == "unknown"


@pytest.mark.parametrize("locale", ["", "en-US/"])
def test_workday_detail_endpoint(monkeypatch: MonkeyPatch, locale: str) -> None:
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(url, {"jobPostingInfo": {"title": "QA", "jobReqId": "R1"}}, 200)

    monkeypatch.setattr(workday._session, "request", request)
    assert workday.check_liveness(f"https://saputo.wd5.myworkdayjobs.com/{locale}Careers/job/Toronto/QA_R1", "saputo:R1") == "alive"
    assert calls == ["https://saputo.wd5.myworkdayjobs.com/wday/cxs/saputo/Careers/job/Toronto/QA_R1"]


def test_workday_shared_host_endpoint(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(url, {"jobPostingInfo": {"title": "QA", "jobReqId": "R1"}}, 200)

    monkeypatch.setattr(workday._session, "request", request)
    url = "https://wd3.myworkdaysite.com/recruiting/mdlz/External/job/Toronto-Canada/QA_R1"
    assert workday.check_liveness(url, "mdlz.wd3.myworkdayjobs.com:R1") == "alive"
    assert calls == ["https://mdlz.wd3.myworkdayjobs.com/wday/cxs/mdlz/External/job/Toronto-Canada/QA_R1"]


@pytest.mark.parametrize("data,expected", [({"errorCode": "S22", "httpStatus": 403}, "gone"),
                                           ({"errorCode": "S1", "httpStatus": 403}, "unknown"), ("denied", "unknown")])
def test_workday_unposted_requisition_is_gone(monkeypatch: MonkeyPatch, data: object, expected: Liveness) -> None:
    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        result = response(url, data, 403)
        result.raise_for_status()
        return result

    monkeypatch.setattr(workday._session, "request", request)
    assert workday.check_liveness("https://saputo.wd5.myworkdayjobs.com/Careers/job/Toronto/QA_R1", "saputo:R1") == expected


@pytest.mark.parametrize("data,expected", [
    ({"jobs": [{"id": "1", "title": "QA", "isListed": False}]}, "alive"),
    ({"jobs": [{"id": "2", "title": "QA"}]}, "gone"), ({"jobs": []}, "gone"),
    ({}, "unknown"), ({"jobs": None}, "unknown"), ({"jobs": [None]}, "unknown"),
    ({"jobs": [{"title": "QA"}]}, "unknown"), ({"jobs": [{"id": "2"}]}, "unknown"),
])
def test_ashby_requires_complete_valid_board(monkeypatch: MonkeyPatch, data: object, expected: Liveness) -> None:
    monkeypatch.setattr(ats._session, "request", lambda method, api, **kwargs: response(api, data, 200))
    assert ats.ashby_liveness("https://example.com/1", "board:1") == expected


@pytest.mark.parametrize("status", [404, 410, 429, 500])
def test_ashby_board_failure_is_unknown(monkeypatch: MonkeyPatch, status: int) -> None:
    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        result = response(url, {}, status)
        result.raise_for_status()
        return result

    monkeypatch.setattr(ats._session, "request", request)
    assert ats.ashby_liveness("https://example.com/1", "board:1") == "unknown"


@pytest.mark.parametrize("other,result", [("gone", "Closed"), ("unknown", ""), ("alive", "")])
def test_every_aggregator_must_confirm_gone(monkeypatch: MonkeyPatch, tmp_path: Path, other: Liveness, result: str) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig())
    store = Store(tmp_path / "t.db")
    for source in ["jobbank", "linkedin"]:
        store.upsert(Job(source=source, source_id=f"board:{source}", url=f"https://example.com/{source}",
                         title="QA", company="Acme", location="Toronto", city="Toronto"), "2026-10-01T12:00:00+00:00")
    store.commit()
    calls: list[SourceLink] = []

    def check(link: SourceLink) -> Liveness:
        calls.append(link)
        return "gone" if link.source == "jobbank" else other

    monkeypatch.setattr("shigoto.liveness.check_source", check)
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-04T12:00:00+00:00")
    assert check_liveness(store, config) == {"liveness_checked": 1, "closed": int(result == "Closed")}
    assert store.visible_jobs()[0].status == result
    assert [link.url for link in calls] == ["https://example.com/jobbank", "https://example.com/linkedin"]
    assert check_liveness(store, config) == {"liveness_checked": 0, "closed": 0}


@pytest.mark.parametrize("employer,result", [("gone", "Closed"), ("unknown", ""), ("alive", "")])
def test_full_listing_answer_settles_job(monkeypatch: MonkeyPatch, tmp_path: Path, employer: Liveness,
                                         result: str) -> None:
    """Aggregators keep listing postings employers removed, so the employer's board decides."""
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig())
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    add(store, "linkedin", "li-1", "QA")
    add(store, "workday", "acme:R1", "QA")
    store.commit()
    calls: list[str] = []

    def check(link: SourceLink) -> Liveness:
        calls.append(link.source)
        return employer if link.source == "workday" else "alive"

    monkeypatch.setattr("shigoto.liveness.check_source", check)
    monkeypatch.setattr(indeed, "check_liveness", lambda ids: {"in-1": "alive"})
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-04T12:00:00+00:00")
    assert check_liveness(store, config) == {"liveness_checked": 1, "closed": int(result == "Closed")}
    assert store.visible_jobs()[0].status == result and calls == ["workday"]


def test_dispatcher_unknown_source() -> None:
    assert check_source(SourceLink("glassdoor", "1", "https://example.com/1")) == "unknown"


LINKEDIN_OPEN = '<h2 class="top-card-layout__title">QA</h2>'
LINKEDIN_CLOSED = LINKEDIN_OPEN + '<figure class="closed-job"><span>No longer accepting applications</span></figure>'


@pytest.mark.parametrize("status,page,expected", [
    (200, LINKEDIN_OPEN, "alive"), (200, LINKEDIN_CLOSED, "gone"), (200, "<html></html>", "unknown"),
    (404, "", "gone"), (410, "", "gone"), (429, "", "unknown"), (999, "", "unknown"),
])
def test_linkedin_classification(monkeypatch: MonkeyPatch, status: int, page: str, expected: Liveness) -> None:
    calls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        result = response(url, page, status)
        result.raise_for_status()
        return result

    monkeypatch.setattr(linkedin_detail._session, "request", request)
    assert linkedin_detail.check_liveness("https://www.linkedin.com/jobs/view/42", "li-42") == expected
    assert calls == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/42"]


def test_indeed_batches_and_ignores_unexpected_jobs(monkeypatch: MonkeyPatch) -> None:
    queries: list[str] = []

    def post_json(url: str, body: dict[str, str], **kwargs: object) -> object:
        queries.append(body["query"])
        if len(queries) == 2:
            raise requests.ConnectionError()
        return {"data": {"jobData": {"results": [
            {"job": {"key": "k1", "expired": True}}, {"job": {"key": "k2", "expired": False}},
            {"job": {"key": "k3", "expired": None}}, {"job": {"key": "other", "expired": True}},
        ]}}}

    monkeypatch.setattr(indeed, "BATCH_SIZE", 3)
    monkeypatch.setattr(indeed._session, "post_json", post_json)
    ids = ["in-k1", "in-k2", "in-k3", "in-k4"]
    assert indeed.check_liveness(ids) == {"in-k1": "gone", "in-k2": "alive"}
    assert ['"k1", "k2", "k3"' in queries[0], '"k4"' in queries[1]] == [True, True]


def add(store: Store, source: str, source_id: str, title: str) -> None:
    store.upsert(Job(source=source, source_id=source_id, url=f"https://example.com/{source_id}", title=title,
                     company="Acme", location="Toronto", city="Toronto"), "2026-10-01T12:00:00+00:00")


def test_indeed_answers_first_and_request_budget(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), liveness_max_per_run=1)
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-open", "QA A")
    add(store, "linkedin", "li-1", "QA A")  # same job: Indeed says open, LinkedIn is never asked
    add(store, "indeed", "in-expired", "QA B")
    add(store, "jobbank", "jb-1", "QA C")
    add(store, "jobbank", "jb-2", "QA D")  # one Job Bank job is over budget and left for the next run
    store.commit()
    calls: list[str] = []

    def check(link: SourceLink) -> Liveness:
        calls.append(link.source_id)
        return "gone"

    monkeypatch.setattr("shigoto.liveness.check_source", check)
    monkeypatch.setattr(indeed, "check_liveness", lambda ids: {"in-open": "alive", "in-expired": "gone"})
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-04T12:00:00+00:00")
    assert check_liveness(store, config) == {"liveness_checked": 3, "closed": 2}
    assert len(calls) == 1 and calls[0].startswith("jb-")
    statuses = {j.title: j.status for j in store.visible_jobs()}
    assert (statuses["QA A"], statuses["QA B"], sorted([statuses["QA C"], statuses["QA D"]])) == ("", "Closed", ["", "Closed"])
    assert check_liveness(store, config) == {"liveness_checked": 1, "closed": 1}


def test_inconclusive_linkedin_stops_linkedin_for_run(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig())
    store = Store(tmp_path / "t.db")
    for n in range(3):
        add(store, "linkedin", f"li-{n}", f"QA {n}")
    add(store, "jobbank", "jb-1", "QA J")
    store.commit()
    calls: list[str] = []

    def check(link: SourceLink) -> Liveness:
        calls.append(link.source_id)
        return "unknown" if link.source == "linkedin" else "gone"

    monkeypatch.setattr("shigoto.liveness.check_source", check)
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-04T12:00:00+00:00")
    assert check_liveness(store, config) == {"liveness_checked": 2, "closed": 1}
    assert len([c for c in calls if c.startswith("li-")]) == 1 and "jb-1" in calls


@pytest.mark.parametrize("uncheckable", ["zip_recruiter", "glassdoor"])
@pytest.mark.parametrize("answer", ["gone", "alive", "unknown"])
def test_indeed_decides_closure_with_uncheckable_link(monkeypatch: MonkeyPatch, tmp_path: Path,
                                                   uncheckable: str, answer: Liveness) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), liveness_max_per_run=0)
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    add(store, uncheckable, "other-1", "QA")
    # Even a fresh uncheckable sighting does not veto a confirmed expiry on Indeed.
    store.conn.execute("UPDATE job_sources SET last_seen='2026-10-04T12:00:00+00:00' WHERE source=?",
                       (uncheckable,))
    store.commit()
    calls: list[list[str]] = []

    def indeed_check(ids: list[str]) -> dict[str, Liveness]:
        calls.append(ids)
        return {source_id: answer for source_id in ids}

    def unexpected(link: SourceLink) -> Liveness:
        pytest.fail(f"Uncheckable source requested: {link.source}")

    monkeypatch.setattr(indeed, "check_liveness", indeed_check)
    monkeypatch.setattr("shigoto.liveness.check_source", unexpected)
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-04T12:00:00+00:00")
    assert check_liveness(store, config) == {"liveness_checked": 1, "closed": int(answer == "gone")}
    assert calls == [["in-1"]]
    assert store.visible_jobs()[0].status == ("Closed" if answer == "gone" else "")
    store.close()


@pytest.mark.parametrize("sources", [["zip_recruiter"], ["glassdoor"], ["zip_recruiter", "glassdoor"]])
def test_uncheckable_only_closes_after_30_days_without_requests(monkeypatch: MonkeyPatch, tmp_path: Path,
                                                             sources: list[str]) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), liveness_max_per_run=0)
    store = Store(tmp_path / "t.db")
    for source in sources:
        add(store, source, f"{source}-1", "QA")
    # Use source timestamps, including offsets, and wait for the latest link to age.
    store.conn.execute("UPDATE job_sources SET last_seen='2026-10-02T14:00:00+02:00' WHERE source=?",
                       (sources[-1],))
    store.commit()
    [original] = store.visible_jobs()

    def unexpected_indeed(ids: list[str]) -> dict[str, Liveness]:
        pytest.fail("An uncheckable-only job should need no Indeed request")

    def unexpected_source(link: SourceLink) -> Liveness:
        pytest.fail(f"An uncheckable-only job should need no request: {link.source}")

    monkeypatch.setattr(indeed, "check_liveness", unexpected_indeed)
    monkeypatch.setattr("shigoto.liveness.check_source", unexpected_source)
    current = "2026-11-01T11:59:59+00:00"
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: current)
    assert check_liveness(store, config) == {"liveness_checked": 0, "closed": 0}
    assert store.visible_jobs()[0].status == ""
    current = "2026-11-01T12:00:00+00:00"
    assert check_liveness(store, config) == {"liveness_checked": 1, "closed": 1}
    [closed] = store.visible_jobs()
    assert closed.status == "Closed" and closed.updated_at == original.updated_at
    assert check_liveness(store, config) == {"liveness_checked": 0, "closed": 0}
    # Seeing the link again reopens the job and restarts the 30-day wait.
    add(store, sources[-1], f"{sources[-1]}-1", "QA")
    store.conn.execute("UPDATE job_sources SET last_seen=? WHERE source=?", (current, sources[-1]))
    store.commit()
    assert check_liveness(store, config) == {"liveness_checked": 0, "closed": 0}
    assert store.visible_jobs()[0].status == ""
    store.close()


def test_least_recently_checked_go_first(tmp_path: Path) -> None:
    """A stale job that keeps answering alive must not stay ahead of never-checked jobs."""
    store = Store(tmp_path / "t.db")
    add(store, "jobbank", "jb-stale", "QA A")
    store.upsert(Job(source="jobbank", source_id="jb-newer", url="https://example.com/jb-newer", title="QA B",
                     company="Acme", location="Toronto", city="Toronto"), "2026-10-02T12:00:00+00:00")
    store.commit()
    now = "2026-10-06T12:00:00+00:00"
    stale = next(t.job_id for t in store.liveness_candidates(now) if t.sources[0].source_id == "jb-stale")
    assert [t.sources[0].source_id for t in store.liveness_candidates(now)] == ["jb-stale", "jb-newer"]
    store.record_liveness(stale, "alive", "2026-10-04T12:00:00+00:00")
    assert [t.sources[0].source_id for t in store.liveness_candidates(now)] == ["jb-newer", "jb-stale"]
