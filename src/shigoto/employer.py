"""Find the employer's own posting behind an aggregator's apply URL.

Aggregators keep listing postings employers removed, and sometimes drop ones still open, so
the employer's page is the best evidence and the best link. Only ATS hosts recognized here
are ever requested: apply URLs come from scraped data, so everything else is display-only.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from shigoto.models import SourceLink

_AGGREGATORS = (".indeed.com", ".ziprecruiter.com", ".linkedin.com")
_GREENHOUSE = re.compile(r"/([^/]+)/jobs/(\d+)/?")
_LEVER = re.compile(r"/([^/]+)/([0-9a-f]{8}-[0-9a-f-]{27})(?:/.*)?")
_ORACLE = re.compile(r"/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/(?:job|jobs/preview|requisitions/preview)/(\d+)/?")


def url_host(url: str) -> str:
    """The lowercase host of an http(s) URL, blank for anything else."""
    try:
        parts = urlsplit(url)
        return (parts.hostname or "").casefold() if parts.scheme in ("http", "https") else ""
    except ValueError:
        return ""


def external_apply_url(url: str) -> str:
    """`url` if it is a web page outside the aggregators, otherwise blank."""
    host = url_host(url)
    return url if host and not f".{host}".endswith(_AGGREGATORS) else ""


def employer_link(apply_url: str) -> SourceLink | None:
    """The check for an apply URL's ATS, or None when we can't check that host or URL shape."""
    host = url_host(apply_url)
    path = urlsplit(apply_url).path if host else ""
    if host.endswith(".myworkdayjobs.com") and "/job/" in path:
        # Apply-flow URLs add /apply..., which would turn the detail lookup into a false 404.
        return SourceLink("workday", f"{host}:", re.sub(r"/apply(?:/.*)?$", "", f"https://{host}{path}"))
    if host.endswith(".greenhouse.io") and (match := _GREENHOUSE.fullmatch(path)):
        return SourceLink("greenhouse", f"{match[1]}:{match[2]}", apply_url)
    if host == "jobs.lever.co" and (match := _LEVER.fullmatch(path)):
        return SourceLink("lever", f"{match[1]}:{match[2]}", apply_url)
    if host.endswith(".oraclecloud.com") and (match := _ORACLE.fullmatch(path)):
        return SourceLink("oracle", f"{match[1]}:{match[2]}", f"https://{host}{path}")
    if host.endswith(".jobs.hr.cloud.sap") and path.startswith("/job/"):
        return SourceLink("successfactors", f"{host}:", f"https://{host}{path}")
    return None
