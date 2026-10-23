"""Typed app configuration, loaded from config.yaml plus env vars for secrets/paths:
GOOGLE_APPLICATION_CREDENTIALS, SHIGOTO_SPREADSHEET_ID, SHIGOTO_DB, SHIGOTO_PROXY (see http.proxy_url)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class City(BaseModel):
    """A target city. `jobspy_location` is what we pass to JobSpy; `aliases` are extra
    place names (suburbs) that count as this city when matching a job's location text."""

    name: str
    jobspy_location: str
    aliases: list[str] = Field(default_factory=list)

    def match_names(self) -> list[str]:
        return [self.name, *self.aliases]


JobSpySiteName = Literal["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"]


class JobSpySite(BaseModel):
    """One JobSpy board. Query groups OR their terms; rotation searches one group per run."""

    site: JobSpySiteName
    query_groups: list[Annotated[list[str], Field(min_length=1)]] | None = Field(default=None, min_length=1)
    rotate_groups: bool = False
    results_wanted: int = Field(default=50, ge=1)
    hours_old: int = Field(default=24, ge=1)
    full_sweep_days: int | None = Field(default=None, ge=1)
    full_sweep_results_wanted: int = Field(default=400, ge=1)
    delay_seconds: float = 2.0
    use_proxy: bool = False  # route searches through SHIGOTO_PROXY when it's set
    enabled: bool = True


class DescriptionFetch(BaseModel):
    """Per-run caps for fetching missing descriptions one page at a time."""

    linkedin_max_per_run: int = 25
    linkedin_delay_seconds: float = 10.0
    # Used instead when SHIGOTO_PROXY is set: each request gets a fresh IP.
    linkedin_proxied_max_per_run: int = 100
    linkedin_proxied_delay_seconds: float = 2.0
    jobbank_max_per_run: int = 60
    ziprecruiter_max_per_run: int = 50
    ziprecruiter_delay_seconds: float = 2.0


class WorkdayBoard(BaseModel):
    name: str
    url: str


class SuccessFactorsBoard(BaseModel):
    name: str
    url: str


class SlugBoard(BaseModel):
    name: str
    slug: str


class Boards(BaseModel):
    """Employer ATS boards. Workday/SmartRecruiters/SuccessFactors search per term;
    Greenhouse/Lever/Ashby return the whole board."""

    successfactors: list[SuccessFactorsBoard] = Field(default_factory=list)
    workday: list[WorkdayBoard] = Field(default_factory=list)
    greenhouse: list[SlugBoard] = Field(default_factory=list)
    lever: list[SlugBoard] = Field(default_factory=list)
    ashby: list[SlugBoard] = Field(default_factory=list)
    smartrecruiters: list[SlugBoard] = Field(default_factory=list)


class SheetConfig(BaseModel):
    spreadsheet_id: str = ""  # usually from SHIGOTO_SPREADSHEET_ID
    worksheet: str = "Shigoto"
    worksheet_id: int | None = Field(default=None, ge=0)
    description_max_chars: int = 20000


class ReviewConfig(BaseModel):
    """Codex judgments use saved ChatGPT login, never an API key."""

    enabled: bool = False
    max_per_run: int = Field(default=120, ge=1)
    model: str = "gpt-6.1-sol"
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "low"
    timeout_seconds: int = Field(default=180, ge=1)
    codex_bin: str = "codex"
    codex_home: Path = Path("data/codex")


class Config(BaseModel):
    search_terms: list[str]
    cities: list[City]
    jobspy: list[JobSpySite] = Field(default_factory=list)
    jobbank_enabled: bool = True
    boards: Boards = Field(default_factory=Boards)
    title_keywords: list[str]  # a job's title must contain one (word-prefix match)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    # An exclusion is ignored when the title also matches one of its exceptions ("Senior Lab Technician").
    exclude_title_exceptions: dict[str, list[str]] = Field(default_factory=dict)
    exclude_companies: list[str] = Field(default_factory=list)
    max_posting_age_days: int | None = Field(default=None, ge=1)
    descriptions: DescriptionFetch = Field(default_factory=DescriptionFetch)
    sheet: SheetConfig
    liveness_max_per_run: int = Field(default=40, ge=0)
    interval_hours: float = 6.0
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    # Filled from env, not yaml.
    db_path: Path = Path("data/shigoto.db")
    google_credentials: Path | None = None

    @model_validator(mode="after")
    def _exceptions_name_exclusions(self) -> Config:
        if unknown := set(self.exclude_title_exceptions) - set(self.exclude_title_keywords):
            raise ValueError(f"exclude_title_exceptions keys are not in exclude_title_keywords: {sorted(unknown)}")
        return self


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    config = Config.model_validate(raw)
    config.db_path = Path(os.environ.get("SHIGOTO_DB", str(config.db_path)))
    config.sheet.spreadsheet_id = os.environ.get("SHIGOTO_SPREADSHEET_ID", config.sheet.spreadsheet_id)
    config.review.codex_home = Path(os.environ.get("CODEX_HOME", str(config.review.codex_home)))
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    config.google_credentials = Path(creds) if creds else None
    return config
