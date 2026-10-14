import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from shigoto.config import City, JobSpySite, load_config
from shigoto.db import Store
from shigoto.normalize import CityMatcher
from shigoto.pipeline import ingest
from shigoto.sources import build_sources
from shigoto.sources.jobspy_source import JobSpySource, row_to_job


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "t.db")
    yield value
    value.close()


def source(store: Store, site: JobSpySite) -> JobSpySource:
    return JobSpySource(site, ["lab", "QA"], [City(name="Toronto", jobspy_location="Toronto, ON")], store)


def test_recency_full_sweep_and_missed_runs(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def scrape(**kwargs: object) -> pd.DataFrame:
        calls.append(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    monkeypatch.setattr("shigoto.sources.jobspy_source.now_iso", lambda: "2026-10-13T12:00:00+00:00")
    site = JobSpySite(site="indeed", hours_old=168, results_wanted=100, full_sweep_days=7,
                      full_sweep_results_wanted=400, delay_seconds=0)
    assert list(source(store, site).fetch()) == []
    assert [(c["hours_old"], c["results_wanted"]) for c in calls] == [(None, 400), (None, 400)]
    assert store.get_meta("jobspy:indeed:last_success") == "2026-10-13T12:00:00+00:00"
    assert store.get_meta("jobspy:indeed:last_full_sweep") == "2026-10-13T12:00:00+00:00"
    calls.clear()
    list(source(store, site).fetch())
    assert [(c["hours_old"], c["results_wanted"]) for c in calls] == [(168, 100), (168, 100)]
    # Missed passes widen the window even when periodic full sweeps are disabled.
    site.full_sweep_days = None
    store.set_meta("jobspy:indeed:last_success", "2026-10-01T11:30:00+00:00")
    calls.clear()
    list(source(store, site).fetch())
    assert calls[0]["hours_old"] == 291
    site.full_sweep_days = 7
    store.set_meta("jobspy:indeed:last_full_sweep", "2026-10-06T12:00:00+00:00")
    calls.clear()
    list(source(store, site).fetch())
    assert calls[0]["hours_old"] is None and calls[0]["results_wanted"] == 400
    store.conn.execute("DELETE FROM meta WHERE key='jobspy:indeed:last_full_sweep'")
    calls.clear()
    list(source(store, site).fetch())
    assert calls[0]["hours_old"] is None


@pytest.mark.parametrize("failure", ["crash", "403", "429", "999", "500"])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_pass_does_not_checkpoint_but_still_rotates(store: Store, monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture, failure: str,
                                                         raises: bool) -> None:
    site = JobSpySite(site="linkedin", query_groups=[["food safety"], ["microbiology"]], rotate_groups=True,
                      delay_seconds=0)
    key = "jobspy:linkedin:group:" + hashlib.sha1(b'"food safety"').hexdigest()[:16]
    existing = {"jobspy:linkedin:next_group": "0", f"{key}:last_success": "2026-10-01T12:00:00+00:00",
                f"{key}:last_full_sweep": "2026-10-01T12:00:00+00:00"}
    for meta_key, value in existing.items():
        store.set_meta(meta_key, value)
    calls: list[object] = []

    def scrape(**kwargs: object) -> pd.DataFrame:
        calls.append(kwargs["location"])
        if len(calls) == 2:
            if raises:
                raise ValueError(f"Search failed: {failure}")
            logging.getLogger("JobSpy:LinkedIn").error("Search failed: %s", failure)
        # A logged failure can return partial results; skip that query's records.
        return pd.DataFrame([{"site": "linkedin", "id": f"li-{len(calls)}", "job_url": "https://example.com/1",
                              "title": "QA technician"}])

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    adapter = source(store, site)
    adapter.cities += [City(name="Ottawa", jobspy_location="Ottawa, ON"),
                       City(name="Vancouver", jobspy_location="Vancouver, BC")]
    jobs = list(adapter.fetch())
    blocked = failure in {"403", "429", "999"}
    assert calls == ["Toronto, ON", "Ottawa, ON"] + ([] if blocked else ["Vancouver, BC"])
    assert [job.source_id for job in jobs] == ["li-1"] + ([] if blocked else ["li-3"])
    assert adapter.incomplete
    assert any(record.levelno == logging.WARNING and "pass incomplete" in record.message
               for record in caplog.records)
    # No checkpoint for the failed group, but rotation still advances so it can't starve the others.
    assert dict(store.conn.execute("SELECT key, value FROM meta WHERE key LIKE 'jobspy:%'")) == (
        existing | {"jobspy:linkedin:next_group": "1"})
    # The next fetch moves to the next group, starts from the next city, and clears the failure state.
    searches: list[tuple[object, object]] = []

    def record(**kwargs: object) -> pd.DataFrame:
        searches.append((kwargs["search_term"], kwargs["location"]))
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", record)
    list(adapter.fetch())
    assert searches == [("microbiology", "Ottawa, ON"), ("microbiology", "Vancouver, BC"),
                        ("microbiology", "Toronto, ON")]
    assert not adapter.incomplete and store.get_meta("jobspy:linkedin:next_group") == "2"


def test_rotation_and_independent_group_checkpoints(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def scrape(**kwargs: object) -> pd.DataFrame:
        calls.append(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    monkeypatch.setattr("shigoto.sources.jobspy_source.now_iso", lambda: "2026-10-13T12:00:00+00:00")
    site = JobSpySite(site="linkedin", query_groups=[["food safety", "QA"], ["microbiology"], ["QC analyst"]],
                      rotate_groups=True, hours_old=72, results_wanted=50, full_sweep_results_wanted=50,
                      delay_seconds=0)
    for _ in range(4):
        list(source(store, site).fetch())
    assert [c["search_term"] for c in calls] == ['"food safety" OR QA', "microbiology", '"QC analyst"',
                                               '"food safety" OR QA']
    assert [c["hours_old"] for c in calls] == [None, None, None, 72]
    assert store.get_meta("jobspy:linkedin:next_group") == "4"
    assert store.conn.execute("SELECT count(*) FROM meta WHERE key LIKE '%:last_success'").fetchone()[0] == 3
    # A different source has no checkpoint even after LinkedIn completed.
    list(source(store, JobSpySite(site="zip_recruiter", delay_seconds=0)).fetch())
    assert calls[-1]["hours_old"] is None


def test_unrotated_groups_search_every_group(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    terms: list[object] = []

    def scrape(**kwargs: object) -> pd.DataFrame:
        terms.append(kwargs["search_term"])
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    list(source(store, JobSpySite(site="glassdoor", query_groups=[["lab"], ["QA", "QC"]], delay_seconds=0)).fetch())
    assert terms == ["lab", "QA OR QC"]


@pytest.mark.parametrize("groups", [[], [[]]])
def test_empty_query_groups_rejected(groups: list[list[str]]) -> None:
    with pytest.raises(ValidationError):
        JobSpySite(site="linkedin", query_groups=groups)


def test_recall_config_and_zip_source_id() -> None:
    config = load_config(Path("config.yaml"))
    indeed = next(s for s in config.jobspy if s.site == "indeed")
    assert (indeed.hours_old, indeed.results_wanted, indeed.full_sweep_days,
            indeed.full_sweep_results_wanted) == (168, 100, 7, 400)
    linkedin = next(s for s in config.jobspy if s.site == "linkedin")
    assert linkedin.query_groups is not None and len(linkedin.query_groups) == 3 and linkedin.rotate_groups
    zip_site = next(s for s in config.jobspy if s.site == "zip_recruiter")
    assert zip_site.enabled and zip_site.query_groups is None
    assert zip_site.results_wanted == zip_site.full_sweep_results_wanted == 20
    glassdoor = next(s for s in config.jobspy if s.site == "glassdoor")
    assert not glassdoor.enabled and glassdoor.query_groups is None
    assert {"laboratory", "quality control inspector", "chemist", "scientist I"} <= set(config.search_terms)
    assert config.review.max_per_run == 120 and config.exclude_companies == []
    assert any(b.slug == "hellofresh" for b in config.boards.greenhouse)
    row = {"site": "zip_recruiter", "id": "zr-stable", "job_url": "https://example.com/1", "title": "Lab technician"}
    first, again = row_to_job(row, "Toronto"), row_to_job(row, "Vancouver")
    assert first and again and first.source == "zip_recruiter" and first.source_id == again.source_id == "zr-stable"


def test_enabled_zip_recruiter_is_registered(store: Store) -> None:
    config = load_config(Path("config.yaml"))
    assert "zip_recruiter" in {s.name for s in build_sources(config, CityMatcher(config.cities), store)}


def test_later_city_failure_preserves_previous_checkpoints(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    site = JobSpySite(site="indeed", full_sweep_days=7, delay_seconds=0)
    previous = "2026-10-01T12:00:00+00:00"
    store.set_meta("jobspy:indeed:last_success", previous)
    store.set_meta("jobspy:indeed:last_full_sweep", previous)
    calls = 0

    def scrape(**kwargs: object) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("second city failed")
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    monkeypatch.setattr("shigoto.sources.jobspy_source.now_iso", lambda: "2026-10-13T12:00:00+00:00")
    adapter = source(store, site)
    adapter.cities.append(City(name="Ottawa", jobspy_location="Ottawa, ON"))
    assert list(adapter.fetch()) == []
    assert calls == 4 and adapter.incomplete
    assert store.get_meta("jobspy:indeed:last_success") == store.get_meta("jobspy:indeed:last_full_sweep") == previous


@pytest.mark.parametrize("full_sweep", [False, True])
def test_checkpoint_uses_pass_start_and_lookback_is_fixed(store: Store, monkeypatch: pytest.MonkeyPatch,
                                                       full_sweep: bool) -> None:
    current = "2026-10-13T12:00:00+00:00"
    calls: list[object] = []
    if not full_sweep:
        store.set_meta("jobspy:indeed:last_success", "2026-10-13T11:00:00+00:00")

    def scrape(**kwargs: object) -> pd.DataFrame:
        nonlocal current
        calls.append(kwargs["hours_old"])
        current = "2026-10-13T16:00:00+00:00"
        return pd.DataFrame()

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    monkeypatch.setattr("shigoto.sources.jobspy_source.now_iso", lambda: current)
    adapter = source(store, JobSpySite(site="indeed", hours_old=1, delay_seconds=0))
    list(adapter.fetch())
    assert calls == ([None, None] if full_sweep else [3, 3])
    assert store.get_meta("jobspy:indeed:last_success") == "2026-10-13T12:00:00+00:00"
    if full_sweep:
        assert store.get_meta("jobspy:indeed:last_full_sweep") == "2026-10-13T12:00:00+00:00"
    calls.clear()
    list(adapter.fetch())
    assert calls == [6, 6]


@pytest.mark.parametrize("failure", ["500", "429"])
def test_ingest_counts_incomplete_pass_and_keeps_successful_jobs(store: Store, monkeypatch: pytest.MonkeyPatch,
                                                               failure: str) -> None:
    config = load_config(Path("config.yaml"))
    adapter = source(store, JobSpySite(site="indeed", delay_seconds=0))
    adapter.terms = ["first", "failed", "last"]

    def scrape(**kwargs: object) -> pd.DataFrame:
        term = kwargs["search_term"]
        if term == "failed":
            raise ValueError(f"Search failed: {failure}")
        return pd.DataFrame([{"site": "indeed", "id": term, "job_url": f"https://example.com/{term}",
                              "title": "QA technician", "location": "Toronto, ON"}])

    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    stats = ingest(store, adapter, CityMatcher(config.cities), config)
    assert stats["error_indeed"] == 1
    assert stats["fetched_indeed"] == stats["new"] == (1 if failure == "429" else 2)
    assert len(store.visible_jobs()) == stats["new"]
    assert store.get_meta("jobspy:indeed:last_success") is None


@pytest.mark.parametrize(("use_proxy", "env", "expected"), [
    (True, "http://u:p@proxy:823", "http://u:p@proxy:823"),
    (True, "", None),
    (False, "http://u:p@proxy:823", None),
])
def test_proxy_only_for_opted_in_sites(store: Store, monkeypatch: pytest.MonkeyPatch, use_proxy: bool,
                                       env: str, expected: str | None) -> None:
    proxies: list[object] = []

    def scrape(**kwargs: object) -> pd.DataFrame:
        proxies.append(kwargs["proxies"])
        return pd.DataFrame()

    monkeypatch.setenv("SHIGOTO_PROXY", env)
    monkeypatch.setattr("shigoto.sources.jobspy_source.scrape_jobs", scrape)
    list(source(store, JobSpySite(site="linkedin", use_proxy=use_proxy, delay_seconds=0)).fetch())
    assert proxies == [expected, expected]  # one search per term
