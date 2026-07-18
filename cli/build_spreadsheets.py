"""Scaffold a fresh combined tracker workbook.

Creates Georgia_Event_Tracker.xlsx with an editable "Websites" input sheet
plus empty "Events"/"Rejected Events" output sheets, all in one file.

Run with: python -m cli.build_spreadsheets
"""

import os

from openpyxl import load_workbook

from utility.io_excel import append_rows
from utility.io_excel import create_websites_sheet

TRACKER_PATH = "Georgia_Event_Tracker.xlsx"

# Seed sites for a brand-new tracker. This is just a starting point - the
# client adds to / removes from this list directly in the Websites sheet.
STARTER_SITES = [
    "https://ai.gatech.edu/events",
    "https://members.tagonline.org/calendar",
]


def build_tracker() -> None:
    """Creates a fresh combined tracker workbook, overwriting any existing one.

    append_rows with an empty row list creates the Events / Rejected Events
    sheets with header rows emitted straight from OUTPUT_COLUMNS (so the schema
    always matches the code), then the Websites input sheet is added on top.
    create_websites_sheet appends it as the last sheet, which is exactly where
    it should sit (results tabs first).
    """
    if os.path.exists(TRACKER_PATH):
        os.remove(TRACKER_PATH)
    append_rows(TRACKER_PATH, [])
    workbook = load_workbook(TRACKER_PATH)
    create_websites_sheet(workbook, STARTER_SITES)
    workbook.save(TRACKER_PATH)


if __name__ == "__main__":
    build_tracker()
    print(f"Created {TRACKER_PATH}")
