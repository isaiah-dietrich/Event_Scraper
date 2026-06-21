"""
AI event discovery prototype — single-site proof of concept.

Pipeline: FETCH (Playwright render) -> REDUCE (strip HTML) -> EXTRACT (Claude
returns structured JSON) -> SCORE (Claude rates fit) -> OUTPUT (console + JSON).
"""

import json
import os
import re
import sys

from playwright.sync_api import sync_playwright
from anthropic import Anthropic

TARGET_URL = "https://ai4.io/?utm_source=google&utm_campaign=ai4-search-campaign-a-phrase-match&utm_term=p&utm_content=tech%20conference&gad_source=1&gad_campaignid=23481927983&gbraid=0AAAAADQPaBIvs9KB5FT19FtCJWdrEgXTe&gclid=CjwKCAjw9NjRBhATEiwA_p2J8W3d3rsU4h1CFmYQqHMD-z2HNtcl6RuHeeaxyoJBMRrQSWZO58o_ARoCHhAQAvD_BwE"
MODEL = "claude-sonnet-4-6"

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

EXTRACTION_FIELDS = [
    "title",
    "date",
    "start_time",
    "location",
    "is_in_person",
    "signup_link",
    "short_description",
]


def fetch_rendered_html(url: str) -> str:
    """FETCH: load the JS-rendered page in (non-headless) Chromium and return its HTML."""
    try:
        with sync_playwright() as p:
            # Headless=False and a realistic user agent because some sites
            # (e.g. Cloudflare-protected pages) detect and block headless
            # automation outright.
            browser = p.chromium.launch(headless=False)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
            )
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Give client-side rendered widgets (and any bot-check challenge)
            # a moment to resolve. Using a fixed wait instead of "networkidle"
            # since some pages (ad pixels, trackers, polling widgets) never
            # go fully idle.
            page.wait_for_timeout(8000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        raise RuntimeError(f"Failed to load page with Playwright: {e}") from e


def reduce_html(html: str) -> str:
    """REDUCE: strip tags/scripts/styles down to visible text to cut tokens and noise."""
    # Drop script/style/svg/noscript blocks entirely.
    html = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Drop HTML comments.
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    # Strip all remaining tags, keeping their text content.
    text = re.sub(r"<[^>]+>", "\n", html)
    # Collapse excess whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract_events(client: Anthropic, page_text: str) -> list:
    """EXTRACT: ask Claude to return a strict JSON array of event objects."""
    prompt = f"""You are given the visible text content of a webpage. This page may be a
dedicated events calendar, or it may be a general site (company homepage, blog post,
news article, etc.) that only mentions one or a few events in passing — for example a
sentence noting the company will be at a conference, or a banner about an upcoming
webinar. Find every distinct real-world or virtual event mentioned anywhere in the
text, no matter how small a portion of the page it occupies.

Return ONLY a valid JSON array (no prose, no markdown code fences, no explanation)
where each element is an object with exactly these fields: {", ".join(EXTRACTION_FIELDS)}.

Rules:
- "is_in_person" must be a JSON boolean (true/false), inferred from the event details.
- If a field is unknown/missing, use an empty string "" (or false for is_in_person).
- "signup_link" should be the registration/details URL if present, else "".
- Do not invent events that are not in the text.
- If the page does not mention any events at all, return an empty JSON array [].

PAGE CONTENT:
{page_text}
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    return safe_parse_json_array(raw)


def safe_parse_json_array(raw: str) -> list:
    """Parse model output as a JSON array, tolerating stray fences/whitespace."""
    cleaned = raw.strip()
    # Strip markdown fences if the model added them despite instructions.
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {e}\nRaw output:\n{raw[:1000]}") from e
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array of events, got: {type(data)}")
    return data


def score_event(client: Anthropic, event: dict) -> dict:
    """SCORE: ask Claude to rate fit 1-5 with a one-sentence reason."""
    prompt = f"""Rate how well this event fits the criteria below, on a scale of 1
(poor fit) to 5 (excellent fit). Use these criteria:
{SCORING_CRITERIA}

Event details (JSON):
{json.dumps(event)}

Return ONLY a valid JSON object (no prose, no markdown fences) with exactly
two fields: "score" (integer 1-5) and "reason" (a single sentence).
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
        score = int(result.get("score", 0))
        reason = str(result.get("reason", "")).strip()
    except (json.JSONDecodeError, ValueError, TypeError):
        score, reason = 0, "Could not parse model score; defaulted to 0."
    return {"score": score, "reason": reason}


def main():
    # --debug: print the reduced text that would be sent to the LLM, then exit
    # before making any API calls. Useful for checking what the extraction
    # prompt actually sees.
    debug = "--debug" in sys.argv

    if not debug:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
            sys.exit(1)
        client = Anthropic(api_key=api_key)

    print(f"Fetching rendered page: {TARGET_URL}")
    try:
        html = fetch_rendered_html(TARGET_URL)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("Reducing HTML to visible text...")
    page_text = reduce_html(html)

    if debug:
        print("\n=== DEBUG: reduced page text sent to the LLM ===\n")
        print(page_text)
        print(f"\n=== DEBUG: {len(page_text)} characters total ===")
        sys.exit(0)
    if not page_text:
        print("ERROR: Reduced page text is empty; nothing to extract.", file=sys.stderr)
        sys.exit(1)

    print("Extracting events with Claude...")
    try:
        events = extract_events(client, page_text)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not events:
        print("No events were extracted from the page. Exiting.")
        sys.exit(0)

    print(f"Extracted {len(events)} event(s). Scoring each for fit...")
    results = []
    for event in events:
        scoring = score_event(client, event)
        merged = dict(event)
        merged["fit_score"] = scoring["score"]
        merged["fit_reason"] = scoring["reason"]
        results.append(merged)

    results.sort(key=lambda e: e.get("fit_score", 0), reverse=True)

    with open("events.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n=== Event Fit Summary (highest first) ===\n")
    for e in results:
        print(f"[{e.get('fit_score', '?')}/5] {e.get('title', 'Untitled')}")
        print(f"    Date: {e.get('date', '?')}  Time: {e.get('start_time', '?')}")
        print(f"    Location: {e.get('location', '?')}  In-person: {e.get('is_in_person', '?')}")
        print(f"    Signup: {e.get('signup_link', '')}")
        print(f"    Reason: {e.get('fit_reason', '')}")
        print()

    print(f"Wrote {len(results)} event(s) to events.json")


if __name__ == "__main__":
    main()
