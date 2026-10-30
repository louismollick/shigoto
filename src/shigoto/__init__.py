"""shigoto: job aggregation -> SQLite -> Google Sheets -> Codex review.

CLI:
  shigoto run [--no-sheet] [--only linkedin,indeed,...]   one run, then exit
  shigoto reprocess [--no-sheet]                      replay saved source records
  shigoto review [--dry-run] [--job ID] [--limit N]     review without scraping or rebuilding
  shigoto serve                                            run every `interval_hours`, ticking in between
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from shigoto.config import Config, load_config
from shigoto.db import Store
from shigoto.pipeline import reprocess, run_once, tick
from shigoto.review import review

log = logging.getLogger("shigoto")
REVIEW_RETRY = timedelta(hours=1)


@contextmanager
def run_lock(config: Config) -> Iterator[None]:
    """Scheduled and manual CLI runs share one nonblocking lock next to SQLite."""
    path = config.db_path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another Shigoto run is already active") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main() -> None:
    parser = argparse.ArgumentParser(prog="shigoto")
    parser.add_argument("command", choices=["run", "serve", "reprocess", "review"])
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("SHIGOTO_CONFIG", "config.yaml")))
    parser.add_argument("--no-sheet", action="store_true", help="skip the Google Sheets sync")
    parser.add_argument("--only", help="comma-separated source names, e.g. indeed,jobbank")
    parser.add_argument("--dry-run", action="store_true", help="review without saving results or writing Sheet cells")
    parser.add_argument("--job", help="review one Job ID")
    parser.add_argument("--limit", type=int, metavar="N", help="positive job limit, overrides review.max_per_run")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    only = set(args.only.split(",")) if args.only else None
    if args.command != "review" and (args.dry_run or args.job or args.limit is not None):
        parser.error("--dry-run, --job and --limit are only supported by review")
    if args.command == "review" and (args.no_sheet or args.only):
        parser.error("review does not support --no-sheet or --only")
    if args.command == "serve":
        serve(config, sync_sheet=not args.no_sheet)
    else:
        with run_lock(config):
            if args.command == "run":
                run_once(config, sync_sheet=not args.no_sheet, only=only)
            elif args.command == "reprocess":
                reprocess(config, sync_sheet=not args.no_sheet)
            else:
                print(review(config, dry_run=args.dry_run, job_id=args.job, limit=args.limit).model_dump_json())


def review_retry_delay(stats: dict[str, int]) -> timedelta:
    """Drain a healthy review backlog next tick; wait an hour after errors or no progress."""
    if stats.get("ai_pending", 0) > 0 and stats.get("ai_reviewed", 0) > 0 and not stats.get("ai_errors", 0):
        return timedelta(0)
    return REVIEW_RETRY


def serve(config: Config, sync_sheet: bool) -> None:
    """Scheduler loop. The next run is due `interval_hours` after the last finished run
    in the DB, so container restarts don't trigger extra scrapes. While waiting, a tick every
    `tick_minutes` checks closures and drains successful review batches. Reviews otherwise
    retry hourly, including after usage limits, failures or an empty queue."""
    interval, step = timedelta(hours=config.interval_hours), timedelta(minutes=config.tick_minutes)
    next_review = datetime.now().astimezone() + REVIEW_RETRY
    announced = None
    while True:
        store = Store(config.db_path)
        last = store.last_finished_run()
        store.close()
        due = datetime.fromisoformat(last) + interval if last else datetime.now().astimezone()
        now = datetime.now().astimezone()
        if due > now:
            if due != announced:
                log.info("next run at %s", due.isoformat(timespec="seconds"))
                announced = due
            time.sleep(min(step, due - now).total_seconds())
            if datetime.now().astimezone() >= due:
                continue
            review_due = datetime.now().astimezone() >= next_review
            try:
                with run_lock(config):
                    if review_due:
                        # Keep the hourly backoff even if tick raises before returning stats.
                        next_review = datetime.now().astimezone() + REVIEW_RETRY
                    stats = tick(config, sync_sheet=sync_sheet, review_due=review_due)
                    if review_due:
                        next_review = datetime.now().astimezone() + review_retry_delay(stats)
                log.info("tick: %s", stats)
            except Exception:
                log.exception("tick failed")
            continue
        try:
            with run_lock(config):
                stats = run_once(config, sync_sheet=sync_sheet)
            next_review = datetime.now().astimezone() + review_retry_delay(stats)
        except Exception:
            log.exception("run failed")
            time.sleep(600)  # don't hot-loop on a persistent failure
