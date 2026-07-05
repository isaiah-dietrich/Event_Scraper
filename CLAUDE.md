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

### Tests
```
pip install -r requirements-dev.txt
pytest                                     # whole suite
pytest tests/test_score.py                 # one file
pytest tests/test_score.py -k confidence   # one test by name/keyword
```
The suite never hits the network or a real browser: Anthropic calls are stubbed via `FakeAnthropicClient` (`tests/conftest.py`), Playwright is stubbed per-test in `test_fetch.py`.

## Architecture

Pipeline per site URL, orchestrated by `cli/batch.py:process_site`:

**fetch** (`scrape/fetch.py`) → **reduce** (`scrape/reduce.py`) → **extract** (`scrape/extract.py`, Claude Sonnet) → **filter** (`cli/batch.py`, no AI call) → **score** (`scrape/score.py`, Claude Haiku) → **write** (`utility/io_excel.py`)

Details worth knowing before touching any one stage:

- **fetch**: Runs Chromium *non-headless* (`headless=False`) via Playwright because some sites (e.g. behind Cloudflare) detect and block headless automation outright. Beyond a single page load it does four things, each independently tunable: (1) polls `page.content()` until it stops changing instead of a fixed sleep (`_wait_for_content_to_settle`); (2) repeatedly scrolls and captures *every* intermediate snapshot, not just the final one, since some calendars (e.g. Luma) virtualize the list and unmount earlier events as later ones load (`_scroll_and_collect_snapshots`) — stops once nothing new loads, once a step's content is entirely more than `_MAX_FUTURE_DAYS` (~60 days) out, or after `_MAX_SCROLL_ATTEMPTS`; (3) clicks a FullCalendar-style (fullcalendar.io) "next month" arrow up to `_MAX_MONTH_CLICKS` times, capturing a snapshot per month, for calendars that render one month at a time with no URL/scroll position to key off of — detected via FullCalendar's own `.fc-next-button`/`.fc-toolbar-title` classes (`_click_next_month_and_collect_snapshots`), skipped entirely if absent, and subject to the same `_MAX_FUTURE_DAYS` cutoff; (4) follows numbered `?page=N` pagination up to `_MAX_ADDITIONAL_PAGES` (`_page_urls_to_follow`) — only that one convention is detected, other pagination styles (Load More buttons, cursor URLs) need a URL added manually. All pages'/snapshots' HTML is concatenated into one string before reduce.
- **reduce**: Strips tags/scripts/styles down to visible text to cut token usage, but special-cases `<img alt="...">` and inlines it as text — event banner images often carry venue/city info that appears nowhere else on the page.
- **extract**: One Claude Sonnet call per site (`MODEL = "claude-sonnet-4-6"` in `scrape/extract.py`), given the reduced page text and today's date, returns a JSON array of events with the fields in `EXTRACTION_FIELDS`. Raises `ValueError` on a truncated (`max_tokens`) or malformed-JSON response; `process_site` catches that and turns it into a `status: failed` row rather than aborting the whole batch.
- **filter**: Two AI-free filters run before scoring, in `cli/batch.py`, so a scoring call is never wasted on an event that can't qualify: `_filter_past_events` drops events dated before today (unparseable dates are kept, with a logged warning, rather than silently dropped) and replaces a parsed date string with a real `datetime.datetime` so Excel can sort on it and dedupe can compare it across runs; `_split_by_state` auto-rejects events whose location confidently names a non-Georgia US state or a foreign country (`_extract_us_state` / `_extract_foreign_country`, both string-matched against the trailing comma-separated segment). Ambiguous locations ("Virtual", "Zoom", a bare city, no location at all) fall through to normal scoring rather than being guessed at.
- **score**: One Claude Haiku call per surviving event (cheaper model — it's just a classification task). `SCORING_CRITERIA` in `scrape/score.py` *is* the fit rubric, written as plain English instructions to the model — edit it directly to retune what counts as a good fit (e.g. AI-relevance is judged from the event's own content, not the hosting org; a blank location is not assumed to be virtual).
- **write** (`utility/io_excel.py`): Appends into an **Events** sheet and a **Rejected Events** sheet (rejected = scored a confident 1/5, or auto-rejected by the state/country filter) inside a self-expanding Excel Table per sheet. Rows are deduped across runs on `(source_url, title, date)` (`_event_key`/`_existing_event_keys`), with one shared key set across both sheets so an event can't end up duplicated between them or flip sheets across runs. `_validate_header` fails loudly if an existing output file's header row doesn't match `OUTPUT_COLUMNS` — appending to a stale header would silently misalign every column after the divergence point.

**Concurrency**: sites are processed in parallel (`ThreadPoolExecutor`, `MAX_WORKERS` in `cli/batch.py`, default 4) — each worker pops its own visible Chromium window, so don't raise this too high locally. Within one site, events are scored concurrently too (`MAX_SCORING_WORKERS`, default 8), independent of the outer pool.

**Two output modes**, selected by CLI flags in `cli/batch.py:main`:
- Normal mode (`append_rows`): the permanent, deduped Events/Rejected Events workbook.
- `--per-site` (`write_per_site_sheets`): diagnostic-only, one sheet per URL with every row type (ok / rejected / failed / no_events) together and unsplit, always rebuilt from scratch — for eyeballing extraction/scoring accuracy site-by-site. Sheet and table names are sanitized/deduped from the URL (`_unique_sheet_title`, `_unique_table_name`) to satisfy Excel's naming rules.

`TEST_URLS` (top of `cli/batch.py`) is the scratch list used by `--test` — edit it directly to try new sites without touching `websites.xlsx` or `events_output.xlsx`.
