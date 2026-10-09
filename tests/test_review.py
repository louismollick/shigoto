import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import gspread
import pytest
from gspread.exceptions import APIError
from gspread.utils import ValueInputOption, ValueRenderOption, a1_to_rowcol
from pydantic import ValidationError
from requests import Response

from shigoto.config import ReviewConfig
from shigoto.db import Store
from shigoto.review import (
    Blocker, CodexUnavailable, Judgment, Pay, ReviewJob, ReviewReport, ReviewSheet, WITHHELD,
    finalize, input_hash, needs_review, policy_hash, read_jobs, review_rows, run_codex, timestamp,
)
from shigoto.sheets import AI_COLUMNS

HEADERS = ["User Notes", "AI Score", "Title", "Job ID", "Company", "Location", "Salary", "Type",
           "Description", "Updated At", "Status", "AI Notes", "AI Reviewed At", "AI Category", "AI Decision"]


@pytest.fixture(autouse=True)
def skip_real_quota_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def record(job_id: str = "one", **changes: str) -> dict[str, str]:
    values = {name: "" for name in HEADERS}
    values.update({"Job ID": job_id, "Title": "QA Technician", "Company": "Saputo",
                   "Location": "Toronto", "Salary": "$60,000 CAD/year", "Type": "Full-time",
                   "Description": "Test food samples and maintain QC records.",
                   "Updated At": "2026-10-08T10:00:00-05:00", "User Notes": "Never change this"})
    values.update(changes)
    return values


def judgment(job_id: str = "one", **changes: object) -> Judgment:
    data: dict[str, object] = {"canonical_job_id": job_id, "duplicate_job_ids": [], "base_score": 89,
                              "blockers": [], "category": "Food QA",
                              "notes": "Food sampling and QC records fit her Saputo role.",
                              "pay": Pay(minimum=60000, maximum=60000, currency="CAD", unit="annual", full_time=True)}
    data.update(changes)
    return Judgment.model_validate(data)


def api_error(code: int = 400) -> APIError:
    response = Response()
    response.status_code = code
    response._content = json.dumps({"error": {"code": code, "message": "write blocked", "status": "FAILED"}}).encode()
    return APIError(response)


class Worksheet:
    title = "Jobs to Review"

    def __init__(self, records: list[dict[str, str]], header: list[str] | None = None) -> None:
        self.rows = [header or HEADERS]
        self.rows += [[record.get(name, "") for name in self.rows[0]] for record in records]
        self.writes: list[list[dict[str, object]]] = []
        self.errors: list[Exception] = []

    def get_all_values(self) -> list[list[str]]:
        return [row.copy() for row in self.rows]

    def row_values(self, row: int, *, value_render_option: ValueRenderOption = ValueRenderOption.formatted) -> list[str]:
        return self.rows[row - 1].copy()

    def col_values(self, column: int) -> list[str]:
        return [row[column - 1] if column <= len(row) else "" for row in self.rows]

    def batch_update(self, data: list[dict[str, object]], *, value_input_option: ValueInputOption) -> None:
        assert value_input_option == ValueInputOption.raw
        self.writes.append(data)
        if self.errors:
            raise self.errors.pop(0)
        for update in data:
            row, column = a1_to_rowcol(str(update["range"]))
            value = cast(list[list[str | int]], update["values"])[0][0]
            self.rows[row - 1][column - 1] = str(value)


def run(store: Store, sheet: Worksheet, config: ReviewConfig | None = None, *, dry_run: bool = False,
        judge: Callable[[ReviewJob, list[ReviewJob], ReviewConfig], Judgment] = run_codex) -> ReviewReport:
    return review_rows(store, cast(gspread.Worksheet, sheet), config or ReviewConfig(), dry_run=dry_run, judge=judge)


def test_all_rows_newest_updated_first_with_offsets_and_closed_skipped(tmp_path: Path) -> None:
    sheet = Worksheet([record("older", **{"Updated At": "2026-10-08T16:00:00+02:00"}),
                       record("newer", **{"Updated At": "2026-10-08T10:00:00-05:00"}),
                       record("closed", Status="Closed", **{"Updated At": "not a timestamp"})])
    calls: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        calls.append(job.job_id)
        return judgment(job.job_id)

    report = run(Store(tmp_path / "t.db"), sheet, ReviewConfig(max_per_run=1), judge=judge)
    assert calls == ["newer"]
    assert report.reviewed == report.recommend == report.pending == 1
    assert len(sheet.writes) == 1 and len(sheet.writes[0]) == 5
    for update in sheet.writes[0]:
        row, column = a1_to_rowcol(str(update["range"]))
        assert row == 3 and HEADERS[column - 1] in AI_COLUMNS
    assert sheet.rows[2][0] == "Never change this" and sheet.rows[3][HEADERS.index("AI Score")] == ""


def test_legacy_reviews_reset_once_per_policy_and_input(tmp_path: Path) -> None:
    sheet = Worksheet([record(**{"AI Reviewed At": "2026-10-09T10:00:00-05:00"})])
    store = Store(tmp_path / "t.db")
    calls: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        calls.append(config.model)
        return judgment()

    assert run(store, sheet, judge=judge).reviewed == 1
    assert run(store, sheet, judge=judge).reviewed == 0
    assert run(store, sheet, ReviewConfig(model="other-model"), judge=judge).reviewed == 1
    sheet.rows[1][HEADERS.index("Salary")] = "$65,000 CAD/year"
    assert run(store, sheet, ReviewConfig(model="other-model"), judge=judge).reviewed == 1
    assert len(calls) == 3


def test_stale_review_timestamp_requires_review_even_with_published_cache(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    store = Store(tmp_path / "t.db")
    run(store, sheet, judge=lambda *args: judgment())
    sheet.rows[1][HEADERS.index("AI Reviewed At")] = "2026-10-08T09:59:59-05:00"
    _, [job] = read_jobs(sheet.rows, ReviewReport())
    assert needs_review(store, job, input_hash([job]), policy_hash(ReviewConfig()))


@pytest.mark.parametrize("bad", ["", "2026-10-08T15:00:00", "broken"])
def test_bad_updated_timestamp_is_reported_and_never_written(tmp_path: Path, bad: str) -> None:
    sheet = Worksheet([record(**{"Updated At": bad})])
    report = run(Store(tmp_path / "t.db"), sheet, judge=lambda *args: pytest.fail("Must not judge invalid row"))
    assert report.reviewed == 0 and report.pending == 1 and len(report.failures) == 1 and not sheet.writes


@pytest.mark.parametrize("header", [HEADERS[:-1], HEADERS + ["AI Score"]])
def test_missing_or_duplicate_header_stops_before_judgments_and_writes(tmp_path: Path, header: list[str]) -> None:
    sheet = Worksheet([record()], header)
    with pytest.raises(RuntimeError, match="exactly one sheet header"):
        run(Store(tmp_path / "t.db"), sheet, judge=lambda *args: pytest.fail("No judgments"))
    assert not sheet.writes


def test_duplicate_job_ids_stop_without_writes(tmp_path: Path) -> None:
    sheet = Worksheet([record(), record()])
    with pytest.raises(RuntimeError, match="Duplicate sheet Job ID"):
        run(Store(tmp_path / "t.db"), sheet)
    assert not sheet.writes


@pytest.mark.parametrize(("score", "pay", "expected_score", "decision"), [
    (89, 60000, 89, "Recommend"), (79, 50000, 71, "Maybe"), (57, 45000, 55, "Maybe"),
    (50, 45000, 40, "Reject"), (75, 55000, 75, "Recommend"),
])
def test_pay_arithmetic_and_maybe_floor(score: int, pay: int, expected_score: int, decision: str) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    result = judgment(base_score=score, pay=Pay(minimum=pay, maximum=pay, currency="CAD", unit="annual", full_time=True))
    fields = finalize(result, job, job)
    assert (fields.score, fields.decision) == (expected_score, decision)
    assert ("below the $55,000" in fields.notes) == (pay < 55000)


@pytest.mark.parametrize(("unit", "full_time", "currency", "amount", "expected"), [
    ("hourly", True, "CAD", 23.25, 80), ("hourly", False, "CAD", 23.25, 89),
    ("annual", True, "USD", 45000, 89), ("unknown", True, "CAD", 45000, 89),
])
def test_hourly_only_annualized_when_full_time_and_cad(unit: str, full_time: bool, currency: str,
                                                     amount: float, expected: int) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    pay = Pay.model_validate({"minimum": amount, "maximum": amount, "currency": currency, "unit": unit, "full_time": full_time})
    assert finalize(judgment(pay=pay), job, job).score == expected


def test_crossing_range_and_unknown_pay_have_no_penalty() -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    crossing = Pay(minimum=50000, maximum=60000, currency="CAD", unit="annual", full_time=True)
    fields = finalize(judgment(pay=crossing), job, job)
    assert fields.score == 89 and "range starts below" in fields.notes
    unknown = Pay(minimum=None, maximum=None, currency=None, unit="unknown", full_time=False)
    assert finalize(judgment(pay=unknown), job, job).score == 89


def test_hard_blocker_enforced_and_notes_timestamp_are_literal() -> None:
    _, [job] = read_jobs(Worksheet([record(Description="[truncated]")]).rows, ReviewReport())
    result = judgment(blockers=[Blocker(code="software_qa", evidence="Required software test automation")],
                      notes="=This is software testing.")
    fields = finalize(result, job, job)
    assert fields.score == 29 and fields.decision == "Reject" and fields.category == "Other"
    assert fields.notes.startswith("=") and "information was limited" in fields.notes
    assert timestamp(fields.reviewed_at).utcoffset() is not None and "T" in fields.reviewed_at


@pytest.mark.parametrize("description", ["", "Part of posting [truncated]"])
def test_limited_description_noted(description: str) -> None:
    _, [job] = read_jobs(Worksheet([record(Description=description)]).rows, ReviewReport())
    assert "information was limited" in finalize(judgment(), job, job).notes


def test_confirmed_duplicates_share_score_and_category_but_separate_openings_do_not(tmp_path: Path) -> None:
    sheet = Worksheet([record("short", Description="Test food."), record("complete"),
                       record("night", Description="Different plant, different shift.")])
    calls: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        assert {j.job_id for j in group} == {"short", "complete", "night"}
        calls.append(job.job_id)
        if job.job_id == "night":
            return judgment("night", base_score=78)
        return judgment("complete", duplicate_job_ids=["short"])

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert report.reviewed == 3 and len(calls) == 2
    assert sheet.rows[1][HEADERS.index("AI Notes")].startswith("Duplicate of complete.")
    assert sheet.rows[1][HEADERS.index("AI Score")] == sheet.rows[2][HEADERS.index("AI Score")] == "89"
    assert sheet.rows[3][HEADERS.index("AI Score")] == "78"


def test_unknown_duplicate_reference_stays_pending(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    report = run(Store(tmp_path / "t.db"), sheet, judge=lambda *args: judgment("missing"))
    assert report.pending == 1 and report.failures and not sheet.writes


def test_duplicate_canonical_outside_cap_reuses_the_same_judgment_next_run(tmp_path: Path) -> None:
    sheet = Worksheet([record("complete", **{"Updated At": "2026-10-08T09:00:00-05:00"}),
                       record("short", Description="Test food.")])
    store = Store(tmp_path / "t.db")
    config = ReviewConfig(max_per_run=1)
    first = run(store, sheet, config, judge=lambda *args: judgment("complete", duplicate_job_ids=["short"]))
    assert first.reviewed == 1 and first.pending == 1
    second = run(store, sheet, config, judge=lambda *args: pytest.fail("Canonical judgment was already made"))
    assert second.reviewed == 1 and second.pending == 0
    assert sheet.rows[1][HEADERS.index("AI Score")] == sheet.rows[2][HEADERS.index("AI Score")] == "89"


def test_sheet_reads_are_paced_and_quota_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", delays.append)
    sheet = ReviewSheet(cast(gspread.Worksheet, Worksheet([record()])))
    sheet.row_values(1)
    sheet.row_values(2)
    assert delays == [1.2]
    calls = 0

    def blocked() -> list[str]:
        nonlocal calls
        calls += 1
        raise api_error(429)

    with pytest.raises(APIError):
        sheet.read(blocked)
    assert calls == 3 and delays.count(60) == 2


def test_conflicting_duplicate_overlap_remains_pending_without_overwriting_prior_group(tmp_path: Path) -> None:
    sheet = Worksheet([record("a", **{"Updated At": "2026-10-08T12:00:00-05:00"}),
                       record("c", **{"Updated At": "2026-10-08T11:00:00-05:00"}), record("b")])

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        if job.job_id == "a":
            return judgment("b", duplicate_job_ids=["a"])
        assert job.job_id == "c"
        return judgment("c", duplicate_job_ids=["b"], base_score=79)

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert report.reviewed == 2 and report.pending == 1
    assert report.failures[0].error == "Conflicting duplicate canonical for Job ID b"
    scores = {row[HEADERS.index("Job ID")]: row[HEADERS.index("AI Score")] for row in sheet.rows[1:]}
    assert scores == {"a": "89", "b": "89", "c": ""}


def test_score_verification_uses_unformatted_values(tmp_path: Path) -> None:
    class FormattedSheet(Worksheet):
        def row_values(self, row: int, *, value_render_option: ValueRenderOption = ValueRenderOption.formatted) -> list[str]:
            values = super().row_values(row, value_render_option=value_render_option)
            index = HEADERS.index("AI Score")
            if row > 1 and values[index]:
                values[index] = f"{float(values[index]):.2f}" if value_render_option == ValueRenderOption.formatted else "89.0"
            return values

    report = run(Store(tmp_path / "t.db"), FormattedSheet([record()]), judge=lambda *args: judgment())
    assert report.reviewed == 1 and report.pending == 0 and not report.failures


def test_resort_after_judgment_writes_to_current_job_id(tmp_path: Path) -> None:
    sheet = Worksheet([record("one"), record("two", Company="Other")])

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        sheet.rows[1:] = list(reversed(sheet.rows[1:]))
        return judgment(job.job_id, base_score=88 if job.job_id == "one" else 73)

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert report.reviewed == 2 and report.recommend == report.maybe == 1
    for row in sheet.rows[1:]:
        assert row[HEADERS.index("AI Score")] == ("88" if row[HEADERS.index("Job ID")] == "one" else "73")


def test_changed_inputs_are_not_published(tmp_path: Path) -> None:
    sheet = Worksheet([record()])

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        sheet.rows[1][HEADERS.index("Salary")] = "$90,000 CAD/year"
        return judgment()

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert report.reviewed == 0 and report.pending == 1 and report.failures and not sheet.writes


def test_blocked_write_retries_one_row_with_exact_fallback_and_records_original_error(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    error = api_error()
    sheet.errors = [error]
    report = run(Store(tmp_path / "t.db"), sheet, judge=lambda *args: judgment())
    assert report.reviewed == 1 and report.pending == 0 and len(sheet.writes) == 2
    [failure] = report.failures
    assert failure.error == str(error) and failure.original_notes == judgment().notes and failure.retry_succeeded is True
    assert sheet.rows[1][HEADERS.index("AI Notes")] == WITHHELD
    assert sheet.writes[0][:2] == sheet.writes[1][:2]


def test_failed_write_resumes_from_cache_after_restart_without_another_model_call(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    sheet.errors = [api_error(), api_error()]
    path = tmp_path / "t.db"
    store = Store(path)
    first = run(store, sheet, judge=lambda *args: judgment())
    assert first.reviewed == 0 and first.pending == 1 and first.failures[0].retry_succeeded is False
    assert first.failures[0].retry_error == str(api_error())
    store.close()
    store = Store(path)
    resumed = run(store, sheet, judge=lambda *args: pytest.fail("Cached judgment must be reused"))
    assert resumed.reviewed == 1 and resumed.pending == 0
    assert sheet.rows[1][HEADERS.index("AI Notes")] == judgment().notes


def test_transient_write_retry_keeps_notes(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    sheet.errors = [api_error(429)]
    report = run(Store(tmp_path / "t.db"), sheet, judge=lambda *args: judgment())
    assert report.reviewed == 1 and sheet.writes[0] == sheet.writes[1]


def test_codex_unavailable_stops_calls_but_publishes_prior_results(tmp_path: Path) -> None:
    sheet = Worksheet([record("three"), record("two"), record("one")])
    calls: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        calls.append(job.job_id)
        if job.job_id == "two":
            raise CodexUnavailable("usage limit reached")
        return judgment(job.job_id)

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert calls == ["two"] and report.reviewed == 0 and report.pending == 3
    # Make the first selected job succeed, then hit the limit on the next one.
    sheet.rows[1][HEADERS.index("Updated At")] = "2026-10-08T11:00:00-05:00"
    calls.clear()
    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert calls == ["three", "two"] and report.reviewed == 1 and report.pending == 2


def test_dry_run_shows_results_without_writes_or_cache_changes(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    store = Store(tmp_path / "t.db")
    report = run(store, sheet, dry_run=True, judge=lambda *args: judgment())
    assert report.reviewed == 0 and report.pending == 1 and report.previews["one"].score == 89
    assert not sheet.writes and store.conn.execute("SELECT count(*) FROM ai_results").fetchone()[0] == 0


def test_interrupted_sheet_rebuild_blocks_review(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.backup_reviews([], {}, "now")
    with pytest.raises(RuntimeError, match="interrupted Sheet sync"):
        run(store, Worksheet([record()]))


def test_codex_process_uses_stdin_schema_chatgpt_and_no_inherited_credentials(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    for name in ["OPENAI_API_KEY", "CODEX_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "T3_ACP_MCP_NODE"]:
        monkeypatch.setenv(name, "must-not-inherit")

    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = cast(dict[str, str], kwargs["env"])
        assert not set(env) & {"OPENAI_API_KEY", "CODEX_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "T3_ACP_MCP_NODE"}
        assert env["CODEX_HOME"] == str(tmp_path / "codex")
        assert command[-1] == "-" and "--ignore-user-config" in command and "--ephemeral" in command
        assert 'forced_login_method="chatgpt"' in command and "features.shell_tool=false" in command
        assert "features.unified_exec=false" in command and "agents.enabled=false" in command
        assert job.values["Description"] in str(kwargs["input"])
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text())
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
        Path(command[command.index("-o") + 1]).write_text(judgment().model_dump_json())
        return subprocess.CompletedProcess(command, 0, stderr="")

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_codex(job, [job], ReviewConfig(codex_home=tmp_path / "codex")).base_score == 89


@pytest.mark.parametrize("result", ["invalid", "absent", "exit", "timeout"])
def test_codex_process_failures_never_become_judgments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())

    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if result == "invalid":
            Path(command[command.index("-o") + 1]).write_text('{"base_score": 999}')
        return subprocess.CompletedProcess(command, 1 if result == "exit" else 0, stderr="needs login")

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises((ValueError, CodexUnavailable)):
        run_codex(job, [job], ReviewConfig(codex_home=tmp_path / "codex"))


def test_strict_result_rejects_extra_fields_and_string_scores() -> None:
    data = judgment().model_dump()
    data["base_score"] = "89"
    with pytest.raises(ValidationError):
        Judgment.model_validate(data)
    with pytest.raises(ValidationError):
        judgment(unexpected="field")
