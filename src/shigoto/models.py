"""The canonical Job every source adapter returns."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal, cast


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


def job_json(job: Job) -> str:
    """Snapshot an adapter's job before normalization mutates it."""
    data = asdict(job)
    data["posted_date"] = job.posted_date.isoformat() if job.posted_date else None
    return json.dumps(data)


def job_from_json(raw: str) -> Job:
    data = cast(dict[str, str], json.loads(raw))
    posted = cast(str | None, data["posted_date"])
    return Job(
        source=data["source"], source_id=data["source_id"], url=data["url"], title=data["title"],
        company=data["company"], location=data["location"], description=data["description"],
        posted_date=date.fromisoformat(posted) if posted else None,
        salary=data["salary"], job_type=data["job_type"], city=data["city"],
    )
