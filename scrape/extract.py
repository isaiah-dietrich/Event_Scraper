"""Extract stage: ask an LLM to pull structured events out of page text."""

import json
import re

from anthropic import Anthropic

MODEL = "claude-sonnet-4-6"

EXTRACTION_FIELDS = [
    "title",
    "date",
    "start_time",
    "location",
    "is_in_person",
    "signup_link",
    "short_description",
]

_FENCE_START_PATTERN = re.compile(r"^```(?:json)?\s*")
_FENCE_END_PATTERN = re.compile(r"\s*```$")

_EXTRACTION_PROMPT_TEMPLATE = """\
You are given the visible text content of a webpage. This page may be a
dedicated events calendar, or it may be a general site (company homepage, blog
post, news article, etc.) that only mentions one or a few events in passing —
for example a sentence noting the company will be at a conference, or a
banner about an upcoming webinar. Find every distinct real-world or virtual
event mentioned anywhere in the text, no matter how small a portion of the
page it occupies.

Return ONLY a valid JSON array (no prose, no markdown code fences, no
explanation) where each element is an object with exactly these fields:
{fields}.

Rules:
- "is_in_person" must be a JSON boolean (true/false), inferred from the event
  details.
- If a field is unknown/missing, use an empty string "" (or false for
  is_in_person).
- "signup_link" should be the registration/details URL if present, else "".
- Do not invent events that are not in the text.
- "short_description" must be based on descriptive text actually present
  near the event (light rewording/trimming for length is fine) - do not
  synthesize a description from just the title, date, and location if no
  such text exists. Leave it "" in that case rather than making one up.
- Ignore generic site-navigation elements, such as a "browse other
  cities/locations" directory or menu, even if entries in it pair a place
  name with a short date fragment (e.g. "Next: Jul 8" or "Last: 1w ago").
  Those are links to other pages, not event listings - a real event has an
  actual title/description of what happens at it, not just a bare place
  name plus a date fragment.
- Only extract events that belong to this page's own subject; do not pull
  in events for other cities/locations that are merely linked to from a
  "browse other cities" directory elsewhere on the page.
- If the page does not mention any events at all, return an empty JSON
  array [].

PAGE CONTENT:
{page_text}
"""


def extract_events(client: Anthropic, page_text: str) -> list[dict]:
    """Asks Claude to extract structured events from page text.

    Args:
        client: An initialized Anthropic client.
        page_text: Reduced, visible page text (see reduce.reduce_html).

    Returns:
        A list of event dicts, one per field in EXTRACTION_FIELDS.

    Raises:
        ValueError: If the model's response is not a valid JSON array.
    """
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        fields=", ".join(EXTRACTION_FIELDS), page_text=page_text
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text.strip()
    if response.stop_reason == "max_tokens":
        raise ValueError(
            "Model response was truncated (hit max_tokens) before completing "
            f"the JSON array.\nRaw output:\n{raw_text[:1000]}"
        )
    return _parse_json_array(raw_text)


def _parse_json_array(raw_text: str) -> list[dict]:
    """Parses model output as a JSON array, tolerating stray code fences.

    Args:
        raw_text: The model's raw response text.

    Returns:
        The parsed list of event dicts.

    Raises:
        ValueError: If the text is not valid JSON, or not a JSON array.
    """
    cleaned = _FENCE_START_PATTERN.sub("", raw_text.strip())
    cleaned = _FENCE_END_PATTERN.sub("", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Model did not return valid JSON: {error}\nRaw output:\n{raw_text[:1000]}"
        ) from error
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array of events, got: {type(data)}")
    return data
