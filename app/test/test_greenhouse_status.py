import os
import sys

import requests

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC_DIR)

from greenhouse_status import (  # noqa: E402
    MAX_REDIRECTS,
    MAX_REQUESTS,
    check_configured_link,
    check_greenhouse_job,
    load_status_sources,
    resolve_greenhouse_board,
)
from link_check import USER_AGENT, LinkStatus  # noqa: E402
from link_fakes import FakeSession, json_page, page  # noqa: E402

API = "https://boards-api.greenhouse.io"
JOB = f"{API}/v1/boards/roblox/jobs/8143982"
BOARD = f"{API}/v1/boards/roblox"
NOT_FOUND = {"status": 404, "error": "Job not found"}


class Board:
    def __init__(self, scraper, board_id, company_name, enabled=True):
        self.scraper = scraper
        self.id = board_id
        self.company_name = company_name
        self.enabled = enabled


def record(job_id="8143982", company="Roblox", title="Software Engineer"):
    return {
        "id": int(job_id),
        "title": title,
        "company_name": company,
        "absolute_url": f"https://careers.roblox.com/jobs/{job_id}?gh_jid={job_id}",
    }


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# The checked-in demo registry names Roblox, and a different spelling is not a match
def test_checked_in_registries_resolve_roblox_and_nothing_nearby():
    sources = load_status_sources()

    assert resolve_greenhouse_board("Roblox", sources) == "roblox"
    assert resolve_greenhouse_board("Stripe", sources) == "stripe"
    assert resolve_greenhouse_board("roblox", sources) is None
    assert resolve_greenhouse_board("NVIDIA", sources) is None
    assert resolve_greenhouse_board("Palantir", sources) is None


# One slug is required. A disabled entry still counts; two ids or a non-slug do not.
def test_resolve_requires_one_safe_greenhouse_id():
    assert resolve_greenhouse_board("Roblox", [Board("greenhouse", "roblox", "Roblox", enabled=False)]) == "roblox"
    assert resolve_greenhouse_board("Roblox", [
        Board("greenhouse", "roblox", "Roblox"),
        Board("greenhouse", "other", "Roblox"),
    ]) is None
    assert resolve_greenhouse_board("Roblox", [Board("greenhouse", "../roblox", "Roblox")]) is None
    assert resolve_greenhouse_board("Roblox", [Board("lever", "roblox", "Roblox")]) is None
    assert resolve_greenhouse_board("Roblox", [Board("greenhouse", "roblox", "Roblox Corporation")]) is None


# A same-host redirect can still reach an exact record
def test_same_host_redirect_then_exact_record_is_open():
    landed = JOB + "?redirect=1"
    session = FakeSession({
        JOB: page(302, "", headers={"Location": landed}),
        landed: json_page(200, record()),
    })

    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)

    assert result.status is LinkStatus.OPEN
    assert session.calls == [JOB, landed]


# A redirect leaves the API host immediately
def test_redirect_off_host_is_unknown_and_not_followed():
    session = FakeSession({JOB: page(302, "", headers={"Location": "https://careers.roblox.com/jobs/8143982"})})

    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)

    assert result.status is LinkStatus.UNKNOWN
    assert "different host" in result.reason
    assert session.calls == [JOB]


# Redirects stop after the configured hop limit
def test_redirect_limit_is_unknown():
    session = FakeSession({})

    def get(url, **kwargs):
        session.calls.append(url)
        return page(302, "", headers={"Location": url})

    session.get = get
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)

    assert result.status is LinkStatus.UNKNOWN
    assert "redirect limit" in result.reason
    assert len(session.calls) == MAX_REDIRECTS + 1
    assert len(session.calls) <= MAX_REQUESTS


# The body cap rejects a truncated payload instead of parsing a prefix
def test_truncated_body_is_unknown():
    session = FakeSession({JOB: json_page(200, record(title="x" * 500))})

    result = check_greenhouse_job("roblox", "8143982", "Roblox", session, max_bytes=40)

    assert result.status is LinkStatus.UNKNOWN
    assert "byte limit" in result.reason


# A job 404 does not spend a probe once the deadline is already gone
def test_time_limit_skips_the_board_probe():
    clock = Clock()

    class Advance(FakeSession):
        def get(self, url, **kwargs):
            clock.t += 100
            return super().get(url, **kwargs)

    session = Advance({JOB: json_page(404, NOT_FOUND), BOARD: json_page(200, {"name": "Roblox"})})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session, clock=clock, deadline_s=12)

    assert result.status is LinkStatus.UNKNOWN
    assert "time limit" in result.reason or "not verified" in result.reason
    assert BOARD not in session.calls


# The request carries a bounded timeout, no automatic redirects, and the checker user agent
def test_request_bounds_timeout_redirects_and_user_agent():
    seen = {}

    class Recording(FakeSession):
        def get(self, url, **kwargs):
            seen.update(kwargs)
            return super().get(url, **kwargs)

    session = Recording({JOB: json_page(200, record())})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session, timeout=8)

    assert result.status is LinkStatus.OPEN
    assert seen["allow_redirects"] is False
    assert seen["stream"] is True
    assert seen["timeout"] <= 8
    assert seen["headers"]["User-Agent"] == USER_AGENT
    assert seen["headers"]["Accept"] == "application/json"


# A string id matches; a boolean id and a different company do not
def test_record_must_match_id_and_company():
    string_id = record()
    string_id["id"] = "8143982"
    assert check_greenhouse_job("roblox", "8143982", "Roblox", FakeSession({JOB: json_page(200, string_id)})).status is LinkStatus.OPEN

    boolean_id = record()
    boolean_id["id"] = True
    assert check_greenhouse_job("roblox", "8143982", "Roblox", FakeSession({JOB: json_page(200, boolean_id)})).status is LinkStatus.UNKNOWN

    other_company = record(company="Other")
    session = FakeSession({JOB: json_page(200, other_company)})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)
    assert result.status is LinkStatus.UNKNOWN
    assert session.calls == [JOB]


# An error string other than the observed job-not-found body is not closure and does not probe
def test_non_authoritative_404_does_not_probe_or_close():
    session = FakeSession({
        JOB: json_page(404, {"status": 404, "error": "Job board not found"}),
        BOARD: json_page(200, {"name": "Roblox", "content": "<p>x</p>"}),
    })

    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)

    assert result.status is LinkStatus.UNKNOWN
    assert session.calls == [JOB]


# The second not-found reuses a verified board probe
def test_verified_board_is_cached_for_the_run():
    other = f"{API}/v1/boards/roblox/jobs/8143983"
    cache = {}
    routes = {
        JOB: json_page(404, NOT_FOUND),
        other: json_page(404, NOT_FOUND),
        BOARD: json_page(200, {"name": "Roblox", "content": "<p>x</p>"}),
    }
    first = FakeSession(routes)
    second = FakeSession(routes)

    assert check_greenhouse_job("roblox", "8143982", "Roblox", first, board_cache=cache).status is LinkStatus.CLOSED
    assert check_greenhouse_job("roblox", "8143983", "Roblox", second, board_cache=cache).status is LinkStatus.CLOSED
    assert first.calls.count(BOARD) == 1
    assert BOARD not in second.calls


# A careers URL is not a board token, and a non-decimal id makes no request
def test_routing_ignores_the_application_url_and_a_bad_id():
    application = "https://careers.roblox.com/jobs/8143982?gh_jid=8143982"
    sources = [Board("greenhouse", "roblox", "Roblox")]
    session = FakeSession({application: page(200, "<h1>Software Engineer</h1>")})

    page_result = check_configured_link(
        application, session, source="lever", source_job_id="8143982", company_name="Roblox", sources=sources,
    )
    assert page_result.status is LinkStatus.OPEN
    assert session.calls == [application]

    quiet = FakeSession({})
    unknown = check_configured_link(
        application, quiet, source="greenhouse", source_job_id="8143982abc", company_name="Roblox", sources=sources,
    )
    assert unknown.status is LinkStatus.UNKNOWN
    assert quiet.calls == []


# A failed request and a 5xx stay unknown
def test_timeout_and_server_error_are_unknown():
    timed_out = check_greenhouse_job("roblox", "8143982", "Roblox", FakeSession({JOB: requests.Timeout("timed out")}))
    failed = check_greenhouse_job("roblox", "8143982", "Roblox", FakeSession({JOB: json_page(503, {"status": 503})}))

    assert timed_out.status is LinkStatus.UNKNOWN and timed_out.http_status is None
    assert failed.status is LinkStatus.UNKNOWN and failed.http_status == 503
