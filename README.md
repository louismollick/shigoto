# shigoto

Self-hosted job aggregator. Every 6 hours it searches job boards for the configured terms and cities, normalizes and deduplicates the results into SQLite, then rebuilds one Google Sheets tab for review (e.g. by ChatGPT). It contains no LLM logic.

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

Indeed and Workday match search terms against descriptions, so "food safety" alone would pull in every line cook with a food-handler certificate. Every job's title must therefore contain one of `title_keywords` (whole word, or prefix with a trailing `*`), and none of `exclude_title_keywords`. New jobs are ingested only for configured cities (or their suburb aliases) and matching titles. Known postings still update if their latest fields fail the filters. Every run also re-applies the current city and title filters to all stored jobs. Jobs that no longer pass get `excluded_at` and disappear from the sheet, but stay in SQLite with their reviews. Relaxing the filters restores them.

## Dedup and change detection

- `job_sources` maps every `(source, source_id)` to a `job_id`, so the same posting seen again is always the same job. It also stores the latest raw adapter record as JSON, before normalization, for `shigoto reprocess`.
- A new posting from another source merges into an existing job when normalized company + title + city match (company suffixes like Inc/Ltd/Canada and punctuation are ignored).
- A job's fields come from its first source; other sources only fill blanks (e.g. Indeed supplies the description for a LinkedIn job). `content_hash` covers only title and description. Only changes to those fields move `Updated At` forward, including description enrichment. Changes to salary, location, company or type update the stored fields without changing `Updated At`. Existing hashes are migrated without changing timestamps.
- A job becomes a closure candidate when every source is a full-listing source (Workday, SmartRecruiters, Greenhouse, Lever, Ashby, SuccessFactors or GC Jobs) and none has seen it for more than two days. Candidates are checked oldest first, at most once per day, with `liveness_max_per_run` defaulting to 40 jobs. Every source must confirm removal before the app sets `closed_at` and marks the job `Closed`: a posting endpoint returns HTTP 404/410, or a successful complete Ashby board response omits the posting. Errors, rate limits, robots restrictions and unexpected responses leave jobs open. Current SmartRecruiters robots rules block its API, Ashby robots retrieval returns 401, and SuccessFactors' generic error redirects are inconclusive, so these checks currently cannot confirm removal. Jobs also seen on Indeed, LinkedIn, Glassdoor or Job Bank stay open. Seeing a closed job again clears `closed_at` and reopens it. Status changes sync without changing `Updated At` or `content_hash`.

## Google Sheet

Only the `Shigoto` tab is touched (created on first run). The app owns columns `Job ID` through `Description`, including `Status` just after `Updated At`. A non-empty header must match that app-owned prefix exactly; an unfamiliar layout stops sync before any writes. The legacy layout without `Status` is no longer supported. Duplicate Job IDs or reviewer header names also stop sync before writes to avoid overwriting reviews.

Every column to the right of `Description` belongs to the reviewer. Names and order can change, and blank headers get a stable name such as `Column X`. At the start of every sync, the app reads the entire tab and commits all reviewer values, including cleared cells, to SQLite's `reviews` table. `meta` stores their header order. `AI Reviewed At` is also copied into `jobs.ai_reviewed_at` by header name; removing that header leaves the stored timestamp alone. Rows with IDs absent from SQLite have their reviews backed up, but are removed from the sheet with a warning.

The app then rebuilds the tab from SQLite in `first_seen`, `job_id` order, pairing each job with its saved reviewer values. Excluded jobs stay hidden; confirmed closed jobs remain visible with `Status = Closed`. The grid shrinks to the written rows and columns. Writes use chunks of at most 200 jobs. App values are written raw so job text cannot become a formula; reviewer values use Google Sheets' user-entered parsing for numbers, dates and checkboxes. Backups contain formatted strings, not formulas or cell formatting. Avoid reviewer edits while a sync is running because the rebuild uses the snapshot taken at its start. If a rebuild fails partway through, the next sync restores the committed snapshot before accepting further reviewer changes; wait for a successful rebuild before editing again.

ChatGPT should skip rows with `Status = Closed`. An open row needs review when `AI Reviewed At` is blank or earlier than `Updated At`.

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
uv run shigoto reprocess --no-sheet                    # replay stored raw jobs, re-apply filters locally
uv run shigoto reprocess                               # replay and rebuild the sheet, without scraping
uv run shigoto serve                                  # loop: next run is due 6h after the last one in the DB
uv run mypy src tests && uv run pytest
```

`reprocess` uses the current normalization and merge rules. It preserves job/source `first_seen` and `last_seen` and does not reopen closed jobs or run liveness checks. Older source rows without raw JSON remain unchanged, though all stored jobs still have their exclusions re-evaluated. Raw records accumulate for accepted jobs and known postings as sources are fetched after this version is deployed. Newly rejected postings are not stored.

CI type-checks, runs the tests, and publishes `ghcr.io/louismollick/shigoto:main` (amd64 + arm64). The VPS runs it from `louismollick-server`'s compose file, and Watchtower picks up new images.

Useful queries:

```bash
sqlite3 /data/shigoto.db "select id, started_at, finished_at, stats from runs order by id desc limit 5"
sqlite3 /data/shigoto.db "select primary_source, city, count(*) from jobs group by 1, 2"
```
