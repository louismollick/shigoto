"""Fill in missing descriptions one detail page at a time, capped per run.

LinkedIn, ZipRecruiter and Job Bank search results carry no description. GC Jobs details can fail
and leave an empty description. Retry missing descriptions, capped per source. Direct
LinkedIn fetches stop at the first sign of blocking; proxied ones skip to the next job.
ZipRecruiter requires a proxy and skips failed jobs for later retry.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import requests

from shigoto.config import Config
from shigoto.db import DescriptionTarget, Store, now_iso
from shigoto.http import RateLimited, RobotsDisallowed, proxy_url
from shigoto.sources import gcjobs, jobbank, linkedin_detail, ziprecruiter_detail

log = logging.getLogger(__name__)


def enrich_descriptions(store: Store, config: Config) -> dict[str, int]:
    d = config.descriptions
    proxied = proxy_url() is not None
    return {
        "described_linkedin": _enrich(
            store, "linkedin", d.linkedin_proxied_max_per_run if proxied else d.linkedin_max_per_run,
            lambda t: linkedin_detail.fetch_description(t.source_id),
            delay=d.linkedin_proxied_delay_seconds if proxied else d.linkedin_delay_seconds,
            # Each proxied request has its own exit IP, so one failure says nothing about the next.
            stop_on_failure=not proxied,
        ),
        "described_jobbank": _enrich(
            store, "jobbank", d.jobbank_max_per_run, lambda t: (jobbank.fetch_description(t.url), ""), delay=0,
        ),
        "described_gcjobs": _enrich(store, "gcjobs", 25, lambda t: (gcjobs.fetch_description(t.url), ""),
                                    delay=0),
        "described_zip_recruiter": _enrich(
            store, "zip_recruiter", d.ziprecruiter_max_per_run,
            lambda t: ziprecruiter_detail.fetch_description(t.url, delay=d.ziprecruiter_delay_seconds),
            delay=d.ziprecruiter_delay_seconds, stop_on_failure=False,
        ) if proxied else 0,
    }


def _enrich(store: Store, source: str, limit: int, fetch: Callable[[DescriptionTarget], tuple[str, str]],
            delay: float, stop_on_failure: bool = True) -> int:
    """`fetch` returns (description, salary). Failed jobs stay unchecked and rotate behind other pending jobs."""
    done = 0
    attempted = 0
    for target in store.missing_descriptions(source, limit):
        # Pace the next job after every outcome, including blocked or unrecognized pages.
        if attempted and delay:
            time.sleep(delay + random.uniform(0, delay / 2))
        attempted += 1
        salary = ""
        try:
            description, salary = fetch(target)
        except RateLimited:
            store.record_description_attempt(target.job_id, now_iso())
            log.warning("%s rate-limited a description fetch%s", source,
                        "; stopping for this run" if stop_on_failure else "")
            if stop_on_failure:
                break
            continue
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status not in (404, 410):
                store.record_description_attempt(target.job_id, now_iso())
                log.warning("%s job %s description fetch got HTTP %s%s", source, target.job_id, status,
                            "; stopping for this run" if stop_on_failure else "")
                if stop_on_failure:
                    break
                continue
            description = ""  # posting gone: mark checked so we don't retry
        except requests.RequestException:
            store.record_description_attempt(target.job_id, now_iso())
            log.exception("%s description fetch failed", source)
            if stop_on_failure:
                break
            continue
        except RobotsDisallowed:
            store.record_description_attempt(target.job_id, now_iso())
            log.exception("%s description fetch disallowed", source)
            break
        except ValueError:
            store.record_description_attempt(target.job_id, now_iso())
            # An unrecognized successful response is retryable, not a checked empty posting.
            log.exception("%s description parse failed", source)
            continue
        store.set_description(target.job_id, description, now_iso(), salary)
        done += 1
    log.info("%s: completed %d of %d description jobs", source, done, attempted)
    return done
