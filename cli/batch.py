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
TEST_OUTPUT_PATH = "events_output_test.xlsx"

# Paste site URLs here to test the pipeline without touching websites.xlsx.
# Run with: python run.py --test
TEST_URLS = [
    "https://ai.gatech.edu/events",
    "https://members.tagonline.org/calendar",
    "https://www.georgiamanufacturingalliance.com/events/",
    "https://www.aimanufacturingconference.com/",
]

# Each site gets its own browser instance and its own LLM call, run
# concurrently. Each worker pops a visible Chromium window (headless=False,
# see ai_event_scraper.fetch), so don't set this too high locally.
MAX_WORKERS = 4

# Within a single site, events are scored concurrently too (one Haiku call
# per event). This is independent of MAX_WORKERS above.
MAX_SCORING_WORKERS = 8


_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


def _filter_past_events(events: list[dict]) -> list[dict]:
    today = datetime.date.today()
    filtered = []
    for event in events:
        date_str = event.get("date", "").strip()
        if not date_str:
            filtered.append(event)
            continue
        for fmt in _DATE_FORMATS:
            try:
                event_date = datetime.datetime.strptime(date_str, fmt).date()
                event["date"] = event_date.strftime("%B %-d, %Y")
                if event_date >= today:
                    filtered.append(event)
                break
            except ValueError:
                continue
        else:
            filtered.append(event)
    return filtered


def _timestamp() -> str:
    """Returns today's local date as "<Month> <Day>, <Year>", e.g. "June 21, 2026"."""
    today = datetime.date.today()
    return f"{today:%B} {today.day}, {today:%Y}"


def process_site(client: Anthropic, url: str, emit=None) -> list[dict]:
    """Runs fetch, reduce, and extract for one site.

    Args:
        client: An initialized Anthropic client.
        url: The site URL to scrape.

    Returns:
        A list of output row dicts: one per extracted event on success, or
        a single row describing the failure/empty-result status otherwise.
    """
    def _emit(event_type, **data):
        if emit:
            emit(event_type, url=url, **data)

    base_row = {"scraped_at": _timestamp(), "source_url": url}

    _emit("step_start", step="fetch")
    try:
        html = fetch_rendered_html(url)
    except RuntimeError as error:
        _emit("step_failed", step="fetch", error=str(error))
        return [{**base_row, "status": f"failed: {error}"}]
    _emit("step_done", step="fetch")

    _emit("step_start", step="reduce")
    page_text = reduce_html(html)
    if not page_text:
        _emit("step_failed", step="reduce", error="empty page text")
        return [{**base_row, "status": "failed: empty page text after reduction"}]
    _emit("step_done", step="reduce")

    _emit("step_start", step="extract")
    try:
        events = extract_events(client, page_text)
    except ValueError as error:
        _emit("step_failed", step="extract", error=str(error))
        return [{**base_row, "status": f"failed: {error}"}]
    events = _filter_past_events(events)
    if not events:
        _emit("step_done", step="extract", detail="0 events")
        return [{**base_row, "status": "no_events"}]
    _emit("step_done", step="extract", detail=f"{len(events)} events")

    _emit("step_start", step="score")
    with ThreadPoolExecutor(max_workers=MAX_SCORING_WORKERS) as executor:
        scorings = list(executor.map(lambda event: score_event(client, event), events))
    _emit("step_done", step="score")

    rows = []
    for event, scoring in zip(events, scorings):
        row = {**base_row, "status": "ok"}
        row.update({field: event.get(field, "") for field in EXTRACTION_FIELDS})
        row["fit_score"] = scoring["score"]
        row["fit_reason"] = scoring["reason"]
        rows.append(row)
        _emit("event_result",
              title=event.get("title", "Untitled"),
              score=scoring["score"],
              reason=scoring["reason"])
    return rows


def main() -> None:
    """Runs the batch pipeline over every URL in INPUT_PATH."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    client = Anthropic(api_key=api_key)

    test_mode = "--test" in sys.argv
    fresh_mode = "--fresh" in sys.argv
    viz_mode = "--viz" in sys.argv

    viz_emit = None
    if viz_mode:
        from viz.server import emit as viz_emit
    if test_mode:
        urls = TEST_URLS
        output_path = TEST_OUTPUT_PATH
        source = "TEST_URLS"
    else:
        try:
            urls = read_input_urls(INPUT_PATH)
        except FileNotFoundError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            sys.exit(1)
        output_path = OUTPUT_PATH
        source = INPUT_PATH

    if (fresh_mode or test_mode) and os.path.exists(output_path):
        os.remove(output_path)
        print(f"Removed existing {output_path}")

    if not urls:
        print(f"No URLs found in {source}. Nothing to do.")
        sys.exit(0)

    print(f"Processing {len(urls)} site(s) from {source} (up to {MAX_WORKERS} at a time)...")
    if viz_emit:
        for url in urls:
            viz_emit("site_queued", url=url)

    all_rows = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {executor.submit(process_site, client, url, viz_emit): url for url in urls}
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

    if viz_emit:
        from viz.server import render_and_open
        render_and_open()

    append_rows(output_path, all_rows)
    print(f"\nAppended {len(all_rows)} row(s) to {output_path}")


if __name__ == "__main__":
    main()
