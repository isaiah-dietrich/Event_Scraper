# AI Event Scraper

Scrapes a list of websites for AI/ML-related events, uses Claude to extract
and score them against a fit rubric (in-person, Georgia-based, AI-focused),
and writes the results into an Excel workbook.

Input and output are one shared file, **`Georgia_Event_Tracker.xlsx`**: the
sites to scrape live in an editable **Websites** sheet (the client adds/removes
URLs there directly), and results are written back into the same workbook's
Events / Rejected Events / past_events sheets.

## Pipeline

Every append-mode run starts by archiving: any row in the workbook
whose event date has passed is moved (with all hand-added notes, highlights,
comments, and hyperlinks intact) from Events/Rejected Events into a
`past_events` sheet, so the live sheets only ever show current/upcoming
events.

Then, for each URL in the Websites sheet: **fetch** (render the page with Playwright/Chromium)
→ **reduce** (strip HTML down to visible text, inlining signup-link URLs) →
**extract** (Claude Sonnet pulls out structured events as JSON) → **filter**
(drop past-dated events; skip events already in the workbook; auto-reject
events whose location names a US state other than Georgia, without spending
an AI call on them) → **score** (Claude Haiku rates the remaining events 1-5
with a confidence level) → **write** (append to the output spreadsheet).

Relevant code: [scrape/fetch.py](scrape/fetch.py), [scrape/reduce.py](scrape/reduce.py),
[scrape/extract.py](scrape/extract.py), [scrape/score.py](scrape/score.py),
[cli/batch.py](cli/batch.py), [utility/io_excel.py](utility/io_excel.py).

## Setup

```
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-...
```

`ANTHROPIC_API_KEY` must be set in the environment; the run exits immediately
with an error if it's missing.

## Running

```
python run.py [flags]
```

### Flags

| Flag | Effect |
|---|---|
| *(none)* | Scrapes every URL in the Websites sheet of `Georgia_Event_Tracker.xlsx` and appends results back into the same file. |
| `--test` | Scrapes the hardcoded `TEST_URLS` list in [cli/batch.py](cli/batch.py) instead of the Websites sheet, writing to a throwaway `events_output_test.xlsx` instead. Lets you iterate without touching the real tracker file. Existing `events_output_test.xlsx` is deleted before the run (always fresh). |
| `--fresh` | Clears existing results (Events / Rejected Events / past_events) before writing, instead of appending/deduping onto whatever's already there. The editable Websites input sheet is preserved. Only meaningful without `--per-site` (see below); combined with `--test` it's redundant since `--test` already clears its output every run. |
| `--per-site` | **Testing only.** Instead of the normal combined Events/Rejected Events workbook, writes one sheet per URL to a separate file, `events_output_by_site.xlsx`, with every row for that site (good, rejected, failed, no_events) together and unsplit — useful for eyeballing extraction/scoring accuracy site-by-site. Always overwrites `events_output_by_site.xlsx` from scratch and never touches `events_output.xlsx`/`events_output_test.xlsx`, so `--fresh` has no effect when combined with it. |

Flags combine freely. `--test` and `--per-site` are independent axes:
`--test` picks *which URLs* get scraped, `--per-site` picks *the output
format*.

### Combinations

| Command | URLs scraped | Output |
|---|---|---|
| `python run.py` | Websites sheet | `Georgia_Event_Tracker.xlsx` (Events / Rejected Events sheets, appended + deduped) |
| `python run.py --fresh` | Websites sheet | `Georgia_Event_Tracker.xlsx`, result sheets cleared and rebuilt (Websites sheet preserved) |
| `python run.py --test` | `TEST_URLS` | `events_output_test.xlsx`, wiped and rebuilt every run |
| `python run.py --per-site` | Websites sheet | `events_output_by_site.xlsx` (one sheet per URL) |
| `python run.py --test --per-site` | `TEST_URLS` | `events_output_by_site.xlsx` (one sheet per URL) |
| `python run.py --fresh --per-site` | Websites sheet | `events_output_by_site.xlsx` (`--fresh` is a no-op here) |
| `python run.py --test --fresh --per-site` | `TEST_URLS` | `events_output_by_site.xlsx` (`--fresh` is a no-op here) |

## Other entry points

- `python -m cli.build_spreadsheets` — scaffolds a fresh
  `Georgia_Event_Tracker.xlsx`: an editable **Websites** sheet (seeded with a
  couple of starter URLs) plus empty **Events**/**Rejected Events** output
  sheets. See [cli/build_spreadsheets.py](cli/build_spreadsheets.py).

## The `Georgia_Event_Tracker.xlsx` workbook

Input and output live in this one shared file (the client edits it too):

- **Websites** (last tab, tinted green) — the editable input. A single-column
  Excel Table of site URLs; add or remove rows to change what gets tracked.
  Picked up on the next run. Never touched by a run, including `--fresh`.
- **Events** — the good matches, each with a fit score, confidence, date,
  location, description, fit reason, and a **Signup Link**. There is no
  source-URL column: when no signup link could be extracted, the cell holds
  the site's base URL with a trailing `*` — meaning "open this base calendar
  page and locate the event's own link from there." Failed/no-event rows show
  the site URL in the Event Title cell instead.
- **Rejected Events** — events the model scored a confident 1/5, or that the
  location pre-filter caught as a non-Georgia state before scoring.
- **past_events** — accumulates rows automatically archived once their event
  date passes; hand-added notes, highlights, comments, and hyperlinks travel
  with the row.

Rows are deduped across runs on `(title, date)`, so the same event
cross-posted on two sites collapses to one row.

`events_output_by_site.xlsx` — separate diagnostic-only output from
`--per-site`, one sheet per URL, always rebuilt from scratch.
