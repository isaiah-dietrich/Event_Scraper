"""Excel I/O: read site URLs and append batch results to a master sheet."""

import os

from openpyxl import load_workbook
from openpyxl import Workbook

from scrape.extract import EXTRACTION_FIELDS

OUTPUT_HEADERS = ["scraped_at", "source_url", "status", *EXTRACTION_FIELDS]


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


def append_rows(path: str, rows: list[dict]) -> None:
    """Appends result rows to a master output spreadsheet.

    Creates the spreadsheet with a header row if it does not already exist.

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

    for row in rows:
        sheet.append([row.get(column, "") for column in OUTPUT_HEADERS])

    workbook.save(path)
