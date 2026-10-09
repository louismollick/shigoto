"""SQLite store: the authoritative job history.

`jobs` holds one row per deduplicated job; `job_sources` maps every (source, source_id)
we've seen to its job. Postings only merge across sources: two ids from the same source
are always two jobs, even with identical company/title/city. A job's fields come from its primary (first) source; other
sources only fill blanks, so the content hash doesn't flip-flop between sources.
`content_hash` tracks edits to every field used for job-fit review. Reviewer data is backed up before
each sheet rebuild; excluded jobs and their reviews remain here as history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from shigoto.config import Config
from shigoto.models import Job, Liveness, job_json
from shigoto.normalize import dedupe_key, title_matches

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    primary_source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    city TEXT NOT NULL,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    posted_date TEXT,
    salary TEXT NOT NULL,
    job_type TEXT NOT NULL,
    url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    description_checked_at TEXT,
    ai_reviewed_at TEXT,
    closed_at TEXT,
    liveness_checked_at TEXT,
    excluded_at TEXT
);
CREATE TABLE IF NOT EXISTS job_sources (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    url TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    raw TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS job_sources_job ON job_sources(job_id);
CREATE TABLE IF NOT EXISTS reviews (
    job_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT
);
CREATE TABLE IF NOT EXISTS ai_results (
    job_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    policy TEXT NOT NULL,
    result TEXT NOT NULL,
    fields TEXT NOT NULL,
    written_at TEXT,
    error TEXT,
    PRIMARY KEY (job_id, fingerprint, policy)
);
"""

UpsertResult = Literal["new", "changed", "seen"]
FULL_LISTING_SOURCES = frozenset({
    "workday", "smartrecruiters", "greenhouse", "lever", "ashby", "successfactors", "gcjobs",
})
# Searches here are partial, so absence proves nothing, but each posting can be checked directly.
DIRECT_CHECK_SOURCES = frozenset({"indeed", "linkedin", "jobbank"})
CLOSED_AFTER = timedelta(days=2)

@dataclass(frozen=True)
class StoredJob:
    """A job row plus its source links, as handed to the sheet sync."""

    job_id: str
    title: str
    company: str
    city: str
    location: str
    description: str
    posted_date: str
    salary: str
    job_type: str
    url: str
    sources: str
    first_seen: str
    updated_at: str
    content_hash: str
    status: Literal["", "Closed"]


@dataclass(frozen=True)
class DescriptionTarget:
    job_id: str
    source_id: str
    url: str

@dataclass(frozen=True)
class SourceLink:
    source: str
    source_id: str
    url: str

@dataclass(frozen=True)
class LivenessTarget:
    job_id: str
    sources: list[SourceLink]


def make_job_id(job: Job, distinct: bool = False) -> str:
    """Cross-source identity from the dedupe key. `distinct` adds the source id, for a
    second posting from the same source that happens to share company/title/city."""
    key = dedupe_key(job) + (f"|{job.source}|{job.source_id}" if distinct else "")
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def content_hash(title: str, description: str, *, company: str = "", city: str = "",
                 location: str = "", salary: str = "", job_type: str = "") -> str:
    """Material changes to any review input advance Updated At."""
    return hashlib.sha1(json.dumps([title, description, company, city, location, salary, job_type]).encode()).hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column in ("closed_at", "liveness_checked_at", "excluded_at"):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        source_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(job_sources)")}
        if "raw" not in source_columns:
            self.conn.execute("ALTER TABLE job_sources ADD COLUMN raw TEXT")
        version = self.conn.execute("SELECT value FROM meta WHERE key='content_hash_version'").fetchone()
        if version is None or version["value"] != "3":
            self.conn.executemany(
                "UPDATE jobs SET content_hash=? WHERE job_id=?",
                [(content_hash(row["title"], row["description"], company=row["company"], city=row["city"],
                               location=row["location"], salary=row["salary"], job_type=row["job_type"]), row["job_id"])
                 for row in self.conn.execute("SELECT * FROM jobs")],
            )
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('content_hash_version', '3')")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, job: Job, now: str, *, raw: str | None = None, seen: bool = True) -> UpsertResult:
        """Merge a normalized job. `seen=False` replays raw data without changing recency or closure."""
        c = self.conn
        mapped = c.execute("SELECT job_id FROM job_sources WHERE source=? AND source_id=?",
                           (job.source, job.source_id)).fetchone()
        job_id: str = mapped["job_id"] if mapped else make_job_id(job)
        if not mapped and c.execute("SELECT 1 FROM job_sources WHERE job_id=? AND source=?",
                                    (job_id, job.source)).fetchone():
            job_id = make_job_id(job, distinct=True)  # same source, different id: a separate posting
        c.execute(
            """INSERT INTO job_sources (source, source_id, job_id, url, first_seen, last_seen, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET
               last_seen=CASE WHEN ? THEN excluded.last_seen ELSE job_sources.last_seen END,
               url=excluded.url, raw=excluded.raw""",
            (job.source, job.source_id, job_id, job.url, now, now, raw if raw is not None else job_json(job), seen),
        )
        posted = job.posted_date.isoformat() if job.posted_date else None
        row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            h = content_hash(job.title, job.description, company=job.company, city=job.city,
                             location=job.location, salary=job.salary, job_type=job.job_type)
            c.execute(
                """INSERT INTO jobs (job_id, primary_source, title, company, city, location, description,
                   posted_date, salary, job_type, url, content_hash, first_seen, last_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, job.source, job.title, job.company, job.city, job.location, job.description,
                 posted, job.salary, job.job_type, job.url, h, now, now, now),
            )
            return "new"

        primary = job.source == row["primary_source"]
        merged: dict[str, str] = {
            "title": job.title if primary else row["title"],
            "company": (job.company or row["company"]) if primary else row["company"],
            "location": (job.location or row["location"]) if primary else row["location"],
            "description": job.description if (job.description and (primary or not row["description"]))
            else row["description"],
            "salary": job.salary if (job.salary and (primary or not row["salary"])) else row["salary"],
            "job_type": (job.job_type or row["job_type"]) if primary else row["job_type"] or job.job_type,
            "city": job.city if primary else row["city"],
        }
        h = content_hash(**merged)
        changed = h != row["content_hash"]
        c.execute(
            """UPDATE jobs SET title=:title, company=:company, location=:location, description=:description,
               salary=:salary, job_type=:job_type, city=:city, posted_date=coalesce(posted_date, :posted),
               content_hash=:hash, closed_at=CASE WHEN :seen THEN NULL ELSE closed_at END,
               liveness_checked_at=CASE WHEN :seen AND closed_at IS NOT NULL THEN NULL
                                        ELSE liveness_checked_at END,
               last_seen=CASE WHEN :seen THEN :now ELSE last_seen END,
               updated_at=CASE WHEN :changed THEN :now ELSE updated_at END
               WHERE job_id=:job_id""",
            {**merged, "posted": posted, "hash": h, "now": now, "changed": changed, "seen": seen, "job_id": job_id},
        )
        return "changed" if changed else "seen"

    def commit(self) -> None:
        self.conn.commit()

    def missing_descriptions(self, source: str, limit: int) -> list[DescriptionTarget]:
        """Newest jobs with no description that `source` could describe, never tried before."""
        rows = self.conn.execute(
            """SELECT j.job_id, s.source_id, s.url FROM jobs j JOIN job_sources s ON s.job_id = j.job_id
               WHERE s.source=? AND j.description='' AND j.description_checked_at IS NULL
               GROUP BY j.job_id ORDER BY j.first_seen DESC LIMIT ?""",
            (source, limit),
        ).fetchall()
        return [DescriptionTarget(r["job_id"], r["source_id"], r["url"]) for r in rows]

    def set_description(self, job_id: str, description: str, now: str) -> None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        h = content_hash(row["title"], description, company=row["company"], city=row["city"],
                         location=row["location"], salary=row["salary"], job_type=row["job_type"])
        self.conn.execute(
            """UPDATE jobs SET description=?, description_checked_at=?, content_hash=?,
               updated_at=CASE WHEN content_hash != ? THEN ? ELSE updated_at END WHERE job_id=?""",
            (description, now, h, h, now, job_id),
        )
        self.conn.commit()

    def liveness_candidates(self, now: str) -> list[LivenessTarget]:
        """Open jobs whose every source can be checked, stalest first, checked at most once per day.

        Full-listing sources must also be missing from their listings for CLOSED_AFTER;
        a recent sighting there already proves the job is open.
        """
        cutoff = (datetime.fromisoformat(now) - CLOSED_AFTER).isoformat()
        checked_cutoff = (datetime.fromisoformat(now) - timedelta(days=1)).isoformat()
        full, checkable = sorted(FULL_LISTING_SOURCES), sorted(FULL_LISTING_SOURCES | DIRECT_CHECK_SOURCES)
        rows = self.conn.execute(
            f"""SELECT j.job_id FROM jobs j JOIN job_sources s ON s.job_id=j.job_id
                WHERE j.closed_at IS NULL AND (j.liveness_checked_at IS NULL
                    OR julianday(j.liveness_checked_at) <= julianday(?))
                GROUP BY j.job_id
                HAVING min(s.source IN ({",".join("?" * len(checkable))}))=1
                    AND coalesce(max(CASE WHEN s.source IN ({",".join("?" * len(full))})
                                     THEN julianday(s.last_seen) END), 0) < julianday(?)
                ORDER BY max(julianday(s.last_seen)), j.job_id""",
            (checked_cutoff, *checkable, *full, cutoff),
        ).fetchall()
        return [LivenessTarget(row["job_id"], [
            SourceLink(s["source"], s["source_id"], s["url"])
            for s in self.conn.execute("SELECT source, source_id, url FROM job_sources WHERE job_id=?",
                                       (row["job_id"],))
        ]) for row in rows]

    def record_liveness(self, job_id: str, result: Liveness, now: str) -> None:
        """Status changes leave material content and its update timestamp untouched."""
        self.conn.execute(
            """UPDATE jobs SET liveness_checked_at=?,
               closed_at=CASE WHEN ?='gone' THEN ? ELSE closed_at END WHERE job_id=?""",
            (now, result, now, job_id),
        )
        self.conn.commit()

    def visible_jobs(self) -> list[StoredJob]:
        """All currently included jobs, with a stable order for the sheet rebuild."""
        rows = self.conn.execute(
            """SELECT j.*, (SELECT group_concat(source || ' ' || url, char(10)) FROM job_sources s
                            WHERE s.job_id = j.job_id) AS sources
               FROM jobs j WHERE excluded_at IS NULL
               ORDER BY first_seen, job_id"""
        ).fetchall()
        return [
            StoredJob(
                job_id=r["job_id"], title=r["title"], company=r["company"], city=r["city"],
                location=r["location"], description=r["description"], posted_date=r["posted_date"] or "",
                salary=r["salary"], job_type=r["job_type"], url=r["url"], sources=r["sources"] or "",
                first_seen=r["first_seen"], updated_at=r["updated_at"], content_hash=r["content_hash"],
                status="Closed" if r["closed_at"] is not None else "",
            )
            for r in rows
        ]

    def reevaluate_exclusions(self, config: Config, now: str) -> int:
        """Hide jobs outside current city/title filters, retaining their history and reviews."""
        cities = {city.name for city in config.cities}
        excluded = 0
        for row in self.conn.execute("SELECT job_id, city, title FROM jobs").fetchall():
            passes = (row["city"] in cities and title_matches(row["title"], config.title_keywords)
                      and not title_matches(row["title"], config.exclude_title_keywords))
            excluded += not passes
            self.conn.execute(
                "UPDATE jobs SET excluded_at=CASE WHEN ? THEN NULL ELSE coalesce(excluded_at, ?) END WHERE job_id=?",
                (passes, now, row["job_id"]),
            )
        self.conn.commit()
        return excluded

    def reviewer_columns(self) -> list[str] | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='reviewer_columns'").fetchone()
        return json.loads(row["value"]) if row else None

    def reviews(self) -> dict[str, dict[str, str]]:
        return {row["job_id"]: json.loads(row["data"]) for row in self.conn.execute("SELECT job_id, data FROM reviews")}

    def sheet_rebuild_pending(self) -> bool:
        return self.conn.execute("SELECT 1 FROM meta WHERE key='sheet_rebuild_pending'").fetchone() is not None

    def finish_sheet_rebuild(self) -> None:
        self.conn.execute("DELETE FROM meta WHERE key='sheet_rebuild_pending'")
        self.conn.commit()

    def backup_reviews(self, columns: list[str], reviews: dict[str, dict[str, str]], now: str) -> int:
        """Commit reviewer values and header order before any sheet writes. Return unknown job count."""
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('reviewer_columns', ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (json.dumps(columns),),
        )
        self.conn.executemany(
            """INSERT INTO reviews (job_id, data, updated_at) VALUES (?, ?, ?)
               ON CONFLICT (job_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
            [(job_id, json.dumps(data), now) for job_id, data in reviews.items()],
        )
        if "AI Reviewed At" in columns:
            self.conn.executemany("UPDATE jobs SET ai_reviewed_at=? WHERE job_id=?",
                                  [(data["AI Reviewed At"] or None, job_id) for job_id, data in reviews.items()])
        known = {row["job_id"] for row in self.conn.execute("SELECT job_id FROM jobs")}
        # A failed rebuild can temporarily pair new IDs with old reviewer cells.
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sheet_rebuild_pending', '1')")
        self.conn.commit()
        return len(reviews.keys() - known)

    def start_run(self, now: str) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now,))
        self.conn.commit()
        return cur.lastrowid or 0

    def finish_run(self, run_id: int, now: str, stats: dict[str, object]) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?", (now, json.dumps(stats), run_id))
        self.conn.commit()

    def last_finished_run(self) -> str | None:
        row = self.conn.execute("SELECT max(started_at) AS t FROM runs WHERE finished_at IS NOT NULL").fetchone()
        return row["t"] if row else None
