"""Fetch stage: retrieve a page's fully-rendered content as markdown via Firecrawl."""

import collections
import os
import threading
import time
import urllib.parse

from firecrawl import Firecrawl

# Firecrawl renders the page server-side (handling JS, embedded iframe
# calendars like GrowthZone/Localist, and anti-bot evasion) and returns
# clean markdown directly - confirmed live against both currently-active
# SITE_URLs, including the TAG Online site whose event calendar lives
# entirely inside an iframe. only_main_content strips nav/footer/social
# chrome the way the old reduce_html's href-worthiness heuristics used to.
_ONLY_MAIN_CONTENT = True

_client: Firecrawl | None = None


def _get_client() -> Firecrawl:
    """Lazily constructs the Firecrawl client from FIRECRAWL_API_KEY.

    Lazy (not module-import-time) so importing this module never requires
    the env var to be set, matching how utility.email_digest reads its own
    credentials only at send time. cli.batch.main checks FIRECRAWL_API_KEY
    itself before any site is processed, so in practice this only ever
    raises if that check was bypassed.
    """
    global _client
    if _client is None:
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            raise RuntimeError("FIRECRAWL_API_KEY environment variable is not set.")
        _client = Firecrawl(api_key=api_key)
    return _client


# The Firecrawl account's plan currently allows this many scrape jobs to run
# concurrently. cli.batch's MAX_WORKERS governs how many sites' pipelines run
# at once and is no longer bottlenecked by "one visible Chromium window per
# worker" - so this semaphore is what actually keeps concurrent scrape calls
# within the plan's limit, independent of MAX_WORKERS.
MAX_CONCURRENT_SCRAPES = 2
_scrape_semaphore = threading.Semaphore(MAX_CONCURRENT_SCRAPES)

# The plan's rate limit is per-minute, not just per-concurrent-job - a live
# run hit "Rate Limit Exceeded ... Consumed (req/min): 11-12, Remaining: 0"
# even with only MAX_CONCURRENT_SCRAPES=2 calls ever in flight at once,
# because each scrape finishes in a few seconds and the semaphore lets the
# next one start right away. A site with numbered-page pagination (see
# _MAX_ADDITIONAL_PAGES below) can alone burst several calls back-to-back.
# Kept conservatively under the observed ~10-11/min cap to leave headroom for
# the account's other Firecrawl usage outside this job.
MAX_SCRAPES_PER_MINUTE = 8


class _RateLimiter:
    """Blocks callers so no more than `max_per_minute` calls start within any
    rolling 60-second window, across all threads.

    A plain call-count limiter, not a smart backoff: it paces call *starts*
    evenly rather than reacting to 429s, which is enough to stay under a
    fixed per-minute cap regardless of how fast individual scrapes complete.
    """

    def __init__(self, max_per_minute: int):
        self._max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._call_times: collections.deque[float] = collections.deque()

    def wait_for_slot(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._call_times and now - self._call_times[0] >= 60:
                    self._call_times.popleft()
                if len(self._call_times) < self._max_per_minute:
                    self._call_times.append(now)
                    return
                sleep_time = 60 - (now - self._call_times[0])
            time.sleep(sleep_time)


_rate_limiter = _RateLimiter(MAX_SCRAPES_PER_MINUTE)


# Hard ceiling on Firecrawl credits one run may spend, across every site and
# every stage. One scrape = one credit, so this is a credit budget in all but
# name. The account's plan allows 1000 credits/month against ~4-5 weekly runs,
# and the description-enrichment stage (see scrape.enrich) makes per-event
# scrapes whose count scales with how many events the week turned up - i.e.
# the one part of the pipeline whose cost isn't bounded by len(SITE_URLS).
# This is the backstop that keeps an unusually large week from eating the
# month; scrape.enrich.MAX_ENRICHMENT_SCRAPES is the tighter, everyday limit
# that normally binds first.
MAX_SCRAPES_PER_RUN = 200


class ScrapeBudgetExceeded(RuntimeError):
    """Raised when a run has spent its MAX_SCRAPES_PER_RUN Firecrawl credits.

    Subclasses RuntimeError so it flows through the same handling as any
    other fetch failure: fetch_page_markdown's callers (cli.batch.
    _process_site_once) already catch RuntimeError and turn it into a
    "failed: ..." status row instead of aborting the batch.
    """


class _ScrapeBudget:
    """Thread-safe count of scrapes spent this run, capped at `limit`.

    Shared across every worker thread, since the cap is per-run (per billing
    period) rather than per-site.
    """

    def __init__(self, limit: int):
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> None:
        """Claims one scrape's worth of budget, or raises if none is left."""
        with self._lock:
            if self._used >= self._limit:
                raise ScrapeBudgetExceeded(
                    f"run-wide Firecrawl budget of {self._limit} scrape(s) is "
                    "exhausted; skipping this scrape"
                )
            self._used += 1

    def remaining(self) -> int:
        with self._lock:
            return max(0, self._limit - self._used)

    def used(self) -> int:
        with self._lock:
            return self._used


_budget = _ScrapeBudget(MAX_SCRAPES_PER_RUN)


def remaining_scrape_budget() -> int:
    """Scrapes this run may still make before MAX_SCRAPES_PER_RUN is hit.

    Lets an optional, cost-scaling stage (see scrape.enrich) size its own
    work to what's actually left rather than starting scrapes that would
    raise ScrapeBudgetExceeded partway through.
    """
    return _budget.remaining()


def scrapes_used() -> int:
    """Scrapes made so far this run - reported at the end of a run so credit
    consumption is visible instead of silent."""
    return _budget.used()

# Numbered-page pagination (e.g. Eventbrite's "?page=1"): follow up to this
# many pages beyond the one given, in addition to whatever Firecrawl's single
# scrape of each page reveals. Ported as-is from the Playwright-era fetch -
# Firecrawl scrapes exactly the URL it's given and doesn't discover
# pagination on its own for this convention. No currently-active SITE_URL
# needs this, but the commented-out Eventbrite entry does.
_MAX_ADDITIONAL_PAGES = 4
# Two pages count as the same content once this fraction of one's non-blank
# lines already appeared on the other - a near-subset, not just a
# byte-for-byte match, to tolerate incidental per-page chrome differences.
_DUPLICATE_PAGE_OVERLAP_RATIO = 0.95


def _page_urls_to_follow(url: str) -> list[str]:
    """Returns [url] plus up to _MAX_ADDITIONAL_PAGES follow-up URLs with an
    incremented "page" query parameter, if `url` has one to increment.

    Deliberately narrow: only recognizes a literal "page" query parameter
    with a numeric value (e.g. "...?page=1"), the one pagination convention
    actually confirmed in the wild so far (Eventbrite). Sites that paginate
    via other URL schemes (cursor-based URLs, a path segment like
    "/page/2/", etc.) aren't detected - those still need a URL added
    manually per page.
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


def _non_blank_lines(text: str) -> set[str]:
    """The set of non-blank lines in `text` - the unit the duplicate-page
    check compares on."""
    return {line for line in text.splitlines() if line.strip()}


def _pages_are_near_duplicate(lines_a: set[str], lines_b: set[str]) -> bool:
    """True if two pages' markdown lines are effectively the same content.

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


def _scrape_one_page(url: str) -> str:
    """Scrapes a single URL via Firecrawl and returns its markdown content.

    Three independent limits apply, each covering something the others
    don't: the budget caps total credits spent per run (see
    MAX_SCRAPES_PER_RUN), the semaphore bounds how many scrape calls are in
    flight at once across all concurrently-processing sites (see
    MAX_CONCURRENT_SCRAPES), and the rate limiter bounds how many calls may
    *start* per minute (see MAX_SCRAPES_PER_MINUTE) - concurrency alone
    doesn't prevent bursting past a per-minute cap when individual calls
    complete quickly. The budget is claimed before the rate limiter so an
    over-budget call fails immediately instead of sleeping out a rate-limit
    wait first. The SDK itself already retries transient failures (network
    errors, 5xx) before raising, so no retry loop is needed here the way the
    old Playwright fetch needed one.

    Raises:
        ScrapeBudgetExceeded: If this run has already used its full budget.
    """
    client = _get_client()
    _budget.consume()
    _rate_limiter.wait_for_slot()
    with _scrape_semaphore:
        try:
            document = client.scrape(url, formats=["markdown"], only_main_content=_ONLY_MAIN_CONTENT)
        except Exception as error:
            raise RuntimeError(f"Failed to scrape page with Firecrawl: {error}") from error
    return document.markdown or ""


def fetch_page_markdown(url: str) -> str:
    """Fetches `url` (and, if paginated, its numbered follow-up pages) via
    Firecrawl and returns the combined markdown.

    See _page_urls_to_follow for how additional numbered pages (e.g.
    Eventbrite's "?page=1", "?page=2", ...) are detected and queued up
    alongside the given one. Numbered pagination stops early once a page
    repeats content already fetched (see _pages_are_near_duplicate).

    Args:
        url: The page to scrape.

    Returns:
        Every scraped page's markdown, joined together as a single string.

    Raises:
        RuntimeError: If any page fails to scrape for any reason.
    """
    pages_markdown: list[str] = []
    previous_page_lines: set[str] | None = None
    for page_url in _page_urls_to_follow(url):
        markdown = _scrape_one_page(page_url)
        pages_markdown.append(markdown)
        page_lines = _non_blank_lines(markdown)
        if previous_page_lines is not None and _pages_are_near_duplicate(page_lines, previous_page_lines):
            break
        previous_page_lines = page_lines
    return "\n\n".join(pages_markdown)
