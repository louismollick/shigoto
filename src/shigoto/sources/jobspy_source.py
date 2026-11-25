"""JobSpy searches with per-source/group checkpoints and periodic full sweeps."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta

import jobspy.linkedin
import jobspy.ziprecruiter
from jobspy import scrape_jobs
from jobspy.model import ScraperInput
from jobspy.util import TLSRotating

from shigoto.config import City, JobSpySite
from shigoto.db import Store, now_iso
from shigoto.http import proxy_url
from shigoto.models import Job
from shigoto.sources import cloudflare
from shigoto.text import clean, clean_description, parse_date

log = logging.getLogger(__name__)


# 403 is a bot challenge (ZipRecruiter on datacenter IPs); 429/999 are rate limits. All block the run.
_BLOCKED = re.compile(r"\b(403|429|999)\b")
# A Cloudflare challenge needs a new clearance, and a curl transport failure means the proxy may be
# down, so both drop it. Rate limits, server errors and parsing failures keep it.
_CLEARANCE_LOST = re.compile(r"\b403\b|curl: \(\d+\)")
_ATTEMPTS = 5  # retries ride out transient errors and short rate limits, or get a new clearance
_ZIP_RECRUITER_RETRY_SECONDS = 5
# Through the static proxy LinkedIn's occasional 429 clears within a minute (Nov 2026), and any
# retry restarts at page 1, so it waits longer than the ~30s between pages.
_PROXIED_RETRY_SECONDS = 60

# LinkedIn rations each IP: through the static proxy, pages 12s apart got 429 every other request,
# while 30s apart passed 10 of 10 (Nov 2026). JobSpy waits 3-7s between pages by default.
setattr(jobspy.linkedin.LinkedIn, "delay", (25, 35))


class _BlockDetector(logging.Handler):
    """Watches JobSpy's swallowed search errors, including rate-limit responses."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.blocked = False
        self.clearance_lost = False
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        self.failed = True
        msg = record.getMessage()
        self.blocked |= bool(_BLOCKED.search(msg))
        self.clearance_lost |= bool(_CLEARANCE_LOST.search(msg))


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
        jobspy_logger = logging.getLogger("jobspy")  # every scraper logs to a child of this
        groups = self.site.query_groups
        queries = [build_queries(group, True) for group in groups] if groups else [self.terms]
        rotate = bool(groups) and self.site.rotate_groups
        cities = self.cities
        selected: list[int] | range = range(len(queries))
        if rotate:
            # Advance every run, even if this one gets blocked, so a failing group or city
            # can't starve the rest. The counter also rotates which city goes first.
            rotation_key = f"jobspy:{self.name}:next_group"
            counter = int(self.store.get_meta(rotation_key) or "0")
            self.store.set_meta(rotation_key, str(counter + 1))
            selected = [counter % len(queries)]
            start = counter % len(cities)
            cities = cities[start:] + cities[:start]
        # Every request leaves from one IP, the VPS's or SHIGOTO_PROXY's. Direct searches stop at the
        # first block. Proxied ones retry, waiting out a rate limit, and stop once retries run out.
        # ZipRecruiter retries transient errors and re-solves a challenged clearance, but stops on a
        # rate limit.
        zip_recruiter = self.name == "zip_recruiter"
        if zip_recruiter and not cloudflare.configured():
            log.info("zip_recruiter skipped: SHIGOTO_BYPARR_URL and SHIGOTO_PROXY are required")
            return
        retrying = zip_recruiter or (self.site.use_proxy and proxy_url() is not None)
        attempts = _ATTEMPTS if retrying else 1
        retry_seconds = _ZIP_RECRUITER_RETRY_SECONDS if zip_recruiter else _PROXIED_RETRY_SECONDS
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
                for city in cities:
                    for query in group_queries:
                        # Union of every attempt's rows: a deep search is ~30 pages, so an attempt that
                        # fails partway still found real rows. LinkedIn retries resume after them, since
                        # its limit allows only a few pages a minute; ZipRecruiter restarts.
                        found: dict[str, Job] = {}
                        for attempt in range(attempts):
                            detector.failed = detector.blocked = detector.clearance_lost = False
                            cleared = cloudflare.clearance() if zip_recruiter else None
                            if zip_recruiter and cleared is None:
                                # Its proxy's IP is always challenged without a clearance.
                                log.warning("%s has no Cloudflare clearance; stopping source for this run",
                                            self.name)
                                self.incomplete = True
                                yield from found.values()
                                return
                            offset = 0 if zip_recruiter else len(found)
                            try:
                                for job in self._search(query, city, hours_old, results_wanted - offset, cleared,
                                                        offset):
                                    found.setdefault(job.source_id, job)
                            except Exception as error:
                                detector.failed = True
                                detector.blocked = bool(_BLOCKED.search(str(error)))
                                detector.clearance_lost = bool(_CLEARANCE_LOST.search(str(error)))
                                log.warning("%s %r in %s failed: %s", self.name, query, city.name, error)
                            if cleared and detector.clearance_lost:
                                cloudflare.invalidate(cleared)
                            elif zip_recruiter and detector.blocked:
                                # A rate limit from its one fixed IP won't clear on retry.
                                log.warning("%s %r in %s rate limited; stopping source for this run",
                                            self.name, query, city.name)
                                self.incomplete = True
                                yield from found.values()
                                return
                            if not detector.failed:
                                break
                            if attempt + 1 < attempts:
                                log.info("%s %r in %s: retrying", self.name, query, city.name)
                                time.sleep(retry_seconds)
                        if detector.failed:
                            complete = False
                            self.incomplete = True
                            if not retrying:
                                # Drop partial rows, and stop on a block.
                                log.warning("%s %r in %s skipped; pass incomplete%s", self.name, query, city.name,
                                            " (rate limited, stopping source)" if detector.blocked else "")
                                if detector.blocked:
                                    return
                                time.sleep(self.site.delay_seconds)
                                continue
                            log.warning("%s %r in %s incomplete; keeping %d partial results", self.name, query,
                                        city.name, len(found))
                            # ZipRecruiter already stopped on a rate limit; its 403s are challenges.
                            if detector.blocked and not zip_recruiter:
                                log.warning("%s still rate limited after %d attempts; stopping source for this run",
                                            self.name, attempts)
                                yield from found.values()
                                return
                        yield from found.values()
                        time.sleep(self.site.delay_seconds)
                if not complete:
                    continue
                self.store.set_meta(f"{key}:last_success", started)
                if full_sweep:
                    self.store.set_meta(f"{key}:last_full_sweep", started)
        finally:
            jobspy_logger.removeHandler(detector)

    def _search(self, query: str, city: City, hours_old: int | None, results_wanted: int,
                cleared: cloudflare.Clearance | None = None, offset: int = 0) -> Iterator[Job]:
        with _zip_recruiter_clearance(cleared):
            df = scrape_jobs(
                site_name=[self.name],
                search_term=query,
                location=city.jobspy_location,
                results_wanted=results_wanted,
                offset=offset,
                hours_old=hours_old,
                country_indeed="Canada",
                description_format="markdown",
                # A clearance only holds from the static IP that solved it.
                proxies=cleared.proxy if cleared else proxy_url() if self.site.use_proxy else None,
                verbose=1,
            )
        log.info("%s %r in %s: %d results", self.name, query[:40], city.name, len(df))
        for row in df.to_dict("records"):
            job = row_to_job({str(key): value for key, value in row.items()}, city.name)
            if job:
                yield job


@contextmanager
def _zip_recruiter_clearance(cleared: cloudflare.Clearance | None) -> Iterator[None]:
    """JobSpy builds its ZipRecruiter session internally, so wrap that factory to add the clearance."""
    if cleared is None:
        yield
        return
    original: Callable[[TLSRotating, ScraperInput, dict[str, str] | None], TLSRotating] = getattr(
        jobspy.ziprecruiter, "create_session")

    def create(session: TLSRotating, scraper_input: ScraperInput,
               headers: dict[str, str] | None = None) -> TLSRotating:
        session = original(session, scraper_input, headers)
        cloudflare.apply(session, cleared)
        return session

    setattr(jobspy.ziprecruiter, "create_session", create)
    try:
        yield
    finally:
        setattr(jobspy.ziprecruiter, "create_session", original)


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
        apply_url=clean(row.get("job_url_direct")),
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
