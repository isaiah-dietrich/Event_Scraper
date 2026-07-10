"""Reduce stage: strip rendered HTML down to visible text."""

import re
import urllib.parse
from html import unescape

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

# hrefs that don't point anywhere useful - an in-page anchor, a JS-driven
# handler, or a mailto/tel link - never a real signup/details URL - so
# inlining them would just add noise ahead of the LLM extraction step.
_NON_NAVIGATING_HREF_PATTERN = re.compile(r"^\s*(#|javascript:|mailto:|tel:)", re.IGNORECASE)

# Signals that an <a>'s target URL is worth inlining (see
# _anchor_url_is_worth_inlining). A heading nested inside the anchor marks an
# event *title* link; a visible label that reads as an action/details phrase
# marks a "Register"/"Learn More"-style link. Everything else is treated as
# nav/footer/social chrome whose URL is dropped.
_HEADING_IN_LABEL_PATTERN = re.compile(r"<h[1-6][\s>]", re.IGNORECASE)
_MIN_LABEL_WORDS_FOR_URL = 4
_LINK_ACTION_PHRASES = (
    "register",
    "registration",
    "sign up",
    "signup",
    "rsvp",
    "learn more",
    "read more",
    "view event",
    "view details",
    "event details",
    "more details",
    "more info",
    "details",
    "tickets",
    "get tickets",
    "buy tickets",
    "reserve",
    "book now",
)


def _inline_image_alt_text(match: re.Match) -> str:
    """Turns an <img alt="..."> into visible text instead of losing it.

    Event banner images often carry real context in their alt text (venue
    names, city names, a description of the flyer) that isn't repeated
    anywhere else on the page - stripping the tag outright like every other
    element throws that information away before the LLM ever sees it.
    """
    alt_text = match.group(1).strip()
    return f"\n{alt_text}\n" if alt_text else ""


def _anchor_url_is_worth_inlining(label_html: str) -> bool:
    """Decides whether an <a>'s target URL should be kept next to its text.

    We inline link URLs so scrape.extract can populate "signup_link", but a
    page's anchors are overwhelmingly nav/footer/social/legal links whose URLs
    are pure token waste (47-65% of a reduced page in practice, almost none of
    it an event link). Keep the URL only when the anchor plausibly points at an
    event:
      - it wraps a heading (an event *title* is usually an <h1>-<h6> link), or
      - its visible text is substantial (>= _MIN_LABEL_WORDS_FOR_URL words) -
        catching card-style title links that carry no heading tag, or
      - its visible text reads as an action/details phrase ("Register", "Learn
        More", "View Event", ... - see _LINK_ACTION_PHRASES).
    Short nav labels ("About", "Login", "Cart") match none of these, so their
    URLs are dropped while the visible label text is still kept.

    Args:
        label_html: The anchor's inner HTML (match group 2), still carrying any
            nested tags - inspected before the generic tag-strip runs.
    """
    if _HEADING_IN_LABEL_PATTERN.search(label_html):
        return True
    visible = unescape(_TAG_PATTERN.sub(" ", label_html))
    # Count only real words - a lone "&", "-" or "/" (common in labels like
    # "Legal & Privacy") isn't a word and shouldn't push a 2-3 word nav label
    # over the substantial-label threshold.
    words = [token for token in visible.split() if any(c.isalnum() for c in token)]
    if len(words) >= _MIN_LABEL_WORDS_FOR_URL:
        return True
    lowered = " ".join(words).lower()
    return any(phrase in lowered for phrase in _LINK_ACTION_PHRASES)


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
    in-page anchors, javascript: links, and mailto:/tel: links (see
    _NON_NAVIGATING_HREF_PATTERN) - those aren't real destinations - and drops
    the URL of any anchor that doesn't look like an event link at all (see
    _anchor_url_is_worth_inlining), keeping only its visible label text so a
    page's nav/footer/social chrome doesn't flood the prompt with URLs.
    """

    def _inline_link_href(match: re.Match) -> str:
        # Chromium's DOM serializer writes "&" in an href's query string as
        # "&amp;", so a link like "?id=1&type=2" would otherwise get inlined
        # as a broken URL - unescape before urljoin/skip checks so both see
        # the real href.
        href, label = unescape(match.group(1).strip()), match.group(2)
        if not href or _NON_NAVIGATING_HREF_PATTERN.match(href):
            return label
        if not _anchor_url_is_worth_inlining(label):
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
    # Decoding entities must happen after the tag strip above, never before:
    # if it ran first, literal text like "&lt;div&gt;" would decode into
    # "<div>" and then get eaten by _TAG_PATTERN as if it were real markup,
    # silently losing legitimate visible text. Also unescapes "&#39;",
    # "&quot;", "&nbsp;", etc. that would otherwise garble titles and waste
    # tokens. \xa0 (from "&nbsp;") is normalized to a regular space so the
    # whitespace-collapse patterns below actually catch it.
    text = unescape(text)
    text = text.replace("\xa0", " ")
    text = _REPEATED_SPACE_PATTERN.sub(" ", text)
    text = _REPEATED_NEWLINE_PATTERN.sub("\n", text)
    return text.strip()
