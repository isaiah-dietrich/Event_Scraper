"""Enrich stage: fill in blank descriptions from each event's own detail page.

Why this exists: every SITE_URL is a calendar *index* page, and the card-grid
ones (Luma, Eventbrite, Meetup, the GMA calendar) render only a title, date,
time and venue per event - no blurb anywhere in the markdown. The extract
stage is deliberately forbidden from inventing a description when none is
present in the text (see scrape.extract), so those events arrive here with
"short_description" empty. Measured over the master workbook and the
2026-07-19 digest: Luma was 0-for-112 and Eventbrite 0-for-51, while sites
that render full listings (Wild Apricot, GaTech, TAG) were near 100%.

That blank isn't only cosmetic - it depresses the fit score. scrape.score
judges AI-relevance from the event's own title/description and auto-assigns
a 1 when it sees no AI content at all, so an AI event whose card shows only
"Fireside Chat with Jane Doe" gets scored on the title alone. In that same
digest, *every* event missing a description scored low or medium confidence
and every 5/5 event had one - the scorer already flags exactly the rows worth
enriching, which is what select_rows_to_enrich keys off.

So: for the events the scorer was unsure about, scrape the one page that does
have the text (the event's own signup_link), pull a description from it, and
re-score with the fuller picture. The scrape is the expensive part - one
Firecrawl credit each - so the candidate set is filtered hard and capped
twice, by MAX_ENRICHMENT_SCRAPES here and by the run-wide budget in
scrape.fetch.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic

from scrape.fetch import remaining_scrape_budget
from scrape.fetch import ScrapeBudgetExceeded
from scrape.fetch import _scrape_one_page
from scrape.reduce import collapse_repeated_blocks
from scrape.score import score_event
from utility.token_usage import tracker as token_usage_tracker

# Pulling one description out of a page already known to describe a single
# event is a small extraction task, so it uses Haiku rather than the Sonnet
# model the multi-event page extraction needs (see scrape.extract.MODEL).
MODEL = "claude-haiku-4-5-20251001"

# Everyday cap on how many detail pages one run may scrape. Deliberately well
# under scrape.fetch.MAX_SCRAPES_PER_RUN so the run-wide budget stays a
# backstop rather than the thing that normally binds: at ~14 listing scrapes
# plus this, a full run costs ~134 credits against a 1000/month plan and
# ~4-5 runs a month. Raise it only alongside that monthly headroom.
MAX_ENRICHMENT_SCRAPES = 120

# Detail pages are single-event pages, so the description is virtually always
# near the top; truncating bounds token spend on the occasional page that
# also carries a long comment thread or a full event archive below the fold.
_MAX_DETAIL_CHARS = 12000

# Descriptions land in a spreadsheet cell the client reads at a glance - the
# ones extraction already produces average ~180 characters, so anything past
# this is more than the column can usefully show.
_MAX_DESCRIPTION_CHARS = 400

# Only the scorer's own uncertainty markers. A "high" confidence score means
# the model had enough to go on, so paying a credit to tell it more is waste
# - and it's also what the state/country auto-reject path always sets (see
# cli.batch._split_by_state), which keeps those events from being scraped.
_ENRICHABLE_CONFIDENCE_LEVELS = {"low", "medium"}

_FENCE_START_PATTERN = re.compile(r"^```(?:json)?\s*")
_FENCE_END_PATTERN = re.compile(r"\s*```$")

_DESCRIPTION_PROMPT_TEMPLATE = """\
Below is the text of a webpage for a single event titled "{title}".

Find the event's own description - the text that says what the event is
about, what happens at it, who it's for, or what will be covered.

Return ONLY a valid JSON object (no prose, no markdown code fences) with
exactly one field: "short_description".

Rules:
- The value must be based on descriptive text actually present on the page.
  Trimming and light rewording for length is fine. Do NOT synthesize a
  description out of just the title, date, time, location, price, or host
  name - if the page has no real descriptive text about the event, return
  an empty string "" instead of making something up.
- Keep it under {max_chars} characters. If the page's description is longer,
  use its opening as a summary rather than truncating mid-sentence.
- Do not include the date, time, ticket price, or venue address - those are
  captured separately. Describe the event's content.
- Return "" if the page is an error page, a login/verification wall, or is
  clearly not about this event at all.

PAGE CONTENT:
{page_text}
"""


def _needs_description(row: dict) -> bool:
    """True if `row` is a scored event with no description and a real link.

    A row only qualifies when all four hold: it's an actual event row
    ("ok" status, so failure/no_events rows are skipped), its description is
    genuinely blank, the scorer was unsure about it (see
    _ENRICHABLE_CONFIDENCE_LEVELS), and its signup_link is a real per-event
    URL. That last check excludes the base-URL fallback cli.batch.
    _build_event_row writes when extraction found no link, which is flagged
    with a trailing " *" - scraping it would just re-fetch the calendar
    index page we already have and burn a credit for nothing.
    """
    if row.get("status") != "ok":
        return False
    if str(row.get("short_description", "") or "").strip():
        return False
    if str(row.get("confidence", "") or "").strip().lower() not in _ENRICHABLE_CONFIDENCE_LEVELS:
        return False
    link = str(row.get("signup_link", "") or "").strip()
    return bool(link) and not link.endswith("*") and link.startswith(("http://", "https://"))


def select_rows_to_enrich(rows: list[dict], limit: int) -> list[dict]:
    """Picks up to `limit` rows to enrich, best-scoring first.

    Ordering matters only when the candidate set is larger than the budget:
    the highest-scoring events are the ones most likely to reach the client,
    so they get the credits first. Ties keep their original order, so the
    same input always selects the same rows.

    Args:
        rows: This run's output rows, aggregated across every site.
        limit: Maximum number of rows to return. A limit <= 0 selects none.

    Returns:
        The chosen row dicts themselves (not copies), so callers can mutate
        them in place.
    """
    if limit <= 0:
        return []
    candidates = [row for row in rows if _needs_description(row)]
    candidates.sort(key=lambda row: row.get("fit_score") or 0, reverse=True)
    return candidates[:limit]


def _extract_description(client: Anthropic, title: str, page_text: str) -> str:
    """Asks Claude for the event's description from its detail page text.

    Returns an empty string - never a raised exception - when the page has
    no real description, when the model's reply can't be parsed, or when the
    reply was cut off by max_tokens. A missing description is the status quo
    for this row, so there is nothing to fail the run over.
    """
    prompt = _DESCRIPTION_PROMPT_TEMPLATE.format(
        title=title or "(untitled)",
        max_chars=_MAX_DESCRIPTION_CHARS,
        page_text=page_text[:_MAX_DETAIL_CHARS],
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    token_usage_tracker.record(response)
    if response.stop_reason == "max_tokens":
        return ""
    raw_text = response.content[0].text.strip()
    cleaned = _FENCE_START_PATTERN.sub("", raw_text)
    cleaned = _FENCE_END_PATTERN.sub("", cleaned)
    try:
        description = str(json.loads(cleaned).get("short_description", "") or "").strip()
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return ""
    return description[:_MAX_DESCRIPTION_CHARS].strip()


def _enrich_one(client: Anthropic, row: dict) -> bool:
    """Scrapes one event's detail page, fills its description, re-scores it.

    Mutates `row` in place. The re-score is the point of the exercise - the
    original score was made without the description this just found - so it
    only happens when a description was actually recovered; a page that
    yielded nothing leaves the row exactly as it was.

    Every failure mode here is non-fatal and logged rather than raised: this
    is an optional quality pass over rows that are already complete and
    valid, so a dead link or a scrape error must never take down a run that
    has otherwise finished all its work.

    Returns:
        True if a description was found and the row was updated.
    """
    title = str(row.get("title", "") or "")
    try:
        markdown = _scrape_one_page(str(row["signup_link"]).strip())
    except ScrapeBudgetExceeded:
        raise
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [enrich] scrape failed for {title!r}: {error}")
        return False

    page_text = collapse_repeated_blocks(markdown)
    if not page_text:
        return False

    try:
        description = _extract_description(client, title, page_text)
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [enrich] description call failed for {title!r}: {error}")
        return False
    if not description:
        return False

    row["short_description"] = description
    try:
        scoring = score_event(client, {field: row.get(field, "") for field in (
            "title", "date", "start_time", "location", "is_in_person",
            "signup_link", "short_description",
        )})
    except Exception as error:  # pylint: disable=broad-except
        # The description is still a genuine improvement on its own, so keep
        # it and leave the original score in place.
        print(f"  [enrich] re-score failed for {title!r}: {error}")
        return True

    old_score = row.get("fit_score")
    row["fit_score"] = scoring["score"]
    row["confidence"] = scoring["confidence"]
    row["fit_reason"] = scoring["reason"]
    if scoring["score"] != old_score:
        print(f"  [enrich] {title!r}: score {old_score} -> {scoring['score']}")
    return True


# Detail-page scrapes are independent of each other, and scrape.fetch's own
# semaphore and rate limiter are what actually pace the Firecrawl calls, so
# this only needs to be wide enough to keep those limits saturated.
MAX_ENRICHMENT_WORKERS = 4


def enrich_descriptions(client: Anthropic, rows: list[dict]) -> int:
    """Fills blank descriptions on this run's rows, in place, within budget.

    Call this once per run, after every site has been processed and the rows
    aggregated, and before the rows are written or emailed - re-scoring can
    move an event between the digest's New Events and Rejected Events
    sheets (see utility.io_excel._is_rejected), so the write must see the
    final scores.

    How many rows get enriched is the smaller of MAX_ENRICHMENT_SCRAPES and
    whatever the run-wide credit budget has left (see
    scrape.fetch.MAX_SCRAPES_PER_RUN), so this stage can never spend credits
    the listing scrapes were going to need - they run first and have already
    taken theirs by the time this is called.

    Args:
        client: An initialized Anthropic client.
        rows: This run's output rows, aggregated across every site. Mutated
            in place; rows that aren't enriched are left untouched.

    Returns:
        The number of rows that gained a description.
    """
    budget = min(MAX_ENRICHMENT_SCRAPES, remaining_scrape_budget())
    selected = select_rows_to_enrich(rows, budget)
    total_missing = sum(1 for row in rows if _needs_description(row))
    if not selected:
        if total_missing:
            print(f"\nSkipping description enrichment: no scrape budget left "
                  f"({total_missing} event(s) could have used it).")
        return 0

    print(f"\nEnriching descriptions for {len(selected)} of {total_missing} "
          f"event(s) missing one (budget: {budget} scrape(s))...")
    with ThreadPoolExecutor(max_workers=MAX_ENRICHMENT_WORKERS) as executor:
        results = list(executor.map(lambda row: _safe_enrich_one(client, row), selected))
    enriched_count = sum(1 for result in results if result)
    print(f"Enriched {enriched_count} of {len(selected)} event(s).")
    return enriched_count


def _safe_enrich_one(client: Anthropic, row: dict) -> bool:
    """_enrich_one, with a budget exhaustion treated as "just stop".

    The budget can run out mid-pass even though enrich_descriptions sized
    the selection to fit it, if something else spent credits concurrently.
    That's a clean stop, not an error worth failing the run over.
    """
    try:
        return _enrich_one(client, row)
    except ScrapeBudgetExceeded:
        return False
