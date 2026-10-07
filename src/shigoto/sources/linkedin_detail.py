"""Fetch a LinkedIn job description from the public guest endpoint.

JobSpy's search results don't include descriptions, and its `fetch_description` option
costs one request per result. We instead fetch descriptions only for new LinkedIn jobs
that no other source already described, capped per run and spaced out (see enrich.py).
"""

from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from shigoto.http import PoliteSession, RateLimited
from shigoto.models import Liveness
from shigoto.text import html_to_text

# Retries are disabled: on a 429 we want to stop for this run, not hammer LinkedIn.
# Description fetches add their own configured delay on top of this floor.
_session = PoliteSession(min_interval=10, retries=0)


def linkedin_job_id(source_id: str) -> str:
    return source_id.removeprefix("li-")


def posting_url(source_id: str) -> str:
    return f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{linkedin_job_id(source_id)}"


def fetch_description(source_id: str) -> str:
    page = _session.get_text(posting_url(source_id))
    block = BeautifulSoup(page, "html.parser").select_one(".show-more-less-html__markup")
    return html_to_text(str(block)) if block else ""


def check_liveness(url: str, source_id: str) -> Liveness:
    """A 404/410 or the "No longer accepting applications" banner proves the posting closed.

    Rate limits and other errors are unknown; the caller stops LinkedIn checks for the run.
    """
    try:
        page = _session.get_text(posting_url(source_id))
    except requests.HTTPError as exc:
        return "gone" if exc.response is not None and exc.response.status_code in (404, 410) else "unknown"
    except (requests.RequestException, RateLimited):
        return "unknown"
    soup = BeautifulSoup(page, "html.parser")
    if soup.select_one("figure.closed-job"):
        return "gone"
    return "alive" if soup.select_one(".top-card-layout__title") else "unknown"
