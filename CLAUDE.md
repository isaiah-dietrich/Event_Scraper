# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A weekly job, run automatically by a scheduled GitHub Action
(`.github/workflows/weekly-digest.yml`, Mondays 9am Central — two fixed-UTC
cron entries plus a `gate` job that kills the one that isn't 9am local this
half of the year): scrapes a fixed
list of websites (`SITE_URLS` in `cli/batch.py`) for AI/ML-related events,
uses Claude to extract and score them against a fit rubric (in-person,
Georgia-based, AI-focused), dedupes against an internal history workbook so
only events not seen in a prior run count as new, and emails a digest
spreadsheet of just that week's new events to a configured recipient. There
is no shared workbook — the only output is the weekly email and its
attachment. `python run.py` still works as a manual/local run (e.g. for an
ad-hoc request) — see README.md's "Automated weekly run" section
for how it stays in sync with the Action's dedupe state, held on a separate
`state` branch rather than committed to `main`.

## Commands

### Setup
```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
export FIRECRAWL_API_KEY=fc-...
```
`ANTHROPIC_API_KEY` and `FIRECRAWL_API_KEY` must both be set; `cli/batch.py:main` exits immediately with an error if either is missing. Actually sending the weekly email additionally requires four more env vars (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_RECIPIENT_EMAIL`, `DIGEST_RECIPIENT_NAME`) — see README.md for how to generate a Gmail app password. None of these are required to run with `--no-email`.

### Run
```
python run.py             # weekly digest run: scrape SITE_URLS, dedupe against events_master.xlsx, write weekly_digests/new_events_<date>.xlsx if there are new events, append everything to the master, email the digest to the configured recipient
python run.py --no-email  # same run, but prints the composed email to stdout instead of sending it (the digest file is still written to disk)
```
`--no-email` is the only flag. There are no other CLI flags anymore — no scratch-URL test mode, no wipe-before-write mode, no diagnostic per-URL output mode — and no input spreadsheet to point at. `SITE_URLS` is the only site list.

No automated test suite is maintained on `main` (it lives on the `tests` branch instead) — verify changes with offline scripts that synthesize rows / mock the Anthropic client, never by running `python run.py` for real, which bills API money for every site scraped.

## Architecture

Pipeline per site URL, orchestrated by `cli/batch.py:process_site`:

**fetch** (`scrape/fetch.py`) → **reduce** (`scrape/reduce.py`) → **extract** (`scrape/extract.py`, Claude Sonnet) → **filter** (`cli/batch.py`, no AI call) → **score** (`scrape/score.py`, Claude Haiku)

Once every site's `process_site` call has returned, `cli/batch.py:main` aggregates all rows across sites and hands them to **write** (`utility/io_excel.py`) and then **email** (`utility/email_digest.py`).

Details worth knowing before touching any one stage:

- **fetch**: A single Firecrawl `scrape` API call per URL (`fetch_page_markdown` in `scrape/fetch.py`, `only_main_content=True`), returning clean markdown of the fully-rendered page. Firecrawl's own browser infrastructure handles JS rendering, embedded `<iframe>` calendars (Google Calendar, Localist, GrowthZone/ChamberMaster), scrolling, and anti-bot evasion server-side — confirmed live against both currently-active `SITE_URLs`, including the TAG Online site whose entire event calendar lives inside a GrowthZone iframe, with no site-specific handling needed on our end. The SDK's client retries transient failures itself, so `fetch_page_markdown` doesn't implement its own retry loop. Concurrency is capped independently via `MAX_CONCURRENT_SCRAPES` (a `threading.Semaphore`, currently 2 to match the Firecrawl account's plan limit) so `cli/batch.py`'s `MAX_WORKERS` can't fire more scrape calls at once than the plan allows, regardless of how high `MAX_WORKERS` itself is set. One thing carried forward from the Playwright-era fetch: numbered `?page=N` pagination is still followed up to `_MAX_ADDITIONAL_PAGES` (`_page_urls_to_follow`), stopping early when a page near-duplicates one already fetched (`_pages_are_near_duplicate`) — Firecrawl scrapes exactly the URL it's given and doesn't discover this pagination convention on its own.
- **reduce**: Now just one pass — `collapse_repeated_blocks` in `scrape/reduce.py` drops any contiguous run of ≥ `_MIN_DUP_BLOCK_LINES` byte-identical lines already seen earlier in the text, keeping the first occurrence. Everything else the old Playwright-era reduce did (tag-stripping, `<img alt>` inlining, href-inlining, entity decoding) is unnecessary now: Firecrawl's markdown already has links as `[Label](URL)` and no raw tags. The block-collapse is still real work, not dead weight — a live test scrape of the TAG Online calendar repeated an entire "TAGwire" news block 3 times verbatim in Firecrawl's own markdown output, and this is what catches that (line-level, conservative: only a long run of *consecutive, byte-identical* lines is removed, never a single recurring line like "6:00 PM" that legitimately repeats across different events).
- **extract**: One Claude Sonnet call per site (`MODEL = "claude-sonnet-4-6"` in `scrape/extract.py`), given the reduced page text and today's date, returns a JSON array of events with the fields in `EXTRACTION_FIELDS`. Raises `ValueError` on a truncated (`max_tokens`) or malformed-JSON response; `process_site` catches that and turns it into a `status: failed` row rather than aborting the whole batch. Before extraction, `process_site` short-circuits bot-challenge pages (short reduced text containing a `_BOT_CHALLENGE_MARKERS` phrase like "just a moment") into a failed row with zero AI calls.
- **filter**: Two AI-free filters run before scoring, in `cli/batch.py`, so a scoring call is never wasted on an event that can't qualify: `_filter_past_events` drops events dated before today (unparseable dates are kept, with a logged warning, rather than silently dropped) and replaces a parsed date string with a real `datetime.datetime` so Excel can sort on it and dedupe can compare it across runs; `_split_by_state` auto-rejects events whose location confidently names a non-Georgia US state or a foreign country (`_extract_us_state` / `_extract_foreign_country`, both string-matched against the trailing comma-separated segment). Ambiguous locations ("Virtual", "Zoom", a bare city, no location at all) fall through to normal scoring rather than being guessed at. A separate, pre-scoring dedupe also runs here: `main()` loads every event key already in the master workbook (`read_existing_event_keys`) and `process_site` drops already-known events before any Haiku call — see **write** below for how that key is built.
- **score**: One Claude Haiku call per surviving event (cheaper model — it's just a classification task). `SCORING_CRITERIA` in `scrape/score.py` *is* the fit rubric, written as plain English instructions to the model — edit it directly to retune what counts as a good fit (e.g. AI-relevance is judged from the event's own content, not the hosting org; a blank location is not assumed to be virtual).
- **write** (`utility/io_excel.py`): Two distinct outputs, built once per run after every site has finished, sharing the same 11 columns (`OUTPUT_COLUMNS`) and banded-table styling. `append_rows` grows the **internal master workbook** (`events_master.xlsx`, created automatically on first run, gitignored, never sent to or edited by anyone) — the permanent, append-only record of every event ever seen, split into its own **Events** and **Rejected Events** sheets (rejected = a confident 1/5 score, or auto-rejected by the state/country filter). `write_weekly_digest` builds the **weekly digest**, a fresh standalone workbook rebuilt from scratch every run (any existing file at that path is overwritten) with **New Events**/**Rejected Events** sheets — only this run's "ok" rows, since the caller has already deduped against the master before handing rows here; failed/`no_events` rows never appear in the digest. There is deliberately **no source-URL column** in either output: a row's only URL is `signup_link`, and when extraction finds none, `process_site` writes the site's base URL suffixed with `" *"` (the asterisk tells the human reader to locate the exact link from that base calendar page); failure/`no_events` rows put the site URL in the *title* cell so they stay identifiable. Rows are deduped on `(title, date)` (`event_key`/`_existing_event_keys`) — no source URL in the key, so the same event cross-posted on two sites collapses to one row — with one shared key set across the master's Events and Rejected Events sheets so an event can't end up duplicated between them or flip sheets across runs. Dedupe also runs *before scoring* (see **filter** above): `main()` loads the master's existing keys via `read_existing_event_keys`, and `process_site` drops already-known events before any Haiku call — write-time dedupe inside `append_rows` stays as the backstop. `_validate_header` fails loudly if an existing file's header row doesn't match `OUTPUT_COLUMNS` — appending to a stale header would silently misalign every column after the divergence point.
- **email** (`utility/email_digest.py`): `send_weekly_digest` composes and sends the digest email via Gmail SMTP_SSL (`smtp.gmail.com:465`), authenticated with a Gmail "app password" (env vars `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_RECIPIENT_EMAIL`, `DIGEST_RECIPIENT_NAME` — all read at send time inside `send_weekly_digest`/`_build_message`'s caller, never at import time, so importing the module never requires credentials). The body template (`_build_message`) is the agreed-upon wording — edit it directly to change what the recipient sees: a greeting, "Attached is a spreadsheet of all the new events found this week." (or, on an empty week, "No new events were found this week." with no attachment — the email is still sent so the recipient can tell "nothing new" apart from "the run never happened"), a "Websites scraped:" list with one status line per site ("N new event(s)" / "no new events" / "FAILED: <reason>"), and a closing line inviting the recipient to reply with sites to add or remove for next week. `dry_run=True` (`--no-email`) never touches the network or requires any env var to be set — it prints the composed subject/body/attachment name to stdout instead. On a real send failure, `main()` prints where the digest file is saved on disk and exits 1 — the dated file is the resend artifact; the user fixes the credentials and resends manually rather than re-running the pipeline (which would otherwise re-score nothing new, since the master was already updated before the send was attempted).

**Concurrency**: sites are processed in parallel (`ThreadPoolExecutor`, `MAX_WORKERS` in `cli/batch.py`, default 4). Firecrawl scrape calls specifically are capped separately and lower (`MAX_CONCURRENT_SCRAPES` in `scrape/fetch.py`, currently 2, matching the Firecrawl account's plan limit) via a semaphore, so raising `MAX_WORKERS` only ever increases extract/score LLM concurrency, never risks exceeding Firecrawl's concurrent-job limit. Within one site, events are scored concurrently too (`MAX_SCORING_WORKERS`, default 8), independent of the outer pool.

Token usage across the whole run is tracked (`utility/token_usage.py`, a single shared thread-safe `TokenUsageTracker`) and logged to `token_usage_history.json` under the single mode `"normal"` — the only run shape that exists now that the old alternate CLI modes are gone. `check_and_record_usage` prints an alert if a run's total tokens grew more than 30% versus the previous `"normal"` run.

`SITE_URLS` (top of `cli/batch.py`) is the production site list scraped every weekly run — edit it directly when asked for sites to be added or removed (commented-out entries are intentionally toggled off by hand — leave them as-is).

## Rebuilding the master workbook from a known-good digest

Local test runs and `workflow_dispatch` test triggers of the Action can leave the master out of sync with reality in two different ways: it can contain events that were scored but never actually emailed (the pipeline then wrongly treats them as already-seen and silently omits them if they come up again), or it can be missing events that genuinely were emailed (the pipeline then wrongly re-sends them as new). Remember there are *two* copies of the master that can each drift independently — the local `events_master.xlsx` and the one committed on the **`state`** branch, which is what the scheduled/dispatched Action actually restores and dedupes against (see README.md's "Automated weekly run"). Fixing only the local file has no effect on the Action.

If you have an authoritative artifact — a previously emailed digest `.xlsx`, which already has the same `Events`/`Rejected Events` sheet shape — rebuild the master from it directly rather than trying to hand-edit dedupe state:

1. Back up, don't delete: `cp events_master.xlsx events_master.xlsx.bak-$(date +%Y-%m-%d)`, then `rm events_master.xlsx` so the next step starts fresh.
2. Map the digest's rows into dicts keyed by the current `OUTPUT_COLUMNS` (`utility/io_excel.py`). If the file predates a schema change — e.g. an old `"URL"` column from before `signup_link` existed — fold it in as `f"{url} *"`, reusing the pipeline's own "no direct link found, here's the base page" convention rather than inventing a new one.
3. Call `utility.io_excel.append_rows(MASTER_PATH, rows)` directly with the mapped rows instead of writing the workbook by hand — it reuses the real table styling/dedupe logic and will naturally collapse any duplicate `(title, date)` keys already present in the source file (e.g. the same event cross-posted on two of the scraped sites).
4. Push the rebuilt file to `state` too, via a disposable worktree so `main` is undisturbed: `git worktree add /tmp/state-push -B state origin/state`, copy the rebuilt `events_master.xlsx` in, commit, `git push origin state`, then `git worktree remove /tmp/state-push`.
5. Decide deliberately whether to **replace** `state`'s copy or **merge** with it — don't default to merging. Events scored by a run that never completed a real email send (e.g. a `workflow_dispatch` test) were never actually reported to the recipient and should be dropped, even though the pipeline itself considers them already-seen.
