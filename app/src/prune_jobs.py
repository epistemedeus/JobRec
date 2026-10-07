"""Remove stored job listings that are no longer open.

Run from app/src:  python prune_jobs.py [--delete] [--limit N] [--sources registry.json]

Without --delete this is a dry run: it follows the links of the candidate
listings and reports how many look closed, but removes nothing. Uses the same
DATABASE_URL setting as the web app.
"""

import argparse
import logging
import sys
import time
from os import environ as env
from pathlib import Path

from dotenv import load_dotenv

from db import Base, make_engine
from job_pruning import prune_closed_listings


def main(argv=None, *, engine=None, session=None, now=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Remove stored job listings whose application link shows they are closed.")
    parser.add_argument("--delete", action="store_true", help="actually remove closed listings (default is a dry run)")
    parser.add_argument("--limit", type=int, default=None, help="follow at most this many links")
    parser.add_argument("--sources", type=Path, default=None, help="registry file to read; default uses the checked-in registries")
    args = parser.parse_args(argv)

    if engine is None:
        load_dotenv()
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        engine = make_engine(env.get("DATABASE_URL", "sqlite+pysqlite:///user_skills.db"))
        Base.metadata.create_all(engine)

    sources = None
    if args.sources is not None:
        from job_sources import load_sources
        sources = load_sources(args.sources)
    result = prune_closed_listings(
        engine, session, dry_run=not args.delete, limit=args.limit, now=now, sleep=sleep, sources=sources,
    )
    if args.delete:
        print(f"Checked {result.checked} links: removed {result.removed} closed listings, "
              f"{result.kept} still open, {result.unknown} undetermined (kept).")
    else:
        print(f"Dry run. Checked {result.checked} links: {result.closed} listings look closed and would be removed, "
              f"{result.kept} still open, {result.unknown} undetermined. Run with --delete to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
