"""Workday adapter (ported from Career Ops' providers/workday.mjs, simplified).

Uses the public CXS API behind `https://<tenant>.<wdN>.myworkdayjobs.com/<site>`:
  POST /wday/cxs/<tenant>/<site>/jobs   {limit, offset, searchText, appliedFacets}
  GET  /wday/cxs/<tenant>/<site>/job/... (detail: description + full location list)

Per tenant we first read the facets once to find a location filter: the "Canada"
country value if the tenant has one, otherwise the location values naming one of our
cities. Then each search term runs server-side under that filter, and titles are
checked against `title_keywords` before fetching details.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from shigoto.config import WorkdayBoard
from shigoto.http import PoliteSession, posting_liveness
from shigoto.models import Job, Liveness
from shigoto.normalize import title_matches
from shigoto.text import clean, html_to_text, parse_date

log = logging.getLogger(__name__)

PAGE_SIZE = 20
MAX_PAGES = 5

# Workday's WAF rate-limits bursts; Career Ops uses 250ms between pages.
_session = PoliteSession(min_interval=0.5)


@dataclass(frozen=True)
class Endpoint:
    jobs_api: str
    detail_base: str
    site_base: str


def endpoint(url: str) -> Endpoint:
    parsed = urlparse(url)
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    parts = [p for p in parsed.path.split("/") if p]
    site = parts[-1]  # path may carry a locale prefix like /en-US/
    root = f"https://{parsed.hostname}"
    return Endpoint(f"{root}/wday/cxs/{tenant}/{site}/jobs", f"{root}/wday/cxs/{tenant}/{site}", f"{root}/{site}")


def iter_facet_values(facets: list[dict[str, Any]], param: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (facetParameter, value) for every selectable facet value, including nested groups."""
    for facet in facets:
        p = facet.get("facetParameter") or param
        for value in facet.get("values") or []:
            if "values" in value:
                yield from iter_facet_values([value], p)
            elif value.get("id"):
                yield p, value


def location_filter(facets: list[dict[str, Any]], is_target: Callable[[str], bool]) -> dict[str, list[str]] | None:
    """Canada country facet if present, else location facet values in a target city; None if neither."""
    values = list(iter_facet_values(facets))
    for param, value in values:
        if value.get("descriptor") == "Canada":
            return {param: [value["id"]]}
    matched: dict[str, list[str]] = {}
    for param, value in values:
        if is_target(value.get("descriptor", "")):
            matched.setdefault(param, []).append(value["id"])
    return matched or None


class WorkdaySource:
    name = "workday"

    def __init__(self, boards: list[WorkdayBoard], terms: list[str], title_keywords: list[str],
                 is_target: Callable[[str], bool]) -> None:
        self.boards = boards
        self.terms = terms
        self.title_keywords = title_keywords
        self.is_target = is_target

    def fetch(self) -> Iterator[Job]:
        for board in self.boards:
            try:
                yield from self._fetch_board(board)
            except Exception:
                log.exception("workday %s failed", board.name)

    def _fetch_board(self, board: WorkdayBoard) -> Iterator[Job]:
        ep = endpoint(board.url)
        first = _session.post_json(ep.jobs_api, {"limit": 1, "offset": 0, "searchText": "", "appliedFacets": {}})
        facets = location_filter(first.get("facets", []), self.is_target)
        if facets is None:
            log.info("workday %s: no Canada/target-city facet, skipping", board.name)
            return
        paths: dict[str, dict[str, Any]] = {}
        for term in self.terms:
            for page in range(MAX_PAGES):
                data = _session.post_json(ep.jobs_api, {
                    "limit": PAGE_SIZE, "offset": page * PAGE_SIZE, "searchText": term, "appliedFacets": facets,
                })
                postings = data.get("jobPostings") or []
                for p in postings:
                    # searchText also matches descriptions, so re-check the title
                    if p.get("externalPath") and title_matches(p.get("title", ""), self.title_keywords):
                        paths.setdefault(p["externalPath"], p)
                if len(postings) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= data.get("total", 0):
                    break
        log.info("workday %s: %d postings match terms", board.name, len(paths))
        for path in paths:
            try:
                job = self._detail(board, ep, path)
            except Exception:
                log.exception("workday detail %s failed", path)
                continue
            if job:
                yield job

    def _detail(self, board: WorkdayBoard, ep: Endpoint, path: str) -> Job | None:
        info = _session.get_json(f"{ep.detail_base}{path}").get("jobPostingInfo") or {}
        locations = [info.get("location") or "", *(info.get("additionalLocations") or [])]
        location = "; ".join(clean(loc) for loc in locations if loc)
        if not self.is_target(location):
            return None
        return Job(
            source="workday",
            source_id=f"{urlparse(board.url).hostname}:{info.get('jobReqId') or path}",
            url=info.get("externalUrl") or f"{ep.site_base}{path}",
            title=clean(info.get("title")),
            company=board.name,
            location=location,
            description=html_to_text(info.get("jobDescription") or ""),
            posted_date=parse_date(info.get("startDate")),
            job_type=clean(info.get("timeType")),
        )


def check_liveness(url: str, source_id: str) -> Liveness:
    """Convert the public posting path, with or without a locale, to its CXS detail URL."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "job" not in parts or not parsed.hostname or not parsed.hostname.endswith(".myworkdayjobs.com"):
        return "unknown"
    index = parts.index("job")
    if index < 1 or index == len(parts) - 1:
        return "unknown"
    ep = endpoint(f"https://{parsed.hostname}/{parts[index - 1]}")
    detail_url = f"{ep.detail_base}/{'/'.join(parts[index:])}"
    return posting_liveness(_session, detail_url, lambda r: workday_alive(r.json()))


def workday_alive(data: object) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("jobPostingInfo"), dict)
            and bool(data["jobPostingInfo"].get("title")) and bool(data["jobPostingInfo"].get("jobReqId")))
