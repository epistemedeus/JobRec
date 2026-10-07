"""Remove stored job listings that no longer accept applicants.

Pruning works in two steps so that it never deletes on a guess:

1. Nominate. A listing is a candidate when its company's most recent scrape did
   not report it (its last-seen time is older than the newest last-seen time for
   the same source and company), or when nothing has reported it for a week.
   Listings the latest scrape did report are known to be open and are not touched.
2. Confirm. Each candidate's application link is followed (see link_check.py).
   A Greenhouse listing whose company matches exactly one configured board is
   confirmed through that board's per-job API instead (see greenhouse_status.py).
   A Greenhouse listing with no single configured board is unknown and its page
   is not requested. Only a listing whose check positively shows it is closed is
   removed. A failed or blocked request, or an unverified board, leaves the
   listing in place and is logged.

Being absent from a scrape is never enough on its own: some scrapers fetch only
part of a large board, so an absent listing may well still be open.
"""

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests
from sqlalchemy import Engine, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from db import Base
from greenhouse_status import check_configured_link, load_status_sources
from job_store import JobListingTable, _to_db
from link_check import LinkStatus

logger = logging.getLogger(__name__)

# A listing nobody has reported for this long is checked even if its company was not scraped since.
DEFAULT_STALE_AFTER = timedelta(days=7)
# Seconds between two requests to the same host.
DEFAULT_PAUSE = 0.5
# A host that fails this many times in a row is left alone for the rest of the run.
MAX_CONSECUTIVE_FAILURES = 3
# If more than half of this many (or more) checked listings on one host look closed, the site
# is more likely misbehaving than the jobs really closing, so nothing on that host is deleted.
MASS_CLOSURE_MIN_CHECKED = 20


@dataclass
class PruneCandidate:
    id: int
    source: str
    source_job_id: Optional[str]
    company_name: str
    title: str
    application_url: str

    @property
    def host(self) -> str:
        return (urlsplit(self.application_url).hostname or "").lower()


@dataclass
class PruneResult:
    checked: int = 0   # links actually requested
    closed: int = 0    # listings whose link showed the job is closed
    removed: int = 0   # listings deleted (0 in a dry run)
    kept: int = 0      # listings confirmed still open
    unknown: int = 0   # listings that could not be determined, including ones skipped by a guard


def find_prune_candidates(
    engine: Engine, *, now: Optional[datetime] = None, stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> list[PruneCandidate]:
    """Return the stored listings worth checking, oldest id first."""
    now = _to_db(now or datetime.now(timezone.utc))
    table = JobListingTable.__table__
    latest = (
        select(table.c.source, table.c.company_name, func.max(table.c.last_seen_at).label("latest"))
        .group_by(table.c.source, table.c.company_name)
        .subquery()
    )
    query = (
        select(table.c.id, table.c.source, table.c.source_job_id, table.c.company_name, table.c.title, table.c.application_url)
        .join(latest, and_(table.c.source == latest.c.source, table.c.company_name == latest.c.company_name))
        .where(or_(table.c.last_seen_at < latest.c.latest, table.c.last_seen_at < now - stale_after))
        .order_by(table.c.id)
    )
    with Session(engine) as session:
        return [PruneCandidate(*row) for row in session.execute(query)]


def delete_listings(engine: Engine, listing_ids: list[int]) -> int:
    """Delete listings by id, first removing rows in other tables that refer to them. Returns the number deleted."""
    if not listing_ids:
        return 0
    listings = JobListingTable.__table__
    with Session(engine) as session:
        for table in Base.metadata.tables.values():
            if table is listings:
                continue
            for column in table.c:
                if any(fk.column.table is listings for fk in column.foreign_keys):
                    session.execute(delete(table).where(column.in_(listing_ids)))
        deleted = session.execute(delete(listings).where(listings.c.id.in_(listing_ids))).rowcount
        session.commit()
    return deleted


def prune_closed_listings(
    engine: Engine,
    session=None,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    sleep: Callable[[float], None] = time.sleep,
    pause: float = DEFAULT_PAUSE,
    rng: Optional[random.Random] = None,
    sources=None,
) -> PruneResult:
    """Check candidate listings' links and remove the ones that are closed.

    With ``dry_run`` (the default) nothing is deleted and the result reports what would be.
    ``limit`` caps how many links are followed; candidates are shuffled first so repeated
    limited runs do not keep checking the same ones.
    """
    session = session or requests.Session()
    registry = load_status_sources() if sources is None else list(sources)
    board_cache: dict = {}
    candidates = find_prune_candidates(engine, now=now, stale_after=stale_after)
    if limit is not None:
        (rng or random.Random()).shuffle(candidates)
        candidates = candidates[:limit]

    result = PruneResult()
    closed_by_host: dict[str, list[PruneCandidate]] = {}
    checked_by_host: dict[str, int] = {}
    failures_in_a_row: dict[str, int] = {}

    for candidate in candidates:
        host = candidate.host
        if failures_in_a_row.get(host, 0) >= MAX_CONSECUTIVE_FAILURES:
            result.unknown += 1
            continue
        if checked_by_host.get(host):
            sleep(pause)

        link = check_configured_link(
            candidate.application_url,
            session,
            source=candidate.source,
            source_job_id=candidate.source_job_id,
            company_name=candidate.company_name,
            sources=registry,
            board_cache=board_cache,
            job_id=candidate.source_job_id,
        )
        result.checked += 1
        checked_by_host[host] = checked_by_host.get(host, 0) + 1

        if link.status is LinkStatus.CLOSED:
            failures_in_a_row[host] = 0
            result.closed += 1
            closed_by_host.setdefault(host, []).append(candidate)
            logger.info(
                "Closed: %s at %s (%s): %s", candidate.title, candidate.company_name, candidate.application_url, link.reason,
            )
        elif link.status is LinkStatus.OPEN:
            failures_in_a_row[host] = 0
            result.kept += 1
        else:
            failures_in_a_row[host] = failures_in_a_row.get(host, 0) + 1
            result.unknown += 1
            logger.warning(
                "Could not determine whether %s is still open; keeping it: %s", candidate.application_url, link.reason,
            )
            if failures_in_a_row[host] == MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "Skipping the remaining listings on %s after %d failures in a row", host, MAX_CONSECUTIVE_FAILURES,
                )

    to_delete: list[int] = []
    for host, closed in closed_by_host.items():
        checked = checked_by_host[host]
        if checked >= MASS_CLOSURE_MIN_CHECKED and len(closed) > checked / 2:
            logger.error(
                "%d of %d checked listings on %s look closed; that is more likely a problem with the site "
                "than real closures, so none of them are removed", len(closed), checked, host,
            )
            continue
        to_delete.extend(candidate.id for candidate in closed)

    if not dry_run:
        result.removed = delete_listings(engine, to_delete)
    logger.info(
        "Pruning%s: %d checked, %d closed, %d removed, %d still open, %d undetermined",
        " (dry run)" if dry_run else "", result.checked, result.closed, result.removed, result.kept, result.unknown,
    )
    return result
