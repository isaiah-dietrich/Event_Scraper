"""Reduce stage: strip rendered HTML down to visible text."""

import re

_BLOCK_TAGS_PATTERN = re.compile(
    r"<(script|style|svg|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_IMG_ALT_PATTERN = re.compile(r'<img\b[^>]*\balt=["\']([^"\']*)["\'][^>]*>', re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_REPEATED_SPACE_PATTERN = re.compile(r"[ \t]+")
_REPEATED_NEWLINE_PATTERN = re.compile(r"\n\s*\n+")


def _inline_image_alt_text(match: re.Match) -> str:
    """Turns an <img alt="..."> into visible text instead of losing it.

    Event banner images often carry real context in their alt text (venue
    names, city names, a description of the flyer) that isn't repeated
    anywhere else on the page - stripping the tag outright like every other
    element throws that information away before the LLM ever sees it.
    """
    alt_text = match.group(1).strip()
    return f"\n{alt_text}\n" if alt_text else ""


def reduce_html(html: str) -> str:
    """Strips tags, scripts, and styles from HTML, leaving visible text.

    This cuts token usage and noise before the text is sent to an LLM.
    Image alt text is kept (see _inline_image_alt_text) since it's the one
    piece of "hidden" markup that can carry content a plain-text read of
    the page would otherwise lose.

    Args:
        html: Raw HTML, typically from a rendered page.

    Returns:
        The page's visible text (plus meaningful image alt text) with
        excess whitespace collapsed.
    """
    html = _BLOCK_TAGS_PATTERN.sub(" ", html)
    html = _COMMENT_PATTERN.sub(" ", html)
    html = _IMG_ALT_PATTERN.sub(_inline_image_alt_text, html)
    text = _TAG_PATTERN.sub("\n", html)
    text = _REPEATED_SPACE_PATTERN.sub(" ", text)
    text = _REPEATED_NEWLINE_PATTERN.sub("\n", text)
    return text.strip()
