"""Tracks Anthropic API token usage across a single run, for cost visibility.

Sites are processed concurrently (see cli.batch.MAX_WORKERS) and events within
a site are scored concurrently too (MAX_SCORING_WORKERS), so every extract/
score call across the whole run lands on this one shared, thread-safe tracker.
"""

import datetime
import json
import os
import threading

# How much a run's total tokens can grow over the previous run of the same
# mode before check_and_record_usage prints an alert - 0.3 means more than
# 130% of the previous run's total triggers it.
ALERT_THRESHOLD = 0.3


class TokenUsageTracker:
    """Thread-safe accumulator for input/output tokens across API calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def record(self, response) -> None:
        """Adds one Anthropic API response's token usage to the running total.

        Does nothing if `response` has no "usage" (e.g. a test double that
        doesn't model it), rather than raising.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        with self._lock:
            self.call_count += 1
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary(self) -> str:
        return (
            f"{self.call_count} API call(s) - {self.input_tokens:,} input + "
            f"{self.output_tokens:,} output = {self.total_tokens:,} total tokens"
        )


# One shared instance for the whole process, so every extract/score call in a
# run (python run.py invocation) reports into the same totals, regardless of
# which thread or module made the call.
tracker = TokenUsageTracker()


def _load_history(path: str) -> list[dict]:
    """Reads past runs' token usage records, tolerating a missing/corrupt file.

    Returns an empty list (rather than raising) if the file doesn't exist yet
    or can't be parsed - a broken history log shouldn't block a real run.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path) as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        print(f"  [warn] could not read token usage history at {path!r}: {error}")
        return []


def _previous_total_for_mode(history: list[dict], mode: str) -> int | None:
    """Returns the most recent past run's total_tokens for this mode, if any.

    Scans from the most recent entry backward rather than filtering the
    whole list, since only the single latest same-mode run matters here.
    """
    for record in reversed(history):
        if record.get("mode") == mode:
            return record.get("total_tokens")
    return None


def check_and_record_usage(usage_tracker: TokenUsageTracker, path: str, mode: str) -> None:
    """Alerts if this run used much more than the previous run, then logs it.

    Compares usage_tracker.total_tokens to the most recent past run recorded
    under the same `mode` (see _previous_total_for_mode) and prints an alert
    if it grew by more than ALERT_THRESHOLD. The first run of a given mode
    has nothing to compare against, so it's just recorded without an alert.

    Args:
        usage_tracker: This run's TokenUsageTracker.
        path: Path to the JSON history file (created if missing).
        mode: A label for which kind of run this is. There is only one run
            shape now - the weekly digest run - so this is always "normal";
            the parameter is kept so past history entries stay comparable.
    """
    history = _load_history(path)
    previous_total = _previous_total_for_mode(history, mode)
    current_total = usage_tracker.total_tokens

    if previous_total and current_total > previous_total * (1 + ALERT_THRESHOLD):
        increase_pct = (current_total / previous_total - 1) * 100
        print(
            f"ALERT: Token usage is up {increase_pct:.0f}% vs the previous "
            f"{mode!r} run ({previous_total:,} -> {current_total:,} tokens)."
        )

    history.append({
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "call_count": usage_tracker.call_count,
        "input_tokens": usage_tracker.input_tokens,
        "output_tokens": usage_tracker.output_tokens,
        "total_tokens": current_total,
    })
    with open(path, "w") as file:
        json.dump(history, file, indent=2)
