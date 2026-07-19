"""Convenience entry point: runs the weekly digest pipeline with one command.

Usage:
    python run.py              # weekly digest run: scrape cli/batch.py's
                                # SITE_URLS, write a dated digest of the new
                                # events, and email it to the client
    python run.py --no-email   # same run, but print the email instead of
                                # sending it (the digest file is still written)
"""

from cli.batch import main

if __name__ == "__main__":
    main()
