"""Fetch missing ZipRecruiter descriptions through a fresh proxied TLS session per job."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from curl_cffi.requests.exceptions import RequestException
from jobspy.util import create_session
from jobspy.ziprecruiter.util import page_values

from shigoto.http import proxy_url
from shigoto.text import html_to_text


def fetch_description(url: str) -> tuple[str, str]:
    """Return (description, salary); unrecognized pages stay pending for another run."""
    session = create_session(proxies=proxy_url(), is_tls=True)
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
        host = urlparse(response.url).hostname or ""
        if host != "ziprecruiter.com" and not host.endswith(".ziprecruiter.com"):
            raise ValueError("ZipRecruiter redirected to an external host")
        response.raise_for_status()
        page = response.text
    except RequestException as exc:
        # Keep curl failures compatible with enrichment's requests error handling.
        status = exc.response.status_code if exc.response is not None else 0
        if isinstance(status, int) and 100 <= status <= 599:
            error_response = requests.Response()
            error_response.status_code = status
            raise requests.HTTPError(str(exc), response=error_response) from exc
        raise requests.RequestException(str(exc)) from exc
    finally:
        session.close()

    soup = BeautifulSoup(page, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        for posting in data if isinstance(data, list) else [data]:
            if isinstance(posting, dict) and posting.get("@type") == "JobPosting":
                markup = posting.get("description")
                if isinstance(markup, str) and (description := html_to_text(markup)):
                    return description, ""

    details = page_values(page, "jobDetails")[0]
    markup = details.get("htmlFullDescription") if isinstance(details, dict) else None
    if isinstance(markup, str) and (description := html_to_text(markup)):
        return description, ""
    raise ValueError("No ZipRecruiter description in the page")
