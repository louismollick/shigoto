"""Google Sheets sync: push new/changed jobs to one dedicated worksheet.

Only the configured worksheet (tab) is touched. Columns up to "Description" are owned by
the app and rewritten when a job materially changes; the "AI ..." columns belong to the
reviewer (ChatGPT) and are never written. "AI Reviewed At" is read back into SQLite.
A row needs (re)review when AI Reviewed At is blank or older than Updated At.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gspread
from gspread.utils import ValueInputOption, rowcol_to_a1

from shigoto.config import SheetConfig
from shigoto.db import StoredJob, Store, now_iso

log = logging.getLogger(__name__)

APP_COLUMNS = [
    "Job ID", "Title", "Company", "City", "Location", "Posted", "Salary", "Type", "URL", "Sources",
    "First Seen", "Updated At", "Description",
]
AI_COLUMNS = ["AI Reviewed At", "AI Score", "AI Notes"]
HEADER = APP_COLUMNS + AI_COLUMNS
AI_REVIEWED_COL = len(APP_COLUMNS)  # 0-based index of "AI Reviewed At"
CHUNK = 500


def job_row(job: StoredJob, description_max: int) -> list[str]:
    description = job.description
    if len(description) > description_max:
        description = description[:description_max] + " [truncated]"
    return [
        job.job_id, job.title, job.company, job.city, job.location, job.posted_date, job.salary,
        job.job_type, job.url, job.sources, job.first_seen, job.updated_at, description,
    ]


def open_worksheet(credentials: Path, config: SheetConfig) -> gspread.Worksheet:
    client = gspread.service_account(filename=str(credentials))
    spreadsheet = client.open_by_key(config.spreadsheet_id)
    try:
        return spreadsheet.worksheet(config.worksheet)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(config.worksheet, rows=1000, cols=len(HEADER))
        ws.update([HEADER], "A1", value_input_option=ValueInputOption.raw)
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.format("A:P", {"wrapStrategy": "CLIP"})
        log.info("created worksheet %r", config.worksheet)
        return ws


def sync(store: Store, ws: gspread.Worksheet, config: SheetConfig) -> dict[str, int]:
    rows = ws.get_all_values()
    if not rows or rows[0][: len(APP_COLUMNS)] != APP_COLUMNS:
        ws.update([HEADER], "A1", value_input_option=ValueInputOption.raw)
    row_of: dict[str, int] = {}
    reviewed: dict[str, str] = {}
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0]:
            row_of[row[0]] = i
            reviewed[row[0]] = row[AI_REVIEWED_COL] if len(row) > AI_REVIEWED_COL else ""
    store.set_ai_reviewed(reviewed)

    delta = store.unsynced()
    updates = [j for j in delta if j.job_id in row_of]
    appends = [j for j in delta if j.job_id not in row_of]
    last_col = rowcol_to_a1(1, len(APP_COLUMNS)).rstrip("1")
    for i in range(0, len(updates), CHUNK):
        chunk = updates[i : i + CHUNK]
        ws.batch_update(
            [{"range": f"A{row_of[j.job_id]}:{last_col}{row_of[j.job_id]}",
              "values": [job_row(j, config.description_max_chars)]} for j in chunk],
            value_input_option=ValueInputOption.raw,
        )
        store.mark_synced(chunk, now_iso())
    for i in range(0, len(appends), CHUNK):
        chunk = appends[i : i + CHUNK]
        ws.append_rows([job_row(j, config.description_max_chars) for j in chunk],
                       value_input_option=ValueInputOption.raw, table_range="A1")
        store.mark_synced(chunk, now_iso())
    log.info("sheet sync: %d appended, %d updated", len(appends), len(updates))
    return {"sheet_appended": len(appends), "sheet_updated": len(updates)}
