"""The real HTTP transport (package A): never follows a redirect, calls only allow-listed https hosts, and tells
"not sent" from "maybe sent" (DESIGN.md 1-4). A loopback server on port 0 plays the far side."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from insia_agents.publishers import FakeTransport, TransportError, json_response
from insia_agents.publishers.http import UrllibTransport

pytestmark = pytest.mark.usefixtures("no_network")


class _Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str]] = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append(("GET", self.path, self.headers.get("Authorization", "")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        found = self.path == "/json?fields=a%2Cb"
        body = b'{"ok": true}' if found else b"nope"
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append(("POST", self.path, self.rfile.read(length).decode()))
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: D401
        return


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_redirects_are_returned_not_followed(server):
    transport = UrllibTransport([], insecure_loopback_for_tests=True)
    response = transport.request("GET", f"{server}/redirect", headers={"Authorization": "Bearer secret-token"})
    assert response.status == 302 and response.header("Location") == "http://127.0.0.1:1/stolen"
    assert [path for _, path, _ in _Handler.seen] == ["/redirect"]  # the token never went anywhere else
    ok = transport.request("GET", f"{server}/json", params={"fields": "a,b"})
    assert ok.status == 200 and ok.json() == {"ok": True} and ok.header("content-type") == "application/json"
    missing = transport.request("GET", f"{server}/missing")
    assert missing.status == 404 and missing.json() is None  # 4xx is an answer, not an exception
    failed = transport.request("POST", f"{server}/form", form={"a": "1", "b": "한글"})
    assert failed.status == 500 and _Handler.seen[-1][2] == "a=1&b=%ED%95%9C%EA%B8%80"


def test_only_allow_listed_https_hosts(server):
    transport = UrllibTransport(["api.linkedin.com"])
    for url in (f"{server}/json", "http://api.linkedin.com/rest/posts", "https://evil.example.com/x",
                "https://user:pw@api.linkedin.com/x", "https://api.linkedin.com:bad/x"):
        with pytest.raises(TransportError) as caught:
            transport.request("GET", url)
        assert caught.value.sent == "no"
    assert _Handler.seen == []
    transport.allow_host("media.example.com")
    assert "media.example.com" in transport.allowed_hosts


def test_connection_refused_is_not_sent():
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    transport = UrllibTransport([], insecure_loopback_for_tests=True)
    with pytest.raises(TransportError) as caught:
        transport.request("GET", f"http://127.0.0.1:{port}/x", timeout=2)
    assert caught.value.sent == "no"


def test_fake_transport_masks_and_refuses_unscripted_requests():
    fake = FakeTransport()
    fake.add("POST", r"/token", json_response(200, {"access_token": "t"}))
    fake.request("POST", "https://h/token?code=abc&x=1", headers={"Authorization": "Bearer real"},
                 form={"client_secret": "s", "code": "c", "keep": "v"})
    record = fake.requests[0]
    assert record.url == "https://h/token?code=***&x=1" and record.headers["authorization"] == "Bearer ***"
    assert record.form == {"client_secret": "***", "code": "***", "keep": "v"}
    fake.assert_done()
    with pytest.raises(AssertionError):
        fake.request("GET", "https://h/other")
