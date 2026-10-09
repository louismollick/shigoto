"""Back up reviewer-owned columns, then rebuild the dedicated worksheet from SQLite."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from itertools import groupby
from pathlib import Path

import gspread
from gspread.utils import ValueInputOption, rowcol_to_a1

from shigoto.config import Config, SheetConfig
from shigoto.db import StoredJob, Store, now_iso

log = logging.getLogger(__name__)

APP_COLUMNS = [
    "Job ID", "Title", "Company", "City", "Location", "Posted", "Salary", "Type", "URL", "Sources",
    "First Seen", "Updated At", "Status", "Description",
]
AI_COLUMNS = ["AI Reviewed At", "AI Score", "AI Notes", "AI Decision", "AI Category"]
HEADER = APP_COLUMNS + AI_COLUMNS
CHUNK = 200


def job_row(job: StoredJob, description_max: int) -> list[str]:
    description = job.description
    if len(description) > description_max:
        description = description[:description_max] + " [truncated]"
    return [
        job.job_id, job.title, job.company, job.city, job.location, job.posted_date, job.salary,
        job.job_type, job.url, job.sources, job.first_seen, job.updated_at, job.status, description,
    ]


def reviewer_value(name: str, value: str) -> str | int:
    """Keep raw AI scores numeric for sorting; other AI values stay literal text."""
    if name == "AI Score":
        try:
            number = Decimal(value)
            if number.is_finite() and number == number.to_integral_value():
                return int(number)
        except InvalidOperation:
            return value
    return value or ("Not Applied" if name == "Application Status" else "")


def open_worksheet(credentials: Path, config: SheetConfig, *, create: bool = True) -> gspread.Worksheet:
    client = gspread.service_account(filename=str(credentials))
    spreadsheet = client.open_by_key(config.spreadsheet_id)
    # An explicit ID survives renames and must never fall back to another tab.
    if config.worksheet_id is not None:
        return spreadsheet.get_worksheet_by_id(config.worksheet_id)
    try:
        return spreadsheet.worksheet(config.worksheet)
    except gspread.WorksheetNotFound:
        if not create:
            raise
        ws = spreadsheet.add_worksheet(config.worksheet, rows=1000, cols=len(HEADER))
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.format("A:Q", {"wrapStrategy": "CLIP"})
        log.info("created worksheet %r", config.worksheet)
        return ws


def sync(store: Store, ws: gspread.Worksheet, config: Config) -> dict[str, int]:
    rows = ws.get_all_values()
    header = rows[0] if rows else []
    if not any(cell.strip() for cell in header):
        if any(any(cell for cell in row) for row in rows[1:]):
            raise RuntimeError("Unrecognized sheet header: data exists without app columns")
        stored_columns = store.reviewer_columns()
        columns = stored_columns if stored_columns is not None else AI_COLUMNS.copy()
    else:
        if header[:len(APP_COLUMNS)] != APP_COLUMNS:
            raise RuntimeError("Unrecognized sheet header: app columns must match APP_COLUMNS")
        # Include cells beyond a short header so values under blank headers survive.
        width = max(map(len, rows))
        columns = [
            header[i] if i < len(header) and header[i].strip()
            else "Column " + rowcol_to_a1(1, i + 1).rstrip("1")
            for i in range(len(APP_COLUMNS), width)
        ]
    if len(set(columns)) != len(columns):
        raise RuntimeError("Duplicate reviewer headers cannot be backed up by name")

    reviews: dict[str, dict[str, str]] = {}
    if store.sheet_rebuild_pending():
        columns = store.reviewer_columns() or []
        log.warning("resuming interrupted sheet rebuild from committed reviews; skipping sheet backup")
    else:
        for row in rows[1:]:
            if not row or not row[0]:
                continue
            if row[0] in reviews:
                raise RuntimeError(f"Duplicate sheet Job ID: {row[0]}")
            reviews[row[0]] = {name: row[i] if i < len(row) else ""
                              for i, name in enumerate(columns, start=len(APP_COLUMNS))}
        unknown = store.backup_reviews(columns, reviews, now_iso())
        if unknown:
            log.warning("backed up reviews for %d sheet jobs absent from SQLite; these rows will be removed", unknown)
    hidden = store.reevaluate_exclusions(config, now_iso())
    jobs = store.visible_jobs()
    backed_up = store.reviews()
    row_count, col_count = len(jobs) + 1, len(APP_COLUMNS) + len(columns)
    # Grow first; shrinking after successful writes removes leftover cells below/right.
    if ws.row_count < row_count or ws.col_count < col_count:
        ws.resize(rows=max(ws.row_count, row_count), cols=max(ws.col_count, col_count))
    ws.update([APP_COLUMNS + columns], "A1", value_input_option=ValueInputOption.raw)
    for i in range(0, len(jobs), CHUNK):
        chunk = jobs[i:i + CHUNK]
        start, end = i + 2, i + len(chunk) + 1
        ws.update(
            [job_row(job, config.sheet.description_max_chars) for job in chunk],
            f"A{start}:{rowcol_to_a1(end, len(APP_COLUMNS))}", value_input_option=ValueInputOption.raw,
        )
        if columns:
            # AI strings must stay literal across rebuilds, including ISO offsets
            # and notes beginning with formula characters. Workflow dates retain
            # Sheets' user-entered parsing. Group adjacent columns to keep calls small.
            for raw, indices in groupby(range(len(columns)), key=lambda n: columns[n] in AI_COLUMNS):
                block = list(indices)
                names = [columns[n] for n in block]
                ws.update(
                    [[reviewer_value(name, backed_up.get(job.job_id, {}).get(name, ""))
                      for name in names] for job in chunk],
                    f"{rowcol_to_a1(start, len(APP_COLUMNS) + block[0] + 1)}:"
                    f"{rowcol_to_a1(end, len(APP_COLUMNS) + block[-1] + 1)}",
                    value_input_option=ValueInputOption.raw if raw else ValueInputOption.user_entered,
                )
    ws.resize(rows=row_count, cols=col_count)
    store.finish_sheet_rebuild()
    log.info("sheet rebuild: %d rows, %d hidden, %d reviews backed up", len(jobs), hidden, len(reviews))
    return {"sheet_rows": len(jobs), "sheet_hidden": hidden, "reviews_backed_up": len(reviews)}
