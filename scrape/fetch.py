"""Fetch stage: render a JS-loaded page in Chromium and return its HTML."""

import datetime
import re
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
# single page load only ever sees whatever fits in the initial viewport. We
# scroll one viewport at a time (see _scroll_and_collect_snapshots) rather
# than jumping straight to the bottom: a jump can skip past content that only
# mounts as it enters the viewport, and some lazy-loaders only fire their
# IntersectionObserver near the sentinel a single step reaches.
_MAX_SCROLL_ATTEMPTS = 24  # counts one-viewport steps now, not bottom-jumps -
# a jump crossed the whole page in one move, so the old cap was small; stepping
# a viewport at a time needs many more steps to walk a long feed to its end.
_SCROLL_WAIT_MS = 1_000  # time to let a scroll's newly-loaded content render
# A 2px slack absorbs sub-pixel rounding in scrollY/scrollHeight so "we've
# reached the bottom" doesn't flap on a page that's effectively at its end.
_AT_BOTTOM_SCRIPT = (
    "(window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 2)"
)

# This pipeline runs weekly, so scrolling deep into the future to reach
# events months out isn't worth it - a later run will pick them up once
# they're closer. Once a scroll step's content is entirely beyond this
# cutoff, scrolling stops (see _is_beyond_future_cutoff).
_MAX_FUTURE_DAYS = 60  # ~2 months
_DATE_SEARCH_SETTINGS = {"PREFER_DATES_FROM": "future"}

# "Load more"/"Show more events" buttons: when scrolling stalls, some lists
# keep the rest of their events behind an explicit control instead of an
# infinite scroll. This is a deliberately conservative text-matched click
# loop, not a generic "click anything that looks like a button" heuristic:
# only a button/link whose visible label *begins* with one of these phrases
# is clicked, so it can't stumble onto "Load more comments", "See more
# photos", or a destructive action elsewhere on the page. Capped low because
# each click pays a fresh settle.
_LOAD_MORE_TEXT_PATTERN = re.compile(
    r"^(?:load|show|view|see)\s+more\b|^more\s+events?\b", re.IGNORECASE
)
_MAX_LOAD_MORE_CLICKS = 5
# Find-and-click happens in one page-context call so the match and the click
# are atomic (the element can't shift between a separate query and click).
# The Python pattern is the single source of truth; its source is embedded as
# a JS string literal (repr keeps the backslashes escaped the way JS wants).
_LOAD_MORE_CLICK_SCRIPT = """
(() => {
    const pattern = new RegExp(__PATTERN__, "i");
    const clickables = document.querySelectorAll('button, a, [role="button"]');
    for (const element of clickables) {
        const label = (element.textContent || "").trim();
        if (!pattern.test(label)) continue;
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        element.click();
        return true;
    }
    return false;
})()
""".replace("__PATTERN__", repr(_LOAD_MORE_TEXT_PATTERN.pattern))

# Embedded calendars (Google Calendar embeds, Localist, GrowthZone /
# ChamberMaster - common for chambers of commerce) live in an <iframe>, and
# page.content() only serializes the main frame, so their events would
# otherwise reach the LLM as zero text. Child frames are captured separately
# (see _capture_child_frames). Trivial frames (about:blank, and the tiny ad
# pixels / social widgets that carry no event text) are skipped by an HTML
# size floor rather than a per-vendor allowlist.
_MIN_FRAME_HTML_CHARS = 500

# Month-by-month calendar navigation: some calendars render only one month at
# a time entirely client-side, with no URL or scroll position to key off of -
# so a single page load only ever sees whichever month opens by default
# (usually the current one). We recognize a small, ordered allowlist of known
# "next month" controls rather than guessing at arbitrary arrows, trying the
# first one actually present on the page (see
# _click_next_month_and_collect_snapshots).
_NEXT_MONTH_SELECTORS = (
    ".fc-next-button",  # FullCalendar (https://fullcalendar.io)
    ".tribe-events-c-nav__next",  # The Events Calendar (WordPress plugin)
    '[aria-label*="next month" i]',  # generic ARIA-labeled next control
)

# Cut short earlier in the common case by _is_beyond_future_cutoff; this is
# just a hard ceiling for when that never trips (e.g. a calendar with an
# effectively unbounded run of future months).
_MAX_MONTH_CLICKS = 3

# Numbered-page pagination (e.g. Eventbrite's "?page=1"): follow up to this
# many pages beyond the one given, in addition to whatever a single page's
# own scrolling reveals. A flat cap bounds the per-site cost; on top of it,
# pagination stops early once a page's content repeats one already fetched
# (see _pages_are_near_duplicate), since many sites serve page 1's listing
# verbatim for out-of-range page numbers.
_MAX_ADDITIONAL_PAGES = 4
# Two pages count as the same content once this fraction of one's non-blank
# text lines already appeared on the other - a near-subset, not just a
# byte-for-byte match, to tolerate incidental per-page chrome differences.
_DUPLICATE_PAGE_OVERLAP_RATIO = 0.95

# A page load can fail transiently (a flaky navigation, a slow CDN, a
# Cloudflare interstitial that clears on a second try). One retry after a
# short pause clears most of those without turning fetch into an unbounded
# retry loop.
_RETRY_WAIT_MS = 2_000

# Snapshot subsumption pruning: every scroll/click step captures the full DOM
# at that moment, so a grow-only page scrolled N times would otherwise be sent
# to the LLM ~N times over. A snapshot is dropped only if it adds essentially
# nothing new over the snapshots already kept (see _prune_subsumed_snapshots).
_PRUNE_MIN_NEW_LINES = 2  # keep a snapshot contributing more than this many
_PRUNE_MIN_NEW_LINE_RATIO = 0.02  # ...or more than this fraction of its own lines


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


def _non_blank_lines(html: str) -> set[str]:
    """The set of non-blank visible-text lines in `html`, via reduce_html.

    A snapshot's reduced text lines are the unit both the duplicate-page
    check and snapshot pruning compare on - working on the same reduced,
    de-marked-up text the LLM eventually sees keeps those decisions aligned
    with what actually reaches extraction.
    """
    return {line for line in reduce_html(html).splitlines() if line.strip()}


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


def _capture_child_frames(page) -> list[str]:
    """Captures the HTML of every non-trivial child frame on the page.

    page.content() only serializes the main frame, so an embedded calendar
    (Google Calendar, Localist, GrowthZone/ChamberMaster) contributes no
    text at all without this. frame.content() works cross-origin because
    Playwright drives it over CDP rather than through the DOM. about:blank
    frames and anything below _MIN_FRAME_HTML_CHARS (ad pixels, social
    widgets) carry no event text and are skipped. Each read is wrapped
    because a frame can detach between the frames() enumeration and the
    read - a detached frame is simply skipped rather than failing the fetch.
    """
    snapshots = []
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        if not frame.url or frame.url == "about:blank":
            continue
        try:
            html = frame.content()
        except Exception:
            continue
        if len(html) < _MIN_FRAME_HTML_CHARS:
            continue
        snapshots.append(html)
    return snapshots


def _scroll_and_collect_snapshots(page) -> list[str]:
    """Scrolls down one viewport at a time, capturing an HTML snapshot each
    step the page's content actually changes.

    Some event-calendar pages (Luma, etc.) use a *virtualized* list: only a
    window of events is ever mounted in the DOM at once, and earlier ones
    get unmounted as later ones scroll into view. That means no single
    scroll position ever shows the whole list - so instead of scrolling to
    the end and capturing once, this captures a snapshot at every step
    where the DOM changed and returns them all, to be combined into one
    block of text before extraction. A duplicate/overlapping event
    mentioned in more than one snapshot is harmless: extraction already
    treats repeated mentions of the same event as one distinct event
    rather than double-counting it.

    Stepping a single viewport at a time (rather than jumping to the
    bottom) matters for exactly those virtualized/lazy lists: a jump can
    skip past content that only mounts as it scrolls through the viewport,
    and some lazy-loaders only fire their IntersectionObserver when the
    sentinel a single step reaches comes into view.

    Skips scrolling entirely if the page doesn't even overflow the
    viewport - there's nothing below the fold to reveal. Otherwise stops
    once the bottom is reached and the page has stopped growing (nothing
    more to load), or once a step's content is entirely beyond
    _MAX_FUTURE_DAYS out (see _is_beyond_future_cutoff). That cutoff only
    stops *further* scrolling - whatever's in the snapshot that crossed it
    is still kept, since content we already have is worth keeping even if
    it's further out than we'd scroll to reach on purpose. Gives up at
    _MAX_SCROLL_ATTEMPTS regardless, since some calendars have an
    effectively unbounded number of future events to keep loading.
    """
    snapshots = []
    previous_height = page.evaluate("document.body.scrollHeight")
    viewport_height = page.evaluate("window.innerHeight")
    if previous_height <= viewport_height:
        return snapshots
    previous_html = page.content()
    for _ in range(_MAX_SCROLL_ATTEMPTS):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(_SCROLL_WAIT_MS)
        html = page.content()
        content_changed = html != previous_html
        if content_changed:
            snapshots.append(html)
        previous_html = html
        current_height = page.evaluate("document.body.scrollHeight")
        at_bottom = page.evaluate(_AT_BOTTOM_SCRIPT)
        grew = current_height > previous_height
        previous_height = current_height
        if content_changed and _is_beyond_future_cutoff(html):
            break
        if at_bottom and not grew:
            break
    return snapshots


def _click_load_more_and_collect_snapshots(page) -> list[str]:
    """Clicks a "Load more"/"Show more events"-style control repeatedly,
    capturing an HTML snapshot each time it actually reveals new content.

    Runs after scrolling has stalled, for lists that gate the rest of their
    events behind an explicit button rather than infinite scroll. Only a
    control whose visible label matches _LOAD_MORE_TEXT_PATTERN is clicked
    (see that constant for why this is kept deliberately narrow). Stops as
    soon as no matching control is found, a click reveals nothing new, a
    click throws (the element detached or got covered), a step's content is
    entirely beyond _MAX_FUTURE_DAYS out, or after _MAX_LOAD_MORE_CLICKS.
    """
    snapshots = []
    previous_html = page.content()
    for _ in range(_MAX_LOAD_MORE_CLICKS):
        try:
            clicked = page.evaluate(_LOAD_MORE_CLICK_SCRIPT)
        except Exception:
            break
        if not clicked:
            break
        html = _wait_for_content_to_settle(page)
        if html == previous_html:
            break
        snapshots.append(html)
        previous_html = html
        if _is_beyond_future_cutoff(html):
            break
    return snapshots


def _click_next_month_and_collect_snapshots(page) -> list[str]:
    """Clicks a calendar's "next month" button repeatedly, capturing an HTML
    snapshot each time it actually advances to new content.

    Skips entirely unless one of _NEXT_MONTH_SELECTORS is present, trying
    them in order and using the first match - a small allowlist of known
    calendar widgets, not a generic "find and click arrows" heuristic (same
    spirit as _page_urls_to_follow only recognizing one URL pagination
    convention rather than guessing at others). The stop signal is generic
    on purpose, since these widgets share no common "current month" label:
    if a click leaves the settled HTML unchanged, nothing advanced, so
    stop. Also stops once a step's content is entirely beyond
    _MAX_FUTURE_DAYS out (see _is_beyond_future_cutoff - same reasoning as
    _scroll_and_collect_snapshots), or after _MAX_MONTH_CLICKS regardless.
    """
    selector = next(
        (
            candidate
            for candidate in _NEXT_MONTH_SELECTORS
            if page.evaluate(f"!!document.querySelector({candidate!r})")
        ),
        None,
    )
    if selector is None:
        return []

    snapshots = []
    previous_html = page.content()
    for _ in range(_MAX_MONTH_CLICKS):
        page.evaluate(f"document.querySelector({selector!r}).click()")
        html = _wait_for_content_to_settle(page)
        if html == previous_html:
            break
        snapshots.append(html)
        previous_html = html
        if _is_beyond_future_cutoff(html):
            break
    return snapshots


def _page_urls_to_follow(url: str) -> list[str]:
    """Returns [url] plus up to _MAX_ADDITIONAL_PAGES follow-up URLs with an
    incremented "page" query parameter, if `url` has one to increment.

    Deliberately narrow: only recognizes a literal "page" query parameter
    with a numeric value (e.g. "...?page=1"), the one pagination convention
    actually confirmed in the wild so far (Eventbrite). Sites that paginate
    via other URL schemes (cursor-based URLs, a path segment like
    "/page/2/", etc.) aren't detected - those still need a URL added
    manually per page. In-page "Load More" buttons are handled separately
    (see _click_load_more_and_collect_snapshots).
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


def _pages_are_near_duplicate(lines_a: set[str], lines_b: set[str]) -> bool:
    """True if two pages' reduced text lines are effectively the same content.

    Not a byte-for-byte comparison: a page counts as a duplicate once
    _DUPLICATE_PAGE_OVERLAP_RATIO of *either* page's non-blank lines already
    appear on the other, i.e. one is a near-subset of the other. That catches
    both the exact-repeat case (an out-of-range "?page=N" served page 1's
    listing again) and the case where the repeat picks up a little extra
    per-page chrome, without treating two genuinely distinct pages of a
    listing as the same.
    """
    if not lines_a or not lines_b:
        return lines_a == lines_b
    overlap = len(lines_a & lines_b)
    return (
        overlap >= _DUPLICATE_PAGE_OVERLAP_RATIO * len(lines_a)
        or overlap >= _DUPLICATE_PAGE_OVERLAP_RATIO * len(lines_b)
    )


def _prune_subsumed_snapshots(snapshots: list[str]) -> list[str]:
    """Drops snapshots whose text is already covered by the ones kept, so a
    grow-only page isn't sent to the LLM once per scroll step.

    Every snapshot is the full DOM at some moment, so on a page that only
    ever grows, the last snapshot is a superset of all earlier ones and
    resending them wastes tokens. Walking newest-first and keeping a
    snapshot only when it contributes meaningfully many lines not already
    seen collapses such a page to (roughly) its final snapshot, while a
    virtualized list's earlier snapshots - which hold events the final
    snapshot has since unmounted - do contribute new lines and survive.

    Direction is load-bearing: newest-first is what keeps the fullest
    snapshot of a grow-only page and discards its earlier subsets. This
    prunes whole snapshots only; it never dedupes individual lines, because
    a line like "6:00 PM" legitimately recurs across different events and
    dropping the repeats would corrupt them.
    """
    kept_lines: set[str] = set()
    kept_indices: list[int] = []
    for index in reversed(range(len(snapshots))):
        lines = _non_blank_lines(snapshots[index])
        if not lines:
            continue
        new_lines = lines - kept_lines
        if (
            len(new_lines) > _PRUNE_MIN_NEW_LINES
            or len(new_lines) > _PRUNE_MIN_NEW_LINE_RATIO * len(lines)
        ):
            kept_indices.append(index)
            kept_lines |= lines
    kept_indices.sort()
    return [snapshots[index] for index in kept_indices]


def _load_and_capture(page, url: str) -> list[str]:
    """Navigates to `url` and returns its settled HTML plus any additional
    content pulled in by embedded frames, scrolling, "load more" clicks, or
    month-by-month calendar navigation, as a list of per-step snapshots (see
    _wait_for_content_to_settle, _capture_child_frames,
    _scroll_and_collect_snapshots, _click_load_more_and_collect_snapshots,
    and _click_next_month_and_collect_snapshots)."""
    page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
    snapshots = [_wait_for_content_to_settle(page)]
    snapshots += _capture_child_frames(page)
    snapshots += _scroll_and_collect_snapshots(page)
    snapshots += _click_load_more_and_collect_snapshots(page)
    snapshots += _click_next_month_and_collect_snapshots(page)
    return snapshots


def _load_and_capture_with_retry(page, url: str) -> list[str]:
    """Runs _load_and_capture, retrying it once after any failure.

    A first navigation can fail transiently (see _RETRY_WAIT_MS). A single
    retry after a short pause clears most of those; if the retry fails too,
    its exception propagates and fetch_rendered_html turns it into a
    RuntimeError, exactly as an un-retried failure would have.
    """
    try:
        return _load_and_capture(page, url)
    except Exception:
        page.wait_for_timeout(_RETRY_WAIT_MS)
        return _load_and_capture(page, url)


def fetch_rendered_html(url: str) -> str:
    """Loads a URL (and, if paginated, its numbered follow-up pages) in
    Chromium and returns the combined rendered HTML.

    Runs Chromium non-headless with a realistic user agent, since some
    sites (e.g. those behind Cloudflare) detect and block headless
    automation outright. See _load_and_capture for how a single page is
    loaded and settled (including embedded frames, scrolling, "load more"
    clicks, and calendar month navigation), and _page_urls_to_follow for how
    additional numbered pages (e.g. Eventbrite's "?page=1", "?page=2", ...)
    are detected and queued up alongside it. Numbered pagination stops early
    once a page repeats content already fetched (see
    _pages_are_near_duplicate), and the collected snapshots are pruned of
    ones subsumed by others (see _prune_subsumed_snapshots) before joining,
    to avoid resending near-identical DOM states to the LLM.

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
            snapshots: list[str] = []
            previous_page_lines: set[str] | None = None
            for page_url in _page_urls_to_follow(url):
                page_snapshots = _load_and_capture_with_retry(page, page_url)
                snapshots += page_snapshots
                page_lines = _non_blank_lines("\n".join(page_snapshots))
                if previous_page_lines is not None and _pages_are_near_duplicate(
                    page_lines, previous_page_lines
                ):
                    break
                previous_page_lines = page_lines
            browser.close()
            return "\n".join(_prune_subsumed_snapshots(snapshots))
    except Exception as error:
        raise RuntimeError(f"Failed to load page with Playwright: {error}") from error
