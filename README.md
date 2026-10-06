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
| GC Jobs | Public HTML results, whole board filtered by title and city; native posters and external HTML details | At least 2s between requests; robots.txt restrictions and crawl delays honored |
| SuccessFactors | RMK HTML tiles, per term in Canada | At least 1s between requests; robots.txt restrictions and crawl delays honored; details only for title/city matches |
| Workday | Public CXS API, per term, filtered to Canada or target-city facets | 0.5s between requests, backoff on 429 |
| SmartRecruiters | Public postings API, per term, `country=ca` | 1s between requests |
| Greenhouse, Lever, Ashby | Public board APIs, whole board filtered by title keywords | 1 request per board |

Employer boards live in `config.yaml` (`boards:`). Each was verified on 2026-10-06 to have Canadian postings. SuccessFactors includes Bureau Veritas, Cargill, Nestlé Canada and Bunge; Ingredion and Unilever use Workday. Some employer career sites still lack a verified adapter route, including Intertek, Charles River and Maple Leaf Foods. Their postings may also appear through Indeed and LinkedIn.

GC Jobs is controlled by `gcjobs_enabled`. It lists current public postings without login, using the same second-part HTML request as the site's JavaScript. It preserves the full location text and language requirements. Closing dates stay in the description, not `posted_date`. Listings that only say "Various Locations" cannot pass the city filter; failed detail fetches yield an empty description so an existing complete description is preserved. Nutrien was removed because its CSB API is disallowed by robots.txt.

## Relevance filter

Indeed and Workday match search terms against descriptions, so "food safety" alone would pull in every line cook with a food-handler certificate. Every job's title must therefore contain one of `title_keywords` (whole word, or prefix with a trailing `*`), and none of `exclude_title_keywords`. Only jobs in the configured cities (or their suburb aliases) are kept.

## Dedup and change detection

- `job_sources` maps every `(source, source_id)` to a `job_id`, so the same posting seen again is always the same job.
- A new posting from another source merges into an existing job when normalized company + title + city match (company suffixes like Inc/Ltd/Canada and punctuation are ignored).
- A job's fields come from its first source; other sources only fill blanks (e.g. Indeed supplies the description for a LinkedIn job). `content_hash` covers title, company, location, salary and description, so it changes only on real edits.
- A job becomes a closure candidate when every source is a full-listing source (Workday, SmartRecruiters, Greenhouse, Lever, Ashby, SuccessFactors or GC Jobs) and none has seen it for more than two days. Candidates are checked oldest first, at most once per day, with `liveness_max_per_run` defaulting to 40 jobs. Every source must confirm removal before the app sets `closed_at` and marks the job `Closed`: a posting endpoint returns HTTP 404/410, or a successful complete Ashby board response omits the posting. Errors, rate limits, robots restrictions and unexpected responses leave jobs open. Current SmartRecruiters robots rules block its API, Ashby robots retrieval returns 401, and SuccessFactors' generic error redirects are inconclusive, so these checks currently cannot confirm removal. Jobs also seen on Indeed, LinkedIn, Glassdoor or Job Bank stay open. Seeing a closed job again clears `closed_at` and reopens it. Status changes sync without changing `Updated At` or `content_hash`.

## Google Sheet

Only the `Shigoto` tab is touched (created on first run). The app writes columns `Job ID` through `Description`: it appends new jobs and rewrites a row in place when its content or status changes. Material content changes move `Updated At` forward. `Status`, just after `Updated At`, is `Closed` only after confirmed removal, or blank for open jobs. On the first sync of an older sheet, the app inserts this column and shifts `Description` and the AI columns right, preserving their values and row alignment. Existing rows start with blank status; only jobs with changed content or status are synced. A non-empty header must match the current or legacy app-owned columns exactly; an unfamiliar layout stops sync before any writes. Reviewer column headers may be absent or renamed.

The `AI Reviewed At`, `AI Score` and `AI Notes` columns belong to the reviewer and are never written. `AI Reviewed At` is copied back into SQLite (`jobs.ai_reviewed_at`). ChatGPT should skip rows with `Status = Closed`. An open row needs review when `AI Reviewed At` is blank or earlier than `Updated At`.

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
