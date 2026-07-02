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


def fetch_rendered_html(url: str) -> str:
    """Loads a URL in Chromium and returns the fully rendered HTML.

    Runs Chromium non-headless with a realistic user agent, since some
    sites (e.g. those behind Cloudflare) detect and block headless
    automation outright. See _wait_for_content_to_settle for how it decides
    the page is done rendering before capturing HTML.

    Args:
        url: The page to load.

    Returns:
        The page's rendered HTML as a string.

    Raises:
        RuntimeError: If the page fails to load for any reason.
    """
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            page = browser.new_page(user_agent=_USER_AGENT, viewport=_VIEWPORT)
            page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            html = _wait_for_content_to_settle(page)
            browser.close()
            return html
    except Exception as error:
        raise RuntimeError(f"Failed to load page with Playwright: {error}") from error
