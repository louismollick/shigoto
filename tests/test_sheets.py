from pathlib import Path
from typing import TypedDict, cast

import gspread
import pytest
from gspread.utils import ValueInputOption, a1_to_rowcol

from shigoto.config import SheetConfig
from shigoto.db import Store
from shigoto.models import Job
from shigoto.sheets import AI_REVIEWED_COL, APP_COLUMNS, HEADER, job_row, open_worksheet, sync

OLD_HEADER = [
    "Job ID", "Title", "Company", "City", "Location", "Posted", "Salary", "Type", "URL", "Sources",
    "First Seen", "Updated At", "Description", "AI Reviewed At", "AI Score", "AI Notes",
]


class Update(TypedDict):
    range: str
    values: list[list[str]]


class FakeWorksheet:
    """The sheet operations used by sync, with columns shifted on insertion."""

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = [row.copy() for row in rows]
        self.inserted_cols: list[int] = []
        self.updated_ranges: list[str] = []
        self.formatted_ranges: list[str] = []
        self.frozen_rows = 0
        self.header_updates = 0

    def get_all_values(self) -> list[list[str]]:
        return [row.copy() for row in self.rows]

    def insert_cols(self, values: list[list[str]], col: int) -> None:
        assert values == [[""]]
        self.inserted_cols.append(col)
        for row in self.rows:
            row.insert(col - 1, "")

    def update(self, values: list[list[str]], range_name: str, *, value_input_option: ValueInputOption) -> None:
        assert range_name == "A1" and value_input_option == ValueInputOption.raw
        self.header_updates += 1
        if not self.rows:
            self.rows.append([])
        self.rows[0][:len(values[0])] = values[0]

    def batch_update(self, data: list[Update], *, value_input_option: ValueInputOption) -> None:
        assert value_input_option == ValueInputOption.raw
        for update in data:
            self.updated_ranges.append(update["range"])
            first, last = update["range"].split(":")
            row, col = a1_to_rowcol(first)
            last_row, last_col = a1_to_rowcol(last)
            assert row == last_row and len(update["values"][0]) == last_col - col + 1
            self.rows[row - 1][col - 1:last_col] = update["values"][0]

    def append_rows(self, values: list[list[str]], *, value_input_option: ValueInputOption, table_range: str) -> None:
        assert value_input_option == ValueInputOption.raw and table_range == "A1"
        self.rows.extend(row.copy() for row in values)

    def freeze(self, rows: int) -> None:
        self.frozen_rows = rows

    def format(self, range_name: str, formatting: dict[str, object]) -> None:
        self.formatted_ranges.append(range_name)


def test_old_sheet_migration_preserves_ai_columns(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    posting = Job(source="indeed", source_id="1", url="https://indeed/1", title="QA Technician",
                  company="Acme", location="Toronto, ON", city="Toronto", description="Test food.")
    store.upsert(posting, "2026-10-01T12:00:00+00:00")
    store.commit()
    [stored] = store.unsynced()
    store.mark_synced([stored], "2026-10-01T12:00:00+00:00")
    # Simulate the legacy fingerprint and the live sheet's 16-column layout.
    assert stored.sync_hash == stored.content_hash
    old_row = job_row(stored, 20000)
    del old_row[12]
    ai_values = ["2026-10-02T12:00:00+00:00", "8", "Review notes"]
    other_row = ["other-job", *([""] * 12), "2026-10-03", "4", "Other notes"]
    ws = FakeWorksheet([OLD_HEADER, old_row + ai_values, other_row])
    worksheet = cast(gspread.Worksheet, ws)

    assert sync(store, worksheet, SheetConfig()) == {"sheet_appended": 0, "sheet_updated": 0}
    assert ws.inserted_cols == [13]
    assert ws.rows[0] == HEADER
    assert ws.rows[1] == job_row(stored, 20000) + ai_values
    assert ws.rows[2][AI_REVIEWED_COL:] == other_row[13:]
    assert ws.updated_ranges == []
    reviewed = store.conn.execute("SELECT ai_reviewed_at FROM jobs WHERE job_id=?", (stored.job_id,)).fetchone()
    assert reviewed["ai_reviewed_at"] == ai_values[0]

    posting.description = "Now with nights."
    store.upsert(posting, "2026-10-03T12:00:00+00:00")
    store.commit()
    assert sync(store, worksheet, SheetConfig()) == {"sheet_appended": 0, "sheet_updated": 1}
    assert ws.inserted_cols == [13]
    assert ws.updated_ranges == ["A2:N2"]
    assert ws.rows[1][APP_COLUMNS.index("Description")] == posting.description
    assert ws.rows[1][AI_REVIEWED_COL:] == ai_values


def test_status_flips_sync_without_ai_review_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "t.db")
    posting = Job(source="lever", source_id="1", url="https://lever/1", title="QA Technician",
                  company="Acme", location="Toronto, ON", city="Toronto")
    first_seen = "2026-10-01T12:00:00+00:00"
    store.upsert(posting, first_seen)
    store.commit()
    monkeypatch.setattr("shigoto.db.now_iso", lambda: first_seen)
    ws = FakeWorksheet([HEADER])
    worksheet = cast(gspread.Worksheet, ws)
    assert sync(store, worksheet, SheetConfig()) == {"sheet_appended": 1, "sheet_updated": 0}
    ai_values = ["2026-10-02T12:00:00+00:00", "8", "Review notes"]
    ws.rows[1].extend(ai_values)

    now = "2026-10-04T12:00:00+00:00"
    store.record_liveness(ws.rows[1][0], "gone", now)
    assert sync(store, worksheet, SheetConfig()) == {"sheet_appended": 0, "sheet_updated": 1}
    assert ws.rows[1][APP_COLUMNS.index("Status")] == "Closed"
    assert ws.rows[1][APP_COLUMNS.index("Updated At")] == first_seen
    assert ws.rows[1][AI_REVIEWED_COL:] == ai_values
    store.upsert(posting, now)
    store.commit()
    assert sync(store, worksheet, SheetConfig()) == {"sheet_appended": 0, "sheet_updated": 1}
    assert ws.rows[1][APP_COLUMNS.index("Status")] == ""
    assert ws.rows[1][AI_REVIEWED_COL:] == ai_values


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
    assert ws.rows == [HEADER]
    assert ws.frozen_rows == 1
    assert ws.formatted_ranges == ["1:1", "A:Q"]


@pytest.mark.parametrize("header", [APP_COLUMNS, OLD_HEADER])
def test_renamed_app_header_raises_without_writes(tmp_path: Path, header: list[str]) -> None:
    rows = [header.copy(), ["job", "untouched", *([""] * 11), "reviewed", "8", "notes"]]
    rows[0][5] = "Posting date"
    ws = FakeWorksheet(rows)
    store = Store(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="Unrecognized sheet header"):
        sync(store, cast(gspread.Worksheet, ws), SheetConfig())
    assert ws.rows == rows
    assert ws.inserted_cols == []
    assert ws.updated_ranges == []


@pytest.mark.parametrize("header", [[], ["", "  "], APP_COLUMNS + ["Reviewed", "Score", "Notes"], APP_COLUMNS])
def test_blank_or_current_headers(tmp_path: Path, header: list[str]) -> None:
    ws = FakeWorksheet([header])
    assert sync(Store(tmp_path / "t.db"), cast(gspread.Worksheet, ws), SheetConfig()) == {
        "sheet_appended": 0, "sheet_updated": 0,
    }
    assert ws.rows[0] == (header if header[:len(APP_COLUMNS)] == APP_COLUMNS else HEADER)
    assert ws.inserted_cols == []


def test_migration_twice_is_no_op(tmp_path: Path) -> None:
    ws = FakeWorksheet([OLD_HEADER, ["job", *([""] * 12), "reviewed", "8", "notes"]])
    store = Store(tmp_path / "t.db")
    worksheet = cast(gspread.Worksheet, ws)
    sync(store, worksheet, SheetConfig())
    rows = ws.get_all_values()
    sync(store, worksheet, SheetConfig())
    assert ws.rows == rows
    assert ws.inserted_cols == [13]
    assert ws.header_updates == 1
