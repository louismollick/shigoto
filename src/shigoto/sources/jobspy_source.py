"""JobSpy adapter: LinkedIn, Indeed, Glassdoor, ... via python-jobspy.

Rate limiting (per JobSpy's README): Indeed has effectively none, so it searches every
term x city. LinkedIn blocks around the 10th page per IP, so it runs with
`combine_terms` (one OR query per city), a delay between cities, and stops for the rest
of the run as soon as JobSpy logs a 429.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from typing import Any

from jobspy import scrape_jobs

from shigoto.config import City, JobSpySite
from shigoto.models import Job
from shigoto.text import clean, clean_description, parse_date

log = logging.getLogger(__name__)


class _BlockDetector(logging.Handler):
    """Watches JobSpy's own logs for rate-limit responses, which it swallows."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.blocked = False

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "429" in msg or "999" in msg:
            self.blocked = True


def build_queries(terms: list[str], combine: bool) -> list[str]:
    """Quote multi-word terms; OR them together when combining."""
    if not combine:
        return terms
    return [" OR ".join(f'"{t}"' if " " in t else t for t in terms)]


class JobSpySource:
    def __init__(self, site: JobSpySite, terms: list[str], cities: list[City], backfill: bool = False) -> None:
        self.site = site
        self.hours_old = site.backfill_hours_old if backfill else site.hours_old
        self.name: str = site.site
        self.terms = terms
        self.cities = cities

    def fetch(self) -> Iterator[Job]:
        detector = _BlockDetector()
        jobspy_logger = logging.getLogger(f"JobSpy:{_jobspy_logger_name(self.name)}")
        jobspy_logger.addHandler(detector)
        try:
            for city in self.cities:
                for query in build_queries(self.terms, self.site.combine_terms):
                    if detector.blocked:
                        log.warning("%s rate-limited us; skipping the rest of this run", self.name)
                        return
                    yield from self._search(query, city)
                    time.sleep(self.site.delay_seconds)
        finally:
            jobspy_logger.removeHandler(detector)

    def _search(self, query: str, city: City) -> Iterator[Job]:
        try:
            df = scrape_jobs(
                site_name=[self.name],
                search_term=query,
                location=city.jobspy_location,
                results_wanted=self.site.results_wanted,
                hours_old=self.hours_old,
                country_indeed="Canada",
                description_format="markdown",
                verbose=1,
            )
        except Exception:
            log.exception("%s search failed: %r in %s", self.name, query, city.name)
            return
        log.info("%s %r in %s: %d results", self.name, query[:40], city.name, len(df))
        for row in df.to_dict("records"):
            job = row_to_job(row, city.name)
            if job:
                yield job


def row_to_job(row: dict[str, Any], city: str) -> Job | None:
    source_id = clean(row.get("id"))
    url = clean(row.get("job_url"))
    title = clean(row.get("title"))
    if not (source_id and url and title):
        return None
    return Job(
        source=clean(row.get("site")),
        source_id=source_id,
        url=url,
        title=title,
        company=clean(row.get("company")),
        location=clean(row.get("location")),
        description=clean_description(clean_text(row.get("description"))),
        posted_date=parse_date(row.get("date_posted")),
        salary=format_salary(row),
        job_type=clean(row.get("job_type")),
        city=city,
    )


def clean_text(value: object) -> str:
    return "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)


def format_salary(row: dict[str, Any]) -> str:
    lo, hi = row.get("min_amount"), row.get("max_amount")
    nums = [f"{v:,.0f}" for v in (lo, hi) if isinstance(v, (int, float)) and not math.isnan(v)]
    if not nums:
        return ""
    currency = clean(row.get("currency")) or "CAD"
    interval = clean(row.get("interval"))
    return f"{' - '.join(dict.fromkeys(nums))} {currency}{f' {interval}' if interval else ''}"


def _jobspy_logger_name(site: str) -> str:
    return {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor",
            "zip_recruiter": "ZipRecruiter", "google": "Google"}[site]
