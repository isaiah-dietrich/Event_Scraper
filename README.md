# AI Event Scraper

Scrapes a list of websites for AI/ML-related events, uses Claude to extract
and score them against a fit rubric (in-person, Georgia-based, AI-focused),
and emails the client a spreadsheet of just that week's new events. It runs
automatically every Monday via a scheduled GitHub Action
([.github/workflows/weekly-digest.yml](.github/workflows/weekly-digest.yml))
— there's no shared client-facing workbook; the client's only artifact is
the weekly email and its attachment. `python run.py` still works as a manual
local run too (see "Automated weekly run" below for how the two stay in
sync).

## Setup

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
export FIRECRAWL_API_KEY=fc-...
```

`ANTHROPIC_API_KEY` and `FIRECRAWL_API_KEY` must both be set in the
environment; the run exits immediately with an error if either is missing.

### Email setup

Actually sending the weekly digest email requires four more environment
variables:

```
export GMAIL_ADDRESS=you@gmail.com
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export DIGEST_RECIPIENT_EMAIL=client@example.com
export DIGEST_RECIPIENT_NAME="Client Name"
```

`GMAIL_APP_PASSWORD` is a Gmail **app password** — not your normal Google
account password. It requires 2-Step Verification to be enabled on the
sending Google account first; generate one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
None of these four are required to run with `--no-email`.

## Running

```
python run.py             # weekly run: scrape, dedupe, write the digest, email it to the client
python run.py --no-email  # same run, but prints the composed email to stdout instead of sending it
```

`--no-email` is the only flag. Each run:

1. Scrapes every URL in `SITE_URLS` (see below) through fetch → reduce →
   extract → filter → score.
2. Dedupes each site's events against `events_master.xlsx` (created
   automatically on first run) so only events not seen in a prior run count
   as "new."
3. If any new events were found, writes them to
   `weekly_digests/new_events_<YYYY-MM-DD>.xlsx` — a "New Events" sheet and
   a "Rejected Events" sheet.
4. Appends everything from this run — new events, rejects, and any
   failed/no-event site results — into `events_master.xlsx`, *before*
   attempting to send the email, so a failed send can never corrupt what's
   considered "already seen."
5. Emails the digest to the client via Gmail.

### What appears on disk

- **`events_master.xlsx`** — internal, append-only "seen events" store plus
  history. Gitignored on `main`. Never sent to or edited by the client or
  anyone else — it exists purely so the pipeline can tell what's already
  been reported. Persisted across scheduled runs on the separate `state`
  branch (see "Automated weekly run" below) rather than committed to `main`.
- **`weekly_digests/new_events_<date>.xlsx`** — one dated file per run,
  gitignored, only written when there's at least one new event that week.
  This is also the resend artifact if the email fails to send (see
  Failure handling below).
- **`token_usage_history.json`** — per-run token usage log, used to flag a
  run that used unexpectedly more tokens than the last one. Also persisted
  on the `state` branch.

## Automated weekly run

A GitHub Action ([.github/workflows/weekly-digest.yml](.github/workflows/weekly-digest.yml))
runs the pipeline automatically every Monday at 9am CST (`cron: "0 15 * * 1"`
— GitHub Actions cron is fixed-UTC and doesn't shift for daylight saving, so
this fires at 8am local during CDT), and can also be triggered on demand
from the Actions tab (`workflow_dispatch`).

Because `events_master.xlsx` and `token_usage_history.json` are gitignored
local state, and every Action run starts from a fresh checkout, they're
persisted on a separate, orphaned **`state`** branch instead of `main` — the
workflow restores them into place before running and commits the updated
versions back after, win or lose (`if: always()`), so a failed email send
still gets that run's newly-seen events recorded. This keeps `main`'s
history free of weekly binary-file churn, the same way this repo already
keeps its test suite on a separate `tests` branch rather than `main`. The
weekly digest `.xlsx` is also uploaded as a workflow run artifact, so a
failed send is still recoverable the same way a local run's saved file is
(see Failure handling below).

**Required repo configuration** (Settings on GitHub, one-time):

1. Settings → Actions → General → Workflow permissions → **"Read and write
   permissions"** (the workflow pushes the updated `state` branch using the
   default `GITHUB_TOKEN`).
2. Settings → Secrets and variables → Actions → add repository secrets for
   `ANTHROPIC_API_KEY`, `FIRECRAWL_API_KEY`, `GMAIL_ADDRESS`,
   `GMAIL_APP_PASSWORD`, `DIGEST_RECIPIENT_EMAIL`, `DIGEST_RECIPIENT_NAME`
   (same values as the local env vars above).

**Running `python run.py` locally too:** still works, but the Action is now
the source of truth for `events_master.xlsx`. Pull the latest state before a
local run and push it back after, so the Action and a local run never
diverge on what's already been reported:

```
git fetch origin state
git show origin/state:events_master.xlsx > events_master.xlsx
git show origin/state:token_usage_history.json > token_usage_history.json
python run.py
# then commit the updated files back to the state branch, e.g. via a
# worktree checked out to `state`, before the next scheduled Action run
```

### What the client receives

An email with subject "Georgia AI Events – New Events for `<date>`" and a
body containing:

- A greeting.
- "Attached is a spreadsheet of all the new events found this week." — or,
  on a week with no new events, "No new events were found this week." with
  no attachment. The email is still sent either way, so the client can tell
  "nothing new this week" apart from "the run never happened."
- A "Websites scraped:" list, one line per site: `N new event(s)`,
  `no new events`, or `FAILED: <reason>`.
- A closing line inviting the client to reply with any sites they'd like
  added or removed for next week.

When there are new events, the attached workbook has "New Events" and
"Rejected Events" sheets, each with the same 11 columns as always: Event
Title, Fit Score, Confidence, Event Date, Date Scraped, Start Time,
Location, Signup Link, Status, Description, Fit Reason. As before, a
`signup_link` of the site's base URL followed by `" *"` means extraction
couldn't find the event's own link — the asterisk tells the reader to go
locate it from that base calendar page themselves.

## Editing the site list

The list of sites scraped every week is `SITE_URLS`, hardcoded at the top of
[cli/batch.py](cli/batch.py) — it's the only site list; there's no input
spreadsheet or config file. When the client replies to the weekly email
asking for a site to be added or removed, edit this list directly (comment
out a URL to disable it without losing it, per the client's request history).

## Failure handling

- A single site failing (blocked by bot protection, a truncated or malformed
  extraction response, a network error, etc.) doesn't stop the run — that
  site just gets a `FAILED: <reason>` line in the email body, and every
  other site's results still go out normally.
- If sending the email itself fails (e.g. a missing or wrong
  `GMAIL_APP_PASSWORD`), `run.py` prints where the digest file was saved on
  disk and exits with a non-zero status. The master workbook has already
  been updated by that point, so simply re-running the pipeline would treat
  that week's events as already-seen and produce an empty digest next time.
  Instead, fix the credentials and resend the saved `.xlsx` file from
  `weekly_digests/` manually (locally), or download the `weekly-digest`
  workflow artifact from the failed Action run (via GitHub Actions).
- A failed scheduled Action run itself is surfaced by GitHub's own
  notification to the repo owner on workflow failure — no extra code needed
  for that. The `state` branch still gets updated in this case (see
  "Automated weekly run" above), so a re-run afterward won't re-report
  events the failed run already recorded.

## Pipeline detail

fetch ([scrape/fetch.py](scrape/fetch.py), Firecrawl) → reduce
([scrape/reduce.py](scrape/reduce.py)) → extract
([scrape/extract.py](scrape/extract.py), Claude Sonnet) → filter
([cli/batch.py](cli/batch.py), no AI call — drops past-dated events, dedupes
against the master, auto-rejects non-Georgia locations) → score
([scrape/score.py](scrape/score.py), Claude Haiku) → write + email
([utility/io_excel.py](utility/io_excel.py),
[utility/email_digest.py](utility/email_digest.py)).

See [CLAUDE.md](CLAUDE.md) for the deep per-stage architecture notes
(fetch's Firecrawl/pagination handling, reduce's block-collapse dedupe,
dedupe key details, etc.).
