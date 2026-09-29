"""Server surface of API publishing (server_publish.py + server.py wiring), offline.

Most tests run against ``StubPublishService``, an in-memory stand-in that follows the ``PublishService``
contract (DESIGN.md 5-3/6-2, publishers/service.py docstrings), so the server's own rules are tested in
isolation: route auth, the human-request check, the OAuth callback's fixed 303, rate limits, error mapping,
locking against agent jobs and edits, the disabled / not-configured switches. ``test_real_service_*`` at the
end run the same routes once against the real service (package A) in fake-platform mode, and check that the
docs/api.md examples have exactly the real answers' keys.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from insia_agents import server as server_module
from insia_agents import server_publish
from insia_agents.config import Settings
from insia_agents.db import AttemptTakenOverError, InvalidTransitionError, ItemLockedError, NotFoundError, Workspace
from insia_agents.models import ContentItem, Draft, PublishAttempt
from insia_agents.publishers import (
    CALLBACK_RESULTS,
    AlreadyPublishedError,
    AttemptStateError,
    ConfirmationMismatchError,
    ConfirmCodeError,
    ConnectStart,
    HumanConfirmation,
    InvalidOptionsError,
    NotConfiguredError,
    NotConnectedError,
    NotHumanRequestError,
    OAuthExchangeError,
    OAuthStateError,
    PlatformError,
    PreviewExpiredError,
    PreviewResult,
    PublishDisabledError,
    PublisherBusyError,
    RateLimitedError,
    ReconnectRequiredError,
    SettingLockedError,
    UnavailableError,
    ValidationFailedError,
    ValidationIssue,
    parse_preview_options,
)
from insia_agents.publishers.base import payload_hash
from insia_agents.publishers.settings import PublishSettings
from insia_agents.server import COOKIE_NAME, InsiaHandler, LoginLimiter, RequestError, RunRecord, make_server
from insia_agents.server_publish import PUBLISH_ROUTES

pytestmark = pytest.mark.usefixtures("no_network")  # loopback only (tests/conftest.py)

TOKEN = "publish-test-token-0123456789"
CLIENT_SECRET = "LI-CLIENT-SECRET-7f3a9c2e1b"
IG_TOKEN = "IGAAtesttokenvalue0123456789abcdef"
OAUTH_CODE = "AQTcode-9f8e7d6c5b4a"
OAUTH_STATE = "state-0123456789abcdefghijklmnopqrstuvwxyzABCDEF"
COOKIE_VALUE = "a1" * 32
SECRETS = (CLIENT_SECRET, IG_TOKEN, OAUTH_CODE, OAUTH_STATE, COOKIE_VALUE, TOKEN)
MEDIA_TOKEN = "0123456789abcdef0123456789abcdef"
ENV_KEYS = ("INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "INSIA_MAX_LIVE_JOBS",
            "INSIA_MAX_MOCK_JOBS")


# ---------------------------------------------------------------------------
# A contract-following stand-in for PublishService
# ---------------------------------------------------------------------------


class StubPublishService:
    """In-memory ``PublishService`` (same method names, arguments, return types and errors)."""

    def __init__(self, workspace: Workspace, *, env: dict[str, str] | None = None, configured: bool = True,
                 public_hosts: tuple[str, ...] = (), server_port: int = 8765) -> None:
        self.workspace = workspace
        self.settings = PublishSettings.from_env(env or {}, workspace.home, public_hosts=public_hosts,
                                                 server_port=server_port)
        self._configured = configured
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.previews: dict[str, PreviewResult] = {}
        self.attempts: dict[str, PublishAttempt] = {}
        self.app: dict[str, str] = {}
        self.instagram_token = ""
        self.states: set[str] = set()
        self.media_files: dict[str, Path] = {}
        self.redirect_origin = "http://localhost:8765"

    def _call(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for call, kwargs in self.calls if call == name]

    # status
    def configured(self) -> bool:
        return self._configured

    def readiness(self, platform):  # pragma: no cover - not used by the server
        raise NotImplementedError

    def platform_status(self, platform: str, *, check: bool = False) -> dict[str, Any]:
        if platform == "linkedin":
            return {"label": "LinkedIn", "channels": ["linkedin"], "state": "connected", "ready": True, "reason": "",
                    "blockers": [], "app": {"client_id_set": bool(self.app.get("client_id")),
                                            "client_secret_set": bool(self.app.get("client_secret")),
                                            "source": "workspace",
                                            "redirect_uri": self.redirect_origin + "/oauth/linkedin/callback"},
                    "account": {"id_hint": "…taQ", "name": "홍길동", "kind": "LinkedIn 개인 프로필"},
                    "token": {"expires_at": "2026-11-27T00:00:00Z", "days_left": 60, "estimated": False,
                              "scopes": ["openid", "profile", "w_member_social"]},
                    "api_version": "202609", "api_version_sunset": "2027-09-15"}
        return {"label": "인스타그램", "channels": ["instagram"], "state": "unavailable", "ready": False,
                "reason": "공개 주소가 없어요.", "blockers": ["public_url_missing"], "beta": True, "enabled_by": "env",
                "account": {"id_hint": "…000", "username": "@insia.kr", "account_type": "BUSINESS"},
                "token": {"expires_at": "", "days_left": None, "estimated": True, "refreshed_at": "",
                          "auto_refresh": True},
                "api_version": "v25.0", "requirements": {"public_https": False, "render": True}}

    def status(self, *, check: bool = False) -> dict[str, Any]:
        self._call("status", check=check)
        return {"enabled": self.settings.enabled, "configured": self._configured, "fake": self.settings.fake,
                "media": self.settings.media_json(),
                "platforms": {p: self.platform_status(p, check=check) for p in ("linkedin", "instagram")}}

    def health_summary(self) -> dict[str, Any]:
        return {"enabled": self.settings.enabled, "configured": self._configured, "fake": self.settings.fake,
                "linkedin": "connected" if self.settings.enabled else "disabled",
                "instagram": "unavailable" if self.settings.enabled else "disabled"}

    def item_block(self, item: ContentItem, *, agent_job: bool = False) -> dict[str, Any] | None:
        if not (self.settings.enabled and self._configured):
            return None
        platform = {"linkedin": "linkedin", "instagram": "instagram"}.get(item.channel)
        blocked = "agent_job" if agent_job else ("" if item.status in ("approved", "scheduled") else "not_approved")
        return {"platform": platform, "available": platform is not None and not blocked, "state": "connected",
                "reason": "", "blocked_by": blocked, "active_attempt": None, "last_attempt": None}

    def summary_line(self) -> str:
        return "API 게시: LinkedIn 연결됨 · 인스타그램 공개 주소 없음(수동 게시)" if self._configured else ""

    def doctor_report(self) -> list[dict[str, str]]:
        return [{"id": "publish.enabled", "level": "ok", "message": "API 게시 켜짐"}]

    # connections
    def save_linkedin_app(self, *, client_id=None, client_secret=None, redirect_uri=None) -> dict[str, Any]:
        self._call("save_linkedin_app", client_id=client_id, redirect_uri=redirect_uri, secret_given=client_secret is not None)
        if self.settings.linkedin_app_from_env:
            raise SettingLockedError()
        for key, value in (("client_id", client_id), ("client_secret", client_secret), ("redirect_uri", redirect_uri)):
            if value is not None:
                self.app[key] = value
        return self.platform_status("linkedin")

    def linkedin_connect(self, *, request_origin: str = "") -> ConnectStart:
        self._call("linkedin_connect", request_origin=request_origin)
        self.states.add(OAUTH_STATE)
        url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(
            {"response_type": "code", "client_id": "86abc", "state": OAUTH_STATE, "scope": "openid profile w_member_social",
             "redirect_uri": self.redirect_origin + "/oauth/linkedin/callback"})
        if request_origin and request_origin == self.redirect_origin:
            return ConnectStart(mode="redirect", authorize_url=url, redirect_uri=self.redirect_origin + "/oauth/linkedin/callback",
                                cookie_value=COOKIE_VALUE)
        return ConnectStart(mode="paste", authorize_url=url, redirect_uri=self.redirect_origin + "/oauth/linkedin/callback",
                            open_url=self.redirect_origin + "/#/brand/connections")

    def linkedin_callback(self, *, code: str = "", state: str = "", error: str = "", cookie: str = "") -> str:
        self._call("linkedin_callback", code=code, state=state, error=error, cookie=cookie)
        if state not in self.states:
            return "invalid"
        self.states.discard(state)
        if error:
            return "cancelled"
        if cookie != COOKIE_VALUE:
            return "invalid"
        return "exchange_failed" if code == "bad" else "ok"

    def linkedin_complete(self, url: str) -> dict[str, Any]:
        self._call("linkedin_complete", url=url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query or url)
        state = (query.get("state") or [""])[0]
        if state not in self.states:
            raise OAuthStateError()
        self.states.discard(state)
        return self.platform_status("linkedin")

    def save_instagram_token(self, access_token: str) -> dict[str, Any]:
        self._call("save_instagram_token", given=bool(access_token))
        self.instagram_token = access_token
        return self.platform_status("instagram")

    def disconnect(self, platform: str, *, forget_app: bool = False) -> dict[str, Any]:
        self._call("disconnect", platform=platform, forget_app=forget_app)
        return {**self.platform_status(platform), "revoke_hint": "LinkedIn 설정 → 데이터 개인정보 → 권한 있는 서비스에서 앱을 지워요."}

    # previews and sending
    def preview(self, item_id: str, *, platform=None, options=None, via, requested_by, issue_confirm_code=False):
        self._call("preview", item_id=item_id, platform=platform, options=options, via=via, requested_by=requested_by,
                   issue_confirm_code=issue_confirm_code)
        if not self._configured:
            raise NotConfiguredError(platform=platform or "")
        detail = self.workspace.get_item(item_id)
        if detail is None:
            raise NotFoundError(f"콘텐츠 {item_id}를 찾을 수 없어요")
        parsed = parse_preview_options(platform, options)
        payload = {"schema": 1, "platform": platform, "item_id": item_id, "version": detail.item.version,
                   "options": parsed.to_json()}
        preview_id = f"pv_{len(self.previews) + 1:024x}"
        result = PreviewResult(
            preview_id=preview_id, preview_hash=payload_hash(payload), expires_at="2026-09-28T12:30:00Z",
            platform=platform, item={"id": item_id, "version": detail.item.version, "title": detail.item.title,
                                     "channel": detail.item.channel, "approved_version": detail.item.approved_version,
                                     "approval_forced": detail.item.approval_forced, "approved_score": detail.item.approved_score},
            account={"name": "홍길동", "kind": "LinkedIn 개인 프로필", "id_hint": "…taQ"},
            content={"text": "본문", "chars": 2, "limit": 3000, "hashtags": [], "options": parsed.to_json()},
            slides=[], errors=[], warnings=[], notices=[{"code": "manual_done", "message": "이미 직접 올렸다면 여기서 게시하지 마세요."}],
            quota=None, first_comment_link="", request_preview=[],
            confirm_code="K7QX2M" if issue_confirm_code else None)
        result.via = via  # type: ignore[attr-defined]
        self.previews[preview_id] = result
        return result

    def preview_slide(self, preview_id: str, n: int) -> bytes:
        if preview_id not in self.previews or n != 1:
            raise NotFoundError("없는 미리보기 이미지예요.")
        return b"\xff\xd8\xff\xe0JPEGDATA\xff\xd9"

    def send(self, confirmation, *, item_id=None, platform=None, background=True, on_step=None) -> PublishAttempt:
        if not isinstance(confirmation, HumanConfirmation):
            raise TypeError("send needs a HumanConfirmation")
        self._call("send", confirmation=confirmation, item_id=item_id, platform=platform, background=background)
        preview = self.previews.get(confirmation.preview_id)
        if preview is None:
            raise PreviewExpiredError()
        if preview.preview_hash != confirmation.preview_hash or getattr(preview, "via", "") != confirmation.via:
            raise ConfirmationMismatchError()
        if confirmation.via == "cli" and confirmation.confirm_code.strip().upper() != preview.confirm_code:
            raise ConfirmCodeError()
        attempt = PublishAttempt(id=f"pa_{len(self.attempts) + 1:024x}", item_id=preview.item["id"],
                                 version=preview.item["version"], platform=preview.platform, preview_id=preview.preview_id,
                                 payload_hash=preview.preview_hash, status="sending", step="check",
                                 requested_by=confirmation.requested_by, created_at="2026-09-28T12:00:00Z",
                                 updated_at="2026-09-28T12:00:00Z")
        self.attempts[attempt.id] = attempt
        if not background:
            if on_step:
                on_step("write")
            attempt = attempt.model_copy(update={"status": "published", "step": "permalink",
                                                 "permalink": "https://www.linkedin.com/feed/update/urn:li:share:7/"})
            self.attempts[attempt.id] = attempt
        return attempt

    # attempts
    def get_attempt(self, attempt_id: str) -> PublishAttempt:
        if attempt_id not in self.attempts:
            raise NotFoundError(f"게시 기록 {attempt_id}를 찾을 수 없어요")
        return self.attempts[attempt_id]

    def list_attempts(self, *, item_id=None, status=None, limit=50) -> list[PublishAttempt]:
        found = [a for a in self.attempts.values() if (item_id is None or a.item_id == item_id)
                 and (status is None or a.status == status)]
        return list(reversed(found))[:limit]

    def attempt_json(self, attempt: PublishAttempt) -> dict[str, Any]:
        data = attempt.model_dump(mode="json", exclude={"state"})
        data["progress"] = {"done": 1 if attempt.status == "published" else 0, "total": 1}
        return data

    def set_permalink(self, attempt_id: str, url: str, *, by: str) -> PublishAttempt:
        self._call("set_permalink", attempt_id=attempt_id, url=url, by=by)
        attempt = self.get_attempt(attempt_id).model_copy(update={"permalink": url})
        self.attempts[attempt_id] = attempt
        return attempt

    def resolve(self, confirmation, outcome, *, url: str = ""):
        if not isinstance(confirmation, HumanConfirmation):
            raise TypeError("resolve needs a HumanConfirmation")
        self._call("resolve", confirmation=confirmation, outcome=outcome, url=url)
        attempt = self.get_attempt(confirmation.preview_id)
        if attempt.status != "unknown":
            raise AttemptStateError()
        status = "published" if outcome == "published" else "abandoned"
        attempt = attempt.model_copy(update={"status": status, "permalink": url, "resolved_by": confirmation.requested_by})
        self.attempts[attempt.id] = attempt
        item = self.workspace.get_item(attempt.item_id).item if status == "published" else None
        return attempt, item

    def check_attempt(self, attempt_id: str) -> PublishAttempt:
        self._call("check_attempt", attempt_id=attempt_id)
        return self.get_attempt(attempt_id)

    # maintenance
    def refresh_tokens(self) -> dict[str, dict[str, Any]]:
        self._call("refresh_tokens")
        return {"instagram": {"refreshed": False, "expires_at": "", "estimated": False, "message": "갱신할 때가 아니에요."}}

    def recover(self) -> list[str]:
        self._call("recover")
        return []

    def cleanup(self) -> dict[str, int]:
        return {"staging": 0, "public": 0}

    def start_background(self) -> None:
        self._call("start_background")

    def start_media_listener(self, host: str):
        self._call("start_media_listener", host=host)
        return None

    def public_media_file(self, url_path: str) -> Path | None:
        return self.media_files.get(url_path)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._call("shutdown", timeout=timeout)


# ---------------------------------------------------------------------------
# Servers (module-scoped: one plain, one token-mode; each test gets a fresh stub and fresh limiters)
# ---------------------------------------------------------------------------


def _start(tmp_dir: Path, **kwargs: Any):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_KEYS:
            mp.delenv(name, raising=False)
        base = Settings.from_env(env={}, mode="mock", speed=0.0, today="2026-09-28")
        settings = replace(base, out_dir=tmp_dir / "outputs", sample_dir=None, web_dir=None, home=tmp_dir / "ws")
        srv = make_server(settings, host="127.0.0.1", port=0, heartbeat=0.2,
                          publish_service=SimpleNamespace(settings=SimpleNamespace(enabled=False, warnings=())), **kwargs)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return srv


def _stop(srv) -> None:
    srv.publish = None
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def _plain(tmp_path_factory):
    srv = _start(tmp_path_factory.mktemp("plain"))
    yield srv
    _stop(srv)


@pytest.fixture(scope="module")
def _tokened(tmp_path_factory):
    srv = _start(tmp_path_factory.mktemp("token"), token=TOKEN)
    yield srv
    _stop(srv)


def _fresh(srv, **stub_kwargs: Any):
    srv.publish = StubPublishService(srv.manager.workspace, **stub_kwargs)
    for name, count in (("oauth_limiter", 10), ("preview_limiter", 10), ("publish_limiter", 5), ("media_limiter", 30)):
        setattr(srv, name, LoginLimiter(count, 60.0))
    srv.limiter = LoginLimiter()
    with srv.manager._lock:
        srv.manager._active.clear()
    return srv


@pytest.fixture
def srv(_plain):
    return _fresh(_plain)


@pytest.fixture
def tsrv(_tokened):
    return _fresh(_tokened)


def call(srv, method, path, body=None, headers=None, *, human=False, host=None):
    """One request; ``human`` adds what the dashboard's fetch() sends (Origin + Sec-Fetch-Site: same-origin)."""
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, (dict, list)) else body
    hdrs: dict[str, str] = {}
    if method in ("POST", "PUT"):
        hdrs["Content-Type"] = "application/json"
    if human:
        hdrs.update({"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
    if host:
        hdrs["Host"] = host
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw
    return resp, payload, raw


def make_item(ws: Workspace, channel: str = "linkedin", *, approve: bool = True) -> ContentItem:
    item = ws.create_item(channel, "반복 업무를 덜어 낸 방법")
    ws.add_version(item.id, Draft(channel=channel, round=0, title="반복 업무를 덜어 낸 방법", content="본문입니다. " * 30,
                                  hashtags=["#1인창업"]), source="human")
    if approve:
        ws.set_item_status(item.id, "approved", force=True)
    return ws.get_item(item.id).item


def preview_then_publish(srv, item: ContentItem, *, headers=None, human=True, **body: Any):
    resp, preview, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {"options": {}})
    assert resp.status == 200, preview
    payload = {"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "confirm": True, **body}
    return call(srv, "POST", f"/api/items/{item.id}/publish", payload, headers=headers, human=human)


def _no_secrets(raw: bytes | str) -> None:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    for secret in SECRETS:
        assert secret not in text


# ---------------------------------------------------------------------------
# Flow, JSON shapes
# ---------------------------------------------------------------------------


def test_preview_publish_poll_and_attempt_list(srv):
    item = make_item(srv.manager.workspace)
    resp, preview, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {"platform": "linkedin",
                                                                                  "options": {"visibility": "CONNECTIONS"}})
    assert resp.status == 200 and preview["can_publish"] is True and "confirm_code" not in preview
    kwargs = srv.publish.called("preview")[-1]
    assert kwargs["via"] == "dashboard" and kwargs["requested_by"] == "dashboard@127.0.0.1"
    assert kwargs["options"] == {"visibility": "CONNECTIONS"} and kwargs["issue_confirm_code"] is False
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish",
                         {"platform": "linkedin", "preview_id": preview["preview_id"],
                          "preview_hash": preview["preview_hash"], "confirm": True}, human=True)
    assert resp.status == 202, body
    attempt = body["attempt"]
    assert body["poll_url"] == f"/api/publish/attempts/{attempt['id']}" and attempt["status"] == "sending"
    assert "state" not in attempt and attempt["progress"] == {"done": 0, "total": 1}
    confirmation = srv.publish.called("send")[-1]["confirmation"]
    assert isinstance(confirmation, HumanConfirmation) and confirmation.via == "dashboard"
    assert confirmation.requested_by == "dashboard@127.0.0.1" and confirmation.confirm_code == ""
    assert srv.publish.called("send")[-1]["background"] is True
    resp, polled, _ = call(srv, "GET", body["poll_url"])
    assert resp.status == 200 and polled["attempt"]["id"] == attempt["id"]
    resp, listed, _ = call(srv, "GET", f"/api/items/{item.id}/publish")
    assert resp.status == 200 and [a["id"] for a in listed["attempts"]] == [attempt["id"]]
    # the slide image route and 404s
    resp, _, raw = call(srv, "GET", f"/api/publish/previews/{preview['preview_id']}/slides/1.jpg")
    assert resp.status == 200 and resp.getheader("Content-Type") == "image/jpeg" and raw.startswith(b"\xff\xd8")
    assert call(srv, "GET", f"/api/publish/previews/{preview['preview_id']}/slides/2.jpg")[0].status == 404
    assert call(srv, "GET", "/api/publish/attempts/pa_missing")[0].status == 404


def test_publish_needs_confirm_true_and_a_matching_preview(srv):
    item = make_item(srv.manager.workspace)
    resp, preview, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {})
    base = {"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"]}
    for confirm in (None, "true", 1):  # (5 publish requests a minute per client: this test uses all of them)
        resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish", {**base, "confirm": confirm}, human=True)
        assert resp.status == 400 and body["code"] == "invalid_input"
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish",
                         {**base, "preview_hash": "sha256:" + "0" * 64, "confirm": True}, human=True)
    assert resp.status == 409 and body["code"] == "changed"
    assert call(srv, "POST", "/api/items/it_nope/publish", {**base, "confirm": True}, human=True)[0].status == 404
    assert len(srv.publish.called("send")) == 1  # only the mismatched hash reached the service (which refused it)


def test_instagram_preview_requires_an_explicit_ai_label_choice(srv):
    item = make_item(srv.manager.workspace, "instagram")
    for options in (None, {}, {"is_ai_generated": None}, {"is_ai_generated": "false"}, {"is_ai_generated": 0}):
        body = {} if options is None else {"options": options}
        resp, answer, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", body)
        assert resp.status == 400 and answer["code"] == "invalid_options"
        assert answer["error"] == "AI 정보 라벨을 붙일지 골라 주세요 (options.is_ai_generated: true 또는 false)"
    assert srv.publish.called("preview") == []  # refused before anything is rendered
    resp, answer, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {"options": {"is_ai_generated": False}})
    assert resp.status == 200 and answer["content"]["options"] == {"is_ai_generated": False}
    assert srv.publish.called("preview")[-1]["options"] == {"is_ai_generated": False}


def test_channels_without_api_publishing_and_bad_platforms(srv):
    blog = make_item(srv.manager.workspace, "naver_blog")
    resp, body, _ = call(srv, "POST", f"/api/items/{blog.id}/publish/preview", {})
    assert resp.status == 409 and body["code"] == "not_publishable"
    item = make_item(srv.manager.workspace)
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {"platform": "naver_blog"})
    assert resp.status == 400 and body["code"] == "invalid_input"
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {"options": {"visibility": "FRIENDS"}})
    assert resp.status == 400 and body["code"] == "invalid_options"
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {"surprise": 1})
    assert resp.status == 400 and "surprise" in body["error"]


def test_resolve_permalink_check_and_their_errors(srv):
    item = make_item(srv.manager.workspace)
    stub = srv.publish
    unknown = PublishAttempt(id="pa_" + "1" * 24, item_id=item.id, version=1, platform="linkedin", status="unknown")
    done = PublishAttempt(id="pa_" + "2" * 24, item_id=item.id, version=1, platform="linkedin", status="published")
    stub.attempts = {unknown.id: unknown, done.id: done}
    resp, body, _ = call(srv, "POST", f"/api/publish/attempts/{done.id}/resolve", {"outcome": "not_published"}, human=True)
    assert resp.status == 409 and body["code"] == "attempt_state"
    resp, body, _ = call(srv, "POST", f"/api/publish/attempts/{unknown.id}/resolve", {"outcome": "maybe"}, human=True)
    assert resp.status == 400
    resp, body, _ = call(srv, "POST", f"/api/publish/attempts/{unknown.id}/resolve",
                         {"outcome": "not_published", "url": "https://www.linkedin.com/x"}, human=True)
    assert resp.status == 400
    resp, body, _ = call(srv, "POST", f"/api/publish/attempts/{unknown.id}/resolve", {"outcome": "not_published"})
    assert resp.status == 403 and body["code"] == "not_human"  # resolve is a human request too
    url = "https://www.linkedin.com/feed/update/urn:li:share:7/"
    resp, body, _ = call(srv, "POST", f"/api/publish/attempts/{unknown.id}/resolve", {"outcome": "published", "url": url},
                         human=True)
    assert resp.status == 200 and body["attempt"]["status"] == "published" and body["item"]["id"] == item.id
    confirmation = stub.called("resolve")[-1]["confirmation"]
    assert confirmation.via == "dashboard" and confirmation.preview_id == unknown.id and confirmation.preview_hash == ""
    resp, body, _ = call(srv, "PUT", f"/api/publish/attempts/{done.id}/permalink", {"permalink": url})
    assert resp.status == 200 and body["attempt"]["permalink"] == url
    assert stub.called("set_permalink")[-1]["by"] == "dashboard@127.0.0.1"
    assert call(srv, "PUT", f"/api/publish/attempts/{done.id}/permalink", {})[0].status == 400
    resp, body, _ = call(srv, "POST", f"/api/publish/attempts/{done.id}/check")
    assert resp.status == 200 and body["attempt"]["id"] == done.id


# ---------------------------------------------------------------------------
# Human-request check (DESIGN.md 6-3), token mode, route auth (3-3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("headers, status", [
    ({"Sec-Fetch-Site": "same-origin"}, 202),                      # a browser on localhost / https
    ({"Origin": "http://127.0.0.1:{port}"}, 202),                 # LAN http: no Fetch Metadata, same Origin
    ({}, 403),                                                   # neither: a script
    ({"Sec-Fetch-Site": "none", "Origin": "http://127.0.0.1:{port}"}, 403),   # typed/bookmarked, not the dashboard
    ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Sec-Fetch-Site": "cross-site", "Origin": "http://127.0.0.1:{port}"}, 403),
    ({"Origin": "http://localhost:{port}"}, 403),                 # another origin than the Host
    ({"Origin": "null"}, 403),
])
def test_publish_accepts_only_same_origin_browser_requests(srv, headers, status):
    item = make_item(srv.manager.workspace)
    port = srv.server_address[1]
    resp, body, _ = preview_then_publish(srv, item, human=False,
                                         headers={k: v.format(port=port) for k, v in headers.items()})
    assert resp.status == status, body
    if status == 403:
        assert srv.publish.called("send") == []


def test_token_mode_publishes_with_the_cookie_but_never_with_a_bearer_token(tsrv):
    item = make_item(tsrv.manager.workspace)
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    resp, preview, _ = call(tsrv, "POST", f"/api/items/{item.id}/publish/preview", {}, headers=bearer)
    assert resp.status == 200  # scripts may look (preview, status); only publishing needs the person
    payload = {"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "confirm": True}
    resp, body, raw = call(tsrv, "POST", f"/api/items/{item.id}/publish", payload, headers=bearer, human=True)
    assert resp.status == 403 and body["code"] == "not_human" and "Bearer" in body["error"]
    cookie = {"Cookie": f"{COOKIE_NAME}={tsrv.session_value}"}
    both = {**bearer, **cookie}
    assert call(tsrv, "POST", f"/api/items/{item.id}/publish", payload, headers=both, human=True)[0].status == 403
    assert call(tsrv, "POST", f"/api/items/{item.id}/publish", payload, human=True)[0].status == 401
    resp, body, _ = call(tsrv, "POST", f"/api/items/{item.id}/publish", payload, headers=cookie, human=True)
    assert resp.status == 202, body
    assert len(tsrv.publish.called("send")) == 1
    _no_secrets(raw)


def test_route_auth_and_host_checks(tsrv, tmp_path):
    stub = _fresh(tsrv, public_hosts=("media.example.com",), env={"INSIA_MEDIA_BASE_URL": "https://media.example.com"}).publish
    assert stub.settings.media_mode == "main"  # configuration B: the main port serves /pub/m/
    jpeg = tmp_path / "01.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9")
    media_path = f"/pub/m/{MEDIA_TOKEN}/01.jpg"
    stub.media_files[media_path] = jpeg
    # /api/publish needs the token like every /api route
    assert call(tsrv, "GET", "/api/publish")[0].status == 401
    assert call(tsrv, "GET", "/api/publish", headers={"Authorization": f"Bearer {TOKEN}"})[0].status == 200
    # the callback and config-B media need no token
    resp, _, raw = call(tsrv, "GET", "/oauth/linkedin/callback?state=x")
    assert resp.status == 303 and resp.getheader("Location") == "/#/brand/connections/linkedin/invalid"
    resp, _, raw = call(tsrv, "GET", media_path)
    assert resp.status == 200 and raw == jpeg.read_bytes() and resp.getheader("Content-Type") == "image/jpeg"
    assert resp.getheader("X-Robots-Tag") == "noindex, nofollow" and resp.getheader("Cache-Control") == "no-store"
    assert call(tsrv, "HEAD", media_path)[0].status == 200
    # ... but every main-port path checks Host (DNS rebinding)
    evil = "evil.example:80"
    assert call(tsrv, "GET", "/api/publish", host=evil, headers={"Authorization": f"Bearer {TOKEN}"})[0].status == 403
    assert call(tsrv, "GET", "/oauth/linkedin/callback?state=x", host=evil)[0].status == 403
    assert call(tsrv, "GET", media_path, host=evil)[0].status == 403
    # anything but the exact media path shape is a 404 without a body
    for bad in (f"/pub/m/{MEDIA_TOKEN.upper()}/01.jpg", f"/pub/m/{MEDIA_TOKEN}/1.jpg", f"/pub/m/{MEDIA_TOKEN}/01.png",
                f"/pub/m/{MEDIA_TOKEN}/%2e%2e/01.jpg", f"/pub/m/{MEDIA_TOKEN}0/01.jpg", "/pub/x", "/pub/m/"):
        resp, _, raw = call(tsrv, "GET", bad)
        assert (resp.status, raw) == (404, b""), bad
    assert call(tsrv, "POST", media_path, {})[0].status == 405
    assert call(tsrv, "GET", "/oauth/elsewhere")[0].status == 404
    assert call(tsrv, "POST", "/oauth/linkedin/callback", {})[0].status == 405
    for method, path in (("POST", media_path), ("GET", "/oauth/elsewhere"), ("POST", "/oauth/linkedin/callback")):
        assert call(tsrv, method, path, {} if method == "POST" else None, host=evil)[0].status == 403  # Host first


def test_main_port_media_is_404_outside_configuration_b(srv, tmp_path):
    stub = _fresh(srv, env={"INSIA_MEDIA_BASE_URL": "https://media.example.com", "INSIA_MEDIA_PORT": "8766"}).publish
    assert stub.settings.media_mode == "listener"  # configuration A: only the media listener serves images
    jpeg = tmp_path / "01.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xd9")
    stub.media_files[f"/pub/m/{MEDIA_TOKEN}/01.jpg"] = jpeg
    resp, _, raw = call(srv, "GET", f"/pub/m/{MEDIA_TOKEN}/01.jpg")
    assert (resp.status, raw) == (404, b"")


def test_media_404s_are_rate_limited_per_client(srv):
    for _ in range(30):
        assert call(srv, "GET", f"/pub/m/{MEDIA_TOKEN}/01.jpg")[0].status == 404
    resp, _, _ = call(srv, "GET", f"/pub/m/{MEDIA_TOKEN}/02.jpg")
    assert resp.status == 429 and resp.getheader("Retry-After") == "60"


# ---------------------------------------------------------------------------
# LinkedIn connection: connect, callback (fixed 303), complete (paste)
# ---------------------------------------------------------------------------


def test_connect_redirect_mode_sets_the_lax_oauth_cookie_and_paste_mode_does_not(srv):
    port = srv.server_address[1]
    srv.publish.redirect_origin = f"http://127.0.0.1:{port}"
    resp, body, _ = call(srv, "POST", "/api/publish/linkedin/connect", {})
    assert resp.status == 200 and body["mode"] == "redirect" and "cookie_value" not in body
    assert srv.publish.called("linkedin_connect")[-1]["request_origin"] == f"http://127.0.0.1:{port}"
    cookie = resp.getheader("Set-Cookie")
    assert cookie == f"insia_oauth={COOKIE_VALUE}; Path=/oauth/; Max-Age=600; HttpOnly; SameSite=Lax"
    srv.publish.redirect_origin = "http://localhost:8765"  # the dashboard is open on another origin
    resp, body, _ = call(srv, "POST", "/api/publish/linkedin/connect", {})
    assert body["mode"] == "paste" and body["open_url"] and resp.getheader("Set-Cookie") is None


def test_callback_is_always_a_fixed_303_and_never_echoes_the_query(srv):
    srv.publish.states.add(OAUTH_STATE)
    injected = urllib.parse.urlencode({"code": OAUTH_CODE, "state": OAUTH_STATE, "error": "user_cancelled_login",
                                       "error_description": "<script>alert('x')</script> 홍길동"})
    resp, _, raw = call(srv, "GET", f"/oauth/linkedin/callback?{injected}")
    assert resp.status == 303 and resp.getheader("Location") == "/#/brand/connections/linkedin/cancelled"
    headers = "\n".join(f"{k}: {v}" for k, v in resp.getheaders())
    for needle in ("<script>", "alert", "홍길동", OAUTH_CODE, OAUTH_STATE, "error_description"):
        assert needle not in headers and needle.encode() not in raw
    assert resp.getheader("Content-Security-Policy") == "default-src 'none'; frame-ancestors 'none'"
    assert resp.getheader("X-Frame-Options") == "DENY" and resp.getheader("Referrer-Policy") == "no-referrer"
    assert resp.getheader("Cache-Control") == "no-store" and resp.getheader("Content-Type").startswith("text/plain")
    assert resp.getheader("Set-Cookie").startswith("insia_oauth=; Path=/oauth/; Max-Age=0")
    # success needs the state and the cookie of the browser that started it
    srv.publish.states.add(OAUTH_STATE)
    resp, _, _ = call(srv, "GET", f"/oauth/linkedin/callback?code={OAUTH_CODE}&state={OAUTH_STATE}",
                      headers={"Cookie": f"insia_oauth={COOKIE_VALUE}"})
    assert resp.getheader("Location") == "/#/brand/connections/linkedin/ok"
    assert srv.publish.called("linkedin_callback")[-1]["cookie"] == COOKIE_VALUE
    srv.publish.states.add(OAUTH_STATE)
    resp, _, _ = call(srv, "GET", f"/oauth/linkedin/callback?code=bad&state={OAUTH_STATE}",
                      headers={"Cookie": f"insia_oauth={COOKIE_VALUE}"})
    assert resp.getheader("Location") == "/#/brand/connections/linkedin/exchange_failed"


def test_callback_maps_anything_unexpected_to_a_fixed_result(srv, caplog):
    def boom(**kwargs):
        raise RuntimeError(f"leaked {kwargs['code']}")

    srv.publish.linkedin_callback = boom
    resp, _, raw = call(srv, "GET", f"/oauth/linkedin/callback?code={OAUTH_CODE}&state={OAUTH_STATE}")
    assert resp.status == 303 and resp.getheader("Location").rsplit("/", 1)[1] in CALLBACK_RESULTS
    assert OAUTH_CODE not in caplog.text and OAUTH_CODE.encode() not in raw
    srv.publish.linkedin_callback = lambda **kwargs: "<b>odd</b>"
    resp, _, _ = call(srv, "GET", "/oauth/linkedin/callback?state=x")
    assert resp.getheader("Location") == "/#/brand/connections/linkedin/invalid"


def test_bad_callback_states_are_limited_separately_from_logins(tsrv):
    for _ in range(10):
        assert call(tsrv, "GET", "/oauth/linkedin/callback?state=guess")[0].status == 303
    resp, _, raw = call(tsrv, "GET", "/oauth/linkedin/callback?state=guess")
    assert resp.status == 429 and resp.getheader("Retry-After") and resp.getheader("Content-Security-Policy")
    assert tsrv.limiter.retry_after("127.0.0.1") == 0  # the dashboard login is not locked out
    resp, body, _ = call(tsrv, "POST", "/api/login", {"token": TOKEN})
    assert resp.status == 200 and body["ok"] is True


def test_complete_accepts_only_this_servers_state_once(srv):
    srv.publish.states.add(OAUTH_STATE)
    url = f"http://localhost:8765/oauth/linkedin/callback?code={OAUTH_CODE}&state={OAUTH_STATE}"
    # the dashboard is open on 127.0.0.1 while the redirect URI says localhost: the paste path has no origin check
    resp, body, raw = call(srv, "POST", "/api/publish/linkedin/complete", {"url": url}, human=True)
    assert resp.status == 200 and body["state"] == "connected"
    _no_secrets(raw)
    resp, body, raw = call(srv, "POST", "/api/publish/linkedin/complete", {"url": url})
    assert resp.status == 400 and body["code"] == "oauth_state"
    _no_secrets(raw)
    assert call(srv, "POST", "/api/publish/linkedin/complete", {})[0].status == 400
    for _ in range(9):  # 1 bad above + 9 = the 10 allowed bad states
        assert call(srv, "POST", "/api/publish/linkedin/complete", {"url": "code=x&state=nope"})[0].status == 400
    assert call(srv, "POST", "/api/publish/linkedin/complete", {"url": "code=x&state=nope"})[0].status == 429


def test_verbose_access_log_never_holds_oauth_code_or_state(tmp_path, capfd):
    srv = _start(tmp_path, quiet=False)
    try:
        _fresh(srv)
        port = srv.server_address[1]
        call(srv, "GET", f"/oauth/linkedin/callback?code={OAUTH_CODE}&state={OAUTH_STATE}")
        # the absolute form of a request target (proxies) reaches the callback too: its query is cut as well
        resp, _, _ = call(srv, "GET", f"http://127.0.0.1:{port}/oauth/linkedin/callback?code={OAUTH_CODE}&state={OAUTH_STATE}")
        assert resp.status == 303
        # names redact() does not know (an encoded "code", error_description): only the query cut keeps them out
        call(srv, "GET", f"/oauth/linkedin/callback?c%6Fde={LOG_MARKER}&error_description={LOG_MARKER}")
        call(srv, "GET", f"/pub/m/{MEDIA_TOKEN}/01.jpg")
        call(srv, "POST", "/api/publish/linkedin/complete",
             {"url": f"http://localhost:8765/oauth/linkedin/callback?code={OAUTH_CODE}&state={OAUTH_STATE}"})
    finally:
        _stop(srv)
    err = capfd.readouterr().err
    assert "/oauth/linkedin/callback" in err and "/pub/m/012345…" in err
    assert OAUTH_CODE not in err and OAUTH_STATE not in err and MEDIA_TOKEN not in err
    assert LOG_MARKER not in err


LOG_MARKER = "RAW-VALUE-4f9d2c"  # a value redact() cannot recognise as a secret by its shape


@pytest.mark.parametrize(("line", "logged"), [
    (f"GET /oauth/linkedin/callback?c%6Fde={LOG_MARKER}&st%61te={LOG_MARKER} HTTP/1.1",
     "GET /oauth/linkedin/callback HTTP/1.1"),
    (f"GET /oauth/linkedin/callback?error=access_denied&error_description={LOG_MARKER} HTTP/1.1",
     "GET /oauth/linkedin/callback HTTP/1.1"),
    (f"GET http://127.0.0.1:8765/oauth/linkedin/callback?x={LOG_MARKER} HTTP/1.1",  # absolute form (proxies)
     "GET http://127.0.0.1:8765/oauth/linkedin/callback HTTP/1.1"),
    (f"GET //oauth/linkedin/callback?x={LOG_MARKER} HTTP/1.1", "GET //oauth/linkedin/callback HTTP/1.1"),
    (f"GET /oauth/linkedin/callback?{LOG_MARKER}", "GET /oauth/linkedin/callback"),  # no version (bad request)
    (f"GET /oauth/linkedin/callback?x={LOG_MARKER} extra HTTP/1.1", "GET /oauth/linkedin/callback HTTP/1.1"),
    (f"GET /oauth/anything?x={LOG_MARKER} HTTP/1.1", "GET /oauth/anything HTTP/1.1"),  # any /oauth/ path
    # other paths keep their query; only secret-looking values are masked there (the second layer)
    (f"GET /api/items?status=approved&q={LOG_MARKER} HTTP/1.1", f"GET /api/items?status=approved&q={LOG_MARKER} HTTP/1.1"),
    ("GET /api/items?code=abcdef123 HTTP/1.1", "GET /api/items?code=*** HTTP/1.1"),
    ("GET /oauth/linkedin/callback HTTP/1.1", "GET /oauth/linkedin/callback HTTP/1.1"),
])
def test_access_log_drops_the_whole_query_of_oauth_requests(line, logged):
    """DESIGN.md 3-1 / 6-5, the first layer: an ``/oauth/`` request line is logged without any of its query,
    whatever the parameter names are; ``redact()`` (second layer) alone would keep these values."""
    assert server_publish.log_requestline(line) == logged


# ---------------------------------------------------------------------------
# Secrets, switches, locks
# ---------------------------------------------------------------------------


def test_no_secret_in_any_publishing_answer(srv):
    item = make_item(srv.manager.workspace)
    answers = [
        call(srv, "PUT", "/api/publish/linkedin/app", {"client_id": "86abc", "client_secret": CLIENT_SECRET,
                                                         "redirect_uri": "http://localhost:8765/oauth/linkedin/callback"}),
        call(srv, "PUT", "/api/publish/linkedin/app", {"client_secret": 12345}),
        call(srv, "PUT", "/api/publish/linkedin/app", {"client_secret": CLIENT_SECRET * 40}),
        call(srv, "PUT", "/api/publish/linkedin/app", {CLIENT_SECRET: "x"}),
        call(srv, "PUT", "/api/publish/instagram/token", {"access_token": IG_TOKEN}),
        call(srv, "PUT", "/api/publish/instagram/token", {"access_token": IG_TOKEN * 200}),
        call(srv, "GET", "/api/publish"),
        call(srv, "GET", "/api/publish?check=1"),
        call(srv, "POST", "/api/publish/linkedin/connect", {}),
        call(srv, "POST", "/api/publish/linkedin/complete", {"url": f"code={OAUTH_CODE}&state={OAUTH_STATE}"}),
        call(srv, "GET", f"/api/items/{item.id}"),
        call(srv, "GET", "/api/health"),
        call(srv, "DELETE", "/api/publish/linkedin?forget_app=1"),
        preview_then_publish(srv, item),
    ]
    assert srv.publish.app["client_secret"] == CLIENT_SECRET and srv.publish.instagram_token == IG_TOKEN
    assert [a[0].status for a in answers[:6]] == [200, 400, 400, 400, 200, 400]
    for index, (resp, body, raw) in enumerate(answers):
        if index == 8:  # connect: the state belongs in the authorize URL the browser must open, nowhere else
            assert OAUTH_STATE in body.pop("authorize_url")
            raw = json.dumps(body, ensure_ascii=False)
        _no_secrets(raw)
        _no_secrets("\n".join(v for _, v in resp.getheaders()))


# The record-only routes never call a platform: they keep answering while publishing is off, so an attempt left
# 'unknown' before INSIA_PUBLISH=0 can still be answered and its item unlocked (final review F1-1).
RECORD_ONLY_ROUTES = {"publish_item_attempts", "publish_attempt", "publish_permalink", "publish_resolve"}


def test_disabled_publishing_answers_409_everywhere(srv, tmp_path):
    stub = _fresh(srv, env={"INSIA_PUBLISH": "0"}).publish
    item = make_item(srv.manager.workspace)
    sample = {"<id>": item.id, r"(linkedin|instagram)": "linkedin", r"(\d{1,2})\.jpg": "1.jpg"}
    for method, pattern, handler, _auth in PUBLISH_ROUTES:
        path = pattern
        for key, value in sample.items():
            path = path.replace(key, value)
        resp, body, _ = call(srv, method, path, {} if method in ("POST", "PUT") else None, human=True)
        if (method, path) == ("GET", "/api/publish"):  # the status route says "off" so the dashboard draws nothing
            assert resp.status == 200 and body["enabled"] is False and body["platforms"] == {}
            assert body["configured"] is False and body["reason"] == "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0)."
            continue
        if handler in RECORD_ONLY_ROUTES:
            assert (body or {}).get("code") != "disabled", (method, path)
            continue
        assert (resp.status, body["code"]) == (409, "disabled"), (method, path)
        assert body["error"] == "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0)."
    resp, _, _ = call(srv, "GET", f"/oauth/linkedin/callback?state={OAUTH_STATE}")
    assert resp.status == 303 and resp.getheader("Location").endswith("/invalid")
    assert call(srv, "GET", f"/pub/m/{MEDIA_TOKEN}/01.jpg")[0].status == 404
    resp, health, _ = call(srv, "GET", "/api/health")
    assert health["publish"]["enabled"] is False
    assert call(srv, "GET", f"/api/items/{item.id}")[1]["publish"] is None
    assert [name for name, _ in stub.calls] == []


def test_item_detail_publish_block_is_null_until_configured(srv):
    item = make_item(srv.manager.workspace)
    _fresh(srv, configured=False)
    resp, detail, _ = call(srv, "GET", f"/api/items/{item.id}")
    assert resp.status == 200 and detail["publish"] is None and "exports" in detail
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {})
    assert resp.status == 409 and body["code"] == "not_configured"
    resp, health, _ = call(srv, "GET", "/api/health")
    assert health["publish"] == {"enabled": True, "configured": False, "fake": False, "linkedin": "connected",
                                 "instagram": "unavailable"}
    _fresh(srv)
    detail = call(srv, "GET", f"/api/items/{item.id}")[1]
    assert detail["publish"]["platform"] == "linkedin" and detail["publish"]["blocked_by"] == ""
    assert "published_via" in detail["item"] and detail["item"]["published_via"] == ""


def test_server_log_records_never_hold_a_registered_secret(srv, caplog):
    from insia_agents.publishers.redact import register_secret

    register_secret(IG_TOKEN)

    def leaky(*, check=False):
        raise RuntimeError(f"unexpected answer for token {IG_TOKEN}")

    srv.publish.status = leaky
    resp, body, raw = call(srv, "GET", "/api/publish")
    assert resp.status == 500 and IG_TOKEN.encode() not in raw
    assert "RuntimeError" in caplog.text and IG_TOKEN not in caplog.text  # the 500 traceback went through the filter


def test_a_broken_service_never_breaks_health_or_item_detail(srv, caplog):
    item = make_item(srv.manager.workspace)

    def broken(*args, **kwargs):
        raise RuntimeError("publishing is broken")

    srv.publish.health_summary = broken
    srv.publish.item_block = broken
    assert call(srv, "GET", "/api/health")[1]["publish"] is None
    resp, detail, _ = call(srv, "GET", f"/api/items/{item.id}")
    assert resp.status == 200 and detail["publish"] is None


def _fake_job(srv, item: ContentItem, kind: str = "review") -> RunRecord:
    record = RunRecord(run_id=f"job-{kind}-1", kind=kind, bus=SimpleNamespace(), mode="mock", model="m",
                       item_id=item.id if kind in ("review", "revise") else "")
    with srv.manager._lock:
        srv.manager._active[record.run_id] = record
    return record


def test_publish_and_preview_wait_for_agent_work_on_the_item(srv):
    item = make_item(srv.manager.workspace)
    resp, preview, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {})
    job = _fake_job(srv, item)
    payload = {"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "confirm": True}
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish", payload, human=True)
    assert resp.status == 409 and body["code"] == "agent_job" and body["run_id"] == job.run_id
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {})
    assert resp.status == 409 and body["code"] == "agent_job"
    detail = call(srv, "GET", f"/api/items/{item.id}")[1]
    assert detail["publish"]["blocked_by"] == "agent_job"
    assert srv.publish.called("send") == []


def test_edits_jobs_and_resumes_wait_for_a_live_publish_attempt(srv, monkeypatch):
    ws = srv.manager.workspace
    item = make_item(ws)
    attempt = PublishAttempt(id="pa_" + "3" * 24, item_id=item.id, version=1, platform="linkedin", status="sending")
    monkeypatch.setattr(ws, "active_publish_attempt", lambda item_id: attempt if item_id == item.id else None)
    resp, body, _ = call(srv, "PUT", f"/api/items/{item.id}/draft", {"title": "새 제목", "content": "새 본문"})
    assert resp.status == 409 and body["code"] == "item_locked" and body["attempt_id"] == attempt.id
    assert body["attempt_status"] == "sending" and body["status"] == 409
    for route in ("review", "revise"):
        resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/{route}", {"options": {"mode": "mock"}})
        assert resp.status == 409 and body["code"] == "item_locked", route
    unknown = attempt.model_copy(update={"status": "unknown"})
    monkeypatch.setattr(ws, "active_publish_attempt", lambda item_id: unknown)
    resp, body, _ = call(srv, "PUT", f"/api/items/{item.id}/draft", {"title": "새 제목", "content": "새 본문"})
    assert resp.status == 409 and body["error"] == ItemLockedError.UNKNOWN_MESSAGE


def test_a_resume_waits_for_a_live_publish_attempt_on_any_of_its_items(srv, monkeypatch):
    from insia_agents.db import pipeline_item_id
    from insia_agents.models import Brief

    ws = srv.manager.workspace
    locked = pipeline_item_id("20260928-120000-aaaa", "instagram")
    attempt = PublishAttempt(id="pa_" + "4" * 24, item_id=locked, version=2, platform="instagram", status="unknown")
    monkeypatch.setattr(ws, "active_publish_attempt", lambda item_id: attempt if item_id == locked else None)
    record = RunRecord(run_id="20260928-120000-aaaa", kind="pipeline", bus=SimpleNamespace(), mode="mock", model="m",
                       brief=Brief(topic="주제", channels=["linkedin", "instagram"]), options={"resumed": True})
    with srv.manager._lock, pytest.raises(ItemLockedError) as caught:
        srv.manager._check_conflicts(record)
    assert caught.value.attempt_id == attempt.id and caught.value.status == "unknown"
    fresh = RunRecord(run_id="20260928-120000-aaaa", kind="pipeline", bus=SimpleNamespace(), mode="mock", model="m",
                      brief=Brief(topic="주제", channels=["linkedin", "instagram"]), options={})
    with srv.manager._lock:
        srv.manager._check_conflicts(fresh)  # a new run writes new items: not locked


def test_a_publish_being_started_holds_off_jobs(srv):
    item = make_item(srv.manager.workspace)
    manager = srv.manager
    with manager.publishing(item.id, item.run_id):
        resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/review", {"options": {"mode": "mock"}})
        assert resp.status == 409 and body["code"] == "item_locked"
        with pytest.raises(ItemLockedError):
            with manager.human_edit(item.id, item.run_id):
                pass
    assert manager._publishing == {}


# ---------------------------------------------------------------------------
# Error mapping order and rate limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc, status, code", [
    (PublishDisabledError(), 409, "disabled"),
    (NotConfiguredError(platform="linkedin"), 409, "not_configured"),
    (NotConnectedError(platform="instagram"), 409, "not_connected"),
    (ReconnectRequiredError(platform="linkedin"), 409, "reconnect"),
    (UnavailableError(platform="instagram", blockers=["public_url_missing"]), 409, "unavailable"),
    (ValidationFailedError([ValidationIssue("error", "too_long", "너무 길어요")]), 422, "validation"),
    (PreviewExpiredError(), 409, "preview_expired"),
    (ConfirmationMismatchError(), 409, "changed"),
    (AlreadyPublishedError(platform="linkedin", permalink="https://www.linkedin.com/x"), 409, "already_published"),
    (PublisherBusyError(), 409, "busy"),
    (NotHumanRequestError(), 403, "not_human"),
    (InvalidOptionsError(), 400, "invalid_options"),
    (SettingLockedError(), 409, "env_locked"),
    (OAuthStateError(), 400, "oauth_state"),
    (OAuthExchangeError(platform="linkedin"), 502, "exchange_failed"),
    (PlatformError(platform="linkedin", platform_status=401), 502, "platform_error"),
    (ItemLockedError(attempt_id="pa_1", platform="linkedin", status="unknown"), 409, "item_locked"),
    (AttemptTakenOverError(attempt_id="pa_1"), 409, "taken_over"),
    (InvalidTransitionError("x"), 409, None),
    (NotFoundError("x"), 404, None),
])
def test_map_error_order(exc, status, code):
    mapped = InsiaHandler._map_error(exc)
    assert isinstance(mapped, RequestError) and mapped.status == status
    assert mapped.extra.get("code") == code
    assert "status" not in mapped.extra and "error" not in mapped.extra  # never clobber the error envelope
    if isinstance(exc, ItemLockedError):
        assert mapped.extra == {"code": "item_locked", "attempt_id": "pa_1", "platform": "linkedin",
                                "attempt_status": "unknown"}
    if isinstance(exc, UnavailableError):
        assert mapped.extra["blockers"] == ["public_url_missing"]


def test_rate_limited_error_carries_retry_after():
    mapped = InsiaHandler._map_error(RateLimitedError(platform="linkedin", retry_after=42.2))
    assert mapped.status == 429 and mapped.headers == {"Retry-After": "43"} and mapped.extra["retry_after"] == 42.2


def test_preview_and_publish_rate_limits(srv):
    item = make_item(srv.manager.workspace)
    for _ in range(10):
        assert call(srv, "POST", f"/api/items/{item.id}/publish/preview", {})[0].status == 200
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish/preview", {})
    assert resp.status == 429 and resp.getheader("Retry-After") and body["code"] == "too_many"
    for _ in range(5):
        call(srv, "POST", f"/api/items/{item.id}/publish", {"confirm": False}, human=True)
    resp, body, _ = call(srv, "POST", f"/api/items/{item.id}/publish", {"confirm": True}, human=True)
    assert resp.status == 429 and body["code"] == "too_many"


def test_linkedin_app_env_lock_and_disconnect(srv):
    _fresh(srv, env={"INSIA_LINKEDIN_CLIENT_ID": "from-env"})
    resp, body, _ = call(srv, "PUT", "/api/publish/linkedin/app", {"client_id": "x"})
    assert resp.status == 409 and body["code"] == "env_locked"
    resp, body, _ = call(srv, "DELETE", "/api/publish/instagram?forget_app=1")
    assert resp.status == 200 and body["revoke_hint"]
    assert srv.publish.called("disconnect")[-1] == {"platform": "instagram", "forget_app": True}
    assert call(srv, "DELETE", "/api/publish/naver_blog")[0].status == 404


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def test_make_server_starts_and_stops_the_publish_service(tmp_path, monkeypatch):
    created: list[StubPublishService] = []

    def build(settings, workspace, **kwargs):
        stub = StubPublishService(workspace, env={"INSIA_MEDIA_BASE_URL": "https://media.example.com",
                                                  "INSIA_MEDIA_PORT": str(kwargs["media_port"])},
                                  server_port=kwargs["server_port"])
        stub.build_kwargs = kwargs  # type: ignore[attr-defined]
        created.append(stub)
        return stub

    monkeypatch.setattr(server_module, "build_publish_service", build)
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    base = Settings.from_env(env={}, mode="mock", speed=0.0, today="2026-09-28")
    settings = replace(base, out_dir=tmp_path / "o", sample_dir=None, web_dir=None, home=tmp_path / "ws")
    srv = make_server(settings, host="127.0.0.1", port=0, media_port=48766, media_base_url="https://media.example.com")
    stub = created[0]
    assert stub.build_kwargs == {"public_hosts": (), "server_port": srv.server_address[1], "media_port": 48766,
                                 "media_base_url": "https://media.example.com", "trust_proxy": False}
    assert [name for name, _ in stub.calls] == ["recover", "start_background", "start_media_listener"]
    assert stub.called("start_media_listener") == [{"host": "127.0.0.1"}]
    assert server_module.publish_summary(srv).startswith("API 게시:")
    srv.server_close()
    assert stub.called("shutdown") and srv.publish is None


# ---------------------------------------------------------------------------
# The same routes once against the real PublishService (package A) in fake-platform mode (in-memory LinkedIn)
# ---------------------------------------------------------------------------


def _real_service_ready() -> bool:
    import inspect

    from insia_agents.publishers import PublishService

    try:
        return "NotImplementedError" not in inspect.getsource(PublishService.send)
    except (OSError, TypeError):  # pragma: no cover
        return False


real_only = pytest.mark.skipif(not _real_service_ready(), reason="package A's PublishService is not implemented yet")
REAL_ENV = {"INSIA_PUBLISH_FAKE": "1", "INSIA_LINKEDIN_CLIENT_ID": "86abcdefghijkl",
            "INSIA_LINKEDIN_CLIENT_SECRET": CLIENT_SECRET, "INSIA_PUBLISH_INSTAGRAM": "1"}


class StepClock:
    """A clock that only moves when told to (OAuth state expiry); never used where a worker sleeps."""

    def __init__(self) -> None:
        from datetime import datetime, timezone

        self.moment = datetime.now(timezone.utc)

    def now(self):
        return self.moment

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.moment += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def wait(self, event, seconds: float) -> bool:
        return event.is_set()


@pytest.fixture
def real(tmp_path):
    """A fresh server + the real service (fake platform) in its own temporary workspace."""
    from insia_agents.publishers import PublishService

    srv = _start(tmp_path)
    srv.publish = PublishService.from_env(srv.manager.workspace, env=REAL_ENV, server_port=srv.server_address[1])
    try:
        yield srv
    finally:
        service, srv.publish = srv.publish, None
        service.shutdown(timeout=2.0)
        _stop(srv)


def _poll(srv, url: str) -> dict[str, Any]:
    import time

    for _ in range(200):
        body = call(srv, "GET", url)[1]["attempt"]
        if body["status"] != "sending":
            return body
        time.sleep(0.01)
    raise AssertionError("the attempt never finished")


@real_only
def test_real_service_connect_by_paste_preview_publish_and_poll(real):
    ws = real.manager.workspace
    port = real.server_address[1]
    raws: list[bytes] = []
    # the dashboard is open on 127.0.0.1 while the redirect URI says localhost: paste mode with a loopback open_url
    resp, start, raw = call(real, "POST", "/api/publish/linkedin/connect", {})
    raws.append(raw)
    assert resp.status == 200 and start["mode"] == "paste" and resp.getheader("Set-Cookie") is None
    assert start["redirect_uri"] == f"http://localhost:{port}/oauth/linkedin/callback"
    assert start["open_url"] == f"http://localhost:{port}/#/brand/connections"
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorize_url"]).query)["state"][0]
    pasted = {"url": f"http://localhost:{port}/oauth/linkedin/callback?code=AQTfake&state={state}"}
    resp, block, raw = call(real, "POST", "/api/publish/linkedin/complete", pasted)
    raws.append(raw)
    assert resp.status == 200 and block["state"] == "connected" and block["account"]["name"] == "가짜 게시 계정"
    resp, body, raw = call(real, "POST", "/api/publish/linkedin/complete", pasted)  # once only
    assert resp.status == 400 and body["code"] == "oauth_state"
    forged = {"url": "code=AQTfake&state=" + "x" * 43}  # a state this server never issued
    assert call(real, "POST", "/api/publish/linkedin/complete", forged)[1]["code"] == "oauth_state"
    # preview → publish (a person in the dashboard) → poll
    item = make_item(ws)
    assert call(real, "GET", f"/api/items/{item.id}")[1]["publish"]["available"] is True
    resp, preview, raw = call(real, "POST", f"/api/items/{item.id}/publish/preview",
                              {"platform": "linkedin", "options": {"visibility": "CONNECTIONS"}})
    raws.append(raw)
    assert resp.status == 200 and preview["can_publish"] is True and "confirm_code" not in preview
    payload = {"platform": "linkedin", "preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"],
               "confirm": True}
    assert call(real, "POST", f"/api/items/{item.id}/publish", payload)[0].status == 403  # a script: refused
    assert ws.list_publish_attempts(item_id=item.id) == []
    resp, started, raw = call(real, "POST", f"/api/items/{item.id}/publish", payload, human=True)
    raws.append(raw)
    assert resp.status == 202 and started["attempt"]["requested_by"] == "dashboard@127.0.0.1"
    done = _poll(real, started["poll_url"])
    assert done["status"] == "published" and done["external_id"].startswith("urn:li:share:")
    detail = call(real, "GET", f"/api/items/{item.id}")[1]
    assert detail["item"]["status"] == "published" and detail["item"]["published_via"] == "fake"
    assert detail["publish"]["blocked_by"] == "published" and detail["publish"]["last_attempt"]["id"] == done["id"]
    resp, again, _ = call(real, "POST", f"/api/items/{item.id}/publish", payload, human=True)
    assert resp.status == 409 and again["code"] == "preview_expired"
    for raw in raws + [call(real, "GET", "/api/publish")[2], call(real, "GET", "/api/health")[2]]:
        assert CLIENT_SECRET.encode() not in raw


@real_only
def test_real_service_redirect_mode_callback_and_state_expiry(real):
    port = real.server_address[1]
    host = f"localhost:{port}"  # the dashboard opened on the redirect URI's own origin
    resp, start, _ = call(real, "POST", "/api/publish/linkedin/connect", {}, host=host,
                          headers={"Origin": f"http://{host}"})
    assert resp.status == 200 and start["mode"] == "redirect"
    cookie = resp.getheader("Set-Cookie")
    assert cookie.startswith("insia_oauth=") and "SameSite=Lax" in cookie and "Path=/oauth/" in cookie
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorize_url"]).query)["state"][0]
    target = f"/oauth/linkedin/callback?code=AQTfake&state={state}"
    resp, _, _ = call(real, "GET", target, host=host)  # the right state from another browser (no cookie)
    assert resp.status == 303 and resp.getheader("Location") == "/#/brand/connections/linkedin/invalid"
    resp, _, raw = call(real, "GET", target, host=host, headers={"Cookie": f"insia_oauth={value}"})
    assert resp.getheader("Location") == "/#/brand/connections/linkedin/ok" and state.encode() not in raw
    resp, _, _ = call(real, "GET", target, host=host, headers={"Cookie": f"insia_oauth={value}"})
    assert resp.getheader("Location") == "/#/brand/connections/linkedin/invalid"  # used
    # a pasted state is good for 10 minutes only
    from insia_agents.publishers import PublishService

    clock = StepClock()
    real.publish.shutdown(timeout=1.0)
    real.publish = PublishService.from_env(real.manager.workspace, env=REAL_ENV, server_port=port, clock=clock)
    start = call(real, "POST", "/api/publish/linkedin/connect", {})[1]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorize_url"]).query)["state"][0]
    clock.advance(601)
    resp, body, _ = call(real, "POST", "/api/publish/linkedin/complete", {"url": f"code=AQTfake&state={state}"})
    assert resp.status == 400 and body["code"] == "oauth_state"


@real_only
def test_real_service_switches_and_the_ai_label(real, tmp_path):
    from insia_agents.publishers import PublishService

    ws = real.manager.workspace
    port = real.server_address[1]
    item = make_item(ws, "instagram")
    resp, body, _ = call(real, "POST", f"/api/items/{item.id}/publish/preview", {"options": {}})
    assert resp.status == 400 and body["code"] == "invalid_options"  # before anything is rendered
    # nothing configured: the library shows nothing, health says so
    real.publish.shutdown(timeout=1.0)
    real.publish = PublishService.from_env(ws, env={}, server_port=port)
    assert call(real, "GET", f"/api/items/{item.id}")[1]["publish"] is None
    assert call(real, "GET", "/api/health")[1]["publish"] == {"enabled": True, "configured": False, "fake": False,
                                                              "linkedin": "not_configured", "instagram": "disabled"}
    # switched off
    real.publish = PublishService.from_env(ws, env={"INSIA_PUBLISH": "0"}, server_port=port)
    assert call(real, "GET", "/api/health")[1]["publish"]["enabled"] is False
    assert call(real, "GET", "/api/publish")[1]["enabled"] is False
    assert call(real, "POST", f"/api/items/{item.id}/publish/preview", {"options": {"is_ai_generated": False}})[1][
        "code"] == "disabled"


def _unknown_attempt(ws: Workspace, item: ContentItem, platform: str = "linkedin") -> PublishAttempt:
    """An attempt whose platform answer was lost after the write step (the item is locked until a person answers)."""
    from insia_agents.models import PublishConnection

    ws.save_publish_connection(PublishConnection(platform=platform, account_id="sub-1", status="connected"))
    digest = "sha256:" + "b" * 64
    preview = ws.create_publish_preview(item.id, item.version, platform, "sub-1", {"schema": 1}, digest,
                                        created_via="dashboard", requested_by="dashboard@test")
    attempt, owner = ws.begin_publish_attempt(preview.id, digest, via="dashboard", requested_by="dashboard@test")
    ws.claim_publish_write(attempt.id, owner)
    return ws.finish_publish_failure(attempt.id, owner, status="unknown", error_code="server_error", error="LinkedIn 500")


@real_only
def test_real_service_switched_off_still_answers_an_unknown_attempt(real):
    """INSIA_PUBLISH=0 after an 'unknown' attempt: the item stays locked, but its lock shows, its history loads and a
    person can still say what happened (final review F1-1); only what would call a platform stays refused."""
    from insia_agents.publishers import PublishService

    ws = real.manager.workspace
    port = real.server_address[1]
    item = make_item(ws)
    attempt = _unknown_attempt(ws, item)
    real.publish.shutdown(timeout=1.0)
    real.publish = PublishService.from_env(ws, env={"INSIA_PUBLISH": "0"}, server_port=port)
    block = call(real, "GET", f"/api/items/{item.id}")[1]["publish"]
    assert block["platform"] == "linkedin" and block["state"] == "disabled" and block["available"] is False
    assert block["active_attempt"]["id"] == attempt.id and block["active_attempt"]["status"] == "unknown"
    assert block["reason"] == ItemLockedError.UNKNOWN_MESSAGE
    resp, body, _ = call(real, "PUT", f"/api/items/{item.id}/draft", {"title": "t", "content": "고친 본문"})
    assert resp.status == 409 and body["code"] == "item_locked"
    resp, body, _ = call(real, "GET", f"/api/items/{item.id}/publish")
    assert resp.status == 200 and [a["id"] for a in body["attempts"]] == [attempt.id]
    assert call(real, "GET", f"/api/publish/attempts/{attempt.id}")[1]["attempt"]["status"] == "unknown"
    resp, body, _ = call(real, "POST", f"/api/publish/attempts/{attempt.id}/check", {})
    assert resp.status == 409 and body["code"] == "disabled"  # a platform call: still off
    resp, body, _ = call(real, "POST", f"/api/publish/attempts/{attempt.id}/resolve", {"outcome": "not_published"})
    assert resp.status == 403  # still a person's answer only
    resp, body, _ = call(real, "POST", f"/api/publish/attempts/{attempt.id}/resolve", {"outcome": "not_published"},
                         human=True)
    assert resp.status == 200 and body["attempt"]["status"] == "abandoned"
    assert call(real, "GET", f"/api/items/{item.id}")[1]["publish"] is None  # nothing live: nothing drawn
    resp, _, _ = call(real, "POST", f"/api/items/{item.id}/status", {"status": "archived"})
    assert resp.status == 200


DOCS = Path(__file__).resolve().parents[1] / "docs" / "api.md"


def _doc_json(heading: str, *, nth: int = 0) -> Any:
    """The ``nth`` ```json block after ``heading`` in docs/api.md (a bare ``"key": {…}`` block gets braces)."""
    text = DOCS.read_text(encoding="utf-8")
    start = text.index(heading)
    blocks = text[start:].split("```json\n")[1:]
    block = blocks[nth].split("```", 1)[0].strip()
    return json.loads(block if block.startswith("{") else "{" + block + "}")


@real_only
def test_docs_api_examples_have_the_real_answers_shape(real):
    """docs/api.md shows answers copied from the real service: the documented keys are exactly the real ones."""
    ws = real.manager.workspace
    start = call(real, "POST", "/api/publish/linkedin/connect", {})[1]
    assert set(start) == set(_doc_json("### `POST /api/publish/linkedin/connect`"))
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorize_url"]).query)["state"][0]
    call(real, "POST", "/api/publish/linkedin/complete", {"url": f"code=AQTfake&state={state}"})
    token_block = call(real, "PUT", "/api/publish/instagram/token", {"access_token": IG_TOKEN})[1]
    status = call(real, "GET", "/api/publish")[1]
    doc = _doc_json("### `GET /api/publish`")
    assert set(status) == set(doc) and set(status["media"]) == set(doc["media"])
    for platform in ("linkedin", "instagram"):
        real_block, doc_block = status["platforms"][platform], doc["platforms"][platform]
        assert set(real_block) == set(doc_block), platform
        for key in ("app", "account", "token", "requirements"):
            if key in doc_block:
                assert set(real_block[key]) == set(doc_block[key]), (platform, key)
    assert set(token_block) == set(_doc_json("### `PUT /api/publish/instagram/token`"))
    item = make_item(ws)
    block = call(real, "GET", f"/api/items/{item.id}")[1]["publish"]
    assert set(block) == set(_doc_json("### 콘텐츠 상세의 `publish` 블록")["publish"])
    preview = call(real, "POST", f"/api/items/{item.id}/publish/preview", {"options": {"visibility": "PUBLIC"}})[1]
    doc_preview = _doc_json("### `POST /api/items/<id>/publish/preview`")
    assert set(preview) == set(doc_preview) and set(preview["content"]) == set(doc_preview["content"])
    assert set(preview["item"]) == set(doc_preview["item"]) and set(preview["account"]) == set(doc_preview["account"])
    payload = {"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "confirm": True}
    started = call(real, "POST", f"/api/items/{item.id}/publish", payload, human=True)[1]
    doc_started = _doc_json("### `POST /api/items/<id>/publish`")
    assert set(started) == set(doc_started) and set(started["attempt"]) == set(doc_started["attempt"])
    done = _poll(real, started["poll_url"])
    assert done["status"] == "published"
    listed = call(real, "GET", f"/api/items/{item.id}/publish")[1]
    assert set(listed) == {"item_id", "attempts"} and set(listed["attempts"][0]) == set(doc_started["attempt"])
