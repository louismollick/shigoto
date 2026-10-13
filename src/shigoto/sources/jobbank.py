"""Canada Job Bank adapter (ported from Career Ops' providers/jobbankca.mjs).

Public Atom feed, no auth. robots.txt sets `Crawl-delay: 5`, which we honor for every
request including detail pages. `locationstring` doesn't filter reliably, so we search
nationwide per keyword and let normalize() keep only target cities. The recent feed
is supplemented with paginated HTML results for older open postings.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from shigoto.db import Store
from shigoto.http import PoliteSession, posting_liveness
from shigoto.models import Job, Liveness
from shigoto.text import clean, html_to_text, parse_date

log = logging.getLogger(__name__)

FEED_URL = "https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed"
SEARCH_URL = "https://www.jobbank.gc.ca/jobsearch/jobsearch"
PAGE_SIZE = 100
MAX_PAGES = 5
CRAWL_DELAY = 5.0
SEARCH_PAGE_SIZE = 25
SEARCH_MAX_PAGES = 1000
HTML_SEARCH_LAST_SUCCESS = "jobbank:html_search:last_success"
ATOM = "{http://www.w3.org/2005/Atom}"

_session = PoliteSession(min_interval=CRAWL_DELAY, respect_robots=True)


class JobBankSource:
    name = "jobbank"

    def __init__(self, terms: list[str], store: Store) -> None:
        self.terms = terms
        self.store = store

    def fetch(self) -> Iterator[Job]:
        seen: set[str] = set()
        for job in self._feed():
            if job.source_id not in seen:
                seen.add(job.source_id)
                yield job
        if html_search_due(self.store):
            for job in self._search():
                if job.source_id not in seen:
                    seen.add(job.source_id)
                    yield job

    def _feed(self) -> Iterator[Job]:
        for term in self.terms:
            for page in range(1, MAX_PAGES + 1):
                url = f"{FEED_URL}?{urlencode({'searchstring': term, 'locationstring': '', 'page': page})}"
                try:
                    xml = _session.get_text(url)
                    jobs = parse_feed(xml)
                except Exception:
                    log.exception("jobbank feed failed for %r", term)
                    break
                log.info("jobbank %r page %d: %d entries", term, page, len(jobs))
                yield from jobs
                if len(jobs) < PAGE_SIZE:
                    break

    def _search(self) -> Iterator[Job]:
        started = datetime.now(timezone.utc)
        succeeded = True
        for term in self.terms:
            seen: set[str] = set()
            total = 0
            failures = 0
            for page in range(1, SEARCH_MAX_PAGES + 1):
                # searchstring resolves keyword/occupation filters; term alone does not.
                url = f"{SEARCH_URL}?{urlencode({'searchstring': term, 'sort': 'D', 'page': page})}"
                try:
                    jobs, total = parse_search(_session.get_text(url))
                except Exception:
                    log.exception("jobbank search %r page %d failed", term, page)
                    succeeded = False
                    failures += 1
                    if failures >= 2 or (total and page * SEARCH_PAGE_SIZE >= total):
                        break
                    continue
                failures = 0
                fresh = [job for job in jobs if job.source_id not in seen]
                if jobs and not fresh:
                    succeeded = False
                    log.warning("jobbank search %r page %d repeated, stopping", term, page)
                    break
                log.info("jobbank search %r page %d: %d of %d entries", term, page, len(jobs), total)
                for job in fresh:
                    seen.add(job.source_id)
                    yield job
                if page * SEARCH_PAGE_SIZE >= total:
                    break
            else:
                succeeded = False
                log.warning("jobbank search %r hit page cap", term)
        if succeeded:
            self.store.set_meta(HTML_SEARCH_LAST_SUCCESS, started.isoformat())


def html_search_due(store: Store, now: datetime | None = None) -> bool:
    """Run the HTML pass once a day, retrying until every page succeeds."""
    last_success = store.get_meta(HTML_SEARCH_LAST_SUCCESS)
    if last_success is None:
        return True
    elapsed = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last_success)
    return elapsed >= timedelta(hours=20)


def parse_search(markup: str) -> tuple[list[Job], int]:
    soup = BeautifulSoup(markup, "html.parser")
    count = soup.select_one("#results-count")
    if not count:
        raise ValueError("Job Bank search has no results count")
    total = int(count.get_text(strip=True).replace(",", ""))
    jobs = []
    for row in soup.select("article[id^=article-]"):
        try:
            link = row.select_one("a.resultJobItem")
            title = row.select_one(".noctitle")
            match = re.search(r"/jobposting/(\d+)(?:[;/?]|$)", clean(link.get("href"))) if link else None
            if not match or not title or not clean(title.get_text()):
                raise ValueError("missing posting ID or title")
            for hidden in row.select(".wb-inv"):
                hidden.decompose()
            fields = {name: clean(node.get_text(" ", strip=True)) if (node := row.select_one(f".{name}")) else ""
                      for name in ("business", "location", "salary", "date")}
            try:
                posted = datetime.strptime(fields["date"], "%B %d, %Y").date()
            except ValueError:
                posted = None
            jobs.append(Job(
                source="jobbank", source_id=match[1], url=f"https://www.jobbank.gc.ca/jobsearch/jobposting/{match[1]}",
                title=clean(title.get_text(" ", strip=True)), company=fields["business"], location=fields["location"],
                salary=re.sub(r"^Salary\s*", "", fields["salary"]), posted_date=posted,
            ))
        except Exception:
            log.exception("jobbank malformed search row, skipping")
    return jobs, total


def parse_feed(xml: str) -> list[Job]:
    root = ET.fromstring(xml)
    jobs: list[Job] = []
    for entry in root.iter(f"{ATOM}entry"):
        link = next((el.get("href", "") for el in entry.iter(f"{ATOM}link")
                     if el.get("rel", "alternate") == "alternate"), "")
        posting_id = link.rstrip("/").rsplit("/", 1)[-1]
        title = clean(entry.findtext(f"{ATOM}title"))
        if not (link.startswith("https://www.jobbank.gc.ca/") and posting_id.isdigit() and title):
            continue
        summary = entry.findtext(f"{ATOM}summary") or ""
        jobs.append(Job(
            source="jobbank",
            source_id=posting_id,
            url=link,
            title=title,
            company=summary_field(summary, "Employer"),
            location=summary_field(summary, "Location"),
            posted_date=parse_date(entry.findtext(f"{ATOM}updated")),
            salary=summary_field(summary, "Salary"),
        ))
    return jobs


def summary_field(summary_html: str, label: str) -> str:
    """Summary is one HTML blob like `<strong>Location:</strong> Toronto (ON) <br />`."""
    m = re.search(rf"<strong>{label}:</strong>\s*(.*?)\s*(?:<br\s*/?>|$)", summary_html, re.I | re.S)
    return clean(html_to_text(m.group(1))) if m else ""


def fetch_description(job_url: str) -> str:
    """Detail page text: the requirements/responsibilities block."""
    page = _session.get_text(job_url)
    soup = BeautifulSoup(page, "html.parser")
    block = soup.select_one(".job-posting-detail-requirements")
    if block and (description := html_to_text(str(block))):
        return description
    if soup.select_one(".job-posting-content #externalJobLink[href]"):
        return ""  # External listings genuinely have no native requirements block.
    raise ValueError("Job Bank detail has no requirements or external posting link")


def check_liveness(url: str, source_id: str) -> Liveness:
    """Expired postings redirect to a 410 page."""
    return posting_liveness(_session, url, lambda r: "job-posting-detail-requirements" in r.text)
