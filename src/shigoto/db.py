"""SQLite store: the authoritative job history.

`jobs` holds one row per deduplicated job; `job_sources` maps every (source, source_id)
we've seen to its job. Postings only merge across sources: two ids from the same source
are always two jobs, even with identical company/title/city. A job's fields come from its primary (first) source; other
sources only fill blanks, so the content hash doesn't flip-flop between sources.
`content_hash` changes only on material edits, and `synced_hash` records what the
Google Sheet last received, so `unsynced()` is exactly the delta to push.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from shigoto.models import Job
from shigoto.normalize import dedupe_key, fold

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
    synced_hash TEXT,
    synced_at TEXT
);
CREATE TABLE IF NOT EXISTS job_sources (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    url TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS job_sources_job ON job_sources(job_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT
);
"""

UpsertResult = Literal["new", "changed", "seen"]


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


@dataclass(frozen=True)
class DescriptionTarget:
    job_id: str
    source_id: str
    url: str


def make_job_id(job: Job, distinct: bool = False) -> str:
    """Cross-source identity from the dedupe key. `distinct` adds the source id, for a
    second posting from the same source that happens to share company/title/city."""
    key = dedupe_key(job) + (f"|{job.source}|{job.source_id}" if distinct else "")
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def content_hash(title: str, company: str, location: str, salary: str, description: str) -> str:
    material = "\x1f".join(" ".join(fold(v).split()) for v in (title, company, location, salary, description))
    return hashlib.sha1(material.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert(self, job: Job, now: str) -> UpsertResult:
        """Insert or merge one normalized job. Commit is left to the caller."""
        c = self.conn
        mapped = c.execute("SELECT job_id FROM job_sources WHERE source=? AND source_id=?",
                           (job.source, job.source_id)).fetchone()
        job_id: str = mapped["job_id"] if mapped else make_job_id(job)
        if not mapped and c.execute("SELECT 1 FROM job_sources WHERE job_id=? AND source=?",
                                    (job_id, job.source)).fetchone():
            job_id = make_job_id(job, distinct=True)  # same source, different id: a separate posting
        c.execute(
            """INSERT INTO job_sources (source, source_id, job_id, url, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET last_seen=excluded.last_seen, url=excluded.url""",
            (job.source, job.source_id, job_id, job.url, now, now),
        )
        posted = job.posted_date.isoformat() if job.posted_date else None
        row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            h = content_hash(job.title, job.company, job.location, job.salary, job.description)
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
            "job_type": row["job_type"] or job.job_type,
        }
        h = content_hash(merged["title"], merged["company"], merged["location"], merged["salary"],
                         merged["description"])
        changed = h != row["content_hash"]
        c.execute(
            """UPDATE jobs SET title=:title, company=:company, location=:location, description=:description,
               salary=:salary, job_type=:job_type, posted_date=coalesce(posted_date, :posted),
               content_hash=:hash,
               last_seen=:now, updated_at=CASE WHEN :changed THEN :now ELSE updated_at END
               WHERE job_id=:job_id""",
            {**merged, "posted": posted, "hash": h, "now": now, "changed": changed, "job_id": job_id},
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
        h = content_hash(row["title"], row["company"], row["location"], row["salary"], description)
        self.conn.execute(
            """UPDATE jobs SET description=?, description_checked_at=?, content_hash=?,
               updated_at=CASE WHEN content_hash != ? THEN ? ELSE updated_at END WHERE job_id=?""",
            (description, now, h, h, now, job_id),
        )
        self.conn.commit()

    def unsynced(self) -> list[StoredJob]:
        rows = self.conn.execute(
            """SELECT j.*, (SELECT group_concat(source || ' ' || url, char(10)) FROM job_sources s
                            WHERE s.job_id = j.job_id) AS sources
               FROM jobs j WHERE synced_hash IS NULL OR synced_hash != content_hash
               ORDER BY first_seen, job_id"""
        ).fetchall()
        return [
            StoredJob(
                job_id=r["job_id"], title=r["title"], company=r["company"], city=r["city"],
                location=r["location"], description=r["description"], posted_date=r["posted_date"] or "",
                salary=r["salary"], job_type=r["job_type"], url=r["url"], sources=r["sources"] or "",
                first_seen=r["first_seen"], updated_at=r["updated_at"], content_hash=r["content_hash"],
            )
            for r in rows
        ]

    def mark_synced(self, jobs: list[StoredJob], now: str) -> None:
        self.conn.executemany("UPDATE jobs SET synced_hash=?, synced_at=? WHERE job_id=?",
                              [(j.content_hash, now, j.job_id) for j in jobs])
        self.conn.commit()

    def set_ai_reviewed(self, reviewed: dict[str, str]) -> None:
        self.conn.executemany("UPDATE jobs SET ai_reviewed_at=? WHERE job_id=?",
                              [(v or None, k) for k, v in reviewed.items()])
        self.conn.commit()

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
