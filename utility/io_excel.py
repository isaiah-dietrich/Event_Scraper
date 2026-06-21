"""Excel I/O: read site URLs and append batch results to a master sheet."""

import os

from openpyxl import load_workbook
from openpyxl import Workbook

from scrape.extract import EXTRACTION_FIELDS

OUTPUT_HEADERS = [
    "scraped_at",
    "source_url",
    "status",
    *EXTRACTION_FIELDS,
    "fit_score",
    "fit_reason",
]


def read_input_urls(path: str) -> list[str]:
    """Reads site URLs from an input spreadsheet's first column.

    Args:
        path: Path to the input .xlsx file. The header row is skipped.

    Returns:
        A list of non-empty URL strings.

    Raises:
        FileNotFoundError: If no spreadsheet exists at the given path.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Input spreadsheet not found: {path}")
    workbook = load_workbook(path)
    sheet = workbook.active
    urls = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if row and row[0]:
            urls.append(str(row[0]).strip())
    return urls


def _event_key(row: dict) -> tuple:
    """Builds a dedupe key for an event row: source site + title + date."""
    return (row.get("source_url", ""), row.get("title", ""), row.get("date", ""))


def _existing_event_keys(sheet) -> set:
    """Collects dedupe keys for every "ok" event row already in the sheet."""
    status_index = OUTPUT_HEADERS.index("status")
    source_url_index = OUTPUT_HEADERS.index("source_url")
    title_index = OUTPUT_HEADERS.index("title")
    date_index = OUTPUT_HEADERS.index("date")

    keys = set()
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or row[status_index] != "ok":
            continue
        keys.add((row[source_url_index] or "", row[title_index] or "", row[date_index] or ""))
    return keys


def append_rows(path: str, rows: list[dict]) -> None:
    """Appends result rows to a master output spreadsheet.

    Creates the spreadsheet with a header row if it does not already exist.
    Rows with status "ok" are skipped if a row with the same source_url,
    title, and date is already present, so re-running the batch over the
    same sites does not duplicate events. Non-"ok" status rows (failures,
    no_events) are always appended.

    Args:
        path: Path to the output .xlsx file.
        rows: A list of dicts, each keyed by a subset of OUTPUT_HEADERS.
    """
    if os.path.exists(path):
        workbook = load_workbook(path)
        sheet = workbook.active
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Events"
        sheet.append(OUTPUT_HEADERS)

    existing_keys = _existing_event_keys(sheet)
    skipped_count = 0

    for row in rows:
        if row.get("status") == "ok":
            key = _event_key(row)
            if key in existing_keys:
                skipped_count += 1
                continue
            existing_keys.add(key)
        sheet.append([row.get(column, "") for column in OUTPUT_HEADERS])

    workbook.save(path)
    if skipped_count:
        print(f"Skipped {skipped_count} duplicate event(s) already in {path}")
