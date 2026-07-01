import pytest

import scrape.fetch as fetch_module
from scrape.fetch import fetch_rendered_html


class FakePage:
    def __init__(self, html="<html>rendered</html>", goto_error=None):
        self._html = html
        self._goto_error = goto_error
        self.goto_calls = []
        self.wait_for_timeout_calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self._goto_error:
            raise self._goto_error

    def wait_for_timeout(self, ms):
        self.wait_for_timeout_calls.append(ms)

    def content(self):
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
    assert page.wait_for_timeout_calls == [fetch_module._SETTLE_WAIT_MS]


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
