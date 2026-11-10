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
from shigoto.models import Job
from shigoto.review import (
    Blocker, CodexUnavailable, Judgment, Pay, ReviewJob, ReviewReport, ReviewSheet, WITHHELD,
    annual_maximum, finalize, needs_review, policy_hash, read_jobs, review_rows, rule_judgment, run_codex,
    salary_pay, timestamp,
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
                              "pay": Pay(minimum=60000, maximum=60000, currency="CAD", unit="annual", full_time=True, negotiable=False)}
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
    sheet.rows[1][HEADERS.index("Updated At")] = "2099-01-01T00:00:00-05:00"
    assert run(store, sheet, ReviewConfig(model="other-model"), judge=judge).reviewed == 1
    assert len(calls) == 3


def test_published_rejection_immediately_stops_closure_requests(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    store.upsert(Job(source="linkedin", source_id="one", url="https://example.com/one",
                     title="QA Technician", company="Saputo", location="Toronto", city="Toronto",
                     description="Test food samples and maintain QC records."), "2026-10-08T10:00:00-05:00")
    store.commit()
    [stored] = store.visible_jobs()
    sheet = Worksheet([record(stored.job_id)])
    now = "2026-11-03T12:00:00+00:00"
    assert len(store.liveness_candidates(now)) == 1
    report = run(store, sheet, judge=lambda *args: judgment(stored.job_id, base_score=20))
    assert report.reviewed == report.reject == 1 and report.pending == 0
    # No Sheet backup runs between publication and closure selection.
    assert store.reviews() == {}
    assert store.liveness_candidates(now) == []
    store.close()


def test_stale_review_timestamp_requires_review_even_with_published_cache(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    store = Store(tmp_path / "t.db")
    run(store, sheet, judge=lambda *args: judgment())
    sheet.rows[1][HEADERS.index("AI Reviewed At")] = "2026-10-08T09:59:59-05:00"
    _, [job] = read_jobs(sheet.rows, ReviewReport())
    assert needs_review(store, job, policy_hash(ReviewConfig()))


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
    (89, 60000, 89, "Recommend"), (79, 50000, 54, "Reject"), (57, 45000, 54, "Reject"),
    (50, 45000, 50, "Reject"), (75, 55000, 75, "Recommend"),
    # Within 5% of the floor counts as negotiable: Avalon Dairy's $26/hr is $54,080.
    (96, 54080, 74, "Maybe"), (96, 52250, 74, "Maybe"), (96, 52249, 54, "Reject"),
])
def test_pay_floor(score: int, pay: int, expected_score: int, decision: str) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    result = judgment(base_score=score, pay=Pay(minimum=pay, maximum=pay, currency="CAD", unit="annual", full_time=True, negotiable=False))
    fields = finalize(result, job, job)
    assert (fields.score, fields.decision) == (expected_score, decision)
    assert ("below the $55,000" in fields.notes) == (pay < 55000)


@pytest.mark.parametrize(("unit", "full_time", "currency", "amount", "expected"), [
    ("hourly", True, "CAD", 23.25, 54), ("hourly", False, "CAD", 23.25, 54),
    ("unknown", True, "CAD", 23.25, 54), ("unknown", True, "CAD", 45000, 89),
    # ZipRecruiter labels Canadian pay USD, and Codex repeats it: Nature's Touch's $21.95/hr stayed Recommend.
    ("hourly", True, "USD", 21.95, 54), ("annual", True, "USD", 45000, 54),
    ("hourly", True, "EUR", 23.25, 89), ("annual", True, None, 45000, 89),
])
def test_cad_hourly_pay_always_annualized(unit: str, full_time: bool, currency: str | None,
                                                     amount: float, expected: int) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    pay = Pay.model_validate({"minimum": amount, "maximum": amount, "currency": currency, "unit": unit, "full_time": full_time, "negotiable": False})
    assert finalize(judgment(pay=pay), job, job).score == expected


@pytest.mark.parametrize(("maximum", "expected"), [(1611.47, 89), (14999, 89), (15000, 54)])
def test_implausible_annual_pay_is_treated_as_unknown(maximum: float, expected: int) -> None:
    # Job Bank listed weekly pay ("$1,362.39 to $1,611.47 annually"); Codex reported it as annual.
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    pay = Pay(minimum=1362.39, maximum=maximum, currency="CAD", unit="annual", full_time=True, negotiable=False)
    fields = finalize(judgment(pay=pay), job, job)
    assert fields.score == expected and ("below the $55,000" in fields.notes) == (expected == 54)


def test_crossing_range_and_unknown_pay_have_no_penalty() -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    crossing = Pay(minimum=50000, maximum=60000, currency="CAD", unit="annual", full_time=True, negotiable=False)
    fields = finalize(judgment(pay=crossing), job, job)
    assert fields.score == 89 and fields.notes == judgment().notes
    unknown = Pay(minimum=None, maximum=None, currency=None, unit="unknown", full_time=False, negotiable=False)
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


def test_conflicting_duplicate_overlap_publishes_focus_standalone(tmp_path: Path) -> None:
    sheet = Worksheet([record("a", Description="Test food.", **{"Updated At": "2026-10-08T12:00:00-05:00"}),
                       record("c", Description="Another plant.", **{"Updated At": "2026-10-08T11:00:00-05:00"}),
                       record("b")])
    store = Store(tmp_path / "t.db")

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        if job.job_id == "a":
            return judgment("b", duplicate_job_ids=["a"])
        assert job.job_id == "c"
        return judgment("c", duplicate_job_ids=["b"], base_score=79)

    report = run(store, sheet, judge=judge)
    assert report.reviewed == 3 and report.pending == 0 and not report.failures
    scores = {row[HEADERS.index("Job ID")]: row[HEADERS.index("AI Score")] for row in sheet.rows[1:]}
    assert scores == {"a": "89", "b": "89", "c": "79"}
    notes = {row[HEADERS.index("Job ID")]: row[HEADERS.index("AI Notes")] for row in sheet.rows[1:]}
    assert notes["a"].startswith("Duplicate of b.") and not notes["c"].startswith("Duplicate of")
    # The prior group keeps its saved result, and the standalone job is not re-judged.
    saved = {row["job_id"]: Judgment.model_validate_json(row["result"])
             for row in store.conn.execute("SELECT job_id, result FROM ai_results")}
    assert saved["a"].canonical_job_id == saved["b"].canonical_job_id == "b"
    assert saved["c"].canonical_job_id == "c" and saved["c"].duplicate_job_ids == []
    again = run(store, sheet, judge=lambda *args: pytest.fail("Standalone job was already reviewed"))
    assert again.reviewed == 0 and again.pending == 0


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
    sheet = Worksheet([record(name, Description=f"Opening {name}.") for name in ("three", "two", "one")])
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


@pytest.mark.parametrize(("score", "expected", "decision"), [(89, 74, "Maybe"), (60, 60, "Maybe"), (50, 50, "Reject")])
def test_negotiable_pay_caps_at_maybe(score: int, expected: int, decision: str) -> None:
    _, [job] = read_jobs(Worksheet([record(Description="Pay depends on experience.")]).rows, ReviewReport())
    pay = Pay(minimum=45000, maximum=50000, currency="CAD", unit="annual", full_time=True, negotiable=True)
    fields = finalize(judgment(base_score=score, pay=pay), job, job)
    assert (fields.score, fields.decision) == (expected, decision)
    assert fields.notes.endswith("Known pay is CAD $50,000/year, below the $55,000 floor but negotiable.")


@pytest.mark.parametrize("code", ["too_far", "off_hours_shift", "casual_role"])
@pytest.mark.parametrize("negotiable", [False, True])
def test_new_blockers_cap_after_pay(code: str, negotiable: bool) -> None:
    _, [job] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    blocker = Blocker.model_validate({"code": code, "evidence": "Explicit condition in the posting"})
    pay = Pay(minimum=50000, maximum=50000, currency="CAD", unit="annual", full_time=True, negotiable=negotiable)
    fields = finalize(judgment(blockers=[blocker], pay=pay), job, job)
    assert fields.score == 29 and fields.decision == "Reject"


@pytest.mark.parametrize("value", [None, "true", 1])
def test_negotiable_is_required_and_strict(value: object) -> None:
    data = judgment().pay.model_dump()
    if value is None:
        del data["negotiable"]
    else:
        data["negotiable"] = value
    with pytest.raises(ValidationError):
        Pay.model_validate(data)


@pytest.mark.parametrize(("limit", "expected"), [(None, 1), (45, 45), (1, 1)])
def test_explicit_limit_overrides_config(tmp_path: Path, limit: int | None, expected: int) -> None:
    sheet = Worksheet([record(str(i)) for i in range(45)])
    report = review_rows(Store(tmp_path / "t.db"), cast(gspread.Worksheet, sheet), ReviewConfig(max_per_run=1),
                         limit=limit, judge=lambda job, group, config: judgment(job.job_id))
    assert report.reviewed == expected and report.pending == 45 - expected


def test_review_config_allows_batches_above_forty() -> None:
    assert ReviewConfig(max_per_run=100).max_per_run == 100
    with pytest.raises(ValidationError):
        ReviewConfig(max_per_run=0)



def test_blank_description_is_never_reviewed(tmp_path: Path) -> None:
    sheet = Worksheet([record("blank", Description="  "), record("described")])
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert judged == ["described"] and report.pending == 0


def add_row(sheet: Worksheet, values: dict[str, str]) -> None:
    sheet.rows.append([values.get(name, "") for name in HEADERS])


def test_duplicate_changing_or_closing_leaves_reviewed_jobs_alone(tmp_path: Path) -> None:
    sheet = Worksheet([record("one", Description="Plant one."), record("two", Description="Plant two.")])
    store = Store(tmp_path / "t.db")
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    assert run(store, sheet, judge=judge).reviewed == 2
    judged.clear()
    # Dynacare's 27-posting group re-judged its unchanged members on each new posting and closure.
    add_row(sheet, record("three", Description="Plant three."))
    report = run(store, sheet, judge=judge)
    assert judged == ["three"] and report.reviewed == 1 and report.pending == 0
    judged.clear()
    sheet.rows[3][HEADERS.index("Status")] = "Closed"
    report = run(store, sheet, judge=judge)
    assert not judged and report.reviewed == 0 and report.pending == 0


def test_unpublished_result_stays_pending_and_resumes_from_cache(tmp_path: Path) -> None:
    sheet = Worksheet([record()])
    store = Store(tmp_path / "t.db")
    sheet.errors = [api_error(), api_error()]
    calls: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        calls.append(job.job_id)
        return judgment()

    assert run(store, sheet, judge=judge).pending == 1
    assert run(store, sheet, judge=judge).reviewed == 1
    assert calls == ["one"]


def test_result_published_before_the_latest_content_stays_pending(tmp_path: Path) -> None:
    # The run wrote the Sheet and died before SQLite: the older published result can't stand in for it.
    sheet = Worksheet([record()])
    store = Store(tmp_path / "t.db")
    run(store, sheet, judge=lambda *args: judgment())
    sheet.rows[1][HEADERS.index("Updated At")] = "2099-01-01T00:00:00-05:00"
    sheet.rows[1][HEADERS.index("AI Reviewed At")] = "2099-01-02T00:00:00-05:00"
    _, [job] = read_jobs(sheet.rows, ReviewReport())
    assert needs_review(store, job, policy_hash(ReviewConfig()))


def test_skip_resumed_from_cache_still_counts_as_skipped(tmp_path: Path) -> None:
    sheet = Worksheet([record(Salary="21 CAD hourly")])
    store = Store(tmp_path / "t.db")
    sheet.errors = [api_error(), api_error()]
    assert run(store, sheet).skipped == 0
    report = run(store, sheet)
    assert (report.reviewed, report.skipped) == (1, 1)


def test_codex_negotiable_flag_needs_wording_in_the_posting() -> None:
    # ALS and Dynacare postings came back negotiable with nothing about pay being open.
    pay = Pay(minimum=44678, maximum=44678, currency="CAD", unit="annual", full_time=True, negotiable=True)
    _, [plain] = read_jobs(Worksheet([record()]).rows, ReviewReport())
    fields = finalize(judgment(pay=pay), plain, plain)
    assert (fields.score, fields.decision) == (54, "Reject") and "negotiable" not in fields.notes
    _, [salary] = read_jobs(Worksheet([record(Salary="$21 to $22 hourly (to be negotiated)")]).rows, ReviewReport())
    assert finalize(judgment(pay=pay), salary, salary).score == 74
    assert finalize(judgment(pay=pay), plain, salary).score == 74
    _, [closed] = read_jobs(Worksheet([record(Description="Salary is not negotiable.")]).rows, ReviewReport())
    assert finalize(judgment(pay=pay), closed, closed).score == 54


@pytest.mark.parametrize(("salary", "unit", "maximum"), [
    ("21 - 21.95 CAD hourly", "hourly", 21.95), ("21 - 21.95 USD hourly", "hourly", 21.95),
    ("$18.50 to $20.00 hourly", "hourly", 20), ("CA$22.00/hr - CA$24.00/hr", "hourly", 24),
    ("$21/hr - $22/hr", "hourly", 22), ("45,000 - 50,000 CAD yearly", "annual", 50000),
    ("$48,000 annually", "annual", 48000), ("CA$47,000.00/yr - CA$50,000.00/yr", "annual", 50000),
])
def test_salary_pay_reads_every_stored_format(salary: str, unit: str, maximum: float) -> None:
    pay = salary_pay(salary)
    assert pay is not None and (pay.unit, pay.maximum, pay.currency) == (unit, maximum, "CAD")


@pytest.mark.parametrize("salary", ["", "50,000 CAD", "$40,000 to $50,000", "4,597.8 - 5,469.59 CAD monthly",
                                    "1,349.34 - 1,697.54 CAD weekly", "300 CAD daily", "45,000 EUR yearly",
                                    "$45,000 - $60k annually", "CA$60K/yr"])
def test_salary_pay_leaves_unclear_periods_and_currencies_unknown(salary: str) -> None:
    assert salary_pay(salary) is None


def skip_for(**changes: str) -> Judgment | None:
    _, [job] = read_jobs(Worksheet([record(**changes)]).rows, ReviewReport())
    return rule_judgment(job)


@pytest.mark.parametrize("salary", ["21 CAD hourly", "21 USD hourly", "25 CAD hourly", "25.12 CAD hourly", "52,249 CAD yearly"])
def test_known_pay_under_the_near_floor_line_skips_codex(salary: str) -> None:
    result = skip_for(Salary=salary)
    assert result is not None and not result.blockers and result.base_score == 0


@pytest.mark.parametrize("salary", [
    "25.13 CAD hourly", "52,250 CAD yearly", "50,000 - 60,000 CAD yearly", "60,000 CAD yearly",
    # Garbled or mislabeled figures, which finalize also treats as unknown.
    "2 CAD hourly", "$1,362.39 to $1,611.47 annually", "$21 to $22 hourly (to be negotiated)",
])
def test_pay_codex_must_judge(salary: str) -> None:
    assert skip_for(Salary=salary) is None


@pytest.mark.parametrize(("description", "skipped"), [
    ("Pay: From $20.00 per hour", False), ("From CAD $21/hour", False), ("From CA$21/hour", False),
    ("Salary commensurate with experience.", False),
    ("Starting at $21 an hour.", False), ("Wages are negotiable.", False),
    ("Pay depending on your experience.", False), ("Wage based on qualifications.", False),
    # Green Infrastructure's posting, a Maybe that the first version of this wording skipped.
    ("The actual base rate will depend on several factors including experience.", False),
    ("A valid licence is non-negotiable.", True), ("Depending on production needs.", True),
    ("Salary is not negotiable.", True),
])
def test_open_pay_wording_keeps_low_pay_jobs_with_codex(description: str, skipped: bool) -> None:
    assert (skip_for(Salary="21 CAD hourly", Description=description) is not None) == skipped


@pytest.mark.parametrize(("description", "kind", "skipped"), [
    ("Status: Casual", "", True), ("Employment Type:** Casual", "", True), ("Status:\nPart Time", "", True),
    ("This is a casual position.", "", True), ("Position Type: Part Time", "", True),
    ("Part-time or full-time position.", "", False), ("Status: Casual", "fulltime", False),
    ("Status: Casual, moving to full time.", "", False), ("Casual dress code for this position.", "", False),
    ("No on-call duties for this position.", "", False), ("This position is not a part-time role.", "", False),
])
def test_casual_wording_skips_codex(description: str, kind: str, skipped: bool) -> None:
    result = skip_for(Description=description, Type=kind)
    assert (result is not None) == skipped
    if result:
        assert [blocker.code for blocker in result.blockers] == ["casual_role"]


def test_skipped_jobs_publish_a_reject_without_calling_codex(tmp_path: Path) -> None:
    sheet = Worksheet([record("pay", Salary="21 CAD hourly"), record("casual", Title="Lab Assistant", Description="Status: Casual", Type=""),
                       record("fit", Title="Lab Technician")])
    store = Store(tmp_path / "t.db")
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    report = run(store, sheet, judge=judge)
    assert judged == ["fit"]
    assert (report.reviewed, report.skipped, report.reject, report.recommend, report.pending) == (3, 2, 2, 1, 0)
    assert report.stats()["ai_skipped"] == 2
    rows = {row[HEADERS.index("Job ID")]: row for row in sheet.rows[1:]}
    assert rows["pay"][HEADERS.index("AI Decision")] == "Reject" and rows["pay"][HEADERS.index("AI Score")] == "0"
    assert rows["pay"][HEADERS.index("AI Notes")] == "Skipped AI review. Known pay is CAD $43,680/year, below the $55,000 floor."
    assert rows["casual"][HEADERS.index("AI Notes")] == 'Skipped AI review: the posting says "Status: Casual".'
    judged.clear()
    assert run(store, sheet, judge=judge).reviewed == 0 and not judged


def test_skip_and_codex_coexist_in_one_duplicate_group(tmp_path: Path) -> None:
    # The low-pay row is newest, so it is skipped first; Codex then judges its unpriced twin.
    sheet = Worksheet([record("low", Salary="21 CAD hourly", **{"Updated At": "2026-10-09T10:00:00-05:00"}),
                       record("blank", Salary="")])
    store = Store(tmp_path / "t.db")
    report = run(store, sheet, judge=lambda job, *rest: judgment(job.job_id, duplicate_job_ids=["low"]))
    assert (report.reviewed, report.skipped, report.failures) == (2, 1, [])


def test_dry_run_previews_a_skip_without_saving(tmp_path: Path) -> None:
    sheet = Worksheet([record(Salary="21 CAD hourly")])
    store = Store(tmp_path / "t.db")
    report = run(store, sheet, dry_run=True)
    assert report.previews["one"].decision == "Reject" and not sheet.writes
    assert store.conn.execute("SELECT count(*) FROM ai_results").fetchone()[0] == 0


def test_annual_maximum_rejects_implausible_hourly_figures() -> None:
    pay = Pay(minimum=2, maximum=2, currency="CAD", unit="hourly", full_time=False, negotiable=False)
    assert annual_maximum(pay) is None


def test_identical_repost_reuses_its_twins_judgment_without_codex(tmp_path: Path) -> None:
    sheet = Worksheet([record("old")])
    store = Store(tmp_path / "t.db")
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id, base_score=91)

    run(store, sheet, judge=judge)
    # A repost under a new ID joins the group and changes its fingerprint; the twin's result still stands.
    add_row(sheet, record("repost", **{"Updated At": "2026-10-09T10:00:00-05:00"}))
    report = run(store, sheet, judge=judge)
    assert judged == ["old"] and (report.reviewed, report.skipped, report.pending) == (1, 1, 0)
    rows = {row[HEADERS.index("Job ID")]: row for row in sheet.rows[1:]}
    assert rows["repost"][HEADERS.index("AI Score")] == rows["old"][HEADERS.index("AI Score")] == "91"
    assert rows["repost"][HEADERS.index("AI Notes")] == rows["old"][HEADERS.index("AI Notes")]
    saved = Judgment.model_validate_json(store.conn.execute(
        "SELECT result FROM ai_results WHERE job_id='repost'").fetchone()["result"])
    assert (saved.canonical_job_id, saved.duplicate_job_ids) == ("repost", [])


def test_identical_new_postings_in_one_run_cost_one_codex_call(tmp_path: Path) -> None:
    sheet = Worksheet([record("one"), record("two")])
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    report = run(Store(tmp_path / "t.db"), sheet, judge=judge)
    assert len(judged) == 1 and (report.reviewed, report.skipped) == (2, 1)


@pytest.mark.parametrize("change", [{"Salary": "$70,000 CAD/year"}, {"Description": "Test dairy samples."},
                                    {"Location": "Mississauga"}])
def test_any_changed_field_still_goes_to_codex(tmp_path: Path, change: dict[str, str]) -> None:
    sheet = Worksheet([record("old")])
    store = Store(tmp_path / "t.db")
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    run(store, sheet, judge=judge)
    add_row(sheet, record("new", **change))
    run(store, sheet, judge=judge)
    assert judged == ["old", "new"]


def test_twin_judged_under_another_policy_is_not_reused(tmp_path: Path) -> None:
    sheet = Worksheet([record("old")])
    store = Store(tmp_path / "t.db")
    run(store, sheet, judge=lambda job, *rest: judgment(job.job_id))
    add_row(sheet, record("repost"))
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    run(store, sheet, ReviewConfig(reasoning_effort="high"), judge=judge)
    assert len(judged) == 1  # one fresh judgment, then its twin reuses it


def test_twin_result_older_than_its_content_is_not_reused(tmp_path: Path) -> None:
    sheet = Worksheet([record("old")])
    store = Store(tmp_path / "t.db")
    run(store, sheet, judge=lambda job, *rest: judgment(job.job_id))
    # The twin's content changed after its review: its own result is stale, so the repost goes to Codex.
    sheet.rows[1][HEADERS.index("Updated At")] = "2099-01-01T00:00:00-05:00"
    add_row(sheet, record("repost", **{"Updated At": "2099-01-01T00:00:00-05:00"}))
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment(job.job_id)

    run(store, sheet, ReviewConfig(max_per_run=1), judge=judge)
    assert len(judged) == 1


def test_twin_published_as_a_duplicate_is_not_reused(tmp_path: Path) -> None:
    # The twin's score came from its canonical's text, which a standalone copy can't see.
    sheet = Worksheet([record("twin", **{"Updated At": "2026-10-09T10:00:00-05:00"}),
                       record("complete", Description="Test food samples and maintain QC records. Pay negotiable.")])
    store = Store(tmp_path / "t.db")
    run(store, sheet, judge=lambda job, *rest: judgment("complete", duplicate_job_ids=["twin"]))
    add_row(sheet, record("repost", **{"Updated At": "2026-10-10T10:00:00-05:00"}))
    judged: list[str] = []

    def judge(job: ReviewJob, group: list[ReviewJob], config: ReviewConfig) -> Judgment:
        judged.append(job.job_id)
        return judgment("complete", duplicate_job_ids=["twin", "repost"])

    run(store, sheet, judge=judge)
    assert judged == ["repost"]
