"""Score stage: ask an LLM to rate how well an event fits our criteria."""

import json
import re

from anthropic import Anthropic

from utility.token_usage import tracker as token_usage_tracker

# Scoring is a small, cheap classification task, so it uses Haiku instead of
# the larger Sonnet model used for extraction (see scrape.extract.MODEL).
MODEL = "claude-haiku-4-5-20251001"

# Edit this to tune what counts as a "good fit" event. Used verbatim in the
# scoring prompt, so keep it as plain instructions for the model.
SCORING_CRITERIA = """
- If the event has zero mention of AI, machine learning, or automation, it
  is an automatic 1 regardless of any other criteria, regardless of the
  hosting org or calendar it was scraped from. Judge this from the event's
  own title/description, not the organization's general focus. However, if
  the event has a genuine AI-focused component (a specific session, panel,
  speaker, or stated theme) even within a broader-themed event, score it on
  that component instead - the automatic-1 rule only applies when the event
  truly has no AI content, not merely because its overall framing isn't
  AI-specific.
- In-person events in Georgia score highest. Events located outside metro
  Atlanta should be favored over an otherwise-comparable Atlanta event - the
  scraped sites skew heavily toward Atlanta, and the digest should read as
  statewide, not Atlanta-only.
- Virtual-only events score 2 or below unless they are clearly Georgia-focused
  or hosted by an organization headquartered or based in Georgia, in which
  case they can score 3-4. A national corporate webinar with no Georgia tie
  scores 1.
- If location is missing/blank, do NOT assume the event is virtual or
  penalize it for ambiguous location - judge it on its AI content and other
  known details instead. A known in-person AI conference should still score
  well even if a particular scraped listing for it lacks a location field.
- Educational AI events are good (technical talks, conferences, industry
  discussions on AI).
- Pure paid AI training or events that are heavy-handed sales pitches for the
  organizer's core business score low.
- Multi-day conferences are fine even if they cost money.
"""

_FENCE_START_PATTERN = re.compile(r"^```(?:json)?\s*")
_FENCE_END_PATTERN = re.compile(r"\s*```$")

_VALID_CONFIDENCE_LEVELS = ("low", "medium", "high")

_SCORING_PROMPT_TEMPLATE = """\
Rate how well this event fits the criteria below, on a scale of 1 (poor fit)
to 5 (excellent fit). Use these criteria:
{criteria}

Event details (JSON):
{event_json}

Also rate your confidence in that score as "low", "medium", or "high",
based on how complete and unambiguous the event details are (e.g. a vague
or incomplete description warrants lower confidence).

Return ONLY a valid JSON object (no prose, no markdown fences) with exactly
three fields: "score" (integer 1-5), "confidence" ("low", "medium", or
"high"), and "reason" (a single sentence).
"""


def score_event(client: Anthropic, event: dict) -> dict:
    """Asks Claude to rate an event's fit from 1-5 with a confidence level.

    Args:
        client: An initialized Anthropic client.
        event: An event dict, as produced by extract.extract_events.

    Returns:
        A dict with "score" (int, 1-5; 0 if unparseable), "confidence"
        ("low"/"medium"/"high", or "" if missing/invalid), and "reason"
        (str).
    """
    # default=str: "date" is a real datetime.datetime by the time an event
    # reaches scoring (see cli.batch._filter_past_events), which the
    # default JSON encoder can't serialize on its own.
    prompt = _SCORING_PROMPT_TEMPLATE.format(
        criteria=SCORING_CRITERIA, event_json=json.dumps(event, default=str)
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    token_usage_tracker.record(response)
    raw_text = response.content[0].text.strip()
    cleaned = _FENCE_START_PATTERN.sub("", raw_text)
    cleaned = _FENCE_END_PATTERN.sub("", cleaned)
    try:
        result = json.loads(cleaned)
        score = int(result.get("score", 0))
        confidence = str(result.get("confidence", "")).strip().lower()
        if confidence not in _VALID_CONFIDENCE_LEVELS:
            confidence = ""
        reason = str(result.get("reason", "")).strip()
    except (json.JSONDecodeError, ValueError, TypeError):
        score, confidence = 0, ""
        reason = "Could not parse model score; defaulted to 0."
    return {"score": score, "confidence": confidence, "reason": reason}
