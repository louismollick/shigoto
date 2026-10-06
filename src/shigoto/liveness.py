"""Confirm stale postings are gone before marking jobs closed."""

from __future__ import annotations

import logging
from collections.abc import Callable

from shigoto.config import Config
from shigoto.db import SourceLink, Store, now_iso
from shigoto.models import Liveness
from shigoto.sources import ats, gcjobs, jobbank, successfactors, workday

log = logging.getLogger(__name__)
_CHECKS: dict[str, Callable[[str, str], Liveness]] = {
    "workday": workday.check_liveness,
    "greenhouse": ats.greenhouse_liveness,
    "lever": ats.lever_liveness,
    "smartrecruiters": ats.smartrecruiters_liveness,
    "ashby": ats.ashby_liveness,
    "successfactors": successfactors.check_liveness,
    "gcjobs": gcjobs.check_liveness,
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
    checked = closed = 0
    for target in store.liveness_candidates(now, config.liveness_max_per_run):
        results = [check_source(link) for link in target.sources]
        result: Liveness = "gone" if all(value == "gone" for value in results) else (
            "alive" if "alive" in results else "unknown"
        )
        store.record_liveness(target.job_id, result, now)
        checked += 1
        closed += result == "gone"
    log.info("liveness: %d checked, %d confirmed closed", checked, closed)
    return {"liveness_checked": checked, "closed": closed}
