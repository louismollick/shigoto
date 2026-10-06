# shigoto

Self-hosted job aggregator. Every 6 hours it searches job boards for the configured terms and cities, normalizes and deduplicates the results into SQLite, then pushes new and changed jobs to one Google Sheets tab for review (e.g. by ChatGPT). It contains no LLM logic.

```
source adapter -> canonical Job -> normalize -> cross-source dedupe -> SQLite -> Google Sheets
```

## Sources

| Source | How | Rate limiting |
| --- | --- | --- |
| Indeed | [JobSpy](https://github.com/speedyapply/JobSpy), every term x city | JobSpy: effectively unlimited |
| LinkedIn | JobSpy, all terms OR'd into one query per city, 90s between cities | Blocks around the 10th page per IP, so ~5-15 page requests per run. Stops for the run on 429. Descriptions fetched for at most 25 new jobs per run, 10-15s apart |
| Glassdoor | JobSpy, off by default | Blocks after ~30 requests; no descriptions |
| Canada Job Bank | Public Atom feed (ported from [Career Ops](https://github.com/career-ops-hq/career-ops)) | robots.txt `Crawl-delay: 5` honored for feed and detail pages |
| Workday | Public CXS API, per term, filtered to Canada or target-city facets | 0.5s between requests, backoff on 429 |
| SmartRecruiters | Public postings API, per term, `country=ca` | 1s between requests |
| Greenhouse, Lever, Ashby | Public board APIs, whole board filtered by title keywords | 1 request per board |

Employer boards live in `config.yaml` (`boards:`). Each was verified on 2026-10-06 to have Canadian postings. Employers on unsupported ATSes (Oracle, SuccessFactors, Taleo, iCIMS, Rippling) include Intertek, Bureau Veritas, Charles River, Cargill and Maple Leaf Foods. Most of their postings still reach us through Indeed and LinkedIn.

## Dedup and change detection

- `job_sources` maps every `(source, source_id)` to a `job_id`, so the same posting seen again is always the same job.
- A new posting from another source merges into an existing job when normalized company + title + city match (company suffixes like Inc/Ltd/Canada and punctuation are ignored).
- A job's fields come from its first source; other sources only fill blanks (e.g. Indeed supplies the description for a LinkedIn job). `content_hash` covers title, company, location, salary and description, so it changes only on real edits.

## Google Sheet

Only the `Shigoto` tab is touched (created on first run). The app writes columns `Job ID` through `Description`: it appends new jobs and rewrites a row in place when its job materially changes (`Updated At` moves forward). The `AI Reviewed At`, `AI Score` and `AI Notes` columns belong to the reviewer and are never written. `AI Reviewed At` is copied back into SQLite (`jobs.ai_reviewed_at`).

A row needs review when `AI Reviewed At` is blank or earlier than `Updated At`.

## Config

`config.yaml` holds search terms, cities (with suburb aliases), JobSpy settings, employer boards and keyword filters. Env vars:

| Var | Default (Docker) |
| --- | --- |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/secrets/google.json` (service account with edit access to the sheet) |
| `SHIGOTO_SPREADSHEET_ID` | required |
| `SHIGOTO_DB` | `/data/shigoto.db` |
| `SHIGOTO_CONFIG` | `/app/config.yaml` (mount your own to override) |

## Running

```bash
uv sync
uv run shigoto run --no-sheet --only indeed,jobbank   # one run, selected sources, no sheet
uv run shigoto run                                    # one full run
uv run shigoto serve                                  # loop: next run is due 6h after the last one in the DB
uv run mypy src tests && uv run pytest
```

CI type-checks, runs the tests, and publishes `ghcr.io/louismollick/shigoto:main` (amd64 + arm64). The VPS runs it from `louismollick-server`'s compose file, and Watchtower picks up new images.

Useful queries:

```bash
sqlite3 /data/shigoto.db "select id, started_at, finished_at, stats from runs order by id desc limit 5"
sqlite3 /data/shigoto.db "select primary_source, city, count(*) from jobs group by 1, 2"
```
