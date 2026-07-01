import pytest
from openpyxl import load_workbook
from openpyxl import Workbook

from utility.io_excel import _is_rejected
from utility.io_excel import append_rows
from utility.io_excel import EVENTS_SHEET_NAME
from utility.io_excel import OUTPUT_HEADERS
from utility.io_excel import read_input_urls
from utility.io_excel import REJECTED_SHEET_NAME


def _ok_row(**overrides):
    row = {
        "title": "AI Meetup",
        "fit_score": 4,
        "confidence": "high",
        "date": "July 4, 2026",
        "scraped_at": "June 30, 2026",
        "source_url": "https://example.com",
        "start_time": "6:00 PM",
        "location": "Atlanta, GA",
        "status": "ok",
        "short_description": "A meetup about AI.",
        "fit_reason": "Great fit.",
    }
    row.update(overrides)
    return row


# --- read_input_urls -------------------------------------------------------


def test_read_input_urls_returns_non_empty_stripped_urls(tmp_path):
    path = tmp_path / "sites.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["url"])
    sheet.append(["https://a.example.com "])
    sheet.append([None])
    sheet.append(["  https://b.example.com"])
    sheet.append([""])
    workbook.save(path)

    urls = read_input_urls(str(path))

    assert urls == ["https://a.example.com", "https://b.example.com"]


def test_read_input_urls_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.xlsx"

    with pytest.raises(FileNotFoundError):
        read_input_urls(str(missing))


# --- _is_rejected ------------------------------------------------------


def test_is_rejected_true_for_high_confidence_score_one():
    assert _is_rejected(_ok_row(fit_score=1, confidence="high")) is True


def test_is_rejected_false_for_low_confidence_score_one():
    assert _is_rejected(_ok_row(fit_score=1, confidence="low")) is False


def test_is_rejected_false_for_score_above_one():
    assert _is_rejected(_ok_row(fit_score=2, confidence="high")) is False


def test_is_rejected_false_for_non_ok_status():
    assert _is_rejected(_ok_row(fit_score=1, confidence="high", status="no_events")) is False


# --- append_rows: sheet creation and routing --------------------------------


def test_append_rows_creates_workbook_with_expected_headers(tmp_path):
    path = tmp_path / "out.xlsx"

    append_rows(str(path), [_ok_row()])

    workbook = load_workbook(path)
    assert set(workbook.sheetnames) >= {EVENTS_SHEET_NAME, REJECTED_SHEET_NAME}
    events_header = [cell.value for cell in workbook[EVENTS_SHEET_NAME][1]]
    assert events_header == OUTPUT_HEADERS


def test_append_rows_routes_good_score_to_events_sheet(tmp_path):
    path = tmp_path / "out.xlsx"

    append_rows(str(path), [_ok_row(title="Good Event", fit_score=4, confidence="high")])

    workbook = load_workbook(path)
    events_titles = [row[0] for row in workbook[EVENTS_SHEET_NAME].iter_rows(min_row=2, values_only=True)]
    rejected_titles = [row[0] for row in workbook[REJECTED_SHEET_NAME].iter_rows(min_row=2, values_only=True)]
    assert "Good Event" in events_titles
    assert "Good Event" not in rejected_titles


def test_append_rows_routes_rejected_score_to_rejected_sheet(tmp_path):
    path = tmp_path / "out.xlsx"

    append_rows(str(path), [_ok_row(title="Bad Event", fit_score=1, confidence="high")])

    workbook = load_workbook(path)
    events_titles = [row[0] for row in workbook[EVENTS_SHEET_NAME].iter_rows(min_row=2, values_only=True)]
    rejected_titles = [row[0] for row in workbook[REJECTED_SHEET_NAME].iter_rows(min_row=2, values_only=True)]
    assert "Bad Event" in rejected_titles
    assert "Bad Event" not in events_titles


def test_append_rows_non_ok_status_goes_to_events_sheet(tmp_path):
    path = tmp_path / "out.xlsx"
    failure_row = {
        "scraped_at": "June 30, 2026",
        "source_url": "https://example.com",
        "status": "failed: timeout",
    }

    append_rows(str(path), [failure_row])

    workbook = load_workbook(path)
    events_rows = list(workbook[EVENTS_SHEET_NAME].iter_rows(min_row=2, values_only=True))
    rejected_rows = list(workbook[REJECTED_SHEET_NAME].iter_rows(min_row=2, values_only=True))
    assert len(events_rows) == 1
    assert len(rejected_rows) == 0


# --- append_rows: dedupe -----------------------------------------------


def test_append_rows_dedupes_identical_ok_event_across_calls(tmp_path):
    path = tmp_path / "out.xlsx"
    row = _ok_row(title="Repeat Event", date="July 4, 2026", source_url="https://x.com")

    append_rows(str(path), [row])
    append_rows(str(path), [row])

    workbook = load_workbook(path)
    events_rows = list(workbook[EVENTS_SHEET_NAME].iter_rows(min_row=2, values_only=True))
    assert len(events_rows) == 1


def test_append_rows_dedupes_across_events_and_rejected_sheets(tmp_path):
    path = tmp_path / "out.xlsx"
    good_row = _ok_row(
        title="Flip Flop Event", date="July 4, 2026", source_url="https://x.com",
        fit_score=4, confidence="high",
    )
    bad_row = _ok_row(
        title="Flip Flop Event", date="July 4, 2026", source_url="https://x.com",
        fit_score=1, confidence="high",
    )

    append_rows(str(path), [good_row])
    append_rows(str(path), [bad_row])

    workbook = load_workbook(path)
    events_rows = list(workbook[EVENTS_SHEET_NAME].iter_rows(min_row=2, values_only=True))
    rejected_rows = list(workbook[REJECTED_SHEET_NAME].iter_rows(min_row=2, values_only=True))
    assert len(events_rows) == 1
    assert len(rejected_rows) == 0


def test_append_rows_does_not_dedupe_different_dates(tmp_path):
    path = tmp_path / "out.xlsx"
    row_a = _ok_row(title="Recurring Event", date="July 4, 2026", source_url="https://x.com")
    row_b = _ok_row(title="Recurring Event", date="August 4, 2026", source_url="https://x.com")

    append_rows(str(path), [row_a])
    append_rows(str(path), [row_b])

    workbook = load_workbook(path)
    events_rows = list(workbook[EVENTS_SHEET_NAME].iter_rows(min_row=2, values_only=True))
    assert len(events_rows) == 2


# --- append_rows: header validation --------------------------------------


def test_append_rows_raises_on_mismatched_existing_header(tmp_path):
    path = tmp_path / "out.xlsx"
    workbook = Workbook()
    workbook.remove(workbook.active)
    sheet = workbook.create_sheet(EVENTS_SHEET_NAME)
    sheet.append(["Some", "Old", "Header"])
    workbook.create_sheet(REJECTED_SHEET_NAME).append(["Some", "Old", "Header"])
    workbook.save(path)

    with pytest.raises(RuntimeError, match="does not match"):
        append_rows(str(path), [_ok_row()])


# --- append_rows: table grows across calls --------------------------------


def test_append_rows_table_ref_grows_across_calls(tmp_path):
    path = tmp_path / "out.xlsx"
    append_rows(str(path), [_ok_row(title="One", date="July 1, 2026")])
    workbook = load_workbook(path)
    events_sheet = workbook[EVENTS_SHEET_NAME]
    table_name = next(iter(events_sheet.tables))
    first_ref = events_sheet.tables[table_name].ref

    append_rows(str(path), [_ok_row(title="Two", date="July 2, 2026")])
    workbook = load_workbook(path)
    events_sheet = workbook[EVENTS_SHEET_NAME]
    second_ref = events_sheet.tables[table_name].ref

    assert first_ref == "A1:K2"
    assert second_ref == "A1:K3"
