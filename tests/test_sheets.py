import json
import logging
import sqlite3
from pathlib import Path
from typing import cast

import gspread
import pytest
from gspread.utils import ValueInputOption, a1_to_rowcol

from shigoto.config import City, Config, SheetConfig
from shigoto.db import Store
from shigoto.models import Job
from shigoto.sheets import APP_COLUMNS, CHUNK, HEADER, job_row, open_worksheet, sync


class FakeWorksheet:
    """In-memory values/grid operations, recording each write's input mode."""

    def __init__(self, rows: list[list[str]], *, row_count: int = 1000, col_count: int = 26) -> None:
        self.rows = [row.copy() for row in rows]
        self.row_count = max(row_count, len(rows))
        self.col_count = max(col_count, max(map(len, rows), default=0))
        self.writes: list[tuple[str, ValueInputOption, int]] = []
        self.resizes: list[tuple[int, int]] = []
        self.formatted_ranges: list[str] = []
        self.frozen_rows = 0

    def get_all_values(self) -> list[list[str]]:
        return [row.copy() for row in self.rows]

    def update(self, values: list[list[str]], range_name: str, *, value_input_option: ValueInputOption) -> None:
        self.writes.append((range_name, value_input_option, len(values)))
        row, col = a1_to_rowcol(range_name.split(":")[0])
        assert row + len(values) - 1 <= self.row_count
        for i, cells in enumerate(values, start=row - 1):
            assert col + len(cells) - 1 <= self.col_count
            while len(self.rows) <= i:
                self.rows.append([])
            self.rows[i].extend([""] * max(0, col - 1 + len(cells) - len(self.rows[i])))
            self.rows[i][col - 1:col - 1 + len(cells)] = cells

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))
        self.row_count, self.col_count = rows, cols
        self.rows = [row[:cols] for row in self.rows[:rows]]

    def freeze(self, rows: int) -> None:
        self.frozen_rows = rows

    def format(self, range_name: str, formatting: dict[str, object]) -> None:
        self.formatted_ranges.append(range_name)


def config_for(path: Path) -> Config:
    return Config(search_terms=[], cities=[City(name="Toronto", jobspy_location="Toronto")],
                  title_keywords=["QA"], sheet=SheetConfig(), db_path=path)


def posting(source_id: str, *, city: str = "Toronto", title: str = "QA Technician") -> Job:
    return Job(source="indeed", source_id=source_id, url=f"https://indeed/{source_id}", title=title,
               company="Acme", location=city, city=city, description="=Never interpret job text")


def rebuild(store: Store, ws: FakeWorksheet, config: Config) -> dict[str, int]:
    return sync(store, cast(gspread.Worksheet, ws), config)


def test_rebuild_preserves_arbitrary_reviews_by_job_and_hides_old_rows(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("early"), "2026-10-01")
    store.upsert(posting("late"), "2026-10-02")
    store.upsert(posting("hidden", city="Montreal"), "2026-10-03")
    store.commit()
    early, late, hidden = store.visible_jobs()
    # All ten live reviewer columns, with the timestamp moved and one blank header.
    columns = ["AI Score", "AI Notes", "AI Decision", "AI Category", "AI Reviewed At",
               "Application Status", "Applied At", "Application Stage", "Follow-up Date", ""]
    early_review = ["8", "early notes", "Yes", "QA", "2026-10-04", "Applied", "2026-10-05", "Interview", "2026-10-10", "private"]
    late_review = ["4", "late notes", "No", "Lab", "", "", "", "", "", ""]
    hidden_review = ["9", "keep me", "Yes", "Lab", "2026-10-04", "", "", "", "", "Montreal notes"]
    ws = FakeWorksheet([
        APP_COLUMNS + columns,
        job_row(late, 20000) + late_review,
        job_row(hidden, 20000) + hidden_review,
        ["unknown", *([""] * 13), "5", "unknown notes"],
        job_row(early, 20000) + early_review,
        ["", *([""] * 23)],
    ])
    with caplog.at_level(logging.WARNING):
        assert rebuild(store, ws, config_for(path)) == {"sheet_rows": 2, "sheet_hidden": 1, "reviews_backed_up": 4}
    assert "1 sheet jobs absent from SQLite" in caplog.text
    columns[-1] = "Column X"
    assert ws.rows == [APP_COLUMNS + columns, job_row(early, 20000) + early_review, job_row(late, 20000) + late_review]
    assert ws.row_count == 3 and ws.col_count == 24
    assert store.reviewer_columns() == columns
    assert store.reviews()[hidden.job_id]["Column X"] == "Montreal notes"
    assert store.reviews()["unknown"]["AI Notes"] == "unknown notes"
    assert store.conn.execute("SELECT ai_reviewed_at FROM jobs WHERE job_id=?", (early.job_id,)).fetchone()[0] == "2026-10-04"
    assert ws.writes == [("A1", ValueInputOption.raw, 1), ("A2:N3", ValueInputOption.raw, 2),
                         ("O2:X3", ValueInputOption.user_entered, 2)]


def test_user_cleared_value_and_changed_header_order(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("1"), "t1")
    store.commit()
    [stored] = store.visible_jobs()
    ws = FakeWorksheet([APP_COLUMNS + ["Notes", "AI Reviewed At", "Score"],
                        job_row(stored, 20000) + ["Keep this", "t2", "8"]])
    rebuild(store, ws, config_for(path))
    # The reviewer reorders/removes headers, adds a custom one, and clears a value.
    ws.rows[0] = APP_COLUMNS + ["Score", "Notes", "Custom"]
    ws.rows[1] = job_row(stored, 20000) + ["8", "", "TRUE"]
    rebuild(store, ws, config_for(path))
    assert ws.rows[1][len(APP_COLUMNS):] == ["8", "", "TRUE"]
    assert store.reviews()[stored.job_id] == {"Score": "8", "Notes": "", "Custom": "TRUE"}
    assert store.reviewer_columns() == ["Score", "Notes", "Custom"]
    # An absent timestamp header leaves the existing SQLite timestamp alone.
    assert store.conn.execute("SELECT ai_reviewed_at FROM jobs").fetchone()[0] == "t2"


def test_excluded_job_returns_with_review_after_config_relaxed(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    config = config_for(path)
    store = Store(path)
    store.upsert(posting("1"), "t1")
    store.commit()
    [stored] = store.visible_jobs()
    review = ["t2", "8", "Keep me"]
    ws = FakeWorksheet([HEADER, job_row(stored, 20000) + review])
    config.exclude_title_keywords = ["Technician"]
    assert rebuild(store, ws, config)["sheet_hidden"] == 1
    assert ws.rows == [HEADER]
    rebuild(store, ws, config)  # An absent hidden row must not clear its backup.
    config.exclude_title_keywords = []
    assert rebuild(store, ws, config) == {"sheet_rows": 1, "sheet_hidden": 0, "reviews_backed_up": 0}
    assert ws.rows == [HEADER, job_row(stored, 20000) + review]
    ws.rows[1][len(APP_COLUMNS)] = ""
    rebuild(store, ws, config)
    assert store.conn.execute("SELECT ai_reviewed_at FROM jobs").fetchone()[0] is None


def test_closed_job_remains_visible_with_review(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("1"), "t1")
    store.commit()
    [stored] = store.visible_jobs()
    ws = FakeWorksheet([HEADER, job_row(stored, 20000) + ["t2", "8", "Notes"]])
    store.record_liveness(stored.job_id, "gone", "t3")
    assert rebuild(store, ws, config_for(path))["sheet_rows"] == 1
    assert ws.rows[1][APP_COLUMNS.index("Status")] == "Closed"
    assert ws.rows[1][APP_COLUMNS.index("Updated At")] == "t1"
    assert ws.rows[1][len(APP_COLUMNS):] == ["t2", "8", "Notes"]


def test_backup_committed_before_sheet_write_failure(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("1"), "t1")
    store.commit()
    [stored] = store.visible_jobs()

    class FailedWorksheet(FakeWorksheet):
        def update(self, values: list[list[str]], range_name: str, *, value_input_option: ValueInputOption) -> None:
            with sqlite3.connect(path) as conn:
                data = json.loads(conn.execute("SELECT data FROM reviews").fetchone()[0])
                assert data == {"Notes": "Durable"}
                assert json.loads(conn.execute("SELECT value FROM meta WHERE key='reviewer_columns'").fetchone()[0]) == ["Notes"]
            raise RuntimeError("sheet offline")

    ws = FailedWorksheet([APP_COLUMNS + ["Notes"], job_row(stored, 20000) + ["Durable"]])
    with pytest.raises(RuntimeError, match="sheet offline"):
        rebuild(store, ws, config_for(path))
    assert store.reviews()[stored.job_id] == {"Notes": "Durable"}


def test_retry_uses_backup_after_ids_and_reviews_become_misaligned(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("early"), "t1")
    store.upsert(posting("late"), "t2")
    store.commit()
    early, late = store.visible_jobs()

    class InterruptedWorksheet(FakeWorksheet):
        fail = True

        def update(self, values: list[list[str]], range_name: str, *, value_input_option: ValueInputOption) -> None:
            if self.fail and value_input_option == ValueInputOption.user_entered:
                raise RuntimeError("interrupted after app rows")
            super().update(values, range_name, value_input_option=value_input_option)

    ws = InterruptedWorksheet([APP_COLUMNS + ["Notes"], job_row(late, 20000) + ["Late notes"],
                               job_row(early, 20000) + ["Early notes"]])
    with pytest.raises(RuntimeError, match="interrupted after app rows"):
        rebuild(store, ws, config_for(path))
    assert ws.rows[1][0] == early.job_id and ws.rows[1][-1] == "Late notes"
    assert store.sheet_rebuild_pending()
    store.close()
    store = Store(path)  # Retry after a process restart.
    ws.fail = False
    assert rebuild(store, ws, config_for(path))["reviews_backed_up"] == 0
    assert ws.rows == [APP_COLUMNS + ["Notes"], job_row(early, 20000) + ["Early notes"],
                       job_row(late, 20000) + ["Late notes"]]
    assert not store.sheet_rebuild_pending()


@pytest.mark.parametrize("header", [APP_COLUMNS[:5] + ["Posting date"] + APP_COLUMNS[6:],
                                   APP_COLUMNS[:12] + ["Description"], ["", ""]])
def test_unrecognized_header_raises_without_writes(tmp_path: Path, header: list[str]) -> None:
    rows = [header, ["job", "untouched", *([""] * 12), "review"]]
    ws = FakeWorksheet(rows)
    store = Store(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="Unrecognized sheet header"):
        rebuild(store, ws, config_for(tmp_path / "t.db"))
    assert ws.rows == rows and ws.writes == [] and ws.resizes == []
    assert store.reviews() == {} and store.reviewer_columns() is None


@pytest.mark.parametrize("header", [[], ["", "  "], APP_COLUMNS, APP_COLUMNS + ["Notes"]])
def test_empty_or_current_headers(tmp_path: Path, header: list[str]) -> None:
    path = tmp_path / "t.db"
    ws = FakeWorksheet([header])
    assert rebuild(Store(path), ws, config_for(path)) == {"sheet_rows": 0, "sheet_hidden": 0, "reviews_backed_up": 0}
    assert ws.rows[0] == (header if header[:len(APP_COLUMNS)] == APP_COLUMNS else HEADER)
    assert ws.row_count == 1 and ws.col_count == len(ws.rows[0])


def test_values_beyond_header_get_column_names(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("1"), "t1")
    store.commit()
    [stored] = store.visible_jobs()
    ws = FakeWorksheet([APP_COLUMNS, job_row(stored, 20000) + ["unlabeled", "extra"]])
    rebuild(store, ws, config_for(path))
    assert ws.rows[0] == APP_COLUMNS + ["Column O", "Column P"]
    assert ws.rows[1][-2:] == ["unlabeled", "extra"]


def test_duplicate_reviewer_headers_fail_before_writes(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    ws = FakeWorksheet([APP_COLUMNS + ["Notes", "Notes"], ["id", *([""] * 13), "one", "two"]])
    with pytest.raises(RuntimeError, match="Duplicate reviewer headers"):
        rebuild(Store(path), ws, config_for(path))
    assert ws.writes == [] and ws.resizes == []


def test_duplicate_job_ids_preserve_both_sheet_rows_and_previous_backup(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    store.upsert(posting("1"), "t1")
    store.commit()
    [stored] = store.visible_jobs()
    ws = FakeWorksheet([APP_COLUMNS + ["Notes"], job_row(stored, 20000) + ["Keep notes"]])
    rebuild(store, ws, config_for(path))
    ws.rows.append(job_row(stored, 20000) + [""])
    before = ws.get_all_values()
    ws.writes.clear()
    ws.resizes.clear()
    with pytest.raises(RuntimeError, match="Duplicate sheet Job ID"):
        rebuild(store, ws, config_for(path))
    assert ws.rows == before and ws.writes == [] and ws.resizes == []
    assert store.reviews()[stored.job_id] == {"Notes": "Keep notes"}


def test_chunked_writes_grow_grid_and_clear_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    for i in range(CHUNK * 2 + 1):
        store.upsert(posting(str(i), title=f"QA {i}"), "t1")
    store.commit()
    ws = FakeWorksheet([HEADER], row_count=1, col_count=len(HEADER))
    assert rebuild(store, ws, config_for(path))["sheet_rows"] == CHUNK * 2 + 1
    assert [count for _, option, count in ws.writes if option == ValueInputOption.user_entered] == [CHUNK, CHUNK, 1]
    assert len(ws.rows) == CHUNK * 2 + 2 and ws.resizes[0] == (CHUNK * 2 + 2, len(HEADER))
    assert all(row[APP_COLUMNS.index("Description")].startswith("=") for row in ws.rows[1:])
    assert all(option == ValueInputOption.raw for name, option, _ in ws.writes if name.startswith("A"))


def test_live_header_and_721_jobs_rebuild_offline(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = Store(path)
    for i in range(721):
        store.upsert(posting(str(i), title=f"QA {i}"), "t1")
    store.commit()
    columns = ["AI Reviewed At", "AI Score", "AI Notes", "AI Decision", "AI Category", "Application Status",
               "Applied At", "Application Stage", "Follow-up Date", "User Notes"]
    jobs = store.visible_jobs()
    reviews = {job.job_id: ["2026-10-06", "8", "", "Yes", "QA", "Applied", "2026-10-06", "", "", job.title]
               for job in jobs}
    ws = FakeWorksheet([APP_COLUMNS + columns] + [job_row(job, 20000) + reviews[job.job_id] for job in reversed(jobs)])
    assert rebuild(store, ws, config_for(path)) == {"sheet_rows": 721, "sheet_hidden": 0, "reviews_backed_up": 721}
    assert ws.rows == [APP_COLUMNS + columns] + [job_row(job, 20000) + reviews[job.job_id] for job in jobs]
    assert ws.resizes[-1] == (722, 24)
    assert [count for _, option, count in ws.writes if option == ValueInputOption.user_entered] == [200, 200, 200, 121]


def test_new_worksheet_header_and_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = FakeWorksheet([])

    class FakeSpreadsheet:
        def worksheet(self, title: str) -> gspread.Worksheet:
            raise gspread.WorksheetNotFound(title)

        def add_worksheet(self, title: str, rows: int, cols: int) -> gspread.Worksheet:
            assert title == "Shigoto" and rows == 1000 and cols == len(HEADER)
            return cast(gspread.Worksheet, ws)

    class FakeClient:
        def open_by_key(self, key: str) -> FakeSpreadsheet:
            assert key == "sheet-id"
            return FakeSpreadsheet()

    def service_account(filename: str) -> FakeClient:
        assert filename == str(tmp_path / "credentials.json")
        return FakeClient()

    monkeypatch.setattr(gspread, "service_account", service_account)
    worksheet = open_worksheet(tmp_path / "credentials.json", SheetConfig(spreadsheet_id="sheet-id"))
    assert worksheet is cast(gspread.Worksheet, ws)
    assert ws.writes == []  # sync writes the header after backing up.
    assert ws.frozen_rows == 1 and ws.formatted_ranges == ["1:1", "A:Q"]


@pytest.mark.parametrize("missing", [False, True])
def test_explicit_worksheet_id_survives_rename_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: bool,
) -> None:
    ws = FakeWorksheet([])

    class FakeSpreadsheet:
        def get_worksheet_by_id(self, worksheet_id: int) -> gspread.Worksheet:
            assert worksheet_id == 0
            if missing:
                raise gspread.WorksheetNotFound(worksheet_id)
            return cast(gspread.Worksheet, ws)

        def worksheet(self, title: str) -> gspread.Worksheet:
            raise AssertionError("An explicit ID cannot fall back to a same-named view")

    class FakeClient:
        def open_by_key(self, key: str) -> FakeSpreadsheet:
            return FakeSpreadsheet()

    monkeypatch.setattr(gspread, "service_account", lambda filename: FakeClient())
    config = SheetConfig(spreadsheet_id="sheet-id", worksheet="Renamed master", worksheet_id=0)
    if missing:
        with pytest.raises(gspread.WorksheetNotFound):
            open_worksheet(tmp_path / "credentials.json", config)
    else:
        assert open_worksheet(tmp_path / "credentials.json", config) is cast(gspread.Worksheet, ws)
