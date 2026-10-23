"""Fetch ZipRecruiter descriptions with paced, fresh-session retries and Flight fallback."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from curl_cffi.requests.exceptions import RequestException
from jobspy.util import TLSRotating
from jobspy.ziprecruiter.util import page_values

from shigoto.http import proxy_url
from shigoto.text import html_to_text

log = logging.getLogger(__name__)
# Redirects consume the same budget as retries: at most 4 HTTP requests per posting.
MAX_REQUESTS = 4
_FLIGHT_REDIRECT = re.compile(r'NEXT_REDIRECT;(?:replace|push);([^;"\n]+);(?:303|307|308);')
_TEXT_ROW = re.compile(rb"[0-9a-f]*:(?:T([0-9a-f]+),)?")


class _UnsafeURL(ValueError):
    """Do not retry a redirect that leaves the secure ZipRecruiter site."""


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or (host != "ziprecruiter.com" and not host.endswith(".ziprecruiter.com")):
        raise _UnsafeURL("ZipRecruiter redirected to an external host or insecure URL")


def create_session(proxy: str | None) -> TLSRotating:
    """A fresh curl_cffi browser-impersonating session, through the proxy when one is set."""
    return TLSRotating([proxy] if proxy else None)


def posting_key(url: str) -> str:
    """The posting's `jid`. Each search can list one posting under a new listing key and URL slug."""
    jid = dict(parse_qsl(urlparse(url).query)).get("jid")
    return f"jid:{jid}" if jid else url


def _flight_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "_rsc"]
    return urlunparse(parsed._replace(query=urlencode([*query, ("_rsc", "")])))


def _description(page: str, flight: bool) -> str:
    # Flight carries the same rows as HTML's self.__next_f.push scripts. Reuse JobSpy's
    # decoder, including its references to length-prefixed UTF-8 text rows.
    descriptions: list[str] = []
    if not flight:
        soup = BeautifulSoup(page, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.get_text())
            except ValueError:
                continue
            for posting in data if isinstance(data, list) else [data]:
                if isinstance(posting, dict) and posting.get("@type") == "JobPosting":
                    markup = posting.get("description")
                    if isinstance(markup, str):
                        descriptions.append(html_to_text(markup))
    try:
        data = page if flight else "".join(json.loads(chunk) for chunk in re.findall(
            r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page))
        # JobSpy slices text rows without checking their declared byte lengths.
        # Reject an incomplete row before it can be saved as a partial description.
        raw, pos = data.encode(), 0
        while match := _TEXT_ROW.match(raw, pos):
            if match[1]:
                end = match.end() + int(match[1], 16)
                if end > len(raw):
                    raise ValueError("Incomplete ZipRecruiter text row")
                raw[match.end():end].decode()  # also reject a split UTF-8 character
                pos = end
            else:
                pos = raw.find(b"\n", pos) + 1 or len(raw)
        html = f'<script>self.__next_f.push([1,{json.dumps(data)}])</script>'
        details = page_values(html, "jobDetails")["jobDetails"]
        markup = details.get("htmlFullDescription") if isinstance(details, dict) else None
        # A truncated Flight stream can omit a referenced text row. JobSpy then
        # leaves "$1" unchanged; never save that token as a checked description.
        if isinstance(markup, str) and not re.fullmatch(r"\$[0-9a-f]+", markup):
            descriptions.append(html_to_text(markup))
    except ValueError:
        pass
    if description := max(descriptions, key=len, default=""):
        return description
    raise ValueError("No ZipRecruiter description in the page")


def fetch_description(url: str, *, delay: float = 2) -> tuple[str, str]:
    """Try HTML then Flight; all requests and redirects share a four-request budget.

    Every request gets a fresh proxy session. Only a ZipRecruiter 404/410 is terminal;
    challenges, transport errors and unrecognized pages remain pending on exhaustion.
    """
    _validate_url(url)
    flight = False
    error: requests.RequestException | ValueError = ValueError("ZipRecruiter redirect budget exhausted")
    for attempt in range(MAX_REQUESTS):
        if attempt and delay:
            time.sleep(delay * min(2 ** (attempt - 1), 4))
        request_url = _flight_url(url) if flight else url
        session = create_session(proxy_url())
        try:
            # Follow redirects ourselves so each hop is paced, budgeted and host-checked.
            response = session.get(request_url, timeout=30, allow_redirects=False,
                                   headers={"RSC": "1"} if flight else {})
            _validate_url(response.url)
            log.info("ZipRecruiter detail %s attempt %d/%d HTTP %d: %s",
                     "Flight" if flight else "HTML", attempt + 1, MAX_REQUESTS, response.status_code, url)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("ZipRecruiter redirect has no Location")
                url = urljoin(response.url, location)
                _validate_url(url)
                continue
            response.raise_for_status()  # type: ignore[no-untyped-call]  # curl_cffi leaves it untyped
            is_flight = "text/x-component" in response.headers.get("Content-Type", "")
            if is_flight and (redirect := _FLIGHT_REDIRECT.search(response.text)):
                url = urljoin(response.url, redirect[1])
                _validate_url(url)
                continue
            description = _description(response.text, is_flight)
            log.info("ZipRecruiter described in %d requests, %d characters: %s",
                     attempt + 1, len(description), url)
            return description, ""
        except RequestException as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if isinstance(status, int) and 100 <= status <= 599:
                error_response = requests.Response()
                error_response.status_code = status
                error = requests.HTTPError(f"ZipRecruiter HTTP {status}", response=error_response)
                if status not in (403, 429) and status < 500:
                    raise error from exc
            else:
                error = requests.RequestException("ZipRecruiter detail connection failed")
        except _UnsafeURL:
            raise
        except ValueError as exc:
            error = exc
        finally:
            session.close()
        flight = True
    log.warning("ZipRecruiter exhausted %d detail requests: %s", MAX_REQUESTS, url)
    raise error
