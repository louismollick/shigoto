"""Oracle Recruiting Cloud posting checks, for apply URLs that lead to it.

The public candidate-experience API answers 200 with `count: 0` once a requisition is gone.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from shigoto.http import PoliteSession, posting_liveness
from shigoto.models import Liveness

_session = PoliteSession(min_interval=1.0)


def posting_found(data: object, requisition: str) -> bool:
    if not isinstance(data, dict) or data.get("count") != 1 or not isinstance(data.get("items"), list):
        return False
    return any(isinstance(item, dict) and str(item.get("Id", "")) == requisition and bool(item.get("Title"))
               for item in data["items"])


def posting_removed(data: object) -> bool:
    return isinstance(data, dict) and data.get("count") == 0 and data.get("items") == []


def check_liveness(url: str, source_id: str) -> Liveness:
    """`source_id` is `<site>:<requisition>`, as both come from the posting's URL."""
    host = urlsplit(url).hostname
    site, _, requisition = source_id.partition(":")
    if not host or not site or not requisition:
        return "unknown"
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f"?onlyData=true&finder=ById;Id=%22{requisition}%22,siteNumber={site}")
    return posting_liveness(_session, api, lambda r: posting_found(r.json(), requisition),
                            lambda r: posting_removed(r.json()))
