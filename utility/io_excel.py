"""Excel I/O: read site URLs and append batch results to a master sheet.

Output rows are written into a self-expanding Excel Table (blue/white
banded style) so the sheet stays sortable/filterable as it grows.
"""

import copy
import datetime
import os

from openpyxl import load_workbook
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table
from openpyxl.worksheet.table import TableStyleInfo

TABLE_STYLE = "TableStyleMedium2"  # Built-in blue header, white/light-blue banded rows.

EVENTS_SHEET_NAME = "Events"
REJECTED_SHEET_NAME = "Rejected Events"
PAST_EVENTS_SHEET_NAME = "past_events"

# The input site list now lives in a "Websites" sheet inside the same workbook
# the pipeline writes its results into (Georgia_Event_Tracker.xlsx), so the
# client can add/remove tracked sites in the one shared file they already look
# at - see read_input_urls / create_websites_sheet. This sheet is deliberately
# kept as the last (rightmost) tab so results stay front-and-center; see
# _move_websites_sheet_last.
WEBSITES_SHEET_NAME = "Websites"
WEBSITES_HEADER = "Website URL"
# Excel table displayName: word-characters only, workbook-unique. "Websites"
# doesn't collide with the Events/Rejected tables (EventsTable/etc.).
WEBSITES_TABLE_NAME = "Websites"

# Statuses whose rows participate in cross-run dedupe (see _event_key,
# _existing_event_keys, _append_to_sheet).
_DEDUPE_STATUSES = {"ok"}

# Internal row keys, in the order they should appear in each sheet.
OUTPUT_COLUMNS = [
    "title",
    "fit_score",
    "confidence",
    "date",
    "scraped_at",
    "start_time",
    "location",
    "signup_link",
    "status",
    "short_description",
    "fit_reason",
]

# Friendly column headers shown in the sheet.
_DISPLAY_HEADERS = {
    "title": "Event Title",
    "fit_score": "Fit Score",
    "confidence": "Confidence",
    "date": "Event Date",
    "scraped_at": "Date Scraped",
    "start_time": "Start Time",
    "location": "Location",
    "signup_link": "Signup Link",
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

# A parsed event's "date" is a real datetime.datetime (always midnight; see
# cli.batch._filter_past_events), so it sorts/filters correctly in Excel -
# but openpyxl's default number format for datetimes includes a time
# component, which would show a distracting "00:00:00" on every row. This
# formats it as a calendar date instead. Applying it to every row in the
# column is safe even for the text values kept for blank/unparseable
# dates - Excel only applies a number format to actual numeric/date values,
# so plain text cells just ignore it and display unchanged.
_DATE_COLUMN_NUMBER_FORMAT = "mmmm d, yyyy"


def read_input_urls(path: str, sheet_name: str = WEBSITES_SHEET_NAME) -> list[str]:
    """Reads site URLs from the workbook's Websites sheet (first column).

    The input site list now lives in a "Websites" sheet inside the combined
    Georgia_Event_Tracker.xlsx (which also holds the Events/Rejected output),
    so this reads that named sheet rather than workbook.active - in a
    multi-sheet workbook `active` is whatever tab was last selected, not
    reliably the sites list. Falls back to workbook.active when the named
    sheet is absent, so a plain single-sheet input file still works.

    Args:
        path: Path to the input .xlsx file. The header row is skipped.
        sheet_name: Sheet to read URLs from (defaults to the Websites sheet).

    Returns:
        A list of non-empty URL strings, whitespace-stripped and deduplicated
        while preserving first-seen order (a client hand-editing the sheet may
        paste the same site twice; scraping it once is enough).

    Raises:
        FileNotFoundError: If no spreadsheet exists at the given path.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Input spreadsheet not found: {path}")
    workbook = load_workbook(path)
    sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.active
    urls = []
    seen = set()
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        url = str(row[0]).strip()
        if url and url.lower() not in seen:
            seen.add(url.lower())
            urls.append(url)
    return urls


def create_websites_sheet(workbook, urls: list[str]):
    """Creates (or replaces) the editable "Websites" input sheet in a workbook.

    This is the client-facing control surface: a single-column Excel Table of
    site URLs they add to / remove from directly in the shared workbook, read
    back by read_input_urls on the next run. Building it as a Table (not bare
    cells) gives it the same banded style as the results sheets and lets Excel
    auto-extend it as the client types new rows.

    A header comment spells out how to use it, the tab is tinted green to stand
    out as the one editable input among the read-only results tabs, and the
    header row is frozen so it stays visible while scrolling a long list.

    Args:
        workbook: An openpyxl Workbook to add the sheet to. Any pre-existing
            sheet of the same name is removed first so this is idempotent.
        urls: Seed URLs to populate, one per row (may be empty).

    Returns:
        The created worksheet.
    """
    if WEBSITES_SHEET_NAME in workbook.sheetnames:
        del workbook[WEBSITES_SHEET_NAME]
    sheet = workbook.create_sheet(WEBSITES_SHEET_NAME)
    sheet.append([WEBSITES_HEADER])
    for url in urls:
        sheet.append([url])

    # A table ref must span at least one row below the header (see _apply_table
    # for why a header-only ref corrupts the file), so only wrap it in a Table
    # once there's a seed URL. An empty list just leaves a plain header the
    # client can start typing under.
    if sheet.max_row >= 2:
        table = Table(displayName=WEBSITES_TABLE_NAME, ref=f"A1:A{sheet.max_row}")
        table.tableStyleInfo = TableStyleInfo(
            name=TABLE_STYLE, showRowStripes=True, showFirstColumn=False
        )
        sheet.add_table(table)

    sheet.column_dimensions["A"].width = 60
    sheet.freeze_panes = "A2"
    sheet.sheet_properties.tabColor = "1F7A3D"  # green - the one editable input tab
    sheet["A1"].comment = Comment(
        "Add or remove site URLs here - one per row. Saved edits are picked up "
        "on the next run.",
        "Georgia Event Tracker",
    )
    return sheet


def _move_websites_sheet_last(workbook) -> None:
    """Moves the Websites sheet to the last (rightmost) tab, if it exists.

    Keeps the client's edit surface out of the way of the results tabs after
    any structural change - notably archive_past_events creating the
    past_events sheet via create_sheet, which appends it at the end and would
    otherwise land it to the right of Websites. A no-op when there's no
    Websites sheet (the --test output never has one).
    """
    if WEBSITES_SHEET_NAME not in workbook.sheetnames:
        return
    index = workbook.sheetnames.index(WEBSITES_SHEET_NAME)
    offset = len(workbook.sheetnames) - 1 - index
    if offset:
        workbook.move_sheet(WEBSITES_SHEET_NAME, offset=offset)


def _is_rejected(row: dict) -> bool:
    """True for a scored event the model is confident is a poor fit (score 1)."""
    return (
        row.get("status") == "ok"
        and row.get("fit_score") == 1
        and str(row.get("confidence", "")).strip().lower() == "high"
    )


def event_key(title: str, date) -> tuple:
    """Builds a dedupe key for an event: title + date.

    Public so cli.batch can build an identical key for an event that hasn't
    been written yet, to skip re-scoring it (see read_existing_event_keys).
    `date` is expected to be a real datetime.datetime for a successfully
    parsed date (see cli.batch._filter_past_events) - openpyxl always reads
    date cells back as datetime.datetime too, which is exactly why a key
    built here from a freshly-filtered event compares equal to the same
    event's key read back from a previous run's output file.

    Deliberately excludes source_url (there is no longer a URL column in
    the output - see OUTPUT_COLUMNS): the same event cross-posted on two
    different sites now dedupes to a single row instead of one per site.
    """
    return (title, date)


def _event_key(row: dict) -> tuple:
    """Builds a dedupe key for an event row: title + date."""
    return event_key(row.get("title", ""), row.get("date", ""))


def _existing_event_keys(sheet) -> set:
    """Collects dedupe keys for every dedupe-eligible row already in the
    sheet (see _DEDUPE_STATUSES)."""
    status_index = OUTPUT_COLUMNS.index("status")
    title_index = OUTPUT_COLUMNS.index("title")
    date_index = OUTPUT_COLUMNS.index("date")

    keys = set()
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or row[status_index] not in _DEDUPE_STATUSES:
            continue
        keys.add((row[title_index] or "", row[date_index] or ""))
    return keys


def _apply_table(sheet, last_row: int) -> None:
    """Creates or resizes this sheet's table to cover all current rows.

    The table name is derived from the sheet title (fine for the fixed
    "Events"/"Rejected Events"/"past_events" titles this is ever called on).

    Does nothing if the sheet has no data rows yet (last_row < 2, i.e. only
    the header). A table ref spanning just the header row (e.g. "A1:K1") is
    structurally invalid per the Excel table spec, which requires at least
    one row below the header - writing one anyway doesn't error in openpyxl,
    but Excel flags the file as corrupt on open and silently strips the
    Table/AutoFilter out during its repair. This routinely happened on the
    "Rejected Events" sheet whenever a run produced zero rejected rows. The
    table gets created on a later run once the sheet has its first real row.
    """
    if last_row < 2:
        return
    table_name = sheet.title.replace(" ", "") + "Table"
    last_column_letter = get_column_letter(len(OUTPUT_COLUMNS))
    table_ref = f"A1:{last_column_letter}{last_row}"

    if table_name in sheet.tables:
        sheet.tables[table_name].ref = table_ref
    else:
        table = Table(displayName=table_name, ref=table_ref)
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


def _apply_date_number_format(sheet, last_row: int) -> None:
    """Formats the "date" column as a calendar date, not datetime-with-time."""
    date_column_index = OUTPUT_COLUMNS.index("date") + 1
    for row_number in range(2, last_row + 1):
        sheet.cell(row=row_number, column=date_column_index).number_format = (
            _DATE_COLUMN_NUMBER_FORMAT
        )


def _validate_header(sheet) -> None:
    """Raises if an existing sheet's header row doesn't match OUTPUT_HEADERS.

    Appending rows in OUTPUT_COLUMNS order to a sheet with a different/older
    header silently misaligns every column after the divergence point (this
    has happened before, when the "date" column was added after some output
    files already existed) - so we fail loudly instead of writing bad data.
    """
    header = [cell.value for cell in sheet[1]]
    if header != OUTPUT_HEADERS:
        raise RuntimeError(
            f"Sheet {sheet.title!r} has header {header}, which does not "
            f"match the current expected columns {OUTPUT_HEADERS}. "
            "Appending would misalign data. Regenerate this output file "
            "(or migrate its header row to match) before running again."
        )


def _get_or_create_sheet(workbook, title: str):
    """Returns the named sheet, creating it with a header row if missing."""
    if title in workbook.sheetnames:
        sheet = workbook[title]
        _validate_header(sheet)
        return sheet
    sheet = workbook.create_sheet(title)
    sheet.append(OUTPUT_HEADERS)
    return sheet


def _append_to_sheet(sheet, rows: list[dict], existing_keys: set) -> int:
    """Appends rows to one sheet, skipping dedupe-eligible rows (see
    _DEDUPE_STATUSES) already in existing_keys.

    Mutates existing_keys with the key of every dedupe-eligible row actually
    written, so a single key set can be shared across sheets to prevent the
    same event ending up duplicated across Events and Rejected Events.
    """
    skipped_count = 0
    for row in rows:
        if row.get("status") in _DEDUPE_STATUSES:
            key = _event_key(row)
            if key in existing_keys:
                skipped_count += 1
                print(f"  [dedup skip] {key[0]} | {key[1]}")
                continue
            existing_keys.add(key)
        sheet.append([row.get(column, "") for column in OUTPUT_COLUMNS])

    _apply_table(sheet, sheet.max_row)
    _autosize_columns(sheet, sheet.max_row)
    _apply_date_number_format(sheet, sheet.max_row)
    return skipped_count


def append_rows(path: str, rows: list[dict]) -> None:
    """Appends result rows to a master output spreadsheet.

    Creates the spreadsheet with "Events" and "Rejected Events" sheets if
    they do not already exist. A scored event is routed to Rejected Events
    if the model gave it fit_score 1 with high confidence, and everything
    else (including non-"ok" failure/no_events rows) goes to Events. Dedupe-
    eligible rows (see _DEDUPE_STATUSES) are skipped if a row with the same
    title and date is already present in either sheet, so re-running the
    batch over the same sites does not duplicate events or let one flip
    between sheets across runs - and the same event cross-posted on two
    different sites now collapses to a single row (see event_key).

    Args:
        path: Path to the output .xlsx file. Row dicts may still carry a
            "source_url" key (e.g. for building a signup_link fallback
            before this call) - it is simply not written as a column.
        rows: A list of dicts, each keyed by a subset of OUTPUT_COLUMNS plus
            any extra fields used only for dedupe (e.g. the event's "date").
    """
    # TODO: once this file lives on OneDrive and is co-edited by the client,
    # record os.path.getmtime(path) here (if it exists) and re-check it right
    # before workbook.save() below - if it changed in between, someone else
    # wrote to the file during this run. On conflict, save to a separate
    # timestamped file instead of overwriting, rather than silently clobbering
    # their edits.
    if os.path.exists(path):
        workbook = load_workbook(path)
    else:
        workbook = Workbook()
        workbook.remove(workbook.active)

    events_sheet = _get_or_create_sheet(workbook, EVENTS_SHEET_NAME)
    rejected_sheet = _get_or_create_sheet(workbook, REJECTED_SHEET_NAME)

    existing_keys = _existing_event_keys(events_sheet) | _existing_event_keys(rejected_sheet)
    normal_rows = [row for row in rows if not _is_rejected(row)]
    rejected_rows = [row for row in rows if _is_rejected(row)]

    skipped_count = _append_to_sheet(events_sheet, normal_rows, existing_keys)
    skipped_count += _append_to_sheet(rejected_sheet, rejected_rows, existing_keys)

    _move_websites_sheet_last(workbook)
    # TODO: mtime conflict check goes here, right before the save.
    workbook.save(path)
    if skipped_count:
        print(f"Skipped {skipped_count} duplicate event(s) already in {path}")


def read_existing_event_keys(path: str) -> set:
    """Returns the dedupe keys of every event already in an output workbook.

    This is the union of _existing_event_keys over both the Events and
    Rejected Events sheets, i.e. exactly the set append_rows would dedupe
    a new run's rows against. Callers (see cli.batch) use it to skip
    scoring an event before it's even written, rather than paying for a
    scoring AI call only to have the row silently dropped by write-time
    dedupe in append_rows.

    Returns an empty set if no file exists at `path` yet - the natural
    "nothing known" starting point for a first run.
    """
    if not os.path.exists(path):
        return set()
    workbook = load_workbook(path)
    keys = set()
    for title in (EVENTS_SHEET_NAME, REJECTED_SHEET_NAME):
        if title not in workbook.sheetnames:
            continue
        sheet = workbook[title]
        _validate_header(sheet)
        keys |= _existing_event_keys(sheet)
    return keys


def reset_result_sheets(path: str) -> None:
    """Wipes only the result sheets from the workbook, preserving Websites.

    This is what `--fresh` uses now that input and output share one file:
    blindly deleting the whole file (the old behavior) would take the client's
    Websites input sheet down with it. Removes the Events / Rejected Events /
    past_events sheets so append_rows rebuilds them empty, while leaving the
    Websites sheet - and any other sheet the client added - untouched.

    If removing the result sheets would leave the workbook with no sheets at
    all (e.g. a results-only file that never had a Websites sheet), the whole
    file is removed instead, matching the old wipe-and-recreate behavior. A
    no-op if the file doesn't exist.
    """
    if not os.path.exists(path):
        return
    workbook = load_workbook(path)
    for title in (EVENTS_SHEET_NAME, REJECTED_SHEET_NAME, PAST_EVENTS_SHEET_NAME):
        if title in workbook.sheetnames:
            del workbook[title]
    if not workbook.sheetnames:
        os.remove(path)
        return
    _move_websites_sheet_last(workbook)
    workbook.save(path)


# --- Past-event archival -----------------------------------------------------
#
# archive_past_events moves any event that has aged into the past out of the
# live Events/Rejected Events sheets into a dedicated "past_events" sheet, so
# the workbook the client actually looks at only ever shows current/upcoming
# events. It is meant to run at the very start of every batch (see
# cli.batch.main), before read_existing_event_keys, so dedupe/scoring for
# that run only ever sees the post-archive state.

# Style attributes that can be assigned directly (openpyxl's style proxy
# objects can't be shared across cells/workbooks - each needs its own
# copy.copy()). "number_format" and value are handled separately below:
# number_format is a plain string (no copy needed), and value obviously
# isn't a style.
_STYLE_COPY_ATTRS = ("fill", "font", "border", "alignment", "protection")


def _validate_header_prefix(sheet) -> None:
    """Like _validate_header, but only checks the first len(OUTPUT_HEADERS)
    cells, tolerating any extra annotation columns a client has typed notes
    into beyond the standard layout (see archive_past_events - unlike the
    Events/Rejected Events sheets, past_events is expected to accumulate
    these over time as archived rows bring their annotations with them).
    """
    header = [cell.value for cell in sheet[1]]
    prefix = header[: len(OUTPUT_HEADERS)]
    if prefix != OUTPUT_HEADERS:
        raise RuntimeError(
            f"Sheet {sheet.title!r} has header {header}, whose first "
            f"{len(OUTPUT_HEADERS)} column(s) do not match the expected "
            f"columns {OUTPUT_HEADERS}. Appending would misalign data. "
            "Regenerate this output file (or migrate its header row to "
            "match) before running again."
        )


def _copy_cell_fully(source_cell, target_cell) -> None:
    """Copies a cell's value plus every style/annotation attribute a client
    might have hand-set: fill, font, border, alignment, number format,
    protection, comment, and hyperlink.

    openpyxl's style proxy objects (fills, fonts, ...) and Comment objects
    are mutable and tied to their originating cell/workbook, so each is
    given its own copy.copy() rather than assigned directly - reusing the
    same instance across cells corrupts the source cell's formatting once
    the target is modified (or raises outright for Comment, which refuses
    to be attached to more than one cell).

    Value is set before the hyperlink: Cell.hyperlink's setter auto-fills
    a blank cell's value from the hyperlink's target/location, which would
    incorrectly override a real (possibly different) cell value if set
    first.
    """
    target_cell.value = source_cell.value
    for attr in _STYLE_COPY_ATTRS:
        setattr(target_cell, attr, copy.copy(getattr(source_cell, attr)))
    target_cell.number_format = source_cell.number_format
    if source_cell.comment is not None:
        target_cell.comment = copy.copy(source_cell.comment)
    if source_cell.hyperlink is not None:
        # The Hyperlink.setter sets `.ref` to the *target* cell's own
        # coordinate automatically, so the copied object always ends up
        # correctly addressed regardless of where it came from.
        target_cell.hyperlink = copy.copy(source_cell.hyperlink)


def _rows_before_today(sheet, today: datetime.date) -> list[int]:
    """Returns the 1-based row numbers (data rows only) whose "date" column
    holds a real date/datetime strictly before `today`.

    Rows with a blank or unparseable (text) date are left out - never
    guessed at, per archive_past_events.
    """
    date_column = OUTPUT_COLUMNS.index("date") + 1
    rows = []
    for row_number in range(2, sheet.max_row + 1):
        value = sheet.cell(row=row_number, column=date_column).value
        if isinstance(value, datetime.datetime):
            value = value.date()
        elif not isinstance(value, datetime.date):
            continue
        if value < today:
            rows.append(row_number)
    return rows


def _fix_hyperlink_refs(sheet) -> None:
    """Repoints every surviving cell's hyperlink `.ref` at its own current
    coordinate.

    openpyxl's Worksheet.delete_rows relocates a shifted cell's value,
    styles, and comment correctly (it moves the same Cell object and
    rewrites its .row/.column), but does NOT update the nested
    Hyperlink.ref string that same cell carries - verified empirically:
    saving/reloading a sheet after delete_rows shifted a hyperlinked cell
    left a stale ref (e.g. "B3") pointing at the cell's old position,
    which Excel/openpyxl then reconstitutes as a phantom cell on reload.
    Rewriting every remaining hyperlink's ref to cell.coordinate right
    after any delete_rows call is a minimal, targeted fix for that.
    """
    for row in sheet.iter_rows():
        for cell in row:
            if cell.hyperlink is not None:
                cell.hyperlink.ref = cell.coordinate


def _move_rows_to_past(source_sheet, row_numbers: list[int], past_sheet) -> None:
    """Copies each row in row_numbers (full width, with every style/
    annotation attribute - see _copy_cell_fully) from source_sheet into a
    freshly appended row on past_sheet, then deletes those rows out of
    source_sheet.

    Copies the full row width (source_sheet.max_column), not just
    len(OUTPUT_COLUMNS), so any extra columns the client has typed notes
    into travel with the row instead of being silently dropped.

    Deletes are issued bottom-up (descending row number) so a row number
    earlier in the list never shifts out from under a later delete_rows
    call.
    """
    width = source_sheet.max_column
    for row_number in row_numbers:
        target_row = past_sheet.max_row + 1
        for column in range(1, width + 1):
            _copy_cell_fully(
                source_sheet.cell(row=row_number, column=column),
                past_sheet.cell(row=target_row, column=column),
            )
        source_height = source_sheet.row_dimensions[row_number].height
        if source_height is not None:
            past_sheet.row_dimensions[target_row].height = source_height

    for row_number in sorted(row_numbers, reverse=True):
        source_sheet.delete_rows(row_number, 1)
    _fix_hyperlink_refs(source_sheet)


def _sync_table_after_deletion(sheet) -> None:
    """Resizes this sheet's table to match its current row count, or removes
    the table entirely if deletions emptied the sheet back down to just its
    header row.

    See _apply_table's docstring for why a header-only ref (e.g. "A1:K1")
    is invalid per the Excel table spec - _apply_table itself only ever
    grows/no-ops, so a shrink-to-nothing case has to be handled here
    instead, or Excel would flag the file as corrupt on open.
    """
    if sheet.max_row < 2:
        for table_name in list(sheet.tables.keys()):
            del sheet.tables[table_name]
        return
    _apply_table(sheet, sheet.max_row)


def archive_past_events(path: str) -> int:
    """Moves every event dated strictly before today out of the Events and
    Rejected Events sheets into a "past_events" sheet (created if missing),
    so the live workbook only ever shows current/upcoming events.

    Everything about a moved row is preserved - not just OUTPUT_COLUMNS'
    fields but the full row width, since the client hand-annotates this
    workbook (extra note columns, cell highlights, comments, hyperlinks;
    see _copy_cell_fully/_move_rows_to_past). Rows with a blank or
    unparseable (text) date are left in place rather than guessed at (see
    _rows_before_today).

    Meant to run once at the very start of every batch (see cli.batch.main),
    before read_existing_event_keys, so that call's dedupe keys - and this
    run's scoring/dedupe generally - only ever see the post-archive state.

    Args:
        path: Path to the output .xlsx file.

    Returns:
        The number of rows moved into past_events. Returns 0 (and leaves
        the file untouched, including not creating past_events) if the file
        doesn't exist yet or nothing needed to move - so a second call in a
        row, or a call against a brand-new workbook, is a true no-op.
    """
    if not os.path.exists(path):
        return 0

    # TODO: same mtime-based conflict check as append_rows belongs here too -
    # record os.path.getmtime(path) now, re-check right before workbook.save()
    # below, and on a mismatch save to a separate file instead of overwriting.
    workbook = load_workbook(path)
    today = datetime.date.today()

    moves = []
    for title in (EVENTS_SHEET_NAME, REJECTED_SHEET_NAME):
        if title not in workbook.sheetnames:
            continue
        sheet = workbook[title]
        row_numbers = _rows_before_today(sheet, today)
        if row_numbers:
            moves.append((sheet, row_numbers))

    total_moved = sum(len(row_numbers) for _, row_numbers in moves)
    if total_moved == 0:
        return 0

    if PAST_EVENTS_SHEET_NAME in workbook.sheetnames:
        past_sheet = workbook[PAST_EVENTS_SHEET_NAME]
        _validate_header_prefix(past_sheet)
    else:
        past_sheet = workbook.create_sheet(PAST_EVENTS_SHEET_NAME)
        past_sheet.append(OUTPUT_HEADERS)

    for sheet, row_numbers in moves:
        _move_rows_to_past(sheet, row_numbers, past_sheet)
        _sync_table_after_deletion(sheet)

    _apply_table(past_sheet, past_sheet.max_row)
    _autosize_columns(past_sheet, past_sheet.max_row)
    _apply_date_number_format(past_sheet, past_sheet.max_row)

    _move_websites_sheet_last(workbook)
    # TODO: mtime conflict check goes here, right before the save.
    workbook.save(path)
    return total_moved
