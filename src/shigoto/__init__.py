"""shigoto: job aggregation -> SQLite -> Google Sheets -> Codex review.

CLI:
  shigoto run [--no-sheet] [--only linkedin,indeed,...]   one run, then exit
  shigoto reprocess [--no-sheet]                      replay saved source records
  shigoto review [--dry-run] [--job ID] [--limit N]     review without scraping or rebuilding
  shigoto serve                                            run every `interval_hours`, forever
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
from shigoto.pipeline import reprocess, run_once
from shigoto.review import review

log = logging.getLogger("shigoto")


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
    parser.add_argument("--limit", type=int, choices=range(1, 41), metavar="1-40", help="maximum jobs to review")
    args = parser.parse_args()

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


def serve(config: Config, sync_sheet: bool) -> None:
    """Scheduler loop. The next run is due `interval_hours` after the last finished run
    in the DB, so container restarts don't trigger extra scrapes."""
    interval = timedelta(hours=config.interval_hours)
    while True:
        store = Store(config.db_path)
        last = store.last_finished_run()
        store.close()
        due = datetime.fromisoformat(last) + interval if last else datetime.now().astimezone()
        wait = (due - datetime.now().astimezone()).total_seconds()
        if wait > 0:
            log.info("next run at %s", due.isoformat(timespec="seconds"))
            time.sleep(wait)
        try:
            with run_lock(config):
                run_once(config, sync_sheet=sync_sheet)
        except Exception:
            log.exception("run failed")
            time.sleep(600)  # don't hot-loop on a persistent failure
