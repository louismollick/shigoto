"""JobSpy searches with per-source/group checkpoints and periodic full sweeps."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta

from jobspy import scrape_jobs
import jobspy.indeed as jobspy_indeed

from shigoto.config import City, JobSpySite
from shigoto.db import Store, now_iso
from shigoto.models import Job
from shigoto.text import clean, clean_description, parse_date

log = logging.getLogger(__name__)


def include_exact_salary(query: str) -> str:
    """Work around JobSpy's Range-only Indeed query without extra detail requests.

    Indeed also returns Exactly.value. Alias it to the bounds JobSpy already reads,
    for both employer pay and estimates. Keep the braces escaped for query.format().
    """
    if "... on Exactly" in query:
        return query
    return query.replace(
        "... on Range {{",
        "... on Exactly {{ min: value max: value }}\n                    ... on Range {{",
    )


# JobSpy imports the query into its scraper module; changing constant.py alone has no effect.
jobspy_indeed.job_search_query = include_exact_salary(jobspy_indeed.job_search_query)


class _BlockDetector(logging.Handler):
    """Watches JobSpy's swallowed search errors, including rate-limit responses."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.blocked = False
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        self.failed = True
        msg = record.getMessage()
        if "429" in msg or "999" in msg:
            self.blocked = True


def build_queries(terms: list[str], combine: bool) -> list[str]:
    """Quote multi-word terms; OR them together when combining."""
    if not combine:
        return terms
    return [" OR ".join(f'"{t}"' if " " in t else t for t in terms)]


class JobSpySource:
    def __init__(self, site: JobSpySite, terms: list[str], cities: list[City], store: Store) -> None:
        self.site = site
        self.name: str = site.site
        self.terms = terms
        self.cities = cities
        self.store = store
        self.incomplete = False

    def fetch(self) -> Iterator[Job]:
        self.incomplete = False
        detector = _BlockDetector()
        jobspy_logger = logging.getLogger(f"JobSpy:{_jobspy_logger_name(self.name)}")
        groups = self.site.query_groups
        queries = [build_queries(group, True) for group in groups] if groups else [self.terms]
        rotation_key = f"jobspy:{self.name}:next_group"
        index = int(self.store.get_meta(rotation_key) or "0") % len(queries)
        selected = [index] if groups and self.site.rotate_groups else range(len(queries))
        jobspy_logger.addHandler(detector)
        try:
            for group_index in selected:
                group_queries = queries[group_index]
                key = f"jobspy:{self.name}"
                if groups:
                    # Changed queries start fresh; reordering groups keeps their history.
                    identity = hashlib.sha1(group_queries[0].encode()).hexdigest()[:16]
                    key += f":group:{identity}"
                started = now_iso()
                now = datetime.fromisoformat(started)
                checkpoint = self.store.get_meta(f"{key}:last_success")
                last_full = self.store.get_meta(f"{key}:last_full_sweep")
                full_sweep = checkpoint is None or (self.site.full_sweep_days is not None and (
                    last_full is None or now - datetime.fromisoformat(last_full)
                    >= timedelta(days=self.site.full_sweep_days)))
                hours_old = None if full_sweep else max(
                    self.site.hours_old,
                    math.ceil((now - datetime.fromisoformat(checkpoint or now.isoformat())).total_seconds() / 3600) + 2,
                )
                results_wanted = (self.site.full_sweep_results_wanted if full_sweep
                                  else self.site.results_wanted)
                complete = True
                for city in self.cities:
                    for query in group_queries:
                        detector.failed = False
                        try:
                            jobs = list(self._search(query, city, hours_old, results_wanted))
                        except Exception as error:
                            detector.failed = True
                            detector.blocked |= "429" in str(error) or "999" in str(error)
                            log.warning("%s %r in %s failed: %s", self.name, query, city.name, error)
                        if detector.failed:
                            complete = False
                            self.incomplete = True
                            log.warning("%s %r in %s skipped; pass incomplete%s", self.name, query, city.name,
                                        " (rate limited, stopping source)" if detector.blocked else "")
                            if detector.blocked:
                                return
                        else:
                            yield from jobs
                        time.sleep(self.site.delay_seconds)
                if not complete:
                    continue
                self.store.set_meta(f"{key}:last_success", started)
                if full_sweep:
                    self.store.set_meta(f"{key}:last_full_sweep", started)
                if groups and self.site.rotate_groups:
                    self.store.set_meta(rotation_key, str((group_index + 1) % len(queries)))
        finally:
            jobspy_logger.removeHandler(detector)

    def _search(self, query: str, city: City, hours_old: int | None, results_wanted: int) -> Iterator[Job]:
        df = scrape_jobs(
            site_name=[self.name],
            search_term=query,
            location=city.jobspy_location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            country_indeed="Canada",
            description_format="markdown",
            verbose=1,
        )
        log.info("%s %r in %s: %d results", self.name, query[:40], city.name, len(df))
        for row in df.to_dict("records"):
            job = row_to_job(row, city.name)
            if job:
                yield job


def row_to_job(row: Mapping[str, object], city: str) -> Job | None:
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
# Pay labels before an amount, including "hourly rate for the role is".
_PAY_LABEL = re.compile(
    rf"\b(?:(?P<unit>{_UNIT})\s+)?(?:base\s+)?(?P<label>pay|salary|compensation|wages?|rate)(?:\s+(?:rate|range))?"
    r"(?:\s+for\s+(?:the|this)\s+(?:role|position|job))?"
    r"\s*(?::|\bof|\bis|\b(?:starts?|starting)\s+(?:at|from))?\s*$",
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
        if label and label["label"].lower() == "rate" and not unit:
            continue  # An unspecified rate can describe an expense rather than pay.
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


def format_salary(row: Mapping[str, object]) -> str:
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
