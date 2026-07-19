"""One-off: send a sample digest email to confirm the URLs render cleanly.

Sends a REAL email (SMTP is not billed - only scraping costs money) using
canned site statuses and no attachment, so you can eyeball the HTML formatting
in your inbox without running the full pipeline. Delete this file when done.

    source .env && python scratch_test_email.py
"""

from utility.email_digest import send_weekly_digest

SAMPLE_STATUSES = [
    ("https://ai.gatech.edu/events", "no new events"),
    ("https://members.tagonline.org/calendar", "no new events"),
    ("https://www.georgiamanufacturingalliance.com/events/", "2 new event(s)"),
    ("https://atlanta.aitinkerers.org/", "FAILED: blocked by bot protection (challenge page)"),
]

if __name__ == "__main__":
    send_weekly_digest(SAMPLE_STATUSES, digest_path=None, dry_run=False)
    print("Sample email sent - check your inbox to confirm the URLs are clean.")
