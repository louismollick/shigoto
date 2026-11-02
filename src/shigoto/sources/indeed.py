"""Batch-check Indeed postings through the mobile GraphQL API that JobSpy searches with.

Indeed's viewjob pages sit behind a bot challenge, but `jobData` reports an `expired`
flag and where applications go for up to 100 job keys per request. Keys Indeed omits from
the response stay unknown.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests
from jobspy.indeed.constant import headers as api_headers

from shigoto.employer import external_apply_url, url_host
from shigoto.http import PoliteSession, RateLimited
from shigoto.models import Liveness

log = logging.getLogger(__name__)

API_URL = "https://apis.indeed.com/graphql"
BATCH_SIZE = 100
_HEADERS = {**api_headers, "indeed-co": "CA", "indeed-locale": "en-CA"}
_session = PoliteSession(min_interval=1)


@dataclass(frozen=True)
class IndeedAnswer:
    """`hosted` means candidates apply on Indeed itself, so `expired` is the employer's own answer."""

    liveness: Liveness
    apply_url: str = ""
    hosted: bool = False


def check_liveness(source_ids: list[str]) -> dict[str, IndeedAnswer]:
    """Map JobSpy source IDs (`in-<key>`) to answers. Failed batches and omitted keys are left out."""
    by_key = {source_id.removeprefix("in-"): source_id for source_id in source_ids}
    keys = list(by_key)
    results: dict[str, IndeedAnswer] = {}
    for start in range(0, len(keys), BATCH_SIZE):
        batch = keys[start:start + BATCH_SIZE]
        query = "query { jobData(jobKeys: %s) { results { job { key expired recruit { viewJobUrl } } } } }" % json.dumps(batch)
        try:
            data = _session.post_json(API_URL, {"query": query}, headers=_HEADERS)
            jobs = [result["job"] for result in data["data"]["jobData"]["results"]]
        except (requests.RequestException, RateLimited, ValueError, TypeError, KeyError):
            log.exception("indeed liveness batch failed")
            continue
        for job in jobs:
            if isinstance(job, dict) and job.get("key") in by_key and isinstance(job.get("expired"), bool):
                recruit = job.get("recruit")
                view_url = recruit.get("viewJobUrl") if isinstance(recruit, dict) else ""
                view_url = view_url if isinstance(view_url, str) else ""
                results[by_key[job["key"]]] = IndeedAnswer("gone" if job["expired"] else "alive",
                                                           external_apply_url(view_url),
                                                           f".{url_host(view_url)}".endswith(".indeed.com"))
    return results
