import datetime
import json

import pytest

from scrape.extract import EXTRACTION_FIELDS
from scrape.extract import extract_events
from tests.conftest import make_response


def _event(**overrides):
    event = {field: "" for field in EXTRACTION_FIELDS}
    event["is_in_person"] = False
    event.update(overrides)
    return event


def test_extracts_valid_json_array(fake_client):
    events = [_event(title="AI Summit", date="July 4, 2026")]
    client = fake_client([json.dumps(events)])

    result = extract_events(client, "some page text")

    assert result == events


def test_strips_markdown_code_fences(fake_client):
    events = [_event(title="Fenced Event")]
    client = fake_client([f"```json\n{json.dumps(events)}\n```"])

    result = extract_events(client, "page text")

    assert result == events


def test_strips_bare_code_fences_without_json_language_tag(fake_client):
    events = [_event(title="Bare Fenced Event")]
    client = fake_client([f"```\n{json.dumps(events)}\n```"])

    result = extract_events(client, "page text")

    assert result == events


def test_empty_page_returns_empty_array(fake_client):
    client = fake_client(["[]"])

    result = extract_events(client, "page with no events")

    assert result == []


def test_raises_value_error_on_truncated_response(fake_client):
    client = fake_client([make_response("[{\"title\": \"cut off", stop_reason="max_tokens")])

    with pytest.raises(ValueError, match="truncated"):
        extract_events(client, "page text")


def test_tolerates_leading_prose_before_json_array(fake_client):
    # Regression test: the model sometimes ignores the "no prose" rule and
    # prefaces the array with an explanation (observed on the calendar-scrape
    # step in production), with no code fence to key off of.
    events = [_event(title="AI Builders #19: Lunch & Learn", date="2026-07-01")]
    raw = (
        "Looking at the calendar, I can see one event in July 2026:\n\n"
        f"{json.dumps(events)}"
    )
    client = fake_client([raw])

    result = extract_events(client, "page text")

    assert result == events


def test_raises_value_error_on_invalid_json(fake_client):
    client = fake_client(["this is not json"])

    with pytest.raises(ValueError, match="did not return valid JSON"):
        extract_events(client, "page text")


def test_raises_value_error_when_response_is_not_a_list(fake_client):
    client = fake_client([json.dumps({"title": "not a list"})])

    with pytest.raises(ValueError, match="Expected a JSON array"):
        extract_events(client, "page text")


def test_prompt_includes_fields_and_page_text(fake_client):
    client = fake_client(["[]"])

    extract_events(client, "UNIQUE_PAGE_MARKER")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "UNIQUE_PAGE_MARKER" in prompt
    for field in EXTRACTION_FIELDS:
        assert field in prompt


def test_prompt_warns_against_city_directory_navigation_widgets(fake_client):
    client = fake_client(["[]"])

    extract_events(client, "some page text")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "browse other cities" in prompt
    assert "navigation" in prompt


def test_prompt_forbids_synthesizing_description_from_title_alone(fake_client):
    client = fake_client(["[]"])

    extract_events(client, "some page text")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "short_description" in prompt
    assert "synthesize" in prompt.lower()
    assert "title, date, and location" in prompt.lower()


def test_prompt_includes_todays_date(fake_client):
    # Regression test: without the real date, the model has to guess a year
    # for date text like "Wed, Jul 8" that omits one, and it guesses wrong -
    # which _filter_past_events then takes literally, silently dropping
    # every real event as "already past".
    client = fake_client(["[]"])

    extract_events(client, "some page text")

    prompt = client.calls[0]["messages"][0]["content"]
    today_str = datetime.date.today().strftime("%B %d, %Y")
    assert today_str in prompt
    assert "resolve it relative to today" in prompt.lower()


def test_prompt_infers_location_from_title_when_venue_is_hidden(fake_client):
    # Regression test: a page that hides the exact venue behind a
    # placeholder like "Register to See Location" shouldn't leave
    # "location" blank/ambiguous when the title/description names a state
    # outright (e.g. "... at Florida Atlantic University") - otherwise our
    # deterministic non-Georgia location filter never gets a chance to
    # auto-reject it, and it falls entirely on AI scoring to catch (which
    # may only do so at low confidence, keeping it visible in Events).
    client = fake_client(["[]"])

    extract_events(client, "some page text")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Register to" in prompt and "See Location" in prompt
    assert "Texas Roadhouse" in prompt  # guardrail against brand-name false positives
