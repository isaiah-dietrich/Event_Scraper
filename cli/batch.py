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
from utility.io_excel import event_key
from utility.io_excel import read_existing_event_keys
from utility.io_excel import read_input_urls
from utility.io_excel import write_per_site_sheets
from utility.token_usage import check_and_record_usage
from utility.token_usage import tracker as token_usage_tracker

INPUT_PATH = "websites.xlsx"
OUTPUT_PATH = "events_output.xlsx"
TEST_OUTPUT_PATH = "events_output_test.xlsx"

# Diagnostic-only output for --per-site (see main()): one sheet per URL, so a
# run's accuracy can be checked site-by-site. Not the permanent output format
# - that's still the single Events/Rejected Events workbook via append_rows.
PER_SITE_OUTPUT_PATH = "events_output_by_site.xlsx"

# Log of every run's token usage (see utility.token_usage), so a run that
# uses much more than the last one of the same mode gets flagged instead of
# silently costing more than expected.
TOKEN_USAGE_HISTORY_PATH = "token_usage_history.json"

# Paste site URLs here to test the pipeline without touching websites.xlsx.
# Run with: python run.py --test
TEST_URLS = [
    "https://ai.gatech.edu/events",
    #"https://members.tagonline.org/calendar",
    "https://www.georgiamanufacturingalliance.com/events/",
    #"https://www.aimanufacturingconference.com/",
    #"https://www.meetup.com/find/?source=EVENTS&categoryId=546&location=us--georgia",
    #"https://luma.com/genai-collective",
    #"https://www.eventbrite.com/d/united-states--georgia/science-and-tech--events--this-month/?page=1"
    #"https://atlanta.aitinkerers.org/",
    #"https://www.meetup.com/atlbitlab/events/"
]

# Each site gets its own browser instance and its own LLM call, run
# concurrently. Each worker pops a visible Chromium window (headless=False,
# see ai_event_scraper.fetch), so don't set this too high locally.
MAX_WORKERS = 4

# Within a single site, events are scored concurrently too (one Haiku call
# per event). This is independent of MAX_WORKERS above.
MAX_SCORING_WORKERS = 8

# A Cloudflare-style "challenge" interstitial (e.g. "Just a moment... Please
# wait while we verify you are human") is what fetch_rendered_html sometimes
# returns instead of real content when a site's bot protection blocks even a
# visible, non-headless browser. reduce_html strips it down to a handful of
# lines of garbage, so process_site checks for these markers right after
# reduce and short-circuits before extract_events - otherwise that garbage
# still gets sent through a full Sonnet extraction call that can't possibly
# produce events. The length guard exists so a legitimate long page that
# merely quotes one of these phrases somewhere in its own content is never
# misclassified: a real challenge page's reduced text is only ever a few
# short lines, so anything past that length is assumed to be genuine content.
_BOT_CHALLENGE_MAX_CHARS = 600
_BOT_CHALLENGE_MARKERS = (
    "just a moment",
    "verifying you are human",
    "checking your browser",
    "enable javascript and cookies",
    "attention required",
)


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


# Country names that appear as the trailing location segment (e.g. "Mumbai,
# India") are auto-rejected the same way as a non-Georgia US state, since a
# clearly-international event costs a scoring AI call for nothing. Not
# exhaustive of all ~195 countries, just the ones worth covering. "Georgia"
# is deliberately excluded - it collides with the US state of the same name
# (see _extract_us_state), so an event genuinely in the country of Georgia
# just falls through to normal AI scoring instead of being misclassified.
_FOREIGN_COUNTRIES = {
    "afghanistan", "albania", "algeria", "argentina", "armenia", "australia",
    "austria", "azerbaijan", "bahrain", "bangladesh", "belarus", "belgium",
    "bolivia", "bosnia and herzegovina", "brazil", "bulgaria", "cambodia",
    "cameroon", "canada", "chile", "china", "colombia", "costa rica",
    "croatia", "cuba", "cyprus", "czechia", "czech republic", "denmark",
    "dominican republic", "ecuador", "egypt", "estonia", "ethiopia",
    "finland", "france", "germany", "ghana", "greece", "guatemala",
    "honduras", "hong kong", "hungary", "iceland", "india", "indonesia",
    "iran", "iraq", "ireland", "israel", "italy", "jamaica", "japan",
    "jordan", "kazakhstan", "kenya", "kuwait", "laos", "latvia", "lebanon",
    "lithuania", "luxembourg", "malaysia", "malta", "mexico", "moldova",
    "monaco", "mongolia", "morocco", "myanmar", "nepal", "netherlands",
    "new zealand", "nicaragua", "nigeria", "north macedonia", "norway",
    "oman", "pakistan", "panama", "paraguay", "peru", "philippines",
    "poland", "portugal", "qatar", "romania", "russia", "rwanda",
    "saudi arabia", "senegal", "serbia", "singapore", "slovakia",
    "slovenia", "south africa", "south korea", "spain", "sri lanka",
    "sweden", "switzerland", "taiwan", "tanzania", "thailand", "tunisia",
    "turkey", "uganda", "ukraine", "united arab emirates", "united kingdom",
    "uruguay", "uzbekistan", "venezuela", "vietnam", "zambia", "zimbabwe",
}
# Longest names first so e.g. "Czech Republic" isn't cut short by a
# "Czechia" match starting at the same spot (not actually a substring
# collision here, but kept consistent with _STATE_NAME_PATTERN's approach).
_FOREIGN_COUNTRY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in sorted(_FOREIGN_COUNTRIES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _extract_foreign_country(location: str) -> str | None:
    """Returns the non-US country named in a location string, if any.

    Mirrors _extract_us_state's approach: the trailing comma-separated
    segment (or the whole string, if there's no comma) is checked against
    _FOREIGN_COUNTRIES. A bare, comma-less location must match exactly (so
    a venue/org name that happens to contain a country's name isn't
    misread - same reasoning as "Texas Roadhouse" not matching a state);
    a trailing segment after a comma only needs to *contain* a country
    name, so trailing text like "India (venue TBD)" still matches.

    Returns None (rather than guessing) for anything not confidently a
    named non-US country (e.g. "Virtual", a bare city, an ambiguous name)
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
            return cleaned if cleaned.lower() in _FOREIGN_COUNTRIES else None
        match = _FOREIGN_COUNTRY_PATTERN.search(cleaned)
        return match.group(1) if match else None
    return None


def _split_by_state(events: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Splits events into (needs_scoring, auto_rejected) by location.

    An event is auto-rejected, skipping the scoring AI call entirely, when
    its location confidently names either a US state other than
    TARGET_STATE (see _extract_us_state) or a non-US country (see
    _extract_foreign_country) - either way it can't be Georgia. Events with
    no location, an unparseable one, or one we can't confidently place
    (e.g. "Virtual", "Zoom", a bare city name) are left in needs_scoring so
    they still go through normal AI scoring rather than being silently
    discarded on a guess.

    Returns:
        (needs_scoring, auto_rejected), where auto_rejected holds
        (event, reason) pairs - `reason` is a ready-to-use sentence
        explaining why that event was rejected without scoring.
    """
    needs_scoring = []
    auto_rejected = []
    for event in events:
        location = event.get("location", "")
        state = _extract_us_state(location)
        if state and state != TARGET_STATE:
            reason = (
                f"Location is in {_STATE_ABBREVIATIONS[state]} ({state}), not "
                f"{_STATE_ABBREVIATIONS[TARGET_STATE]}; auto-rejected without scoring."
            )
            auto_rejected.append((event, reason))
            continue
        country = _extract_foreign_country(location)
        if country:
            reason = (
                f"Location is in {country}, not "
                f"{_STATE_ABBREVIATIONS[TARGET_STATE]}, USA; auto-rejected without scoring."
            )
            auto_rejected.append((event, reason))
            continue
        needs_scoring.append(event)
    return needs_scoring, auto_rejected


def _parse_event_date(date_str: str) -> datetime.datetime | None:
    """Parses a free-text event date string, preferring future occurrences.

    Uses PREFER_DATES_FROM="future" so a yearless date like "January 10"
    scraped in June resolves to next January rather than the one already
    passed. Returns None (rather than raising) for blank/unparseable input,
    so callers can decide how to handle that case themselves.
    """
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    return dateparser.parse(date_str, settings=_DATE_PARSER_SETTINGS)


def _filter_past_events(events: list[dict]) -> list[dict]:
    """Drops events dated before today; keeps events with unparseable dates.

    Events whose date can't be parsed at all are kept (rather than dropped)
    so an unusual format doesn't silently lose a real event, but a warning
    is logged so bad formats are visible instead of silent.

    A successfully parsed event's "date" is replaced with a real
    datetime.datetime (not a formatted string), so it's written to the
    spreadsheet as an actual date value that Excel can sort/filter on
    correctly - see utility.io_excel.append_rows. It's specifically a
    datetime rather than a plain date because openpyxl always reads date
    cells back as datetime.datetime, and dedupe (_event_key) needs a run's
    freshly computed value to compare equal to that same event re-read from
    a previous run's output.
    """
    today = datetime.date.today()
    filtered = []
    for event in events:
        date_str = event.get("date", "").strip()
        if not date_str:
            filtered.append(event)
            continue
        parsed = _parse_event_date(date_str)
        if parsed is None:
            print(f"  [warn] could not parse event date {date_str!r} "
                  f"for {event.get('title', 'Untitled')!r}; keeping it")
            filtered.append(event)
            continue
        event_date = parsed.date()
        event["date"] = datetime.datetime.combine(event_date, datetime.time())
        if event_date >= today:
            filtered.append(event)
    return filtered


def _timestamp() -> str:
    """Returns today's local date as "<Month> <Day>, <Year>", e.g. "June 21, 2026"."""
    today = datetime.date.today()
    return f"{today:%B} {today.day}, {today:%Y}"


def process_site(
    client: Anthropic, url: str, known_keys: frozenset = frozenset()
) -> list[dict]:
    """Runs fetch, reduce, and extract for one site.

    Args:
        client: An initialized Anthropic client.
        url: The site URL to scrape.
        known_keys: Dedupe keys (see utility.io_excel.event_key) for events
            already present in the output workbook from a previous run.
            After date-filtering, any event whose key is in known_keys is
            dropped before scoring - no scoring call, no output row - since
            write-time dedupe in append_rows would silently drop its row
            anyway, so scoring it first would only have been wasted API
            cost. Defaults to an empty set so existing callers, and the
            --per-site diagnostic path in particular, are unaffected.

    Returns:
        A list of output row dicts: one per extracted event on success, or
        a single row describing the failure/empty-result status otherwise.
        Can also be an empty list if every extracted event turned out to
        already be known (see known_keys).
    """
    base_row = {"scraped_at": _timestamp(), "source_url": url}

    try:
        html = fetch_rendered_html(url)
    except RuntimeError as error:
        return [{**base_row, "status": f"failed: {error}"}]

    page_text = reduce_html(html, base_url=url)
    if not page_text:
        return [{**base_row, "status": "failed: empty page text after reduction"}]

    if len(page_text) < _BOT_CHALLENGE_MAX_CHARS and any(
        marker in page_text.lower() for marker in _BOT_CHALLENGE_MARKERS
    ):
        return [{**base_row, "status": "failed: blocked by bot protection (challenge page)"}]

    try:
        events = extract_events(client, page_text)
    except ValueError as error:
        return [{**base_row, "status": f"failed: {error}"}]
    events = _filter_past_events(events)
    if not events:
        return [{**base_row, "status": "no_events"}]

    if known_keys:
        remaining = []
        skipped_count = 0
        for event in events:
            key = event_key(url, event.get("title", ""), event.get("date", ""))
            if key in known_keys:
                skipped_count += 1
            else:
                remaining.append(event)
        events = remaining
        if skipped_count:
            print(f"    [skip] {skipped_count} event(s) already in workbook")

    events_to_score, auto_rejected = _split_by_state(events)

    rows = []
    for event, reason in auto_rejected:
        row = {**base_row, "status": "ok"}
        row.update({field: event.get(field, "") for field in EXTRACTION_FIELDS})
        row["fit_score"] = 1
        row["confidence"] = "high"
        row["fit_reason"] = reason
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

    # Labels which output shape this run produced (see check_and_record_usage)
    # so token usage is only ever compared against past runs of the same
    # shape - "--test" scrapes a handful of scratch URLs (TEST_URLS) and
    # would otherwise look like a nonsensical swing against a full run over
    # websites.xlsx, or vice versa. --fresh doesn't get its own label since
    # it only changes whether output is wiped first, not which URLs/output
    # format are used.
    usage_mode = ("test_" if test_mode else "") + ("per_site" if per_site_mode else "normal")

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

    # Pre-scoring dedupe: load which events are already in the output
    # workbook (a no-op empty set on a fresh/just-wiped file) so process_site
    # can skip a Haiku scoring call for any of them entirely, instead of
    # scoring them only to have write-time dedupe in append_rows silently
    # drop the row anyway. Skipped for --per-site: that mode writes to its
    # own diagnostic file, not output_path, and its whole point is showing
    # fresh scoring for every event on every run (see CLAUDE.md), so it must
    # never be handed a nonempty known_keys.
    known_keys = frozenset() if per_site_mode else frozenset(read_existing_event_keys(output_path))

    print(f"Processing {len(urls)} site(s) from {source} (up to {MAX_WORKERS} at a time)...")

    all_rows = []
    rows_by_url = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {
            executor.submit(process_site, client, url, known_keys): url for url in urls
        }
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

    print(f"Token usage: {token_usage_tracker.summary()}")
    check_and_record_usage(token_usage_tracker, TOKEN_USAGE_HISTORY_PATH, usage_mode)


if __name__ == "__main__":
    main()
