"""Typed app configuration, loaded from config.yaml plus env vars for secrets/paths:
GOOGLE_APPLICATION_CREDENTIALS, SHIGOTO_SPREADSHEET_ID, SHIGOTO_DB."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


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
    """One JobSpy board. `combine_terms` ORs all search terms into one query per city,
    which is how we keep LinkedIn to a handful of requests per run."""

    site: JobSpySiteName
    combine_terms: bool = False
    results_wanted: int = 50
    hours_old: int = 24
    delay_seconds: float = 2.0
    enabled: bool = True


class DescriptionFetch(BaseModel):
    """Per-run caps for fetching missing descriptions one page at a time."""

    linkedin_max_per_run: int = 25
    linkedin_delay_seconds: float = 10.0
    jobbank_max_per_run: int = 60


class WorkdayBoard(BaseModel):
    name: str
    url: str


class SlugBoard(BaseModel):
    name: str
    slug: str


class Boards(BaseModel):
    """Employer ATS boards. Workday + SmartRecruiters are searched per term server-side;
    Greenhouse/Lever/Ashby return the whole board and are filtered by `ats_title_keywords`."""

    workday: list[WorkdayBoard] = Field(default_factory=list)
    greenhouse: list[SlugBoard] = Field(default_factory=list)
    lever: list[SlugBoard] = Field(default_factory=list)
    ashby: list[SlugBoard] = Field(default_factory=list)
    smartrecruiters: list[SlugBoard] = Field(default_factory=list)


class SheetConfig(BaseModel):
    spreadsheet_id: str = ""  # usually from SHIGOTO_SPREADSHEET_ID
    worksheet: str = "Shigoto"
    description_max_chars: int = 20000


class Config(BaseModel):
    search_terms: list[str]
    cities: list[City]
    jobspy: list[JobSpySite] = Field(default_factory=list)
    jobbank_enabled: bool = True
    boards: Boards = Field(default_factory=Boards)
    ats_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    descriptions: DescriptionFetch = Field(default_factory=DescriptionFetch)
    sheet: SheetConfig
    interval_hours: float = 6.0

    # Filled from env, not yaml.
    db_path: Path = Path("data/shigoto.db")
    google_credentials: Path | None = None


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    config = Config.model_validate(raw)
    config.db_path = Path(os.environ.get("SHIGOTO_DB", str(config.db_path)))
    config.sheet.spreadsheet_id = os.environ.get("SHIGOTO_SPREADSHEET_ID", config.sheet.spreadsheet_id)
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    config.google_credentials = Path(creds) if creds else None
    return config
