"""The canonical Job every source adapter returns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


@dataclass
class Job:
    """One posting as seen by one source.

    `source` is the board family ("linkedin", "indeed", "jobbank", "workday", ...);
    `source_id` is unique within that source. `city` is one of the configured target
    cities: sources that search by city set it up front, others leave it blank and
    normalize() derives it from `location`.
    """

    source: str
    source_id: str
    url: str
    title: str
    company: str
    location: str
    description: str = ""
    posted_date: date | None = None
    salary: str = ""
    job_type: str = ""
    city: str = ""


Liveness = Literal["gone", "alive", "unknown"]
