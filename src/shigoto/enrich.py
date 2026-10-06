"""Fill in missing descriptions one detail page at a time, capped per run.

LinkedIn and Job Bank search results carry no description. We fetch them only for
jobs no other source has described, newest first, and stop LinkedIn at the first
sign of blocking so the next run starts fresh.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import requests

from shigoto.config import Config
from shigoto.db import DescriptionTarget, Store, now_iso
from shigoto.http import RateLimited
from shigoto.sources import jobbank, linkedin_detail

log = logging.getLogger(__name__)


def enrich_descriptions(store: Store, config: Config) -> dict[str, int]:
    d = config.descriptions
    return {
        "described_linkedin": _enrich(
            store, "linkedin", d.linkedin_max_per_run, lambda t: linkedin_detail.fetch_description(t.source_id),
            delay=d.linkedin_delay_seconds,
        ),
        "described_jobbank": _enrich(
            store, "jobbank", d.jobbank_max_per_run, lambda t: jobbank.fetch_description(t.url), delay=0,
        ),
    }


def _enrich(store: Store, source: str, limit: int, fetch: Callable[[DescriptionTarget], str], delay: float) -> int:
    done = 0
    for target in store.missing_descriptions(source, limit):
        try:
            description = fetch(target)
        except RateLimited:
            log.warning("%s rate-limited description fetches; stopping for this run", source)
            break
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status not in (404, 410):
                log.warning("%s description fetch got HTTP %s; stopping for this run", source, status)
                break
            description = ""  # posting gone: mark checked so we don't retry
        except requests.RequestException:
            log.exception("%s description fetch failed", source)
            break
        store.set_description(target.job_id, description, now_iso())
        done += 1
        if delay:
            time.sleep(delay + random.uniform(0, delay / 2))
    log.info("%s: fetched %d descriptions", source, done)
    return done
