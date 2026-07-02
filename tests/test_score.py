import datetime
import json

from scrape.score import score_event


def test_parses_valid_score_response(fake_client):
    client = fake_client([json.dumps({"score": 4, "confidence": "high", "reason": "Good fit."})])

    result = score_event(client, {"title": "AI Conference"})

    assert result == {"score": 4, "confidence": "high", "reason": "Good fit."}


def test_strips_markdown_code_fences(fake_client):
    payload = json.dumps({"score": 3, "confidence": "medium", "reason": "Ok fit."})
    client = fake_client([f"```json\n{payload}\n```"])

    result = score_event(client, {"title": "Some Event"})

    assert result == {"score": 3, "confidence": "medium", "reason": "Ok fit."}


def test_normalizes_confidence_case_and_whitespace(fake_client):
    client = fake_client([json.dumps({"score": 2, "confidence": " HIGH ", "reason": "x"})])

    result = score_event(client, {"title": "Event"})

    assert result["confidence"] == "high"


def test_invalid_confidence_value_becomes_empty_string(fake_client):
    client = fake_client([json.dumps({"score": 2, "confidence": "very sure", "reason": "x"})])

    result = score_event(client, {"title": "Event"})

    assert result["confidence"] == ""


def test_malformed_json_defaults_to_zero_score(fake_client):
    client = fake_client(["not valid json at all"])

    result = score_event(client, {"title": "Event"})

    assert result["score"] == 0
    assert result["confidence"] == ""
    assert "Could not parse" in result["reason"]


def test_missing_fields_default_gracefully(fake_client):
    client = fake_client(["{}"])

    result = score_event(client, {"title": "Event"})

    assert result == {"score": 0, "confidence": "", "reason": ""}


def test_non_integer_score_defaults_to_zero(fake_client):
    client = fake_client([json.dumps({"score": "not-a-number", "confidence": "low", "reason": "x"})])

    result = score_event(client, {"title": "Event"})

    assert result["score"] == 0
    assert result["confidence"] == ""


def test_prompt_includes_event_json_and_criteria(fake_client):
    client = fake_client([json.dumps({"score": 1, "confidence": "low", "reason": "x"})])
    event = {"title": "UNIQUE_EVENT_MARKER"}

    score_event(client, event)

    prompt = client.calls[0]["messages"][0]["content"]
    assert "UNIQUE_EVENT_MARKER" in prompt
    assert "Georgia" in prompt


def test_handles_event_with_real_datetime_date(fake_client):
    # By the time an event reaches scoring, "date" is a real datetime.datetime
    # (see cli.batch._filter_past_events) - json.dumps(event) must not choke
    # on that, the way it would with the plain json encoder by default.
    client = fake_client([json.dumps({"score": 4, "confidence": "high", "reason": "x"})])
    event = {"title": "Dated Event", "date": datetime.datetime(2026, 10, 12)}

    result = score_event(client, event)

    assert result["score"] == 4
    prompt = client.calls[0]["messages"][0]["content"]
    assert "2026-10-12" in prompt
