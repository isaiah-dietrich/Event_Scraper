"""Convenience entry point: runs the batch pipeline with one command.

Usage:
    python run.py            # process every site in websites.xlsx
    python run.py --test     # process cli/batch.py's TEST_URLS instead
    python run.py --fresh    # delete the output file before writing (no duplicates)
    python run.py --test --fresh  # combine both flags
"""

from cli.batch import main

if __name__ == "__main__":
    main()
