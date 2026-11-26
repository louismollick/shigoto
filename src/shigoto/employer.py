"""Find the employer's own posting behind an aggregator's apply URL.

Aggregators keep listing postings employers removed, and sometimes drop ones still open, so
the employer's page is the best evidence and the best link. Only ATS hosts recognized here
are ever requested: apply URLs come from scraped data, so everything else is display-only.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

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



_WORKDAY_ID = re.compile(r".+?_([a-z]*-?\d[\w-]*)", re.I)  # Quality-Assurance-Technician_R820542, _JR-102075
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_YEARLESS_NUMBER = re.compile(r"(?!(?:19|20)\d\d$)\d{4,}")
_LEADING_ID = re.compile(r"(\d{3,}|[0-9a-f]{12,})(?:-.*)?", re.I)  # SmartRecruiters, Breezy: <id>-<slug>
_LOCALE = re.compile(r"[a-z]{2}(?:-[a-z]{2})?", re.I)
# SmartRecruiters gives job boards their own id for a posting, so its ids prove a match but
# never prove two openings.
_NO_VETO_SCOPES = frozenset({"jobs.smartrecruiters.com"})


def _after(segments: list[str], marker: str) -> str:
    """The path segment after `marker`, blank when absent."""
    return segments[segments.index(marker) + 1] if marker in segments[:-1] else ""


def _number(value: str) -> str:
    return value if value.isdigit() else ""


def _code(value: str) -> str:
    """A short-link job code like Workable's D42B350581 or Jobvite's oGppAfwO, not a word like "search"."""
    return value if len(value) >= 7 and re.fullmatch(r"\w+", value) and re.search(r"[\dA-Z]", value) else ""


def requisition(url: str) -> str | None:
    """`scope|id` naming the employer requisition behind an employer page, or None.

    Postings that share it, a title and a city are one opening. Only known ATS link shapes,
    with ids of the shape that ATS uses, count: a generic guess can mistake a board, category or
    search page for a job. The scope includes the tenant wherever ids are only unique per employer.
    Slugs, locales, /apply suffixes and tracking parameters are ignored, since aggregators link
    one requisition each of those ways.
    """
    if not external_apply_url(url):
        return None
    host = url_host(url).removeprefix("www.")
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    params = {name.casefold(): value for name, value in parse_qsl(parts.query)}
    scope, job_id = "", ""
    if host.endswith(".myworkdayjobs.com") or host.endswith(".myworkdaysite.com"):
        # The tenant is in the host, or in /recruiting/<tenant>/ on the shared myworkdaysite.com.
        tenant = _after(segments, "recruiting") if host.endswith(".myworkdaysite.com") else host.split(".")[0]
        after_job = segments[segments.index("job") + 1:] if "job" in segments else []
        scope = f"workday:{tenant.casefold()}" if tenant else ""
        job_id = next((m[1] for segment in reversed(after_job) if (m := _WORKDAY_ID.fullmatch(segment))), "")
    elif "gh_jid" in params or host.endswith(".greenhouse.io"):
        scope, job_id = "greenhouse", _number(params.get("gh_jid") or _after(segments, "jobs"))
    elif host in ("jobs.lever.co", "jobs.ashbyhq.com", "ats.rippling.com"):
        scope, job_id = host, next((s for s in segments if _UUID.fullmatch(s)), "")
    elif host == "jobs.smartrecruiters.com" and len(segments) > 1 and (m := _LEADING_ID.fullmatch(segments[1])):
        scope, job_id = host, m[1]
    elif host.endswith(".icims.com"):
        scope, job_id = host, _number(_after(segments, "jobs"))
    elif host == "jobs.dayforcehcm.com" and len(segments) > 1 and _LOCALE.fullmatch(segments[0]):
        scope, job_id = f"dayforce:{segments[1].casefold()}", _number(_after(segments, "jobs"))  # /<locale>/<tenant>/...
    elif host.endswith(".adp.com") and params.get("cid"):
        scope, job_id = f"adp:{params['cid']}", _number(params.get("jobid", ""))
    elif host.endswith(".bamboohr.com"):
        scope, job_id = host, _number(_after(segments, "careers"))
    elif host.endswith(".oraclecloud.com"):  # One number per host, whatever the site: .../job/<id>, .../preview/<id>
        scope, job_id = host, _number(_after(segments, "preview") or _after(segments, "job"))
    elif host.endswith(".workable.com") or host.endswith(".careerplug.com"):
        scope, job_id = host, _code(_after(segments, "j"))
    elif host.endswith(".jobvite.com"):
        scope, job_id = "jobvite", _code(params.get("j") or _after(segments, "job"))
    elif host.endswith(".breezy.hr") and (m := _LEADING_ID.fullmatch(_after(segments, "p"))):
        scope, job_id = host, m[1]
    elif host.endswith(".applytojob.com"):
        scope, job_id = host, _code(_after(segments, "apply"))
    elif ".ultipro." in host:
        scope, job_id = host, params.get("opportunityid", "") if _UUID.fullmatch(params.get("opportunityid", "")) else ""
    elif host.endswith(".csod.com"):
        scope, job_id = host, _number(_after(segments, "requisition"))
    elif host.endswith(".njoyn.com") and params.get("clid"):
        scope, job_id = f"njoyn:{params['clid']}", params.get("jobid", "")
    elif "job" in segments and len(segments) > 2 and segments[-1].isdigit() and segments[-2].isdigit():
        scope, job_id = f"{host}:{segments[-2]}", segments[-1]  # Radancy: /job/<city>/<slug>/<tenant>/<id>
    elif _YEARLESS_NUMBER.fullmatch(_after(segments, "job")):
        scope, job_id = host, _after(segments, "job")  # Phenom: /<locale>/job/<id>[/<slug>]
    elif len(segments) == 3 and segments[0] == "job" and segments[2].isdigit() and len(segments[2]) >= 6:
        scope, job_id = host, segments[2]  # SuccessFactors career sites: /job/<slug>/<id>/
    if not scope or not job_id or not re.fullmatch(r"[\w-]+", job_id):
        return None
    case_sensitive = scope == "jobvite" or host.endswith(".applytojob.com")  # mixed-case short codes
    return f"{scope}|{job_id if case_sensitive else job_id.casefold()}"


def conflicts(keys: set[str], others: set[str]) -> bool:
    """Whether merging would add a second requisition in one ATS tenant: two openings, never one job.
    A row that already holds two ids in a tenant can still take postings with ids it holds."""
    def scopes(group: set[str]) -> set[str]:
        return {key.rsplit("|", 1)[0] for key in group} - _NO_VETO_SCOPES

    return bool(scopes(keys - others) & scopes(others - keys))
