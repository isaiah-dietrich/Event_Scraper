"""Fetch stage: retrieve a page's fully-rendered content as markdown via Firecrawl."""

import collections
import datetime
import os
import re
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
#
# That "Consumed 11-12, Remaining 0" reading puts the account's real ceiling
# at ~10 requests/minute - the published limit for the tier whose concurrency
# is the 2 MAX_CONCURRENT_SCRAPES is already set to. The old value of 8 was
# set against that ceiling with no real margin, and the 2026-08-24 run showed
# why margin is needed: requests this limiter never sees still land in the
# same minute and count against the same quota.
#   * The SDK retries a 502 or a connection-level failure up to
#     max_retries=3 times *inside* one client.scrape() call (verified in
#     firecrawl-py 4.32.1's v2/utils/http_client.py), so a single slot taken
#     here can be three requests charged there.
#   * A refused request still increments Firecrawl's counter - the two
#     failures that morning read 11 and then 12 against a Remaining of 0, so
#     the count keeps climbing once a run is over the line.
#   * Anything else the account does outside this job shares the same quota.
# 6/minute leaves four requests of headroom - enough to absorb two calls each
# taking the SDK's full three attempts and still stay under 10.
MAX_SCRAPES_PER_MINUTE = 6

# Counting calls per rolling minute still permits a burst: all
# MAX_SCRAPES_PER_MINUTE of them may start within the same few seconds and
# then nothing for the rest of the minute - which is exactly what happened,
# since a scrape returns in seconds and nothing was spacing the next one out.
# Firecrawl's accounting need not use the same window ours does, and a burst
# is what any shorter window sees. So calls are additionally spaced evenly,
# one every 60/MAX_SCRAPES_PER_MINUTE seconds, making the instantaneous rate
# equal to the average rate. Derived rather than written as its own literal so
# retuning the cap can never leave the two disagreeing.
_MIN_SECONDS_BETWEEN_SCRAPES = 60.0 / MAX_SCRAPES_PER_MINUTE


class _RateLimiter:
    """Blocks callers so no more than `max_per_minute` calls start within any
    rolling 60-second window *and* no two start closer together than
    `min_interval` seconds, across all threads.

    Two rules because either alone leaves a gap: the window is the hard
    "never more than N in a minute" invariant, which still holds when a
    caller is blocked elsewhere and wakes up late, while the minimum interval
    is what keeps those N from arriving as one burst. Still not a smart
    backoff - it paces call *starts* and never reacts to a 429; the retry
    loop in _scrape_one_page is what handles a limit crossed anyway.
    """

    def __init__(self, max_per_minute: int, min_interval: float):
        self._max_per_minute = max_per_minute
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._call_times: collections.deque[float] = collections.deque()
        self._last_call_time: float | None = None

    def wait_for_slot(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._call_times and now - self._call_times[0] >= 60:
                    self._call_times.popleft()
                window_wait = 0.0
                if len(self._call_times) >= self._max_per_minute:
                    window_wait = 60 - (now - self._call_times[0])
                spacing_wait = 0.0
                if self._last_call_time is not None:
                    spacing_wait = self._min_interval - (now - self._last_call_time)
                sleep_time = max(window_wait, spacing_wait)
                if sleep_time <= 0:
                    self._call_times.append(now)
                    self._last_call_time = now
                    return
            # Slept with the lock released, so a thread that is merely waiting
            # never blocks the one whose slot comes up next. Claiming happens
            # under the lock, so two threads can't take the same slot and end
            # up starting within min_interval of each other.
            time.sleep(sleep_time)


_rate_limiter = _RateLimiter(MAX_SCRAPES_PER_MINUTE, _MIN_SECONDS_BETWEEN_SCRAPES)


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


# A 429 from Firecrawl is transient by construction - its own message says
# "please retry after 1s" - but firecrawl-py 4.32.1 does not retry it. Its
# HttpClient retry loop covers only status 502 and connection-level
# requests.RequestException; a 429 is turned straight into a RateLimitError
# (verified in that version's v2/utils/http_client.py and
# v2/utils/error_handler.py). Without a retry here, that one refused second
# becomes a permanent "failed: ..." row for the whole site for the whole
# week: cli.batch._process_site_once catches the RuntimeError fetch raises
# and returns a failure row from it, so it never reaches the one-shot retry
# _process_site wraps around that call. That is what cost the 2026-08-24 run
# its Eventbrite and ai.georgia.gov results. No amount of pacing can
# guarantee zero 429s when the same quota is shared with the SDK's own
# invisible retries and with everything else on the account, so the retry is
# the part that has to exist - the pacing above only makes it rare.
_MAX_RATE_LIMIT_RETRIES = 3

# Base of the exponential fallback (10s, then 20s, then 40s) used when the
# error text carries no usable hint. It starts at roughly one call slot
# (_MIN_SECONDS_BETWEEN_SCRAPES) rather than at the ~1s Firecrawl suggests,
# because the minute's quota is shared: if this call was refused, the other
# threads pacing through the limiter are about to spend whatever frees up
# first, and coming back in a second just spends a retry to be refused again.
_RATE_LIMIT_BACKOFF_SECONDS = 10.0

# Ceiling on any single wait, however long the hint asks for. The limit being
# waited on is per *minute*, so no honest reset is more than ~60s out;
# anything longer means clock skew between the runner and the API, or a
# "resets at" describing some other window, and isn't worth sleeping through.
# Worst case one page adds _MAX_RATE_LIMIT_RETRIES * this = 225s before
# giving up - bounded enough to sit inside the weekly GitHub Action.
_MAX_RATE_LIMIT_WAIT_SECONDS = 75.0

# The two hint shapes Firecrawl puts in a 429 body, both present in the
# 2026-08-24 failures: "...please retry after 1s, resets at Mon Aug 24 2026
# 14:34:04 GMT+0000". "retry after" is preferred because it's a duration and
# so needs no clock agreement with the API at all; "resets at" is the
# fallback for a message carrying only the absolute time.
_RETRY_AFTER_PATTERN = re.compile(r"retry after\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_RESETS_AT_PATTERN = re.compile(
    r"resets at\s+([A-Za-z]{3} [A-Za-z]{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2} GMT[+-]\d{4})"
)
_RESETS_AT_FORMAT = "%a %b %d %Y %H:%M:%S GMT%z"


def _is_rate_limit_error(error: BaseException) -> bool:
    """True if `error` is Firecrawl refusing a call for rate-limit reasons.

    Duck-typed on the status code rather than by importing the SDK's
    RateLimitError: every firecrawl-py error carries .status_code, and
    matching on 429 keeps working if that exception class ever moves or is
    renamed. The message match is only a fallback for an error with no status
    code at all - a call that failed with some *other* status is definitively
    not a rate limit and must not burn retries as one.
    """
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        return status_code == 429
    return "rate limit" in str(error).lower()


def _parse_rate_limit_hint(message: str) -> float | None:
    """Seconds Firecrawl's own 429 text asks us to wait, if it says.

    Returns None when neither hint (see _RETRY_AFTER_PATTERN,
    _RESETS_AT_PATTERN) is present or parseable, leaving the caller on its
    exponential fallback. Nothing here is load-bearing - a missing or wrong
    hint only makes a wait slightly the wrong length - so every failure path
    just returns None rather than raising into the middle of a retry.
    """
    match = _RETRY_AFTER_PATTERN.search(message)
    if match:
        return float(match.group(1))

    match = _RESETS_AT_PATTERN.search(message)
    if match:
        try:
            resets_at = datetime.datetime.strptime(match.group(1), _RESETS_AT_FORMAT)
        except ValueError:
            return None
        return (resets_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return None


def _rate_limit_wait_seconds(error: BaseException, attempt: int) -> float:
    """How long to wait before retrying a rate-limited scrape.

    The larger of Firecrawl's own hint and this attempt's exponential
    backoff, capped at _MAX_RATE_LIMIT_WAIT_SECONDS. Taking the larger rather
    than obeying the hint outright is what makes a page that keeps being
    refused actually back off instead of re-firing on the same "retry after
    1s" every time; taking the hint when it's the larger is what keeps a
    genuinely long reset from being retried into three wasted attempts.

    Args:
        error: The rate-limit error just raised, whose text may carry a hint.
        attempt: 0-based index of the attempt that just failed.
    """
    backoff = _RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt)
    hint = _parse_rate_limit_hint(str(error))
    return min(max(backoff, hint or 0.0), _MAX_RATE_LIMIT_WAIT_SECONDS)


def _scrape_one_page(url: str) -> str:
    """Scrapes a single URL via Firecrawl and returns its markdown content.

    Three independent limits apply, each covering something the others
    don't: the budget caps total credits spent per run (see
    MAX_SCRAPES_PER_RUN), the semaphore bounds how many scrape calls are in
    flight at once across all concurrently-processing sites (see
    MAX_CONCURRENT_SCRAPES), and the rate limiter bounds how fast calls may
    *start* (see MAX_SCRAPES_PER_MINUTE and _MIN_SECONDS_BETWEEN_SCRAPES) -
    concurrency alone doesn't prevent bursting past a per-minute cap when
    individual calls complete quickly.

    On top of those, a rate-limited call is retried rather than failed (see
    _MAX_RATE_LIMIT_RETRIES). Three details of how that's arranged matter:

      * The budget is claimed once, before the first attempt, never per
        attempt. MAX_SCRAPES_PER_RUN is a *credit* budget and Firecrawl
        charges no credit for a request it refused, so counting retries
        against it would quietly shrink the run's real budget every time the
        limit was brushed. It is still claimed before the rate limiter, so an
        over-budget call fails immediately instead of first sleeping out a
        pacing wait it was never going to use.
      * Every attempt does go through the rate limiter. A retry is a fresh
        request against the same per-minute quota and has to be paced like
        any other, or the retries themselves become the burst.
      * Both the pacing wait and the backoff sleep happen *outside*
        _scrape_semaphore, so a waiting caller never sits on one of the
        MAX_CONCURRENT_SCRAPES slots that a runnable thread needs.

    Only rate-limit errors are retried; anything else (a bad URL, a dead
    host, a 4xx) still fails on the first attempt. The SDK retries transient
    failures of its own, but only status 502 and connection-level errors, up
    to max_retries=3 - never a 429.

    Raises:
        ScrapeBudgetExceeded: If this run has already used its full budget.
        RuntimeError: If the scrape fails for any other reason, including a
            rate limit that outlived every retry. Unchanged contract:
            cli.batch._process_site_once turns this into a "failed: ..."
            status row rather than aborting the batch.
    """
    client = _get_client()
    _budget.consume()
    attempt = 0
    while True:
        _rate_limiter.wait_for_slot()
        failure: Exception | None = None
        with _scrape_semaphore:
            try:
                document = client.scrape(url, formats=["markdown"], only_main_content=_ONLY_MAIN_CONTENT)
            except Exception as error:
                failure = error
        if failure is None:
            return document.markdown or ""
        if attempt >= _MAX_RATE_LIMIT_RETRIES or not _is_rate_limit_error(failure):
            raise RuntimeError(f"Failed to scrape page with Firecrawl: {failure}") from failure
        wait_seconds = _rate_limit_wait_seconds(failure, attempt)
        print(
            f"  [fetch] rate limited by Firecrawl on {url} - retry "
            f"{attempt + 1}/{_MAX_RATE_LIMIT_RETRIES} in {wait_seconds:.0f}s"
        )
        time.sleep(wait_seconds)
        attempt += 1


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
