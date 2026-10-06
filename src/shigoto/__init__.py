"""shigoto: deterministic job aggregation -> SQLite -> Google Sheets.

CLI:
  shigoto run [--no-sheet] [--only linkedin,indeed,...]   one run, then exit
  shigoto serve                                            run every `interval_hours`, forever
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from shigoto.config import Config, load_config
from shigoto.db import Store
from shigoto.pipeline import run_once

log = logging.getLogger("shigoto")


def main() -> None:
    parser = argparse.ArgumentParser(prog="shigoto")
    parser.add_argument("command", choices=["run", "serve"])
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("SHIGOTO_CONFIG", "config.yaml")))
    parser.add_argument("--no-sheet", action="store_true", help="skip the Google Sheets sync")
    parser.add_argument("--only", help="comma-separated source names, e.g. indeed,jobbank")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    only = set(args.only.split(",")) if args.only else None
    if args.command == "run":
        run_once(config, sync_sheet=not args.no_sheet, only=only)
    else:
        serve(config, sync_sheet=not args.no_sheet)


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
            run_once(config, sync_sheet=sync_sheet)
        except Exception:
            log.exception("run failed")
            time.sleep(600)  # don't hot-loop on a persistent failure
