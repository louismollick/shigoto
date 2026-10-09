"""Codex judges job fit; Python selects, validates, stores and publishes reviews.

The sheet is read in full without changing its controls. Cached judgments are
keyed by all supplied job inputs and the prompt/model version, not row numbers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Self, TypeVar
from zoneinfo import ZoneInfo

import gspread
from gspread.exceptions import APIError
from gspread.utils import ValueInputOption, ValueRenderOption, rowcol_to_a1
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from requests.exceptions import RequestException

from shigoto.config import Config, ReviewConfig
from shigoto.db import Store
from shigoto.normalize import norm_company, norm_title
from shigoto.sheets import AI_COLUMNS, open_worksheet

log = logging.getLogger(__name__)
INPUT_COLUMNS = ["Job ID", "Title", "Company", "Location", "Salary", "Type", "Description", "Updated At", "Status"]
PROMPT = files("shigoto").joinpath("review_prompt.md").read_text()
POLICY_VERSION = "1"
PAY_TARGET = 55000
WITHHELD = "Notes withheld: write blocked."
Category = Literal["Food QA", "Microbiology Lab", "QC Lab", "Biotech/Pharma", "Environmental Lab",
                   "Regulatory/Food Safety", "R&D/Product Development", "Other"]
Decision = Literal["Recommend", "Maybe", "Reject"]
BlockerCode = Literal["french_essential", "missing_mandatory_cert", "software_qa", "management_senior",
                      "essential_4plus_years", "phd_required", "unrelated"]
ReadResult = TypeVar("ReadResult")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Blocker(StrictModel):
    code: BlockerCode
    evidence: str = Field(min_length=1, max_length=1000)


class Pay(StrictModel):
    minimum: float | None = Field(ge=0, allow_inf_nan=False)
    maximum: float | None = Field(ge=0, allow_inf_nan=False)
    currency: str | None
    unit: Literal["hourly", "annual", "unknown"]
    full_time: bool

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("Pay minimum and maximum must both be known or both be null")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Pay minimum exceeds maximum")
        return self


class Judgment(StrictModel):
    canonical_job_id: str = Field(min_length=1)
    duplicate_job_ids: list[str]
    base_score: int = Field(ge=0, le=100)
    blockers: list[Blocker]
    category: Category
    notes: str = Field(min_length=1, max_length=1200)
    pay: Pay


class AIFields(StrictModel):
    reviewed_at: str
    score: int = Field(ge=0, le=100)
    notes: str
    decision: Decision
    category: Category

    def cells(self) -> dict[str, str | int]:
        return {"AI Reviewed At": self.reviewed_at, "AI Score": self.score, "AI Notes": self.notes,
                "AI Decision": self.decision, "AI Category": self.category}


class Failure(StrictModel):
    job_id: str
    error: str
    original_notes: str | None = None
    retry_succeeded: bool | None = None
    retry_error: str | None = None


class ReviewReport(BaseModel):
    reviewed: int = 0
    recommend: int = 0
    maybe: int = 0
    reject: int = 0
    pending: int = 0
    failures: list[Failure] = Field(default_factory=list)
    # Populated only for dry runs so proposed scores/notes can be inspected.
    previews: dict[str, AIFields] = Field(default_factory=dict)

    def stats(self) -> dict[str, int]:
        return {"ai_reviewed": self.reviewed, "ai_recommend": self.recommend, "ai_maybe": self.maybe,
                "ai_reject": self.reject, "ai_pending": self.pending, "ai_errors": len(self.failures)}


@dataclass(frozen=True)
class ReviewJob:
    values: dict[str, str]
    updated_at: datetime

    @property
    def job_id(self) -> str:
        return self.values["Job ID"]

    @property
    def limited(self) -> bool:
        description = self.values["Description"].strip()
        return not description or description.endswith("[truncated]")


class CodexUnavailable(RuntimeError):
    """Stop further calls this run on login, usage, process or timeout failures."""


class ReviewSheet:
    """Pace reads below the service-account quota and retry transient read failures.

    The write path retains its own one-row retry/reporting rules. Read pacing also
    spaces those writes, so a cached 40-job run cannot burst through Sheets quotas.
    """

    def __init__(self, worksheet: gspread.Worksheet) -> None:
        self.worksheet = worksheet
        self.next_read = 0.0

    def read(self, fetch: Callable[[], ReadResult]) -> ReadResult:
        for attempt in range(3):
            delay = self.next_read - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_read = time.monotonic() + 1.2  # 50 reads/minute; leave room for sync/open calls.
            try:
                return fetch()
            except (APIError, RequestException) as error:
                if isinstance(error, APIError) and error.code != 429 and error.code < 500:
                    raise
                if attempt == 2:
                    raise
                time.sleep(60 if isinstance(error, APIError) and error.code == 429 else 2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def get_all_values(self) -> list[list[str]]:
        return self.read(self.worksheet.get_all_values)

    def row_values(self, row: int, *, unformatted: bool = False) -> list[str]:
        option = ValueRenderOption.unformatted if unformatted else ValueRenderOption.formatted
        return [str(value) for value in self.read(lambda: self.worksheet.row_values(row, value_render_option=option))]

    def col_values(self, column: int) -> list[str]:
        return [str(value) if value is not None else ""
                for value in self.read(lambda: self.worksheet.col_values(column))]


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Timestamp has no timezone offset: {value!r}")
    return parsed


def read_jobs(rows: list[list[str]], report: ReviewReport) -> tuple[dict[str, int], list[ReviewJob]]:
    """Resolve headers and read all underlying rows, including filtered ones."""
    header = rows[0] if rows else []
    required = INPUT_COLUMNS + AI_COLUMNS
    for name in required:
        if header.count(name) != 1:
            raise RuntimeError(f"Expected exactly one sheet header {name!r}")
    columns = {name: index for index, name in enumerate(header) if name}
    jobs: list[ReviewJob] = []
    ids: set[str] = set()
    for row in rows[1:]:
        values = {name: row[index] if index < len(row) else "" for name, index in columns.items()}
        job_id = values["Job ID"]
        if not job_id:
            continue
        if job_id in ids:
            raise RuntimeError(f"Duplicate sheet Job ID: {job_id}")
        ids.add(job_id)
        if values["Status"] == "Closed":
            continue
        try:
            updated = timestamp(values["Updated At"])
        except ValueError as error:
            report.failures.append(Failure(job_id=job_id, error=str(error)))
            continue
        jobs.append(ReviewJob(values, updated))
    return columns, jobs


def job_input(job: ReviewJob) -> dict[str, str]:
    return {name: job.values.get(name, "") for name in [*INPUT_COLUMNS, "City"]}


def group_key(job: ReviewJob) -> tuple[str, str, str]:
    return (norm_company(job.values["Company"]), norm_title(job.values["Title"]),
            (job.values.get("City") or job.values["Location"]).casefold().strip())


def policy_hash(config: ReviewConfig) -> str:
    return hashlib.sha256(json.dumps([POLICY_VERSION, PROMPT, config.model, config.reasoning_effort]).encode()).hexdigest()


def input_hash(group: list[ReviewJob]) -> str:
    return hashlib.sha256(json.dumps([job_input(j) for j in sorted(group, key=lambda j: j.job_id)],
                                    sort_keys=True).encode()).hexdigest()


def needs_review(store: Store, job: ReviewJob, fingerprint: str, policy: str) -> bool:
    try:
        if timestamp(job.values["AI Reviewed At"]) < job.updated_at:
            return True
    except ValueError:
        return True
    # The first Codex run re-reviews legacy scores once. Prompt/model changes reset
    # that version without a future cutoff that repeatedly selects the same rows.
    row = store.conn.execute("SELECT written_at FROM ai_results WHERE job_id=? AND fingerprint=? AND policy=?",
                             (job.job_id, fingerprint, policy)).fetchone()
    return row is None or row["written_at"] is None


def run_codex(focus: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
    """A fresh, tool-free CLI invocation using a dedicated saved ChatGPT login."""
    with TemporaryDirectory(prefix="shigoto-review-") as directory:
        work = Path(directory)
        schema, output = work / "schema.json", work / "result.json"
        schema.write_text(json.dumps(Judgment.model_json_schema()))
        prompt = PROMPT + "\n\nJOB DATA\n" + json.dumps({
            "focus_job_id": focus.job_id, "jobs": [job_input(job) for job in group],
        }, ensure_ascii=False)
        env = {name: value for name, value in os.environ.items()
               if name in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE"}}
        # No Sheets credentials, provider overrides or T3 MCP injection are inherited.
        env.update(HOME=directory, CODEX_HOME=str(config.codex_home.resolve()))
        command = [config.codex_bin, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--output-schema", str(schema), "-o", str(output),
                   "--model", config.model]
        overrides = ["forced_login_method=\"chatgpt\"", "cli_auth_credentials_store=\"file\"",
                     "approval_policy=\"never\"", "features.shell_tool=false", "features.unified_exec=false",
                     "features.apps=false", "agents.enabled=false", "web_search=\"disabled\"",
                     "project_doc_max_bytes=0", f"model_reasoning_effort=\"{config.reasoning_effort}\""]
        for override in overrides:
            command.extend(["-c", override])
        command.append("-")
        try:
            completed = subprocess.run(command, input=prompt, text=True, cwd=work, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       timeout=config.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CodexUnavailable(str(error)) from error
        if completed.returncode:
            raise CodexUnavailable(f"Codex exited {completed.returncode}: {completed.stderr.strip()}")
        if not output.exists():
            raise ValueError("Codex produced no structured review")
        return Judgment.model_validate_json(output.read_text())


def validate_references(result: Judgment, focus: ReviewJob, group: list[ReviewJob]) -> None:
    ids = {job.job_id for job in group}
    duplicates = result.duplicate_job_ids
    if result.canonical_job_id not in ids or not set(duplicates) <= ids:
        raise ValueError("Codex referenced an unknown Job ID")
    if result.canonical_job_id in duplicates or len(duplicates) != len(set(duplicates)):
        raise ValueError("Invalid duplicate references")
    if focus.job_id not in {result.canonical_job_id, *duplicates}:
        raise ValueError("Codex did not review the requested Job ID")


def finalize(result: Judgment, target: ReviewJob, canonical: ReviewJob) -> AIFields:
    """Apply arithmetic and fixed decision rules, never keyword scoring."""
    pay = result.pay
    annual_min, annual_max = pay.minimum, pay.maximum
    if pay.currency != "CAD" or pay.unit == "unknown" or (pay.unit == "hourly" and not pay.full_time):
        annual_min = annual_max = None
    elif pay.unit == "hourly" and annual_min is not None and annual_max is not None:
        annual_min *= 2080
        annual_max *= 2080
    score = result.base_score
    notes = result.notes.strip()
    details: list[str] = []
    if not notes:
        raise ValueError("Codex notes are blank")
    if annual_max is not None and annual_max < PAY_TARGET:
        penalty = round(6 + 4 * min(1, (PAY_TARGET - annual_max) / 10000))
        score = max(55 if score >= 55 else 0, score - penalty)
        details.append(f"known pay is CAD ${annual_max:,.0f}/year, below the $55,000 preference")
    elif annual_min is not None and annual_min < PAY_TARGET:
        details.append("the pay range starts below the $55,000 preference")
    if result.blockers:
        score = min(score, 29)
    decision: Decision = "Recommend" if score >= 75 else "Maybe" if score >= 55 else "Reject"
    category = result.category
    if any(blocker.code in {"unrelated", "software_qa"} for blocker in result.blockers):
        category = "Other"
    if target.limited or canonical.limited:
        details.append("description information was limited because it was blank or truncated")
    if details:
        sentence = "; ".join(details)
        notes += " " + sentence[0].upper() + sentence[1:] + "."
    if target.job_id != canonical.job_id:
        notes = f"Duplicate of {canonical.job_id}. " + notes
    return AIFields(reviewed_at=datetime.now(ZoneInfo("America/Winnipeg")).isoformat(timespec="seconds"),
                    score=score, notes=notes, decision=decision, category=category)


def save_result(store: Store, job: ReviewJob, fingerprint: str, policy: str,
                result: Judgment, fields: AIFields) -> None:
    store.conn.execute("""INSERT INTO ai_results (job_id, fingerprint, policy, result, fields)
        VALUES (?, ?, ?, ?, ?) ON CONFLICT (job_id, fingerprint, policy)
        DO UPDATE SET result=excluded.result, fields=excluded.fields, written_at=NULL, error=NULL""",
        (job.job_id, fingerprint, policy, result.model_dump_json(), fields.model_dump_json()))
    store.commit()


def locate_job(ws: ReviewSheet, job: ReviewJob) -> tuple[dict[str, int], int]:
    """Refresh headers and IDs for every row; reject changed or closed inputs."""
    columns = {name: index for index, name in enumerate(ws.row_values(1)) if name}
    for name in INPUT_COLUMNS + AI_COLUMNS:
        if name not in columns:
            raise RuntimeError(f"Sheet header disappeared: {name}")
    matches = [index for index, value in enumerate(ws.col_values(columns["Job ID"] + 1), start=1)
               if index > 1 and value == job.job_id]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one current row for Job ID {job.job_id}")
    row_number = matches[0]
    row = ws.row_values(row_number)
    for name, original in job_input(job).items():
        index = columns.get(name)
        current = row[index] if index is not None and index < len(row) else ""
        if current != original:
            raise RuntimeError(f"Job {job.job_id} changed during review: {name}")
    return columns, row_number


def write_fields(ws: ReviewSheet, job: ReviewJob, fields: AIFields) -> None:
    columns, row_number = locate_job(ws, job)
    ws.worksheet.batch_update([{"range": rowcol_to_a1(row_number, columns[name] + 1), "values": [[value]]}
                               for name, value in fields.cells().items()], value_input_option=ValueInputOption.raw)
    # Refresh identity again so normal re-sorts do not make verification use old rows.
    columns, row_number = locate_job(ws, job)
    row = ws.row_values(row_number, unformatted=True)
    for name, value in fields.cells().items():
        index = columns[name]
        matches = index < len(row) and row[index] == str(value)
        if name == "AI Score" and index < len(row):
            try:
                matches = float(row[index]) == value
            except ValueError:
                matches = False
        if not matches:
            raise RuntimeError(f"Review write could not be verified for {job.job_id}: {name}")


def review_rows(store: Store, ws: gspread.Worksheet, config: ReviewConfig, *, dry_run: bool = False,
                job_id: str | None = None, limit: int | None = None,
                judge: Callable[[ReviewJob, list[ReviewJob], ReviewConfig], Judgment] = run_codex) -> ReviewReport:
    """Prepare all judgments before publishing each selected row with its own call."""
    if store.sheet_rebuild_pending():
        raise RuntimeError("Finish the interrupted Sheet sync before reviewing jobs")
    report = ReviewReport()
    sheet = ReviewSheet(ws)
    _, jobs = read_jobs(sheet.get_all_values(), report)
    groups: dict[tuple[str, str, str], list[ReviewJob]] = {}
    for job in jobs:
        groups.setdefault(group_key(job), []).append(job)
    policy = policy_hash(config)
    fingerprints = {job.job_id: input_hash(groups[group_key(job)]) for job in jobs}
    eligible = [job for job in jobs if needs_review(store, job, fingerprints[job.job_id], policy)]
    report.pending = len(eligible) + len(report.failures)
    selected = sorted((job for job in eligible if job_id is None or job.job_id == job_id),
                      key=lambda job: (job.updated_at, job.job_id), reverse=True)
    selected = selected[:min(config.max_per_run, limit if limit is not None else config.max_per_run)]
    if job_id is not None and not any(job.job_id == job_id for job in jobs):
        raise ValueError(f"Open Job ID not found: {job_id}")
    prepared: dict[str, AIFields] = {}
    by_id = {job.job_id: job for job in jobs}
    selected_ids = {job.job_id for job in selected}
    for job in selected:
        if job.job_id in prepared:
            continue
        fingerprint = fingerprints[job.job_id]
        group = groups[group_key(job)]
        try:
            cached = store.conn.execute("SELECT result FROM ai_results WHERE job_id=? AND fingerprint=? AND policy=?",
                                        (job.job_id, fingerprint, policy)).fetchone()
            result = Judgment.model_validate_json(cached["result"]) if cached else judge(job, group, config)
            validate_references(result, job, group)
            # Never overwrite a member of an already confirmed group with a
            # conflicting canonical. Leave this job pending for another review.
            for member_id in {result.canonical_job_id, *result.duplicate_job_ids}:
                member = store.conn.execute("SELECT result FROM ai_results WHERE job_id=? AND fingerprint=? AND policy=?",
                                            (member_id, fingerprint, policy)).fetchone()
                if member and Judgment.model_validate_json(member["result"]).canonical_job_id != result.canonical_job_id:
                    raise ValueError(f"Conflicting duplicate canonical for Job ID {member_id}")
            # A newly confirmed duplicate inherits an existing canonical judgment,
            # including when its canonical was published in an earlier capped run.
            known = store.conn.execute("SELECT result FROM ai_results WHERE job_id=? AND fingerprint=? AND policy=?",
                                       (result.canonical_job_id, fingerprint, policy)).fetchone()
            if known and not cached:
                prior = Judgment.model_validate_json(known["result"])
                members = {result.canonical_job_id, *result.duplicate_job_ids, *prior.duplicate_job_ids}
                result = prior.model_copy(update={"duplicate_job_ids": sorted(members - {prior.canonical_job_id})})
                validate_references(result, job, group)
            canonical = by_id[result.canonical_job_id]
            for target_id in {result.canonical_job_id, *result.duplicate_job_ids}:
                target = by_id[target_id]
                fields = finalize(result, target, canonical)
                if target_id in selected_ids:
                    prepared[target_id] = fields
                if not dry_run:
                    existing = store.conn.execute("SELECT result FROM ai_results WHERE job_id=? AND fingerprint=? AND policy=?",
                                                  (target_id, fingerprints[target_id], policy)).fetchone()
                    if target_id not in selected_ids and existing and existing["result"] == result.model_dump_json():
                        continue
                    save_result(store, target, fingerprints[target_id], policy, result, fields)
        except CodexUnavailable as error:
            report.failures.append(Failure(job_id=job.job_id, error=str(error)))
            break  # Authentication/usage failures must not burn another 39 calls.
        except (ValueError, ValidationError) as error:
            report.failures.append(Failure(job_id=job.job_id, error=str(error)))
    if dry_run:
        report.previews = prepared
        return report
    # Reread the complete ID column before publication as well as before each call.
    header = sheet.row_values(1)
    sheet.col_values(header.index("Job ID") + 1)
    decisions: Counter[str] = Counter()
    for job in selected:
        ready = prepared.get(job.job_id)
        if ready is None:
            continue
        fields = ready
        failure: Failure | None = None
        try:
            write_fields(sheet, job, fields)
        except (APIError, RequestException) as error:
            failure = Failure(job_id=job.job_id, error=str(error), original_notes=fields.notes, retry_succeeded=False)
            report.failures.append(failure)
            # Retain notes for transient transport/quota failures. A rejected payload
            # gets exactly the legacy one-row fallback; all other values stay fixed.
            if isinstance(error, APIError) and error.code not in {429} and error.code < 500:
                fields = fields.model_copy(update={"notes": WITHHELD})
            try:
                if isinstance(error, APIError) and error.code == 429:
                    time.sleep(60)
                write_fields(sheet, job, fields)
                failure.retry_succeeded = True
            except (APIError, RequestException, RuntimeError) as retry_error:
                failure.retry_error = str(retry_error)
                store.conn.execute("UPDATE ai_results SET error=? WHERE job_id=? AND fingerprint=? AND policy=?",
                                   (failure.model_dump_json(), job.job_id, fingerprints[job.job_id], policy))
                store.commit()
                continue
        except RuntimeError as error:
            report.failures.append(Failure(job_id=job.job_id, error=str(error), original_notes=fields.notes))
            break  # A changed identity or an uncertain write needs inspection.
        store.conn.execute("UPDATE ai_results SET fields=?, written_at=?, error=? "
                           "WHERE job_id=? AND fingerprint=? AND policy=?",
                           (fields.model_dump_json(), fields.reviewed_at, failure.error if failure else None,
                            job.job_id, fingerprints[job.job_id], policy))
        store.commit()
        decisions[fields.decision] += 1
    report.reviewed = sum(decisions.values())
    report.recommend, report.maybe, report.reject = (decisions[name] for name in ("Recommend", "Maybe", "Reject"))
    # Report the live backlog after writes, so changed/added rows remain visible.
    remaining = ReviewReport()
    try:
        _, current = read_jobs(sheet.get_all_values(), remaining)
    except (APIError, RequestException, RuntimeError) as error:
        report.pending = max(0, report.pending - report.reviewed)
        report.failures.append(Failure(job_id="", error=f"Backlog refresh failed: {error}"))
        return report
    current_groups: dict[tuple[str, str, str], list[ReviewJob]] = {}
    for job in current:
        current_groups.setdefault(group_key(job), []).append(job)
    report.pending = sum(needs_review(store, job, input_hash(current_groups[group_key(job)]), policy)
                         for job in current) + len(remaining.failures)
    return report


def review(config: Config, *, dry_run: bool = False, job_id: str | None = None,
           limit: int | None = None) -> ReviewReport:
    """Manual review never creates, rebuilds, resizes or configures a worksheet."""
    if config.google_credentials is None or not config.sheet.spreadsheet_id:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS and SHIGOTO_SPREADSHEET_ID must be set")
    if limit is not None and not 1 <= limit <= 40:
        raise ValueError("Review limit must be between 1 and 40")
    ws = open_worksheet(config.google_credentials, config.sheet, create=False)
    if ws.title in {"Applied", "Closed"}:
        raise RuntimeError("Cannot review a formula-view worksheet")
    store = Store(config.db_path)
    try:
        return review_rows(store, ws, config.review, dry_run=dry_run, job_id=job_id, limit=limit)
    finally:
        store.close()
