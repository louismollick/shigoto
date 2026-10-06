"""Canada Job Bank adapter (ported from Career Ops' providers/jobbankca.mjs).

Public Atom feed, no auth. robots.txt sets `Crawl-delay: 5`, which we honor for every
request including detail pages. `locationstring` doesn't filter reliably, so we search
nationwide per keyword and let normalize() keep only target cities. The feed only
lists recent postings, which is fine because we poll every few hours.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from shigoto.http import PoliteSession
from shigoto.models import Job
from shigoto.text import clean, html_to_text, parse_date

log = logging.getLogger(__name__)

FEED_URL = "https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed"
PAGE_SIZE = 100
MAX_PAGES = 5
CRAWL_DELAY = 5.0
ATOM = "{http://www.w3.org/2005/Atom}"

_session = PoliteSession(min_interval=CRAWL_DELAY)


class JobBankSource:
    name = "jobbank"

    def __init__(self, terms: list[str]) -> None:
        self.terms = terms

    def fetch(self) -> Iterator[Job]:
        for term in self.terms:
            for page in range(1, MAX_PAGES + 1):
                url = f"{FEED_URL}?{urlencode({'searchstring': term, 'locationstring': '', 'page': page})}"
                try:
                    xml = _session.get_text(url)
                except Exception:
                    log.exception("jobbank feed failed for %r", term)
                    break
                jobs = parse_feed(xml)
                log.info("jobbank %r page %d: %d entries", term, page, len(jobs))
                yield from jobs
                if len(jobs) < PAGE_SIZE:
                    break


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
    block = BeautifulSoup(page, "html.parser").select_one(".job-posting-detail-requirements")
    return html_to_text(str(block)) if block else ""
