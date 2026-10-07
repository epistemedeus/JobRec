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

A redirect is followed only when it stays on the same board and, for a job
request, the same job id. Another path on the API host is not evidence.

``requests`` timeout is an idle/connect timeout. On the main thread each
exchange is also bounded by ITIMER_REAL for the time left in the listing
budget, and the previous handler and interval timer are restored. A body or a
cached board observed only after that budget is unknown and is not cached.
Where that timer cannot be armed, the result is unknown and nothing is requested.

A job 404 body of ``{"status": 404, "error": "Job not found"}`` is also what
that API returns for a board token it does not host. Closure therefore requires
the separate board record's ``name`` to match the configured company. Anything
else stays unknown, which keeps the listing.
"""

import json
import re
import signal
import threading
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
_TIME_LIMIT = "greenhouse time limit reached"
_REQUEST_LIMIT = "greenhouse request limit reached"
_DEADLINE_UNSUPPORTED = "greenhouse deadline cannot be enforced in this context"


class _DeadlineExceeded(Exception):
    """The wall-clock interval elapsed while an exchange was still running."""


class _DeadlineUnsupported(Exception):
    """This call cannot arm a real total deadline."""


def _deadline_context_supported() -> bool:
    """A real total bound needs the main thread and an interval timer.

    ``requests`` timeout is an idle/connect timeout. ITIMER_REAL is what
    interrupts a read that keeps receiving bytes. Off the main thread that
    timer cannot be armed, so the caller must not pretend the bound exists.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    return hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")


def _handle_wall_deadline(signum, frame):
    raise _DeadlineExceeded()


def _restore_unrelated_timer(old_delay: float, old_interval: float, elapsed: float) -> None:
    """Put back a timer this exchange paused, without replacing its handler."""
    if old_delay <= 0:
        return
    remaining = old_delay - elapsed
    if remaining <= 0:
        # It was due while this exchange held the only interval timer.
        remaining = 1e-4
    signal.setitimer(signal.ITIMER_REAL, remaining, old_interval)


def _run_with_wall_deadline(action, seconds: float):
    """Run ``action`` and interrupt it when ``seconds`` of wall time elapse.

    The previous SIGALRM handler and any pending ITIMER_REAL are restored,
    including when the deadline fires. This is not a process-wide guard.
    """
    if not _deadline_context_supported():
        raise _DeadlineUnsupported(_DEADLINE_UNSUPPORTED)
    if seconds <= 0:
        raise _DeadlineExceeded()
    started = time.monotonic()
    previous_handler = signal.signal(signal.SIGALRM, _handle_wall_deadline)
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        return action()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        _restore_unrelated_timer(old_delay, old_interval, time.monotonic() - started)


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
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    return host == API_HOST and port in (None, 443)


def _resource_identity(url: str) -> Optional[tuple]:
    """Board token and job id named by an API URL.

    The job id is None for the board record itself. A different path on the
    same host is a different resource. The query is not part of the identity:
    a same-path redirect may add one.
    """
    if not _is_api_url(url):
        return None
    try:
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    except ValueError:
        return None
    if len(segments) < 3 or segments[0] != "v1" or segments[1] != "boards":
        return None
    token = segments[2]
    if _BOARD_TOKEN.fullmatch(token) is None:
        return None
    if len(segments) == 3:
        return (token, None)
    if len(segments) == 5 and segments[3] == "jobs" and _JOB_ID.fullmatch(segments[4]):
        return (token, segments[4])
    return None


def _read_limited(response, max_bytes: int, deadline: float, clock: Callable[[], float]) -> tuple[bytes, bool, bool]:
    """Read at most max_bytes. The third value is true when the clock is past the deadline."""
    chunks = []
    remaining = max_bytes + 1
    late = clock() >= deadline
    for chunk in response.iter_content(chunk_size=8192):
        if clock() >= deadline:
            late = True
            break
        if not chunk:
            continue
        take = chunk[:remaining]
        chunks.append(take)
        remaining -= len(take)
        if remaining <= 0:
            break
    if clock() >= deadline:
        late = True
    data = b"".join(chunks)
    truncated = len(data) > max_bytes
    return data[:max_bytes], truncated, late


@dataclass
class _Exchange:
    kind: str
    status: Optional[int] = None
    location: str = ""
    content_type: str = ""
    data: bytes = b""
    truncated: bool = False
    detail: str = ""


def _bounded_exchange(
    session,
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    seconds: float,
    clock: Callable[[], float],
    deadline: float,
) -> _Exchange:
    """GET one URL and read its body, without following redirects.

    ``timeout`` is the requests idle/connect timeout. ``seconds`` is a wall-clock
    bound around the whole exchange, including a body that keeps trickling.
    The response is closed before this returns.
    """
    if not _deadline_context_supported():
        return _Exchange(kind="unsupported", detail=_DEADLINE_UNSUPPORTED)
    held: list = []

    def action():
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        held.append(response)
        status = response.status_code
        if status in _REDIRECT_STATUSES:
            return _Exchange(kind="redirect", status=status, location=_header(response, "Location"))
        try:
            data, truncated, late = _read_limited(response, max_bytes, deadline, clock)
        except (requests.RequestException, OSError) as exc:
            return _Exchange(kind="error", status=status, detail=f"greenhouse request failed: {exc}")
        if late:
            return _Exchange(kind="late", status=status)
        return _Exchange(
            kind="body", status=status, content_type=_header(response, "Content-Type"), data=data, truncated=truncated,
        )

    try:
        if seconds <= 0:
            return _Exchange(kind="late")
        try:
            return _run_with_wall_deadline(action, seconds)
        except _DeadlineExceeded:
            return _Exchange(kind="late")
        except _DeadlineUnsupported:
            return _Exchange(kind="unsupported", detail=_DEADLINE_UNSUPPORTED)
        except (requests.RequestException, OSError) as exc:
            return _Exchange(kind="error", detail=f"greenhouse request failed: {exc}")
    finally:
        if held:
            held[0].close()


def _request(session, url: str, deadline: float, counter: list, clock: Callable[[], float], timeout: float, max_bytes: int) -> _Fetched:
    """GET one API URL. Redirects must stay on that same board and job."""
    origin = _resource_identity(url)
    current = url
    redirects = 0
    while True:
        if counter[0] >= MAX_REQUESTS:
            return _Fetched(error=_REQUEST_LIMIT, final_url=current)
        remaining = deadline - clock()
        if remaining < 0.05:
            return _Fetched(error=_TIME_LIMIT, final_url=current)
        if not _deadline_context_supported():
            return _Fetched(error=_DEADLINE_UNSUPPORTED, final_url=current)
        if origin is None or _resource_identity(current) != origin:
            return _Fetched(error="greenhouse redirect does not name the requested resource", final_url=current)
        counter[0] += 1
        exchange = _bounded_exchange(
            session,
            current,
            timeout=min(timeout, remaining),
            max_bytes=max_bytes,
            seconds=remaining,
            clock=clock,
            deadline=deadline,
        )
        if exchange.kind == "unsupported":
            return _Fetched(error=_DEADLINE_UNSUPPORTED, final_url=current)
        if exchange.kind == "late" or clock() >= deadline:
            return _Fetched(status=exchange.status, error=_TIME_LIMIT, final_url=current)
        if exchange.kind == "error":
            return _Fetched(status=exchange.status, error=exchange.detail or "greenhouse request failed", final_url=current)
        status = exchange.status
        if exchange.kind == "redirect":
            if redirects >= MAX_REDIRECTS:
                return _Fetched(status=status, final_url=current, error="greenhouse redirect limit reached")
            location = exchange.location
            if not location:
                return _Fetched(status=status, final_url=current, error="greenhouse redirect had no location")
            try:
                nxt = urljoin(current, location)
                same_host = _is_api_url(nxt)
                same_resource = _resource_identity(nxt) == origin
            except ValueError:
                return _Fetched(status=status, final_url=current, error="greenhouse redirect could not be read")
            if not same_host:
                return _Fetched(status=status, final_url=current, error="greenhouse redirected to a different host")
            if not same_resource:
                return _Fetched(status=status, final_url=current, error="greenhouse redirect does not name the requested resource")
            redirects += 1
            current = nxt
            continue
        if exchange.truncated:
            return _Fetched(status=status, final_url=current, error="greenhouse response exceeded the byte limit")
        content_type = exchange.content_type.lower()
        text = exchange.data.decode("utf-8", errors="replace")
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
    # A cached answer is an observation too. It cannot authorize closure after the budget.
    if clock() >= deadline:
        return (False, _TIME_LIMIT)
    cache_key = (board_token, company_name)
    cached = board_cache.get(cache_key)
    if cached is not None:
        return cached
    fetched = _request(
        session, f"https://{API_HOST}/v1/boards/{board_token}", deadline, counter, clock, timeout, max_bytes,
    )
    # A listing that ran out of time or requests has not learned anything about the board.
    # The next listing gets its own budget, so that miss is not cached.
    if clock() >= deadline or fetched.error in (_TIME_LIMIT, _REQUEST_LIMIT):
        if clock() >= deadline:
            return (False, _TIME_LIMIT)
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
    if not _deadline_context_supported():
        return LinkResult(LinkStatus.UNKNOWN, _DEADLINE_UNSUPPORTED)

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
    # The body, not the start of session.get, is the observation. A clock that
    # moved while bytes were still arriving cannot authorize open or closed.
    if clock() >= deadline:
        return _unknown(_TIME_LIMIT, fetched)
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
        if clock() >= deadline:
            return _unknown(_TIME_LIMIT, fetched)
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

    A greenhouse listing whose board is missing, ambiguous, or not a single
    slug is unknown. Its application page is not requested, because a page 404
    would otherwise delete a listing the registry does not identify. Every
    other scraper keeps the existing page check. A resolved Greenhouse company
    with a missing or non-decimal post id is likewise unknown.
    """
    if sources is None:
        sources = load_status_sources()
    if board_cache is None:
        board_cache = {}
    if source == "greenhouse":
        token = resolve_greenhouse_board(company_name, sources)
        if not token:
            return LinkResult(LinkStatus.UNKNOWN, "greenhouse board is not configured for this company")
        if not isinstance(source_job_id, str) or _JOB_ID.fullmatch(source_job_id) is None:
            return LinkResult(LinkStatus.UNKNOWN, "configured greenhouse job id is not a decimal post id")
        return check_greenhouse_job(
            token, source_job_id, company_name, session, board_cache=board_cache,
        )
    return check_link(url, session, job_id=job_id if job_id is not None else source_job_id)
