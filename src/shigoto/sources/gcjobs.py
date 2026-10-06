"""Public GC Jobs HTML search, using the same second-part request as jobSearch.js.

No login or JavaScript is needed. Paginate the public board, keeping full locations.
Closing dates and language requirements stay in the description: Job has no closing
date field, and a deadline must never be used as a posting date.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

from shigoto.http import PoliteSession, posting_liveness
from shigoto.models import Job, Liveness
from shigoto.normalize import title_matches
from shigoto.text import clean, html_to_text, parse_date

log = logging.getLogger(__name__)
ROOT = "https://emploisfp-psjobs.cfp-psc.gc.ca"
SEARCH_URL = f"{ROOT}/psrs-srfp/applicant/page2440"
DETAIL_URL = f"{ROOT}/psrs-srfp/applicant/page1800"
MAX_PAGES = 40
_session = PoliteSession(min_interval=2.0, timeout=90.0, respect_robots=True, robots_http_fallback=ROOT)


def parse_results(markup: str) -> list[Job]:
    soup = BeautifulSoup(markup, "html.parser")
    if not soup.select_one("#searchResults"):
        raise ValueError("GC Jobs did not return its search results fragment")
    jobs = []
    for row in soup.select("li.searchResult"):
        link = row.select_one("a[href*=page1800]")
        cells = row.select(".tableCell")
        if not link or not cells:
            continue
        posting_id = parse_qs(urlparse(clean(link.get("href"))).query).get("poster", [""])[0]
        fields = [clean(line) for line in cells[0].get_text("\n").splitlines() if clean(line)]
        if not posting_id.isdigit() or len(fields) < 3:
            continue
        metadata = html_to_text(str(cells[1])) if len(cells) > 1 else ""
        jobs.append(Job(
            source="gcjobs", source_id=posting_id,
            url=f"{DETAIL_URL}?{urlencode({'poster': posting_id, 'toggleLanguage': 'en'})}",
            title=clean(link.get_text(" ", strip=True)), company=fields[1], location="; ".join(fields[2:]),
            description="\n".join([fields[0], metadata]).strip(),
            salary=next((line for line in metadata.splitlines() if "$" in line), ""),
        ))
    return jobs


def next_page(markup: str, current: int) -> int | None:
    soup = BeautifulSoup(markup, "html.parser")
    for link in soup.select("a[href*=requestedPage]"):
        query = parse_qs(urlparse(clean(link.get("href"))).query)
        page = query.get("requestedPage", [""])[0]
        if page.isdigit() and int(page) == current + 1:
            return int(page)
    return None


def parse_detail(markup: str) -> tuple[str, str]:
    """Poster text, including language requirements, and any external posting link."""
    soup = BeautifulSoup(markup, "html.parser")
    block = soup.select_one("main, .jobdescription, [itemprop=description]")
    if not block:
        raise ValueError("GC Jobs detail has no posting content")
    external = ""
    heading = block.select_one("h1")
    if heading and "You will leave" in heading.get_text():
        link = block.select_one("a[href^=http]")
        external = clean(link.get("href")) if link else ""
    for node in block.select("script, style, nav, .breadcrumb, .pagedetails"):
        node.decompose()
    return html_to_text(str(block)), external


class GCJobsSource:
    name = "gcjobs"

    def __init__(self, title_keywords: list[str], is_target: Callable[[str], bool]) -> None:
        self.title_keywords = title_keywords
        self.is_target = is_target

    def _get_text(self, url: str) -> str:
        return _session.get_text(url)

    def fetch(self) -> Iterator[Job]:
        seen: set[str] = set()
        page = 1
        for _ in range(MAX_PAGES):
            params = {"toggleLanguage": "en", "isSecondPartOfPage": "1", "isInitialNetworkCheck": "1",
                      "tab": "1", "requestedPage": str(page), "fromPage": str(page - 1)}
            try:
                markup = self._get_text(f"{SEARCH_URL}?{urlencode(params)}")
                jobs = parse_results(markup)
            except Exception:
                log.exception("gcjobs search page %d failed", page)
                return
            log.info("gcjobs page %d: %d listings", page, len(jobs))
            fresh = [j for j in jobs if j.source_id not in seen]
            if jobs and not fresh:
                log.warning("gcjobs page %d repeated earlier postings, stopping", page)
                return
            for job in fresh:
                seen.add(job.source_id)
                if not title_matches(job.title, self.title_keywords) or not self.is_target(job.location):
                    continue
                try:
                    detail, external = parse_detail(self._get_text(job.url))
                    if external:
                        detail, _ = parse_detail(self._get_text(external))
                        job.description += f"\nExternal posting: {external}"
                    job.description += f"\n\n{detail}"
                    posted = re.search(r"Date posted:\s*(\d{4}-\d{2}-\d{2})", detail)
                    job.posted_date = parse_date(posted[1]) if posted else None
                except Exception:
                    job.description = ""  # Preserve an existing description when detail retrieval fails.
                    log.exception("gcjobs detail %s failed", job.source_id)
                yield job
            following = next_page(markup, page)
            if following is None:
                return
            page = following
        log.warning("gcjobs hit page cap")


def check_liveness(url: str, source_id: str) -> Liveness:
    def is_alive(response: requests.Response) -> bool:
        soup = BeautifulSoup(response.text, "html.parser")
        return bool(soup.select_one("main h1") and parse_detail(response.text)[0])

    return posting_liveness(_session, url, is_alive)
