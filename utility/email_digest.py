"""Weekly digest email: compose and send via Gmail.

After each weekly run, ``cli/batch.py`` writes a digest workbook of that
week's NEW events and hands it off here to be emailed to the client (Sarah)
from the user's Gmail account via SMTP, authenticated with a Gmail "app
password" (not the normal account password - that requires 2-Step
Verification to be enabled on the Google account; see
https://myaccount.google.com/apppasswords).

Two different emails are composed here, from the same Gmail account and over
the same SMTP plumbing:

- The **client digest** (send_weekly_digest), sent only when the run-health
  gate says the run is healthy (see utility.run_health).
- The **owner alert** (send_health_alert), sent *instead* when it is not.
  The client never receives anything on that path - the whole point is that
  a run with error text in it stops here and is reported to the owner
  instead. See OWNER_ALERT_EMAIL and _assert_owner_only_recipient.

All configuration is read from environment variables *at send time* (inside
send_weekly_digest / send_health_alert), never at import time, so that
importing this module never requires credentials to be present - e.g. so a
dry run or an unrelated unit test can import it freely.

Public API:
    send_weekly_digest(site_statuses, digest_path, dry_run=False)
    send_health_alert(report, dry_run=False)
    render_client_body(site_statuses, digest_path) -> str

Private helpers (exposed mainly for testing):
    _build_message(...) -> EmailMessage
    _build_text_body(intro, site_statuses, recipient_name) -> str
    _build_html_body(intro, site_statuses, recipient_name) -> str
    _build_alert_message(report, sender_address) -> EmailMessage
    _missing_env_vars() -> list[str]
    _missing_alert_env_vars() -> list[str]
"""

import datetime
import html
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from utility.run_health import RunHealthReport

# Where a gated run's alert goes. Deliberately a hardcoded module constant
# rather than a GitHub secret / env var, at the owner's explicit request: it
# is the owner's own address, it never varies per-environment, and an alert
# that silently fails to send because a secret was never configured would
# defeat the entire purpose of having an alert. (Everything else here is
# env-driven; this one is the deliberate exception.) run_health imports
# nothing from this module, so this import direction stays acyclic.
OWNER_ALERT_EMAIL = "isaiahdietrich@gmail.com"

# Environment variables read at send time (not import time) - see module
# docstring. GMAIL_ADDRESS also doubles as the "From" header.
_ENV_GMAIL_ADDRESS = "GMAIL_ADDRESS"
_ENV_GMAIL_APP_PASSWORD = "GMAIL_APP_PASSWORD"
_ENV_RECIPIENT_EMAIL = "DIGEST_RECIPIENT_EMAIL"
_ENV_RECIPIENT_NAME = "DIGEST_RECIPIENT_NAME"

# Optional: one or more comma-separated addresses to Cc on the digest email
# (e.g. "isaiah@example.com, someone-else@example.com"). Unlike the four
# required env vars above, a blank/unset value is a valid steady-state - it
# just means no Cc header is added - so this is deliberately not part of
# _missing_env_vars.
_ENV_CC_EMAIL = "DIGEST_CC_EMAIL"

# Placeholders shown in dry-run output when the real env var isn't set, so a
# dry run is still readable without any configuration in place.
_PLACEHOLDERS = {
    _ENV_GMAIL_ADDRESS: "<GMAIL_ADDRESS>",
    _ENV_GMAIL_APP_PASSWORD: "<GMAIL_APP_PASSWORD>",
    _ENV_RECIPIENT_EMAIL: "<DIGEST_RECIPIENT_EMAIL>",
    _ENV_RECIPIENT_NAME: "<DIGEST_RECIPIENT_NAME>",
}

_ATTACHMENT_MAINTYPE = "application"
_ATTACHMENT_SUBTYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _missing_env_vars() -> list[str]:
    """Returns the names of any required env vars that aren't set (non-empty)."""
    required = [
        _ENV_GMAIL_ADDRESS,
        _ENV_GMAIL_APP_PASSWORD,
        _ENV_RECIPIENT_EMAIL,
        _ENV_RECIPIENT_NAME,
    ]
    return [name for name in required if not os.environ.get(name)]


def _intro_line(digest_path: str | None) -> str:
    """The one-sentence lead of the digest email.

    Shared by the plain-text and HTML bodies (and by render_client_body, so
    the run-health gate scans exactly the sentence that would be sent)
    rather than being written out twice, which is how the two alternatives
    would eventually drift apart.
    """
    if digest_path is not None:
        return "Attached is a spreadsheet of all the new events found this week."
    return "No new events were found this week."


def _build_text_body(
    intro: str,
    site_statuses: list[tuple[str, str]],
    recipient_name: str,
) -> str:
    """Renders the plain-text alternative of the digest email.

    This wording is the agreed-upon client-facing template - edit it here to
    change what the recipient reads. Kept as its own function (mirroring
    _build_html_body) so utility.run_health can be handed the exact body
    that would go out, without _build_message's sender/recipient arguments
    or its attachment read.
    """
    lines = [f"Hi {recipient_name},", "", intro, "", "Websites scraped:"]
    for url, status_line in site_statuses:
        lines.append(f"- {url} — {status_line}")
    lines.append("")
    lines.append("Please reply to this email with any sites you'd like added or removed for next week.")
    lines.append("")
    lines.append("Thanks,")
    lines.append("Isaiah")
    return "\n".join(lines)


def _build_html_body(
    intro: str,
    site_statuses: list[tuple[str, str]],
    recipient_name: str,
) -> str:
    """Renders the HTML alternative of the digest email.

    Each scraped site is an <a> whose visible text is the clean URL, so a
    recipient behind a link-rewriting gateway still sees a readable link (see
    _build_message for why the plain-text version alone isn't enough). All
    interpolated values are HTML-escaped - an "&" in a URL would otherwise
    produce broken markup.
    """
    items = []
    for url, status_line in site_statuses:
        safe_url = html.escape(url, quote=True)
        safe_status = html.escape(status_line, quote=True)
        items.append(
            f'    <li><a href="{safe_url}">{safe_url}</a> — {safe_status}</li>'
        )
    list_html = "\n".join(items)
    return (
        "<html><body>"
        f"<p>Hi {html.escape(recipient_name, quote=True)},</p>"
        f"<p>{html.escape(intro, quote=True)}</p>"
        "<p>Websites scraped:</p>"
        f"<ul>\n{list_html}\n</ul>"
        "<p>Please reply to this email with any sites you'd like added or "
        "removed for next week.</p>"
        "<p>Thanks,<br>Isaiah</p>"
        "</body></html>"
    )


def _build_message(
    site_statuses: list[tuple[str, str]],
    digest_path: str | None,
    sender_address: str,
    recipient_email: str,
    recipient_name: str,
    cc_email: str = "",
) -> EmailMessage:
    """Composes the weekly digest EmailMessage (subject, body, attachment).

    Pure/offline: does not touch the network or read environment variables -
    all recipient/sender details are passed in explicitly so this can be
    exercised directly in tests.

    Args:
        cc_email: One or more comma-separated addresses for the "Cc" header,
            or "" to omit it entirely (the common case - most weeks have no
            Cc). smtplib.send_message reads recipients from the message's
            To/Cc headers automatically, so setting this is sufficient to
            actually deliver to the Cc'd address(es) too, not just display
            them.
    """
    today = datetime.date.today()
    subject_date = f"{today.strftime('%B')} {today.day}, {today.year}"

    intro = _intro_line(digest_path)
    body = _build_text_body(intro, site_statuses, recipient_name)

    message = EmailMessage()
    message["Subject"] = f"Georgia AI Events – New Events for {subject_date}"
    message["From"] = sender_address
    message["To"] = recipient_email
    if cc_email:
        message["Cc"] = cc_email
    message.set_content(body)

    # Also attach an HTML alternative. In a plain-text-only email the recipient's
    # mail client auto-linkifies each bare URL, and link-rewriting security
    # gateways (Proofpoint URL Defense at wisc.edu and many corporate domains)
    # then rewrite that VISIBLE text into a long "urldefense.com/v3/__..." wrapper
    # - so the reader sees a wall of gibberish instead of the site. In HTML the
    # URL lives in an <a href> while the anchor text stays the clean URL; the
    # gateway rewrites only the href, so the reader still sees a tidy link.
    message.add_alternative(
        _build_html_body(intro, site_statuses, recipient_name), subtype="html"
    )

    if digest_path is not None:
        attachment_bytes = Path(digest_path).read_bytes()
        filename = Path(digest_path).name
        message.add_attachment(
            attachment_bytes,
            maintype=_ATTACHMENT_MAINTYPE,
            subtype=_ATTACHMENT_SUBTYPE,
            filename=filename,
        )

    return message


def render_client_body(
    site_statuses: list[tuple[str, str]],
    digest_path: str | None,
) -> str:
    """Returns the exact plain-text body the client digest email would carry.

    Exists for the run-health gate (utility.run_health.scan_for_failure_text),
    which needs to scan the *rendered* body - not a re-derivation of it -
    before that body is allowed anywhere near the client. It goes through the
    same _intro_line/_build_text_body that _build_message uses, so the two can
    never disagree about what is in the email.

    Pure and offline: touches no network, requires no credentials. The
    recipient's name is read from the environment if it happens to be set
    (purely so the greeting line looks realistic) and falls back to the same
    placeholder a dry run shows, so this is safe to call with nothing
    configured at all.
    """
    recipient_name = (
        os.environ.get(_ENV_RECIPIENT_NAME) or _PLACEHOLDERS[_ENV_RECIPIENT_NAME]
    )
    return _build_text_body(
        _intro_line(digest_path), site_statuses, recipient_name
    )


def _print_dry_run(
    message: EmailMessage,
    digest_path: str | None,
    label: str = "email that would be sent",
) -> None:
    """Prints the composed email to stdout instead of sending it.

    `label` names which email this is, so a dry run of a gated run is
    obviously the owner alert and not the client digest - the two look
    similar enough at a glance to be worth labelling.
    """
    print(f"--- DRY RUN: {label} ---")
    print(f"Subject: {message['Subject']}")
    print(f"From: {message['From']}")
    print(f"To: {message['To']}")
    if message["Cc"]:
        print(f"Cc: {message['Cc']}")
    attachment_name = Path(digest_path).name if digest_path is not None else "(no attachment)"
    print(f"Attachment: {attachment_name}")
    print("")
    # message is multipart/mixed whenever an attachment is present, and
    # EmailMessage.get_content() only has a content manager for a message's
    # own (non-multipart) payload - get_body() walks to the plain-text part.
    print(message.get_body(preferencelist=("plain",)).get_content())
    print("--- END DRY RUN ---")


def send_weekly_digest(
    site_statuses: list[tuple[str, str]],
    digest_path: str | None,
    dry_run: bool = False,
) -> None:
    """Sends (or, with dry_run, prints) the weekly digest email to the client.

    Args:
        site_statuses: ordered (url, status_line) pairs, one per scraped
            site, rendered verbatim as "- {url} — {status_line}".
        digest_path: path to the .xlsx attachment for this week's new
            events, or None if no new events were found this week (the email
            is still sent, without an attachment, so the client can tell
            "nothing new" apart from "the run never happened").
        dry_run: if True, never touches the network and never requires
            credentials - prints the full subject/body/attachment name to
            stdout and returns.

    Raises:
        RuntimeError: (non-dry-run only) if any required environment
            variable is missing, naming exactly which one(s).
        smtplib.SMTPException / OSError subclasses: propagated verbatim from
            smtplib if the real send fails, so the caller can tell the user
            how to resend manually.
    """
    if dry_run:
        sender_address = os.environ.get(_ENV_GMAIL_ADDRESS) or _PLACEHOLDERS[_ENV_GMAIL_ADDRESS]
        recipient_email = os.environ.get(_ENV_RECIPIENT_EMAIL) or _PLACEHOLDERS[_ENV_RECIPIENT_EMAIL]
        recipient_name = os.environ.get(_ENV_RECIPIENT_NAME) or _PLACEHOLDERS[_ENV_RECIPIENT_NAME]
        message = _build_message(
            site_statuses,
            digest_path,
            sender_address=sender_address,
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            cc_email=os.environ.get(_ENV_CC_EMAIL, ""),
        )
        _print_dry_run(message, digest_path)
        return

    missing = _missing_env_vars()
    if missing:
        raise RuntimeError(
            "ERROR: missing required environment variable(s): "
            + ", ".join(missing)
            + ". GMAIL_APP_PASSWORD must be a Gmail \"app password\" (Google Account > "
            "Security > 2-Step Verification > App passwords - requires 2-Step "
            "Verification to be enabled), NOT the normal account password."
        )

    gmail_address = os.environ[_ENV_GMAIL_ADDRESS]
    gmail_app_password = os.environ[_ENV_GMAIL_APP_PASSWORD]
    recipient_email = os.environ[_ENV_RECIPIENT_EMAIL]
    recipient_name = os.environ[_ENV_RECIPIENT_NAME]

    message = _build_message(
        site_statuses,
        digest_path,
        sender_address=gmail_address,
        recipient_email=recipient_email,
        recipient_name=recipient_name,
        cc_email=os.environ.get(_ENV_CC_EMAIL, ""),
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(message)



# ---------------------------------------------------------------------------
# Owner alert: what gets sent INSTEAD of the client digest on a gated run.
# ---------------------------------------------------------------------------

# The alert needs only the sending account - not the client's address or
# name. That is deliberate: a run gated *because* DIGEST_RECIPIENT_EMAIL is
# missing must still be able to tell the owner so.
def _missing_alert_env_vars() -> list[str]:
    """Returns the names of any env vars the owner alert needs but lacks."""
    required = [_ENV_GMAIL_ADDRESS, _ENV_GMAIL_APP_PASSWORD]
    return [name for name in required if not os.environ.get(name)]


# Shared alert prose, defined once and rendered into both the plain-text and
# HTML alternatives below, so the two cannot drift apart the way two
# hand-maintained copies of the same paragraph eventually would.
_ALERT_LEAD = (
    "This week's Georgia AI Events run finished with errors, so the client "
    "digest email was NOT sent. Nobody else received this - you are the only "
    "recipient."
)
_ALERT_NEXT_STEPS = (
    "The master workbook (events_master.xlsx) was deliberately left "
    "untouched. A gated run reported nothing to the client, so recording its "
    "events as already-seen would make every later run skip them and the "
    "client would never hear about them at all. Because it was left alone, "
    "the next run rediscovers these same events and reports them normally - "
    "doing nothing loses nothing.",
    "If the failures below turn out to be harmless, you can forward the "
    "attached spreadsheet to the client yourself. Expect those same events to "
    "show up once more in the next digest if you do, since the master has no "
    "record of them having been sent.",
)
_ALERT_BODY_FENCE = "-" * 60


def _alert_summary_pairs(report: RunHealthReport) -> list[tuple[str, str]]:
    """The alert's "Run summary" label/value pairs, in display order."""
    if report.digest_path:
        digest = f"{report.digest_path} (attached)"
    else:
        digest = "none written (no new events, or the run failed before the write)"
    return [
        ("Sites scraped", str(report.sites_scraped)),
        ("Sites failed", str(len(report.site_failures))),
        ("New events found", str(report.new_event_count)),
        ("Digest workbook", digest),
        ("Master workbook", "NOT updated - see above"),
    ]


def _build_alert_text_body(report: RunHealthReport) -> str:
    """Renders the plain-text alternative of the owner alert.

    Written to be the thing that replaces opening the Action log: every
    failure's error text is included in full and never truncated, and the
    client email that was withheld is quoted verbatim at the end so the owner
    can see exactly what the gate stopped.
    """
    lines = ["Hi Isaiah,", "", _ALERT_LEAD, ""]

    lines.append("WHY THIS RUN WAS GATED")
    for reason in report.gate_reasons:
        lines.append(f"  - {reason}")
    lines.append("")

    lines.append("WHAT HAPPENS NEXT")
    for paragraph in _ALERT_NEXT_STEPS:
        lines.append(f"  {paragraph}")
        lines.append("")

    lines.append("RUN SUMMARY")
    for label, value in _alert_summary_pairs(report):
        lines.append(f"  - {label}: {value}")
    lines.append("")

    if report.site_failures:
        lines.append("WHAT FAILED (full error text)")
        for index, failure in enumerate(report.site_failures, start=1):
            marker = " [unexpected error]" if failure.unexpected else ""
            lines.append(f"  {index}. {failure.url}{marker}")
            lines.append(f"     {failure.reason}")
        lines.append("")

    if report.pipeline_error:
        lines.append("THE PIPELINE ITSELF RAISED")
        lines.append(f"  {report.pipeline_error}")
        if report.pipeline_traceback:
            lines.append("")
            for traceback_line in report.pipeline_traceback.rstrip().splitlines():
                lines.append(f"  {traceback_line}")
        lines.append("")

    if report.backstop_only:
        # Only when the backstop stands alone (see RunHealthReport.
        # backstop_only) - otherwise this section would repeat, at length,
        # error text the failure section above already printed in full.
        lines.append("FAILURE TEXT FOUND IN THE COMPOSED CLIENT EMAIL")
        lines.append("  No site reported a failure, yet the body below still")
        lines.append("  contains failure text - which means some code path is")
        lines.append("  producing error text without a \"failed:\" status.")
        for hit in report.keyword_hits:
            lines.append(f"  - {hit}")
        lines.append("")

    if report.site_statuses:
        lines.append("PER-SITE STATUS (the list the client would have seen)")
        for url, status_line in report.site_statuses:
            lines.append(f"  - {url} — {status_line}")
        lines.append("")

    if report.client_body:
        lines.append("THE CLIENT EMAIL THAT WAS WITHHELD")
        lines.append(_ALERT_BODY_FENCE)
        lines.extend(report.client_body.splitlines())
        lines.append(_ALERT_BODY_FENCE)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_alert_html_body(report: RunHealthReport) -> str:
    """Renders the HTML alternative of the owner alert.

    Same reason the digest email has one (see _build_message): the alert is
    full of URLs, and in a plain-text-only email a link-rewriting gateway
    mangles the visible text of every one of them. Error text and the quoted
    client email go in <pre> blocks so their line breaks and indentation
    survive. Everything interpolated is HTML-escaped - error messages in
    particular are arbitrary text straight from an API and routinely contain
    "&" and "<".
    """
    def escape(value: str) -> str:
        return html.escape(str(value), quote=True)

    parts = [
        "<html><body>",
        "<p>Hi Isaiah,</p>",
        f"<p><strong>{escape(_ALERT_LEAD)}</strong></p>",
        "<h3>Why this run was gated</h3>",
        "<ul>",
    ]
    parts.extend(f"<li>{escape(reason)}</li>" for reason in report.gate_reasons)
    parts.append("</ul>")

    parts.append("<h3>What happens next</h3>")
    parts.extend(f"<p>{escape(paragraph)}</p>" for paragraph in _ALERT_NEXT_STEPS)

    parts.append("<h3>Run summary</h3><ul>")
    parts.extend(
        f"<li>{escape(label)}: {escape(value)}</li>"
        for label, value in _alert_summary_pairs(report)
    )
    parts.append("</ul>")

    if report.site_failures:
        parts.append("<h3>What failed (full error text)</h3><ol>")
        for failure in report.site_failures:
            marker = " <em>[unexpected error]</em>" if failure.unexpected else ""
            safe_url = escape(failure.url)
            parts.append(
                f'<li><a href="{safe_url}">{safe_url}</a>{marker}'
                f'<pre style="white-space:pre-wrap">{escape(failure.reason)}</pre></li>'
            )
        parts.append("</ol>")

    if report.pipeline_error:
        parts.append("<h3>The pipeline itself raised</h3>")
        parts.append(f"<p>{escape(report.pipeline_error)}</p>")
        if report.pipeline_traceback:
            parts.append(
                '<pre style="white-space:pre-wrap">'
                f"{escape(report.pipeline_traceback)}</pre>"
            )

    if report.backstop_only:
        parts.append("<h3>Failure text found in the composed client email</h3>")
        parts.append(
            "<p>No site reported a failure, yet the body below still contains "
            "failure text - which means some code path is producing error "
            'text without a "failed:" status.</p><ul>'
        )
        parts.extend(f"<li>{escape(hit)}</li>" for hit in report.keyword_hits)
        parts.append("</ul>")

    if report.site_statuses:
        parts.append("<h3>Per-site status (the list the client would have seen)</h3><ul>")
        for url, status_line in report.site_statuses:
            safe_url = escape(url)
            parts.append(
                f'<li><a href="{safe_url}">{safe_url}</a> — {escape(status_line)}</li>'
            )
        parts.append("</ul>")

    if report.client_body:
        parts.append("<h3>The client email that was withheld</h3>")
        parts.append(
            '<pre style="white-space:pre-wrap">'
            f"{escape(report.client_body)}</pre>"
        )

    parts.append("</body></html>")
    return "".join(parts)


def _message_recipients(message: EmailMessage) -> list[str]:
    """Every address smtplib.send_message would actually deliver `message` to.

    Mirrors smtplib's own rule (it derives the envelope recipients from the
    To, Cc and Bcc headers), so this is a faithful preview of who would
    receive the message rather than an approximation of it.
    """
    addresses = []
    for header in ("To", "Cc", "Bcc"):
        for value in message.get_all(header, []):
            addresses.extend(
                part.strip() for part in str(value).split(",") if part.strip()
            )
    return addresses


def _assert_owner_only_recipient(message: EmailMessage) -> None:
    """Raises unless `message` goes to OWNER_ALERT_EMAIL and nobody else.

    The single most important property of the whole gate is that the client
    does not receive the alert, and the likeliest way to break it is a Cc
    header - DIGEST_CC_EMAIL is set in production, smtplib reads recipients
    straight out of the headers, and copy-pasting the digest's composition
    code would bring that Cc along silently. So rather than trusting that
    _build_alert_message never sets one, this checks the finished message and
    refuses to hand it to smtplib if anything but the owner's address is on
    it. Called from _build_alert_message itself, so an alert message that
    could leak cannot even be constructed.
    """
    recipients = _message_recipients(message)
    if recipients != [OWNER_ALERT_EMAIL]:
        raise RuntimeError(
            "ERROR: refusing to send the run-health alert - it must go to "
            f"exactly [{OWNER_ALERT_EMAIL}], but its headers resolve to "
            f"{recipients}. The client must never receive this email."
        )


def _build_alert_message(
    report: RunHealthReport,
    sender_address: str,
) -> EmailMessage:
    """Composes the owner-alert EmailMessage for a gated run.

    Pure/offline apart from reading the digest file it attaches: no network,
    no environment variables (the sender is passed in), so this can be
    exercised directly in tests.

    Note what is deliberately absent: any Cc header, and any use of
    DIGEST_RECIPIENT_EMAIL / DIGEST_RECIPIENT_NAME. The only recipient is
    OWNER_ALERT_EMAIL, and _assert_owner_only_recipient enforces that on the
    finished message before it is returned.
    """
    today = datetime.date.today()
    subject_date = f"{today.strftime('%B')} {today.day}, {today.year}"

    message = EmailMessage()
    message["Subject"] = (
        f"[Georgia AI Events] Run had errors – client digest NOT sent "
        f"({subject_date})"
    )
    message["From"] = sender_address
    message["To"] = OWNER_ALERT_EMAIL
    message.set_content(_build_alert_text_body(report))
    message.add_alternative(_build_alert_html_body(report), subtype="html")

    # Attach the digest if this run got far enough to write one, so the owner
    # can inspect the events and forward the file by hand if the failures
    # turn out to be benign. is_file() rather than a bare truthiness check:
    # a missing file must not turn "the run had errors" into "the alert about
    # the run had errors also failed".
    if report.digest_path and Path(report.digest_path).is_file():
        message.add_attachment(
            Path(report.digest_path).read_bytes(),
            maintype=_ATTACHMENT_MAINTYPE,
            subtype=_ATTACHMENT_SUBTYPE,
            filename=Path(report.digest_path).name,
        )

    _assert_owner_only_recipient(message)
    return message


def send_health_alert(report: RunHealthReport, dry_run: bool = False) -> None:
    """Sends (or, with dry_run, prints) the owner alert for a gated run.

    Called by cli.batch instead of send_weekly_digest when
    utility.run_health says the run is unhealthy. The client digest email is
    not sent at all in that case - not to the client, not to DIGEST_CC_EMAIL,
    not to anyone.

    Args:
        report: The run's RunHealthReport (see utility.run_health). Expected
            to be an unhealthy one; nothing here checks, because the decision
            of whether to alert belongs to the caller.
        dry_run: if True, never touches the network and never requires
            credentials - prints the full alert to stdout and returns, same
            contract as send_weekly_digest's dry_run.

    Raises:
        RuntimeError: (non-dry-run only) if GMAIL_ADDRESS or
            GMAIL_APP_PASSWORD is missing, or if the composed message would
            reach anyone other than OWNER_ALERT_EMAIL.
        smtplib.SMTPException / OSError subclasses: propagated verbatim from
            smtplib if the real send fails.
    """
    if dry_run:
        sender_address = (
            os.environ.get(_ENV_GMAIL_ADDRESS) or _PLACEHOLDERS[_ENV_GMAIL_ADDRESS]
        )
        message = _build_alert_message(report, sender_address=sender_address)
        _print_dry_run(
            message, report.digest_path, label="run-health alert that would be sent"
        )
        return

    missing = _missing_alert_env_vars()
    if missing:
        raise RuntimeError(
            "ERROR: cannot send the run-health alert - missing required "
            "environment variable(s): " + ", ".join(missing)
        )

    gmail_address = os.environ[_ENV_GMAIL_ADDRESS]
    gmail_app_password = os.environ[_ENV_GMAIL_APP_PASSWORD]
    message = _build_alert_message(report, sender_address=gmail_address)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(message)
