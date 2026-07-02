"""Batch CLI: scrape every site in an input spreadsheet and append results.

Reads site URLs from INPUT_PATH, runs the fetch/reduce/extract/score
pipeline against each one concurrently, and appends the results to
OUTPUT_PATH.
"""

import datetime
import os
import re
import sys
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor

import dateparser
from anthropic import Anthropic

from scrape.extract import EXTRACTION_FIELDS
from scrape.extract import extract_events
from scrape.fetch import fetch_rendered_html
from scrape.reduce import reduce_html
from scrape.score import score_event
from utility.io_excel import append_rows
from utility.io_excel import read_input_urls
from utility.io_excel import write_per_site_sheets

INPUT_PATH = "websites.xlsx"
OUTPUT_PATH = "events_output.xlsx"
TEST_OUTPUT_PATH = "events_output_test.xlsx"

# Diagnostic-only output for --per-site (see main()): one sheet per URL, so a
# run's accuracy can be checked site-by-site. Not the permanent output format
# - that's still the single Events/Rejected Events workbook via append_rows.
PER_SITE_OUTPUT_PATH = "events_output_by_site.xlsx"

# Paste site URLs here to test the pipeline without touching websites.xlsx.
# Run with: python run.py --test
TEST_URLS = [
    #"https://ai.gatech.edu/events",
    #"https://members.tagonline.org/calendar",
    #"https://www.georgiamanufacturingalliance.com/events/",
    #"https://www.aimanufacturingconference.com/",
    #"https://www.meetup.com/find/?source=EVENTS&categoryId=546&location=us--georgia",
    #"https://luma.com/genai-collective",
    #"https://gec1.wildapricot.org/events",
    #"https://www.eventbrite.com/d/united-states--georgia/science-and-tech--events/?page=1",
    "https://atlanta.aitinkerers.org/"

]

# Each site gets its own browser instance and its own LLM call, run
# concurrently. Each worker pops a visible Chromium window (headless=False,
# see ai_event_scraper.fetch), so don't set this too high locally.
MAX_WORKERS = 4

# Within a single site, events are scored concurrently too (one Haiku call
# per event). This is independent of MAX_WORKERS above.
MAX_SCORING_WORKERS = 8


_DATE_PARSER_SETTINGS = {"PREFER_DATES_FROM": "future"}

# Events whose location clearly names a US state other than this one are
# auto-rejected before scoring (see _extract_us_state / _split_by_state), so
# they never trigger a scoring AI call.
TARGET_STATE = "GA"

_STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_NAME_TO_ABBR = {name.lower(): abbr for abbr, name in _STATE_ABBREVIATIONS.items()}

# Trailing location segments that carry no state info and should be skipped
# when scanning from the right (e.g. "Atlanta, GA, USA").
_NOISE_LOCATION_SEGMENTS = {"usa", "us", "u.s.", "u.s.a.", "united states"}

# Matches a standalone state abbreviation (uppercase only, to avoid matching
# common lowercase words like "in" or "or") within a segment, e.g. the "GA"
# in "GA (contact venue@example.com)".
_STATE_ABBR_PATTERN = re.compile(r"\b(" + "|".join(_STATE_ABBREVIATIONS) + r")\b")
# Matches a full state name within a segment, longest names first so "West
# Virginia" isn't cut short by a "Virginia" match starting at the same spot.
_STATE_NAME_PATTERN = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in sorted(_STATE_ABBREVIATIONS.values(), key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _extract_us_state(location: str) -> str | None:
    """Returns the 2-letter US state code named in a location string, if any.

    Expects the common "<city>, <STATE>" convention: it splits on commas and
    looks at the last non-empty, non-country segment. If the location has no
    comma at all, that lone segment must exactly equal a state name/
    abbreviation (a bare "Georgia" counts, but "Georgia Institute of
    Technology" or "Texas Roadhouse" don't - free text incidentally
    containing a state's name isn't a reliable signal on its own). If the
    location does have commas, that final segment only needs to *contain* a
    state abbreviation/name (so trailing text like
    "GA (contact venue@example.com)" still matches), since a dedicated
    trailing segment is a much stronger signal that it's meant to carry
    state/region info.

    Returns None (rather than guessing) for locations with no confidently
    identifiable US state (e.g. "Virtual", "Zoom", a foreign city/country)
    so those events fall through to normal AI scoring instead of being
    silently discarded.
    """
    if not location:
        return None
    raw_segments = location.split(",")
    has_multiple_segments = len(raw_segments) > 1
    for part in reversed([segment.strip() for segment in raw_segments]):
        cleaned = re.sub(r"\d", "", part).strip()
        if not cleaned or cleaned.lower() in _NOISE_LOCATION_SEGMENTS:
            continue
        if not has_multiple_segments:
            if cleaned.upper() in _STATE_ABBREVIATIONS:
                return cleaned.upper()
            return _STATE_NAME_TO_ABBR.get(cleaned.lower())
        abbr_match = _STATE_ABBR_PATTERN.search(cleaned)
        if abbr_match:
            return abbr_match.group(1)
        name_match = _STATE_NAME_PATTERN.search(cleaned)
        if name_match:
            return _STATE_NAME_TO_ABBR[name_match.group(1).lower()]
        return None
    return None


def _split_by_state(events: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Splits events into (needs_scoring, auto_rejected) by location state.

    An event is auto-rejected only when its location names a US state we can
    confidently parse and that state isn't TARGET_STATE. Events with no
    location, an unparseable location, or a non-US location are left in
    needs_scoring so they still go through normal AI scoring.
    """
    needs_scoring = []
    auto_rejected = []
    for event in events:
        state = _extract_us_state(event.get("location", ""))
        if state and state != TARGET_STATE:
            auto_rejected.append((event, state))
        else:
            needs_scoring.append(event)
    return needs_scoring, auto_rejected


def _filter_past_events(events: list[dict]) -> list[dict]:
    """Drops events dated before today; keeps events with unparseable dates.

    Uses PREFER_DATES_FROM="future" so a yearless date like "January 10"
    scraped in June resolves to next January rather than the one already
    passed. Events whose date can't be parsed at all are kept (rather than
    dropped) so an unusual format doesn't silently lose a real event, but a
    warning is logged so bad formats are visible instead of silent.
    """
    today = datetime.date.today()
    filtered = []
    for event in events:
        date_str = event.get("date", "").strip()
        if not date_str:
            filtered.append(event)
            continue
        parsed = dateparser.parse(date_str, settings=_DATE_PARSER_SETTINGS)
        if parsed is None:
            print(f"  [warn] could not parse event date {date_str!r} "
                  f"for {event.get('title', 'Untitled')!r}; keeping it")
            filtered.append(event)
            continue
        event_date = parsed.date()
        event["date"] = event_date.strftime("%B %-d, %Y")
        if event_date >= today:
            filtered.append(event)
    return filtered


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
    events = _filter_past_events(events)
    if not events:
        return [{**base_row, "status": "no_events"}]

    events_to_score, auto_rejected = _split_by_state(events)

    rows = []
    for event, state in auto_rejected:
        row = {**base_row, "status": "ok"}
        row.update({field: event.get(field, "") for field in EXTRACTION_FIELDS})
        row["fit_score"] = 1
        row["confidence"] = "high"
        row["fit_reason"] = (
            f"Location is in {_STATE_ABBREVIATIONS[state]} ({state}), not "
            f"{_STATE_ABBREVIATIONS[TARGET_STATE]}; auto-rejected without scoring."
        )
        rows.append(row)

    if events_to_score:
        with ThreadPoolExecutor(max_workers=MAX_SCORING_WORKERS) as executor:
            scorings = list(
                executor.map(lambda event: score_event(client, event), events_to_score)
            )
        for event, scoring in zip(events_to_score, scorings):
            row = {**base_row, "status": "ok"}
            row.update({field: event.get(field, "") for field in EXTRACTION_FIELDS})
            row["fit_score"] = scoring["score"]
            row["confidence"] = scoring["confidence"]
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

    test_mode = "--test" in sys.argv
    fresh_mode = "--fresh" in sys.argv
    per_site_mode = "--per-site" in sys.argv

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

    # --per-site writes to its own dedicated file (see PER_SITE_OUTPUT_PATH)
    # instead of output_path, so there's nothing to clear out here for it -
    # write_per_site_sheets always starts from a blank workbook on its own.
    if (fresh_mode or test_mode) and not per_site_mode and os.path.exists(output_path):
        os.remove(output_path)
        print(f"Removed existing {output_path}")

    if not urls:
        print(f"No URLs found in {source}. Nothing to do.")
        sys.exit(0)

    print(f"Processing {len(urls)} site(s) from {source} (up to {MAX_WORKERS} at a time)...")

    all_rows = []
    rows_by_url = {}
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
            rows_by_url[url] = rows

    if per_site_mode:
        # Written in the original URL order (not completion order) for
        # readability, since as_completed() finishes them out of order.
        write_per_site_sheets(PER_SITE_OUTPUT_PATH, {url: rows_by_url[url] for url in urls})
        print(f"\nWrote {len(urls)} site sheet(s) to {PER_SITE_OUTPUT_PATH}")
    else:
        append_rows(output_path, all_rows)
        print(f"\nAppended {len(all_rows)} row(s) to {output_path}")


if __name__ == "__main__":
    main()
