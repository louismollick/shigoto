from typing import cast

import gspread

from shigoto.sheet_controls import configure_tracker
from shigoto.sheets import APP_COLUMNS

REVIEWERS = ["AI Reviewed At", "AI Score", "AI Notes", "AI Decision", "AI Category", "Application Status",
             "Applied At", "Application Stage", "Follow-up Date", "User Notes"]
SHEET_ID = 589266369


class FakeSpreadsheet:
    """Serves sheet metadata and records batch_update bodies."""

    def __init__(self, rows: int, cols: int, basic_filter: dict[str, object] | None) -> None:
        self.sheet: dict[str, object] = {"properties": {
            "sheetId": SHEET_ID, "gridProperties": {"rowCount": rows, "columnCount": cols}}}
        if basic_filter is not None:
            self.sheet["basicFilter"] = basic_filter
        self.params: list[dict[str, str]] = []
        self.bodies: list[dict[str, list[dict[str, dict[str, object]]]]] = []

    def fetch_sheet_metadata(self, params: dict[str, str]) -> dict[str, object]:
        self.params.append(params)
        return {"sheets": [{"properties": {"sheetId": 1}}, self.sheet]}

    def batch_update(self, body: dict[str, list[dict[str, dict[str, object]]]]) -> None:
        self.bodies.append(body)


class FakeWorksheet:
    def __init__(self, header: list[str], spreadsheet: FakeSpreadsheet) -> None:
        self.id, self.header, self.spreadsheet = SHEET_ID, header, spreadsheet

    def row_values(self, row: int) -> list[str]:
        assert row == 1
        return self.header


def run(header: list[str], *, rows: int = 51, basic_filter: dict[str, object] | None = None
        ) -> list[dict[str, dict[str, object]]]:
    spreadsheet = FakeSpreadsheet(rows, len(header), basic_filter)
    configure_tracker(cast(gspread.Worksheet, FakeWorksheet(header, spreadsheet)))
    assert len(spreadsheet.bodies) == 1
    return spreadsheet.bodies[0]["requests"]


def basic_filter(requests: list[dict[str, dict[str, object]]]) -> object:
    return next(r["setBasicFilter"]["filter"] for r in requests if "setBasicFilter" in r)


def sort_range(requests: list[dict[str, dict[str, object]]]) -> list[dict[str, object]]:
    return [r["sortRange"] for r in requests if "sortRange" in r]


def test_fresh_sheet_gets_dropdowns_dates_and_default_filter() -> None:
    requests = run(APP_COLUMNS + REVIEWERS)
    status, stage = APP_COLUMNS.index("Status"), len(APP_COLUMNS)
    validations = {r["setDataValidation"]["range"]["startColumnIndex"]: r["setDataValidation"]["rule"]  # type: ignore[index]
                   for r in requests if "setDataValidation" in r}
    assert validations[stage + 5]["condition"]["values"] == [  # type: ignore[index]
        {"userEnteredValue": "Not Applied"}, {"userEnteredValue": "Applied"}, {"userEnteredValue": "Skip"}]
    assert validations[stage + 7]["condition"]["values"][-1] == {"userEnteredValue": "Withdrawn"}  # type: ignore[index]
    assert validations[stage + 6]["condition"] == {"type": "DATE_IS_VALID"}  # type: ignore[index]
    assert validations[stage + 8]["condition"] == {"type": "DATE_IS_VALID"}  # type: ignore[index]
    assert all(r["setDataValidation"]["filteredRowsIncluded"] is True for r in requests if "setDataValidation" in r)
    formats = [r["repeatCell"] for r in requests if "repeatCell" in r]
    assert [f["fields"] for f in formats] == ["userEnteredFormat.numberFormat"] * 2
    assert [f["range"]["startColumnIndex"] for f in formats] == [stage + 6, stage + 8]  # type: ignore[index]
    # Data rows only: the header row and other columns keep their formatting.
    assert all(f["range"]["startRowIndex"] == 1 and f["range"]["endRowIndex"] == 51 for f in formats)  # type: ignore[index]
    assert basic_filter(requests) == {
        "range": {"sheetId": SHEET_ID, "startRowIndex": 0, "endRowIndex": 51,
                  "startColumnIndex": 0, "endColumnIndex": 24},
        "filterSpecs": [{"columnIndex": status, "filterCriteria": {"hiddenValues": ["Closed"]}},
                        {"columnIndex": stage + 3, "filterCriteria": {"hiddenValues": ["Reject"]}}],
        "sortSpecs": [{"dimensionIndex": stage + 1, "sortOrder": "DESCENDING"}],
    }
    # Data rows across the full A:X width are physically sorted, so reviews move with their row.
    assert sort_range(requests) == [{
        "range": {"sheetId": SHEET_ID, "startRowIndex": 1, "endRowIndex": 51,
                  "startColumnIndex": 0, "endColumnIndex": 24},
        "sortSpecs": [{"dimensionIndex": stage + 1, "sortOrder": "DESCENDING"}],
    }]


def test_existing_filter_is_preserved_and_extended_to_grown_grid() -> None:
    user_filter: dict[str, object] = {
        "filterSpecs": [{"columnIndex": 2, "filterCriteria": {"condition": {
            "type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Pharma"}]}}}],
        "sortSpecs": [{"dimensionIndex": 5, "sortOrder": "ASCENDING"}],
    }
    requests = run(APP_COLUMNS + REVIEWERS, rows=301, basic_filter=user_filter)
    assert basic_filter(requests) == {
        "range": {"sheetId": SHEET_ID, "startRowIndex": 0, "endRowIndex": 301,
                  "startColumnIndex": 0, "endColumnIndex": 24},
        **user_filter,
    }
    assert sort_range(requests)[0]["sortSpecs"] == user_filter["sortSpecs"]


def test_existing_filter_drops_specs_beyond_shrunk_grid() -> None:
    user_filter: dict[str, object] = {
        "filterSpecs": [{"columnIndex": 12, "filterCriteria": {"hiddenValues": ["Closed"]}},
                        {"columnIndex": 20, "filterCriteria": {"hiddenValues": ["Skip"]}}],
        "sortSpecs": [{"dimensionIndex": 15, "sortOrder": "DESCENDING"}],
    }
    found = basic_filter(run(APP_COLUMNS, basic_filter=user_filter))
    assert found["filterSpecs"] == [user_filter["filterSpecs"][0]]  # type: ignore[index]
    assert found["sortSpecs"] == []  # type: ignore[index]
    assert sort_range(run(APP_COLUMNS, basic_filter=user_filter)) == []


def test_existing_empty_filter_stays_empty() -> None:
    # A filter the user cleared of all criteria and sort must not get the defaults back.
    empty: dict[str, object] = {"range": {"sheetId": SHEET_ID, "startRowIndex": 0, "endRowIndex": 51,
                                          "startColumnIndex": 0, "endColumnIndex": 24}}
    spreadsheet = FakeSpreadsheet(51, 24, empty)
    configure_tracker(cast(gspread.Worksheet, FakeWorksheet(APP_COLUMNS + REVIEWERS, spreadsheet)))
    assert "basicFilter(range," in spreadsheet.params[0]["fields"]
    requests = spreadsheet.bodies[0]["requests"]
    found = basic_filter(requests)
    assert found["filterSpecs"] == [] and found["sortSpecs"] == []  # type: ignore[index]
    assert sort_range(requests) == []


def test_missing_reviewer_headers_only_filter_status() -> None:
    requests = run(APP_COLUMNS + ["AI Reviewed At", "AI Score", "AI Notes"])
    assert [list(r) for r in requests] == [["setBasicFilter"], ["sortRange"]]
    found = basic_filter(requests)
    assert found["filterSpecs"] == [  # type: ignore[index]
        {"columnIndex": APP_COLUMNS.index("Status"), "filterCriteria": {"hiddenValues": ["Closed"]}}]
    assert found["sortSpecs"] == [{"dimensionIndex": 15, "sortOrder": "DESCENDING"}]  # type: ignore[index]


def test_header_only_sheet_sets_filter_without_cell_controls() -> None:
    requests = run(APP_COLUMNS + REVIEWERS, rows=1)
    assert [list(r) for r in requests] == [["setBasicFilter"]]
