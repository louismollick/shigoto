from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from urllib.robotparser import RobotFileParser

import pytest
import requests
from pytest import MonkeyPatch

from shigoto.config import Config, SheetConfig, SlugBoard
from shigoto.db import Store
from shigoto.http import PoliteSession, RateLimited, RobotsDisallowed, posting_liveness
from shigoto.liveness import check_liveness, check_source
from shigoto.models import Job, Liveness, SourceLink
from shigoto.sources import ats, indeed, jobbank, linkedin_detail, oracle, successfactors, workday
from shigoto.sources.indeed import IndeedAnswer

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
    monkeypatch.setattr(indeed, "check_liveness", lambda ids: {"in-1": IndeedAnswer("alive")})
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
            {"job": {"key": "k1", "expired": True, "recruit": {"viewJobUrl": "https://acme.example/job/1"}}},
            {"job": {"key": "k2", "expired": False, "recruit": {"viewJobUrl": "http://ca.indeed.com/job/2"}}},
            {"job": {"key": "k3", "expired": None}}, {"job": {"key": "other", "expired": True}},
        ]}}}

    monkeypatch.setattr(indeed, "BATCH_SIZE", 3)
    monkeypatch.setattr(indeed._session, "post_json", post_json)
    ids = ["in-k1", "in-k2", "in-k3", "in-k4"]
    assert indeed.check_liveness(ids) == {
        "in-k1": IndeedAnswer("gone", "https://acme.example/job/1"), "in-k2": IndeedAnswer("alive", hosted=True),
    }
    assert ['"k1", "k2", "k3"' in queries[0], '"k4"' in queries[1]] == [True, True]


def test_malformed_indeed_recruit_still_answers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(indeed._session, "post_json", lambda url, body, **kwargs: {"data": {"jobData": {"results": [
        {"job": {"key": "k1", "expired": False, "recruit": "bad"}},
        {"job": {"key": "k2", "expired": False, "recruit": {"viewJobUrl": 7}}},
    ]}}})
    assert indeed.check_liveness(["in-k1", "in-k2"]) == {"in-k1": IndeedAnswer("alive"), "in-k2": IndeedAnswer("alive")}


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
    monkeypatch.setattr(indeed, "check_liveness", lambda ids: {"in-open": IndeedAnswer("alive"),
                                                               "in-expired": IndeedAnswer("gone")})
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

    def indeed_check(ids: list[str]) -> dict[str, IndeedAnswer]:
        calls.append(ids)
        return {source_id: IndeedAnswer(answer) for source_id in ids}

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
@pytest.mark.parametrize("decision", ["", "Reject"])
def test_uncheckable_only_closes_after_30_days_without_requests(monkeypatch: MonkeyPatch, tmp_path: Path,
                                                             sources: list[str], decision: str) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), liveness_max_per_run=0)
    store = Store(tmp_path / "t.db")
    for source in sources:
        add(store, source, f"{source}-1", "QA")
    # Use source timestamps, including offsets, and wait for the latest link to age.
    store.conn.execute("UPDATE job_sources SET last_seen='2026-10-02T14:00:00+02:00' WHERE source=?",
                       (sources[-1],))
    store.commit()
    [original] = store.visible_jobs()
    store.backup_reviews(["AI Decision", "AI Reviewed At"], {original.job_id: {
        "AI Decision": decision, "AI Reviewed At": "2026-10-03T12:00:00+00:00",
    }}, "2026-10-04T12:00:00+00:00")
    store.finish_sheet_rebuild()

    def unexpected_indeed(ids: list[str]) -> dict[str, IndeedAnswer]:
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


def test_rejects_spend_no_indeed_or_posting_requests(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), liveness_max_per_run=1)
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "reject-indeed", "QA A")
    add(store, "jobbank", "reject-jobbank", "QA B")
    add(store, "jobbank", "applied", "QA C")
    reviews = {job.job_id: {"AI Decision": "Reject", "AI Reviewed At": "2026-10-02T12:00:00+00:00",
                           "Application Status": "Applied" if job.title == "QA C" else "Not Applied"}
               for job in store.visible_jobs()}
    store.backup_reviews(list(next(iter(reviews.values()))), reviews, "2026-10-03T12:00:00+00:00")
    store.finish_sheet_rebuild()
    calls: list[str] = []

    def unexpected_indeed(ids: list[str]) -> dict[str, IndeedAnswer]:
        pytest.fail("AI rejects must not be included in the Indeed request")

    def check(link: SourceLink) -> Liveness:
        calls.append(link.source_id)
        return "alive"

    monkeypatch.setattr(indeed, "check_liveness", unexpected_indeed)
    monkeypatch.setattr("shigoto.liveness.check_source", check)
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-04T12:00:00+00:00")
    assert check_liveness(store, config) == {"liveness_checked": 1, "closed": 0}
    assert calls == ["applied"]
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


def config() -> Config:
    return Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig())


def freeze(monkeypatch: MonkeyPatch, answers: dict[str, IndeedAnswer] | None = None,
           checks: dict[str, Liveness] | None = None) -> list[SourceLink]:
    """Stub Indeed and per-posting checks. Unlisted checks answer unknown; returns the links requested."""
    calls: list[SourceLink] = []

    def check(link: SourceLink) -> Liveness:
        calls.append(link)
        return (checks or {}).get(link.source, "unknown")

    monkeypatch.setattr("shigoto.liveness.check_source", check)
    monkeypatch.setattr(indeed, "check_liveness", lambda ids: answers or {})
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: NOW)
    return calls


NOW = "2026-10-04T12:00:00+00:00"
LEVER_URL = "https://jobs.lever.co/acme/87a54373-296e-4a28-abb2-00a601414c38"


def add_apply(store: Store, source: str, source_id: str, apply_url: str) -> None:
    store.upsert(Job(source=source, source_id=source_id, url=f"https://example.com/{source_id}", title="QA",
                     company="Acme", location="Toronto", city="Toronto", apply_url=apply_url),
                 "2026-10-01T12:00:00+00:00")
    store.commit()


@pytest.mark.parametrize("employer,aggregator,status", [
    ("gone", "alive", "Closed"),  # the employer removed it while Indeed still lists it
    ("alive", "gone", ""),  # the listing ended but the job is still open
    ("unknown", "gone", "Closed"),
    ("unknown", "alive", ""),
])
def test_employer_page_settles_job_and_unknown_falls_back(monkeypatch: MonkeyPatch, tmp_path: Path, employer: Liveness,
                                                          aggregator: Liveness, status: str) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer(aggregator)}, {"lever": employer})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": int(status == "Closed")}
    assert store.visible_jobs()[0].status == status
    assert [(link.source, link.source_id) for link in calls] == [
        ("lever", "acme:87a54373-296e-4a28-abb2-00a601414c38")]
    store.close()


def test_indeed_supplies_employer_page_for_stored_jobs(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Jobs scraped before apply URLs existed backfill from the free batch check."""
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    store.commit()
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("alive", LEVER_URL)}, {"lever": "gone"})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 1}
    assert [link.source for link in calls] == ["lever"]
    assert store.visible_jobs()[0].url == LEVER_URL
    store.close()


def test_uncheckable_only_job_is_checked_through_its_apply_url(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "zip_recruiter", "zr-1", LEVER_URL)
    calls = freeze(monkeypatch, checks={"lever": "gone"})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 1}
    assert [link.source for link in calls] == ["lever"]
    store.close()


@pytest.mark.parametrize("employer,now,closed", [
    ("unknown", "2026-10-30T12:00:00+00:00", False),
    ("unknown", "2026-11-01T12:00:00+00:00", True),  # 30 days unseen still closes it
    ("alive", "2026-11-01T12:00:00+00:00", False),
])
def test_uncheckable_job_with_employer_page_keeps_the_30_day_rule(monkeypatch: MonkeyPatch, tmp_path: Path,
                                                                  employer: Liveness, now: str, closed: bool) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "zip_recruiter", "zr-1", LEVER_URL)
    freeze(monkeypatch, checks={"lever": employer})
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: now)
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": int(closed)}
    store.close()


def test_unrecognized_apply_host_is_never_requested(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", "https://careers.acme.example/job/1")
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("alive", "https://careers.acme.example/job/1")})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 0}
    assert calls == []
    assert store.visible_jobs()[0].url == "https://careers.acme.example/job/1"
    store.close()


def test_replaced_apply_url_is_the_one_checked(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", "https://jobs.lever.co/old/87a54373-296e-4a28-abb2-00a601414c38")
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("alive", LEVER_URL)}, {"lever": "gone"})
    check_liveness(store, config())
    assert [link.source_id for link in calls] == ["acme:87a54373-296e-4a28-abb2-00a601414c38"]
    store.close()


def test_move_to_indeed_hosted_drops_the_old_employer_page(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("alive", hosted=True)}, {"lever": "gone"})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 0}
    assert calls == []
    assert store.visible_jobs()[0].url == "https://example.com/in-1"
    store.close()


def test_blank_indeed_answer_keeps_the_known_employer_page(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("alive")}, {"lever": "alive"})
    check_liveness(store, config())
    assert [link.source for link in calls] == ["lever"]
    store.close()


def test_job_that_spent_requests_rotates_back(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Unsettled jobs must not hold the front of the queue and use up every run's budget."""
    cfg = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), liveness_max_per_run=1)
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    add(store, "jobbank", "jb-1", "QA")  # same job, and the budget is gone before it's asked
    add(store, "jobbank", "jb-2", "Other QA")
    store.conn.execute("UPDATE job_sources SET last_seen='2026-10-02T12:00:00+00:00' WHERE source_id='jb-2'")
    store.commit()
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("gone")}, {"jobbank": "gone"})
    assert check_liveness(store, cfg) == {"liveness_checked": 1, "closed": 0}
    assert check_liveness(store, cfg) == {"liveness_checked": 1, "closed": 1}
    assert [link.source_id for link in calls][-1] == "jb-2"
    store.close()


def test_own_listing_wins_over_apply_url(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    add(store, "workday", "acme:R1", "QA")
    store.commit()
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("alive")}, {"workday": "alive", "lever": "gone"})
    check_liveness(store, config())
    assert [link.source for link in calls] == ["workday"]
    store.close()


@pytest.mark.parametrize("hosted,status", [(True, "Closed"), (False, "")])
def test_expired_indeed_hosted_posting_outranks_linkedin(monkeypatch: MonkeyPatch, tmp_path: Path, hosted: bool,
                                                         status: str) -> None:
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    add(store, "linkedin", "li-1", "QA")
    store.commit()
    calls = freeze(monkeypatch, {"in-1": IndeedAnswer("gone", hosted=hosted)}, {"linkedin": "alive"})
    assert check_liveness(store, config())["closed"] == int(hosted)
    assert store.visible_jobs()[0].status == status
    assert [link.source for link in calls] == ([] if hosted else ["linkedin"])
    store.close()


@pytest.mark.parametrize("sibling_seen,status", [
    ("2026-10-04T00:00:00+00:00", "Closed"),  # the board answered without this posting
    ("2026-10-01T12:00:00+00:00", ""),  # the board's scrape failed: everything looks missing
])
def test_listing_absence_needs_a_board_that_still_answers(monkeypatch: MonkeyPatch, tmp_path: Path, sibling_seen: str,
                                                          status: str) -> None:
    store = Store(tmp_path / "t.db")
    add(store, "smartrecruiters", "SGS:1", "QA")
    add(store, "smartrecruiters", "SGS:2", "Other QA")
    add(store, "smartrecruiters", "SGSX:3", "Third QA")  # a board sharing the prefix is no evidence
    store.conn.execute("UPDATE job_sources SET last_seen='2026-10-04T00:00:00+00:00' WHERE source_id='SGSX:3'")
    store.conn.execute("UPDATE job_sources SET last_seen=? WHERE source_id='SGS:2'", (sibling_seen,))
    store.commit()
    freeze(monkeypatch)  # robots keeps direct checks at unknown
    check_liveness(store, config())
    statuses = {job.title: job.status for job in store.visible_jobs()}
    assert statuses["QA"] == status
    store.close()


def test_listing_absence_is_not_used_when_a_direct_check_works(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Workday IDs don't carry the site, so another site's posting says nothing about this board."""
    store = Store(tmp_path / "t.db")
    add(store, "workday", "acme.wd3.myworkdayjobs.com:R1", "QA")
    add(store, "workday", "acme.wd3.myworkdayjobs.com:R2", "Other QA")
    store.conn.execute("UPDATE job_sources SET last_seen=? WHERE source_id LIKE '%:R2'", (NOW,))
    store.commit()
    freeze(monkeypatch)
    check_liveness(store, config())
    assert {job.title: job.status for job in store.visible_jobs()}["QA"] == ""
    store.close()


def test_employer_closure_survives_aggregator_sightings(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Aggregators keep listing removed postings, so they only prompt a recheck once a week."""
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    freeze(monkeypatch, {"in-1": IndeedAnswer("alive")}, {"lever": "gone"})
    assert check_liveness(store, config())["closed"] == 1

    def sight(now: str) -> None:
        store.upsert(Job(source="indeed", source_id="in-1", url="https://example.com/in-1", title="QA",
                         company="Acme", location="Toronto", city="Toronto"), now)
        store.commit()

    sight("2026-10-11T11:59:59+00:00")
    assert store.visible_jobs()[0].status == "Closed"
    sight("2026-10-11T12:00:00+00:00")
    assert store.visible_jobs()[0].status == ""
    assert store.conn.execute("SELECT liveness_checked_at FROM jobs").fetchone()[0] is None
    store.close()


def test_employer_listing_sighting_reopens_employer_closure(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add(store, "workday", "acme:R1", "QA")
    store.commit()
    freeze(monkeypatch, checks={"workday": "gone"})
    assert check_liveness(store, config())["closed"] == 1
    assert store.conn.execute("SELECT gone_at FROM job_sources").fetchone()[0] == NOW
    add(store, "workday", "acme:R1", "QA")
    store.commit()
    [job] = store.visible_jobs()
    assert (job.status, job.url) == ("", "https://example.com/acme:R1")
    assert store.conn.execute("SELECT gone_at FROM job_sources").fetchone()[0] is None
    store.close()


def test_indeed_hosted_expiry_is_rechecked_on_sighting(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Checking Indeed is free, so a reactivated posting recovers on the next sighting."""
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    store.commit()
    freeze(monkeypatch, {"in-1": IndeedAnswer("gone", hosted=True)})
    assert check_liveness(store, config())["closed"] == 1
    add(store, "indeed", "in-1", "QA")
    store.commit()
    assert store.visible_jobs()[0].status == ""
    store.close()


def test_aggregator_closure_still_reopens_on_sighting(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add(store, "jobbank", "jb-1", "QA")
    store.commit()
    freeze(monkeypatch, checks={"jobbank": "gone"})
    assert check_liveness(store, config())["closed"] == 1
    add(store, "jobbank", "jb-1", "QA")
    store.commit()
    assert store.visible_jobs()[0].status == ""
    store.close()


def test_sheet_link_moves_off_a_dead_listing(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    add(store, "linkedin", "li-1", "QA")
    store.commit()
    freeze(monkeypatch, {"in-1": IndeedAnswer("gone")}, {"linkedin": "alive"})
    assert store.visible_jobs()[0].url == "https://example.com/in-1"
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 0}
    [job] = store.visible_jobs()
    assert (job.url, job.status) == ("https://example.com/li-1", "")
    assert "https://example.com/in-1" in job.sources
    store.close()


def test_sheet_link_keeps_the_first_link_when_all_are_dead(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add(store, "indeed", "in-1", "QA")
    add(store, "linkedin", "li-1", "QA")
    store.commit()
    freeze(monkeypatch, {"in-1": IndeedAnswer("gone")}, {"linkedin": "gone"})
    check_liveness(store, config())
    assert store.visible_jobs()[0].url == "https://example.com/in-1"
    store.close()


def test_replay_keeps_a_known_apply_url(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    store.upsert(Job(source="indeed", source_id="in-1", url="https://example.com/in-1", title="QA", company="Acme",
                     location="Toronto", city="Toronto"), NOW, seen=False)
    store.commit()
    assert store.visible_jobs()[0].url == LEVER_URL
    store.close()


def test_employer_listing_is_the_link_over_an_apply_url(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_apply(store, "indeed", "in-1", LEVER_URL)
    add(store, "workday", "acme:R1", "QA")
    store.commit()
    assert store.visible_jobs()[0].url == "https://example.com/acme:R1"
    store.close()


UNAVAILABLE = "<p><strong>You can't view this job because it's not available at this time.</strong></p>"


@pytest.mark.parametrize("page,expected", [
    (UNAVAILABLE, "gone"), (UNAVAILABLE.replace("'", "&#39;"), "gone"),
    ('<div class="jobdescription">QA</div>', "alive"),
    (UNAVAILABLE + '<div class="jobdescription">QA</div>', "alive"), ("<html></html>", "unknown"),
])
def test_successfactors_unavailable_page(monkeypatch: MonkeyPatch, page: str, expected: Liveness) -> None:
    monkeypatch.setattr(successfactors._session, "request", lambda method, url, **kwargs: response(url, page, 200))
    assert successfactors.check_liveness("https://olymel.jobs.hr.cloud.sap/job/QA/1-en_US", "x:1") == expected


@pytest.mark.parametrize("data,expected", [
    ({"count": 0, "items": []}, "gone"),
    ({"count": 1, "items": [{"Id": "119", "Title": "QA"}]}, "alive"),
    ({"count": 1, "items": [{"Id": "120", "Title": "QA"}]}, "unknown"),
    ({"count": 0}, "unknown"), ([], "unknown"),
])
def test_oracle_requisition_lookup(monkeypatch: MonkeyPatch, data: object, expected: Liveness) -> None:
    urls: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        urls.append(url)
        return response(url, data, 200)

    monkeypatch.setattr(oracle._session, "request", request)
    assert oracle.check_liveness("https://ibanqy.fa.ocs.oraclecloud.com/x", "CX_4:119") == expected
    assert 'finder=ById;Id=%22119%22,siteNumber=CX_4' in urls[0] and urls[0].startswith("https://ibanqy.fa.ocs")


def test_failed_smartrecruiters_detail_is_still_a_sighting(monkeypatch: MonkeyPatch) -> None:
    def get_json(url: str, **kwargs: object) -> object:
        if url.endswith("/postings"):
            return {"totalFound": 1, "content": [{"id": "1", "name": "QA", "location": {"fullLocation": "Toronto"}}]}
        raise requests.ConnectionError()

    monkeypatch.setattr(ats._session, "get_json", get_json)
    source = ats.SmartRecruitersSource([SlugBoard(slug="SGS", name="SGS")], ["qa"], ["QA"], lambda location: True)
    [job] = list(source.fetch())
    assert (job.source_id, job.description, job.location) == ("SGS:1", "", "Toronto")


def test_posting_check_refuses_off_host_redirect(monkeypatch: MonkeyPatch) -> None:
    session = PoliteSession(min_interval=0)
    session._robots["https://jobs.lever.co"] = RobotFileParser()
    session._robots["https://jobs.lever.co"].parse([])
    requested: list[str] = []

    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        requested.append(url)
        result = response(url, "", 302)
        result.headers["Location"] = "http://127.0.0.1:8080/admin"
        return result

    monkeypatch.setattr(session, "_request", request)
    assert posting_liveness(session, "https://jobs.lever.co/acme/1", lambda r: True) == "unknown"
    assert requested == ["https://jobs.lever.co/acme/1"]


def test_smartrecruiters_inactive_posting_is_not_alive(monkeypatch: MonkeyPatch) -> None:
    page = json.dumps({"id": "1", "name": "QA", "active": False})
    monkeypatch.setattr(ats._session, "request", lambda method, url, **kwargs: response(url, page, 200))
    assert ats.smartrecruiters_liveness("https://example.com/1", "board:1") == "unknown"


def add_zip(store: Store, source_id: str, title: str = "QA", jid: str = "1",
            last_seen: str = "2026-10-01T12:00:00+00:00") -> str:
    store.upsert(Job(source="zip_recruiter", source_id=source_id, title=title, company="Acme",
                     url=f"https://www.ziprecruiter.com/c/Acme/Job/QA/-in-Toronto,ON?jid={jid}",
                     location="Toronto", city="Toronto"), last_seen)
    store.commit()
    return str(store.conn.execute("SELECT job_id FROM job_sources WHERE source_id=?", (source_id,)).fetchone()[0])


def zip_mode(monkeypatch: MonkeyPatch, checks: dict[str, Liveness] | None = None,
             proxy: str | None = "http://proxy:1") -> list[SourceLink]:
    monkeypatch.setattr("shigoto.liveness.proxy_url", lambda: proxy)
    monkeypatch.setattr("shigoto.liveness.time.sleep", lambda seconds: None)
    return freeze(monkeypatch, checks=checks)


def set_review(store: Store, job_id: str, **values: str) -> None:
    store.backup_reviews(list(values), {job_id: values}, "2026-10-03T12:00:00+00:00")
    store.finish_sheet_rebuild()


@pytest.mark.parametrize("answer,status", [("gone", "Closed"), ("alive", ""), ("unknown", "")])
def test_zip_recruiter_only_job_is_checked_with_a_proxy(monkeypatch: MonkeyPatch, tmp_path: Path,
                                                        answer: Liveness, status: str) -> None:
    store = Store(tmp_path / "t.db")
    add_zip(store, "zr-1")
    calls = zip_mode(monkeypatch, {"zip_recruiter": answer})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": int(answer == "gone")}
    assert [link.source for link in calls] == ["zip_recruiter"]
    [job] = store.visible_jobs()
    assert job.status == status
    # A ZipRecruiter closure isn't the employer's, so a later sighting reopens it for a recheck.
    assert store.conn.execute("SELECT closed_by_employer FROM jobs").fetchone()[0] == 0
    store.close()


def test_zip_recruiter_is_not_checked_without_a_proxy(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_zip(store, "zr-1")
    calls = zip_mode(monkeypatch, {"zip_recruiter": "gone"}, proxy=None)
    assert check_liveness(store, config()) == {"liveness_checked": 0, "closed": 0}
    assert calls == []
    store.close()


def test_recent_zip_recruiter_sighting_waits(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_zip(store, "zr-1", last_seen="2026-10-03T12:00:00+00:00")  # a day before NOW
    calls = zip_mode(monkeypatch, {"zip_recruiter": "gone"})
    assert check_liveness(store, config()) == {"liveness_checked": 0, "closed": 0}
    assert calls == []
    store.close()


@pytest.mark.parametrize("review", [{"Application Status": "Skip"},
                                    {"AI Decision": "Reject", "AI Reviewed At": "2026-10-03T12:00:00+00:00"}])
def test_unwanted_zip_recruiter_job_spends_nothing_but_still_closes_at_30_days(
        monkeypatch: MonkeyPatch, tmp_path: Path, review: dict[str, str]) -> None:
    store = Store(tmp_path / "t.db")
    job_id = add_zip(store, "zr-1")
    set_review(store, job_id, **review)
    calls = zip_mode(monkeypatch, {"zip_recruiter": "gone"})
    assert check_liveness(store, config()) == {"liveness_checked": 0, "closed": 0}
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-31T12:00:00+00:00")
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 1}
    assert calls == []
    store.close()


def test_30_day_rule_beats_zip_recruiter_backoff_and_budget(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    add_zip(store, "zr-1")
    # ZipRecruiter kept saying active the day before, but the posting hasn't been listed in 30 days.
    store.conn.execute("UPDATE jobs SET liveness_checked_at='2026-10-30T12:00:00+00:00', liveness_alive_streak=3")
    store.commit()
    calls = zip_mode(monkeypatch, {"zip_recruiter": "alive"})
    monkeypatch.setattr("shigoto.liveness.now_iso", lambda: "2026-10-31T12:00:00+00:00")
    assert check_liveness(store, config(), budget=0) == {"liveness_checked": 1, "closed": 1}
    assert calls == []
    store.close()


def test_jobs_sharing_a_zip_recruiter_posting_cost_one_check(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    first, second = add_zip(store, "zr-a", title="QA"), add_zip(store, "zr-b", title="QA Technician")
    assert first != second
    calls = zip_mode(monkeypatch, {"zip_recruiter": "gone"})
    assert check_liveness(store, config(), budget=1) == {"liveness_checked": 2, "closed": 2}
    assert len(calls) == 1
    store.close()


def test_zip_recruiter_checks_stop_after_repeated_unknowns(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    for n in range(5):
        add_zip(store, f"zr-{n}", title=f"QA {n}", jid=str(n))
    waits: list[float] = []
    calls = zip_mode(monkeypatch)
    monkeypatch.setattr("shigoto.liveness.time.sleep", waits.append)
    assert check_liveness(store, config()) == {"liveness_checked": 3, "closed": 0}
    assert len(calls) == 3
    assert waits == [2.0, 2.0]  # paced between postings, not before the first
    store.close()


@pytest.mark.parametrize("other", ["indeed", "linkedin", "lever-apply"])
def test_zip_recruiter_never_votes_on_a_job_with_other_evidence(monkeypatch: MonkeyPatch, tmp_path: Path,
                                                                other: str) -> None:
    store = Store(tmp_path / "t.db")
    add_zip(store, "zr-1")
    if other == "lever-apply":
        store.conn.execute("UPDATE job_sources SET apply_url=?", (LEVER_URL,))
        store.commit()
    else:
        store.upsert(Job(source=other, source_id=f"{other}-1", url="https://example.com/1", title="QA",
                         company="Acme", location="Toronto", city="Toronto"), "2026-10-01T12:00:00+00:00")
        store.commit()
    calls = zip_mode(monkeypatch, {"zip_recruiter": "alive", "indeed": "gone", "linkedin": "gone", "lever": "gone"})
    monkeypatch.setattr(indeed, "check_liveness", lambda ids: {i: IndeedAnswer("gone") for i in ids})
    assert check_liveness(store, config()) == {"liveness_checked": 1, "closed": 1}
    assert "zip_recruiter" not in [link.source for link in calls]
    store.close()
