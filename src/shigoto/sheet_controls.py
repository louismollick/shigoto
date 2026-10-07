"""Native Sheets controls for the master tracker: dropdowns, date formats and the basic filter.

Only data validation, number formats and the basic filter change. Values, reviewer cells and all
other formatting are left alone. The sheet is addressed by its ID, so renaming it is safe.
"""

from __future__ import annotations

import gspread

STATUS_CHOICES = ["Not Applied", "Applied", "Skip"]
STAGE_CHOICES = ["Applied", "Screening", "Interview", "Offer", "Rejected", "Withdrawn"]
DATE_FORMATS = {"Applied At": ("DATE_TIME", "yyyy-mm-dd hh:mm"), "Follow-up Date": ("DATE", "yyyy-mm-dd")}
# Default filter for a sheet without one: hide these exact values; blanks stay visible.
HIDDEN_VALUES = {"Status": ["Closed"], "AI Decision": ["Reject"]}
SORT_COLUMN = "AI Score"


def configure_tracker(ws: gspread.Worksheet) -> None:
    """Reapply controls after each sync/rebuild. Skips any control whose header is missing.

    An existing basic filter keeps its criteria and sort; only its range grows to the full grid.
    """
    meta = ws.spreadsheet.fetch_sheet_metadata(
        {"fields": "sheets(properties(sheetId,gridProperties),basicFilter(range,filterSpecs,sortSpecs))"})
    sheet = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == ws.id)
    grid = sheet["properties"]["gridProperties"]
    rows, cols = grid["rowCount"], grid["columnCount"]
    columns = {name: i for i, name in enumerate(ws.row_values(1)) if name}

    def body(i: int) -> dict[str, int]:
        return {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": rows,
                "startColumnIndex": i, "endColumnIndex": i + 1}

    # filteredRowsIncluded: rows hidden by the filter (Closed, Reject) still get controls.
    requests: list[dict[str, object]] = []
    if rows > 1:
        for name, choices in (("Application Status", STATUS_CHOICES), ("Application Stage", STAGE_CHOICES)):
            if name in columns:
                requests.append({"setDataValidation": {"range": body(columns[name]), "rule": {
                    "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": c} for c in choices]},
                    "strict": True, "showCustomUi": True}, "filteredRowsIncluded": True}})
        for name, (kind, pattern) in DATE_FORMATS.items():
            if name in columns:
                # Date validation gives blank cells a date picker; strict=False keeps odd legacy values.
                requests.append({"setDataValidation": {"range": body(columns[name]), "rule": {
                    "condition": {"type": "DATE_IS_VALID"}, "strict": False}, "filteredRowsIncluded": True}})
                requests.append({"repeatCell": {
                    "range": body(columns[name]),
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat"}})

    # range is always set on a real filter, so an empty filter (no criteria, no sort) still
    # arrives as a non-empty object and keeps the user's choice instead of reverting to defaults.
    existing = sheet.get("basicFilter")
    if existing is not None:
        specs = [s for s in existing.get("filterSpecs", []) if s["columnIndex"] < cols]
        sort = [s for s in existing.get("sortSpecs", []) if s["dimensionIndex"] < cols]
    else:
        specs = [{"columnIndex": columns[name], "filterCriteria": {"hiddenValues": values}}
                 for name, values in HIDDEN_VALUES.items() if name in columns]
        sort = ([{"dimensionIndex": columns[SORT_COLUMN], "sortOrder": "DESCENDING"}]
                if SORT_COLUMN in columns else [])
    requests.append({"setBasicFilter": {"filter": {
        "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": rows,
                  "startColumnIndex": 0, "endColumnIndex": cols},
        "filterSpecs": specs, "sortSpecs": sort}}})
    if sort and rows > 1:
        # The filter's sortSpecs alone don't reorder rows the sync just wrote. Whole rows move,
        # so reviewer cells stay with their job.
        requests.append({"sortRange": {"range": {
            "sheetId": ws.id, "startRowIndex": 1, "endRowIndex": rows,
            "startColumnIndex": 0, "endColumnIndex": cols}, "sortSpecs": sort}})
    ws.spreadsheet.batch_update({"requests": requests})
