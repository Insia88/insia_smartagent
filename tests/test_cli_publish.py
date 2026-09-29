"""``insia publish …``, ``insia backup``, the doctor's publishing lines and ``serve --media-*`` (offline).

The publish commands run against ``CliStub``, an in-memory stand-in that follows the ``PublishService`` contract
(publishers/service.py) and records every method the command calls; it never talks to a platform, so there
"nothing was sent" means ``send`` was never called. The same commands also run once against the real
``PublishService`` in fake-platform mode, where the recorded transport requests show what reached the "platform"
(the preview's read-only account check, DESIGN.md 4-0, and never a post without the right code). What is tested
here is the command line's own job: the terminal-only ``send`` with its random confirm code (no ``--yes``), the
required ``--ai-label`` for Instagram (DESIGN.md 14.2), Ctrl+C handling (1-7), the off switch, secrets never
printed, the safe backup, and the server's redirect URI derived from the same environment.
"""

from __future__ import annotations

import builtins
import getpass
import json
import os
import sqlite3
import urllib.parse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from insia_agents import cli
from insia_agents import server as server_module
from insia_agents.cli import main
from insia_agents.config import Settings
from insia_agents.db import Workspace
from insia_agents.models import Draft, PublishAttempt
from insia_agents.publishers import (
    ConfirmationMismatchError,
    ConfirmCodeError,
    ConnectStart,
    HumanConfirmation,
    OAuthStateError,
    PreviewResult,
    parse_preview_options,
)
from insia_agents.publishers.base import confirm_code_hash, new_confirm_code, payload_hash, step_label
from insia_agents.publishers.service import ATTEMPT_JSON_STATE
from insia_agents.publishers.settings import PUBLISH_ENV_VARS, PublishSettings

pytestmark = pytest.mark.usefixtures("no_network")  # loopback only (tests/conftest.py)

CLIENT_SECRET = "LI-CLIENT-SECRET-cli-5d1e9a"
IG_TOKEN = "IGAAclitokenvalue9876543210fedcba"
STATE = "cli-state-0123456789abcdefghijklmnopqrstuvwxyz"
SECRETS = (CLIENT_SECRET, IG_TOKEN)


# ---------------------------------------------------------------------------
# A contract-following stand-in for PublishService
# ---------------------------------------------------------------------------


class CliStub:
    """In-memory ``PublishService`` for the CLI (same method names, arguments, return types and errors)."""

    def __init__(self, workspace: Workspace, env: dict[str, str] | None = None) -> None:
        self.workspace = workspace
        self.settings = PublishSettings.from_env(env or {}, workspace.home)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.previews: dict[str, PreviewResult] = {}
        self.code_hashes: dict[str, str] = {}
        self.attempts: dict[str, PublishAttempt] = {}
        self.secret = ""
        self.instagram_token = ""
        self.interrupt: tuple[str, str] | None = None  # (step, status) at which send raises KeyboardInterrupt
        self.late_interrupts: set[str] = set()  # "lookup" / "shutdown": a later Ctrl+C lands there once
        self.sent = False  # send() got as far as creating its attempt
        self.redirect_uri = "http://localhost:8765/oauth/linkedin/callback"

    def _call(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for call, kwargs in self.calls if call == name]

    # status
    def platform_status(self, platform: str, *, check: bool = False) -> dict[str, Any]:
        if platform == "linkedin":
            return {"label": "LinkedIn", "channels": ["linkedin"], "state": "connected", "ready": True, "reason": "",
                    "blockers": [], "app": {"client_id_set": True, "client_secret_set": bool(self.secret),
                                            "source": "workspace", "redirect_uri": self.redirect_uri},
                    "account": {"id_hint": "…taQ", "name": "홍길동", "kind": "LinkedIn 개인 프로필"},
                    "token": {"expires_at": "2026-11-27T00:00:00Z", "days_left": 60, "estimated": False,
                              "scopes": ["openid", "profile", "w_member_social"]},
                    "api_version": "202609", "api_version_sunset": "2027-09-15"}
        return {"label": "인스타그램", "channels": ["instagram"], "state": "connected", "ready": True, "reason": "",
                "blockers": [], "beta": True, "enabled_by": "env",
                "account": {"id_hint": "…000", "username": "@insia.kr", "account_type": "BUSINESS"},
                "token": {"expires_at": "2026-11-20T00:00:00Z", "days_left": 53, "estimated": True,
                          "refreshed_at": "", "auto_refresh": True},
                "api_version": "v25.0", "requirements": {"public_https": True, "render": True}}

    def status(self, *, check: bool = False) -> dict[str, Any]:
        self._call("status", check=check)
        return {"enabled": True, "configured": True, "fake": False, "media": self.settings.media_json(),
                "platforms": {p: self.platform_status(p) for p in ("linkedin", "instagram")}}

    def doctor_report(self) -> list[dict[str, str]]:
        self._call("doctor_report")
        return [{"id": "publish.enabled", "level": "ok", "message": "API 게시 기능이 켜져 있어요"},
                {"id": "linkedin.app", "level": "ok", "message": "LinkedIn 앱 정보: 설정됨 (워크스페이스)"},
                {"id": "instagram.beta", "level": "ok", "message": "인스타그램 API 게시(시험 중)가 켜져 있어요."},
                {"id": "instagram.media", "level": "warn", "message": "이미지 공개 주소를 쓸 수 없어요: 미디어 공개 주소가 없어요"},
                {"id": "instagram.render", "level": "ok", "message": "카드 이미지 렌더링을 쓸 수 있어요."},
                {"id": "credentials.permissions", "level": "ok", "message": "credentials 폴더·파일 권한이 맞아요 (0700/0600)."}]

    # connections
    def save_linkedin_app(self, *, client_id=None, client_secret=None, redirect_uri=None) -> dict[str, Any]:
        self._call("save_linkedin_app", client_id=client_id, redirect_uri=redirect_uri,
                   secret_given=client_secret is not None)
        if client_secret is not None:
            self.secret = client_secret
        return self.platform_status("linkedin")

    def linkedin_connect(self, *, request_origin: str = "") -> ConnectStart:
        self._call("linkedin_connect", request_origin=request_origin)
        url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(
            {"response_type": "code", "client_id": "86abc", "redirect_uri": self.redirect_uri, "state": STATE,
             "scope": "openid profile w_member_social"})
        return ConnectStart(mode="paste", authorize_url=url, redirect_uri=self.redirect_uri)

    def linkedin_complete(self, url: str) -> dict[str, Any]:
        self._call("linkedin_complete", url=url)
        query = urllib.parse.urlsplit(url).query if url.startswith("http") else url
        values = urllib.parse.parse_qs(query)
        if (values.get("state") or [""])[0] != STATE:
            raise OAuthStateError()
        return self.platform_status("linkedin")

    def save_instagram_token(self, access_token: str) -> dict[str, Any]:
        self._call("save_instagram_token", given=bool(access_token))
        self.instagram_token = access_token
        return self.platform_status("instagram")

    def disconnect(self, platform: str, *, forget_app: bool = False) -> dict[str, Any]:
        self._call("disconnect", platform=platform, forget_app=forget_app)
        return {**self.platform_status(platform), "revoke_hint": "LinkedIn 설정 → 데이터 개인정보 → 권한 있는 서비스"}

    # previews and sending
    def preview(self, item_id: str, *, platform=None, options=None, via, requested_by, issue_confirm_code=False):
        self._call("preview", item_id=item_id, platform=platform, options=options, via=via,
                   requested_by=requested_by, issue_confirm_code=issue_confirm_code)
        detail = self.workspace.get_item(item_id)
        parsed = parse_preview_options(platform, options)
        payload = {"schema": 1, "platform": platform, "item_id": item_id, "version": detail.item.version,
                   "options": parsed.to_json()}
        preview_id = f"pv_{len(self.previews) + 1:024x}"
        code = new_confirm_code() if issue_confirm_code else None
        if code:
            self.code_hashes[preview_id] = confirm_code_hash(code)
        text = "혼자 창업하면 마케팅은 늘 '이번 주만 넘기고'가 돼요.\n\n#1인창업 #AI마케팅"
        result = PreviewResult(
            preview_id=preview_id, preview_hash=payload_hash(payload), expires_at="2026-09-28T12:30:00Z",
            platform=platform, item={"id": item_id, "version": detail.item.version, "title": detail.item.title,
                                     "channel": detail.item.channel, "approved_version": detail.item.approved_version,
                                     "approval_forced": detail.item.approval_forced, "approved_score": None},
            account={"name": "홍길동", "kind": "LinkedIn 개인 프로필", "id_hint": "…taQ"},
            content={"text": text, "chars": len(text), "limit": 3000, "hashtags": ["#1인창업", "#AI마케팅"],
                     "options": parsed.to_json()},
            slides=[], errors=[], warnings=[],
            notices=[{"code": "manual_done", "message": "이미 LinkedIn에 직접 올렸다면 여기서 게시하지 마세요."}],
            quota=None, first_comment_link="", request_preview=[], confirm_code=code)
        self.previews[preview_id] = result
        return result

    def send(self, confirmation, *, item_id=None, platform=None, background=True, on_step=None) -> PublishAttempt:
        if not isinstance(confirmation, HumanConfirmation):
            raise TypeError("send needs a HumanConfirmation")
        self._call("send", confirmation=confirmation, item_id=item_id, platform=platform, background=background)
        preview = self.previews[confirmation.preview_id]
        if preview.preview_hash != confirmation.preview_hash:
            raise ConfirmationMismatchError()
        if confirmation.via == "cli" and confirm_code_hash(confirmation.confirm_code) != self.code_hashes.get(
                confirmation.preview_id):
            raise ConfirmCodeError()
        attempt = PublishAttempt(id=f"pa_{len(self.attempts) + 1:024x}", item_id=item_id, version=1,
                                 platform=platform, preview_id=preview.preview_id, payload_hash=preview.preview_hash,
                                 status="sending", step="check", requested_by=confirmation.requested_by,
                                 created_at="2026-09-28T12:00:00Z", updated_at="2026-09-28T12:00:00Z")
        self.attempts[attempt.id] = attempt
        if self.interrupt:
            step, status = self.interrupt  # the service closes the attempt, then re-raises (contract, DESIGN.md 1-7)
            update: dict[str, Any] = {"step": step, "status": status}
            if status in ("failed", "unknown"):
                update["error_code"] = "interrupted"
            elif status == "published":  # Ctrl+C after the platform confirmed (while the worker cleaned up)
                update["permalink"] = "https://www.linkedin.com/feed/update/urn:li:share:7243/"
            self.attempts[attempt.id] = attempt.model_copy(update=update)
            self.sent = True
            raise KeyboardInterrupt
        if on_step:
            on_step("write")
        done = attempt.model_copy(update={"status": "published", "step": "permalink",
                                          "permalink": "https://www.linkedin.com/feed/update/urn:li:share:7243/"})
        self.attempts[attempt.id] = done
        self.sent = True
        return done

    # attempts
    def get_attempt(self, attempt_id: str) -> PublishAttempt:
        return self.attempts[attempt_id]

    def list_attempts(self, *, item_id=None, status=None, limit=50) -> list[PublishAttempt]:
        if self.sent and "lookup" in self.late_interrupts:
            self.late_interrupts.discard("lookup")  # a second Ctrl+C while the command reads the attempt back
            raise KeyboardInterrupt
        found = [a for a in self.attempts.values() if item_id is None or a.item_id == item_id]
        return list(reversed(found))[:limit]

    def attempt_json(self, attempt: PublishAttempt) -> dict[str, Any]:
        data = attempt.model_dump(mode="json", exclude={"state"})
        data["progress"] = {"done": 1 if attempt.status == "published" else 0, "total": 1}
        data["step_label"] = step_label(attempt.step)  # the service's contract: the step in words, safe state notes
        data.update({key: value for key, value in (attempt.state or {}).items() if key in ATTEMPT_JSON_STATE})
        return data

    def resolve(self, confirmation, outcome, *, url: str = ""):
        if not isinstance(confirmation, HumanConfirmation):
            raise TypeError("resolve needs a HumanConfirmation")
        self._call("resolve", confirmation=confirmation, outcome=outcome, url=url)
        attempt = self.attempts[confirmation.preview_id]
        attempt = attempt.model_copy(update={"status": "published" if outcome == "published" else "abandoned",
                                             "permalink": url, "resolved_by": confirmation.requested_by})
        self.attempts[attempt.id] = attempt
        return attempt, None

    def check_attempt(self, attempt_id: str) -> PublishAttempt:
        self._call("check_attempt", attempt_id=attempt_id)
        return self.attempts[attempt_id]

    def set_permalink(self, attempt_id: str, url: str, *, by: str) -> PublishAttempt:
        self._call("set_permalink", attempt_id=attempt_id, url=url, by=by)
        attempt = self.attempts[attempt_id].model_copy(update={"permalink": url})
        self.attempts[attempt_id] = attempt
        return attempt

    # maintenance
    def refresh_tokens(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        self._call("refresh_tokens", **kwargs)
        return {"instagram": {"refreshed": True, "expires_at": "2026-11-27T00:00:00Z", "estimated": False,
                              "message": "60일 연장했어요."}}

    def recover(self) -> list[str]:
        self._call("recover")
        return []

    def shutdown(self, timeout: float = 5.0) -> None:
        self._call("shutdown")
        if "shutdown" in self.late_interrupts:
            self.late_interrupts.discard("shutdown")  # Ctrl+C while the command closes the service
            raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    monkeypatch.setenv("INSIA_HOME", str(home))
    monkeypatch.setenv("INSIA_TODAY", "2026-09-28")
    for name in (*PUBLISH_ENV_VARS, "INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "INSIA_PORT",
                 "INSIA_DEBUG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def stubs(monkeypatch):
    """Every publish command gets a fresh CliStub; the list keeps them for assertions."""
    made: list[CliStub] = []

    def factory(settings, ws, **kwargs):
        stub = CliStub(ws, env=dict(os.environ))
        made.append(stub)
        return stub

    monkeypatch.setattr(cli, "_publish_service", factory)
    return made


@pytest.fixture
def tty(monkeypatch):
    """A person at a terminal; ``answers`` feeds input() (a callable answer sees the prompt)."""
    answers: list[Any] = []
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        answer = answers.pop(0)
        return answer(prompt) if callable(answer) else answer

    monkeypatch.setattr(cli, "_publish_is_tty", lambda: True)
    monkeypatch.setattr(builtins, "input", fake_input)
    return SimpleNamespace(answers=answers, prompts=prompts)


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def make_item(home: Path, channel: str = "linkedin") -> str:
    ws = Workspace(home)
    try:
        item = ws.create_item(channel, "반복 업무를 덜어 낸 방법")
        ws.add_version(item.id, Draft(channel=channel, round=0, title="반복 업무를 덜어 낸 방법", content="본문입니다. " * 30,
                                      hashtags=["#1인창업"]), source="human")
        ws.set_item_status(item.id, "approved", force=True)
        return item.id
    finally:
        ws.close()


def code_from(prompt: str) -> str:
    return prompt.split("확인 코드 ", 1)[1].split("를", 1)[0]


# ---------------------------------------------------------------------------
# send: terminal only, random code, no --yes
# ---------------------------------------------------------------------------


def test_send_refuses_without_a_terminal_and_touches_nothing(env, stubs, capsys):
    item_id = make_item(env)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 2 and "터미널에서 직접 확인할 때만" in err and "cron" in err
    assert stubs == []  # not even a service (so no preview, no confirm code, no request)


def test_send_with_the_wrong_code_exits_2_and_sends_nothing(env, stubs, tty, capsys):
    item_id = make_item(env)
    tty.answers.append("WRONG9")
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 2 and "확인 코드가 맞지 않아요" in err and "아무것도 올리지 않았어요" in err
    stub = stubs[0]
    assert stub.called("preview")[0]["issue_confirm_code"] is True and stub.called("preview")[0]["via"] == "cli"
    assert stub.called("send") == []
    assert "LinkedIn에 게시하기 전에 확인해 주세요" in out and "공개 범위: 전체 공개" in out


def test_send_with_the_shown_code_publishes_exactly_that_preview(env, stubs, tty, capsys):
    item_id = make_item(env)
    tty.answers.append(lambda prompt: f"  {code_from(prompt).lower()} ")  # typed loosely: trimmed, any case
    code, out, err = run(capsys, "publish", "send", item_id, "--visibility", "CONNECTIONS")
    assert code == 0, err
    stub = stubs[0]
    [sent] = stub.called("send")
    confirmation = sent["confirmation"]
    assert isinstance(confirmation, HumanConfirmation) and confirmation.via == "cli"
    assert confirmation.requested_by.startswith("cli:") and sent["background"] is False
    [preview] = stub.previews.values()
    assert (confirmation.preview_id, confirmation.preview_hash) == (preview.preview_id, preview.preview_hash)
    assert stub.called("preview")[0]["options"] == {"visibility": "CONNECTIONS"}
    assert "게시했어요: https://www.linkedin.com/feed/update/urn:li:share:7243/" in out
    assert "게시 요청 보내는 중" in out  # the step printer
    # the code was shown once, on this terminal only: the prompt, never the preview text
    shown = code_from(tty.prompts[0])
    assert out.count(shown) == 0 and len(shown) == 6


def test_every_send_gets_a_fresh_code_and_enter_cancels(env, stubs, tty, capsys):
    item_id = make_item(env)
    tty.answers.extend(["", ""])
    assert run(capsys, "publish", "send", item_id)[0] == 1
    assert run(capsys, "publish", "send", item_id)[0] == 1
    first, second = (code_from(p) for p in tty.prompts)
    assert first != second and len(first) == len(second) == 6  # a new random code for every preview
    assert all(stub.called("send") == [] for stub in stubs)


def test_send_ctrl_c_says_what_happened(env, stubs, tty, capsys, monkeypatch):
    item_id = make_item(env)
    real = CliStub

    def interrupting(step, status):
        def factory(settings, ws, **kwargs):
            stub = real(ws)
            stub.interrupt = (step, status)
            stubs.append(stub)
            return stub
        monkeypatch.setattr(cli, "_publish_service", factory)

    interrupting("check", "failed")  # before the write step: the service closed it as failed
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 1 and "게시 요청을 보내기 전이라 아무것도 올리지 않았어요" in err
    interrupting("write", "unknown")  # after the write step: it may be up
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 1 and "올라갔을 수도 있어요" in err and "insia publish resolve pa_" in err
    # Ctrl+C at the code prompt (before any attempt): nothing was sent, exit 1 (not 130)
    def ctrl_c(prompt: str) -> str:
        raise KeyboardInterrupt

    tty.answers.append(ctrl_c)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 1 and "아무것도 올리지 않았어요" in err and stubs[-1].called("send") == []


NOTHING_SENT = "아무것도 올리지 않았어요"
PERMALINK = "https://www.linkedin.com/feed/update/urn:li:share:7243/"


@pytest.mark.parametrize(("interrupt", "late", "want_code", "want_out", "want_err"), ids=[
    "closing-after-published", "closing-after-unknown", "second-ctrl-c-reading-the-attempt", "after-the-platform-confirmed",
], argvalues=[
    # the send finished and its outcome was printed, then Ctrl+C while the service closes
    (None, {"shutdown"}, 0, ["게시했어요: " + PERMALINK], ["게시 결과는 위에 적은 그대로예요"]),
    # Ctrl+C during the send (after the write step), then a second one while the service closes
    (("write", "unknown"), {"shutdown"}, 1, [], ["올라갔을 수도 있어요", "insia publish resolve pa_", "위에 적은 그대로"]),
    # Ctrl+C during the send, then a second one while the command reads the attempt back: do not guess
    (("write", "unknown"), {"lookup"}, 1, [], ["올라갔는지 확인하지 못했어요", "insia publish attempts"]),
    # Ctrl+C after the platform confirmed (the worker was cleaning up): the post is up, exit 0
    (("permalink", "published"), set(), 0, ["중단했지만 이미 게시됐어요: " + PERMALINK], []),
])
def test_send_ctrl_c_after_the_send_started_never_says_nothing_was_sent(env, tty, capsys, monkeypatch, interrupt,
                                                                       late, want_code, want_out, want_err):
    item_id = make_item(env)
    made: list[CliStub] = []

    def factory(settings, ws, **kwargs):
        stub = CliStub(ws)
        stub.interrupt, stub.late_interrupts = interrupt, set(late)
        made.append(stub)
        return stub

    monkeypatch.setattr(cli, "_publish_service", factory)
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == want_code, (out, err)
    assert NOTHING_SENT not in err and NOTHING_SENT not in out
    assert all(text in out for text in want_out), out
    assert all(text in err for text in want_err), err
    assert made[0].late_interrupts == set()  # the planned late Ctrl+C really happened


def test_send_ctrl_c_while_the_outcome_prints_keeps_the_outcome(env, stubs, tty, capsys, monkeypatch):
    item_id = make_item(env)

    def printing(attempt, label, *, first_comment_link=""):
        print("게시했", end="")  # half a line, then Ctrl+C
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_publish_print_outcome", printing)
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 0 and "중단했지만 이미 게시됐어요: " + PERMALINK in out
    assert NOTHING_SENT not in err + out


def test_there_is_no_yes_flag(env, stubs, capsys):
    code, out, err = run(capsys, "publish", "send", "it_anything", "--yes")
    assert code == 2 and "--yes" in err and stubs == []
    help_text = cli.build_parser()._subparsers._group_actions[0].choices["publish"].format_help()
    assert "--yes" not in help_text


# ---------------------------------------------------------------------------
# preview, Instagram's AI label, other commands
# ---------------------------------------------------------------------------


def test_preview_json_never_carries_a_confirm_code(env, stubs, capsys):
    item_id = make_item(env)
    code, out, err = run(capsys, "publish", "preview", item_id, "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["can_publish"] is True and data["preview_id"].startswith("pv_")
    assert "confirm_code" not in out and "code_hash" not in out
    assert stubs[0].called("preview")[0]["issue_confirm_code"] is False and stubs[0].code_hashes == {}
    code, out, _ = run(capsys, "publish", "preview", item_id)
    assert code == 0 and "게시하려면 터미널에서: insia publish send" in out


def test_instagram_needs_an_explicit_ai_label_choice(env, stubs, tty, capsys):
    item_id = make_item(env, "instagram")
    for argv in (("publish", "preview", item_id), ("publish", "send", item_id)):
        code, out, err = run(capsys, *argv)
        assert code == 2 and "--ai-label yes" in err and "--ai-label no" in err
    assert all(stub.called("preview") == [] for stub in stubs)
    code, out, err = run(capsys, "publish", "preview", item_id, "--ai-label", "maybe")
    assert code == 2  # argparse: yes | no only
    code, out, err = run(capsys, "publish", "preview", item_id, "--ai-label", "no", "--json")
    assert code == 0 and stubs[-1].called("preview")[0]["options"] == {"is_ai_generated": False}
    code, out, err = run(capsys, "publish", "preview", item_id, "--ai-label", "yes", "--visibility", "PUBLIC")
    assert code == 2 and "LinkedIn에서만" in err
    linkedin = make_item(env)
    code, out, err = run(capsys, "publish", "preview", linkedin, "--ai-label", "yes")
    assert code == 2 and "인스타그램에서만" in err


def test_connect_instagram_token_stdin_never_echoes_the_token(env, stubs, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(IG_TOKEN + "\n"))
    code, out, err = run(capsys, "publish", "connect", "instagram", "--token-stdin")
    assert code == 0, err
    assert stubs[0].instagram_token == IG_TOKEN and "@insia.kr" in out
    assert IG_TOKEN not in out + err
    code, out, err = run(capsys, "publish", "connect", "instagram", "--paste")
    assert code == 2 and "LinkedIn 연결에서만" in err


def test_setup_and_connect_linkedin_by_pasting(env, stubs, tty, capsys, monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": CLIENT_SECRET)
    tty.answers.extend(["86abc", ""])  # Client ID, keep the suggested redirect URI
    code, out, err = run(capsys, "publish", "setup", "linkedin")
    assert code == 0, err
    # an empty redirect URI keeps what applies now (stored, or derived from the server's address)
    assert stubs[0].called("save_linkedin_app")[0] == {"client_id": "86abc", "redirect_uri": None, "secret_given": True}
    assert CLIENT_SECRET not in out + err and "Authorized redirect URLs" in out
    pasted = f"http://localhost:8765/oauth/linkedin/callback?code=AQTcode&state={STATE}"
    tty.answers.append(pasted)
    code, out, err = run(capsys, "publish", "connect", "linkedin", "--paste")
    assert code == 0 and "LinkedIn 계정을 연결했어요: 홍길동" in out
    assert stubs[-1].called("linkedin_complete") == [{"url": pasted}]
    tty.answers.append("http://localhost:8765/oauth/linkedin/callback?code=x&state=forged")
    code, out, err = run(capsys, "publish", "connect", "linkedin", "--paste")
    assert code == 1 and "연결 요청이 만료됐거나 올바르지 않아요" in err


def test_connect_linkedin_one_time_listener_takes_only_its_own_state(env, stubs, capsys, monkeypatch):
    import http.client
    import socket
    import threading

    with socket.socket() as probe:  # a free loopback port for the redirect URI
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    answers: list[int] = []
    visits: list[threading.Thread] = []

    def browser(url: str) -> bool:
        def visit() -> None:
            for state in ("someone-else", STATE):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", f"/oauth/linkedin/callback?code=AQTcode&state={state}",
                             headers={"Host": f"localhost:{port}"})
                answers.append(conn.getresponse().status)
                conn.close()
        visits.append(threading.Thread(target=visit, daemon=True))
        visits[-1].start()
        return True

    monkeypatch.setattr("webbrowser.open", browser)
    original = CliStub.__init__

    def init(self, workspace, env=None):
        original(self, workspace, env)
        self.redirect_uri = f"http://localhost:{port}/oauth/linkedin/callback"

    monkeypatch.setattr(CliStub, "__init__", init)
    code, out, err = run(capsys, "publish", "connect", "linkedin")
    visits[0].join(5)
    assert code == 0, err
    assert answers == [400, 200]  # a request with another state neither ends the wait nor connects
    [complete] = stubs[0].called("linkedin_complete")
    assert urllib.parse.parse_qs(complete["url"]) == {"code": ["AQTcode"], "state": [STATE]}


def test_status_attempts_resolve_refresh_disconnect(env, stubs, tty, capsys, monkeypatch):
    monkeypatch.setenv("INSIA_LINKEDIN_CLIENT_SECRET", CLIENT_SECRET)  # present in the environment, never printed
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 0 and json.loads(out)["enabled"] is True
    assert CLIENT_SECRET not in out + err
    code, out, err = run(capsys, "publish", "status")
    assert code == 0 and "LinkedIn: 연결됨" in out and "계정: 홍길동" in out and CLIENT_SECRET not in out
    # attempts + resolve (terminal + y/N) + --check (no terminal needed)
    item_id = make_item(env)
    attempt = PublishAttempt(id="pa_" + "9" * 24, item_id=item_id, version=1, platform="linkedin", status="unknown",
                             created_at="2026-09-28T12:00:00Z", updated_at="2026-09-28T12:00:00Z")
    original = CliStub.__init__

    def init(self, workspace, env=None):
        original(self, workspace, env)
        self.attempts = {attempt.id: attempt}

    monkeypatch.setattr(CliStub, "__init__", init)
    code, out, err = run(capsys, "publish", "attempts", "--json")
    assert code == 0 and [a["id"] for a in json.loads(out)["attempts"]] == [attempt.id]
    monkeypatch.setattr(cli, "_publish_is_tty", lambda: False)
    code, out, err = run(capsys, "publish", "resolve", "pa_9999", "--published")
    assert code == 2 and "터미널에서 직접" in err
    code, out, err = run(capsys, "publish", "resolve", "pa_9999", "--check")
    assert code == 0 and stubs[-1].called("check_attempt") == [{"attempt_id": attempt.id}]
    monkeypatch.setattr(cli, "_publish_is_tty", lambda: True)
    tty.answers.append("n")
    code, out, err = run(capsys, "publish", "resolve", "pa_9999", "--not-published")
    assert code == 1 and stubs[-1].called("resolve") == [] and "두 번 올라갈 수 있어요" in out
    tty.answers.append("y")
    url = "https://www.linkedin.com/feed/update/urn:li:share:7/"
    code, out, err = run(capsys, "publish", "resolve", "pa_9999", "--published", "--url", url)
    assert code == 0 and "올라갔어요" in out
    [resolved] = stubs[-1].called("resolve")
    assert resolved["outcome"] == "published" and resolved["url"] == url
    assert resolved["confirmation"].via == "cli" and resolved["confirmation"].preview_id == attempt.id
    assert run(capsys, "publish", "resolve", "pa_9999", "--not-published", "--url", url)[0] == 2
    # refresh: token refresh only — no preview, no send, no request
    code, out, err = run(capsys, "publish", "refresh", "--json")
    assert code == 0 and json.loads(out)["instagram"]["refreshed"] is True
    assert stubs[-1].names() == ["recover", "refresh_tokens", "shutdown"]
    assert stubs[-1].called("refresh_tokens") == [{"stored_when_off": True}]  # a cron without the IG switch still refreshes
    code, out, err = run(capsys, "publish", "disconnect", "linkedin", "--forget-app")
    assert code == 0 and stubs[-1].called("disconnect") == [{"platform": "linkedin", "forget_app": True}]
    assert "권한 있는 서비스" in out


def test_every_publish_command_says_off_when_disabled(env, stubs, tty, capsys, monkeypatch):
    monkeypatch.setenv("INSIA_PUBLISH", "0")
    item_id = make_item(env)
    for argv in (("status", "--json"), ("refresh",), ("preview", item_id), ("send", item_id),
                 ("resolve", "pa_1", "--check")):
        code, out, err = run(capsys, "publish", *argv)
        assert code == 1 and "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0)." in err, argv
        assert out == ""
    assert all(stub.names() == ["shutdown"] for stub in stubs)  # not even recovery ran


def _unknown_attempt(home: Path, item_id: str, platform: str = "linkedin") -> str:
    """A real 'unknown' attempt (the platform's answer was lost after the write step): it locks the item."""
    from insia_agents.models import PublishConnection

    ws = Workspace(home)
    try:
        ws.save_publish_connection(PublishConnection(platform=platform, account_id="sub-1", status="connected"))
        digest = "sha256:" + "c" * 64
        version = ws.get_item(item_id).item.version
        preview = ws.create_publish_preview(item_id, version, platform, "sub-1", {"schema": 1}, digest,
                                            created_via="cli", requested_by="cli:test")
        attempt, owner = ws.begin_publish_attempt(preview.id, digest, via="cli", requested_by="cli:test")
        ws.claim_publish_write(attempt.id, owner)
        return ws.finish_publish_failure(attempt.id, owner, status="unknown", error="LinkedIn 500").id
    finally:
        ws.close()


def test_record_only_commands_still_answer_an_unknown_attempt_when_switched_off(env, tty, capsys, monkeypatch):
    """Final review F1-1: INSIA_PUBLISH=0 after an 'unknown' attempt must not strand its item. ``attempts`` and
    ``resolve --published/--not-published`` touch only the records (the real service, no platform); ``--check``
    and everything that could post stay off. A locked item's error names the way out."""
    item_id = make_item(env)
    attempt_id = _unknown_attempt(env, item_id)
    monkeypatch.setenv("INSIA_PUBLISH", "0")
    code, out, err = run(capsys, "items", "publish", item_id, "--url", "https://www.linkedin.com/feed/update/urn:li:share:1/")
    assert code == 1 and "먼저 정리해 주세요" in err and f"insia publish resolve {attempt_id} --published" in err
    code, out, err = run(capsys, "publish", "attempts", "--json")
    assert code == 0 and [a["id"] for a in json.loads(out)["attempts"]] == [attempt_id]
    code, out, err = run(capsys, "publish", "resolve", attempt_id, "--check")
    assert code == 1 and "API 게시가 꺼져 있어요" in err
    tty.answers.append("y")
    code, out, err = run(capsys, "publish", "resolve", attempt_id, "--not-published")
    assert code == 0 and "'안 올라갔어요'로 정리했어요" in out, err
    code, out, err = run(capsys, "items", "archive", item_id)
    assert code == 0, err


@pytest.mark.parametrize("argv", [("review",), ("revise", "--instructions", "더 짧게")], ids=["review", "revise"])
def test_review_and_revise_stop_before_any_agent_call_on_a_locked_item(env, capsys, argv):
    """Final review F1-2: the CLI checks the publish lock before the job starts (like the server), so no paid
    research, rewrite or review runs only to fail at the first write."""
    item_id = make_item(env)
    attempt_id = _unknown_attempt(env, item_id)
    code, out, err = run(capsys, argv[0], item_id, *argv[1:], "--mode", "mock", "--speed", "0")
    assert code == 1 and "먼저 정리해 주세요" in err and f"insia publish resolve {attempt_id}" in err
    assert "검수" not in out and "수정 중" not in out  # no agent progress at all
    ws = Workspace(env)
    try:
        assert ws.list_runs(kind=argv[0]) == []
        assert ws.get_item(item_id).item.version == 1
    finally:
        ws.close()


def test_approve_and_schedule_mention_api_publishing_only_when_the_dashboard_offers_it(env, capsys, monkeypatch):
    """Final review F1-14: ‘API로 게시’ exists only for a connected LinkedIn/Instagram account with publishing on;
    everyone else is told the manual way (export → post → items publish), as before API publishing existed."""
    from insia_agents.models import PublishConnection

    manual = "INSIA는 예정일에 자동으로 게시하지 않아요. 그날 직접 올린 뒤 'insia items publish'로 표시해 주세요."

    def messages(channel: str) -> str:
        item_id = make_item(env, channel)
        code, out, err = run(capsys, "items", "schedule", item_id, "--date", "2026-10-07")
        assert code == 0, err
        code, approved, err = run(capsys, "items", "approve", item_id)  # scheduled → approved keeps the approval
        assert code == 0, err
        return out + approved

    for channel in ("linkedin", "instagram", "naver_blog", "bizplan"):  # nothing set up
        text = messages(channel)
        assert "API로 게시" not in text and manual in text, channel
    ws = Workspace(env)
    try:
        for platform in ("linkedin", "instagram"):
            ws.save_publish_connection(PublishConnection(platform=platform, account_id="sub-1", status="connected"))
    finally:
        ws.close()
    text = messages("linkedin")
    assert "대시보드의 ‘API로 게시’를 눌러 주세요" in text and "insia publish send" in text
    assert "API로 게시" not in messages("naver_blog")  # no API publishing for this channel at all
    # Instagram is a beta, off unless INSIA_PUBLISH_INSTAGRAM says on (the dashboard draws no button then)
    assert "API로 게시" not in messages("instagram")
    monkeypatch.setenv("INSIA_PUBLISH_INSTAGRAM", "1")
    assert "insia publish send" in messages("instagram") and "--ai-label yes|no" in messages("instagram")
    monkeypatch.setenv("INSIA_PUBLISH_INSTAGRAM", "0")
    assert "API로 게시" not in messages("instagram")
    monkeypatch.setenv("INSIA_PUBLISH_FAKE", "1")  # the service may refuse it here (not a temporary workspace)
    assert "API로 게시" not in messages("linkedin")
    monkeypatch.delenv("INSIA_PUBLISH_FAKE")
    monkeypatch.setenv("INSIA_PUBLISH", "0")
    assert "API로 게시" not in messages("linkedin")


def test_the_item_commands_publish_switches_follow_the_publish_settings(tmp_path, monkeypatch):
    """The item commands read the switches without the publishing package; they must agree with
    ``PublishSettings`` (Instagram's default included), and never say on where the service is off."""
    for publish in (None, "0", "1", "off", "YES", "maybe"):
        for instagram in (None, "0", "1", "on", "no", "maybe"):
            for fake in (None, "1"):
                env = {name: value for name, value in (("INSIA_PUBLISH", publish), ("INSIA_PUBLISH_INSTAGRAM", instagram),
                                                       ("INSIA_PUBLISH_FAKE", fake)) if value is not None}
                for name in ("INSIA_PUBLISH", "INSIA_PUBLISH_INSTAGRAM", "INSIA_PUBLISH_FAKE"):
                    monkeypatch.delenv(name, raising=False)
                for name, value in env.items():
                    monkeypatch.setenv(name, value)
                settings = PublishSettings.from_env(env, tmp_path / "ws")
                switched = {p: cli._api_publishing_switched_on(p) for p in ("linkedin", "instagram")}
                service = {"linkedin": settings.enabled, "instagram": settings.instagram_enabled}
                if fake:  # only the service can tell a temporary workspace: the hints stay out
                    assert switched == {"linkedin": False, "instagram": False}, env
                else:
                    assert switched == service, env
    assert not cli._api_publishing_switched_on("naver_blog")


def test_a_locked_instagram_item_offers_the_recheck_only_when_it_would_run(monkeypatch):
    """``resolve --check`` asks Instagram, so the locked-item hint names it only while Instagram API publishing is
    switched on; the record-only answers (--published / --not-published) are always there."""
    from insia_agents.db import ItemLockedError

    locked = ItemLockedError(attempt_id="pa_" + "4" * 24, platform="instagram", status="unknown")
    for publish, instagram, offered in ((None, None, False), (None, "1", True), ("0", "1", False), (None, "0", False)):
        for name, value in (("INSIA_PUBLISH", publish), ("INSIA_PUBLISH_INSTAGRAM", instagram)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        hint = cli.locked_item_hint(locked)
        assert f"insia publish resolve {locked.attempt_id} --published" in hint and "--not-published" in hint
        assert ("--check" in hint) is offered, (publish, instagram)
    linkedin = ItemLockedError(attempt_id="pa_" + "5" * 24, platform="linkedin", status="unknown")
    assert "--check" not in cli.locked_item_hint(linkedin)


def test_attempts_table_shows_steps_in_words_and_how_to_fill_a_missing_address(env, stubs, capsys, monkeypatch):
    """Final review F1-3 (follow-up): ``insia publish attempts`` names an attempt in flight by its step in words
    (the attempt JSON's ``step_label``), never ``self_check`` / ``media`` / ``write``; a published attempt without
    an address says so (not its last step) and the Instagram candidates and ``--permalink`` follow the table."""
    item_id = make_item(env, "instagram")
    candidates = [{"id": "18000000000000002", "permalink": "https://www.instagram.com/p/MANUAL/",
                   "timestamp": "2026-09-28T03:04:00Z"},
                  {"id": "18000000000000001", "permalink": "https://www.instagram.com/p/APIPOST/",
                   "timestamp": "2026-09-28T03:00:20Z"}]

    def attempt(digit: str, status: str, step: str, **fields: Any) -> PublishAttempt:
        return PublishAttempt(id="pa_" + digit * 24, item_id=item_id, version=1, platform="instagram", status=status,
                              step=step, created_at="2026-09-28T03:00:00Z", updated_at="2026-09-28T03:10:00Z", **fields)

    attempts = [attempt("1", "sending", "self_check"), attempt("2", "sending", "children 3/8"),
                attempt("3", "sending", "media"), attempt("4", "sending", "some_future_step"),
                attempt("5", "published", "write", state={"candidates": candidates, "permalink_missing": True}),
                attempt("6", "published", "write"),
                attempt("7", "published", "write", permalink="https://www.instagram.com/p/DONE/"),
                attempt("8", "failed", "write", error="인스타그램에서 오류가 났어요(코드 100). 아무것도 게시되지 않았어요.")]
    original = CliStub.__init__

    def init(self, workspace, env=None):
        original(self, workspace, env)
        self.attempts = {a.id: a for a in attempts}

    monkeypatch.setattr(CliStub, "__init__", init)
    code, out, err = run(capsys, "publish", "attempts")
    assert code == 0, err
    rows = {line.split()[0][3:4]: line for line in out.splitlines() if line.startswith("pa_")}
    assert set(rows) == set("12345678")
    for raw in ("self_check", "media", "children", "write", "some_future_step"):
        assert raw not in out, raw
    assert rows["1"].endswith("단계: 공개 주소 확인") and rows["2"].endswith("단계: 이미지 등록 3/8")
    assert rows["3"].endswith("단계: 이미지 올릴 준비") and rows["4"].rstrip().endswith(item_id)  # no words: left out
    assert rows["5"].endswith("게시물 주소 없음 (후보 2개)") and rows["6"].endswith("게시물 주소 없음")
    assert rows["7"].endswith("https://www.instagram.com/p/DONE/") and "코드 100" in rows["8"]
    first, second = "pa_" + "5" * 24, "pa_" + "6" * 24
    assert f"{first}: 게시물 주소를 하나로 정하지 못했어요" in out
    assert "1. 2026-09-28 12:04  https://www.instagram.com/p/MANUAL/" in out  # KST, like resolve --check
    assert f"insia publish resolve {first} --permalink <주소>" in out
    assert f"{second}: 게시물 주소를 받지 못했어요" in out and f"insia publish resolve {second} --permalink <주소>" in out
    code, out, err = run(capsys, "publish", "attempts", "--json")
    assert code == 0 and {a["id"]: a["step_label"] for a in json.loads(out)["attempts"]}["pa_" + "1" * 24] == "공개 주소 확인"


def test_send_progress_and_failure_lines_use_the_dashboards_words(capsys):
    """Final review F1-3: Instagram's 'media'/'self_check' steps print in Korean, and an Instagram failure that
    already says "아무것도 게시되지 않았어요" does not get a second "nothing was posted" sentence."""
    printer = cli._publish_step_printer("instagram")
    for step in ("check", "media", "self_check", "children 2/7", "some_future_step", "write"):
        printer(step)
    assert capsys.readouterr().out.splitlines() == ["  · 연결 확인", "  · 이미지 올릴 준비", "  · 공개 주소 확인",
                                                    "  · 이미지 등록 2/7", "  · 게시 요청 보내는 중"]
    failed = PublishAttempt(id="pa_" + "1" * 24, item_id="it_x", version=1, platform="instagram", status="failed",
                            error="인스타그램에서 오류가 났어요(코드 100). 아무것도 게시되지 않았어요.")
    assert cli._publish_print_outcome(failed, "인스타그램") == 1
    err = capsys.readouterr().err
    assert err.strip() == "게시하지 못했어요: 인스타그램에서 오류가 났어요(코드 100). 아무것도 게시되지 않았어요."
    assert cli._publish_report_interrupted(failed.model_copy(update={"error": "이미지 형식을 받지 않았어요."}),
                                           "인스타그램", looked_up=True) == 1
    assert capsys.readouterr().err.count("아무것도 올라가지 않았어요") == 1


def test_resolve_check_lists_instagram_candidates_and_permalink_records_one(env, stubs, capsys, monkeypatch):
    """Final review F1-6: a re-check that found several recent posts prints them (time + address) and says how to
    record the right one; ``--permalink`` fills it in without a terminal (a record, never a post)."""
    item_id = make_item(env, "instagram")
    candidates = [{"id": "18000000000000002", "permalink": "https://www.instagram.com/p/MANUAL/",
                   "timestamp": "2026-09-28T03:04:00Z"},
                  {"id": "18000000000000001", "permalink": "https://www.instagram.com/p/APIPOST/",
                   "timestamp": "2026-09-28T03:00:20Z"}]
    attempt = PublishAttempt(id="pa_" + "7" * 24, item_id=item_id, version=1, platform="instagram", status="published",
                             resolved_by="instagram_check", state={"candidates": candidates, "permalink_missing": True},
                             created_at="2026-09-28T03:00:00Z", updated_at="2026-09-28T03:10:00Z")
    original = CliStub.__init__

    def init(self, workspace, env=None):
        original(self, workspace, env)
        self.attempts = {attempt.id: attempt}

    monkeypatch.setattr(CliStub, "__init__", init)
    monkeypatch.setattr(cli, "_publish_is_tty", lambda: False)  # both are fine in a script
    code, out, err = run(capsys, "publish", "resolve", "pa_7777", "--check")
    assert code == 0, err
    assert "게시물 주소를 하나로 정하지 못했어요" in out
    assert "1. 2026-09-28 12:04  https://www.instagram.com/p/MANUAL/" in out  # KST
    assert "2. 2026-09-28 12:00  https://www.instagram.com/p/APIPOST/" in out
    assert f"insia publish resolve {attempt.id} --permalink <주소>" in out
    code, out, err = run(capsys, "publish", "resolve", "pa_7777", "--permalink", "https://www.instagram.com/p/APIPOST/")
    assert code == 0 and "게시물 주소를 넣었어요: https://www.instagram.com/p/APIPOST/" in out, err
    assert stubs[-1].called("set_permalink")[0]["url"] == "https://www.instagram.com/p/APIPOST/"
    assert stubs[-1].called("resolve") == []


# ---------------------------------------------------------------------------
# backup, doctor, serve
# ---------------------------------------------------------------------------


def test_backup_leaves_out_credentials_publish_and_logs(env, tmp_path, capsys):
    make_item(env)
    (env / "credentials").mkdir(mode=0o700)
    with sqlite3.connect(env / "credentials" / "secrets.sqlite") as conn:
        conn.execute("CREATE TABLE secrets (platform TEXT, key TEXT, value TEXT)")
        conn.execute("INSERT INTO secrets VALUES ('instagram', 'access_token', ?)", (IG_TOKEN,))
    for folder, name in (("publish/public/0123456789abcdef0123456789abcdef", "01.jpg"), ("logs", "server.log"),
                         ("uploads", "회사소개서.pdf")):
        (env / folder).mkdir(parents=True, exist_ok=True)
        (env / folder / name).write_bytes(b"data " + IG_TOKEN.encode() if folder == "logs" else b"data")
    (env / "prices.json").write_text("{}", encoding="utf-8")
    out_dir = tmp_path / "backups" / "2026-09-28"
    code, out, err = run(capsys, "backup", "--out", out_dir, "--json")
    assert code == 0, err
    report = json.loads(out)
    assert report["ok"] is True and report["db_check"] == "ok"
    assert set(report["copied"]) == {"insia.db", "uploads/", "prices.json"}
    assert {"credentials/", "publish/", "logs/"} <= set(report["skipped"])
    names = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*")}
    assert names == {"insia.db", "uploads", "uploads/회사소개서.pdf", "prices.json"}
    assert not any(IG_TOKEN.encode() in p.read_bytes() for p in out_dir.rglob("*") if p.is_file())
    with sqlite3.connect(out_dir / "insia.db") as conn:  # the copy is a whole, openable database
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    # refuses a non-empty folder and a folder inside what must stay out
    assert run(capsys, "backup", "--out", out_dir)[0] == 1
    assert run(capsys, "backup", "--out", env / "credentials" / "copy")[0] == 2


def test_doctor_reports_publishing_checks_without_values(env, stubs, capsys, monkeypatch):
    monkeypatch.setenv("INSIA_LINKEDIN_CLIENT_SECRET", CLIENT_SECRET)
    code, out, err = run(capsys, "doctor", "--json")
    labels = {check["label"]: check for check in json.loads(out)["checks"]}
    assert labels["API 게시"]["level"] == "ok" and labels["API 게시 · LinkedIn"]["level"] == "ok"
    assert labels["API 게시 · 인스타그램"]["message"].startswith("인스타그램 API 게시")
    assert labels["API 게시 · 이미지 공개 주소"]["level"] == "warn"
    assert labels["API 게시 · 카드 렌더링"]["level"] == "ok"
    assert labels["API 게시 · 토큰 폴더"]["message"].startswith("credentials")
    assert CLIENT_SECRET not in out + err
    assert stubs[0].names() == ["doctor_report", "shutdown"]


def test_serve_passes_the_media_options(env, capsys, monkeypatch):
    seen: dict[str, Any] = {}

    class FakeServer:
        url = "http://127.0.0.1:8765/"
        token_required = False
        public_hosts = ()
        web_root = None
        manager = SimpleNamespace(interrupted_on_start=0)
        publish = SimpleNamespace(summary_line=lambda: "API 게시: LinkedIn 연결됨 · 인스타그램 공개 주소 없음(수동 게시)")

        def serve_forever(self, poll_interval=0.5):
            raise KeyboardInterrupt

        def server_close(self):
            seen["closed"] = True

    def fake_make_server(settings, host, port, web_dir, *, token=None, public_hosts=(), trust_proxy=False,
                         quiet=True, media_port=None, media_base_url=None):
        seen.update(media_port=media_port, media_base_url=media_base_url)
        return FakeServer()

    monkeypatch.setattr(server_module, "make_server", fake_make_server)
    code, out, err = run(capsys, "serve", "--media-port", "8766", "--media-base-url", "https://media.example.com")
    assert code == 0, err
    assert seen == {"media_port": 8766, "media_base_url": "https://media.example.com", "closed": True}
    assert "API 게시: LinkedIn 연결됨" in out


# ---------------------------------------------------------------------------
# The same commands once against the real PublishService (package A) in fake-platform mode
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
            "INSIA_LINKEDIN_CLIENT_SECRET": CLIENT_SECRET}


class Interrupting:
    """The fake platform, but Ctrl+C arrives while one chosen request is on its way."""

    def __init__(self, when=None) -> None:
        from insia_agents.publishers.http import FakePlatformTransport

        self.inner = FakePlatformTransport()
        self.when = when

    @property
    def requests(self):
        return self.inner.requests

    def request(self, method: str, url: str, **kwargs: Any):
        if self.when is not None and self.when(method, url):
            self.inner.requests.append((method.upper(), url))
            raise KeyboardInterrupt
        return self.inner.request(method, url, **kwargs)


@pytest.fixture
def real_cli(env, monkeypatch):
    """A workspace with a (fake-platform) LinkedIn connection; ``made`` holds each command's real service."""
    from insia_agents.publishers import PublishService

    for key, value in REAL_ENV.items():
        monkeypatch.setenv(key, value)
    ws = Workspace(env)
    try:
        service = PublishService.from_env(ws)
        start = service.linkedin_connect()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(start.authorize_url).query)["state"][0]
        service.linkedin_complete(f"code=AQTfake&state={state}")
        service.shutdown(timeout=1.0)
    finally:
        ws.close()
    original = cli._publish_service
    made: list[Any] = []
    box = SimpleNamespace(made=made, when=None)

    def factory(settings, workspace, **kwargs):
        made.append(original(settings, workspace, transport=Interrupting(box.when), **kwargs))
        return made[-1]

    monkeypatch.setattr(cli, "_publish_service", factory)
    return box


def _posts(service) -> list[Any]:
    return [r for r in service.transport.requests if r[0] == "POST" and "/rest/posts" in r[1]]


@real_only
def test_real_service_send_needs_the_code_shown_on_this_terminal(env, real_cli, tty, capsys, monkeypatch):
    item_id = make_item(env)
    monkeypatch.setattr(cli, "_publish_is_tty", lambda: False)  # cron, a script, a pipe
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 2 and real_cli.made == []  # no service was built, so no transport call of any kind
    monkeypatch.setattr(cli, "_publish_is_tty", lambda: True)
    tty.answers.append("AAAAAA")
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 2 and "확인 코드가 맞지 않아요" in err
    # the only request is the preview's read-only account check (DESIGN.md 4-0), made before the code prompt so
    # the person sees which account would post; nothing is written
    requests = real_cli.made[-1].transport.requests
    assert requests == [("GET", "api.linkedin.com/v2/userinfo")]
    assert _posts(real_cli.made[-1]) == []
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 0, err
    assert "게시했어요: https://example.invalid/linkedin/feed/update/urn:li:share:" in out
    assert len(_posts(real_cli.made[-1])) == 1
    ws = Workspace(env)
    try:
        item = ws.get_item(item_id).item
        assert item.status == "published" and item.published_via == "fake"
        [attempt] = ws.list_publish_attempts(item_id=item_id)
        assert attempt.requested_by.startswith("cli:")
    finally:
        ws.close()
    assert CLIENT_SECRET not in out + err


@real_only
def test_real_service_ctrl_c_before_and_after_the_write_step(env, real_cli, tty, capsys, monkeypatch):
    item_id = make_item(env)

    def stop_at(wanted):
        def printer(platform):
            def on_step(step):
                if step == wanted:
                    raise KeyboardInterrupt
            return on_step
        return printer

    monkeypatch.setattr(cli, "_publish_step_printer", stop_at("check"))  # before anything irreversible
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 1 and "아무것도 올리지 않았어요" in err and _posts(real_cli.made[-1]) == []
    ws = Workspace(env)
    try:
        [first] = ws.list_publish_attempts(item_id=item_id)
        assert (first.status, first.error_code) == ("failed", "interrupted")
    finally:
        ws.close()
    monkeypatch.setattr(cli, "_publish_step_printer", lambda platform: None)
    real_cli.when = lambda method, url: method == "POST" and url.endswith("/rest/posts")  # while the post is sent
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert code == 1 and "올라갔을 수도 있어요" in err
    ws = Workspace(env)
    try:
        latest = ws.list_publish_attempts(item_id=item_id)[0]
        assert latest.status == "unknown" and latest.id in err
        assert ws.active_publish_attempt(item_id).id == latest.id  # the item stays locked until a person says
    finally:
        ws.close()
    real_cli.when = None
    tty.answers.append("y")
    code, out, err = run(capsys, "publish", "resolve", latest.id, "--not-published")
    assert code == 0 and "안 올라갔어요" in out


@real_only
def test_real_service_ctrl_c_while_closing_after_the_post_keeps_the_outcome(env, real_cli, tty, capsys, monkeypatch):
    from insia_agents.publishers import PublishService

    item_id = make_item(env)
    original = PublishService.shutdown
    pressed: list[bool] = []

    def shutdown(self, timeout=5.0):
        original(self, timeout=timeout)
        if not pressed:
            pressed.append(True)
            raise KeyboardInterrupt  # the person presses Ctrl+C while the command closes, after the post went out

    monkeypatch.setattr(PublishService, "shutdown", shutdown)
    tty.answers.append(code_from)
    code, out, err = run(capsys, "publish", "send", item_id)
    assert pressed and code == 0, err
    assert "게시했어요: https://example.invalid/linkedin/feed/update/urn:li:share:" in out
    assert NOTHING_SENT not in err + out and "게시 결과는 위에 적은 그대로예요" in err
    ws = Workspace(env)
    try:
        assert ws.get_item(item_id).item.status == "published"
        assert [a.status for a in ws.list_publish_attempts(item_id=item_id)] == ["published"]
    finally:
        ws.close()


# The CLI next to a server (``docker compose exec insia insia publish connect linkedin --paste``) must derive the
# redirect URI and media mode the server uses, from the same INSIA_PUBLIC_HOSTS / INSIA_TRUST_PROXY / INSIA_PORT.
CALLBACK = "/oauth/linkedin/callback"


@real_only
@pytest.mark.parametrize(("public_hosts", "trust_proxy", "expected"), ids=[
    "domain-behind-proxy", "localhost-port", "domain-without-proxy",
], argvalues=[
    ("insia.example.com", "1", "https://insia.example.com" + CALLBACK),  # one domain behind a reverse proxy
    ("", "", "http://localhost:{port}" + CALLBACK),                        # INSIA_PORT decides the port
    (" NAS.local. ,", "", "http://localhost:{port}" + CALLBACK),          # a domain without a proxy: not https
])
def test_cli_derives_the_servers_redirect_uri_from_the_same_environment(env, capsys, monkeypatch, public_hosts,
                                                                        trust_proxy, expected):
    for key, value in REAL_ENV.items():
        if key != "INSIA_PUBLISH_FAKE":
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("INSIA_PUBLIC_HOSTS", public_hosts)
    monkeypatch.setenv("INSIA_TRUST_PROXY", trust_proxy)
    settings = replace(Settings.from_env(env={}, mode="mock", speed=0.0), home=env, out_dir=env.parent / "outputs",
                       sample_dir=None, web_dir=None)
    server = server_module.make_server(settings, "127.0.0.1", 0, token="cli-redirect-token-0123456789")
    try:  # the server reads the same environment (make_server's defaults)
        port = int(server.server_address[1])
        server_uri = server.publish.platform_status("linkedin")["app"]["redirect_uri"]
    finally:
        server.server_close()
    monkeypatch.setenv("INSIA_PORT", str(port))
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 0, err
    assert json.loads(out)["platforms"]["linkedin"]["app"]["redirect_uri"] == server_uri == expected.format(port=port)


@real_only
def test_cli_reads_public_hosts_like_the_server(env, capsys, monkeypatch):
    for key, value in REAL_ENV.items():
        if key != "INSIA_PUBLISH_FAKE":
            monkeypatch.setenv(key, value)
    # an IPv6 literal in brackets is the same host as the media URL's (the server's normalize_public_hosts), so
    # the media mode is "main" (configuration B) for the CLI as it is for the server
    monkeypatch.setenv("INSIA_PUBLIC_HOSTS", "[2606:4700::1111]")
    monkeypatch.setenv("INSIA_TRUST_PROXY", "yes")
    monkeypatch.setenv("INSIA_MEDIA_BASE_URL", "https://[2606:4700::1111]")
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["platforms"]["linkedin"]["app"]["redirect_uri"] == "https://[2606:4700::1111]" + CALLBACK
    assert (data["media"]["mode"], data["media"]["valid"]) == ("main", True)
    # a value the server refuses to start with stops the command too (exit 1, nothing opened on the platform)
    monkeypatch.setenv("INSIA_PUBLIC_HOSTS", "https://insia.example.com")
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 1 and "INSIA_PUBLIC_HOSTS" in err and out == ""
