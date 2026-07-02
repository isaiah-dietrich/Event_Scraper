"""Fetch stage: render a JS-loaded page in Chromium and return its HTML."""

from playwright.sync_api import sync_playwright

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

    Stops once a scroll doesn't grow the page (nothing more to load),
    rather than always running the full _MAX_SCROLL_ATTEMPTS - but gives up
    at that cap regardless, since some calendars have an effectively
    unbounded number of future events to keep loading.
    """
    snapshots = []
    previous_height = page.evaluate("document.body.scrollHeight")
    for _ in range(_MAX_SCROLL_ATTEMPTS):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(_SCROLL_WAIT_MS)
        current_height = page.evaluate("document.body.scrollHeight")
        if current_height <= previous_height:
            break
        snapshots.append(page.content())
        previous_height = current_height
    return snapshots


def fetch_rendered_html(url: str) -> str:
    """Loads a URL in Chromium and returns its rendered HTML.

    Runs Chromium non-headless with a realistic user agent, since some
    sites (e.g. those behind Cloudflare) detect and block headless
    automation outright. See _wait_for_content_to_settle for how it decides
    the initial page is done rendering, and _scroll_and_collect_snapshots
    for how additional lazy/infinite-scroll content (including from
    virtualized lists that unmount earlier items) is pulled in and combined
    with it.

    Args:
        url: The page to load.

    Returns:
        The initial settled HTML, plus one snapshot per scroll step that
        loaded new content, joined together as a single string.

    Raises:
        RuntimeError: If the page fails to load for any reason.
    """
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            page = browser.new_page(user_agent=_USER_AGENT, viewport=_VIEWPORT)
            page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            first_snapshot = _wait_for_content_to_settle(page)
            more_snapshots = _scroll_and_collect_snapshots(page)
            browser.close()
            return "\n".join([first_snapshot] + more_snapshots)
    except Exception as error:
        raise RuntimeError(f"Failed to load page with Playwright: {error}") from error
