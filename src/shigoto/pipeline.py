"""One run: sources -> canonical Job -> normalize -> dedupe into SQLite -> enrich -> sheet sync."""

from __future__ import annotations

import logging
from collections import Counter

from shigoto.config import Config
from shigoto.db import Store, now_iso
from shigoto.enrich import enrich_descriptions
from shigoto.liveness import check_liveness, reset_robots
from shigoto.models import job_from_json, job_json
from shigoto.normalize import CityMatcher, normalize
from shigoto.sheets import open_worksheet, sync
from shigoto.sources import Source, build_sources

log = logging.getLogger(__name__)


def run_once(config: Config, *, sync_sheet: bool = True, only: set[str] | None = None) -> dict[str, int]:
    """Run every source (or just `only`), then rebuild the sheet."""
    store = Store(config.db_path)
    backfill = store.last_finished_run() is None
    run_id = store.start_run(now_iso())
    stats: Counter[str] = Counter()
    try:
        reset_robots()
        matcher = CityMatcher(config.cities)
        for source in build_sources(config, matcher, backfill):
            if only is None or source.name in only:
                stats.update(ingest(store, source, matcher, config))
        stats.update(enrich_descriptions(store, config))
        stats.update(check_liveness(store, config))
        if sync_sheet:
            stats.update(sync_to_sheet(store, config))
        else:
            stats["sheet_hidden"] = store.reevaluate_exclusions(config, now_iso())
    finally:
        store.finish_run(run_id, now_iso(), dict(stats))
        store.close()
    log.info("run finished: %s", dict(stats))
    return dict(stats)


def ingest(store: Store, source: Source, matcher: CityMatcher, config: Config) -> Counter[str]:
    stats: Counter[str] = Counter()
    now = now_iso()
    try:
        for raw in source.fetch():
            stats[f"fetched_{source.name}"] += 1
            snapshot = job_json(raw)
            job = normalize(raw, matcher, config.title_keywords, config.exclude_title_keywords)
            if job is None:
                stats["dropped"] += 1
                known = store.conn.execute("SELECT 1 FROM job_sources WHERE source=? AND source_id=?",
                                           (raw.source, raw.source_id)).fetchone()
                if known is None:
                    continue
                # Keep known postings current even when their latest title/location fails filters.
            stats[store.upsert(raw, now, raw=snapshot)] += 1
    except Exception:
        log.exception("source %s crashed", source.name)
        stats[f"error_{source.name}"] += 1
    store.commit()
    log.info("%s: %s", source.name, dict(stats))
    return stats


def sync_to_sheet(store: Store, config: Config) -> dict[str, int]:
    if config.google_credentials is None or not config.sheet.spreadsheet_id:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS and SHIGOTO_SPREADSHEET_ID must be set")
    return sync(store, open_worksheet(config.google_credentials, config.sheet), config)


def reprocess(config: Config, *, sync_sheet: bool = True) -> dict[str, int]:
    """Replay saved adapter records with current normalization, without counting them as seen."""
    store = Store(config.db_path)
    stats: Counter[str] = Counter()
    try:
        matcher, now = CityMatcher(config.cities), now_iso()
        records = store.conn.execute(
            "SELECT raw FROM job_sources WHERE raw IS NOT NULL ORDER BY first_seen, source, source_id"
        ).fetchall()
        for record in records:
            job = job_from_json(record["raw"])
            included = normalize(job, matcher, config.title_keywords, config.exclude_title_keywords)
            stats["reprocessed"] += 1
            if included is None:
                stats["dropped"] += 1
            # Existing records still need corrected fields so exclusion uses the replayed title/city.
            stats[store.upsert(job, now, raw=record["raw"], seen=False)] += 1
        store.commit()
        if sync_sheet:
            stats.update(sync_to_sheet(store, config))
        else:
            stats["sheet_hidden"] = store.reevaluate_exclusions(config, now)
    finally:
        store.close()
    log.info("reprocess finished: %s", dict(stats))
    return dict(stats)
