"""Find the employer's own posting behind an aggregator's apply URL.

Aggregators keep listing postings employers removed, and sometimes drop ones still open, so
the employer's page is the best evidence and the best link. Only ATS hosts recognized here
are ever requested: apply URLs come from scraped data, so everything else is display-only.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from shigoto.models import SourceLink
from shigoto.normalize import norm_company

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


_WORKDAY_ID = re.compile(r".+_([a-z]*\d[\w-]*)", re.I)  # Quality-Assurance-Technician_R820542
# Job numbers and UUIDs in a path; years are skipped, since blog-style paths start /2026/10/20/<slug>.
_PATH_ID = re.compile(r"(?!(?:19|20)\d\d$)\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_LEADING_ID = re.compile(r"(\d{5,}|[0-9a-f]{12,})-.*", re.I)  # SmartRecruiters, Breezy: <id>-<slug>
_ID_PARAMS = ("jobid", "jobopeningid", "opportunityid", "rid", "j")
_TENANT_PARAMS = ("cid", "clid", "org", "c")  # ADP, Njoyn, Taleo and Cornerstone ids are per tenant


def requisitions(url: str, company: str) -> set[str]:
    """Keys for the employer requisition behind an employer page: postings that share a key and a
    title are one opening. Empty for aggregators and pages without a recognizable job id.

    Keys are host-scoped (`host:id`), since ids from different systems can collide. A numeric id
    of 5+ digits also yields `company#id`, which matches one requisition shown on two of the
    employer's sites (pepsicojobs.com and PepsiCo's iCIMS). Slugs are ignored: aggregators link
    the same requisition with different slugs and /apply suffixes.
    """
    host = url_host(url).removeprefix("www.")
    if not external_apply_url(url):
        return set()
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    params = {name.casefold(): value for name, value in parse_qsl(parts.query)}
    job_id, scope = "", host
    if host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")):
        # Tenant from the host, or from /recruiting/<tenant>/ on the shared myworkdaysite.com.
        scope = "workday:" + (segments[1] if host.endswith(".myworkdaysite.com") and len(segments) > 1
                              else host.split(".")[0])
        after_job = segments[segments.index("job") + 1:] if "job" in segments else []
        job_id = next((m[1] for segment in reversed(after_job) if (m := _WORKDAY_ID.fullmatch(segment))), "")
    elif host.endswith(".greenhouse.io") or "gh_jid" in params:
        scope = "greenhouse"
        job_id = params.get("gh_jid") or next((s for s in reversed(segments) if s.isdigit()), "")
    elif name := next((name for name in _ID_PARAMS if params.get(name)), None):
        scope = ":".join([host, *(params[t] for t in _TENANT_PARAMS if params.get(t))])
        job_id = params[name]
    elif "j" in segments[:-1]:  # Workable, CareerPlug: /j/<code>
        job_id = segments[segments.index("j") + 1]
    elif host.endswith(".applytojob.com") and "apply" in segments[:-1]:
        job_id = segments[segments.index("apply") + 1]
    else:
        job_id = next((s for s in reversed(segments) if _PATH_ID.fullmatch(s)), "")
        if not job_id and segments and (match := _LEADING_ID.fullmatch(segments[-1])):
            job_id = match[1]
        if host == "jobs.dayforcehcm.com" and len(segments) > 1:
            scope = f"{host}:{segments[1].casefold()}"  # /<locale>/<tenant>/...: ids are per tenant
    if not job_id:
        return set()
    job_id = job_id.casefold()
    keys = {f"{scope}:{job_id}"}
    if job_id.isdigit() and len(job_id) >= 5 and (name := norm_company(company)):
        keys.add(f"{name}#{job_id}")
    return keys
