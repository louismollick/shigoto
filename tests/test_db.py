from pathlib import Path

from shigoto.db import Store
from shigoto.models import Job


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


def test_description_enrichment_targets(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(job(source="linkedin", source_id="li-5", description=""), "t1")
    store.commit()
    [target] = store.missing_descriptions("linkedin", 10)
    assert target.source_id == "li-5"
    store.set_description(target.job_id, "Now described", "t2")
    assert store.missing_descriptions("linkedin", 10) == []
    assert store.unsynced()[0].description == "Now described"
