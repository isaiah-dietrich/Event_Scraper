"""Weekly digest email: compose and send via Gmail.

After each weekly run, ``cli/batch.py`` writes a digest workbook of that
week's NEW events and hands it off here to be emailed to the client (Sarah)
from the user's Gmail account via SMTP, authenticated with a Gmail "app
password" (not the normal account password - that requires 2-Step
Verification to be enabled on the Google account; see
https://myaccount.google.com/apppasswords).

All configuration is read from environment variables *at send time* (inside
send_weekly_digest / _build_message's caller), never at import time, so that
importing this module never requires credentials to be present - e.g. so a
dry run or an unrelated unit test can import it freely.

Public API:
    send_weekly_digest(site_statuses, digest_path, dry_run=False)

Private helpers (exposed mainly for testing):
    _build_message(...) -> EmailMessage
    _build_html_body(intro, site_statuses, recipient_name) -> str
    _missing_env_vars() -> list[str]
"""

import datetime
import html
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

# Environment variables read at send time (not import time) - see module
# docstring. GMAIL_ADDRESS also doubles as the "From" header.
_ENV_GMAIL_ADDRESS = "GMAIL_ADDRESS"
_ENV_GMAIL_APP_PASSWORD = "GMAIL_APP_PASSWORD"
_ENV_RECIPIENT_EMAIL = "DIGEST_RECIPIENT_EMAIL"
_ENV_RECIPIENT_NAME = "DIGEST_RECIPIENT_NAME"

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
) -> EmailMessage:
    """Composes the weekly digest EmailMessage (subject, body, attachment).

    Pure/offline: does not touch the network or read environment variables -
    all recipient/sender details are passed in explicitly so this can be
    exercised directly in tests.
    """
    today = datetime.date.today()
    subject_date = f"{today.strftime('%B')} {today.day}, {today.year}"

    if digest_path is not None:
        intro = "Attached is a spreadsheet of all the new events found this week."
    else:
        intro = "No new events were found this week."

    lines = [f"Hi {recipient_name},", "", intro, "", "Websites scraped:"]
    for url, status_line in site_statuses:
        lines.append(f"- {url} — {status_line}")
    lines.append("")
    lines.append("Please reply to this email with any sites you'd like added or removed for next week.")
    lines.append("")
    lines.append("Thanks,")
    lines.append("Isaiah")
    body = "\n".join(lines)

    message = EmailMessage()
    message["Subject"] = f"Georgia AI Events – New Events for {subject_date}"
    message["From"] = sender_address
    message["To"] = recipient_email
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


def _print_dry_run(message: EmailMessage, digest_path: str | None) -> None:
    """Prints the composed email to stdout instead of sending it."""
    print("--- DRY RUN: email that would be sent ---")
    print(f"Subject: {message['Subject']}")
    print(f"From: {message['From']}")
    print(f"To: {message['To']}")
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
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.send_message(message)
