"""Convenience entry point: runs the batch pipeline with one command.

Usage:
    python run.py              # process every site in Georgia_Event_Tracker.xlsx's
                                # Websites sheet, appending results to the same file
    python run.py --test       # process cli/batch.py's TEST_URLS instead
    python run.py --fresh      # clear existing results before writing (the
                                # Websites input sheet is preserved)
    python run.py --per-site   # testing only: write one sheet per URL to
                                # events_output_by_site.xlsx instead of the
                                # normal combined Events/Rejected workbook
    python run.py --test --fresh --per-site  # flags combine freely
"""

from cli.batch import main

if __name__ == "__main__":
    main()
