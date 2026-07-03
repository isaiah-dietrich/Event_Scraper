import datetime

import pytest

import cli.batch as batch


# --- _extract_us_state -------------------------------------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Atlanta, GA", "GA"),
        ("Atlanta, Georgia", "GA"),
        ("Georgia", "GA"),
        ("Sandy Springs Performing Arts Center, Sandy Springs, Georgia", "GA"),
        ("San Francisco, CA", "CA"),
        ("Manchester, NH", "NH"),
        ("New York, NY, United States", "NY"),
        ("Charleston, West Virginia", "WV"),
        ("Charleston, Virginia", "VA"),
        ("Marietta, GA (contact info@skelora.com for venue information)", "GA"),
        ("Austin, TX (map link)", "TX"),
        ("Washington, DC", "DC"),
        ("Atlanta, GA 30309, USA", "GA"),
        ("123 Main St, Springfield, IL", "IL"),
        # No confident US state: kept for AI scoring rather than guessed at.
        ("Georgia Tech Savannah", None),
        ("Texas Roadhouse", None),
        ("Washington Avenue Ballroom", None),
        ("Georgia Institute of Technology", None),
        ("Islamabad, Pakistan", None),
        ("Minneapolis Saint Paul", None),
        ("Zürich", None),
        ("Virtual", None),
        ("Virtual/Instructor-led", None),
        ("Online", None),
        ("", None),
    ],
)
def test_extract_us_state(location, expected):
    assert batch._extract_us_state(location) == expected


# --- _extract_foreign_country -----------------------------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Mumbai, India", "India"),
        ("Islamabad, Pakistan", "Pakistan"),
        ("Singapore", "Singapore"),
        ("Toronto, Canada", "Canada"),
        ("Hlavní město Praha, Czechia", "Czechia"),
        ("Nairobi, Kenya", "Kenya"),
        ("Wien, Austria", "Austria"),
        ("Adelaide, Australia", "Australia"),
        ("Barcelona, Spain (venue TBD)", "Spain"),
        # No confident non-US country: kept for AI scoring rather than
        # guessed at.
        ("Atlanta, GA", None),
        ("Atlanta, Georgia", None),  # US state, not the country
        ("Virtual", None),
        ("Zoom", None),
        ("Google Meet", None),
        ("Register to See Location", None),
        ("France Cafe", None),  # brand name, not a comma-separated country
        ("", None),
    ],
)
def test_extract_foreign_country(location, expected):
    assert batch._extract_foreign_country(location) == expected


# --- _split_by_state -----------------------------------------------------


def test_split_by_state_routes_non_target_state_to_auto_rejected():
    events = [
        {"title": "Texas Event", "location": "Austin, TX"},
        {"title": "Georgia Event", "location": "Atlanta, GA"},
    ]

    needs_scoring, auto_rejected = batch._split_by_state(events)

    assert needs_scoring == [events[1]]
    assert len(auto_rejected) == 1
    rejected_event, reason = auto_rejected[0]
    assert rejected_event == events[0]
    assert "Texas" in reason
    assert "auto-rejected" in reason


def test_split_by_state_routes_foreign_country_to_auto_rejected():
    events = [
        {"title": "India Event", "location": "Mumbai, India"},
        {"title": "Georgia Event", "location": "Atlanta, GA"},
    ]

    needs_scoring, auto_rejected = batch._split_by_state(events)

    assert needs_scoring == [events[1]]
    assert len(auto_rejected) == 1
    rejected_event, reason = auto_rejected[0]
    assert rejected_event == events[0]
    assert "India" in reason
    assert "auto-rejected" in reason


def test_split_by_state_keeps_unknown_location_for_scoring():
    events = [{"title": "Mystery Event", "location": "Virtual"}]

    needs_scoring, auto_rejected = batch._split_by_state(events)

    assert needs_scoring == events
    assert auto_rejected == []


def test_split_by_state_keeps_missing_location_for_scoring():
    events = [{"title": "No Location Event"}]

    needs_scoring, auto_rejected = batch._split_by_state(events)

    assert needs_scoring == events
    assert auto_rejected == []


# --- _filter_past_events ---------------------------------------------------


def test_filter_past_events_drops_events_dated_in_the_past():
    events = [{"title": "Old Event", "date": "January 1, 2000"}]

    assert batch._filter_past_events(events) == []


def test_filter_past_events_keeps_events_dated_in_the_future():
    events = [{"title": "Future Event", "date": "January 1, 2099"}]

    result = batch._filter_past_events(events)

    assert len(result) == 1
    assert result[0]["title"] == "Future Event"


def test_filter_past_events_converts_date_to_real_datetime():
    events = [{"title": "Future Event", "date": "2099-03-07"}]

    result = batch._filter_past_events(events)

    assert result[0]["date"] == datetime.datetime(2099, 3, 7)
    assert isinstance(result[0]["date"], datetime.datetime)


def test_filter_past_events_keeps_events_with_empty_date():
    events = [{"title": "No Date Event", "date": ""}]

    assert batch._filter_past_events(events) == events


def test_filter_past_events_keeps_events_missing_date_key():
    events = [{"title": "No Date Key Event"}]

    assert batch._filter_past_events(events) == events


def test_filter_past_events_keeps_unparseable_date_and_warns(capsys):
    events = [{"title": "Weird Date Event", "date": "not a real date at all"}]

    result = batch._filter_past_events(events)

    assert result == events
    assert "could not parse event date" in capsys.readouterr().out


def test_filter_past_events_yearless_date_resolves_to_future():
    events = [{"title": "Yearless Event", "date": "January 10"}]

    result = batch._filter_past_events(events)

    assert len(result) == 1
    assert result[0]["date"].date() >= datetime.date.today()


# --- _timestamp ------------------------------------------------------------


def test_timestamp_matches_todays_date_format():
    today = datetime.date.today()
    expected = f"{today:%B} {today.day}, {today:%Y}"

    assert batch._timestamp() == expected


# --- process_site ------------------------------------------------------


def _stub_extraction_fields(title, location="", date=""):
    return {field: "" for field in batch.EXTRACTION_FIELDS} | {
        "title": title,
        "location": location,
        "date": date,
    }


def test_process_site_returns_failure_row_on_fetch_error(monkeypatch):
    def fake_fetch(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(batch, "fetch_rendered_html", fake_fetch)

    rows = batch.process_site(client=object(), url="https://example.com")

    assert len(rows) == 1
    assert rows[0]["status"] == "failed: boom"
    assert rows[0]["source_url"] == "https://example.com"


def test_process_site_returns_failure_row_on_empty_page_text(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html></html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "")

    rows = batch.process_site(client=object(), url="https://example.com")

    assert rows == [{
        "scraped_at": batch._timestamp(),
        "source_url": "https://example.com",
        "status": "failed: empty page text after reduction",
    }]


def test_process_site_returns_failure_row_on_extract_error(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")

    def fake_extract(client, page_text):
        raise ValueError("bad json")

    monkeypatch.setattr(batch, "extract_events", fake_extract)

    rows = batch.process_site(client=object(), url="https://example.com")

    assert rows[0]["status"] == "failed: bad json"


def test_process_site_returns_no_events_status(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    monkeypatch.setattr(batch, "extract_events", lambda client, page_text: [])

    rows = batch.process_site(client=object(), url="https://example.com")

    assert rows[0]["status"] == "no_events"


def test_process_site_returns_no_events_when_all_events_are_in_the_past(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    past_event = _stub_extraction_fields("Old Event", date="January 1, 2000")
    monkeypatch.setattr(batch, "extract_events", lambda client, page_text: [past_event])

    rows = batch.process_site(client=object(), url="https://example.com")

    assert rows[0]["status"] == "no_events"


def test_process_site_auto_rejects_non_target_state_without_scoring_call(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    non_ga_event = _stub_extraction_fields(
        "Texas Event", location="Austin, TX", date="January 1, 2099"
    )
    monkeypatch.setattr(batch, "extract_events", lambda client, page_text: [non_ga_event])

    score_calls = []
    monkeypatch.setattr(
        batch, "score_event", lambda client, event: score_calls.append(event) or {}
    )

    rows = batch.process_site(client=object(), url="https://example.com")

    assert score_calls == []
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["title"] == "Texas Event"
    assert row["fit_score"] == 1
    assert row["confidence"] == "high"
    assert "Texas" in row["fit_reason"]
    assert "auto-rejected" in row["fit_reason"]


def test_process_site_auto_rejects_foreign_country_without_scoring_call(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    foreign_event = _stub_extraction_fields(
        "India Event", location="Mumbai, India", date="January 1, 2099"
    )
    monkeypatch.setattr(batch, "extract_events", lambda client, page_text: [foreign_event])

    score_calls = []
    monkeypatch.setattr(
        batch, "score_event", lambda client, event: score_calls.append(event) or {}
    )

    rows = batch.process_site(client=object(), url="https://example.com")

    assert score_calls == []
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["title"] == "India Event"
    assert row["fit_score"] == 1
    assert row["confidence"] == "high"
    assert "India" in row["fit_reason"]
    assert "auto-rejected" in row["fit_reason"]


def test_process_site_scores_target_state_and_unknown_location_events(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    ga_event = _stub_extraction_fields("GA Event", location="Atlanta, GA", date="January 1, 2099")
    unknown_event = _stub_extraction_fields("Virtual Event", location="Virtual", date="January 1, 2099")
    monkeypatch.setattr(
        batch, "extract_events", lambda client, page_text: [ga_event, unknown_event]
    )
    monkeypatch.setattr(
        batch,
        "score_event",
        lambda client, event: {"score": 5, "confidence": "high", "reason": "great fit"},
    )

    rows = batch.process_site(client=object(), url="https://example.com")

    assert len(rows) == 2
    titles = {row["title"] for row in rows}
    assert titles == {"GA Event", "Virtual Event"}
    for row in rows:
        assert row["fit_score"] == 5
        assert row["confidence"] == "high"
        assert row["fit_reason"] == "great fit"


def test_process_site_scores_event_with_real_score_event_and_real_date(monkeypatch, fake_client):
    # Regression test: uses the *real* score_event (not mocked), so a
    # future-dated event's "date" is a genuine datetime.datetime by the
    # time it reaches scoring (see _filter_past_events) and must survive
    # score_event's json.dumps(event) call rather than raising.
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    ga_event = _stub_extraction_fields("GA Event", location="Atlanta, GA", date="January 1, 2099")
    monkeypatch.setattr(batch, "extract_events", lambda client, page_text: [ga_event])

    client = fake_client(['{"score": 4, "confidence": "high", "reason": "Solid fit."}'])

    rows = batch.process_site(client=client, url="https://example.com")

    assert len(rows) == 1
    assert rows[0]["fit_score"] == 4
    assert isinstance(rows[0]["date"], datetime.datetime)


def test_process_site_mixes_auto_rejected_and_scored_rows(monkeypatch):
    monkeypatch.setattr(batch, "fetch_rendered_html", lambda url: "<html>x</html>")
    monkeypatch.setattr(batch, "reduce_html", lambda html: "some text")
    ga_event = _stub_extraction_fields("GA Event", location="Atlanta, GA", date="January 1, 2099")
    tx_event = _stub_extraction_fields("TX Event", location="Austin, TX", date="January 1, 2099")
    monkeypatch.setattr(batch, "extract_events", lambda client, page_text: [ga_event, tx_event])
    monkeypatch.setattr(
        batch,
        "score_event",
        lambda client, event: {"score": 5, "confidence": "high", "reason": "great fit"},
    )

    rows = batch.process_site(client=object(), url="https://example.com")

    by_title = {row["title"]: row for row in rows}
    assert by_title["GA Event"]["fit_reason"] == "great fit"
    assert "auto-rejected" in by_title["TX Event"]["fit_reason"]


# --- main --------------------------------------------------------------


def test_main_exits_when_api_key_missing(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        batch.main()

    assert excinfo.value.code == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_main_exits_cleanly_when_no_urls_found(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py"])
    monkeypatch.setattr(batch, "read_input_urls", lambda path: [])

    with pytest.raises(SystemExit) as excinfo:
        batch.main()

    assert excinfo.value.code == 0


def test_main_exits_when_input_file_missing(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py"])

    def fake_read_input_urls(path):
        raise FileNotFoundError(f"no such file: {path}")

    monkeypatch.setattr(batch, "read_input_urls", fake_read_input_urls)

    with pytest.raises(SystemExit) as excinfo:
        batch.main()

    assert excinfo.value.code == 1
    assert "no such file" in capsys.readouterr().err


def test_main_test_mode_uses_test_urls_and_removes_existing_output(monkeypatch, tmp_path):
    output_path = tmp_path / "test_output.xlsx"
    output_path.write_text("stale contents")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py", "--test"])
    monkeypatch.setattr(batch, "TEST_OUTPUT_PATH", str(output_path))
    monkeypatch.setattr(batch, "TEST_URLS", ["https://only-test-site.example.com"])
    monkeypatch.setattr(
        batch, "process_site", lambda client, url: [{"status": "ok", "title": "T", "source_url": url}]
    )

    appended = {}
    monkeypatch.setattr(
        batch, "append_rows", lambda path, rows: appended.update(path=path, rows=rows)
    )

    batch.main()

    assert not output_path.exists()  # removed before rewrite, and append_rows was mocked
    assert appended["path"] == str(output_path)
    assert appended["rows"] == [
        {"status": "ok", "title": "T", "source_url": "https://only-test-site.example.com"}
    ]


def test_main_aggregates_rows_from_all_urls(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py"])
    monkeypatch.setattr(
        batch, "read_input_urls", lambda path: ["https://a.example.com", "https://b.example.com"]
    )
    monkeypatch.setattr(batch, "OUTPUT_PATH", str(tmp_path / "out.xlsx"))
    monkeypatch.setattr(
        batch,
        "process_site",
        lambda client, url: [{"status": "ok", "title": f"Event for {url}", "source_url": url}],
    )

    appended = {}
    monkeypatch.setattr(
        batch, "append_rows", lambda path, rows: appended.update(path=path, rows=rows)
    )

    batch.main()

    assert len(appended["rows"]) == 2
    urls_seen = {row["source_url"] for row in appended["rows"]}
    assert urls_seen == {"https://a.example.com", "https://b.example.com"}


def test_main_converts_unexpected_process_site_error_into_failure_row(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py"])
    monkeypatch.setattr(batch, "read_input_urls", lambda path: ["https://crashes.example.com"])
    monkeypatch.setattr(batch, "OUTPUT_PATH", str(tmp_path / "out.xlsx"))

    def exploding_process_site(client, url):
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr(batch, "process_site", exploding_process_site)

    appended = {}
    monkeypatch.setattr(
        batch, "append_rows", lambda path, rows: appended.update(path=path, rows=rows)
    )

    batch.main()

    assert len(appended["rows"]) == 1
    assert "unexpected crash" in appended["rows"][0]["status"]


def test_main_per_site_mode_writes_one_sheet_per_url_instead_of_append_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py", "--per-site"])
    monkeypatch.setattr(
        batch, "read_input_urls", lambda path: ["https://a.example.com", "https://b.example.com"]
    )
    monkeypatch.setattr(batch, "PER_SITE_OUTPUT_PATH", str(tmp_path / "by_site.xlsx"))
    monkeypatch.setattr(
        batch,
        "process_site",
        lambda client, url: [{"status": "ok", "title": f"Event for {url}", "source_url": url}],
    )

    append_calls = []
    monkeypatch.setattr(batch, "append_rows", lambda path, rows: append_calls.append((path, rows)))

    per_site_calls = []
    monkeypatch.setattr(
        batch,
        "write_per_site_sheets",
        lambda path, rows_by_url: per_site_calls.append((path, rows_by_url)),
    )

    batch.main()

    assert append_calls == []
    assert len(per_site_calls) == 1
    written_path, rows_by_url = per_site_calls[0]
    assert written_path == str(tmp_path / "by_site.xlsx")
    assert list(rows_by_url.keys()) == ["https://a.example.com", "https://b.example.com"]
    assert rows_by_url["https://a.example.com"][0]["title"] == "Event for https://a.example.com"


def test_main_per_site_mode_does_not_touch_normal_output_path(monkeypatch, tmp_path):
    normal_output = tmp_path / "out.xlsx"
    normal_output.write_text("existing real output - must survive")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py", "--per-site", "--fresh"])
    monkeypatch.setattr(batch, "read_input_urls", lambda path: ["https://a.example.com"])
    monkeypatch.setattr(batch, "OUTPUT_PATH", str(normal_output))
    monkeypatch.setattr(batch, "PER_SITE_OUTPUT_PATH", str(tmp_path / "by_site.xlsx"))
    monkeypatch.setattr(batch, "process_site", lambda client, url: [{"status": "no_events"}])
    monkeypatch.setattr(batch, "write_per_site_sheets", lambda path, rows_by_url: None)

    batch.main()

    # --fresh would normally delete OUTPUT_PATH, but --per-site never writes
    # there, so it should be left untouched rather than deleted for nothing.
    assert normal_output.read_text() == "existing real output - must survive"


def test_main_per_site_mode_combines_with_test_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(batch, "Anthropic", lambda api_key: object())
    monkeypatch.setattr(batch.sys, "argv", ["run.py", "--test", "--per-site"])
    monkeypatch.setattr(batch, "TEST_URLS", ["https://only-test-site.example.com"])
    monkeypatch.setattr(batch, "PER_SITE_OUTPUT_PATH", str(tmp_path / "by_site.xlsx"))
    monkeypatch.setattr(
        batch, "process_site", lambda client, url: [{"status": "ok", "title": "T", "source_url": url}]
    )

    per_site_calls = []
    monkeypatch.setattr(
        batch,
        "write_per_site_sheets",
        lambda path, rows_by_url: per_site_calls.append((path, rows_by_url)),
    )

    batch.main()

    assert len(per_site_calls) == 1
    _, rows_by_url = per_site_calls[0]
    assert list(rows_by_url.keys()) == ["https://only-test-site.example.com"]
