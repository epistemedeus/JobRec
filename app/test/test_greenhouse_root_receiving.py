"""Root receiving controls for source identity and status deadlines."""
import os
import sys
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC_DIR)
from greenhouse_status import check_configured_link, check_greenhouse_job
from link_check import LinkStatus
from link_fakes import FakeSession, json_page, page

API = "https://boards-api.greenhouse.io"
JOB = API + "/v1/boards/roblox/jobs/8143982"
BOARD = API + "/v1/boards/roblox"
MISSING = {"status": 404, "error": "Job not found"}
RECORD = {"id": 8143982, "title": "Engineer", "company_name": "Roblox",
          "absolute_url": "https://careers.roblox.com/jobs/8143982"}

class Clock:
    t = 0.0
    def __call__(self):
        return self.t

def test_redirect_to_different_board_job_cannot_close_original():
    other = API + "/v1/boards/not-roblox/jobs/8143982"
    session = FakeSession({JOB: page(302, "", headers={"Location": other}),
        other: json_page(404, MISSING), BOARD: json_page(200, {"name": "Roblox"})})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)
    assert result.status is LinkStatus.UNKNOWN
    assert other not in session.calls

def test_redirected_board_probe_cannot_verify_different_endpoint():
    other = API + "/v1/boards/another-board"
    session = FakeSession({JOB: json_page(404, MISSING),
        BOARD: page(302, "", headers={"Location": other}),
        other: json_page(200, {"name": "Roblox"})})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)
    assert result.status is LinkStatus.UNKNOWN
    assert other not in session.calls

def test_body_that_completes_after_deadline_cannot_be_open():
    clock = Clock()
    response = json_page(200, RECORD)
    original = response.iter_content
    def late_body(chunk_size=8192):
        clock.t = 13.0
        yield from original(chunk_size)
    response.iter_content = late_body
    session = FakeSession({JOB: response})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session,
                                 clock=clock, deadline_s=12)
    assert result.status is LinkStatus.UNKNOWN
    assert response.closed

def test_cached_board_does_not_authorize_late_job_closure():
    clock = Clock()
    class LateSession(FakeSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            clock.t = 13.0
            return response
    session = LateSession({JOB: json_page(404, MISSING)})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session,
        board_cache={("roblox", "Roblox"): (True, "verified")},
        clock=clock, deadline_s=12)
    assert result.status is LinkStatus.UNKNOWN

def test_unknown_greenhouse_registry_does_not_use_html404_to_prune():
    url = "https://careers.roblox.com/jobs/8143982"
    session = FakeSession({url: page(404, "Not found")})
    result = check_configured_link(url, session, source="greenhouse",
        source_job_id="8143982", company_name="Roblox", sources=[])
    assert result.status is LinkStatus.UNKNOWN
    assert session.calls == []

def test_malformed_redirect_port_is_unknown_not_an_exception():
    target = "https://boards-api.greenhouse.io:bad/v1/boards/roblox/jobs/8143982"
    response = page(302, "", headers={"Location": target})
    session = FakeSession({JOB: response})
    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)
    assert result.status is LinkStatus.UNKNOWN
    assert response.closed
