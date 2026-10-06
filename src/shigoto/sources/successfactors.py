"""SuccessFactors RMK tile search (ported from Career Ops).

Search terms run server-side. Only title/city matches get a detail request.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from datetime import date, datetime
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from shigoto.config import SuccessFactorsBoard
from shigoto.http import PoliteSession, posting_liveness
from shigoto.models import Job, Liveness
from shigoto.normalize import title_matches
from shigoto.text import clean, html_to_text, parse_date

log = logging.getLogger(__name__)
_session = PoliteSession(min_interval=1.0, respect_robots=True)
MAX_PAGES = 40


def board_base(url: str) -> str:
    """Keep brand prefixes, stripping search/category endpoints."""
    parsed = urlparse(url)
    path = re.sub(r"/go/[^/]+/\d+(?:/\d+)?/?$", "", parsed.path, flags=re.I)
    path = re.sub(r"/(?:search|tile-search-results)/?$", "", path, flags=re.I)
    return f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/')}"


def city_from_slug(path: str, title: str) -> str:
    match = re.search(r"/job/([^/]+)/", unquote(path))
    words = re.findall(r"[^\W_]+", title.lower())
    if not match or not words:
        return ""
    slug = match[1].lower()
    anchor = re.search(r"[\W_]+".join(re.escape(w) for w in words[:2]), slug)
    return " ".join(re.findall(r"[^\W_]+", slug[:anchor.start()])).title() if anchor else ""


def sf_date(value: object) -> date | None:
    """ISO or RMK English dates."""
    result = parse_date(value)
    if result:
        return result
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%a %b %d %H:%M:%S UTC %Y"):
        try:
            return datetime.strptime(clean(value), fmt).date()
        except ValueError:
            continue
    return None


def parse_tiles(markup: str, board: SuccessFactorsBoard) -> list[Job]:
    soup = BeautifulSoup(markup, "html.parser")
    jobs: dict[str, Job] = {}
    parsed = urlparse(board.url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    for tile in soup.select("li.job-tile"):
        match = re.search(r"\bjob-id-(\d+)\b", " ".join(tile.get_attribute_list("class")))
        link = tile.select_one("a.jobTitle-link")
        path = clean(tile.get("data-url"))
        if not match or not link or not path:
            continue
        title = clean(link.get_text(" ", strip=True))
        if not title:
            continue
        location = tile.select_one('[id$="-section-location-value"], [id$="-section-city-value"]')
        posted = tile.select_one('[id$="-section-date-value"]')
        jobs.setdefault(match[1], Job(
            source="successfactors", source_id=f"{board_base(board.url)}:{match[1]}",
            url=urljoin(origin, path), title=title, company=board.name,
            location=clean(location.get_text(" ", strip=True)) if location else city_from_slug(path, title),
            posted_date=sf_date(posted.get_text(strip=True)) if posted else None,
        ))
    return list(jobs.values())


def parse_detail(markup: str) -> tuple[str, date | None]:
    soup = BeautifulSoup(markup, "html.parser")
    block = soup.select_one(".jobdescription, [itemprop=description], #job-description")
    posted = soup.select_one("[itemprop=datePosted], .jobDate")
    return (html_to_text(str(block)) if block else "",
            sf_date(posted.get("content") or posted.get_text(strip=True)) if posted else None)


class SuccessFactorsSource:
    name = "successfactors"

    def __init__(self, boards: list[SuccessFactorsBoard], terms: list[str], title_keywords: list[str],
                 is_target: Callable[[str], bool]) -> None:
        self.boards = boards
        self.terms = terms
        self.title_keywords = title_keywords
        self.is_target = is_target

    def fetch(self) -> Iterator[Job]:
        for board in self.boards:
            try:
                jobs = self._list_board(board)
            except Exception:
                log.exception("successfactors %s failed", board.name)
                continue
            kept = [j for j in jobs if title_matches(j.title, self.title_keywords) and self.is_target(j.location)]
            log.info("successfactors %s: %d listings, %d title/city matches", board.name, len(jobs), len(kept))
            for job in kept:
                try:
                    job.description, posted = parse_detail(_session.get_text(job.url))
                    job.posted_date = posted or job.posted_date
                except Exception:
                    log.exception("successfactors detail %s failed", job.url)
                yield job

    def _list_board(self, board: SuccessFactorsBoard) -> list[Job]:
        base = board_base(board.url)
        jobs: dict[str, Job] = {}
        for term in self.terms or [""]:
            offset = 0
            seen: set[str] = set()
            for page in range(MAX_PAGES):
                markup = _session.get_text(f"{base}/tile-search-results/?{urlencode({'q': term, 'locationsearch': 'Canada', 'startrow': offset})}")
                rows = parse_tiles(markup, board)
                fresh = [j for j in rows if j.source_id not in seen]
                if not fresh:
                    break
                for job in fresh:
                    seen.add(job.source_id)
                    jobs.setdefault(job.source_id, job)
                offset += len(rows)
            else:
                log.warning("successfactors %s %r hit page cap", board.name, term)
        return list(jobs.values())


def check_liveness(url: str, source_id: str) -> Liveness:
    # RMK's generic error-page redirects are inconclusive and robots-disallowed.
    return posting_liveness(_session, url, lambda r: bool(parse_detail(r.text)[0]))
