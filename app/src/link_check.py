"""Follow a job's application link and judge whether the job still accepts applicants.

The judgement is deliberately cautious, because pruning deletes listings on the
strength of it. A link is CLOSED only on positive evidence that the job is gone.
Anything that merely failed, was blocked, or went somewhere unexpected is
UNKNOWN, and an ordinary page is OPEN.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import requests

from job_normalization import clean_text

logger = logging.getLogger(__name__)

# Some sites refuse requests that do not look like a browser.
USER_AGENT = "Mozilla/5.0 (compatible; JobRecLinkCheck/1.0; +https://github.com/Work-At-JobRec/JobRec)"
DEFAULT_TIMEOUT = 10
# Only the start of a page is read; the closed notice, when there is one, is near the top.
DEFAULT_MAX_BYTES = 200_000

# Status codes that mean the page itself no longer exists.
_GONE_STATUSES = (404, 410)
# Phrases a careers site shows in place of a job that has closed. Matched against the
# visible text only, because script bundles often contain these strings for every page.
_CLOSED_MARKERS = (
    "no longer accepting applications",
    "job is no longer available",
    "position is no longer available",
    "posting is no longer available",
    "position has been filled",
    "job has been filled",
    "job has expired",
    "posting has expired",
    "job not found",
    "job posting not found",
)


class LinkStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass
class LinkResult:
    status: LinkStatus
    reason: str
    http_status: Optional[int] = None
    final_url: Optional[str] = None
    # Visible text of the page (start of it); this is what the closed markers are matched against.
    text: str = ""
    # Page source (start of it), including metadata and structured data that pages rendered in the
    # browser carry instead of visible text; for callers that want to check what the page is about.
    raw: str = ""


def _read_start(response, max_bytes: int) -> str:
    """Read at most max_bytes of the response body and decode it."""
    chunks = []
    remaining = max_bytes
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        chunks.append(chunk[:remaining])
        remaining -= len(chunks[-1])
        if remaining <= 0:
            break
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _greenhouse_host(hostname: Optional[str]) -> bool:
    """True for greenhouse.io and its real subdomains, not a lookalike suffix."""
    host = (hostname or "").rstrip(".").lower()
    return host == "greenhouse.io" or host.endswith(".greenhouse.io")


def _error_true_parameter(query: str) -> bool:
    """True when the query has an ``error`` parameter whose value is ``true``.

    ``noterror=true`` is a different parameter. A substring of the raw query is not.
    """
    return any(value == "true" for value in parse_qs(query).get("error", ()))


def check_link(
    url: str,
    session,
    timeout: float = DEFAULT_TIMEOUT,
    job_id: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> LinkResult:
    """Request a job's application URL and report whether the job is open, closed, or undetermined.

    ``job_id`` is the source's id for the job; when given, a redirect to an address that
    no longer contains it (typically a general careers page) is treated as undetermined.
    """
    if not (url or "").strip():
        return LinkResult(LinkStatus.UNKNOWN, "listing has no application URL")
    try:
        response = session.get(
            url, timeout=timeout, allow_redirects=True, stream=True, headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        return LinkResult(LinkStatus.UNKNOWN, f"request failed: {exc}")

    try:
        status = response.status_code
        final_url = response.url or url
        redirected = bool(response.history) or final_url != url
        if redirected:
            parts = urlsplit(final_url)
            # Greenhouse sends a closed job to the company's own board with error=true.
            if _greenhouse_host(parts.hostname) and _error_true_parameter(parts.query):
                return LinkResult(LinkStatus.CLOSED, "redirected to the board with error=true", status, final_url)
            # A redirect that drops the job id is not evidence, including a 404 of that other page.
            if job_id and job_id not in final_url:
                return LinkResult(LinkStatus.UNKNOWN, "redirected to a page that no longer names the job", status, final_url)
        if status in _GONE_STATUSES:
            return LinkResult(LinkStatus.CLOSED, f"HTTP {status}", status, final_url)
        if status != 200:
            return LinkResult(LinkStatus.UNKNOWN, f"HTTP {status}", status, final_url)

        try:
            raw = _read_start(response, max_bytes)
        except requests.RequestException as exc:
            return LinkResult(LinkStatus.UNKNOWN, f"reading the page failed: {exc}", status, final_url)
        text = clean_text(raw)
        lowered = text.lower()
        for marker in _CLOSED_MARKERS:
            if marker in lowered:
                return LinkResult(LinkStatus.CLOSED, f'page says "{marker}"', status, final_url, text, raw)
        return LinkResult(LinkStatus.OPEN, "page loaded", status, final_url, text, raw)
    finally:
        response.close()
