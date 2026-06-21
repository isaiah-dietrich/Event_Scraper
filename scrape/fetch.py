"""Fetch stage: render a JS-loaded page in Chromium and return its HTML."""

from playwright.sync_api import sync_playwright

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1366, "height": 768}
_GOTO_TIMEOUT_MS = 30_000
_SETTLE_WAIT_MS = 8_000


def fetch_rendered_html(url: str) -> str:
    """Loads a URL in Chromium and returns the fully rendered HTML.

    Runs Chromium non-headless with a realistic user agent, since some
    sites (e.g. those behind Cloudflare) detect and block headless
    automation outright. Waits a fixed duration after navigation instead
    of relying on network-idle, since some pages never go fully idle
    (ad pixels, trackers, polling widgets).

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
            page.wait_for_timeout(_SETTLE_WAIT_MS)
            html = page.content()
            browser.close()
            return html
    except Exception as error:
        raise RuntimeError(f"Failed to load page with Playwright: {error}") from error
