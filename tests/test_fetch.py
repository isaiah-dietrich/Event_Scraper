import datetime

import pytest

import scrape.fetch as fetch_module
from scrape.fetch import fetch_rendered_html


class FakePage:
    def __init__(
        self,
        html="<html>rendered</html>",
        goto_error=None,
        content_sequence=None,
        content_by_url=None,
        scroll_heights=None,
        viewport_height=800,
    ):
        """content_sequence, if given, is returned one entry per content()
        call (holding on the last entry once exhausted) instead of the
        fixed `html` value - used to simulate a page whose markup changes
        across polls before settling, or one that never stops changing.

        content_by_url, if given, maps a goto() URL to the html content()
        should return while "on" that page - used to simulate navigating
        across multiple distinct pages (e.g. pagination), where each page
        settles to its own fixed content rather than changing over time.

        scroll_heights, if given, is returned one entry per
        "document.body.scrollHeight" evaluate() call (holding on the last
        entry once exhausted) - used to simulate a page that grows as it's
        scrolled, or one that's already fully loaded (constant height).
        Defaults to a constant 1000, comfortably above the default
        viewport_height so scrolling is attempted unless a test says
        otherwise.
        """
        self._html = html
        self._goto_error = goto_error
        self._content_sequence = list(content_sequence) if content_sequence is not None else None
        self._content_by_url = content_by_url
        self._scroll_heights = list(scroll_heights) if scroll_heights is not None else None
        self._viewport_height = viewport_height
        self.goto_calls = []
        self.wait_for_timeout_calls = []
        self.evaluate_calls = []
        self.content_calls = 0
        self._scroll_height_query_count = 0
        self._current_url = None

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        self._current_url = url
        if self._goto_error:
            raise self._goto_error

    def wait_for_timeout(self, ms):
        self.wait_for_timeout_calls.append(ms)

    def content(self):
        self.content_calls += 1
        if self._content_by_url is not None:
            return self._content_by_url.get(self._current_url, self._html)
        if self._content_sequence is not None:
            index = min(self.content_calls - 1, len(self._content_sequence) - 1)
            return self._content_sequence[index]
        return self._html

    def evaluate(self, script):
        self.evaluate_calls.append(script)
        if script == "document.body.scrollHeight":
            if self._scroll_heights is None:
                return 1000  # constant: nothing more to load by default
            index = min(self._scroll_height_query_count, len(self._scroll_heights) - 1)
            self._scroll_height_query_count += 1
            return self._scroll_heights[index]
        if script == "window.innerHeight":
            return self._viewport_height
        return None


def _scroll_to_call_count(page) -> int:
    return sum(1 for call in page.evaluate_calls if call.startswith("window.scrollTo"))


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


# --- fetch_rendered_html: integration-level behavior -----------------------


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


def test_navigates_with_expected_url_and_timeout(monkeypatch):
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


def test_follows_numbered_pagination_and_combines_results(monkeypatch):
    # A "?page=1" URL should pull in every follow-up page too, all
    # navigated in the same browser tab and combined into one result.
    base = "https://example.com/events?page="
    page = FakePage(content_by_url={f"{base}{n}": f"<html>p{n}</html>" for n in range(1, 6)})
    _install_fake_playwright(monkeypatch, page)

    result = fetch_rendered_html(f"{base}1")

    assert [call["url"] for call in page.goto_calls] == [f"{base}{n}" for n in range(1, 6)]
    for n in range(1, 6):
        assert f"<html>p{n}</html>" in result


def test_does_not_paginate_urls_without_a_page_parameter(monkeypatch):
    page = FakePage()
    _install_fake_playwright(monkeypatch, page)

    fetch_rendered_html("https://example.com/events")

    assert len(page.goto_calls) == 1


def test_scrolls_to_load_more_content_before_returning(monkeypatch):
    # A page that grows once when scrolled: proves the scroll step is
    # actually wired into the full fetch flow (and its snapshot combined
    # into the final result), not just unit-testable in isolation.
    page = FakePage(scroll_heights=[1000, 1500, 1500])
    _install_fake_playwright(monkeypatch, page)

    result = fetch_rendered_html("https://example.com")

    assert _scroll_to_call_count(page) >= 1
    # One snapshot from the initial settle, plus one appended for the
    # single growth step.
    assert result.count("<html>rendered</html>") == 2


def test_no_extra_snapshot_when_scrolling_finds_nothing_new(monkeypatch):
    # Default FakePage has a constant scroll height (nothing more to load),
    # so the result shouldn't be needlessly duplicated with an identical
    # scroll-step snapshot.
    page = FakePage()
    _install_fake_playwright(monkeypatch, page)

    result = fetch_rendered_html("https://example.com")

    assert result == "<html>rendered</html>"


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


# --- _wait_for_content_to_settle (unit-level) ------------------------------


def test_settle_confirms_stability_before_returning_static_content():
    page = FakePage()

    result = fetch_module._wait_for_content_to_settle(page)

    assert result == "<html>rendered</html>"
    # One minimum wait, then two poll intervals to confirm two consecutive
    # identical reads.
    assert page.wait_for_timeout_calls == [
        fetch_module._MIN_SETTLE_WAIT_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
    ]


def test_settle_waits_out_content_that_is_still_changing():
    # Simulates a page whose event list is still loading in: the markup
    # changes once before it settles, so settling must not stop at the
    # first read - it needs _SETTLE_STABLE_CHECKS consecutive matches.
    page = FakePage(content_sequence=[
        "<html>loading</html>",
        "<html>loaded</html>",
        "<html>loaded</html>",
        "<html>loaded</html>",
    ])

    result = fetch_module._wait_for_content_to_settle(page)

    assert result == "<html>loaded</html>"
    assert page.wait_for_timeout_calls == [
        fetch_module._MIN_SETTLE_WAIT_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
        fetch_module._SETTLE_POLL_INTERVAL_MS,
    ]


def test_settle_gives_up_at_max_wait_for_content_that_never_stabilizes():
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

    result = fetch_module._wait_for_content_to_settle(page)

    expected_polls = (
        fetch_module._MAX_SETTLE_WAIT_MS - fetch_module._MIN_SETTLE_WAIT_MS
    ) // fetch_module._SETTLE_POLL_INTERVAL_MS
    assert len(page.wait_for_timeout_calls) == 1 + expected_polls
    assert result == f"<html>v{page.content_calls}</html>"


# --- _scroll_and_collect_snapshots (unit-level) ----------------------------


def test_scroll_captures_one_snapshot_per_growth_step_then_stops():
    # Height grows once (1000 -> 1500), then plateaus - should scroll twice
    # (the second scroll is what confirms nothing more loaded) but only
    # capture one snapshot, for the step that actually grew.
    page = FakePage(scroll_heights=[1000, 1500, 1500])

    snapshots = fetch_module._scroll_and_collect_snapshots(page)

    assert _scroll_to_call_count(page) == 2
    assert page.wait_for_timeout_calls == [fetch_module._SCROLL_WAIT_MS] * 2
    assert snapshots == ["<html>rendered</html>"]


def test_scroll_captures_nothing_when_height_never_grows():
    # Page is already fully loaded - a single scroll attempt confirms
    # nothing more to load, and it should not keep trying or capture a
    # redundant snapshot.
    page = FakePage(scroll_heights=[1000, 1000])

    snapshots = fetch_module._scroll_and_collect_snapshots(page)

    assert _scroll_to_call_count(page) == 1
    assert page.wait_for_timeout_calls == [fetch_module._SCROLL_WAIT_MS]
    assert snapshots == []


def test_scroll_gives_up_after_max_attempts_when_always_growing():
    # A calendar with an effectively unbounded number of future events -
    # every scroll loads more, so it must not scroll forever.
    class AlwaysGrowingPage(FakePage):
        def __init__(self):
            super().__init__()
            self._height = 1000

        def evaluate(self, script):
            self.evaluate_calls.append(script)
            if script == "document.body.scrollHeight":
                self._height += 500
                return self._height
            if script == "window.innerHeight":
                return self._viewport_height
            return None

    page = AlwaysGrowingPage()

    snapshots = fetch_module._scroll_and_collect_snapshots(page)

    assert _scroll_to_call_count(page) == fetch_module._MAX_SCROLL_ATTEMPTS
    assert len(snapshots) == fetch_module._MAX_SCROLL_ATTEMPTS


def test_scroll_skips_entirely_when_page_fits_in_viewport():
    # Page is shorter than the viewport - there's nothing below the fold,
    # so scrolling can't possibly reveal more content. Should not even
    # attempt a scroll.
    page = FakePage(scroll_heights=[500], viewport_height=800)

    snapshots = fetch_module._scroll_and_collect_snapshots(page)

    assert _scroll_to_call_count(page) == 0
    assert page.wait_for_timeout_calls == []
    assert snapshots == []


def test_scroll_stops_but_keeps_snapshot_once_beyond_future_cutoff():
    # This pipeline runs weekly, so scrolling deep into the future isn't
    # worth it - once a step's content is entirely beyond _MAX_FUTURE_DAYS
    # out, stop scrolling *further*. The snapshot that crossed the cutoff
    # is still kept, though - it's fine to have if we already found it,
    # just not worth deliberately scrolling more to reach.
    far_future = datetime.date.today() + datetime.timedelta(days=200)
    far_future_html = f"<html><body>Event on {far_future:%B %d, %Y}</body></html>"
    # Height keeps growing on every step, so without the cutoff check this
    # would keep scrolling well past _scroll_to_call_count == 1.
    page = FakePage(scroll_heights=[1000, 1200, 1600, 2000], content_sequence=[far_future_html])

    snapshots = fetch_module._scroll_and_collect_snapshots(page)

    assert _scroll_to_call_count(page) == 1
    assert snapshots == [far_future_html]


def test_scroll_continues_normally_when_content_is_within_future_cutoff():
    # A near-future date shouldn't trip the cutoff check - normal
    # grow-then-plateau behavior should proceed as before.
    near_future = datetime.date.today() + datetime.timedelta(days=10)
    near_future_html = f"<html><body>Event on {near_future:%B %d, %Y}</body></html>"
    page = FakePage(scroll_heights=[1000, 1200, 1200], content_sequence=[near_future_html])

    snapshots = fetch_module._scroll_and_collect_snapshots(page)

    assert _scroll_to_call_count(page) == 2
    assert snapshots == [near_future_html]


# --- _is_beyond_future_cutoff (unit-level) ---------------------------------


def test_is_beyond_future_cutoff_true_for_only_far_future_dates():
    far_future = datetime.date.today() + datetime.timedelta(days=200)
    html = f"<html><body>Event on {far_future:%B %d, %Y}</body></html>"

    assert fetch_module._is_beyond_future_cutoff(html) is True


def test_is_beyond_future_cutoff_false_for_near_future_dates():
    near_future = datetime.date.today() + datetime.timedelta(days=10)
    html = f"<html><body>Event on {near_future:%B %d, %Y}</body></html>"

    assert fetch_module._is_beyond_future_cutoff(html) is False


def test_is_beyond_future_cutoff_false_when_no_dates_found():
    assert fetch_module._is_beyond_future_cutoff("<html><body>No dates here</body></html>") is False


def test_is_beyond_future_cutoff_false_for_mixed_near_and_far_dates():
    # A single near-term date mixed in means we haven't scrolled entirely
    # past the useful window yet.
    near_future = datetime.date.today() + datetime.timedelta(days=10)
    far_future = datetime.date.today() + datetime.timedelta(days=200)
    html = (
        f"<html><body>Event on {near_future:%B %d, %Y} and another on "
        f"{far_future:%B %d, %Y}</body></html>"
    )

    assert fetch_module._is_beyond_future_cutoff(html) is False


# --- _page_urls_to_follow (unit-level) -------------------------------------


def test_page_urls_to_follow_increments_page_parameter():
    urls = fetch_module._page_urls_to_follow("https://example.com/events?page=1")

    assert urls == [
        "https://example.com/events?page=1",
        "https://example.com/events?page=2",
        "https://example.com/events?page=3",
        "https://example.com/events?page=4",
        "https://example.com/events?page=5",
    ]


def test_page_urls_to_follow_starts_from_the_given_page_number():
    urls = fetch_module._page_urls_to_follow("https://example.com/events?page=3")

    assert urls == [
        "https://example.com/events?page=3",
        "https://example.com/events?page=4",
        "https://example.com/events?page=5",
        "https://example.com/events?page=6",
        "https://example.com/events?page=7",
    ]


def test_page_urls_to_follow_preserves_other_query_parameters():
    urls = fetch_module._page_urls_to_follow("https://example.com/events?foo=bar&page=1")

    assert urls[1] == "https://example.com/events?foo=bar&page=2"


def test_page_urls_to_follow_unchanged_when_no_page_parameter():
    urls = fetch_module._page_urls_to_follow("https://example.com/events?category=tech")

    assert urls == ["https://example.com/events?category=tech"]


def test_page_urls_to_follow_unchanged_for_non_numeric_page_value():
    urls = fetch_module._page_urls_to_follow("https://example.com/events?page=abc")

    assert urls == ["https://example.com/events?page=abc"]


def test_page_urls_to_follow_unchanged_for_page_in_path_not_query():
    # "page" appearing as a path segment (not a query parameter) shouldn't
    # be treated as pagination.
    urls = fetch_module._page_urls_to_follow("https://example.com/page/1")

    assert urls == ["https://example.com/page/1"]
