"""Score stage: ask an LLM to rate how well an event fits our criteria."""

import json
import re

from anthropic import Anthropic

# Scoring is a small, cheap classification task, so it uses Haiku instead of
# the larger Sonnet model used for extraction (see scrape.extract.MODEL).
MODEL = "claude-haiku-4-5-20251001"

# Edit this to tune what counts as a "good fit" event. Used verbatim in the
# scoring prompt, so keep it as plain instructions for the model.
SCORING_CRITERIA = """
- In-person events in Georgia are preferred. Virtual-only events score low.
- Educational AI events are good (technical talks, conferences, industry
  discussions on AI).
- Pure paid AI training or events that are heavy-handed sales pitches for the
  organizer's core business score low.
- Multi-day conferences are fine even if they cost money.
"""

_FENCE_START_PATTERN = re.compile(r"^```(?:json)?\s*")
_FENCE_END_PATTERN = re.compile(r"\s*```$")

_SCORING_PROMPT_TEMPLATE = """\
Rate how well this event fits the criteria below, on a scale of 1 (poor fit)
to 5 (excellent fit). Use these criteria:
{criteria}

Event details (JSON):
{event_json}

Return ONLY a valid JSON object (no prose, no markdown fences) with exactly
two fields: "score" (integer 1-5) and "reason" (a single sentence).
"""


def score_event(client: Anthropic, event: dict) -> dict:
    """Asks Claude to rate an event's fit from 1-5 with a one-line reason.

    Args:
        client: An initialized Anthropic client.
        event: An event dict, as produced by extract.extract_events.

    Returns:
        A dict with "score" (int, 1-5; 0 if unparseable) and "reason" (str).
    """
    prompt = _SCORING_PROMPT_TEMPLATE.format(
        criteria=SCORING_CRITERIA, event_json=json.dumps(event)
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text.strip()
    cleaned = _FENCE_START_PATTERN.sub("", raw_text)
    cleaned = _FENCE_END_PATTERN.sub("", cleaned)
    try:
        result = json.loads(cleaned)
        score = int(result.get("score", 0))
        reason = str(result.get("reason", "")).strip()
    except (json.JSONDecodeError, ValueError, TypeError):
        score, reason = 0, "Could not parse model score; defaulted to 0."
    return {"score": score, "reason": reason}
