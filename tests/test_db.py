import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from shigoto.config import City, Config, SheetConfig
from shigoto.db import DIRECT_CHECK_SOURCES, FULL_LISTING_SOURCES, Store
from shigoto.models import Job, Liveness, job_from_json, job_json
from shigoto.pipeline import reprocess


def job(**kw: object) -> Job:
    base: dict[str, object] = dict(source="indeed", source_id="in-1", url="https://indeed/1",
                                   title="QA Technician", company="Acme", location="Toronto, ON",
                                   city="Toronto", description="Test food.")
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_cross_source_dedupe_and_change_detection(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    assert store.upsert(job(), "t1") == "new"
    assert store.upsert(job(), "t2") == "seen"
    # Same job on LinkedIn without a description: merges, doesn't blank the description.
    assert store.upsert(job(source="linkedin", source_id="li-9", description="", company="Acme Inc."), "t2") == "seen"
    store.commit()
    [only] = store.visible_jobs()
    assert "linkedin" in only.sources and "indeed" in only.sources
    # Non-primary source can't overwrite; primary source edit is a material change.
    assert store.upsert(job(source="linkedin", source_id="li-9", description="Different"), "t3") == "seen"
    assert store.upsert(job(description="Test food. Now with nights."), "t3") == "changed"
    store.commit()
    [changed] = store.visible_jobs()
    assert changed.updated_at == "t3" and changed.first_seen == "t1"


def test_same_source_postings_stay_separate(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    t1, t2 = "2026-10-01T12:00:00+00:00", "2026-10-02T12:00:00+00:00"
    assert store.upsert(job(source="workday", source_id="a", description="Plant A"), t1) == "new"
    assert store.upsert(job(source="workday", source_id="b", description="Plant B"), t1) == "new"
    assert store.upsert(job(source="workday", source_id="a", description="Plant A"), t2) == "seen"
    assert store.upsert(job(source="workday", source_id="b", description="Plant B"), t2) == "seen"
    store.commit()
    assert len(store.visible_jobs()) == 2


def test_description_enrichment_targets(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="linkedin", source_id="li-5", description=""), "t1")
    store.commit()
    [target] = store.missing_descriptions("linkedin", 10)
    assert target.source_id == "li-5"
    store.set_description(target.job_id, "Now described", "t2")
    assert store.missing_descriptions("linkedin", 10) == []
    assert store.visible_jobs()[0].description == "Now described"


def test_description_queue_skips_hidden_and_closed_jobs_without_using_the_limit(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    for source_id in ("open", "excluded", "closed"):
        store.upsert(job(source="zip_recruiter", source_id=source_id, description="", title=source_id), "t1")
    store.conn.execute("UPDATE jobs SET excluded_at='t2' WHERE title='excluded'")
    store.conn.execute("UPDATE jobs SET closed_at='t2' WHERE title='closed'")
    store.commit()
    assert [target.source_id for target in store.missing_descriptions("zip_recruiter", 1)] == ["open"]
    store.conn.execute("UPDATE jobs SET excluded_at=NULL, closed_at=NULL")
    store.commit()
    assert len(store.missing_descriptions("zip_recruiter", 3)) == 3


@pytest.mark.parametrize("source", ["linkedin", "workday", "zip_recruiter"])
def test_closure_checks_pause_for_excluded_jobs_and_resume_when_restored(tmp_path: Path, source: str) -> None:
    store = Store(tmp_path / "t.db")
    first = "2026-10-01T12:00:00+00:00"
    now = "2026-11-02T12:00:00+00:00"
    store.upsert(job(source=source), first)
    store.commit()
    [original] = store.visible_jobs()
    [candidate] = store.liveness_candidates(now)
    config = config_for(tmp_path / "t.db")
    config.exclude_title_keywords = ["Technician"]
    assert store.reevaluate_exclusions(config, now) == 1
    assert store.liveness_candidates(now) == []
    config.exclude_title_keywords = []
    assert store.reevaluate_exclusions(config, now) == 0
    assert store.liveness_candidates(now) == [candidate]
    assert store.visible_jobs() == [original]
    row = store.conn.execute("SELECT liveness_checked_at, closed_at FROM jobs").fetchone()
    assert row["liveness_checked_at"] is None and row["closed_at"] is None
    store.close()


@pytest.mark.parametrize("source", ["indeed", "linkedin", "workday", "zip_recruiter"])
@pytest.mark.parametrize("decision,reviewed,application,eligible", [
    ("Reject", "2026-10-02T12:00:00+00:00", {}, False),
    ("Reject", "2026-10-01T07:00:00-05:00", {}, False),
    ("Recommend", "2026-10-02T12:00:00+00:00", {}, True),
    ("Maybe", "2026-10-02T12:00:00+00:00", {}, True),
    ("", "", {}, True),
    ("Reject", "", {}, True),
    ("Reject", "invalid", {}, True),
    ("Reject", "2026-10-02T12:00:00", {}, True),
    ("Reject", "2026-09-30T12:00:00+00:00", {}, True),
    ("Reject", "2026-10-02T12:00:00+00:00", {"Application Status": "Applied"}, True),
    ("Reject", "2026-10-02T12:00:00+00:00", {"Applied At": "2026-10-03"}, True),
    ("Reject", "2026-10-02T12:00:00+00:00", {"Application Status": "Not Applied"}, False),
    ("Reject", "2026-10-02T12:00:00+00:00", {"Application Status": "Skip"}, False),
])
def test_closure_checks_skip_current_rejects_unless_applied(
        tmp_path: Path, source: str, decision: str, reviewed: str, application: dict[str, str],
        eligible: bool) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source=source), "2026-10-01T12:00:00+00:00")
    store.commit()
    [original] = store.visible_jobs()
    data = {"AI Decision": decision, "AI Reviewed At": reviewed, **application}
    store.backup_reviews(list(data), {original.job_id: data}, "2026-10-04T12:00:00+00:00")
    store.finish_sheet_rebuild()
    assert bool(store.liveness_candidates("2026-11-02T12:00:00+00:00")) is eligible
    assert store.visible_jobs() == [original]
    store.close()


def test_closure_checks_follow_published_reviews_and_newer_sheet_edits(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="linkedin"), "2026-10-01T12:00:00+00:00")
    store.commit()
    [original] = store.visible_jobs()
    columns = ["AI Decision", "AI Reviewed At", "Application Status"]
    now = "2026-11-02T12:00:00+00:00"

    def publish(decision: str, reviewed: str, *, written: bool = True) -> None:
        store.conn.execute(
            "INSERT INTO ai_results (job_id, fingerprint, policy, result, fields, written_at) "
            "VALUES (?, ?, 'policy', '{}', ?, ?)",
            (original.job_id, reviewed, json.dumps({"decision": decision, "reviewed_at": reviewed}),
             reviewed if written else None))
        store.commit()

    def backup(decision: str, at: str, application: str = "Not Applied") -> None:
        store.backup_reviews(columns, {original.job_id: {
            "AI Decision": decision, "AI Reviewed At": "2026-10-02T12:00:00+00:00",
            "Application Status": application,
        }}, at)
        store.finish_sheet_rebuild()

    backup("Recommend", "2026-10-03T12:00:00+00:00")
    publish("Reject", "2026-10-04T12:00:00+00:00")
    assert store.liveness_candidates(now) == []
    # A prepared but unpublished promotion does not replace the current rejection.
    publish("Maybe", "2026-10-05T07:00:00-05:00", written=False)
    assert store.liveness_candidates(now) == []
    store.conn.execute("UPDATE ai_results SET written_at=? WHERE written_at IS NULL",
                       ("2026-10-05T07:00:00-05:00",))
    store.commit()
    assert len(store.liveness_candidates(now)) == 1
    publish("Reject", "2026-10-05T13:00:00+00:00")
    assert store.liveness_candidates(now) == []
    # Later Sheet edits, including a cleared decision, take precedence over cached results.
    backup("Maybe", "2026-10-06T12:00:00+00:00")
    assert len(store.liveness_candidates(now)) == 1
    backup("", "2026-10-07T12:00:00+00:00")
    assert len(store.liveness_candidates(now)) == 1
    # Editing the posting makes its old rejection stale, without discarding the review.
    backup("Reject", "2026-10-08T12:00:00+00:00")
    assert store.liveness_candidates(now) == []
    assert store.upsert(job(source="linkedin", description="New duties"),
                        "2026-10-09T12:00:00+00:00") == "changed"
    store.commit()
    assert len(store.liveness_candidates(now)) == 1
    assert store.reviews()[original.job_id]["AI Decision"] == "Reject"
    backup("Reject", "2026-10-10T12:00:00+00:00", application="Applied")
    publish("Reject", "2026-10-11T12:00:00+00:00")
    assert len(store.liveness_candidates(now)) == 1
    store.close()


@pytest.mark.parametrize("source", sorted(FULL_LISTING_SOURCES))
def test_board_job_needs_confirmed_closure(tmp_path: Path, source: str) -> None:
    store = Store(tmp_path / "t.db")
    first_seen = "2026-10-01T12:00:00+00:00"
    now = "2026-10-04T12:00:00+00:00"
    store.upsert(job(source=source), first_seen)
    store.commit()
    [open_job] = store.visible_jobs()
    assert open_job.status == ""
    store.record_liveness(open_job.job_id, "gone", now)
    [closed] = store.visible_jobs()
    assert closed.status == "Closed"
    assert closed.updated_at == open_job.updated_at == first_seen
    assert closed.content_hash == open_job.content_hash
    assert store.liveness_candidates("2026-10-10T12:00:00+00:00") == []

@pytest.mark.parametrize("result", ["unknown", "alive"])
def test_inconclusive_or_alive_check_stays_open(tmp_path: Path, result: Liveness) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="workday"), "2026-10-01T12:00:00+00:00")
    store.commit()
    [stored] = store.visible_jobs()
    now = "2026-10-04T12:00:00+00:00"
    store.record_liveness(stored.job_id, result, now)
    assert store.visible_jobs()[0].status == ""
    row = store.conn.execute("SELECT closed_at, liveness_checked_at FROM jobs").fetchone()
    assert row["closed_at"] is None and row["liveness_checked_at"] == now
    assert store.liveness_candidates("2026-10-05T11:59:59+00:00") == []
    assert len(store.liveness_candidates("2026-10-05T12:00:00+00:00")) == 1

@pytest.mark.parametrize("source", ["glassdoor", "zip_recruiter"])
def test_uncheckable_link_is_omitted_from_closure_candidate(tmp_path: Path, source: str) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="workday"), "2026-10-01T12:00:00+00:00")
    store.upsert(job(source=source), "2026-10-01T12:00:00+00:00")
    store.commit()
    [candidate] = store.liveness_candidates("2026-10-10T12:00:00+00:00")
    assert [link.source for link in candidate.sources] == ["workday"]
    assert store.visible_jobs()[0].status == ""


@pytest.mark.parametrize("source", sorted(DIRECT_CHECK_SOURCES))
def test_direct_check_source_is_candidate_while_listed(tmp_path: Path, source: str) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source=source), "2026-10-04T12:00:00+00:00")
    store.commit()
    [candidate] = store.liveness_candidates("2026-10-04T12:00:00+00:00")
    assert [link.source for link in candidate.sources] == [source]


def test_recent_full_listing_sighting_prevents_closure_candidate(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="indeed"), "2026-10-01T12:00:00+00:00")
    store.upsert(job(source="workday"), "2026-10-04T12:00:00+00:00")
    store.commit()
    assert store.liveness_candidates("2026-10-05T12:00:00+00:00") == []
    assert len(store.liveness_candidates("2026-10-06T12:00:01+00:00")) == 1


def test_reappearing_job_reopens(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    first_seen = "2026-10-01T12:00:00+00:00"
    now = "2026-10-04T12:00:00+00:00"
    store.upsert(job(source="greenhouse"), first_seen)
    store.commit()
    [stored] = store.visible_jobs()
    store.record_liveness(stored.job_id, "gone", now)
    [closed] = store.visible_jobs()
    assert store.upsert(job(source="greenhouse"), now) == "seen"
    store.commit()
    [reopened] = store.visible_jobs()
    assert reopened.status == ""
    assert reopened.updated_at == first_seen
    assert reopened.content_hash == closed.content_hash
    # Recheck right away: Indeed search can still list a posting its API reports expired.
    assert tuple(store.conn.execute("SELECT closed_at, liveness_checked_at FROM jobs").fetchone()) == (None, None)


def test_candidates_use_latest_source_instant_across_offsets(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="greenhouse"), "2026-10-01T23:30:00+02:00")
    store.upsert(job(source="lever"), "2026-10-01T18:00:00-05:00")
    store.commit()
    assert store.liveness_candidates("2026-10-03T22:30:00+00:00") == []
    assert store.liveness_candidates("2026-10-03T23:00:00+00:00") == []
    [candidate] = store.liveness_candidates("2026-10-03T23:00:01+00:00")
    assert {link.source for link in candidate.sources} == {"greenhouse", "lever"}
    assert all(link.url == "https://indeed/1" for link in candidate.sources)


def test_candidates_oldest_first(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    for posting_id, seen in [("new", "2026-10-03"), ("middle", "2026-10-02"), ("old", "2026-10-01")]:
        store.upsert(job(source="workday", source_id=posting_id), f"{seen}T12:00:00+00:00")
    store.commit()
    now = "2026-10-05T12:00:00+00:00"
    old, middle = store.liveness_candidates(now)
    assert [old.sources[0].source_id, middle.sources[0].source_id] == ["old", "middle"]
    store.record_liveness(old.job_id, "unknown", now)
    assert store.liveness_candidates(now) == [middle]


def test_old_schema_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    original = Store(path)
    original.upsert(job(), "2026-10-01T12:00:00+00:00")
    original.commit()
    [stored] = original.visible_jobs()
    original.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN closed_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN liveness_checked_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN excluded_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN description_attempted_at")
        conn.execute("ALTER TABLE job_sources DROP COLUMN raw")
        conn.execute("ALTER TABLE jobs ADD COLUMN synced_hash TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN synced_at TEXT")
        conn.execute("DELETE FROM meta WHERE key='content_hash_version'")
        conn.execute("UPDATE jobs SET content_hash='old-hash'")
    for _ in range(2):
        migrated = Store(path)
        columns = {row["name"] for row in migrated.conn.execute("PRAGMA table_info(jobs)")}
        assert {"closed_at", "liveness_checked_at", "excluded_at", "description_attempted_at", "synced_hash", "synced_at"} <= columns
        assert "raw" in {r["name"] for r in migrated.conn.execute("PRAGMA table_info(job_sources)")}
        row = migrated.conn.execute("SELECT * FROM jobs").fetchone()
        assert row["job_id"] == stored.job_id and row["closed_at"] is None
        assert row["description_attempted_at"] is None
        assert row["content_hash"] == stored.content_hash
        assert row["updated_at"] == "2026-10-01T12:00:00+00:00"
        assert migrated.upsert(job(), "2026-10-02T12:00:00+00:00") == "seen"
        assert migrated.visible_jobs()[0].updated_at == "2026-10-01T12:00:00+00:00"
        migrated.close()

@pytest.mark.parametrize("fields", [
    {"salary": "$60,000"}, {"company": "Acme Canada"}, {"location": "Toronto, Ontario"},
    {"job_type": "Full-time"},
])
def test_other_review_field_changes_bump_updated_at(tmp_path: Path, fields: dict[str, str]) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(), "t1")
    assert store.upsert(job(**fields), "t2") == "changed"
    row = store.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["updated_at"] == "t2" and row["last_seen"] == "t2"
    for field, value in fields.items():
        assert row[field] == value

@pytest.mark.parametrize("fields", [{"title": "QA Specialist"}, {"description": "Test samples."}])
def test_review_field_changes_bump_updated_at(tmp_path: Path, fields: dict[str, str]) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(), "t1")
    assert store.upsert(job(**fields), "t2") == "changed"
    assert store.visible_jobs()[0].updated_at == "t2"


def test_enrichment_only_bumps_on_description_change(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(), "t1")
    [stored] = store.visible_jobs()
    store.set_description(stored.job_id, stored.description, "t2")
    assert store.visible_jobs()[0].updated_at == "t1"
    store.set_description(stored.job_id, "New description", "t3")
    assert store.visible_jobs()[0].updated_at == "t3"


def config_for(path: Path) -> Config:
    return Config(search_terms=[], cities=[City(name="Toronto", jobspy_location="Toronto")],
                  title_keywords=["QA"], sheet=SheetConfig(), db_path=path)

@pytest.mark.parametrize("change", ["city", "title", "exclude"])
def test_exclusions_toggle_without_changing_job_history(tmp_path: Path, change: str) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(), "t1")
    config = config_for(tmp_path / "t.db")
    if change == "city":
        config.cities = []
    elif change == "title":
        config.title_keywords = ["Microbiology"]
    else:
        config.exclude_title_keywords = ["Technician"]
    assert store.reevaluate_exclusions(config, "t2") == 1
    assert store.visible_jobs() == []
    assert store.reevaluate_exclusions(config, "t3") == 1
    row = store.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["excluded_at"] == "t2" and row["updated_at"] == "t1"
    assert store.reevaluate_exclusions(config_for(tmp_path / "t.db"), "t4") == 0
    assert store.visible_jobs()[0].updated_at == "t1"
    assert store.conn.execute("SELECT excluded_at FROM jobs").fetchone()[0] is None


def test_raw_record_roundtrip_and_latest_upsert(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    posting = job(title="  QA Technician  ", posted_date=date(2026, 10, 1))
    raw = job_json(posting)
    assert job_from_json(raw) == posting
    assert json.loads(raw)["posted_date"] == "2026-10-01"
    store.upsert(replace(posting, title="QA Technician"), "t1", raw=raw)
    assert store.conn.execute("SELECT raw FROM job_sources").fetchone()[0] == raw
    latest = replace(posting, description="Now with nights.")
    store.upsert(latest, "t2", raw=job_json(latest))
    assert job_from_json(store.conn.execute("SELECT raw FROM job_sources").fetchone()[0]) == latest
    assert job_from_json(job_json(job())).posted_date is None


def test_reprocess_preserves_recency_closure_and_applies_current_filters(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    store = Store(path)
    first_seen = "2026-10-01T12:00:00+00:00"
    raw_job = job(description="  New description.  ", city="", source="workday")
    store.upsert(job(source="workday", description="Old description"), first_seen, raw=job_json(raw_job))
    [stored] = store.visible_jobs()
    store.record_liveness(stored.job_id, "gone", "2026-10-04T12:00:00+00:00")
    # Pre-existing sources without snapshots are left alone.
    store.upsert(job(source_id="legacy", title="QA Legacy"), first_seen)
    store.conn.execute("UPDATE job_sources SET raw=NULL WHERE source_id='legacy'")
    store.commit()
    store.close()

    stats = reprocess(config, sync_sheet=False)
    assert stats["reprocessed"] == 1 and stats["changed"] == 1
    store = Store(path)
    row = store.conn.execute("SELECT * FROM jobs WHERE job_id=?", (stored.job_id,)).fetchone()
    assert row["description"] == "New description."
    assert row["first_seen"] == row["last_seen"] == first_seen
    assert row["closed_at"] == "2026-10-04T12:00:00+00:00"
    source = store.conn.execute("SELECT * FROM job_sources WHERE job_id=?", (stored.job_id,)).fetchone()
    assert source["first_seen"] == source["last_seen"] == first_seen
    assert source["raw"] == job_json(raw_job)
    store.close()

    config.exclude_title_keywords = ["Technician"]
    assert reprocess(config, sync_sheet=False)["sheet_hidden"] == 1
    store = Store(path)
    assert [j.title for j in store.visible_jobs()] == ["QA Legacy"]
    assert store.conn.execute("SELECT last_seen FROM jobs WHERE job_id=?", (stored.job_id,)).fetchone()[0] == first_seen
    store.close()
    config.exclude_title_keywords = []
    assert reprocess(config, sync_sheet=False)["sheet_hidden"] == 0


def test_reprocess_updates_fields_even_when_new_normalization_excludes_job(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    config.exclude_title_keywords = ["Manager"]
    store = Store(path)
    original = job()
    store.upsert(original, "t1", raw=job_json(replace(original, title="QA Manager")))
    store.commit()
    store.close()
    stats = reprocess(config, sync_sheet=False)
    assert stats["dropped"] == stats["sheet_hidden"] == 1
    store = Store(path)
    assert store.visible_jobs() == []
    row = store.conn.execute("SELECT title, last_seen FROM jobs").fetchone()
    assert row["title"] == "QA Manager" and row["last_seen"] == "t1"


def test_company_exclusions_hide_and_restore_without_losing_reviews(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(company="MERIEUX NutriSciences"), "t1")
    store.commit()
    [saved] = store.visible_jobs()
    store.backup_reviews(["Notes"], {saved.job_id: {"Notes": "Keep this"}}, "t1")
    config = config_for(tmp_path / "t.db")
    config.exclude_companies = ["Mérieux"]
    assert store.reevaluate_exclusions(config, "t2") == 1
    assert store.visible_jobs() == []
    assert store.reevaluate_exclusions(config, "t3") == 1
    assert store.conn.execute("SELECT excluded_at FROM jobs").fetchone()[0] == "t2"
    config.exclude_companies = []
    assert store.reevaluate_exclusions(config, "t4") == 0
    assert store.visible_jobs()[0].updated_at == "t1"
    assert store.reviews()[saved.job_id] == {"Notes": "Keep this"}


def test_posting_age_boundary_and_rolling_cutoff(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    config = config_for(tmp_path / "t.db")
    config.max_posting_age_days = 180
    for source_id, posted in [("old", date(2023, 1, 1)), ("before", date(2026, 4, 16)),
                              ("boundary", date(2026, 4, 17)), ("new", date(2026, 10, 14)),
                              ("undated", None), ("future", date(2026, 10, 15))]:
        store.upsert(job(source_id=source_id, company=source_id, posted_date=posted), "t1")
    store.commit()
    assert store.reevaluate_exclusions(config, "2026-10-14T23:00:00-05:00") == 2
    assert {j.company for j in store.visible_jobs()} == {"boundary", "new", "undated", "future"}
    assert store.reevaluate_exclusions(config, "2026-10-15T00:00:00-05:00") == 3
    assert {j.company for j in store.visible_jobs()} == {"new", "undated", "future"}


@pytest.mark.parametrize("relaxed", [365, None])
def test_posting_age_restoration_preserves_history_and_reviews(tmp_path: Path, relaxed: int | None) -> None:
    store = Store(tmp_path / "t.db")
    posting = job(posted_date=date(2026, 1, 1))
    store.upsert(posting, "2026-10-01T12:00:00+00:00", raw=job_json(posting))
    store.commit()
    [saved] = store.visible_jobs()
    store.backup_reviews(["AI Score", "Notes"], {saved.job_id: {"AI Score": "80", "Notes": "Keep"}}, "t1")
    store.record_liveness(saved.job_id, "gone", "2026-10-04T12:00:00+00:00")
    before = dict(store.conn.execute("SELECT * FROM jobs").fetchone())
    sources = [tuple(row) for row in store.conn.execute("SELECT * FROM job_sources")]
    config = config_for(tmp_path / "t.db")
    config.max_posting_age_days = 180
    assert store.reevaluate_exclusions(config, "2026-10-14T12:00:00+00:00") == 1
    assert store.visible_jobs() == []
    assert store.reevaluate_exclusions(config, "2026-10-15T12:00:00+00:00") == 1
    assert store.conn.execute("SELECT excluded_at FROM jobs").fetchone()[0] == "2026-10-14T12:00:00+00:00"
    config.max_posting_age_days = relaxed
    assert store.reevaluate_exclusions(config, "2026-10-15T12:00:00+00:00") == 0
    assert dict(store.conn.execute("SELECT * FROM jobs").fetchone()) == before
    assert [tuple(row) for row in store.conn.execute("SELECT * FROM job_sources")] == sources
    assert store.reviews()[saved.job_id] == {"AI Score": "80", "Notes": "Keep"}
    [restored] = store.visible_jobs()
    assert restored.job_id == saved.job_id and restored.status == "Closed"


@pytest.mark.parametrize("reverse", [False, True])
def test_unrelated_employers_with_shared_words_stay_separate(tmp_path: Path, reverse: bool) -> None:
    store = Store(tmp_path / "t.db")
    companies = ["Metro", "Metro Supply Chain"]
    if reverse:
        companies.reverse()
    store.upsert(job(company=companies[0]), "t1")
    [original] = store.visible_jobs()
    store.backup_reviews(["Notes"], {original.job_id: {"Notes": "Keep"}}, "t1")
    assert store.upsert(job(company=companies[1], source="linkedin"), "t2") == "new"
    assert len(store.visible_jobs()) == 2
    assert {j.company for j in store.visible_jobs()} == set(companies)
    assert store.reviews()[original.job_id] == {"Notes": "Keep"}
    assert store.conn.execute("SELECT count(DISTINCT job_id) FROM job_sources").fetchone()[0] == 2


def test_existing_mappings_do_not_merge_retroactively(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(company="ALS"), "t1")
    store.upsert(job(company="Different", source="linkedin"), "t1")
    ids = [r[0] for r in store.conn.execute("SELECT job_id FROM job_sources ORDER BY source")]
    store.upsert(job(company="ALS Global", source="linkedin"), "t2")
    assert [r[0] for r in store.conn.execute("SELECT job_id FROM job_sources ORDER BY source")] == ids
    assert len(store.visible_jobs()) == 2


def test_rejected_prune_uses_last_sighting_and_offsets(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    raw = job_json(job())
    store.reject(job(), raw, "title", "2026-08-14T14:00:00+02:00")
    assert store.prune_rejected("2026-10-13T12:00:00+00:00") == 0
    assert store.prune_rejected("2026-10-13T12:00:01+00:00") == 1
    store.reject(job(), raw, "title", "2026-08-01T12:00:00+00:00")
    store.reject(job(), raw, "company", "2026-10-01T12:00:00+00:00")
    row = store.conn.execute("SELECT * FROM rejected_postings").fetchone()
    assert row["first_seen"] == "2026-08-01T12:00:00+00:00" and row["reason"] == "company"
    assert store.prune_rejected("2026-10-13T12:00:00+00:00") == 0


def test_alive_answers_back_off_up_to_four_days(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="jobbank"), "2026-10-01T12:00:00+00:00")
    store.commit()
    [candidate] = store.liveness_candidates("2026-10-01T12:00:00+00:00")
    checked = "2026-10-02T12:00:00+00:00"
    for streak, wait_days in [(1, 1), (2, 2), (3, 4), (4, 4)]:
        store.record_liveness(candidate.job_id, "alive", checked)
        due = date.fromisoformat(checked[:10]).toordinal() + wait_days
        before, at = (date.fromordinal(due - 1).isoformat() + "T12:00:00+00:00",
                      date.fromordinal(due).isoformat() + "T12:00:00+00:00")
        assert store.liveness_candidates(before) == [], streak
        assert len(store.liveness_candidates(at)) == 1, streak
        checked = at
    store.record_liveness(candidate.job_id, "unknown", checked)  # inconclusive keeps the streak
    assert store.conn.execute("SELECT liveness_alive_streak FROM jobs").fetchone()[0] == 4


def test_part_time_job_type_is_excluded_and_rejected_table_migrates(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE rejected_postings (source TEXT NOT NULL, source_id TEXT NOT NULL, raw TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN ('location', 'title', 'excluded_title', 'company')),
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (source, source_id))""")
    old.execute("INSERT INTO rejected_postings VALUES ('indeed', '1', '{}', 'title', 'a', 'b')")
    old.commit()
    old.close()
    store = Store(path)
    store.reject(job(source_id="2"), "{}", "job_type", "2026-10-01T12:00:00+00:00")
    assert [r[0] for r in store.conn.execute("SELECT reason FROM rejected_postings ORDER BY source_id")] == \
        ["title", "job_type"]
    store.upsert(job(job_type="parttime"), "2026-10-01T12:00:00+00:00")
    store.commit()
    config = Config(search_terms=[], cities=[City(name="Toronto", jobspy_location="Toronto")],
                    title_keywords=["QA"], sheet=SheetConfig())
    assert store.reevaluate_exclusions(config, "2026-10-01T12:00:00+00:00") == 1
