import sqlite3
from pathlib import Path

import pytest

from shigoto.db import FULL_LISTING_SOURCES, Store
from shigoto.models import Job, Liveness


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
    [only] = store.unsynced()
    assert "linkedin" in only.sources and "indeed" in only.sources
    store.mark_synced([only], "t2")
    assert store.unsynced() == []
    # Non-primary source can't overwrite; primary source edit is a material change.
    assert store.upsert(job(source="linkedin", source_id="li-9", description="Different"), "t3") == "seen"
    assert store.upsert(job(description="Test food. Now with nights."), "t3") == "changed"
    store.commit()
    [changed] = store.unsynced()
    assert changed.updated_at == "t3" and changed.first_seen == "t1"


def test_same_source_postings_stay_separate(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    t1, t2 = "2026-10-01T12:00:00+00:00", "2026-10-02T12:00:00+00:00"
    assert store.upsert(job(source="workday", source_id="a", description="Plant A"), t1) == "new"
    assert store.upsert(job(source="workday", source_id="b", description="Plant B"), t1) == "new"
    assert store.upsert(job(source="workday", source_id="a", description="Plant A"), t2) == "seen"
    assert store.upsert(job(source="workday", source_id="b", description="Plant B"), t2) == "seen"
    store.commit()
    assert len(store.unsynced()) == 2


def test_description_enrichment_targets(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="linkedin", source_id="li-5", description=""), "t1")
    store.commit()
    [target] = store.missing_descriptions("linkedin", 10)
    assert target.source_id == "li-5"
    store.set_description(target.job_id, "Now described", "t2")
    assert store.missing_descriptions("linkedin", 10) == []
    assert store.unsynced()[0].description == "Now described"



@pytest.mark.parametrize("source", sorted(FULL_LISTING_SOURCES))
def test_board_job_needs_confirmed_closure(tmp_path: Path, source: str) -> None:
    store = Store(tmp_path / "t.db")
    first_seen = "2026-10-01T12:00:00+00:00"
    now = "2026-10-04T12:00:00+00:00"
    store.upsert(job(source=source), first_seen)
    store.commit()
    [open_job] = store.unsynced()
    assert open_job.status == ""
    store.mark_synced([open_job], first_seen)
    assert store.unsynced() == []  # Staleness alone never closes a job.
    store.record_liveness(open_job.job_id, "gone", now)
    [closed] = store.unsynced()
    assert closed.status == "Closed"
    assert closed.updated_at == open_job.updated_at == first_seen
    assert closed.content_hash == open_job.content_hash
    store.mark_synced([closed], now)
    assert store.unsynced() == []
    assert store.liveness_candidates("2026-10-10T12:00:00+00:00", 40) == []


@pytest.mark.parametrize("result", ["unknown", "alive"])
def test_inconclusive_or_alive_check_stays_open(tmp_path: Path, result: Liveness) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="workday"), "2026-10-01T12:00:00+00:00")
    store.commit()
    [stored] = store.unsynced()
    store.mark_synced([stored], "2026-10-01T12:00:00+00:00")
    now = "2026-10-04T12:00:00+00:00"
    store.record_liveness(stored.job_id, result, now)
    assert store.unsynced() == []
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
    assert store.unsynced()[0].status == ""


def test_reappearing_job_reopens(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    first_seen = "2026-10-01T12:00:00+00:00"
    now = "2026-10-04T12:00:00+00:00"
    store.upsert(job(source="greenhouse"), first_seen)
    store.commit()
    [stored] = store.unsynced()
    store.record_liveness(stored.job_id, "gone", now)
    [closed] = store.unsynced()
    store.mark_synced([closed], now)
    assert store.upsert(job(source="greenhouse"), now) == "seen"
    store.commit()
    [reopened] = store.unsynced()
    assert reopened.status == ""
    assert reopened.updated_at == first_seen
    assert reopened.content_hash == closed.content_hash
    assert store.conn.execute("SELECT closed_at FROM jobs").fetchone()[0] is None
    store.mark_synced([reopened], now)
    assert store.unsynced() == []


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
    [stored] = original.unsynced()
    original.mark_synced([stored], "2026-10-01T12:00:00+00:00")
    original.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN closed_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN liveness_checked_at")
    for _ in range(2):
        migrated = Store(path)
        columns = {row["name"] for row in migrated.conn.execute("PRAGMA table_info(jobs)")}
        assert {"closed_at", "liveness_checked_at"} <= columns
        assert migrated.unsynced() == []
        row = migrated.conn.execute("SELECT * FROM jobs").fetchone()
        assert row["job_id"] == stored.job_id and row["closed_at"] is None
        assert row["content_hash"] == stored.content_hash
        migrated.close()
