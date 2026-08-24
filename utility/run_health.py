"""Run-health gate: decides whether a weekly run is fit to email to the client.

Why this exists: on 2026-08-24 a scheduled run hit Firecrawl's per-minute
rate limit on two sites, and the client's digest email went out with raw
internal error text pasted into its "Websites scraped:" list -

    https://www.eventbrite.com/... - FAILED: Failed to scrape page with
    Firecrawl: Rate Limit Exceeded: ... Upgrade your plan at ...

Nothing in the pipeline was watching for that, because a single failing site
has always been survivable by design (every other site's events still go
out). The gate makes it *structurally* impossible for a run in that shape to
reach the client at all: cli.batch classifies the run here first, and only a
healthy run is allowed to send the client email. An unhealthy run's report
goes to the owner instead (see utility.email_digest.send_health_alert).

Two independent checks, in order of authority:

1. **Structured status check (primary).** Every row cli.batch produces
   carries a `status` of exactly "ok", "no_events", or "failed: <reason>" -
   that field *is* the ground truth the email body is merely a rendering of.
   Any row whose status starts with "failed" makes the run unhealthy. Exact,
   free, deterministic, and it cannot drift out of sync with the wording of
   the email, because it never looks at the wording.
2. **Rendered-body keyword scan (backstop).** The composed client body is
   also scanned for failure text before it is allowed out. This exists only
   to catch a *future* change that finds some new way to get error text into
   the body without going through a "failed:" status - it is deliberately
   not the primary mechanism, and on today's code it never fires without
   check 1 firing first.

Plus, from the caller's side, an unexpected exception anywhere in the
pipeline (passed in as `pipeline_error`) - a crash that produces no rows at
all is the most unhealthy a run can possibly be, and before this gate it
would have surfaced only as a stack trace in an Action log nobody reads.

This module is deliberately AI-free. An LLM call to judge the output would
cost money, be non-deterministic, and - worst of all - add a failure mode to
the very component whose entire job is to be the thing that still works when
something else has broken. It also imports nothing from
utility.email_digest: classification stays independent of composition and
sending, so it can be exercised on its own with plain dicts.
"""

import re
from dataclasses import dataclass

# cli.batch writes exactly one of "ok", "no_events", or f"failed: {reason}"
# into every row's "status". Matching on the prefix (rather than an equality
# test) is what makes this robust: the reason text varies endlessly, but the
# marker in front of it does not.
FAILED_STATUS_PREFIX = "failed"

# main() labels a row this way when a site's whole pipeline raised something
# neither fetch nor extract knew how to turn into a normal failure row. Worth
# calling out separately in the alert: it means an unhandled code path, not
# just a site being a site.
_UNEXPECTED_ERROR_MARKER = "unexpected error:"

# URLs are masked out before the keyword scan runs. The client body lists
# every scraped site URL verbatim, and a URL's path or query string is
# arbitrary text we do not control - a site could perfectly reasonably serve
# its calendar from ".../error-handling-meetups" - so scanning inside URLs
# would be inviting a false positive for no detection benefit. Error text is
# never reported *inside* a URL; it is always reported around one.
_URL_PATTERN = re.compile(r"https?://\S+")
_URL_MASK = "<url>"

# The scan reports *which line* tripped it, not the line's full contents: a
# single Firecrawl error is 300+ characters, the alert already prints every
# failure's error text verbatim in its own section, and repeating all of it
# again here just buries the finding. Same reasoning for the hit cap - once a
# handful of lines have failure text in them, listing more adds nothing.
_MAX_QUOTED_CHARS = 160
_MAX_REPORTED_HITS = 5

# The failure patterns, and the rationale for each. Every one of these is
# anchored on something machine-generated rather than on a bare word,
# because the body can legitimately contain human-written event copy: an
# event genuinely titled "Error Handling in Production ML" or "Exception
# Handling in Python" must not gate the run, and a bare \berror\b would gate
# both. (Event titles do not reach the client body today - only site URLs and
# per-site status lines do - but the whole point of a backstop is to still
# behave when today's assumptions stop holding.)
_FAILURE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # A diagnostic label: a failure word immediately followed by a colon.
    # This is how essentially all machine error text is rendered - "FAILED:
    # ...", "failed: ...", "RuntimeError: ...", "ValueError: ...",
    # "Exception: ..." - and it is a shape human event copy virtually never
    # takes (prose puts a colon after the phrase, not after the word:
    # "Error Handling in Production ML: A Workshop" does not match, because
    # the colon is not adjacent to "Error"). The leading \w* is what lets
    # this catch CamelCase exception class names, where there is no word
    # boundary in front of "Error".
    (
        re.compile(r"\b\w*(?:failed|failures?|errors?|exceptions?|traceback)\s*:",
                   re.IGNORECASE),
        'a diagnostic label such as "FAILED:", "RuntimeError:" or "Exception:"',
    ),
    # The exact header Python prints above a stack trace. Unmistakable.
    (
        re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
        "a Python traceback header",
    ),
    # The full phrase, not a bare "rate limit" - an AI meetup could easily be
    # titled "Rate Limits and Backpressure", but nothing except an API
    # complaining is going to say "rate limit exceeded".
    (
        re.compile(r"\brate limit exceeded\b", re.IGNORECASE),
        "a rate-limit error message",
    ),
    # Same reasoning: an HTTP 500's canonical wording, which no event listing
    # is going to produce on its own.
    (
        re.compile(r"\binternal server error\b", re.IGNORECASE),
        "an HTTP 500 error message",
    ),
    # Case-SENSITIVE, because all-caps FAILED is specifically the marker
    # cli.batch._site_status_line emits, and matching it case-sensitively
    # keeps a Title Case "Failed" inside human event copy from tripping the
    # gate. Redundant with the first pattern today (that marker is always
    # followed by a colon), and kept anyway so dropping the colon in some
    # future rewording cannot quietly disable the backstop.
    (
        re.compile(r"\bFAILED\b"),
        'the all-caps "FAILED" marker a per-site status line emits',
    ),
)

# Deliberately NOT in the list above, having been considered and rejected as
# too false-positive-prone to be safe on their own: a bare "error", "failed",
# "failure", "exception" or "traceback" with no colon (all of them plausible
# words in a real AI/ML event title), a bare "rate limit" (see above),
# "timeout"/"refused"/"unavailable" (plausible in event copy, and already
# covered by check 1 whenever they appear in a real failure), and "warning"
# (not a failure at all - the pipeline logs warnings on healthy runs, e.g.
# for an unparseable event date, and gating on those would withhold digests
# from the client for no reason).


@dataclass(frozen=True)
class SiteFailure:
    """One site that did not come back clean, with its full error text.

    Attributes:
        url: The site URL, taken from the row's source_url.
        reason: Everything after the "failed: " prefix, verbatim and
            untruncated - this is the text the owner needs to actually
            diagnose the run, so it is deliberately never shortened.
        unexpected: True when the status was "failed: unexpected error: ...",
            i.e. the per-site pipeline raised something no stage knew how to
            handle, as opposed to a known failure mode like a bot challenge
            or a malformed extraction response.
    """

    url: str
    reason: str
    unexpected: bool


@dataclass(frozen=True)
class RunHealthReport:
    """The verdict on one run, plus everything the owner alert needs to say.

    Deliberately a plain data object with no rendering on it: cli.batch reads
    `healthy` to decide what to do, and utility.email_digest reads the rest
    to compose the alert. Frozen (and holding tuples, not lists) so a report
    handed to the emailer cannot be edited out from under the decision that
    was already made on it.
    """

    healthy: bool
    site_failures: tuple[SiteFailure, ...] = ()
    keyword_hits: tuple[str, ...] = ()
    pipeline_error: str | None = None
    pipeline_traceback: str | None = None
    site_statuses: tuple[tuple[str, str], ...] = ()
    sites_scraped: int = 0
    new_event_count: int = 0
    digest_path: str | None = None
    client_body: str | None = None

    @property
    def backstop_only(self) -> bool:
        """True when the keyword backstop is the ONLY thing that gated this run.

        Worth distinguishing loudly, because it means the structured status
        check saw a perfectly clean run while error text reached the email
        body anyway - i.e. some code path is now producing failure text
        without setting a "failed:" status, and that is a bug in the pipeline
        rather than a bad week for one of the sites. In the ordinary case the
        backstop merely re-finds text a failed site already put there, which
        is corroboration, not a separate finding.
        """
        return bool(self.keyword_hits) and not self.site_failures and not self.pipeline_error

    @property
    def gate_reasons(self) -> tuple[str, ...]:
        """One human-readable line per reason this run was gated.

        Empty for a healthy run. Ordered most-fundamental-first (a crash
        outranks a failed site, which outranks the backstop) so the first
        line of the alert is the one worth reading first.
        """
        reasons = []
        if self.pipeline_error:
            reasons.append(
                "The pipeline itself raised an unexpected exception: "
                f"{self.pipeline_error}"
            )
        if self.site_failures:
            unexpected = sum(1 for failure in self.site_failures if failure.unexpected)
            line = (
                f"{len(self.site_failures)} of {self.sites_scraped} scraped "
                f"site(s) failed"
            )
            if unexpected:
                line += f", {unexpected} of them with an unexpected error"
            reasons.append(line + ".")
        if self.backstop_only:
            for hit in self.keyword_hits:
                reasons.append(
                    "The client email body that would have been sent contains "
                    f"{hit}."
                )
        elif self.keyword_hits:
            # Quoting these again would just restate the failures above in a
            # longer form; the fact that the backstop agrees is the only new
            # information, and it fits on one line.
            reasons.append(
                "The email-body backstop independently flagged the same "
                "failure text, as expected."
            )
        return tuple(reasons)


def scan_for_failure_text(body: str) -> tuple[str, ...]:
    """Scans a composed email body for failure text; returns what it found.

    The defense-in-depth backstop described in the module docstring. Returns
    a tuple of human-readable descriptions - one per offending *line*, each
    naming what was found and quoting (an abbreviated form of) the line, so
    the owner can see precisely what tripped the gate - or an empty tuple if
    the body looks clean.

    Only the plain-text alternative needs scanning: the HTML alternative is
    built from the same intro string and the same (url, status_line) pairs
    (see utility.email_digest._build_html_body), so it cannot contain failure
    text the plain-text body does not.

    Every URL in the body is masked before matching - see _URL_PATTERN for
    why - and matching is line-oriented so the description can quote the one
    offending line rather than the whole email.
    """
    hits = []
    for raw_line in body.splitlines():
        line = _URL_PATTERN.sub(_URL_MASK, raw_line).strip()
        if not line:
            continue
        for pattern, description in _FAILURE_PATTERNS:
            if pattern.search(line):
                if len(line) > _MAX_QUOTED_CHARS:
                    line = line[:_MAX_QUOTED_CHARS].rstrip() + "..."
                hits.append(f'{description}: "{line}"')
                break  # One finding per line; naming every pattern it also
                       # matches would say the same thing several times over.
        if len(hits) >= _MAX_REPORTED_HITS:
            break
    return tuple(hits)


def classify_run(
    rows: list[dict],
    site_statuses: list[tuple[str, str]],
    digest_path: str | None = None,
    client_body: str | None = None,
    pipeline_error: str | None = None,
    pipeline_traceback: str | None = None,
) -> RunHealthReport:
    """Decides whether a finished run may be emailed to the client.

    Args:
        rows: Every output row the run produced, across all sites (the same
            list cli.batch would hand to append_rows).
        site_statuses: The ordered (url, status_line) pairs destined for the
            email body - carried into the report verbatim so the alert can
            show the owner exactly what the client would have seen.
        digest_path: Path to this run's digest workbook if one was written,
            so the alert can attach it. A gated run still writes its digest:
            it is the artifact the owner inspects, and forwards by hand if
            the failure turns out to be benign.
        client_body: The rendered plain-text client email body, for the
            keyword backstop. Pass None to skip that check (e.g. when a
            crash means no body was ever composed).
        pipeline_error: A short "<ExceptionType>: <message>" description if
            the pipeline raised instead of returning. Any value here makes
            the run unhealthy on its own.
        pipeline_traceback: The full formatted traceback for that exception,
            included verbatim in the alert - this is the diagnostic that
            replaces digging through the Action log.

    Returns:
        A RunHealthReport. `healthy` is True only if all three checks pass:
        no failed row, no pipeline exception, and no failure text in the
        composed body.
    """
    failures = []
    for row in rows:
        status = str(row.get("status", ""))
        if not status.startswith(FAILED_STATUS_PREFIX):
            continue
        reason = status.split(":", 1)[1].strip() if ":" in status else status
        failures.append(
            SiteFailure(
                # source_url is on every row cli.batch builds; title is the
                # site URL on failure rows specifically (see
                # cli.batch._process_site_once's base_row), so it is a
                # faithful fallback rather than a placeholder.
                url=str(row.get("source_url") or row.get("title") or "(unknown site)"),
                reason=reason,
                unexpected=_UNEXPECTED_ERROR_MARKER in status,
            )
        )

    keyword_hits = scan_for_failure_text(client_body) if client_body else ()

    return RunHealthReport(
        healthy=not failures and not keyword_hits and pipeline_error is None,
        site_failures=tuple(failures),
        keyword_hits=keyword_hits,
        pipeline_error=pipeline_error,
        pipeline_traceback=pipeline_traceback,
        site_statuses=tuple(site_statuses),
        sites_scraped=len(site_statuses),
        new_event_count=sum(1 for row in rows if row.get("status") == "ok"),
        digest_path=digest_path,
        client_body=client_body,
    )


def report_for_crash(
    error: BaseException,
    traceback_text: str,
    rows: list[dict] | None = None,
    site_statuses: list[tuple[str, str]] | None = None,
    digest_path: str | None = None,
    client_body: str | None = None,
) -> RunHealthReport:
    """Builds an unhealthy report for a run that raised instead of finishing.

    A thin convenience wrapper over classify_run so there is still exactly
    one classification code path. `rows` and `site_statuses` are whatever the
    caller managed to collect before the exception - often nothing, which is
    fine: the traceback is the diagnostic that matters in this case.
    """
    return classify_run(
        rows or [],
        site_statuses or [],
        digest_path=digest_path,
        client_body=client_body,
        pipeline_error=f"{type(error).__name__}: {error}",
        pipeline_traceback=traceback_text,
    )
