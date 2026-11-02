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
from shigoto.employer import employer_link, external_apply_url
from shigoto.models import Job, Liveness, SourceLink, job_json
from shigoto.normalize import RejectionReason, dedupe_key, part_time_only, title_excluded, title_matches

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
    description_attempted_at TEXT,
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
    apply_url TEXT,
    gone_at TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS job_sources_job ON job_sources(job_id);
CREATE TABLE IF NOT EXISTS rejected_postings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    raw TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('location', 'title', 'excluded_title', 'company', 'job_type')),
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
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
    "workday", "smartrecruiters", "greenhouse", "lever", "ashby", "successfactors",
})
# Searches here are partial, so absence proves nothing, but each posting can be checked directly.
DIRECT_CHECK_SOURCES = frozenset({"indeed", "linkedin", "jobbank"})
UNCHECKABLE_SOURCES = frozenset({"zip_recruiter", "glassdoor"})
CLOSED_AFTER = timedelta(days=2)
UNCHECKABLE_CLOSED_AFTER = timedelta(days=30)
EMPLOYER_CLOSURE_RECHECK = timedelta(days=7)

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
    attempted_at: str | None = None

@dataclass(frozen=True)
class LivenessTarget:
    """`sources` are the links to check; `apply_urls` covers every link, even uncheckable ones.
    `long_unseen` means no link has been seen for UNCHECKABLE_CLOSED_AFTER."""

    job_id: str
    sources: list[SourceLink]
    apply_urls: list[str]
    long_unseen: bool


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


def _has_employer_check(apply_url: str | None) -> bool:
    return apply_url is not None and employer_link(apply_url) is not None


def current_reject(review: dict[str, str], updated_at: str) -> bool:
    """An AI rejection of the posting as it stands; a changed posting needs a new review."""
    if review.get("AI Decision") != "Reject":
        return False
    try:
        reviewed, updated = datetime.fromisoformat(review.get("AI Reviewed At", "")), datetime.fromisoformat(updated_at)
    except ValueError:
        return False  # A missing or invalid timestamp leaves the job unreviewed.
    return reviewed.tzinfo is not None and updated.tzinfo is not None and reviewed >= updated


def best_url(job_url: str, links: list[sqlite3.Row]) -> str:
    """The link to show: the employer's page, else a listing not known to be gone.

    Employer pages come first even when an aggregator is still live: applications end up
    there anyway, and a dead employer page is worth seeing right away.
    """
    live = sorted((link for link in links if link["gone_at"] is None),
                  key=lambda link: link["source"] in UNCHECKABLE_SOURCES)
    employer = [link["url"] for link in live if link["source"] in FULL_LISTING_SOURCES]
    employer += [link["apply_url"] for link in links if link["apply_url"]]
    if employer:
        return str(employer[0])
    urls = [str(link["url"]) for link in live]
    return job_url if job_url in urls or not urls else urls[0]


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column in ("closed_at", "liveness_checked_at", "excluded_at", "description_attempted_at"):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        if "liveness_alive_streak" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN liveness_alive_streak INTEGER NOT NULL DEFAULT 0")
        if "closed_by_employer" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN closed_by_employer INTEGER NOT NULL DEFAULT 0")
        rejected_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='rejected_postings'").fetchone()["sql"]
        if "'job_type'" not in rejected_sql:
            # SQLite can't alter a CHECK constraint; rebuild the table with the current schema in one
            # transaction, so an interrupted copy can't leave an empty table that looks migrated.
            self.conn.executescript("BEGIN; ALTER TABLE rejected_postings RENAME TO rejected_postings_old;"
                                    + SCHEMA + """INSERT INTO rejected_postings SELECT * FROM rejected_postings_old;
                                    DROP TABLE rejected_postings_old; COMMIT;""")
        source_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(job_sources)")}
        for column in ("raw", "apply_url", "gone_at"):
            if column not in source_columns:
                self.conn.execute(f"ALTER TABLE job_sources ADD COLUMN {column} TEXT")
        self.conn.create_function("has_employer_check", 1, _has_employer_check, deterministic=True)
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

    def upsert(self, job: Job, now: str, *, raw: str | None = None, seen: bool = True,
               first_seen: str | None = None, last_seen: str | None = None) -> UpsertResult:
        """Merge a normalized job. `seen=False` replays raw data without changing recency or closure.

        Sightings reopen closed jobs for a recheck, except that aggregators keep listing postings
        employers removed: a job its employer closed only reopens on the employer's listing, or
        once a week so a requisition reopened at the same URL, or a wrong answer, can recover.
        """
        c = self.conn
        mapped = c.execute("SELECT job_id FROM job_sources WHERE source=? AND source_id=?",
                           (job.source, job.source_id)).fetchone()
        job_id: str = mapped["job_id"] if mapped else make_job_id(job)
        if not mapped:
            rejected = c.execute("SELECT first_seen FROM rejected_postings WHERE source=? AND source_id=?",
                                 (job.source, job.source_id)).fetchone()
            if rejected and first_seen is None:
                first_seen = rejected["first_seen"]
        if not mapped and c.execute("SELECT 1 FROM job_sources WHERE job_id=? AND source=?",
                                    (job_id, job.source)).fetchone():
            job_id = make_job_id(job, distinct=True)  # same source, different id: a separate posting
        c.execute(
            """INSERT INTO job_sources (source, source_id, job_id, url, first_seen, last_seen, raw, apply_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET
               last_seen=CASE WHEN ? THEN excluded.last_seen ELSE job_sources.last_seen END,
               url=excluded.url, raw=excluded.raw, apply_url=coalesce(excluded.apply_url, job_sources.apply_url),
               gone_at=CASE WHEN ? THEN NULL ELSE job_sources.gone_at END""",
            (job.source, job.source_id, job_id, job.url, first_seen or now, last_seen or now,
             raw if raw is not None else job_json(job), external_apply_url(job.apply_url) or None, seen,
             # A full listing proves the posting is live; aggregators' search results don't.
             seen and job.source in FULL_LISTING_SOURCES),
        )
        c.execute("DELETE FROM rejected_postings WHERE source=? AND source_id=?", (job.source, job.source_id))
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
                 posted, job.salary, job.job_type, job.url, h, first_seen or now, last_seen or now, now),
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
        reopen = seen and (not row["closed_by_employer"] or job.source in FULL_LISTING_SOURCES
                           or datetime.fromisoformat(now) - datetime.fromisoformat(row["closed_at"])
                           >= EMPLOYER_CLOSURE_RECHECK)
        c.execute(
            """UPDATE jobs SET title=:title, company=:company, location=:location, description=:description,
               salary=:salary, job_type=:job_type, city=:city, posted_date=coalesce(posted_date, :posted),
               content_hash=:hash, closed_at=CASE WHEN :reopen THEN NULL ELSE closed_at END,
               closed_by_employer=CASE WHEN :reopen THEN 0 ELSE closed_by_employer END,
               liveness_checked_at=CASE WHEN :reopen AND closed_at IS NOT NULL THEN NULL
                                        ELSE liveness_checked_at END,
               last_seen=CASE WHEN :seen THEN :now ELSE last_seen END,
               updated_at=CASE WHEN :changed THEN :now ELSE updated_at END
               WHERE job_id=:job_id""",
            {**merged, "posted": posted, "hash": h, "now": now, "changed": changed, "seen": seen,
             "reopen": reopen, "job_id": job_id},
        )
        return "changed" if changed else "seen"

    def commit(self) -> None:
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def reject(self, job: Job, raw: str, reason: RejectionReason, now: str, *, seen: bool = True) -> None:
        self.conn.execute(
            """INSERT INTO rejected_postings (source, source_id, raw, reason, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, source_id) DO UPDATE SET
               raw=excluded.raw, reason=excluded.reason,
               last_seen=CASE WHEN ? THEN excluded.last_seen ELSE rejected_postings.last_seen END""",
            (job.source, job.source_id, raw, reason, now, now, seen),
        )

    def prune_rejected(self, now: str) -> int:
        cutoff = (datetime.fromisoformat(now) - timedelta(days=60)).isoformat()
        return self.conn.execute("DELETE FROM rejected_postings WHERE julianday(last_seen) < julianday(?)",
                                 (cutoff,)).rowcount

    def missing_descriptions(self, source: str, limit: int | None = None) -> list[DescriptionTarget]:
        """Pending descriptions for `source`: untried newest first, then oldest failed attempts."""
        rows = self.conn.execute(
            """SELECT j.job_id, s.source_id, s.url, j.description_attempted_at
               FROM jobs j JOIN job_sources s ON s.job_id = j.job_id
               WHERE s.source=? AND j.description='' AND j.description_checked_at IS NULL
                   AND j.excluded_at IS NULL AND j.closed_at IS NULL
               GROUP BY j.job_id ORDER BY j.description_attempted_at IS NOT NULL,
                   j.description_attempted_at, j.first_seen DESC LIMIT ?""",
            (source, -1 if limit is None else limit),
        ).fetchall()
        return [DescriptionTarget(r["job_id"], r["source_id"], r["url"], r["description_attempted_at"])
                for r in rows]

    def described_urls(self, source: str) -> list[tuple[str, str, str]]:
        """(source URL, description, salary) for every described job seen on `source`."""
        rows = self.conn.execute(
            """SELECT s.url, j.description, j.salary FROM jobs j JOIN job_sources s ON s.job_id = j.job_id
               WHERE s.source=? AND j.description<>''""",
            (source,),
        ).fetchall()
        return [(r["url"], r["description"], r["salary"]) for r in rows]

    def record_description_attempt(self, job_id: str, now: str) -> None:
        """Move a failed fetch behind other pending descriptions without marking it checked."""
        self.conn.execute("UPDATE jobs SET description_attempted_at=? WHERE job_id=?", (now, job_id))
        self.conn.commit()

    def set_description(self, job_id: str, description: str, now: str, salary: str = "") -> None:
        """Store a fetched description. A detail-page salary only fills an empty salary."""
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        salary = row["salary"] or salary
        h = content_hash(row["title"], description, company=row["company"], city=row["city"],
                         location=row["location"], salary=salary, job_type=row["job_type"])
        self.conn.execute(
            """UPDATE jobs SET description=?, salary=?, description_checked_at=?, content_hash=?,
               updated_at=CASE WHEN content_hash != ? THEN ? ELSE updated_at END WHERE job_id=?""",
            (description, salary, now, h, h, now, job_id),
        )
        self.conn.commit()

    def liveness_candidates(self, now: str) -> list[LivenessTarget]:
        """Included open jobs due for closure checks, least recently checked first, then stalest.

        Excluded jobs, skipped jobs and current AI rejects spend no requests. Applied jobs remain
        eligible, and a changed posting resumes checks until it has a current review again.
        Uncheckable-only skips and rejects still close after 30 days unseen, since that needs no request.

        A job is due a day after its last check; each consecutive "alive" answer beyond the first
        doubles that wait, up to 4 days, so postings that stay open use fewer requests. Ordering by
        staleness alone let stale jobs that keep answering "alive" use the budget and starve the rest.

        Full-listing sources must also be missing from their listings for CLOSED_AFTER;
        a recent sighting there already proves the job is open. Uncheckable links are
        omitted, but an apply URL we can check makes even an uncheckable-only job checkable.
        """
        cutoff = (datetime.fromisoformat(now) - CLOSED_AFTER).isoformat()
        uncheckable_cutoff = (datetime.fromisoformat(now) - UNCHECKABLE_CLOSED_AFTER).isoformat()
        full, checkable = sorted(FULL_LISTING_SOURCES), sorted(FULL_LISTING_SOURCES | DIRECT_CHECK_SOURCES)
        supported = sorted(set(checkable) | UNCHECKABLE_SOURCES)
        rows = self.conn.execute(
            f"""SELECT j.job_id, j.updated_at, max(julianday(s.last_seen)) <= julianday(?) AS long_unseen,
                    max(s.source IN ({",".join("?" * len(checkable))})
                        OR has_employer_check(s.apply_url)) AS has_checkable_source
                FROM jobs j JOIN job_sources s ON s.job_id=j.job_id
                WHERE j.excluded_at IS NULL AND j.closed_at IS NULL AND (j.liveness_checked_at IS NULL
                    OR julianday(j.liveness_checked_at)
                       <= julianday(?) - min(1 << max(j.liveness_alive_streak - 1, 0), 4))
                GROUP BY j.job_id
                HAVING min(s.source IN ({",".join("?" * len(supported))}))=1
                    AND CASE WHEN max(s.source IN ({",".join("?" * len(checkable))})
                                      OR has_employer_check(s.apply_url))=1
                        THEN coalesce(max(CASE WHEN s.source IN ({",".join("?" * len(full))})
                                          THEN julianday(s.last_seen) END), 0) < julianday(?)
                        ELSE max(julianday(s.last_seen)) <= julianday(?) END
                ORDER BY coalesce(julianday(j.liveness_checked_at), 0), max(julianday(s.last_seen)), j.job_id""",
            (uncheckable_cutoff, *checkable, now, *supported, *checkable, *full, cutoff, uncheckable_cutoff),
        ).fetchall()
        reviews = self.reviews()
        # Published AI decisions can be newer than the last Sheet backup. Unpublished results
        # do not supersede it; a later backup also preserves manual decision edits and clears.
        for result in self.conn.execute(
            """SELECT a.job_id, a.fields FROM ai_results a LEFT JOIN reviews r ON r.job_id=a.job_id
               WHERE a.written_at IS NOT NULL
                   AND julianday(a.written_at) > coalesce(julianday(r.updated_at), 0)
               ORDER BY julianday(a.written_at), a.rowid"""
        ):
            fields = json.loads(result["fields"])
            reviews.setdefault(result["job_id"], {}).update({
                "AI Decision": fields["decision"], "AI Reviewed At": fields["reviewed_at"],
            })
        targets = []
        for row in rows:
            review = reviews.get(row["job_id"], {})
            applied = (review.get("Application Status", "").strip().casefold() == "applied"
                       or bool(review.get("Applied At", "").strip()))
            skipped = review.get("Application Status", "").strip().casefold() == "skip"
            if not row["has_checkable_source"] or applied or not (skipped or current_reject(review, row["updated_at"])):
                targets.append(row)
        result = []
        for row in targets:
            links = self.conn.execute("SELECT source, source_id, url, apply_url FROM job_sources WHERE job_id=?",
                                      (row["job_id"],)).fetchall()
            result.append(LivenessTarget(
                row["job_id"],
                [SourceLink(s["source"], s["source_id"], s["url"]) for s in links
                 if s["source"] not in UNCHECKABLE_SOURCES],
                [s["apply_url"] for s in links if s["apply_url"]],
                bool(row["long_unseen"]),
            ))
        return result

    def record_liveness(self, job_id: str, result: Liveness, now: str, *, by_employer: bool = False) -> None:
        """Status changes leave material content and its update timestamp untouched."""
        self.conn.execute(
            """UPDATE jobs SET liveness_checked_at=:now,
               liveness_alive_streak=CASE :result WHEN 'alive' THEN liveness_alive_streak + 1
                                                  WHEN 'gone' THEN 0 ELSE liveness_alive_streak END,
               closed_by_employer=:by_employer,
               closed_at=CASE WHEN :result='gone' THEN :now ELSE closed_at END WHERE job_id=:job_id""",
            {"now": now, "result": result, "job_id": job_id, "by_employer": by_employer},
        )
        self.conn.commit()

    def record_link(self, link: SourceLink, result: Liveness, now: str) -> None:
        """Remember one link's answer, so the sheet can avoid a dead link while a job stays open.

        A sighting doesn't clear it: Indeed search can still list expired postings.
        """
        if result != "unknown":
            self.conn.execute("UPDATE job_sources SET gone_at=? WHERE source=? AND source_id=?",
                              (now if result == "gone" else None, link.source, link.source_id))

    def record_apply_url(self, source: str, source_id: str, apply_url: str) -> None:
        self.conn.execute("UPDATE job_sources SET apply_url=? WHERE source=? AND source_id=?",
                          (apply_url, source, source_id))

    def dropped_from_listing(self, link: SourceLink, now: str) -> bool:
        """The link's board answered within CLOSED_AFTER without it, so absence proves removal.

        Requiring a recent sighting of another posting on the same board means a failed
        scrape, or a board removed from the config, can't look like every posting closing.
        Source IDs are `<board>:<posting>`.
        """
        board = link.source_id.rpartition(":")[0] + ":"
        cutoff = (datetime.fromisoformat(now) - CLOSED_AFTER).isoformat()
        return self.conn.execute(
            """SELECT 1 FROM job_sources mine JOIN job_sources other
                   ON other.source=mine.source AND substr(other.source_id, 1, ?)=?
               WHERE mine.source=? AND mine.source_id=? AND julianday(mine.last_seen) < julianday(?)
                   AND julianday(other.last_seen) >= julianday(?) LIMIT 1""",
            (len(board), board, link.source, link.source_id, cutoff, cutoff),
        ).fetchone() is not None

    def visible_jobs(self) -> list[StoredJob]:
        """All currently included jobs, with a stable order for the sheet rebuild."""
        rows = self.conn.execute(
            """SELECT j.*, (SELECT group_concat(source || ' ' || url, char(10)) FROM job_sources s
                            WHERE s.job_id = j.job_id) AS sources
               FROM jobs j WHERE excluded_at IS NULL
               ORDER BY first_seen, job_id"""
        ).fetchall()
        links: dict[str, list[sqlite3.Row]] = {}
        for link in self.conn.execute(
                "SELECT job_id, source, url, apply_url, gone_at FROM job_sources ORDER BY first_seen, source"):
            links.setdefault(link["job_id"], []).append(link)
        return [
            StoredJob(
                job_id=r["job_id"], title=r["title"], company=r["company"], city=r["city"],
                location=r["location"], description=r["description"], posted_date=r["posted_date"] or "",
                salary=r["salary"], job_type=r["job_type"], url=best_url(r["url"], links.get(r["job_id"], [])),
                sources=r["sources"] or "",
                first_seen=r["first_seen"], updated_at=r["updated_at"], content_hash=r["content_hash"],
                status="Closed" if r["closed_at"] is not None else "",
            )
            for r in rows
        ]

    def reevaluate_exclusions(self, config: Config, now: str) -> int:
        """Hide jobs outside current filters, retaining their history and reviews. Undated jobs stay eligible."""
        cities = {city.name for city in config.cities}
        cutoff = ((datetime.fromisoformat(now).date() - timedelta(days=config.max_posting_age_days)).isoformat()
                  if config.max_posting_age_days is not None else None)
        excluded = 0
        for row in self.conn.execute("SELECT job_id, city, title, company, job_type, posted_date FROM jobs").fetchall():
            passes = (row["city"] in cities and title_matches(row["title"], config.title_keywords)
                      and not title_excluded(row["title"], config.exclude_title_keywords,
                                             config.exclude_title_exceptions)
                      and not title_matches(row["company"], config.exclude_companies)
                      and not part_time_only(row["job_type"])
                      and (cutoff is None or not row["posted_date"] or row["posted_date"] >= cutoff))
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
