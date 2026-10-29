import sys
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest

from shigoto import main, run_lock
from shigoto.config import City, Config, ReviewConfig, SheetConfig
from shigoto.db import Store
from shigoto.models import Job, job_from_json, job_json
from shigoto.normalize import CityMatcher
from shigoto import pipeline
from shigoto.pipeline import ingest, reprocess, run_once
from shigoto.review import ReviewReport


def config_for(path: Path) -> Config:
    return Config(search_terms=[], cities=[City(name="Toronto", jobspy_location="Toronto")],
                  title_keywords=["QA"], sheet=SheetConfig(), db_path=path)


def test_ingest_saves_raw_before_in_place_normalization(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    store = Store(path)

    class FakeSource:
        name = "indeed"

        def fetch(self) -> Iterator[Job]:
            yield Job(source=self.name, source_id="1", url="https://example.com/1", title="  QA Technician  ",
                      company=" Acme ", location=" Toronto ", description="  Test food.  ",
                      posted_date=date(2026, 10, 1))

    assert ingest(store, FakeSource(), CityMatcher(config.cities), config) == {"fetched_indeed": 1, "new": 1}
    [stored] = store.visible_jobs()
    assert stored.title == "QA Technician" and stored.city == "Toronto"
    raw = job_from_json(store.conn.execute("SELECT raw FROM job_sources").fetchone()[0])
    assert raw.title == "  QA Technician  " and raw.company == " Acme " and raw.city == ""
    assert raw.description == "  Test food.  " and raw.posted_date == date(2026, 10, 1)


def test_run_without_sheet_reapplies_filters_to_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    config.exclude_title_keywords = ["Manager"]
    store = Store(path)
    store.upsert(Job(source="indeed", source_id="1", url="https://example.com/1", title="QA Manager",
                     company="Acme", location="Toronto", city="Toronto"), "t1")
    store.commit()
    store.close()
    monkeypatch.setattr("shigoto.pipeline.build_sources", lambda config, matcher, store: [])
    def enrich_only_eligible(store: Store, config: Config) -> dict[str, int]:
        assert store.missing_descriptions("indeed", 25) == []
        return {}

    monkeypatch.setattr("shigoto.pipeline.enrich_descriptions", enrich_only_eligible)
    monkeypatch.setattr("shigoto.pipeline.check_liveness", lambda store, config: {})
    assert run_once(config, sync_sheet=False) == {"sheet_hidden": 1}
    store = Store(path)
    assert store.visible_jobs() == []
    assert store.conn.execute("SELECT last_seen FROM jobs").fetchone()[0] == "t1"
    store.close()


def test_run_hides_new_old_posting_and_reprocess_can_restore_it(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path / "t.db")
    config.max_posting_age_days = 180
    posting = Job(source="indeed", source_id="old", url="https://example.com/old", title="QA Technician",
                  company="Acme", location="Toronto", posted_date=date(2025, 10, 1))

    class FakeSource:
        name = "indeed"

        def fetch(self) -> Iterator[Job]:
            yield job_from_json(job_json(posting))

    now = "2026-10-14T12:00:00-05:00"
    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: now)
    monkeypatch.setattr("shigoto.pipeline.build_sources", lambda *args: [FakeSource()])
    monkeypatch.setattr("shigoto.pipeline.enrich_descriptions", lambda *args: {})
    monkeypatch.setattr("shigoto.pipeline.check_liveness", lambda *args: {})
    stats = run_once(config, sync_sheet=False)
    assert stats["new"] == stats["sheet_hidden"] == 1
    store = Store(config.db_path)
    assert store.visible_jobs() == []
    row = store.conn.execute("SELECT job_id, first_seen, last_seen FROM jobs").fetchone()
    saved_id = row["job_id"]
    assert row["first_seen"] == row["last_seen"] == now
    assert store.conn.execute("SELECT raw FROM job_sources").fetchone()[0] == job_json(posting)
    store.close()
    config.max_posting_age_days = None
    assert reprocess(config, sync_sheet=False)["sheet_hidden"] == 0
    store = Store(config.db_path)
    [restored] = store.visible_jobs()
    assert restored.job_id == saved_id and restored.posted_date == "2025-10-01"
    assert restored.first_seen == restored.updated_at == now
    store.close()


def test_known_rejected_posting_keeps_latest_fields_and_raw_for_replay(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    store = Store(path)
    store.upsert(Job(source="indeed", source_id="1", url="https://example.com/1", title="QA Technician",
                     company="Acme", location="Toronto", city="Toronto"), "t1")
    store.commit()

    class FakeSource:
        name = "indeed"

        def fetch(self) -> Iterator[Job]:
            yield Job(source=self.name, source_id="1", url="https://example.com/1", title="  Restaurant Manager  ",
                      company="Acme", location="Toronto", city="Toronto")

    stats = ingest(store, FakeSource(), CityMatcher(config.cities), config)
    assert stats["dropped"] == stats["changed"] == 1
    assert store.reevaluate_exclusions(config, "t2") == 1
    row = store.conn.execute("SELECT title, last_seen FROM jobs").fetchone()
    assert store.conn.execute("SELECT count(*) FROM rejected_postings").fetchone()[0] == 0
    assert row["title"] == "Restaurant Manager"
    raw = job_from_json(store.conn.execute("SELECT raw FROM job_sources").fetchone()[0])
    assert raw.title == "  Restaurant Manager  "
    last_seen = row["last_seen"]
    store.close()
    config.title_keywords = ["Restaurant"]
    assert reprocess(config, sync_sheet=False)["sheet_hidden"] == 0
    store = Store(path)
    assert store.visible_jobs()[0].title == "Restaurant Manager"
    assert store.conn.execute("SELECT last_seen FROM jobs").fetchone()[0] == last_seen
    store.close()


@pytest.mark.parametrize("no_sheet", [False, True])
def test_reprocess_cli_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sheet: bool) -> None:
    config = config_for(tmp_path / "t.db")
    calls: list[bool] = []

    def replay(config_arg: Config, *, sync_sheet: bool) -> dict[str, int]:
        assert config_arg is config
        calls.append(sync_sheet)
        return {}

    monkeypatch.setattr("shigoto.load_config", lambda path: config)
    monkeypatch.setattr("shigoto.reprocess", replay)
    monkeypatch.setattr(sys, "argv", ["shigoto", "reprocess"] + (["--no-sheet"] if no_sheet else []))
    main()
    assert calls == [not no_sheet]


@pytest.mark.parametrize("sync_sheet", [False, True])
def test_automatic_reviews_follow_successful_sync_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      sync_sheet: bool) -> None:
    config = config_for(tmp_path / "t.db")
    config.review.enabled = True
    order: list[str] = []
    monkeypatch.setattr("shigoto.pipeline.build_sources", lambda *args: [])
    monkeypatch.setattr("shigoto.pipeline.enrich_descriptions", lambda *args: {})
    monkeypatch.setattr("shigoto.pipeline.check_liveness", lambda *args: {})

    def sync(store: Store, config_arg: Config) -> dict[str, int]:
        order.append("sync")
        return {"sheet_rows": 5}

    def review(config_arg: Config) -> ReviewReport:
        order.append("review")
        return ReviewReport(reviewed=1, maybe=1, pending=4)

    monkeypatch.setattr("shigoto.pipeline.sync_to_sheet", sync)
    monkeypatch.setattr("shigoto.pipeline.review", review)
    stats = run_once(config, sync_sheet=sync_sheet)
    assert order == (["sync", "review"] if sync_sheet else [])
    if sync_sheet:
        assert stats["ai_reviewed"] == stats["ai_maybe"] == 1 and stats["ai_pending"] == 4
    else:
        assert "ai_reviewed" not in stats


def test_review_failure_preserves_completed_scrape_and_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path / "t.db")
    config.review.enabled = True
    monkeypatch.setattr("shigoto.pipeline.build_sources", lambda *args: [])
    monkeypatch.setattr("shigoto.pipeline.enrich_descriptions", lambda *args: {})
    monkeypatch.setattr("shigoto.pipeline.check_liveness", lambda *args: {})
    monkeypatch.setattr("shigoto.pipeline.sync_to_sheet", lambda *args: {"sheet_rows": 5})

    def failed(config_arg: Config) -> ReviewReport:
        raise RuntimeError("Missing review header")

    monkeypatch.setattr("shigoto.pipeline.review", failed)
    assert run_once(config) == {"sheet_rows": 5, "ai_errors": 1}
    store = Store(config.db_path)
    assert store.last_finished_run() is not None
    store.close()


def test_review_cli_dispatch_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    config = config_for(tmp_path / "t.db")

    def review(config_arg: Config, *, dry_run: bool, job_id: str | None, limit: int | None) -> ReviewReport:
        assert config_arg is config and dry_run and job_id == "one" and limit == 100
        return ReviewReport(pending=2)

    monkeypatch.setattr("shigoto.load_config", lambda path: config)
    monkeypatch.setattr("shigoto.review", review)
    monkeypatch.setattr(sys, "argv", ["shigoto", "review", "--dry-run", "--job", "one", "--limit", "100"])
    main()
    assert '"pending":2' in capsys.readouterr().out


def test_cli_lock_prevents_manual_overlap_and_releases_after_error(tmp_path: Path) -> None:
    config = config_for(tmp_path / "t.db")
    with pytest.raises(ValueError):
        with run_lock(config):
            with pytest.raises(RuntimeError, match="already active"):
                with run_lock(config):
                    pytest.fail("Second run acquired the lock")
            raise ValueError("interrupted")
    with run_lock(config):
        pass


def test_ingest_drops_excluded_company(tmp_path: Path) -> None:
    config = config_for(tmp_path / "t.db")
    config.exclude_companies = ["Mérieux"]
    store = Store(config.db_path)

    class FakeSource:
        name = "indeed"

        def fetch(self) -> Iterator[Job]:
            yield Job(source=self.name, source_id="1", url="https://example.com/1", title="QA Technician",
                      company="MERIEUX NutriSciences", location="Toronto")

    assert ingest(store, FakeSource(), CityMatcher(config.cities), config) == {"fetched_indeed": 1, "dropped": 1, "rejected_company": 1}
    assert store.visible_jobs() == []
    assert store.conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] == 0


@pytest.mark.parametrize("limit", ["0", "-1", "not-an-int"])
def test_review_cli_rejects_nonpositive_or_invalid_limit(limit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["shigoto", "review", "--limit", limit])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize("fields,reason", [
    ({"location": "Boston, MA"}, "location"), ({"title": "Line Cook"}, "title"),
    ({"title": "QA Manager"}, "excluded_title"), ({"company": "Mérieux"}, "company"),
])
def test_rejected_records_keep_raw_reason_and_sighting_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             fields: dict[str, str], reason: str) -> None:
    config = config_for(tmp_path / "t.db")
    config.exclude_title_keywords = ["Manager"]
    config.exclude_companies = ["Mérieux"]
    store = Store(config.db_path)
    posting = Job(source="indeed", source_id="rejected", url="https://example.com/r", title="  QA Technician  ",
                  company=" Acme ", location=" Toronto ")
    for key, value in fields.items():
        setattr(posting, key, value)
    snapshot = job_json(posting)

    class FakeSource:
        name = "indeed"

        def fetch(self) -> Iterator[Job]:
            yield job_from_json(snapshot)

    first, last = "2026-10-01T12:00:00+00:00", "2026-10-02T12:00:00+00:00"
    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: first)
    stats = ingest(store, FakeSource(), CityMatcher(config.cities), config)
    assert stats == {"fetched_indeed": 1, "dropped": 1, f"rejected_{reason}": 1}
    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: last)
    ingest(store, FakeSource(), CityMatcher(config.cities), config)
    row = store.conn.execute("SELECT * FROM rejected_postings").fetchone()
    assert row["raw"] == snapshot and row["reason"] == reason
    assert row["first_seen"] == first and row["last_seen"] == last
    assert store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] == 0


def test_reprocess_rejected_promotes_merges_and_keeps_failed_sighting_times(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path / "t.db")
    store = Store(config.db_path)
    first, last, now = "2026-10-01T12:00:00+00:00", "2026-10-02T12:00:00+00:00", "2026-10-13T12:00:00+00:00"
    posting = Job(source="indeed", source_id="r", url="https://example.com/r", title="Bioanalytical Technician",
                  company="Acme", location="Toronto")
    store.reject(posting, job_json(posting), "title", first)
    store.reject(posting, job_json(posting), "title", last)
    # Another rejection will remain excluded and have its reason corrected by replay.
    other = Job(source="indeed", source_id="other", url="https://example.com/o", title="Line Cook",
                company="Acme", location="Toronto")
    store.reject(other, job_json(other), "company", first)
    store.commit()
    store.close()
    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: now)
    config.title_keywords.append("bioanalytic*")
    stats = reprocess(config, sync_sheet=False)
    assert stats["recovered"] == stats["new"] == stats["dropped"] == stats["rejected_title"] == 1
    assert stats["reprocessed"] == 2
    store = Store(config.db_path)
    [saved] = store.visible_jobs()
    assert saved.first_seen == first
    row = store.conn.execute("SELECT * FROM job_sources").fetchone()
    assert row["first_seen"] == first and row["last_seen"] == last and row["raw"] == job_json(posting)
    assert store.conn.execute("SELECT last_seen FROM jobs").fetchone()[0] == last
    rejected = store.conn.execute("SELECT * FROM rejected_postings").fetchone()
    assert rejected["source_id"] == "other" and rejected["reason"] == "title"
    assert rejected["first_seen"] == rejected["last_seen"] == first
    store.close()
    assert reprocess(config, sync_sheet=False)["reprocessed"] == 2


def test_reprocess_rejected_can_merge_into_known_job_without_changing_history(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path / "t.db")
    store = Store(config.db_path)
    known = Job(source="workday", source_id="known", url="https://example.com/k", title="QA Technician",
                company="ALS Global", location="Toronto", city="Toronto")
    first, now = "2026-10-01T12:00:00+00:00", "2026-10-13T12:00:00+00:00"
    store.upsert(known, first)
    [saved] = store.visible_jobs()
    store.backup_reviews(["Notes"], {saved.job_id: {"Notes": "Keep"}}, first)
    store.record_liveness(saved.job_id, "gone", "2026-10-04T12:00:00+00:00")
    rejected = Job(source="indeed", source_id="r", url="https://example.com/r", title="QA Technician",
                   company="ALS Global Inc.", location="Toronto")
    store.reject(rejected, job_json(rejected), "company", first)
    store.commit()
    store.close()
    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: now)
    assert reprocess(config, sync_sheet=False)["recovered"] == 1
    store = Store(config.db_path)
    [merged] = store.visible_jobs()
    assert merged.job_id == saved.job_id and merged.first_seen == first and merged.status == "Closed"
    assert store.reviews()[saved.job_id] == {"Notes": "Keep"}
    assert store.conn.execute("SELECT count(*) FROM rejected_postings").fetchone()[0] == 0
    assert {r[0] for r in store.conn.execute("SELECT last_seen FROM job_sources")} == {first}


def test_run_prunes_stale_rejected_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path / "t.db")
    store = Store(config.db_path)
    rejected = Job(source="indeed", source_id="r", url="u", title="Cook", company="Acme", location="Toronto")
    store.reject(rejected, job_json(rejected), "title", "2026-08-01T12:00:00+00:00")
    store.commit()
    store.close()
    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: "2026-10-13T12:00:00+00:00")
    monkeypatch.setattr("shigoto.pipeline.build_sources", lambda *args: [])
    monkeypatch.setattr("shigoto.pipeline.enrich_descriptions", lambda *args: {})
    monkeypatch.setattr("shigoto.pipeline.check_liveness", lambda *args: {})
    run_once(config, sync_sheet=False)
    store = Store(config.db_path)
    assert store.conn.execute("SELECT count(*) FROM rejected_postings").fetchone()[0] == 0


def test_scrape_acceptance_preserves_rejected_first_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path / "t.db")
    store = Store(config.db_path)
    posting = Job(source="indeed", source_id="r", url="https://example.com/r", title="QA Technician",
                  company="Acme", location="Toronto")
    first, now = "2026-10-01T12:00:00+00:00", "2026-10-13T12:00:00+00:00"
    store.reject(posting, job_json(posting), "title", first)

    class FakeSource:
        name = "indeed"

        def fetch(self) -> Iterator[Job]:
            yield posting

    monkeypatch.setattr("shigoto.pipeline.now_iso", lambda: now)
    assert ingest(store, FakeSource(), CityMatcher(config.cities), config)["new"] == 1
    [accepted] = store.visible_jobs()
    assert accepted.first_seen == first and accepted.updated_at == now
    source_row = store.conn.execute("SELECT first_seen, last_seen FROM job_sources").fetchone()
    assert tuple(source_row) == (first, now)
    assert store.conn.execute("SELECT last_seen FROM jobs").fetchone()[0] == now
    assert store.conn.execute("SELECT count(*) FROM rejected_postings").fetchone()[0] == 0


def test_ingest_commits_long_sources_periodically(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    store = Store(path)
    visible_mid_fetch: list[int] = []

    class FakeSource:
        name = "jobbank"

        def fetch(self) -> Iterator[Job]:
            for i in range(150):
                if i == 120:
                    # Another connection already sees the first committed batch.
                    visible_mid_fetch.append(Store(path).conn.execute("SELECT count(*) FROM jobs").fetchone()[0])
                yield Job(source=self.name, source_id=str(i), url=f"https://example.com/{i}", title="QA Technician",
                          company=f"Acme {i}", location="Toronto")

    assert ingest(store, FakeSource(), CityMatcher(config.cities), config)["new"] == 150
    assert visible_mid_fetch == [99]


@pytest.mark.parametrize("closed,review_due", [(0, False), (2, False), (0, True)])
def test_tick_reconciles_status_and_reapplies_controls_only_on_change(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, closed: int, review_due: bool) -> None:
    config = Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(spreadsheet_id="sheet"),
                    db_path=tmp_path / "t.db", google_credentials=tmp_path / "creds.json", liveness_max_per_tick=3,
                    review=ReviewConfig(enabled=True))
    calls: list[str] = []

    def liveness(store: object, cfg: Config, budget: int | None = None) -> dict[str, int]:
        calls.append(f"liveness:{budget}")
        return {"liveness_checked": 5, "closed": closed}

    monkeypatch.setattr(pipeline, "check_liveness", liveness)
    def record(name: str, value: object = None) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            calls.append(name)
            return value
        return call

    monkeypatch.setattr(pipeline, "open_worksheet", record("open", object()))
    monkeypatch.setattr(pipeline, "sync_status", record("status", closed))
    monkeypatch.setattr(pipeline, "configure_tracker", record("controls"))
    monkeypatch.setattr(pipeline, "review", record("review", ReviewReport()))
    stats = pipeline.tick(config, review_due=review_due)
    expected = ["liveness:3", "open", "status"] + (["controls"] if closed else []) + (["review"] if review_due else [])
    assert calls == expected and stats["closed"] == closed
