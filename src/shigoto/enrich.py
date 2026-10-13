"""Fill in missing descriptions one detail page at a time, capped per run.

LinkedIn and Job Bank search results carry no description. GC Jobs details can fail
and leave only search metadata. Retry missing descriptions, capped per source, and
stop LinkedIn at the first sign of blocking so the next run starts fresh.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import requests

from shigoto.config import Config
from shigoto.db import DescriptionTarget, Store, now_iso
from shigoto.http import RateLimited, RobotsDisallowed
from shigoto.sources import gcjobs, jobbank, linkedin_detail

log = logging.getLogger(__name__)
GCJOBS_MAX_PER_RUN = 25


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
        "described_gcjobs": _enrich_gcjobs(store),
    }


def _enrich_gcjobs(store: Store) -> int:
    """Retry empty and search-metadata-only GC posters without changing the store API."""
    rows = store.conn.execute(
        """SELECT j.job_id, s.url, j.description FROM jobs j JOIN job_sources s ON s.job_id=j.job_id
           WHERE s.source='gcjobs'
             AND ((j.description='' AND j.description_checked_at IS NULL)
                  OR (j.primary_source='gcjobs' AND j.description!=''
                      AND instr(j.description, char(10) || char(10))=0
                      AND (j.description_checked_at IS NULL
                           OR julianday(s.last_seen) > julianday(j.description_checked_at))))
           GROUP BY j.job_id ORDER BY j.first_seen DESC LIMIT ?""",
        (GCJOBS_MAX_PER_RUN,),
    ).fetchall()
    done = 0
    for row in rows:
        try:
            detail = gcjobs.fetch_description(row["url"])
        except RateLimited:
            log.warning("gcjobs rate-limited description fetches; stopping for this run")
            break
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in (404, 410):
                log.exception("gcjobs description fetch failed")
                continue
            detail = ""
        except (requests.RequestException, RobotsDisallowed, ValueError):
            log.exception("gcjobs description fetch failed")
            continue
        description = "\n\n".join(part for part in (row["description"], detail) if part)
        store.set_description(row["job_id"], description, now_iso())
        done += 1
    log.info("gcjobs: fetched %d descriptions", done)
    return done


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
        except RobotsDisallowed:
            log.exception("%s description fetch disallowed", source)
            break
        except ValueError:
            # An unrecognized successful response is retryable, not a checked empty posting.
            log.exception("%s description parse failed", source)
            continue
        store.set_description(target.job_id, description, now_iso())
        done += 1
        if delay:
            time.sleep(delay + random.uniform(0, delay / 2))
    log.info("%s: fetched %d descriptions", source, done)
    return done
