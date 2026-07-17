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
# Zero-width characters (word-joiners, BOM, U+200B spacers) carry no visible
# text but survive tag-stripping as their own "lines" - some SPA calendars
# (e.g. Luma) emit hundreds of U+200B spacer nodes, each becoming a junk line.
# Drop them outright before whitespace is collapsed so they don't bloat the
# prompt or defeat the blank-line collapse below.
_ZERO_WIDTH_PATTERN = re.compile("[​‌‍⁠﻿]")

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

# The href *itself* is often the strongest signal an anchor points at an event,
# independent of how its visible label reads. A card whose <a> wraps only a
# short title ("AI Tinkerers") or an image (alt text, no words) fails every
# label test above, yet an href shaped like "/event/...", "/register",
# "eventbrite.com/e/...", GrowthZone/ChamberMaster "/calendar/details/...",
# etc. clearly leads to the signup/details page the client cares about most.
# Matched (case-insensitively) against the resolved absolute URL, so these keep
# MORE real signup links even as the length cap and de-duplication below cut
# the total URL count. Kept deliberately specific so ordinary nav/marketing
# links ("/about", "/pricing") don't match.
_EVENT_HREF_PATTERN = re.compile(
    r"/events?/"                 # /event/ , /events/
    r"|/rsvp"
    r"|/register|/registration"
    r"|/tickets?\b|/ticket/"
    r"|/sign[-_]?up\b"
    r"|/attend\b"
    r"|/webinar"
    r"|/calendar/details/"       # GrowthZone / ChamberMaster event pages
    r"|eventbrite\.[a-z.]+/e/"   # Eventbrite event pages
    r"|lu\.ma/",                 # Luma event pages / short links
    re.IGNORECASE,
)

# Query parameters that are pure click-tracking / campaign attribution: they
# never change where a link points, but they bloat the inlined URL (and would
# be written verbatim into the spreadsheet's signup_link). Stripped before the
# URL is inlined. Names are matched case-insensitively; anything starting with
# "utm_" is dropped as a family.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "msclkid", "yclid",
    "twclid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "igshid",
    "ref_src", "vero_id", "vero_conv", "oly_anon_id", "oly_enc_id", "_ga",
    "s_cid", "icid", "ncid", "cmpid", "spm",
})

# No legitimate signup/details URL runs to hundreds of characters (the longest
# real one across sampled Georgia event sites was ~250). A URL this long after
# tracking-param stripping is an ad-network redirect or parked-domain search
# link (seen at 7,700+ chars each on an ad-wall Meetup capture) - pure token
# waste - so its URL is dropped and only the visible label kept. Set with a
# wide margin over real signup URLs so a genuine long registration link is
# never sacrificed for the critical signup_link field.
_MAX_INLINE_URL_CHARS = 500

# Cross-snapshot / cross-page block collapse. scrape.fetch joins many DOM
# snapshots (settled page + per-scroll-step + load-more + next-month + iframe +
# ?page=N follow-ups) into one string before reduction. Whole-snapshot
# subsumption (fetch._prune_subsumed_snapshots) only drops a snapshot that adds
# almost nothing new, so a page that *grows slightly* each step - or a
# persistent widget (a mini-calendar, a nav bar, a repeating news carousel) -
# still reaches the LLM many times over. This collapses any contiguous run of
# >= _MIN_DUP_BLOCK_LINES lines that already appeared verbatim earlier, keeping
# the first occurrence and dropping the repeats. The threshold is deliberately
# large: single lines legitimately recur across different events ("6:00 PM"),
# but a run of this many *consecutive, byte-identical* lines is repeated chrome
# or a re-listed event, never two distinct events - a distinct event's title,
# date, and signup URL differ, breaking any such run.
_MIN_DUP_BLOCK_LINES = 8


def _inline_image_alt_text(match: re.Match) -> str:
    """Turns an <img alt="..."> into visible text instead of losing it.

    Event banner images often carry real context in their alt text (venue
    names, city names, a description of the flyer) that isn't repeated
    anywhere else on the page - stripping the tag outright like every other
    element throws that information away before the LLM ever sees it. Placing
    the alt text just before the anchor's own text also means an image-only
    link (<a href><img alt="Event Title"></a>) ends up with "Event Title" as
    its inline label, which _anchor_url_is_worth_inlining can then weigh.
    """
    alt_text = match.group(1).strip()
    return f"\n{alt_text}\n" if alt_text else ""


def _anchor_url_is_worth_inlining(label_html: str, absolute_url: str) -> bool:
    """Decides whether an <a>'s target URL should be kept next to its text.

    We inline link URLs so scrape.extract can populate "signup_link", but a
    page's anchors are overwhelmingly nav/footer/social/legal links whose URLs
    are pure token waste (47-65% of a reduced page in practice, almost none of
    it an event link). Keep the URL when either the anchor's *label* or its
    *href* looks like an event link:
      - the label wraps a heading (an event *title* is usually an <h1>-<h6>
        link), or
      - the label's visible text is substantial (>= _MIN_LABEL_WORDS_FOR_URL
        words) - catching card-style title links that carry no heading tag, or
      - the label reads as an action/details phrase ("Register", "Learn More",
        "View Event", ... - see _LINK_ACTION_PHRASES), or
      - the *href* is shaped like an event/signup page (see _EVENT_HREF_PATTERN)
        - this alone rescues a short-title card ("AI Tinkerers") or an
        image-only anchor whose label carries too few words to pass on its own,
        which is exactly where signup links were being dropped before.
    Short nav labels ("About", "Login", "Cart") with an ordinary href match
    none of these, so their URLs are dropped while the visible label text is
    still kept.

    Args:
        label_html: The anchor's inner HTML (match group 2), still carrying any
            nested tags - inspected before the generic tag-strip runs.
        absolute_url: The anchor's resolved absolute href, matched against
            _EVENT_HREF_PATTERN.
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
    if any(phrase in lowered for phrase in _LINK_ACTION_PHRASES):
        return True
    return bool(_EVENT_HREF_PATTERN.search(absolute_url))


def _normalize_inline_url(absolute_url: str) -> str:
    """Strips click-tracking/campaign query parameters from an inlined URL.

    utm_*, fbclid, gclid, mc_cid, and similar (see _TRACKING_PARAM_NAMES /
    _TRACKING_PARAM_PREFIXES) never change where a signup link points, so
    dropping them is lossless - it just trims tokens and keeps the URL written
    to the spreadsheet clean. A URL with no query string, or no tracking
    params, is returned unchanged (including its exact separators), so ordinary
    links are untouched.
    """
    parsed = urllib.parse.urlsplit(absolute_url)
    if not parsed.query:
        return absolute_url
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not (
            key.lower() in _TRACKING_PARAM_NAMES
            or key.lower().startswith(_TRACKING_PARAM_PREFIXES)
        )
    ]
    new_query = urllib.parse.urlencode(kept)
    if new_query == parsed.query:
        return absolute_url
    return urllib.parse.urlunsplit(parsed._replace(query=new_query))


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

    Three further trims keep the inlined URLs lean without losing any signup
    link:
      - tracking parameters are stripped (see _normalize_inline_url);
      - the same target is inlined in full only once per page (`seen`); a
        second/third anchor to it (an event card's title + image + "Details"
        button all point to the same page) keeps just its label, since the
        URL already reached the model once, right next to this event;
      - an absurdly long URL (see _MAX_INLINE_URL_CHARS) is dropped as
        ad/redirect junk, keeping only its label.
    """
    seen: set[str] = set()

    def _inline_link_href(match: re.Match) -> str:
        # Chromium's DOM serializer writes "&" in an href's query string as
        # "&amp;", so a link like "?id=1&type=2" would otherwise get inlined
        # as a broken URL - unescape before urljoin/skip checks so both see
        # the real href.
        href, label = unescape(match.group(1).strip()), match.group(2)
        if not href or _NON_NAVIGATING_HREF_PATTERN.match(href):
            return label
        absolute_url = urllib.parse.urljoin(base_url, href) if base_url else href
        if not _anchor_url_is_worth_inlining(label, absolute_url):
            return label
        absolute_url = _normalize_inline_url(absolute_url)
        if absolute_url in seen or len(absolute_url) > _MAX_INLINE_URL_CHARS:
            return label
        seen.add(absolute_url)
        return f"{label} ({absolute_url})"

    return _inline_link_href


def _collapse_repeated_blocks(text: str) -> str:
    """Drops contiguous runs of >= _MIN_DUP_BLOCK_LINES lines already seen
    verbatim earlier in the text, keeping the first occurrence.

    scrape.fetch concatenates many DOM snapshots (and paginated follow-up
    pages) before reduction, and whole-snapshot subsumption only removes a
    snapshot that adds essentially nothing new - so a page that grows a little
    each scroll step, or that carries a persistent widget/nav/news carousel,
    still repeats large blocks of identical text to the LLM. Collapsing those
    repeats is a big token win on multi-snapshot pages (a virtualized calendar
    feed can be >70% repeated chrome) and cross-page chrome on ?page=N feeds.

    Line-level (not snapshot-level) and conservative: only a run of many
    *consecutive, byte-identical* lines is removed, never a single recurring
    line ("6:00 PM" legitimately repeats across different events). Two distinct
    events can't form such a run - their titles, dates, and signup URLs differ,
    breaking it - so no event is ever merged away. The first occurrence of
    every block is always kept, so every distinct line (and its inlined signup
    URL) still reaches the model.
    """
    lines = text.split("\n")
    n = len(lines)
    if n < 2 * _MIN_DUP_BLOCK_LINES:
        return text
    seen_grams: set[tuple] = set()
    removable = bytearray(n)
    for i in range(n - _MIN_DUP_BLOCK_LINES + 1):
        gram = tuple(lines[i:i + _MIN_DUP_BLOCK_LINES])
        if gram in seen_grams:
            for k in range(i, i + _MIN_DUP_BLOCK_LINES):
                removable[k] = 1
        else:
            seen_grams.add(gram)
    if not any(removable):
        return text
    return "\n".join(lines[i] for i in range(n) if not removable[i])


def reduce_html(html: str, base_url: str = "") -> str:
    """Strips tags, scripts, and styles from HTML, leaving visible text.

    This cuts token usage and noise before the text is sent to an LLM.
    Image alt text is kept (see _inline_image_alt_text), and every link's
    target URL is inlined next to its visible label text (see
    _make_inline_link_href) - both are "hidden" markup that a plain
    text-only read of the page would otherwise lose. As a final pass, large
    blocks of text repeated verbatim across the joined DOM snapshots are
    collapsed (see _collapse_repeated_blocks).

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
    text = _ZERO_WIDTH_PATTERN.sub("", text)
    text = _REPEATED_SPACE_PATTERN.sub(" ", text)
    text = _REPEATED_NEWLINE_PATTERN.sub("\n", text)
    text = _collapse_repeated_blocks(text)
    return text.strip()
