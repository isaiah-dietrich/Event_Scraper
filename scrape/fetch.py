"""Fetch stage: render a JS-loaded page in Chromium and return its HTML."""

import datetime
import urllib.parse

from dateparser.search import search_dates
from playwright.sync_api import sync_playwright

from scrape.reduce import reduce_html

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1366, "height": 768}
_GOTO_TIMEOUT_MS = 30_000

# Content-settling: instead of a single fixed sleep, poll page.content() and
# consider the page settled once a couple of consecutive reads come back
# identical. This adapts to how long a given site's event list actually
# takes to render, rather than guessing one duration for every site.
_MIN_SETTLE_WAIT_MS = 1_500  # give client-side JS a moment to kick off first
_SETTLE_POLL_INTERVAL_MS = 500
_SETTLE_STABLE_CHECKS = 2  # consecutive unchanged reads required to call it settled
_MAX_SETTLE_WAIT_MS = 20_000  # give up and use whatever's rendered by here

# Scroll-triggered lazy loading: some event-calendar pages (e.g. Luma) only
# render the next batch of events once you scroll near the bottom, so a
# single page load only ever sees whatever fits in the initial viewport.
_MAX_SCROLL_ATTEMPTS = 8  # give up here even if the page keeps growing
_SCROLL_WAIT_MS = 1_000  # time to let a scroll's newly-loaded content render

# This pipeline runs weekly, so scrolling deep into the future to reach
# events months out isn't worth it - a later run will pick them up once
# they're closer. Once a scroll step's content is entirely beyond this
# cutoff, scrolling stops (see _is_beyond_future_cutoff).
_MAX_FUTURE_DAYS = 60  # ~2 months
_DATE_SEARCH_SETTINGS = {"PREFER_DATES_FROM": "future"}

# Numbered-page pagination (e.g. Eventbrite's "?page=1"): follow up to this
# many pages beyond the one given, in addition to whatever a single page's
# own scrolling reveals. Deliberately a flat cap rather than "stop once a
# page looks empty/duplicate" - some sites' listings aren't sorted
# chronologically (unlike a scrolling feed, which usually is), so there's
# no safe date-based signal to stop on early; a predictable cap keeps the
# per-site cost bounded instead.
_MAX_ADDITIONAL_PAGES = 4


def _is_beyond_future_cutoff(html: str) -> bool:
    """True if every date mentioned in this snapshot is more than
    _MAX_FUTURE_DAYS out.

    Uses dateparser's free-text date search directly on the page (no AI
    call needed) as a cheap signal for how far into the future a scroll
    step has reached. If no dates are found at all, returns False rather
    than guessing - an ambiguous snapshot shouldn't stop scrolling.

    Two dateparser quirks matter here, both seen on real pages with a
    persistent mini-calendar widget (e.g. Luma): a bare month/weekday name
    with no day number (e.g. "July", "Tuesday") often misparses to a wrong
    year, and search_dates resolves matches *sequentially*, using each
    match's resolved date as context for the next - so one bad bare-word
    match can cascade and corrupt an otherwise-correct date later in the
    same call. Searching line by line (page text has one date-ish token per
    line) prevents that cascade, and skipping matches whose matched text
    has no digit at all filters out the bare month/weekday names causing
    it. languages=["en"] additionally avoids cross-language fuzzy matches
    misreading ordinary English words (e.g. "here") as dates.
    """
    cutoff = datetime.date.today() + datetime.timedelta(days=_MAX_FUTURE_DAYS)
    found_a_date = False
    for line in reduce_html(html).splitlines():
        if not line.strip():
            continue
        matches = search_dates(line, languages=["en"], settings=_DATE_SEARCH_SETTINGS)
        if not matches:
            continue
        for matched_text, parsed in matches:
            if not any(character.isdigit() for character in matched_text):
                continue
            found_a_date = True
            if parsed.date() <= cutoff:
                return False
    return found_a_date


def _wait_for_content_to_settle(page) -> str:
    """Polls page.content() until it stops changing, then returns it.

    Some sites' event lists finish loading well under a second after
    domcontentloaded; others take much longer. Polling adapts to either,
    without hanging indefinitely on pages that never fully go idle (ad
    pixels, trackers, polling widgets) - those are simply cut off at
    _MAX_SETTLE_WAIT_MS and whatever's rendered by then is returned.
    """
    page.wait_for_timeout(_MIN_SETTLE_WAIT_MS)
    elapsed_ms = _MIN_SETTLE_WAIT_MS
    current_html = page.content()
    stable_count = 0
    while stable_count < _SETTLE_STABLE_CHECKS and elapsed_ms < _MAX_SETTLE_WAIT_MS:
        page.wait_for_timeout(_SETTLE_POLL_INTERVAL_MS)
        elapsed_ms += _SETTLE_POLL_INTERVAL_MS
        previous_html, current_html = current_html, page.content()
        stable_count = stable_count + 1 if current_html == previous_html else 0
    return current_html


def _scroll_and_collect_snapshots(page) -> list[str]:
    """Scrolls to the bottom repeatedly, capturing an HTML snapshot each time
    the page actually grows.

    Some event-calendar pages (Luma, etc.) use a *virtualized* list: only a
    window of events is ever mounted in the DOM at once, and earlier ones
    get unmounted as later ones scroll into view. That means no single
    scroll position ever shows the whole list - so instead of scrolling to
    the end and capturing once, this captures a snapshot at every step
    where new content loaded and returns them all, to be combined into one
    block of text before extraction. A duplicate/overlapping event
    mentioned in more than one snapshot is harmless: extraction already
    treats repeated mentions of the same event as one distinct event
    rather than double-counting it.

    Skips scrolling entirely if the page doesn't even overflow the
    viewport - there's nothing below the fold to reveal. Otherwise stops
    once a scroll doesn't grow the page (nothing more to load) or once a
    step's content is entirely beyond _MAX_FUTURE_DAYS out (see
    _is_beyond_future_cutoff). That cutoff only stops *further* scrolling -
    whatever's in the snapshot that crossed it is still kept, since content
    we already have is worth keeping even if it's further out than we'd
    scroll to reach on purpose. Gives up at _MAX_SCROLL_ATTEMPTS regardless,
    since some calendars have an effectively unbounded number of future
    events to keep loading.
    """
    snapshots = []
    previous_height = page.evaluate("document.body.scrollHeight")
    viewport_height = page.evaluate("window.innerHeight")
    if previous_height <= viewport_height:
        return snapshots
    for _ in range(_MAX_SCROLL_ATTEMPTS):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(_SCROLL_WAIT_MS)
        current_height = page.evaluate("document.body.scrollHeight")
        if current_height <= previous_height:
            break
        html = page.content()
        snapshots.append(html)
        previous_height = current_height
        if _is_beyond_future_cutoff(html):
            break
    return snapshots


def _page_urls_to_follow(url: str) -> list[str]:
    """Returns [url] plus up to _MAX_ADDITIONAL_PAGES follow-up URLs with an
    incremented "page" query parameter, if `url` has one to increment.

    Deliberately narrow: only recognizes a literal "page" query parameter
    with a numeric value (e.g. "...?page=1"), the one pagination convention
    actually confirmed in the wild so far (Eventbrite). Sites that paginate
    some other way (a "Load More" button, cursor-based URLs, a path segment
    like "/page/2/", etc.) aren't detected - those still need a URL added
    manually per page, same as before this existed.
    """
    parsed = urllib.parse.urlsplit(url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    current_page = next(
        (int(value) for key, value in query_pairs if key == "page" and value.isdigit()),
        None,
    )
    if current_page is None:
        return [url]

    urls = [url]
    for offset in range(1, _MAX_ADDITIONAL_PAGES + 1):
        next_pairs = [
            (key, str(current_page + offset)) if key == "page" else (key, value)
            for key, value in query_pairs
        ]
        next_query = urllib.parse.urlencode(next_pairs)
        urls.append(urllib.parse.urlunsplit(parsed._replace(query=next_query)))
    return urls


def _load_and_capture(page, url: str) -> str:
    """Navigates to `url` and returns its settled HTML plus any additional
    content pulled in by scrolling (see _wait_for_content_to_settle and
    _scroll_and_collect_snapshots)."""
    page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
    first_snapshot = _wait_for_content_to_settle(page)
    more_snapshots = _scroll_and_collect_snapshots(page)
    return "\n".join([first_snapshot] + more_snapshots)


def fetch_rendered_html(url: str) -> str:
    """Loads a URL (and, if paginated, its numbered follow-up pages) in
    Chromium and returns the combined rendered HTML.

    Runs Chromium non-headless with a realistic user agent, since some
    sites (e.g. those behind Cloudflare) detect and block headless
    automation outright. See _load_and_capture for how a single page is
    loaded and settled, and _page_urls_to_follow for how additional
    numbered pages (e.g. Eventbrite's "?page=1", "?page=2", ...) are
    detected and queued up alongside it.

    Args:
        url: The page to load.

    Returns:
        Every loaded page's settled (and scrolled) HTML, joined together as
        a single string.

    Raises:
        RuntimeError: If any page fails to load for any reason.
    """
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            page = browser.new_page(user_agent=_USER_AGENT, viewport=_VIEWPORT)
            snapshots = [_load_and_capture(page, page_url) for page_url in _page_urls_to_follow(url)]
            browser.close()
            return "\n".join(snapshots)
    except Exception as error:
        raise RuntimeError(f"Failed to load page with Playwright: {error}") from error
