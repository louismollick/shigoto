"""Confirm postings are gone before marking jobs closed.

An employer's full listing (Workday, Greenhouse, ...) is authoritative when it answers.
Indeed is checked in batches up front. Other sources cost one request per posting,
capped by `liveness_max_per_run`; LinkedIn checks stop for the run at the first
inconclusive answer, which is usually a rate limit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from shigoto.config import Config
from shigoto.db import FULL_LISTING_SOURCES, SourceLink, Store, now_iso
from shigoto.models import Liveness
from shigoto.sources import ats, indeed, jobbank, linkedin_detail, successfactors, workday

log = logging.getLogger(__name__)
_CHECKS: dict[str, Callable[[str, str], Liveness]] = {
    "workday": workday.check_liveness,
    "greenhouse": ats.greenhouse_liveness,
    "lever": ats.lever_liveness,
    "smartrecruiters": ats.smartrecruiters_liveness,
    "ashby": ats.ashby_liveness,
    "successfactors": successfactors.check_liveness,
    "jobbank": jobbank.check_liveness,
    "linkedin": linkedin_detail.check_liveness,
}


def reset_robots() -> None:
    for module in (ats, jobbank, successfactors, workday):
        module._session.reset_robots()


def check_source(link: SourceLink) -> Liveness:
    try:
        check = _CHECKS.get(link.source)
        return check(link.url, link.source_id) if check else "unknown"
    except Exception:
        log.exception("%s liveness check failed for %s", link.source, link.source_id)
        return "unknown"


def check_liveness(store: Store, config: Config, budget: int | None = None) -> dict[str, int]:
    """Check due jobs with `budget` per-posting requests, default `liveness_max_per_run`."""
    now = now_iso()
    targets = store.liveness_candidates(now)
    indeed_ids = [link.source_id for target in targets for link in target.sources if link.source == "indeed"]
    indeed_results = indeed.check_liveness(indeed_ids) if indeed_ids else {}
    budget = config.liveness_max_per_run if budget is None else budget
    linkedin_blocked = False
    checked = closed = 0
    for target in targets:
        # Empty sources: all links are uncheckable and have been unseen for 30 days.
        result: Liveness | None = "gone"  # None: out of request budget
        # The employer's own full listing settles the job either way, even when aggregators
        # still list a posting it removed. Then free Indeed answers; any other source that
        # isn't gone settles the job.
        for link in sorted(target.sources, key=lambda link: (link.source not in FULL_LISTING_SOURCES,
                                                             link.source != "indeed")):
            if link.source == "indeed":
                value = indeed_results.get(link.source_id, "unknown")
            elif budget == 0 or (link.source == "linkedin" and linkedin_blocked):
                result = None
                break
            else:
                budget -= 1
                value = check_source(link)
                linkedin_blocked |= link.source == "linkedin" and value == "unknown"
            if value != "gone" or link.source in FULL_LISTING_SOURCES:
                result = value
                break
        if result is None:
            continue  # retry next run without spending today's check
        store.record_liveness(target.job_id, result, now)
        checked += 1
        closed += result == "gone"
    log.info("liveness: %d checked, %d confirmed closed", checked, closed)
    return {"liveness_checked": checked, "closed": closed}
