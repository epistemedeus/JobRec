"""Measure how many stored application links are valid (validation test NF-05, requirement NFR-006).

Run from app/src:  python check_links.py [--sample 100] [--sources registry.json]

Samples stored listings, follows each application link, and reports the share
that resolve to a live page and the share whose page mentions the stored job
title or company. The requirement is at least 95%.

A Greenhouse listing whose company matches exactly one configured board is
confirmed through that board's per-job API instead of its application page.
An unknown result is not counted as a live page.
"""

import argparse
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from os import environ as env
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from db import Base, make_engine
from greenhouse_status import check_configured_link, load_status_sources
from job_store import JobListingTable
from link_check import LinkStatus

DEFAULT_SAMPLE_SIZE = 100
DEFAULT_PAUSE = 0.5


@dataclass
class LinkReport:
    sampled: int = 0
    resolved: int = 0   # links that returned a live page
    matched: int = 0    # resolved links whose page mentions the stored title or company
    problems: list[tuple[str, str]] = field(default_factory=list)   # (url, what was wrong)

    @property
    def resolved_share(self) -> float:
        return self.resolved / self.sampled if self.sampled else 0.0

    @property
    def matched_share(self) -> float:
        return self.matched / self.sampled if self.sampled else 0.0


def check_stored_links(
    engine: Engine,
    session=None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    *,
    sleep: Callable[[float], None] = time.sleep,
    pause: float = DEFAULT_PAUSE,
    rng: Optional[random.Random] = None,
    sources=None,
) -> LinkReport:
    """Follow a random sample of stored application links and report how many are valid."""
    session = session or requests.Session()
    registry = load_status_sources() if sources is None else list(sources)
    board_cache: dict = {}
    table = JobListingTable.__table__
    with Session(engine) as db:
        rows = list(db.execute(select(
            table.c.title, table.c.company_name, table.c.application_url, table.c.source, table.c.source_job_id,
        ).order_by(table.c.id)))
    rows = (rng or random.Random()).sample(rows, min(sample_size, len(rows)))

    report = LinkReport()
    seen_hosts: set[str] = set()
    for title, company, url, source, source_job_id in rows:
        host = (urlsplit(url).hostname or "").lower()
        if host in seen_hosts:
            sleep(pause)
        seen_hosts.add(host)

        link = check_configured_link(
            url,
            session,
            source=source,
            source_job_id=source_job_id,
            company_name=company,
            sources=registry,
            board_cache=board_cache,
            job_id=source_job_id,
        )
        report.sampled += 1
        if link.status is not LinkStatus.OPEN or link.http_status != 200:
            report.problems.append((url, link.reason))
            continue
        report.resolved += 1
        # Search the page source, not just its visible text: pages rendered in the browser
        # name the job only in their metadata or structured data.
        page_source = link.raw.lower()
        if title.strip().lower() in page_source or company.strip().lower() in page_source:
            report.matched += 1
        else:
            report.problems.append((url, "page does not mention the stored title or company"))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report how many stored application links are valid.")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE_SIZE, help="number of stored links to check")
    parser.add_argument("--sources", type=Path, default=None, help="registry file to read; default uses the checked-in registries")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    engine = make_engine(env.get("DATABASE_URL", "sqlite+pysqlite:///user_skills.db"))
    Base.metadata.create_all(engine)

    sources = None
    if args.sources is not None:
        from job_sources import load_sources
        sources = load_sources(args.sources)
    report = check_stored_links(engine, sample_size=args.sample, sources=sources)
    print(f"Sampled {report.sampled} stored application links")
    print(f"  resolve to a live page:            {report.resolved} ({report.resolved_share:.0%})")
    print(f"  page mentions the title or company: {report.matched} ({report.matched_share:.0%})")
    for url, reason in report.problems:
        print(f"  - {url}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
