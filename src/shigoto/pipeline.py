"""One run: sources -> canonical Job -> normalize -> dedupe into SQLite -> enrich -> sheet sync."""

from __future__ import annotations

import logging
from collections import Counter

from shigoto.config import Config
from shigoto.db import Store, now_iso
from shigoto.enrich import enrich_descriptions
from shigoto.liveness import check_liveness, reset_robots
from shigoto.normalize import CityMatcher, normalize
from shigoto.sheets import open_worksheet, sync
from shigoto.sources import Source, build_sources

log = logging.getLogger(__name__)


def run_once(config: Config, *, sync_sheet: bool = True, only: set[str] | None = None) -> dict[str, int]:
    """Run every source (or just `only`), then push the delta to the sheet."""
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
            if config.google_credentials is None or not config.sheet.spreadsheet_id:
                raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS and SHIGOTO_SPREADSHEET_ID must be set")
            stats.update(sync(store, open_worksheet(config.google_credentials, config.sheet), config.sheet))
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
            job = normalize(raw, matcher, config.title_keywords, config.exclude_title_keywords)
            if job is None:
                stats["dropped"] += 1
                continue
            stats[store.upsert(job, now)] += 1
    except Exception:
        log.exception("source %s crashed", source.name)
        stats[f"error_{source.name}"] += 1
    store.commit()
    log.info("%s: %s", source.name, dict(stats))
    return stats
