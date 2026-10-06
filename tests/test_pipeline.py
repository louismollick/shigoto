import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from shigoto import main
from shigoto.config import City, Config, SheetConfig
from shigoto.db import Store
from shigoto.models import Job, job_from_json
from shigoto.normalize import CityMatcher
from shigoto.pipeline import ingest, reprocess, run_once


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
    monkeypatch.setattr("shigoto.pipeline.build_sources", lambda config, matcher, backfill: [])
    monkeypatch.setattr("shigoto.pipeline.enrich_descriptions", lambda store, config: {})
    monkeypatch.setattr("shigoto.pipeline.check_liveness", lambda store, config: {})
    assert run_once(config, sync_sheet=False) == {"sheet_hidden": 1}
    store = Store(path)
    assert store.visible_jobs() == []
    assert store.conn.execute("SELECT last_seen FROM jobs").fetchone()[0] == "t1"
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
