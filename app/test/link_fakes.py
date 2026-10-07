"""Stand-ins for requests responses and sessions, shared by the link-check and pruning tests."""

import json


class FakeResponse:
    """A page as requests would return it: status, body, and the URL the request ended on."""

    def __init__(self, status=200, body="", url=None, redirected=False, encoding="utf-8", headers=None):
        self.status_code = status
        self._body = body.encode(encoding)
        self.url = url
        self.history = [object()] if redirected else []
        self.encoding = encoding
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Maps each URL to a FakeResponse, or to an exception that the request raises."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        outcome = self.routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome.url is None:
            outcome.url = url
        return outcome


def page(status=200, body="<h1>Software Engineer</h1><p>Apply now</p>", **kwargs):
    return FakeResponse(status, body, **kwargs)


def json_page(status, payload, **kwargs):
    """A JSON API body. Extra headers are merged over the JSON content type."""
    headers = {"Content-Type": "application/json"}
    headers.update(kwargs.pop("headers", None) or {})
    return FakeResponse(status, json.dumps(payload), headers=headers, **kwargs)
