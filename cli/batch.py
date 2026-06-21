"""Batch CLI: scrape every site in an input spreadsheet and append results.

Reads site URLs from INPUT_PATH, runs the fetch/reduce/extract/score
pipeline against each one concurrently, and appends the results to
OUTPUT_PATH.
"""

import datetime
import os
import sys
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic

from scrape.extract import EXTRACTION_FIELDS
from scrape.extract import extract_events
from scrape.fetch import fetch_rendered_html
from scrape.reduce import reduce_html
from scrape.score import score_event
from utility.io_excel import append_rows
from utility.io_excel import read_input_urls

INPUT_PATH = "websites.xlsx"
OUTPUT_PATH = "events_output.xlsx"

# Each site gets its own browser instance and its own LLM call, run
# concurrently. Each worker pops a visible Chromium window (headless=False,
# see ai_event_scraper.fetch), so don't set this too high locally.
MAX_WORKERS = 4

# Within a single site, events are scored concurrently too (one Haiku call
# per event). This is independent of MAX_WORKERS above.
MAX_SCORING_WORKERS = 8


def _timestamp() -> str:
    """Returns today's local date as "<Month> <Day>, <Year>", e.g. "June 21, 2026"."""
    today = datetime.date.today()
    return f"{today:%B} {today.day}, {today:%Y}"


def process_site(client: Anthropic, url: str) -> list[dict]:
    """Runs fetch, reduce, and extract for one site.

    Args:
        client: An initialized Anthropic client.
        url: The site URL to scrape.

    Returns:
        A list of output row dicts: one per extracted event on success, or
        a single row describing the failure/empty-result status otherwise.
    """
    base_row = {"scraped_at": _timestamp(), "source_url": url}

    try:
        html = fetch_rendered_html(url)
    except RuntimeError as error:
        return [{**base_row, "status": f"failed: {error}"}]

    page_text = reduce_html(html)
    if not page_text:
        return [{**base_row, "status": "failed: empty page text after reduction"}]

    try:
        events = extract_events(client, page_text)
    except ValueError as error:
        return [{**base_row, "status": f"failed: {error}"}]

    if not events:
        return [{**base_row, "status": "no_events"}]

    with ThreadPoolExecutor(max_workers=MAX_SCORING_WORKERS) as executor:
        scorings = list(executor.map(lambda event: score_event(client, event), events))

    rows = []
    for event, scoring in zip(events, scorings):
        row = {**base_row, "status": "ok"}
        row.update({field: event.get(field, "") for field in EXTRACTION_FIELDS})
        row["fit_score"] = scoring["score"]
        row["fit_reason"] = scoring["reason"]
        rows.append(row)
    return rows


def main() -> None:
    """Runs the batch pipeline over every URL in INPUT_PATH."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    client = Anthropic(api_key=api_key)

    try:
        urls = read_input_urls(INPUT_PATH)
    except FileNotFoundError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    if not urls:
        print(f"No URLs found in {INPUT_PATH}. Nothing to do.")
        sys.exit(0)

    print(f"Processing {len(urls)} site(s) from {INPUT_PATH} (up to {MAX_WORKERS} at a time)...")
    all_rows = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {executor.submit(process_site, client, url): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                rows = future.result()
            except Exception as error:  # pylint: disable=broad-except
                rows = [{
                    "scraped_at": _timestamp(),
                    "source_url": url,
                    "status": f"failed: unexpected error: {error}",
                }]
            print(f"[done] {url}")
            for row in rows:
                suffix = f": {row.get('title')}" if row["status"] == "ok" else ""
                print(f"    -> {row['status']}{suffix}")
            all_rows.extend(rows)

    append_rows(OUTPUT_PATH, all_rows)
    print(f"\nAppended {len(all_rows)} row(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
