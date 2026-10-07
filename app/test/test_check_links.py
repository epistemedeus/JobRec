import os
import random
import sys

import requests

# Lets this test import app/src/check_links.py
SRC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src")
)
sys.path.insert(0, SRC_DIR)

from check_links import check_stored_links  # noqa: E402
from job_listing import JobListing  # noqa: E402
from job_store import upsert_listings  # noqa: E402
from link_fakes import FakeSession, json_page, page  # noqa: E402


def make_listing(job_id: str, title: str = "Software Engineer", company: str = "Acme") -> JobListing:
    # Not greenhouse: an unresolved greenhouse source is unknown and is not fetched.
    return JobListing(title=title, company_name=company, source="lever",
                      application_url=f"https://acme.com/jobs/{job_id}", source_job_id=job_id)


def no_sleep(seconds):
    return None


# Each sampled link is classed by whether it resolves and whether the page is about the stored job
def test_report_counts_resolved_and_matching_links(engine):
    upsert_listings(engine, [
        make_listing("title", title="Data Scientist"),
        make_listing("company", title="Unusual Title", company="Globex"),
        make_listing("generic"),
        make_listing("gone"),
        make_listing("down"),
    ])
    session = FakeSession({
        "https://acme.com/jobs/title": page(200, "<h1>Data Scientist</h1>"),
        "https://acme.com/jobs/company": page(200, "<h1>Work at Globex</h1>"),
        "https://acme.com/jobs/generic": page(200, "<div id='root'></div>"),
        "https://acme.com/jobs/gone": page(404, "Not found"),
        "https://acme.com/jobs/down": requests.ConnectionError("refused"),
    })

    report = check_stored_links(engine, session, sample_size=100, sleep=no_sleep)

    assert report.sampled == 5
    assert report.resolved == 3
    assert report.matched == 2
    assert report.resolved_share == 0.6
    assert report.matched_share == 0.4
    assert sorted(url.rsplit("/", 1)[-1] for url, _ in report.problems) == ["down", "generic", "gone"]


# Pages rendered in the browser name the job only in their metadata; that still counts as being about the job
def test_title_in_page_metadata_counts_as_a_match(engine):
    upsert_listings(engine, [make_listing("meta", title="Renewals Manager"), make_listing("ld", title="Site Reliability Engineer")])
    session = FakeSession({
        "https://acme.com/jobs/meta": page(200, '<head><meta property="og:title" content="Renewals Manager"></head><div id="root"></div>'),
        "https://acme.com/jobs/ld": page(200, '<script type="application/ld+json">{"title": "Site Reliability Engineer"}</script><div id="root"></div>'),
    })

    report = check_stored_links(engine, session, sleep=no_sleep)

    assert report.resolved == 2
    assert report.matched == 2
    assert report.problems == []


# Only the requested number of links is sampled
def test_sample_size_caps_the_links_checked(engine):
    upsert_listings(engine, [make_listing(str(i)) for i in range(10)])
    session = FakeSession({f"https://acme.com/jobs/{i}": page(200, "<h1>Software Engineer</h1>") for i in range(10)})

    report = check_stored_links(engine, session, sample_size=4, sleep=no_sleep, rng=random.Random(1))

    assert report.sampled == 4
    assert len(session.calls) == 4


# --- configured Greenhouse board ---

API = "https://boards-api.greenhouse.io"


class Board:
    def __init__(self, scraper, board_id, company_name):
        self.scraper = scraper
        self.id = board_id
        self.company_name = company_name


def greenhouse_listing(job_id: str, company: str, title: str = "Software Engineer", source: str = "greenhouse") -> JobListing:
    return JobListing(
        title=title,
        company_name=company,
        source=source,
        application_url=f"https://careers.roblox.com/jobs/{job_id}?gh_jid={job_id}",
        source_job_id=job_id,
    )


def job_api(token: str, job_id: str) -> str:
    return f"{API}/v1/boards/{token}/jobs/{job_id}"


def board_api(token: str) -> str:
    return f"{API}/v1/boards/{token}"


# The per-job record confirms the stored title without opening the careers page
def test_exact_greenhouse_record_counts_as_a_live_match(engine):
    title = "[2027] Associate Product Designer, Early Career"
    listing = greenhouse_listing("8143982", "Roblox", title=title)
    job = job_api("roblox", "8143982")
    upsert_listings(engine, [listing])
    session = FakeSession({job: json_page(200, {
        "id": 8143982,
        "title": title,
        "company_name": "Roblox",
        "absolute_url": listing.application_url,
    })})

    report = check_stored_links(
        engine, session, sleep=no_sleep, sources=[Board("greenhouse", "roblox", "Roblox")],
    )

    assert report.sampled == 1
    assert report.resolved == 1 and report.matched == 1
    assert report.problems == []
    assert session.calls == [job]
    assert listing.application_url not in session.calls


# A wrong board, a different id, a timeout, and a success page are not live links
def test_ambiguous_greenhouse_results_are_not_counted_as_live(engine):
    upsert_listings(engine, [
        greenhouse_listing("8143982", "Roblox", title="Designer"),
        greenhouse_listing("42", "Figma", title="Analyst"),
        greenhouse_listing("77", "Cloudflare", title="Operator"),
        greenhouse_listing("88", "Reddit", title="Moderator"),
    ])
    sources = [
        Board("greenhouse", "wrong-board", "Roblox"),
        Board("greenhouse", "figma", "Figma"),
        Board("greenhouse", "cloudflare", "Cloudflare"),
        Board("greenhouse", "reddit", "Reddit"),
    ]
    session = FakeSession({
        job_api("wrong-board", "8143982"): json_page(404, {"status": 404, "error": "Job not found"}),
        board_api("wrong-board"): json_page(404, {"status": 404, "error": "Job board not found"}),
        job_api("figma", "42"): json_page(200, {
            "id": 999,
            "title": "Analyst",
            "company_name": "Figma",
            "absolute_url": "https://boards.greenhouse.io/figma/jobs/999",
        }),
        job_api("cloudflare", "77"): requests.Timeout("timed out"),
        job_api("reddit", "88"): page(200, "<html><body>Success</body></html>", headers={"Content-Type": "text/html"}),
    })

    report = check_stored_links(engine, session, sleep=no_sleep, sources=sources)

    assert report.sampled == 4
    assert report.resolved == 0 and report.matched == 0
    assert {url.rsplit("/", 1)[-1] for url, _ in report.problems} == {
        "42?gh_jid=42", "77?gh_jid=77", "88?gh_jid=88", "8143982?gh_jid=8143982",
    }
    assert job_api("figma", "42") in session.calls
    assert board_api("figma") not in session.calls


# An empty database gives an empty report rather than a division error
def test_empty_database_gives_an_empty_report(engine):
    report = check_stored_links(engine, FakeSession({}), sleep=no_sleep)

    assert report.sampled == 0
    assert report.resolved_share == 0.0 and report.matched_share == 0.0


# A redirect that drops the stored id is undetermined, so it is not a live link
def test_redirect_that_drops_the_job_id_is_not_resolved(engine):
    upsert_listings(engine, [make_listing("123")])
    session = FakeSession({
        "https://acme.com/jobs/123": page(200, "<h1>Careers at Acme</h1>", url="https://acme.com/careers", redirected=True),
    })

    report = check_stored_links(engine, session, sleep=no_sleep, sources=[])

    assert report.sampled == 1
    assert report.resolved == 0 and report.matched == 0
    assert len(report.problems) == 1
