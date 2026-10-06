import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from shigoto.config import City, Config, SheetConfig
from shigoto.db import FULL_LISTING_SOURCES, Store
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
    assert store.liveness_candidates("2026-10-10T12:00:00+00:00", 40) == []

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
    assert store.liveness_candidates("2026-10-05T11:59:59+00:00", 40) == []
    assert len(store.liveness_candidates("2026-10-05T12:00:00+00:00", 40)) == 1

@pytest.mark.parametrize("source", ["indeed", "linkedin", "glassdoor", "jobbank"])
def test_recency_source_prevents_closure_candidate(tmp_path: Path, source: str) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="workday"), "2026-10-01T12:00:00+00:00")
    store.upsert(job(source=source), "2026-10-01T12:00:00+00:00")
    store.commit()
    assert store.liveness_candidates("2026-10-10T12:00:00+00:00", 40) == []
    assert store.visible_jobs()[0].status == ""


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
    assert store.conn.execute("SELECT closed_at FROM jobs").fetchone()[0] is None


def test_candidates_use_latest_source_instant_across_offsets(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="greenhouse"), "2026-10-01T23:30:00+02:00")
    store.upsert(job(source="lever"), "2026-10-01T18:00:00-05:00")
    store.commit()
    assert store.liveness_candidates("2026-10-03T22:30:00+00:00", 40) == []
    assert store.liveness_candidates("2026-10-03T23:00:00+00:00", 40) == []
    [candidate] = store.liveness_candidates("2026-10-03T23:00:01+00:00", 40)
    assert {link.source for link in candidate.sources} == {"greenhouse", "lever"}
    assert all(link.url == "https://indeed/1" for link in candidate.sources)


def test_candidates_oldest_first_and_capped(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    for posting_id, seen in [("new", "2026-10-03"), ("middle", "2026-10-02"), ("old", "2026-10-01")]:
        store.upsert(job(source="workday", source_id=posting_id), f"{seen}T12:00:00+00:00")
    store.commit()
    now = "2026-10-05T12:00:00+00:00"
    assert store.liveness_candidates(now, 0) == []
    [old] = store.liveness_candidates(now, 1)
    assert old.sources[0].source_id == "old"
    assert [t.sources[0].source_id for t in store.liveness_candidates(now, 40)] == ["old", "middle"]
    store.record_liveness(old.job_id, "unknown", now)
    [middle] = store.liveness_candidates(now, 40)
    assert middle.sources[0].source_id == "middle"


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
        conn.execute("ALTER TABLE job_sources DROP COLUMN raw")
        conn.execute("ALTER TABLE jobs ADD COLUMN synced_hash TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN synced_at TEXT")
        conn.execute("DELETE FROM meta WHERE key='content_hash_version'")
        conn.execute("UPDATE jobs SET content_hash='old-hash'")
    for _ in range(2):
        migrated = Store(path)
        columns = {row["name"] for row in migrated.conn.execute("PRAGMA table_info(jobs)")}
        assert {"closed_at", "liveness_checked_at", "excluded_at", "synced_hash", "synced_at"} <= columns
        assert "raw" in {r["name"] for r in migrated.conn.execute("PRAGMA table_info(job_sources)")}
        row = migrated.conn.execute("SELECT * FROM jobs").fetchone()
        assert row["job_id"] == stored.job_id and row["closed_at"] is None
        assert row["content_hash"] == stored.content_hash
        assert row["updated_at"] == "2026-10-01T12:00:00+00:00"
        assert migrated.upsert(job(), "2026-10-02T12:00:00+00:00") == "seen"
        assert migrated.visible_jobs()[0].updated_at == "2026-10-01T12:00:00+00:00"
        migrated.close()

@pytest.mark.parametrize("fields", [
    {"salary": "$60,000"}, {"company": "Acme Canada"}, {"location": "Toronto, Ontario"},
    {"job_type": "Full-time"},
])
def test_other_field_changes_preserve_updated_at(tmp_path: Path, fields: dict[str, str]) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(), "t1")
    assert store.upsert(job(**fields), "t2") == "seen"
    row = store.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["updated_at"] == "t1" and row["last_seen"] == "t2"
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
