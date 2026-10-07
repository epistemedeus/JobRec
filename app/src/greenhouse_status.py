"""Confirm one Greenhouse listing through the public Job Board API.

The application page is the wrong witness for a board that does not answer
automated requests. This module asks the unauthenticated per-job endpoint
instead, and only for a board token that the checked-in source registry names
for that company. It never reads a board token out of the application URL.

Documented endpoints, read 2026-10-07 from
https://developers.greenhouse.io/job-board.html (served from
https://docs.greenhouse.io/job-board.html):

- ``GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}``
- ``GET https://boards-api.greenhouse.io/v1/boards/{board_token}``

GET needs no authentication. The application POST is not used.

A job 404 body of ``{"status": 404, "error": "Job not found"}`` is also what
that API returns for a board token it does not host. Closure therefore requires
the separate board record's ``name`` to match the configured company. Anything
else stays unknown, which keeps the listing.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests

from link_check import DEFAULT_MAX_BYTES, USER_AGENT, LinkResult, LinkStatus, check_link

API_HOST = "boards-api.greenhouse.io"
MAX_REDIRECTS = 2
MAX_REQUESTS = 4
MAX_BYTES = DEFAULT_MAX_BYTES
REQUEST_TIMEOUT = 8
TOTAL_DEADLINE_SECONDS = 12

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# Registry ids are slugs such as "roblox". Reject anything that could change the path or host.
_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,63})$")
# Greenhouse job post ids are positive decimals. The scraper stores them as strings.
_JOB_ID = re.compile(r"^[1-9][0-9]{0,17}$")
_JOB_NOT_FOUND = "Job not found"


@dataclass
class _Fetched:
    status: Optional[int] = None
    final_url: Optional[str] = None
    text: str = ""
    payload: Any = None
    html: bool = False
    error: Optional[str] = None


def _source_fields(source) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if isinstance(source, dict):
        return source.get("scraper"), source.get("company_name"), source.get("id")
    return getattr(source, "scraper", None), getattr(source, "company_name", None), getattr(source, "id", None)


def resolve_greenhouse_board(company_name: Optional[str], sources) -> Optional[str]:
    """Return the one registry board id for this exact company, or None.

    Zero matches, or two different ids, do not guess. A token that is not a
    single slug is ignored so it cannot be pasted into the request path.
    """
    if not isinstance(company_name, str) or company_name == "":
        return None
    tokens = set()
    for source in sources:
        scraper, name, token = _source_fields(source)
        if scraper != "greenhouse" or name != company_name or not isinstance(token, str):
            continue
        tokens.add(token)
    if len(tokens) != 1:
        return None
    token = tokens.pop()
    if _BOARD_TOKEN.fullmatch(token) is None:
        return None
    return token


def load_status_sources() -> list:
    """Load the checked-in registries that name boards the status check may use.

    The default registry and the demo registry are both explicit configuration.
    Roblox is listed only in the demo registry. Identical entries are collapsed.
    A company whose files name two different Greenhouse ids is left unresolved.
    """
    from pathlib import Path

    from job_sources import DEFAULT_SOURCES_PATH, load_sources

    paths = (DEFAULT_SOURCES_PATH, Path(DEFAULT_SOURCES_PATH).with_name("job_sources_demo.json"))
    loaded = []
    seen = set()
    for path in paths:
        if not path.exists():
            continue
        for source in load_sources(path):
            key = (source.scraper, source.id, source.company_name)
            if key in seen:
                continue
            seen.add(key)
            loaded.append(source)
    return loaded


def _header(response, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return "" if value is None else str(value)
    return ""


def _is_api_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    return host == API_HOST and parts.port in (None, 443)


def _read_limited(response, max_bytes: int) -> tuple[bytes, bool]:
    chunks = []
    remaining = max_bytes + 1
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        take = chunk[:remaining]
        chunks.append(take)
        remaining -= len(take)
        if remaining <= 0:
            break
    data = b"".join(chunks)
    truncated = len(data) > max_bytes
    return data[:max_bytes], truncated


def _request(session, url: str, deadline: float, counter: list, clock: Callable[[], float], timeout: float, max_bytes: int) -> _Fetched:
    """GET one API URL, following only a few redirects that stay on the API host."""
    current = url
    redirects = 0
    while True:
        if counter[0] >= MAX_REQUESTS:
            return _Fetched(error="greenhouse request limit reached", final_url=current)
        remaining = deadline - clock()
        if remaining < 0.05:
            return _Fetched(error="greenhouse time limit reached", final_url=current)
        if not _is_api_url(current):
            return _Fetched(error="greenhouse redirected to a different host", final_url=current)
        counter[0] += 1
        try:
            response = session.get(
                current,
                timeout=min(timeout, remaining),
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except (requests.RequestException, OSError) as exc:
            return _Fetched(error=f"greenhouse request failed: {exc}", final_url=current)
        try:
            status = response.status_code
            if status in _REDIRECT_STATUSES:
                if redirects >= MAX_REDIRECTS:
                    return _Fetched(status=status, final_url=current, error="greenhouse redirect limit reached")
                location = _header(response, "Location")
                if not location:
                    return _Fetched(status=status, final_url=current, error="greenhouse redirect had no location")
                nxt = urljoin(current, location)
                if not _is_api_url(nxt):
                    return _Fetched(status=status, final_url=current, error="greenhouse redirected to a different host")
                redirects += 1
                current = nxt
                continue
            content_type = _header(response, "Content-Type").lower()
            try:
                data, truncated = _read_limited(response, max_bytes)
            except (requests.RequestException, OSError) as exc:
                return _Fetched(status=status, final_url=current, error=f"greenhouse request failed: {exc}")
        finally:
            response.close()

        if truncated:
            return _Fetched(status=status, final_url=current, error="greenhouse response exceeded the byte limit")
        text = data.decode("utf-8", errors="replace")
        stripped = text.lstrip()
        if "html" in content_type or stripped[:9].lower() == "<!doctype" or stripped[:5].lower() == "<html":
            return _Fetched(status=status, final_url=current, text=text, html=True)
        payload = None
        if text.strip():
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        return _Fetched(status=status, final_url=current, text=text, payload=payload)


def _same_name(value, expected: str) -> bool:
    return isinstance(value, str) and isinstance(expected, str) and value.strip() != "" and value.strip() == expected.strip()


def _id_matches(value, job_id: str) -> bool:
    # bool is an int subclass and must not count as a job id.
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return str(value) == job_id
    if isinstance(value, str):
        return value == job_id
    return False


def _job_kind(payload, job_id: str, company_name: str) -> str:
    """Classify a 200 body as open, unrelated, or malformed."""
    if not isinstance(payload, dict):
        return "malformed"
    if "id" not in payload or "title" not in payload or "absolute_url" not in payload:
        return "malformed"
    if not _id_matches(payload.get("id"), job_id):
        return "unrelated" if payload.get("id") not in (None, "") else "malformed"
    title = payload.get("title")
    absolute = payload.get("absolute_url")
    if not isinstance(title, str) or title.strip() == "" or not isinstance(absolute, str) or absolute.strip() == "":
        return "malformed"
    if not _same_name(payload.get("company_name"), company_name):
        return "unrelated"
    return "open"


def _is_job_not_found(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != 404 or payload.get("error") != _JOB_NOT_FOUND:
        return False
    return "id" not in payload and "title" not in payload and "absolute_url" not in payload


def _board_matches(payload, company_name: str) -> bool:
    if not isinstance(payload, dict) or "error" in payload:
        return False
    return _same_name(payload.get("name"), company_name)


def _verify_board(session, board_token: str, company_name: str, deadline, counter, clock, timeout, max_bytes, board_cache) -> tuple[bool, str]:
    cache_key = (board_token, company_name)
    cached = board_cache.get(cache_key)
    if cached is not None:
        return cached
    fetched = _request(
        session, f"https://{API_HOST}/v1/boards/{board_token}", deadline, counter, clock, timeout, max_bytes,
    )
    # A listing that ran out of time or requests has not learned anything about the board.
    # The next listing gets its own budget, so that miss is not cached.
    if fetched.error in ("greenhouse time limit reached", "greenhouse request limit reached"):
        return (False, fetched.error)
    if fetched.error:
        result = (False, fetched.error)
    elif fetched.html or fetched.status != 200 or not _board_matches(fetched.payload, company_name):
        result = (False, "board probe did not match the configured company")
    else:
        result = (True, "verified")
    board_cache[cache_key] = result
    return result


def _unknown(reason: str, fetched: Optional[_Fetched] = None) -> LinkResult:
    if fetched is None:
        return LinkResult(LinkStatus.UNKNOWN, reason)
    return LinkResult(LinkStatus.UNKNOWN, reason, fetched.status, fetched.final_url, fetched.text, fetched.text)


def check_greenhouse_job(
    board_token: str,
    job_id: str,
    company_name: str,
    session,
    *,
    board_cache: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    clock: Callable[[], float] = time.monotonic,
    deadline_s: float = TOTAL_DEADLINE_SECONDS,
) -> LinkResult:
    """Return OPEN, CLOSED, or UNKNOWN for one configured Greenhouse post id.

    OPEN is an exact job record: the id matches, the title and absolute URL are
    present, and ``company_name`` matches the configured company. CLOSED is a
    job-not-found body on a board whose own record names that same company.
    A wrong or unverified board, a different id, a page, a timeout, a rate
    limit, or a 5xx stays UNKNOWN.
    """
    if board_cache is None:
        board_cache = {}
    if _BOARD_TOKEN.fullmatch(board_token or "") is None:
        return LinkResult(LinkStatus.UNKNOWN, "greenhouse board token is not a configured slug")
    if _JOB_ID.fullmatch(job_id or "") is None:
        return LinkResult(LinkStatus.UNKNOWN, "configured greenhouse job id is not a decimal post id")
    if not isinstance(company_name, str) or company_name.strip() == "":
        return LinkResult(LinkStatus.UNKNOWN, "configured greenhouse company name is empty")

    deadline = clock() + deadline_s
    counter = [0]
    fetched = _request(
        session,
        f"https://{API_HOST}/v1/boards/{board_token}/jobs/{job_id}",
        deadline,
        counter,
        clock,
        timeout,
        max_bytes,
    )
    if fetched.error:
        return _unknown(fetched.error, fetched)
    if fetched.html:
        return _unknown("greenhouse returned a page instead of a job record", fetched)
    if fetched.status == 200:
        kind = _job_kind(fetched.payload, job_id, company_name)
        if kind == "open":
            return LinkResult(
                LinkStatus.OPEN,
                "greenhouse job record matches the configured board and id",
                fetched.status,
                fetched.final_url,
                fetched.text,
                fetched.text,
            )
        if kind == "unrelated":
            return _unknown("greenhouse payload does not match the configured job", fetched)
        return _unknown("greenhouse payload is not a job record", fetched)
    if fetched.status == 404 and _is_job_not_found(fetched.payload):
        # The same 404 body is returned when the board token itself is unknown.
        verified, detail = _verify_board(
            session, board_token, company_name, deadline, counter, clock, timeout, max_bytes, board_cache,
        )
        if verified:
            return LinkResult(
                LinkStatus.CLOSED,
                "greenhouse job not found on the verified board",
                fetched.status,
                fetched.final_url,
                fetched.text,
                fetched.text,
            )
        reason = "greenhouse job not found, but the board was not verified"
        if detail and detail != "verified":
            reason = f"{reason}: {detail}"
        return _unknown(reason, fetched)
    if fetched.status is None:
        return _unknown(fetched.error or "greenhouse request failed", fetched)
    return _unknown(f"greenhouse HTTP {fetched.status}", fetched)


def check_configured_link(
    url: str,
    session,
    *,
    source: Optional[str] = None,
    source_job_id: Optional[str] = None,
    company_name: Optional[str] = None,
    sources=None,
    board_cache: Optional[dict] = None,
    job_id: Optional[str] = None,
) -> LinkResult:
    """Check a stored listing, using the Greenhouse API only for a resolved board.

    Every other listing keeps the existing page check. A configured Greenhouse
    company with a missing or non-decimal post id is unknown rather than judged
    from the application page, because a page 404 would otherwise look closed.
    """
    if sources is None:
        sources = load_status_sources()
    if board_cache is None:
        board_cache = {}
    if source == "greenhouse":
        token = resolve_greenhouse_board(company_name, sources)
        if token:
            if not isinstance(source_job_id, str) or _JOB_ID.fullmatch(source_job_id) is None:
                return LinkResult(LinkStatus.UNKNOWN, "configured greenhouse job id is not a decimal post id")
            return check_greenhouse_job(
                token, source_job_id, company_name, session, board_cache=board_cache,
            )
    return check_link(url, session, job_id=job_id if job_id is not None else source_job_id)
