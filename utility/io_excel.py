"""Excel I/O: read site URLs and append batch results to a master sheet.

Output rows are written into a self-expanding Excel Table (blue/white
banded style) so the sheet stays sortable/filterable as it grows.
"""

import os

from openpyxl import load_workbook
from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table
from openpyxl.worksheet.table import TableStyleInfo

TABLE_NAME = "EventsTable"
TABLE_STYLE = "TableStyleMedium2"  # Built-in blue header, white/light-blue banded rows.

# Internal row keys, in the order they should appear in the spreadsheet.
# "date" (the event's own date) and "scraped_at" are tracked internally for
# dedupe but only "scraped_at" is shown, per the requested column layout.
OUTPUT_COLUMNS = [
    "title",
    "fit_score",
    "date",
    "scraped_at",
    "source_url",
    "start_time",
    "location",
    "status",
    "short_description",
    "fit_reason",
]

# Friendly column headers shown in the sheet.
_DISPLAY_HEADERS = {
    "title": "Event Title",
    "fit_score": "Fit Score",
    "date": "Event Date",
    "scraped_at": "Date Scraped",
    "source_url": "URL",
    "start_time": "Start Time",
    "location": "Location",
    "status": "Status",
    "short_description": "Description",
    "fit_reason": "Fit Reason",
}
OUTPUT_HEADERS = [_DISPLAY_HEADERS[column] for column in OUTPUT_COLUMNS]

# These two columns hold long free-text and are exempt from column autosizing.
_NO_AUTOSIZE_COLUMNS = {"short_description", "fit_reason"}
_WRAP_COLUMN_WIDTH = 60
_MAX_AUTOSIZE_WIDTH = 50
_AUTOSIZE_PADDING = 2


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
    status_index = OUTPUT_COLUMNS.index("status")
    source_url_index = OUTPUT_COLUMNS.index("source_url")
    title_index = OUTPUT_COLUMNS.index("title")
    date_index = OUTPUT_COLUMNS.index("date")

    keys = set()
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or row[status_index] != "ok":
            continue
        keys.add((row[source_url_index] or "", row[title_index] or "", row[date_index] or ""))
    return keys


def _apply_table(sheet, last_row: int) -> None:
    """Creates or resizes the Events table to cover all current rows."""
    last_column_letter = get_column_letter(len(OUTPUT_COLUMNS))
    table_ref = f"A1:{last_column_letter}{last_row}"

    if TABLE_NAME in sheet.tables:
        sheet.tables[TABLE_NAME].ref = table_ref
    else:
        table = Table(displayName=TABLE_NAME, ref=table_ref)
        table.tableStyleInfo = TableStyleInfo(
            name=TABLE_STYLE, showRowStripes=True, showFirstColumn=False
        )
        sheet.add_table(table)


def _autosize_columns(sheet, last_row: int) -> None:
    """Sizes each column to fit its longest value, except free-text columns."""
    for index, column in enumerate(OUTPUT_COLUMNS, start=1):
        letter = get_column_letter(index)
        if column in _NO_AUTOSIZE_COLUMNS:
            sheet.column_dimensions[letter].width = _WRAP_COLUMN_WIDTH
            for row_number in range(2, last_row + 1):
                sheet.cell(row=row_number, column=index).alignment = Alignment(
                    wrap_text=True, vertical="top"
                )
            continue

        max_length = len(str(_DISPLAY_HEADERS[column]))
        for row_number in range(2, last_row + 1):
            value = sheet.cell(row=row_number, column=index).value
            if value is not None:
                max_length = max(max_length, len(str(value)))
        sheet.column_dimensions[letter].width = min(
            max_length + _AUTOSIZE_PADDING, _MAX_AUTOSIZE_WIDTH
        )


def append_rows(path: str, rows: list[dict]) -> None:
    """Appends result rows to a master output spreadsheet.

    Creates the spreadsheet with a header row and an Excel Table if it does
    not already exist. Rows with status "ok" are skipped if a row with the
    same source_url and title is already present, so re-running the batch
    over the same sites does not duplicate events. Non-"ok" status rows
    (failures, no_events) are always appended.

    Args:
        path: Path to the output .xlsx file.
        rows: A list of dicts, each keyed by a subset of OUTPUT_COLUMNS plus
            any extra fields used only for dedupe (e.g. the event's "date").
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
                print(f"  [dedup skip] {key[0]} | {key[1]}")
                continue
            existing_keys.add(key)
        sheet.append([row.get(column, "") for column in OUTPUT_COLUMNS])

    _apply_table(sheet, sheet.max_row)
    _autosize_columns(sheet, sheet.max_row)

    workbook.save(path)
    if skipped_count:
        print(f"Skipped {skipped_count} duplicate event(s) already in {path}")
