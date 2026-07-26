"""Fetch stage: retrieve a page's fully-rendered content as markdown via Firecrawl."""

import os
import threading
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

    The semaphore bounds how many scrape calls are in flight at once across
    all concurrently-processing sites (see MAX_CONCURRENT_SCRAPES). The SDK
    itself already retries transient failures (network errors, 5xx) before
    raising, so no retry loop is needed here the way the old Playwright fetch
    needed one.
    """
    client = _get_client()
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
