"""Confirm postings are gone before marking jobs closed.

Indeed is checked in batches up front. Other sources cost one request per posting,
capped by `liveness_max_per_run`; LinkedIn checks stop for the run at the first
inconclusive answer, which is usually a rate limit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from shigoto.config import Config
from shigoto.db import SourceLink, Store, now_iso
from shigoto.models import Liveness
from shigoto.sources import ats, gcjobs, indeed, jobbank, linkedin_detail, successfactors, workday

log = logging.getLogger(__name__)
_CHECKS: dict[str, Callable[[str, str], Liveness]] = {
    "workday": workday.check_liveness,
    "greenhouse": ats.greenhouse_liveness,
    "lever": ats.lever_liveness,
    "smartrecruiters": ats.smartrecruiters_liveness,
    "ashby": ats.ashby_liveness,
    "successfactors": successfactors.check_liveness,
    "gcjobs": gcjobs.check_liveness,
    "jobbank": jobbank.check_liveness,
    "linkedin": linkedin_detail.check_liveness,
}


def reset_robots() -> None:
    for module in (ats, gcjobs, jobbank, successfactors, workday):
        module._session.reset_robots()


def check_source(link: SourceLink) -> Liveness:
    try:
        check = _CHECKS.get(link.source)
        return check(link.url, link.source_id) if check else "unknown"
    except Exception:
        log.exception("%s liveness check failed for %s", link.source, link.source_id)
        return "unknown"


def check_liveness(store: Store, config: Config) -> dict[str, int]:
    now = now_iso()
    targets = store.liveness_candidates(now)
    indeed_results = indeed.check_liveness(
        [link.source_id for target in targets for link in target.sources if link.source == "indeed"])
    budget = config.liveness_max_per_run
    linkedin_blocked = False
    checked = closed = 0
    for target in targets:
        result: Liveness | None = "gone"  # None: out of request budget
        # Free Indeed answers first; any source that isn't gone settles the job.
        for link in sorted(target.sources, key=lambda link: link.source != "indeed"):
            if link.source == "indeed":
                value = indeed_results.get(link.source_id, "unknown")
            elif budget == 0 or (link.source == "linkedin" and linkedin_blocked):
                result = None
                break
            else:
                budget -= 1
                value = check_source(link)
                linkedin_blocked |= link.source == "linkedin" and value == "unknown"
            if value != "gone":
                result = value
                break
        if result is None:
            continue  # retry next run without spending today's check
        store.record_liveness(target.job_id, result, now)
        checked += 1
        closed += result == "gone"
    log.info("liveness: %d checked, %d confirmed closed", checked, closed)
    return {"liveness_checked": checked, "closed": closed}
