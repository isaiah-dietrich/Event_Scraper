"""Reduce stage: strip rendered HTML down to visible text."""

import re
import urllib.parse

_BLOCK_TAGS_PATTERN = re.compile(
    r"<(script|style|svg|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_IMG_ALT_PATTERN = re.compile(r'<img\b[^>]*\balt=["\']([^"\']*)["\'][^>]*>', re.IGNORECASE)
_A_HREF_PATTERN = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']*)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_REPEATED_SPACE_PATTERN = re.compile(r"[ \t]+")
_REPEATED_NEWLINE_PATTERN = re.compile(r"\n\s*\n+")

# hrefs that don't point anywhere useful - an in-page anchor or a JS-driven
# handler, never a real signup/details URL - so inlining them would just add
# noise ahead of the LLM extraction step.
_NON_NAVIGATING_HREF_PATTERN = re.compile(r"^\s*(#|javascript:)", re.IGNORECASE)


def _inline_image_alt_text(match: re.Match) -> str:
    """Turns an <img alt="..."> into visible text instead of losing it.

    Event banner images often carry real context in their alt text (venue
    names, city names, a description of the flyer) that isn't repeated
    anywhere else on the page - stripping the tag outright like every other
    element throws that information away before the LLM ever sees it.
    """
    alt_text = match.group(1).strip()
    return f"\n{alt_text}\n" if alt_text else ""


def _make_inline_link_href(base_url: str):
    """Returns a re.sub callback that turns an <a href="..."> into visible
    text with its target URL appended, instead of losing the link entirely
    when tags are stripped.

    Event titles and "Learn More"/"Register" buttons are almost always the
    <a> itself, with the actual signup/details URL living only in its href -
    nowhere in the visible text - so a plain tag-strip throws away exactly
    the information scrape.extract's "signup_link" field depends on.
    Resolves a relative href (e.g. "/events/123") against the page's own URL
    so what reaches the model is always a usable absolute URL. Skips
    in-page anchors and javascript: links (see _NON_NAVIGATING_HREF_PATTERN)
    - those aren't real destinations.
    """

    def _inline_link_href(match: re.Match) -> str:
        href, label = match.group(1).strip(), match.group(2)
        if not href or _NON_NAVIGATING_HREF_PATTERN.match(href):
            return label
        absolute_url = urllib.parse.urljoin(base_url, href) if base_url else href
        return f"{label} ({absolute_url})"

    return _inline_link_href


def reduce_html(html: str, base_url: str = "") -> str:
    """Strips tags, scripts, and styles from HTML, leaving visible text.

    This cuts token usage and noise before the text is sent to an LLM.
    Image alt text is kept (see _inline_image_alt_text), and every link's
    target URL is inlined next to its visible label text (see
    _make_inline_link_href) - both are "hidden" markup that a plain
    text-only read of the page would otherwise lose.

    Args:
        html: Raw HTML, typically from a rendered page.
        base_url: The page's own URL, used to resolve relative hrefs (e.g.
            "/events/123") to absolute URLs. Defaults to "", meaning
            relative hrefs are inlined as-is (only meaningful for callers
            that don't care about link URLs at all, e.g. scrape.fetch's
            date-cutoff check).

    Returns:
        The page's visible text (plus meaningful image alt text and inlined
        link URLs) with excess whitespace collapsed.
    """
    html = _BLOCK_TAGS_PATTERN.sub(" ", html)
    html = _COMMENT_PATTERN.sub(" ", html)
    html = _IMG_ALT_PATTERN.sub(_inline_image_alt_text, html)
    html = _A_HREF_PATTERN.sub(_make_inline_link_href(base_url), html)
    text = _TAG_PATTERN.sub("\n", html)
    text = _REPEATED_SPACE_PATTERN.sub(" ", text)
    text = _REPEATED_NEWLINE_PATTERN.sub("\n", text)
    return text.strip()
