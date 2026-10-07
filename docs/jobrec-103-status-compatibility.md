# JobRec issue 103: Greenhouse per-job status

Checked 2026-10-07. This is a proposed patch on top of open PR 102. It is not a
pull request, an issue comment, or a change to upstream main.

## What was received

- Issue 103, "[BUG] NF-05: Roblox application links time out for automated checks",
  is open, assigned to pranavputta22, and had no comments at the check.
  https://github.com/Work-At-JobRec/JobRec/issues/103
- PR 102, "[#68] Add listing pruning and a link checker", is open and unmerged.
  Author pranavputta22. Head `ae4c2a0eb64a484269600760942824c029c0db44`, base
  `aa801b9b464f4bfa7c7c80f44005b1582511e1e2`.
  https://github.com/Work-At-JobRec/JobRec/pull/102
- Upstream main does not contain `link_check.py`, `job_pruning.py`, or
  `check_links.py`. Its scrapers live under `app/jobrec/`. No open PR or fork
  branch already asks the per-job endpoint before deleting a listing.
- The fork has no license file. This patch does not add one and does not
  relicense upstream code.

## What this does not reuse as a Roblox fix

Pilot's maintained careers route reads two named boards, Acxiom (Workday) and
LiveRamp (Ashby), and reports explicit coverage for that direct read. It is not
a Greenhouse status API and it does not observe Roblox. This patch does not
call that route.

## Source

Greenhouse Job Board API, unauthenticated GET, read 2026-10-07 from
https://developers.greenhouse.io/job-board.html, which served
https://docs.greenhouse.io/job-board.html. Job retrieval is also described in
https://github.com/grnhse/greenhouse-api-docs/blob/master/source/includes/job-board/_jobs.md.

- `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}`
  returns one published post (`id`, `title`, `company_name`, `absolute_url`,
  and other fields).
- `GET https://boards-api.greenhouse.io/v1/boards/{board_token}` returns
  `{"name", "content"}`.
- GET does not use authentication. The application POST was not called.

The docs page that was read does not define the not-found body. The bodies
below were observed live on 2026-10-07. If those strings change, the checker
stops closing jobs and keeps them.

## Routing

A listing uses the API only when all of these are true:

- `source` is `greenhouse`
- `company_name` equals exactly one Greenhouse `company_name` in the checked-in
  registries (`job_sources.json` and `job_sources_demo.json`), including a
  disabled entry
- that entry's `id` is a single slug
- `source_job_id` is a positive decimal post id

The application URL is not parsed. `careers.roblox.com` does not select the
`roblox` token. Two different ids for one company do not select either.
`--sources` on `check_links.py` and `prune_jobs.py` replaces that default with
one registry file. Lever and Workday stay on the existing page check.

Roblox is configured only in `job_sources_demo.json` (`id` `roblox`,
`company_name` `Roblox`). The default status check loads both registries, so
the issue's demo scrape can be confirmed without a new flag. A `greenhouse`
listing whose company has no slug, two slugs, or a slug that is not safe is
unknown. Its application page is not requested and pruning does not delete it.
Companies whose two files disagree are in that case. Other scrapers are
unchanged.

## Status

| Evidence | Result | Prune |
| --- | --- | --- |
| HTTP 200 JSON, `id` matches, non-empty `title` and `absolute_url`, `company_name` matches the configured company | OPEN | keep |
| HTTP 404 `{"status": 404, "error": "Job not found"}` and the board record's `name` matches that company | CLOSED | may delete, subject to the existing guards |
| Same 404 body but the board probe is missing, named for someone else, malformed, redirected, or failed | UNKNOWN | keep |
| HTTP 200 whose `id` or `company_name` is a different job | UNKNOWN | keep |
| Redirect on the API host to a different board or job id, or a malformed redirect | UNKNOWN, and that URL is not requested | keep |
| HTML page, truncated body, timeout, 408/429/5xx, other statuses, non-decimal id | UNKNOWN | keep |
| Observation finished at or after the listing budget, including a cached board used that late | UNKNOWN | keep |
| Listing reported by the latest scrape | not requested | keep |
| `source` is `greenhouse` and the board is absent, ambiguous, or not a safe slug | UNKNOWN, page not requested | keep |
| Any other scraper, including a page 404 | existing page check | unchanged |

An open record is OPEN even if it contains `application_deadline`. Absence from
a scrape is still not closure. Dry-run remains the default. Unknown results are
not counted as live links by `check_links.py`. The stored post id is passed
into the existing redirect rule, so a redirect that drops the id is unknown
rather than a resolved page.

The same job-not-found JSON is what the API returned for post `1` on board
`roblox` and for a real Roblox post id on board `roblox-not-a-board-103370`.
A 404 alone is the old checker's CLOSED signal. It is not enough here.

## Bounds

Per listing: at most 4 HTTP requests, 2 redirects, and 200000 response bytes.
The listing budget is 12 seconds on the clock passed into the check
(`time.monotonic` in the CLI). A `requests` timeout is only an idle/connect
timeout, at most 8 seconds, and is not that budget.

On the main thread each exchange is armed with `ITIMER_REAL` for the seconds
still left. Incoming bytes do not refresh that timer. A blocked read is
interrupted, and so is a body that keeps arriving. The SIGALRM
handler and any pending interval timer are put back afterward, including when
the deadline fires. The status is decided only if that clock is still inside
the budget after the body has been read. A cached board is consulted only
inside the budget. A late result is unknown and is not stored as a fact about
the board. A completed in-budget board answer is reused for that board and
company for the rest of the run.

Where `ITIMER_REAL` cannot be armed (this thread is not the main thread, or
the platform has no `setitimer`), the check returns unknown and does not
request. It does not claim a 12 second bound it cannot enforce. The page
checker used for other scrapers still has only its `requests` timeout. That
timeout is not a total streaming deadline, and this patch does not describe it
as one.

A redirect is requested only when it is `https://boards-api.greenhouse.io`
(port 443 or omitted, no userinfo) and its path is the same board token and,
for a job request, the same job id. A query may be added. A different path is
not requested. A redirect URL whose port or host cannot be read is unknown,
and the response is closed.

The page checker treats `error=true` as Greenhouse closure only for the host
`greenhouse.io` or a subdomain of it, and only when `error` is a real query
parameter. A redirected 404 or 410 whose final URL no longer contains the
stored job id is unknown. A direct 404 or 410 is still closed.

The existing per-host pause, three-failure skip, and mass-closure guard are
unchanged and still key off the application host.

## Live GETs on 2026-10-07

Prior observations from the original check. They were not repeated for the
redirect, deadline, or registry repair.

No login, application, or paid call.

| Request | Result |
| --- | --- |
| `GET /v1/boards/roblox` | 200, 1562 bytes, 0.038s, `name` `Roblox` |
| `GET /v1/boards/roblox/jobs` | 200; client stopped reading at 20000 bytes. The prefix named post `8143982`. |
| `GET /v1/boards/roblox/jobs/8143982` | 200, 8184 bytes, 0.041s. `id` 8143982, `company_name` `Roblox`, `title` `[2027] Associate Product Designer, Early Career`, `absolute_url` `https://careers.roblox.com/jobs/8143982?gh_jid=8143982` |
| `GET /v1/boards/roblox/jobs/1` | 404 `{"status":404,"error":"Job not found"}` |
| `GET /v1/boards/roblox-not-a-board-103370` | 404 `{"status":404,"error":"Job board not found"}` |
| `GET /v1/boards/roblox-not-a-board-103370/jobs/8143982` | 404 `{"status":404,"error":"Job not found"}` |
| `GET https://careers.roblox.com/jobs/8143982?gh_jid=8143982` | read timed out at 8.061s |

Post 8143982 was open at that request. The tests use this shape with fakes.
They do not call the network, and a later disappearance of that post is not a
failed test.

## Remaining prerequisite

Landing the checker on current upstream main is separate work. Main has moved
scrapers to `app/jobrec/` and does not yet contain PR 102. This patch applies
to PR 102's `app/src/` tree. A company whose Greenhouse `company_name` or board
`name` is not exactly the registry `company_name` stays unknown and is kept.
Closure also depends on the observed `Job not found` string.
