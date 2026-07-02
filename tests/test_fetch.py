import pytest

import scrape.fetch as fetch_module
from scrape.fetch import fetch_rendered_html


class FakePage:
    def __init__(self, html="<html>rendered</html>", goto_error=None, content_sequence=None):
        """content_sequence, if given, is returned one entry per content()
        call (holding on the last entry once exhausted) instead of the
        fixed `html` value - used to simulate a page whose markup changes
        across polls before settling, or one that never stops changing.
        """
        self._html = html
        self._goto_error = goto_error
        self._content_sequence = list(content_sequence) if content_sequence is not None else None
        self.goto_calls = []
        self.wait_for_timeout_calls = []
        self.content_calls = 0

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self._goto_error:
            raise self._goto_error

    def wait_for_timeout(self, ms):
        self.wait_for_timeout_calls.append(ms)

    def content(self):
        self.content_calls += 1
        if self._content_sequence is not None:
            index = min(self.content_calls - 1, len(self._content_sequence) - 1)
            return self._content_sequence[index]
        return self._html


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.new_page_calls = []
        self.closed = False

    def new_page(self, user_agent=None, viewport=None):
        self.new_page_calls.append({"user_agent": user_agent, "viewport": viewport})
        return self._page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser
        self.launch_calls = []

    def launch(self, headless=None):
        self.launch_calls.append(headless)
        return self._browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class FakeSyncPlaywrightContext:
    def __init__(self, playwright):
        self._playwright = playwright

    def __enter__(self):
        return self._playwright

    def __exit__(self, *exc_info):
        return False


def _install_fake_playwright(monkeypatch, page):
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(
        fetch_module, "sync_playwright", lambda: FakeSyncPlaywrightContext(playwright)
    )
    return chromium, browser


def test_returns_rendered_html(monkeypatch):
    page = FakePage(html="<html>hello</html>")
    _install_fake_playwright(monkeypatch, page)

    result = fetch_rendered_html("https://example.com")

    assert result == "<html>hello</html>"


def test_launches_chromium_non_headless(monkeypatch):
    page = FakePage()
    chromium, _ = _install_fake_playwright(monkeypatch, page)

    fetch_rendered_html("https://example.com")

    assert chromium.launch_calls == [False]


def test_navigates_with_expected_wait_and_timeout(monkeypatch):
    page = FakePage()
    _install_fake_playwright(monkeypatch, page)

    fetch_rendered_html("https://example.com/page")

    assert page.goto_calls == [
        {
            "url": "https://example.com/page",
            "wait_until": "domcontentloaded",
            "timeout": fetch_module._GOTO_TIMEOUT_MS,
        }
    ]
    # Static content settles as soon as two consecutive polls match: one
    # minimum wait, then two poll intervals to confirm stability.
    assert page.wait_for_timeout_calls == [
        fetch_module._MIN_SETTLE_WAIT_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
    ]


def test_settling_waits_out_content_that_is_still_changing(monkeypatch):
    # Simulates a page whose event list is still loading in: the markup
    # changes once before it settles, so settling must not stop at the
    # first read - it needs _SETTLE_STABLE_CHECKS consecutive matches.
    page = FakePage(content_sequence=[
        "<html>loading</html>",
        "<html>loaded</html>",
        "<html>loaded</html>",
        "<html>loaded</html>",
    ])
    _install_fake_playwright(monkeypatch, page)

    result = fetch_rendered_html("https://example.com")

    assert result == "<html>loaded</html>"
    assert page.wait_for_timeout_calls == [
        fetch_module._MIN_SETTLE_WAIT_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
    ]


def test_settling_gives_up_at_max_wait_for_content_that_never_stabilizes(monkeypatch):
    # A page that never stops changing (ad pixels, trackers, polling
    # widgets) must not hang forever - it should be cut off at
    # _MAX_SETTLE_WAIT_MS and return whatever the last read was.
    def _ever_changing_html():
        counter = 0
        while True:
            counter += 1
            yield f"<html>v{counter}</html>"

    generator = _ever_changing_html()

    class AlwaysChangingPage(FakePage):
        def content(self):
            self.content_calls += 1
            return next(generator)

    page = AlwaysChangingPage()
    _install_fake_playwright(monkeypatch, page)

    result = fetch_rendered_html("https://example.com")

    expected_polls = (
        fetch_module._MAX_SETTLE_WAIT_MS - fetch_module._MIN_SETTLE_WAIT_MS
    ) // fetch_module._SETTLE_POLL_INTERVAL_MS
    assert len(page.wait_for_timeout_calls) == 1 + expected_polls
    assert result == f"<html>v{page.content_calls}</html>"


def test_closes_browser_after_success(monkeypatch):
    page = FakePage()
    _, browser = _install_fake_playwright(monkeypatch, page)

    fetch_rendered_html("https://example.com")

    assert browser.closed is True


def test_wraps_navigation_failure_in_runtime_error(monkeypatch):
    page = FakePage(goto_error=TimeoutError("navigation timed out"))
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(RuntimeError, match="Failed to load page with Playwright"):
        fetch_rendered_html("https://example.com")


def test_runtime_error_chains_original_exception(monkeypatch):
    original = TimeoutError("navigation timed out")
    page = FakePage(goto_error=original)
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(RuntimeError) as excinfo:
        fetch_rendered_html("https://example.com")

    assert excinfo.value.__cause__ is original
