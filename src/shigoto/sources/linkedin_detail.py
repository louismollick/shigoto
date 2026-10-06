"""Fetch a LinkedIn job description from the public guest endpoint.

JobSpy's search results don't include descriptions, and its `fetch_description` option
costs one request per result. We instead fetch descriptions only for new LinkedIn jobs
that no other source already described, capped per run and spaced out (see enrich.py).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from shigoto.http import PoliteSession
from shigoto.text import html_to_text

# Retries are disabled: on a 429 we want to stop for this run, not hammer LinkedIn.
_session = PoliteSession(min_interval=0, retries=0)


def linkedin_job_id(source_id: str) -> str:
    return source_id.removeprefix("li-")


def fetch_description(source_id: str) -> str:
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{linkedin_job_id(source_id)}"
    page = _session.get_text(url)
    block = BeautifulSoup(page, "html.parser").select_one(".show-more-less-html__markup")
    return html_to_text(str(block)) if block else ""
