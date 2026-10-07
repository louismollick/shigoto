"""JobSpy adapter: LinkedIn, Indeed, Glassdoor, ... via python-jobspy.

Rate limiting (per JobSpy's README): Indeed has effectively none, so it searches every
term x city. LinkedIn blocks around the 10th page per IP, so it runs with
`combine_terms` (one OR query per city), a delay between cities, and stops for the rest
of the run as soon as JobSpy logs a 429.
"""

from __future__ import annotations

import logging
import math
import re
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


_UNIT = r"hourly|hours?|hrs?|yearly|years?|annum|annually|annual|monthly|months?|weekly|weeks?|daily|days?"
_AMOUNT = r"\d+(?:,\d{3})*(?:\.\d{1,2})?[kK]?"
_CURRENCY = r"CAD\s*\$?|USD\s*\$?|CA\$|C\$|US\$|\$"
_DESCRIPTION_PAY = re.compile(
    rf"(?P<currency>{_CURRENCY})\s*(?P<lo>{_AMOUNT})(?!\d|[.,]\d)"
    rf"(?:\s*(?:[-–—]|to)\s*(?:{_CURRENCY})?\s*(?P<hi>{_AMOUNT})(?!\d|[.,]\d))?"
    r"\s*(?P<suffix_currency>CAD|USD)?\s*"
    rf"(?:(?:/|per\s+|an?\s+)?(?P<unit>{_UNIT})\b)?"
    r"\s*(?P<end_currency>CAD|USD)?",
    re.IGNORECASE,
)
# "Pay Rate:", "Salary", "hourly pay of", "annual salary is" directly before an amount.
_PAY_LABEL = re.compile(
    rf"\b(?:(?P<unit>{_UNIT})\s+)?(?:base\s+)?(?:pay|salary|compensation|wages?)(?:\s+(?:rate|range))?"
    r"\s*(?::|\bof|\bis)?\s*$",
    re.IGNORECASE,
)
# Extras like "Afternoon Shift Premium $2.00/hour" in the same clause as the amount are not base pay.
_NOT_BASE_PAY = re.compile(r"\b(?:premiums?|bonus(?:es)?|differentials?|allowances?|stipends?)\b", re.IGNORECASE)


def _format_pay(amounts: list[float], currency: str, interval: str) -> str:
    nums = [f"{value:,.2f}".rstrip("0").rstrip(".") for value in amounts]
    return f"{' - '.join(dict.fromkeys(nums))} {currency}{f' {interval}' if interval else ''}"


def description_salary(description: str, default_currency: str = "CAD") -> str:
    """Read explicit money amounts with a pay period or an adjacent salary label.

    Unlabelled amounts without a period may be bonuses or benefits, so skip them, as are
    amounts in the same clause as a premium, bonus, differential, allowance or stipend.
    A labelled amount without a period stays unspecified rather than guessing one.
    """
    text = re.sub(r"[*_]", "", description)
    for match in _DESCRIPTION_PAY.finditer(text):
        prefix = text[:match.start()]
        if _NOT_BASE_PAY.search(re.split(r"[.;!?](?:\s|$)|\n", prefix)[-1]):
            continue
        label = _PAY_LABEL.search(prefix)
        unit = (match["unit"] or (label and label["unit"]) or "").lower()
        if not match["unit"] and not label:
            continue
        amounts = []
        for raw in (match["lo"], match["hi"]):
            if raw:
                amounts.append(float(raw.rstrip("kK").replace(",", "")) * (1000 if raw[-1].lower() == "k" else 1))
        if any(value <= 0 for value in amounts) or (len(amounts) == 2 and amounts[0] > amounts[1]):
            continue
        currency_text = (match["suffix_currency"] or match["end_currency"] or match["currency"]).upper()
        currency = "USD" if currency_text.startswith("US") else "CAD" if currency_text.startswith("C") else default_currency
        interval = ""
        if unit:
            if unit.startswith("h"):
                interval = "hourly"
            elif unit.startswith(("y", "a")):
                interval = "yearly"
            elif unit.startswith("m"):
                interval = "monthly"
            elif unit.startswith("w"):
                interval = "weekly"
            else:
                interval = "daily"
        return _format_pay(amounts, currency, interval)
    return ""


def format_salary(row: dict[str, object]) -> str:
    lo, hi = row.get("min_amount"), row.get("max_amount")
    amounts = [float(v) for v in (lo, hi) if isinstance(v, (int, float)) and math.isfinite(v)]
    currency = clean(row.get("currency")) or "CAD"
    if not amounts:
        return description_salary(clean_text(row.get("description")), currency)
    interval = clean(row.get("interval"))
    return _format_pay(amounts, currency, interval)


def _jobspy_logger_name(site: str) -> str:
    return {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor",
            "zip_recruiter": "ZipRecruiter", "google": "Google"}[site]
