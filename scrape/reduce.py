"""Reduce stage: collapse content blocks repeated verbatim in scraped markdown."""

# Firecrawl already returns clean markdown (see scrape.fetch) - no
# tag-stripping, entity decoding, or href-inlining needed here anymore, since
# links already arrive as standard "[Label](URL)" markdown. What Firecrawl
# does NOT do on its own: collapse content that repeats verbatim within a
# single scrape (e.g. a "latest news" carousel rendered more than once) or
# across scrape.fetch's numbered pagination follow-up pages. Confirmed live -
# a real scrape of the TAG Online calendar repeated an entire news block 3
# times - so this collapse is still a real token win, not a hypothetical one.

# Line-level (not block/snapshot-level) and conservative: only a run of many
# *consecutive, byte-identical* lines is removed, never a single recurring
# line ("6:00 PM" legitimately repeats across different events). Two distinct
# events can't form such a run - their titles, dates, and signup URLs differ,
# breaking it - so no event is ever merged away.
_MIN_DUP_BLOCK_LINES = 8


def collapse_repeated_blocks(text: str) -> str:
    """Drops contiguous runs of >= _MIN_DUP_BLOCK_LINES lines already seen
    verbatim earlier in the text, keeping the first occurrence.

    The first occurrence of every block is always kept, so every distinct
    line (and its markdown link) still reaches the model.
    """
    lines = text.split("\n")
    n = len(lines)
    if n < 2 * _MIN_DUP_BLOCK_LINES:
        return text.strip()
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
        return text.strip()
    return "\n".join(lines[i] for i in range(n) if not removable[i]).strip()
