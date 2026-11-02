"""Confirm postings are gone before marking jobs closed.

The employer settles a job when it answers: its full listing (Workday, Greenhouse, ...), or the
page behind an aggregator's apply URL. A posting that dropped off a listing whose board still
answers counts as gone even when its direct check can't say. When the employer can't answer,
every checkable aggregator must agree, because a listing ending often doesn't mean the job did.
The exception is an Indeed posting hosted on Indeed, where the employer has no other page.

Indeed is checked in batches up front. Other sources cost one request per posting, capped by
`liveness_max_per_run`; LinkedIn checks stop for the run at the first inconclusive answer,
which is usually a rate limit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from shigoto.config import Config
from shigoto.db import FULL_LISTING_SOURCES, LivenessTarget, Store, now_iso
from shigoto.employer import employer_link
from shigoto.models import Liveness, SourceLink
from shigoto.sources import ats, indeed, jobbank, linkedin_detail, oracle, successfactors, workday

log = logging.getLogger(__name__)
_CHECKS: dict[str, Callable[[str, str], Liveness]] = {
    "workday": workday.check_liveness,
    "greenhouse": ats.greenhouse_liveness,
    "lever": ats.lever_liveness,
    "smartrecruiters": ats.smartrecruiters_liveness,
    "ashby": ats.ashby_liveness,
    "successfactors": successfactors.check_liveness,
    "oracle": oracle.check_liveness,
    "jobbank": jobbank.check_liveness,
    "linkedin": linkedin_detail.check_liveness,
}
EMPLOYER_SOURCES = FULL_LISTING_SOURCES | {"oracle"}


def reset_robots() -> None:
    for module in (ats, jobbank, oracle, successfactors, workday):
        module._session.reset_robots()


def check_source(link: SourceLink) -> Liveness:
    try:
        check = _CHECKS.get(link.source)
        return check(link.url, link.source_id) if check else "unknown"
    except Exception:
        log.exception("%s liveness check failed for %s", link.source, link.source_id)
        return "unknown"


def employer_pages(target: LivenessTarget) -> list[SourceLink]:
    """Pages behind the job's apply URLs. A job on the employer's own listing already has its check."""
    if any(link.source in EMPLOYER_SOURCES for link in target.sources):
        return []
    return list({page.url: page for url in target.apply_urls if (page := employer_link(url))}.values())


def check_liveness(store: Store, config: Config, budget: int | None = None) -> dict[str, int]:
    """Check due jobs with `budget` per-posting requests, default `liveness_max_per_run`."""
    now = now_iso()
    targets = store.liveness_candidates(now)
    indeed_ids = [link.source_id for target in targets for link in target.sources if link.source == "indeed"]
    answers = indeed.check_liveness(indeed_ids) if indeed_ids else {}
    for source_id, found in answers.items():
        if found.apply_url or found.hosted:
            store.record_apply_url("indeed", source_id, found.apply_url)
    targets = store.liveness_candidates(now)  # now with Indeed's current apply URLs
    budget = config.liveness_max_per_run if budget is None else budget
    linkedin_blocked = False
    checked = closed = 0
    for target in targets:
        # No links to check: all are uncheckable, so 30 days unseen is the only evidence.
        result: Liveness | None = "gone" if not target.sources and target.long_unseen else "unknown"
        by_employer = False
        spent = False
        links = sorted([*target.sources, *employer_pages(target)],
                       key=lambda link: (link.source not in EMPLOYER_SOURCES, link.source != "indeed"))
        for link in links:
            answer = answers.get(link.source_id) if link.source == "indeed" else None
            if link.source == "indeed":
                value: Liveness = answer.liveness if answer else "unknown"
            elif budget == 0 or (link.source == "linkedin" and linkedin_blocked):
                result = None  # out of request budget
                break
            else:
                budget -= 1
                spent = True
                value = check_source(link)
                linkedin_blocked |= link.source == "linkedin" and value == "unknown"
                if (value == "unknown" and link in target.sources and link.source in FULL_LISTING_SOURCES
                        and store.dropped_from_listing(link, now)):
                    value = "gone"
            store.record_link(link, value, now)
            employer = link.source in EMPLOYER_SOURCES
            if employer and value == "unknown":
                continue  # fall back on the aggregators
            result = value
            if employer or value != "gone" or (answer and answer.hosted):
                by_employer = employer and value == "gone"
                break
        if result is None and not spent:
            continue  # retry next run without spending today's check
        # A job that spent requests rotates back even when unsettled, or it would keep the
        # front of the queue and use up the budget of every run.
        store.record_liveness(target.job_id, result or "unknown", now, by_employer=by_employer)
        checked += 1
        closed += result == "gone"
    store.commit()
    log.info("liveness: %d checked, %d confirmed closed", checked, closed)
    return {"liveness_checked": checked, "closed": closed}
