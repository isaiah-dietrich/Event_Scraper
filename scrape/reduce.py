"""Reduce stage: strip rendered HTML down to visible text."""

import re

_BLOCK_TAGS_PATTERN = re.compile(
    r"<(script|style|svg|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_REPEATED_SPACE_PATTERN = re.compile(r"[ \t]+")
_REPEATED_NEWLINE_PATTERN = re.compile(r"\n\s*\n+")


def reduce_html(html: str) -> str:
    """Strips tags, scripts, and styles from HTML, leaving visible text.

    This cuts token usage and noise before the text is sent to an LLM.

    Args:
        html: Raw HTML, typically from a rendered page.

    Returns:
        The page's visible text with excess whitespace collapsed.
    """
    html = _BLOCK_TAGS_PATTERN.sub(" ", html)
    html = _COMMENT_PATTERN.sub(" ", html)
    text = _TAG_PATTERN.sub("\n", html)
    text = _REPEATED_SPACE_PATTERN.sub(" ", text)
    text = _REPEATED_NEWLINE_PATTERN.sub("\n", text)
    return text.strip()
