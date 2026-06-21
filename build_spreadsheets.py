"""One-off helper to create the initial input/output spreadsheet templates."""

from openpyxl import Workbook

SAMPLE_SITES = [
    "https://ai.gatech.edu/events",
    "https://georgiaai.org/#Events"
]


def build_input():
    wb = Workbook()
    ws = wb.active
    ws.title = "Sites"
    ws.append(["url"])
    for url in SAMPLE_SITES:
        ws.append([url])
    wb.save("websites.xlsx")


def build_output_template():
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append([
        "scraped_at",
        "source_url",
        "status",
        "title",
        "date",
        "start_time",
        "location",
        "is_in_person",
        "signup_link",
        "short_description",
    ])
    wb.save("events_output.xlsx")


if __name__ == "__main__":
    build_input()
    build_output_template()
    print("Created websites.xlsx and events_output.xlsx")
