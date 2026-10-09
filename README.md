# shigoto

Self-hosted job aggregator. Every 6 hours it searches job boards, normalizes and deduplicates results into SQLite, syncs Google Sheets, then reviews up to 40 jobs with Codex CLI using a saved ChatGPT subscription login. Python handles selection, pay arithmetic, validation and writes. Codex judges job fit from the complete posting.

```
source adapter -> canonical Job -> normalize -> cross-source dedupe -> SQLite -> Google Sheets -> Codex review -> five AI cells
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
- A job's fields come from its first source; other sources only fill blanks, such as Indeed supplying a LinkedIn description. `content_hash` covers title, description, company, city, location, salary and type. Changes to any of those fields move `Updated At` forward, including description enrichment. Existing hashes are migrated without changing historical timestamps.
- A job becomes a closure candidate when every source can be checked and no full-listing source (Workday, SmartRecruiters, Greenhouse, Lever, Ashby, SuccessFactors or GC Jobs) has seen it in the last two days. Indeed, LinkedIn and Job Bank searches are partial, so their postings are checked directly whether or not they are still listed. Glassdoor cannot be checked, so jobs with a Glassdoor source stay open. Candidates are checked stalest first, at most once per day. Every source must confirm removal before the app sets `closed_at` and marks the job `Closed`:
  - Indeed: one GraphQL `jobData` request (the mobile API JobSpy uses) per 100 jobs reports `expired`. Viewjob pages are behind a bot challenge.
  - LinkedIn: the guest posting page returns 404/410 or shows its "No longer accepting applications" banner. Requests are at least 10s apart, and the first inconclusive answer, usually a rate limit, stops LinkedIn checks for the run.
  - Job Bank and board sources: a posting endpoint returns HTTP 404/410 (expired Job Bank postings redirect to a 410), or a successful complete Ashby board response omits the posting.

  Indeed answers come first and settle the job when it is still open there. Other sources share `liveness_max_per_run` (default 40) requests per run; jobs over that budget wait for the next run. Errors, rate limits, robots restrictions and unexpected responses leave jobs open. Current SmartRecruiters robots rules block its API, Ashby robots retrieval returns 401, and SuccessFactors' generic error redirects are inconclusive, so these checks currently cannot confirm removal. Seeing a closed job again clears `closed_at` and queues it for an immediate recheck, since Indeed search can still list expired postings. Status changes sync without changing `Updated At` or `content_hash`.

## Google Sheet

The canonical tab is `Jobs to Review`. The configured `sheet.worksheet_id` identifies the existing master tab across renames; a missing explicit ID stops the run rather than falling back to a same-named lookup view. Without an ID, `sheet.worksheet` selects the tab by name and creates it when missing. The app owns columns `Job ID` through `Description`, including `Status` just after `Updated At`. A non-empty header must match that app-owned prefix exactly; an unfamiliar layout stops sync before any writes. The legacy layout without `Status` is no longer supported. Duplicate Job IDs or reviewer header names also stop sync before writes to avoid overwriting reviews.

Every column to the right of `Description` belongs to the reviewer. Names and order can change, and blank headers get a stable name such as `Column X`. At the start of every sync, the app reads the entire tab and commits all reviewer values, including cleared cells, to SQLite's `reviews` table. `meta` stores their header order. `AI Reviewed At` is also copied into `jobs.ai_reviewed_at` by header name; removing that header leaves the stored timestamp alone. Rows with IDs absent from SQLite have their reviews backed up, but are removed from the sheet with a warning.

The app then rebuilds the tab from SQLite in `first_seen`, `job_id` order, pairing each job with its saved reviewer values. Excluded jobs stay hidden; confirmed closed jobs remain visible with `Status = Closed`. The grid shrinks to the written rows and columns. Writes use chunks of at most 200 jobs. App and AI values are written raw so text cannot become a formula and AI timestamps retain their timezone offsets. AI scores remain numeric. Other reviewer values use Google Sheets' user-entered parsing for dates and checkboxes. Backups contain formatted strings, not formulas or cell formatting. Avoid reviewer edits while a sync is running because the rebuild uses the snapshot taken at its start. If a rebuild fails partway through, the next sync restores the committed snapshot before accepting further reviewer changes; wait for a successful rebuild before editing again.

The review step reads all underlying rows, including filtered-out rows, and skips `Status = Closed`. It resolves columns by header and rows by Job ID. It writes only `AI Reviewed At`, `AI Score`, `AI Notes`, `AI Decision` and `AI Category`, one job per request with a readback check. It never changes filters, sort, headers, dropdowns or grid size, and never edits the `Applied` or `Closed` formula views. The scrape's existing sync and control setup still run before review.

After every sync, `shigoto.sheet_controls.configure_tracker` reapplies native controls; no Apps Script is needed. `Application Status` and `Application Stage` get dropdowns. `Applied At` and `Follow-up Date` get date formats and a date picker. If the tab has no basic filter, one is created that hides `Status = Closed` and `AI Decision = Reject` (blanks stay visible) and sorts by `AI Score`, highest first. An existing filter keeps its criteria and sort, even when both are empty; only its range grows to cover the rebuilt grid. Cell values are never written.

When `Application Status` exists, sync writes `Not Applied` for jobs with a blank or missing status, including new jobs. Existing statuses and all other reviewer values are preserved.

The `Closed` tab is read-only and built from two formulas. Widen both if reviewer columns go past X:

- A1: `=ARRAYFORMULA('Jobs to Review'!A1:X1)`
- A2: `=IFNA(FILTER('Jobs to Review'!A2:X,'Jobs to Review'!M2:M="Closed"),"")`

## Config

`config.yaml` holds search terms, cities (with suburb aliases), JobSpy settings, employer boards and keyword filters. Env vars:

| Var | Default (Docker) |
| --- | --- |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/secrets/google.json` (service account with edit access to the sheet) |
| `SHIGOTO_SPREADSHEET_ID` | required |
| `SHIGOTO_DB` | `/data/shigoto.db` |
| `SHIGOTO_CONFIG` | `/app/config.yaml` (mount your own to override) |
| `CODEX_HOME` | `/data/codex` in Docker; `data/codex` locally |

## Codex reviews

`review.enabled: true` in the shipped config runs reviews after each successful scrape and Sheet sync. `run --no-sheet` and `reprocess` do not review. Manual `review` reads the existing master tab without rebuilding it. Missing required headers stop review rather than changing the sheet. Scheduled and manual commands share a nonblocking lock next to SQLite.

Selection is deterministic: open jobs with a missing, invalid or stale AI timestamp, or no published result for the current posting and policy version, newest `Updated At` first, at most `review.max_per_run` jobs, capped at 40. Timestamps are compared with their offsets. The first Codex run re-reviews old ChatGPT scores once. This version check replaces the old fixed-date cutoff, so jobs are not repeatedly reselected before that date. Changing the prompt, model or reasoning setting queues a new review.

Each fresh Codex invocation receives the candidate profile and scoring instructions in [review_prompt.md](src/shigoto/review_prompt.md), plus the full job data. Possible duplicates with the same company, title and city are included for comparison; Codex must confirm that their descriptions describe the same opening. Confirmed duplicates share the most complete posting's judgment, including across capped runs. Separate shifts and plants can receive different judgments.

Codex returns schema-checked JSON containing the base score, duties and gaps, category, hard blockers, pay facts and duplicate IDs. Python applies the pay penalty and decision thresholds, caps hard blockers below 30, adds limited-description notes and the current Winnipeg timestamp, then persists the result in SQLite's `ai_results` table before publishing. Failed writes resume from the cached judgment without another model call. A rejected payload gets one retry with `Notes withheld: write blocked.`; transport and quota retries retain the original notes. Logs include each Job ID, exact errors, original notes and retry outcome. Reads are paced below the service-account quota.

Login, usage-limit, process and timeout failures stop further model calls for that run. Already prepared results can still publish; remaining jobs wait for the next scrape. Review failure does not undo a completed scrape or sync. Normal reports contain reviewed and pending counts, decisions and failures; dry runs also include proposed values.

The subprocess uses a temporary working directory, a dedicated `CODEX_HOME`, an ephemeral session and no inherited API keys, Google credentials or T3 configuration. Shell tools, agents, apps and web search are disabled. Posting text is treated as untrusted data. Codex has no Sheets tools; Shigoto uses its existing service-account connection to write results.

### Subscription login

Install the pinned CLI locally with `npm install -g @openai/codex@0.160.0`. Docker includes it already. Authenticate once as the same user that runs Shigoto. The container's default file storage keeps credentials in its dedicated persistent directory:

```bash
# Local development
CODEX_HOME="$PWD/data/codex" codex login --device-auth

# Existing Docker Compose service, with its /data volume mounted
docker compose exec shigoto codex login --device-auth
docker compose exec shigoto codex login status
```

Complete the device login in your browser. Keep `/data` persistent across container updates, with access restricted to the service user. Do not mount your normal Codex home, plugins or configuration. This runner forces ChatGPT login and does not fall back to API billing. Subscription usage limits still apply. See the official [authentication](https://learn.chatgpt.com/docs/auth), [noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode) and [usage](https://learn.chatgpt.com/docs/pricing) docs.

After enabling the service, disable the old ChatGPT scheduled review task so there is only one reviewer. A lost or revoked login needs another device login; pending jobs remain in the queue.

## Running

```bash
uv sync
uv run shigoto run --no-sheet --only indeed,jobbank   # one run, selected sources, no sheet
uv run shigoto run                                    # one full run
uv run shigoto reprocess --no-sheet                    # replay stored raw jobs, re-apply filters locally
uv run shigoto reprocess                               # replay and rebuild the sheet, without scraping
uv run shigoto review --dry-run --limit 3               # inspect proposed reviews without saving or writing
uv run shigoto review --job JOB_ID --limit 1            # publish one eligible job's review
uv run shigoto review                                  # review the next batch without scraping or rebuilding
uv run shigoto serve                                  # loop: next run is due 6h after the last one in the DB
uv run mypy src tests && uv run pytest
```

`reprocess` uses the current normalization and merge rules. It preserves job/source `first_seen` and `last_seen` and does not reopen closed jobs or run liveness checks. Older source rows without raw JSON remain unchanged, though all stored jobs still have their exclusions re-evaluated. Raw records accumulate for accepted jobs and known postings as sources are fetched after this version is deployed. Newly rejected postings are not stored.

`review --job` still respects eligibility; it does not force a fresh score for an already current job. Dry runs may consume subscription usage but do not save judgments or change Sheet cells.

CI type-checks, runs the tests, and publishes `ghcr.io/louismollick/shigoto:main` (amd64 + arm64). The VPS runs it from `louismollick-server`'s compose file, and Watchtower picks up new images.

Useful queries:

```bash
sqlite3 /data/shigoto.db "select id, started_at, finished_at, stats from runs order by id desc limit 5"
sqlite3 /data/shigoto.db "select primary_source, city, count(*) from jobs group by 1, 2"
```
