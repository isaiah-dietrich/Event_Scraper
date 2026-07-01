"""Shared test fixtures.

Nothing in this suite makes real network calls: Anthropic responses are
stubbed with FakeAnthropicClient, and Playwright is stubbed per-test in
test_fetch.py. This keeps the suite fast, deterministic, and runnable
without API keys or browser binaries.
"""

from types import SimpleNamespace

import pytest


class FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic that returns canned responses.

    `responses` is a list of either raw text strings (wrapped into a
    successful response) or pre-built SimpleNamespace response objects.
    Each call to `messages.create` pops the next one, in order, and every
    call's kwargs are recorded in `.calls` for assertions.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, str):
            return make_response(response)
        return response


def make_response(text: str, stop_reason: str = "end_turn"):
    """Builds a minimal fake anthropic Message with the given text/stop_reason."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason)


@pytest.fixture
def fake_client():
    """Returns a factory: fake_client(["response text", ...]) -> FakeAnthropicClient."""
    return FakeAnthropicClient
