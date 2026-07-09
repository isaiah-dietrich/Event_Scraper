# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scrapes a list of websites for AI/ML-related events, uses Claude to extract and score them against a fit rubric (in-person, Georgia-based, AI-focused), and writes the results into an Excel workbook.

## Commands

### Setup
```
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-...
```
`ANTHROPIC_API_KEY` must be set; `cli/batch.py:main` exits immediately with an error if it's missing.

### Run
```
python run.py                     # scrape websites.xlsx -> events_output.xlsx (append + dedupe)
python run.py --test              # scrape TEST_URLS (cli/batch.py) -> events_output_test.xlsx (always fresh)
python run.py --fresh             # wipe output before writing instead of append/dedupe
python run.py --per-site          # diagnostic: one sheet per URL -> events_output_by_site.xlsx
python -m cli.build_spreadsheets  # scaffold a fresh websites.xlsx + empty events_output.xlsx template
```
Flags combine freely (`--test` picks *which URLs*, `--per-site` picks *the output format* — independent axes). See README.md's combination table for the exact input/output pairing of each combo.

No automated test suite is maintained on `main` (it lives on the `tests` branch instead) — verify changes by running the pipeline directly (e.g. `python run.py --test` or `--per-site`).

## Architecture

Pipeline per site URL, orchestrated by `cli/batch.py:process_site`:

**fetch** (`scrape/fetch.py`) → **reduce** (`scrape/reduce.py`) → **extract** (`scrape/extract.py`, Claude Sonnet) → **filter** (`cli/batch.py`, no AI call) → **score** (`scrape/score.py`, Claude Haiku) → **write** (`utility/io_excel.py`)

Details worth knowing before touching any one stage:

- **fetch**: Runs Chromium *non-headless* (`headless=False`) via Playwright because some sites (e.g. behind Cloudflare) detect and block headless automation outright. Beyond a single page load it does several things, each independently tunable: (1) polls `page.content()` until it stops changing instead of a fixed sleep (`_wait_for_content_to_settle`); (2) captures every non-trivial child `<iframe>`'s content (`_capture_child_frames`) — embedded calendars (Google Calendar, Localist, GrowthZone/ChamberMaster) live in iframes that `page.content()` alone never sees; (3) scrolls one viewport at a time (not bottom-jumps, which can skip virtualized/lazy-mounted content) capturing a snapshot per step that changed the DOM (`_scroll_and_collect_snapshots`) — stops at the bottom once the page stops growing, once a step's content is entirely more than `_MAX_FUTURE_DAYS` (~60 days) out, or after `_MAX_SCROLL_ATTEMPTS` viewport steps; (4) clicks conservative text-matched "Load more"/"Show more events" controls up to `_MAX_LOAD_MORE_CLICKS` (`_click_load_more_and_collect_snapshots`); (5) clicks "next month" for a small allowlist of calendar widgets — FullCalendar, WordPress The Events Calendar, generic `aria-label*="next month"` (`_NEXT_MONTH_SELECTORS`) — stopping when a click leaves the settled HTML unchanged; (6) follows numbered `?page=N` pagination up to `_MAX_ADDITIONAL_PAGES` (`_page_urls_to_follow`), stopping early when a page near-duplicates one already fetched (`_pages_are_near_duplicate`). Each URL gets one retry after a transient failure (`_load_and_capture_with_retry`). Before joining, snapshots subsumed by already-kept ones are pruned newest-first (`_prune_subsumed_snapshots`) so a grow-only page isn't sent to the LLM once per scroll step — whole snapshots only, never line-level dedupe (a line like "6:00 PM" legitimately recurs across different events).
- **reduce**: Strips tags/scripts/styles down to visible text to cut token usage, with two carve-outs: `<img alt="...">` is inlined as text (event banners often carry venue/city info appearing nowhere else), and every `<a href="...">` is inlined as `Label (absolute URL)` — resolving relative hrefs against `base_url`, skipping `#`/`javascript:`/`mailto:`/`tel:` — since signup links live only in hrefs, which is what `signup_link` extraction depends on. HTML entities are decoded *after* tag-stripping (`&amp;` inside hrefs would otherwise write broken URLs to the spreadsheet; decoding before the strip would let literal `&lt;div&gt;` text get eaten as markup).
- **extract**: One Claude Sonnet call per site (`MODEL = "claude-sonnet-4-6"` in `scrape/extract.py`), given the reduced page text and today's date, returns a JSON array of events with the fields in `EXTRACTION_FIELDS`. Raises `ValueError` on a truncated (`max_tokens`) or malformed-JSON response; `process_site` catches that and turns it into a `status: failed` row rather than aborting the whole batch. Before extraction, `process_site` short-circuits bot-challenge pages (short reduced text containing a `_BOT_CHALLENGE_MARKERS` phrase like "just a moment") into a failed row with zero AI calls.
- **filter**: Two AI-free filters run before scoring, in `cli/batch.py`, so a scoring call is never wasted on an event that can't qualify: `_filter_past_events` drops events dated before today (unparseable dates are kept, with a logged warning, rather than silently dropped) and replaces a parsed date string with a real `datetime.datetime` so Excel can sort on it and dedupe can compare it across runs; `_split_by_state` auto-rejects events whose location confidently names a non-Georgia US state or a foreign country (`_extract_us_state` / `_extract_foreign_country`, both string-matched against the trailing comma-separated segment). Ambiguous locations ("Virtual", "Zoom", a bare city, no location at all) fall through to normal scoring rather than being guessed at.
- **score**: One Claude Haiku call per surviving event (cheaper model — it's just a classification task). `SCORING_CRITERIA` in `scrape/score.py` *is* the fit rubric, written as plain English instructions to the model — edit it directly to retune what counts as a good fit (e.g. AI-relevance is judged from the event's own content, not the hosting org; a blank location is not assumed to be virtual).
- **write** (`utility/io_excel.py`): Appends into an **Events** sheet and a **Rejected Events** sheet (rejected = scored a confident 1/5, or auto-rejected by the state/country filter) inside a self-expanding Excel Table per sheet. Rows are deduped across runs on `(source_url, title, date)` (`event_key`/`_existing_event_keys`), with one shared key set across both sheets so an event can't end up duplicated between them or flip sheets across runs. Dedupe also runs *before scoring*: in normal append mode, `main()` loads the workbook's existing keys (`read_existing_event_keys`) and `process_site` drops already-known events before any Haiku call — write-time dedupe stays as the backstop. (`--per-site` deliberately skips this: its point is fresh diagnostic scoring.) `_validate_header` fails loudly if an existing output file's header row doesn't match `OUTPUT_COLUMNS` — appending to a stale header would silently misalign every column after the divergence point.

**Concurrency**: sites are processed in parallel (`ThreadPoolExecutor`, `MAX_WORKERS` in `cli/batch.py`, default 4) — each worker pops its own visible Chromium window, so don't raise this too high locally. Within one site, events are scored concurrently too (`MAX_SCORING_WORKERS`, default 8), independent of the outer pool.

**Two output modes**, selected by CLI flags in `cli/batch.py:main`:
- Normal mode (`append_rows`): the permanent, deduped Events/Rejected Events workbook.
- `--per-site` (`write_per_site_sheets`): diagnostic-only, one sheet per URL with every row type (ok / rejected / failed / no_events) together and unsplit, always rebuilt from scratch — for eyeballing extraction/scoring accuracy site-by-site. Sheet and table names are sanitized/deduped from the URL (`_unique_sheet_title`, `_unique_table_name`) to satisfy Excel's naming rules.

`TEST_URLS` (top of `cli/batch.py`) is the scratch list used by `--test` — edit it directly to try new sites without touching `websites.xlsx` or `events_output.xlsx`.
