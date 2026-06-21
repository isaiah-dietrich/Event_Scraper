# AI Event Scraping

Prototype pipeline that scrapes JS-rendered event pages with Playwright,
extracts structured event data with Claude, and outputs results to Excel.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=your_key_here
```

## Usage

Single-site prototype (extracts and scores events for one hardcoded URL):

```bash
python scrape_events.py
python scrape_events.py --debug   # print reduced page text and exit, no LLM calls
```

Batch mode (reads `websites.xlsx`, appends results to `events_output.xlsx`):

```bash
python batch_scrape.py
```

Regenerate the input/output spreadsheet templates:

```bash
python build_spreadsheets.py
```

## Notes

- `ANTHROPIC_API_KEY` must be set as an environment variable. Never commit
  keys to this repo.
- Some sites use bot-protection (e.g. Cloudflare) that blocks headless
  browsers; `fetch_rendered_html` runs Chromium non-headless with a
  realistic user agent to work around this for now.
- Event fit scoring (`score_event` in `scrape_events.py`) is not yet wired
  into the batch pipeline.
