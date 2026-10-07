"""Streaming deadline, redirect identity, and registry controls for the repair.

The nine receiving tests stay in test_greenhouse_root_receiving.py. These add
the wall-clock bound, the contexts where that bound cannot be armed, and the
caller and prune consumer.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC_DIR)

from greenhouse_status import _bounded_exchange, check_configured_link, check_greenhouse_job  # noqa: E402
from job_listing import JobListing  # noqa: E402
from job_pruning import prune_closed_listings  # noqa: E402
from job_store import list_listings, upsert_listings  # noqa: E402
from link_check import LinkStatus, check_link  # noqa: E402
from link_fakes import FakeSession, json_page, page  # noqa: E402

API = "https://boards-api.greenhouse.io"
JOB = f"{API}/v1/boards/roblox/jobs/8143982"
BOARD = f"{API}/v1/boards/roblox"
NOT_FOUND = {"status": 404, "error": "Job not found"}
RECORD = {
    "id": 8143982,
    "title": "Engineer",
    "company_name": "Roblox",
    "absolute_url": "https://careers.roblox.com/jobs/8143982",
}

# Parent kills the child here. The idle timeout inside the child is much longer,
# and the server withholds the body longer than this too.
_EXTERNAL_BOUND_S = 3.0
_DRIP_DEADLINE_S = 0.4
_WITHHOLD_S = 8.0


class _Board:
    def __init__(self, scraper, board_id, company_name):
        self.scraper = scraper
        self.id = board_id
        self.company_name = company_name


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _WithholdHandler(BaseHTTPRequestHandler):
    """Send headers, then hold the body until asked to stop."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "10000000")
            self.end_headers()
            self.wfile.flush()
            if self.server.stop_drip.wait(_WITHHOLD_S):
                return
            self.wfile.write(b"x")
            self.wfile.flush()
        except Exception:
            return

    def log_message(self, format, *args):
        return


class _WithholdServer(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _WithholdHandler)
        self.stop_drip = threading.Event()


_CHILD = r"""
import signal
import sys
import time

import requests

from greenhouse_status import _bounded_exchange

url = sys.argv[1]
seconds = float(sys.argv[2])
fired = []

def remember(signum, frame):
    fired.append(signum)

previous = signal.signal(signal.SIGALRM, remember)
signal.setitimer(signal.ITIMER_REAL, 30.0)
try:
    deadline = time.monotonic() + seconds
    started = time.monotonic()
    fetched = _bounded_exchange(
        requests.Session(),
        url,
        timeout=30,
        max_bytes=200000,
        seconds=seconds,
        clock=time.monotonic,
        deadline=deadline,
    )
    elapsed = time.monotonic() - started
    remaining = signal.getitimer(signal.ITIMER_REAL)[0]
    restored = signal.getsignal(signal.SIGALRM) is remember
    print(f"kind={fetched.kind}")
    print(f"elapsed={elapsed:.4f}")
    print(f"handler_restored={restored}")
    print(f"timer_remaining={remaining:.3f}")
    print(f"unrelated_fired={len(fired)}")
finally:
    signal.setitimer(signal.ITIMER_REAL, 0.0)
    signal.signal(signal.SIGALRM, previous)
"""


def _child_env():
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    parts = [SRC_DIR, "/tmp/jobrec-py"]
    if previous:
        parts.append(previous)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


# A stalled body must stop inside a bound the parent enforces, not the idle timeout.
def test_withheld_body_stops_inside_an_external_bound():
    server = _WithholdServer()
    thread = threading.Thread(target=server.serve_forever, name="amend-drip-server")
    thread.start()
    proc = None
    before = {item.ident for item in threading.enumerate()}
    try:
        port = server.server_address[1]
        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD, f"http://127.0.0.1:{port}/slow", str(_DRIP_DEADLINE_S)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )
        started = time.monotonic()
        try:
            stdout, stderr = proc.communicate(timeout=_EXTERNAL_BOUND_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"read was still running after {_EXTERNAL_BOUND_S}s\n{stdout}\n{stderr}"
            )
        parent_elapsed = time.monotonic() - started
        assert proc.returncode == 0, stderr
        fields = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
        assert fields["kind"] == "late"
        assert float(fields["elapsed"]) < 1.5
        assert parent_elapsed < _EXTERNAL_BOUND_S
        assert fields["handler_restored"] == "True"
        assert float(fields["timer_remaining"]) > 20
        assert fields["unrelated_fired"] == "0"
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        server.stop_drip.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert proc is None or proc.poll() is not None
        leftovers = {item.ident for item in threading.enumerate()} - before
        assert leftovers == set()


# The fast path also puts back a timer it did not own.
def test_fast_exchange_restores_an_unrelated_timer():
    fired = []

    def remember(signum, frame):
        fired.append(signum)

    previous = signal.signal(signal.SIGALRM, remember)
    signal.setitimer(signal.ITIMER_REAL, 30.0)
    try:
        result = check_greenhouse_job("roblox", "8143982", "Roblox", FakeSession({JOB: json_page(200, RECORD)}))
        remaining = signal.getitimer(signal.ITIMER_REAL)[0]
        assert result.status is LinkStatus.OPEN
        assert signal.getsignal(signal.SIGALRM) is remember
        assert remaining > 20
        assert fired == []
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


# A worker thread cannot arm the interval timer, so it must not request.
def test_deadline_off_the_main_thread_is_unknown_and_makes_no_request():
    session = FakeSession({JOB: json_page(200, RECORD)})
    box = {}

    def run():
        box["result"] = check_greenhouse_job("roblox", "8143982", "Roblox", session)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert box["result"].status is LinkStatus.UNKNOWN
    assert "cannot be enforced" in box["result"].reason
    assert session.calls == []


# A late board body must not become a cached authorization for the next listing.
def test_late_board_probe_is_not_cached():
    clock = _Clock()

    class LateBoard(FakeSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if url == BOARD:
                clock.t = 13.0
            return response

    cache = {}
    session = LateBoard({JOB: json_page(404, NOT_FOUND), BOARD: json_page(200, {"name": "Roblox"})})
    result = check_greenhouse_job(
        "roblox", "8143982", "Roblox", session, board_cache=cache, clock=clock, deadline_s=12,
    )

    assert result.status is LinkStatus.UNKNOWN
    assert ("roblox", "Roblox") not in cache


# The same board with a different post id is a different resource.
def test_redirect_to_a_different_job_id_is_not_requested():
    other = f"{API}/v1/boards/roblox/jobs/1"
    session = FakeSession({
        JOB: page(302, "", headers={"Location": other}),
        other: json_page(404, NOT_FOUND),
        BOARD: json_page(200, {"name": "Roblox"}),
    })

    result = check_greenhouse_job("roblox", "8143982", "Roblox", session)

    assert result.status is LinkStatus.UNKNOWN
    assert other not in session.calls
    assert BOARD not in session.calls


# Two ids for one company are not a board, and the page is not a substitute.
def test_ambiguous_greenhouse_identity_makes_no_request():
    url = "https://careers.roblox.com/jobs/8143982"
    session = FakeSession({url: page(404, "Not found")})
    sources = [
        _Board("greenhouse", "roblox", "Roblox"),
        _Board("greenhouse", "other", "Roblox"),
    ]

    result = check_configured_link(
        url, session, source="greenhouse", source_job_id="8143982", company_name="Roblox", sources=sources,
    )

    assert result.status is LinkStatus.UNKNOWN
    assert session.calls == []


# Lever still treats its own HTTP 404 as closure.
def test_other_scraper_html_404_still_closes():
    url = "https://jobs.lever.co/acme/123"
    session = FakeSession({url: page(404, "Not found")})

    result = check_configured_link(
        url, session, source="lever", source_job_id="123", company_name="Acme", sources=[],
    )

    assert result.status is LinkStatus.CLOSED
    assert session.calls == [url]


# The prune consumer keeps a greenhouse listing it cannot tie to one board.
def test_unresolved_greenhouse_listing_is_not_pruned(engine):
    application = "https://careers.roblox.com/jobs/8143982"
    listing = JobListing(
        title="Engineer",
        company_name="Roblox",
        source="greenhouse",
        application_url=application,
        source_job_id="8143982",
    )
    seen = datetime(2026, 10, 1, tzinfo=timezone.utc)
    upsert_listings(engine, [listing], now=seen)
    session = FakeSession({application: page(404, "Not found")})

    result = prune_closed_listings(
        engine, session, dry_run=False, now=seen + timedelta(days=8), sleep=lambda _seconds: None, sources=[],
    )

    assert result.removed == 0 and result.closed == 0
    assert session.calls == []
    assert [stored.source_job_id for stored in list_listings(engine)] == ["8143982"]


# The real error parameter still closes when the job id has been dropped.
def test_official_error_parameter_still_closes_without_the_job_id():
    original = "https://boards.greenhouse.io/roblox/jobs/8143982"
    landed = "https://boards.greenhouse.io/roblox?error=true&ok=1"
    session = FakeSession({original: page(200, "Openings", url=landed, redirected=True)})

    result = check_link(original, session, job_id="8143982")

    assert result.status is LinkStatus.CLOSED


# A redirected 404 that still names the job is the job's own gone page.
def test_redirected_404_that_keeps_the_job_id_stays_closed():
    original = "https://careers.example.test/jobs/8143982"
    landed = "https://jobs.example.test/roles/8143982"
    session = FakeSession({original: page(404, "Missing", url=landed, redirected=True)})

    result = check_link(original, session, job_id="8143982")

    assert result.status is LinkStatus.CLOSED
