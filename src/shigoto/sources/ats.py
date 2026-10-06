"""Public ATS board adapters (ported from Career Ops' providers/{greenhouse,lever,ashby,smartrecruiters}.mjs).

Greenhouse, Lever and Ashby return a company's whole board in one request, so we
filter titles by `ats_title_keywords`. SmartRecruiters supports server-side keyword +
country search, so it runs per search term like Workday.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from shigoto.config import SlugBoard
from shigoto.http import PoliteSession
from shigoto.models import Job
from shigoto.normalize import title_matches
from shigoto.text import clean, html_to_text, parse_date

log = logging.getLogger(__name__)

_session = PoliteSession(min_interval=1.0)


class BoardSource:
    """Whole-board ATS (Greenhouse/Lever/Ashby): one request per company."""

    def __init__(self, name: str, boards: list[SlugBoard], title_keywords: list[str],
                 fetch_board: Callable[[SlugBoard], list[Job]]) -> None:
        self.name = name
        self.boards = boards
        self.title_keywords = title_keywords
        self.fetch_board = fetch_board

    def fetch(self) -> Iterator[Job]:
        for board in self.boards:
            try:
                jobs = self.fetch_board(board)
            except Exception:
                log.exception("%s board %s failed", self.name, board.slug)
                continue
            kept = [j for j in jobs if title_matches(j.title, self.title_keywords)]
            log.info("%s %s: %d jobs, %d title matches", self.name, board.slug, len(jobs), len(kept))
            yield from kept


def greenhouse_board(board: SlugBoard) -> list[Job]:
    data = _session.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs?content=true")
    return [
        Job(
            source="greenhouse",
            source_id=f"{board.slug}:{j['id']}",
            url=j.get("absolute_url") or "",
            title=clean(j.get("title")),
            company=board.name,
            location=clean((j.get("location") or {}).get("name")),
            description=html_to_text(j.get("content") or ""),
            posted_date=parse_date(j.get("first_published") or j.get("updated_at")),
        )
        for j in data.get("jobs", [])
    ]


def lever_board(board: SlugBoard) -> list[Job]:
    data: list[dict[str, Any]] = _session.get_json(f"https://api.lever.co/v0/postings/{board.slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or [cats.get("location") or ""]
        lists = "\n\n".join(f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in j.get("lists", []))
        jobs.append(Job(
            source="lever",
            source_id=f"{board.slug}:{j['id']}",
            url=j.get("hostedUrl") or "",
            title=clean(j.get("text")),
            company=board.name,
            location="; ".join(clean(loc) for loc in locations if loc),
            description="\n\n".join(p for p in (j.get("descriptionPlain"), lists, j.get("additionalPlain")) if p),
            posted_date=parse_date(j.get("createdAt")),
            job_type=clean(cats.get("commitment")),
        ))
    return jobs


def ashby_board(board: SlugBoard) -> list[Job]:
    data = _session.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        jobs.append(Job(
            source="ashby",
            source_id=f"{board.slug}:{j['id']}",
            url=j.get("jobUrl") or "",
            title=clean(j.get("title")),
            company=board.name,
            location="; ".join(clean(loc) for loc in locations if loc),
            description=j.get("descriptionPlain") or "",
            posted_date=parse_date(j.get("publishedAt")),
            job_type=clean(j.get("employmentType")),
        ))
    return jobs


class SmartRecruitersSource:
    """Keyword search restricted to Canada; descriptions come from one detail call per
    posting, made only for postings located in a target city."""

    name = "smartrecruiters"
    API = "https://api.smartrecruiters.com/v1/companies"
    MAX_PAGES = 3

    def __init__(self, boards: list[SlugBoard], terms: list[str], in_target_city: Callable[[str], bool]) -> None:
        self.boards = boards
        self.terms = terms
        self.in_target_city = in_target_city

    def fetch(self) -> Iterator[Job]:
        for board in self.boards:
            seen: set[str] = set()
            for term in self.terms:
                try:
                    postings = self._search(board.slug, term)
                except Exception:
                    log.exception("smartrecruiters %s %r failed", board.slug, term)
                    continue
                for p in postings:
                    location = clean((p.get("location") or {}).get("fullLocation"))
                    if p["id"] in seen or not self.in_target_city(location):
                        continue
                    seen.add(p["id"])
                    try:
                        yield self._detail(board, p, location)
                    except Exception:
                        log.exception("smartrecruiters detail %s failed", p["id"])
            log.info("smartrecruiters %s: %d jobs in target cities", board.slug, len(seen))

    def _search(self, company: str, term: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in range(self.MAX_PAGES):
            data = _session.get_json(f"{self.API}/{company}/postings",
                                     params={"q": term, "country": "ca", "limit": 100, "offset": page * 100})
            content = data.get("content", [])
            results.extend(content)
            if len(results) >= data.get("totalFound", 0) or not content:
                break
        return results

    def _detail(self, board: SlugBoard, posting: dict[str, Any], location: str) -> Job:
        detail = _session.get_json(f"{self.API}/{board.slug}/postings/{posting['id']}")
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        description = "\n\n".join(
            f"{s.get('title', '')}\n{html_to_text(s.get('text', ''))}" for s in sections.values() if s.get("text")
        )
        return Job(
            source="smartrecruiters",
            source_id=f"{board.slug}:{posting['id']}",
            url=detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board.slug}/{posting['id']}",
            title=clean(posting.get("name")),
            company=board.name,
            location=location,
            description=description,
            posted_date=parse_date(posting.get("releasedDate")),
            job_type=clean((posting.get("typeOfEmployment") or {}).get("label")),
        )
