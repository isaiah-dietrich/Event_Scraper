"""One-off helper to create the initial input/output spreadsheet templates."""

from openpyxl import Workbook

from utility.io_excel import OUTPUT_HEADERS

SAMPLE_SITES = [
    "https://ai.gatech.edu/events",
    "https://georgiaai.org/#Events",
]

INPUT_PATH = "websites.xlsx"
OUTPUT_PATH = "events_output.xlsx"


def build_input() -> None:
    """Creates the input spreadsheet with a header row and sample site URLs."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sites"
    sheet.append(["url"])
    for url in SAMPLE_SITES:
        sheet.append([url])
    workbook.save(INPUT_PATH)


def build_output_template() -> None:
    """Creates an empty output spreadsheet with the expected header row."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Events"
    sheet.append(OUTPUT_HEADERS)
    workbook.save(OUTPUT_PATH)


if __name__ == "__main__":
    build_input()
    build_output_template()
    print(f"Created {INPUT_PATH} and {OUTPUT_PATH}")
