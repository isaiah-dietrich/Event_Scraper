"""Single-site CLI: fetch, reduce, extract, and score events for one URL.

Usage:
    python -m cli.single_site            # run the full pipeline
    python -m cli.single_site --debug    # print reduced page text, then exit
"""

import json
import os
import sys

from anthropic import Anthropic

from scrape.extract import extract_events
from scrape.fetch import fetch_rendered_html
from scrape.reduce import reduce_html
from scrape.score import score_event

TARGET_URL = "https://members.tagonline.org/calendar"
OUTPUT_PATH = "events.json"


def main() -> None:
    """Runs the single-site pipeline and writes results to OUTPUT_PATH."""
    debug = "--debug" in sys.argv
    client = None

    if not debug:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
            sys.exit(1)
        client = Anthropic(api_key=api_key)

    print(f"Fetching rendered page: {TARGET_URL}")
    try:
        html = fetch_rendered_html(TARGET_URL)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    print("Reducing HTML to visible text...")
    page_text = reduce_html(html)

    if debug:
        print("\n=== DEBUG: reduced page text sent to the LLM ===\n")
        print(page_text)
        print(f"\n=== DEBUG: {len(page_text)} characters total ===")
        sys.exit(0)

    if not page_text:
        print("ERROR: Reduced page text is empty; nothing to extract.", file=sys.stderr)
        sys.exit(1)

    print("Extracting events with Claude...")
    try:
        events = extract_events(client, page_text)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    if not events:
        print("No events were extracted from the page. Exiting.")
        sys.exit(0)

    print(f"Extracted {len(events)} event(s). Scoring each for fit...")
    results = []
    for event in events:
        scoring = score_event(client, event)
        result = dict(event)
        result["fit_score"] = scoring["score"]
        result["fit_reason"] = scoring["reason"]
        results.append(result)

    results.sort(key=lambda result: result.get("fit_score", 0), reverse=True)

    with open(OUTPUT_PATH, "w") as output_file:
        json.dump(results, output_file, indent=2)

    print("\n=== Event Fit Summary (highest first) ===\n")
    for result in results:
        print(f"[{result.get('fit_score', '?')}/5] {result.get('title', 'Untitled')}")
        print(f"    Date: {result.get('date', '?')}  Time: {result.get('start_time', '?')}")
        print(
            f"    Location: {result.get('location', '?')}  "
            f"In-person: {result.get('is_in_person', '?')}"
        )
        print(f"    Signup: {result.get('signup_link', '')}")
        print(f"    Reason: {result.get('fit_reason', '')}")
        print()

    print(f"Wrote {len(results)} event(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
