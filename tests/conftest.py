from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # allow running without `pip install -e .`
    sys.path.insert(0, str(ROOT / "src"))

from insia_agents.config import Settings  # noqa: E402
from insia_agents.models import ALL_CHANNELS, Brief  # noqa: E402
from insia_agents.prompt_loader import clear_cache  # noqa: E402
from insia_agents.publishers.settings import PUBLISH_ENV_VARS  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """No real credentials, no ~/.config/anthropic, no INSIA_* leaking in."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "INSIA_MODE", "INSIA_MODEL", "INSIA_FALLBACKS",
                 "INSIA_OUT_DIR", "INSIA_PROMPTS_DIR", "INSIA_SAMPLE_DIR", "INSIA_WEB_DIR", "INSIA_TODAY",
                 "INSIA_EFFORT_ORCHESTRATOR", "INSIA_EFFORT_RESEARCHER", "INSIA_EFFORT_REVIEWER",
                 *PUBLISH_ENV_VARS):  # API publishing: no real app ids, secrets, media URLs or fake mode leak in
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def settings(tmp_path) -> Settings:
    base = Settings.from_env(env={}, mode="mock", speed=0.0, today="2026-09-28")
    return replace(base, out_dir=tmp_path / "outputs", sample_dir=None, web_dir=None)


@pytest.fixture
def brief() -> Brief:
    return Brief(
        topic="테스트 주제",
        goal="서비스 소개",
        audience="1인 창업자",
        channels=list(ALL_CHANNELS),
        keywords=["AI 마케팅 자동화", "1인 창업"],
        tone="친근한 전문가 톤",
    )


@pytest.fixture
def prompts_dir(tmp_path, monkeypatch) -> Path:
    """Dummy prompt files so tests never depend on the real prompt contents."""
    root = tmp_path / "prompts"
    for role in ("orchestrator", "researcher", "reviewer"):
        (root / "agents").mkdir(parents=True, exist_ok=True)
        (root / "agents" / f"{role}.md").write_text(f"# {role} 테스트 프롬프트\n역할: {role}", encoding="utf-8")
    for channel in ALL_CHANNELS:
        (root / "channels").mkdir(parents=True, exist_ok=True)
        (root / "channels" / f"{channel}.md").write_text(f"# {channel} 테스트 가이드", encoding="utf-8")
    monkeypatch.setenv("INSIA_PROMPTS_DIR", str(root))
    clear_cache()
    return root


# ---------------------------------------------------------------------------
# API publishing (package A): no network, a controllable clock, one real JPEG render per session
# ---------------------------------------------------------------------------

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}


def _loopback(host: object) -> bool:
    name = str(host or "").strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


@pytest.fixture
def no_network(monkeypatch):
    """Any connection (or DNS lookup) to a non-loopback address fails the test with ``AssertionError``.

    Publishing test modules switch it on with ``pytestmark = pytest.mark.usefixtures("no_network")``; loopback
    test servers (port 0) keep working."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(address: object) -> None:
        if isinstance(address, tuple) and address and not _loopback(address[0]):
            raise AssertionError(f"network access in a test: {address[0]}")

    def connect(self, address):  # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check(address)
        return real_connect(self, address)

    def connect_ex(self, address):  # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):  # noqa: ANN001
        if host is not None and not _loopback(host):
            raise AssertionError(f"DNS lookup in a test: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield


class FakeClock:
    """``publishers.base.Clock`` for tests: ``sleep`` only moves time forward (and is recorded); ``wait`` (the
    heartbeat / maintenance threads) blocks a few real milliseconds on the event without moving time, so those
    threads keep beating without racing the test's own time line."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = (start or datetime.now(timezone.utc)).replace(microsecond=0)
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += timedelta(seconds=float(seconds))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.advance(seconds)

    def wait(self, event: threading.Event, seconds: float) -> bool:
        return event.wait(min(float(seconds), 0.02))


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def rendered_jpegs():
    """A real 2-slide carousel rendered once as JPEG 1080×1350 (``render_images(..., image_type="jpeg")``).
    Skips when Playwright, Chromium or a Hangul font is missing."""
    from insia_agents.exporters.instagram import RenderUnavailable, parse_carousel, render_images, slides_html

    content = ("## 캐러셀\n### 슬라이드 1 — 표지\n- 문구: 혼자 운영하는 채널 셋\n- 대체텍스트: 남색 배경 표지\n"
               "### 슬라이드 2 — 마무리\n- 문구: 저장해 두세요\n- 대체텍스트: 마무리 장\n")
    slides = parse_carousel(content)
    try:
        return render_images(slides_html(slides, None, render_mode=True), len(slides), image_type="jpeg")
    except RenderUnavailable as exc:
        pytest.skip(f"JPEG 렌더링을 쓸 수 없어요: {exc}")


def fake_jpeg(width: int = 1080, height: int = 1350, *, marker: int = 0xC0, mpf: bool = False, tag: bytes = b"") -> bytes:
    """A tiny hand-made JPEG header (SOI, APP0, [APP2 MPF], SOF0/SOF2, SOS, EOI) — enough for ``jpeg_info``."""
    import struct

    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app2 = (b"\xff\xe2" + struct.pack(">H", 10) + b"MPF\x00II*\x00") if mpf else b""
    sof = bytes([0xFF, marker]) + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + app2 + sof + b"\xff\xda\x00\x02" + tag + b"\xff\xd9"


LINKEDIN_BODY = ("혼자 창업하면 마케팅은 늘 '이번 주만 넘기고'가 돼요.\n\n"
                 "그래서 반복 업무부터 덜어 냈어요 (출처: 통계청, 2025년 기준).\n\n"
                 "여러분은 어떤 일을 먼저 덜어 내고 싶나요?")
INSTAGRAM_CONTENT = ("## 캐러셀\n" + "".join(f"### 슬라이드 {n} — 제목 {n}\n- 문구: 문구 {n}\n- 대체텍스트: 슬라이드 {n} 설명\n"
                                          for n in range(1, 8))
                     + "\n## 캡션\n혼자 채널 셋을 운영한다면 저장해 두세요.\n\n#1인창업 #AI마케팅 #SNS운영")


class PublishKit:
    """Builds publishing test worlds: a temporary workspace, approved items, a service with ``FakeTransport``,
    ``FakeClock`` and a fake JPEG renderer, and connections stored directly (no OAuth round trip)."""

    LI_TOKEN = "AQUv-li-secret-token-0123456789"
    LI_SUB = "782bbtaQ"
    IG_TOKEN = "IGAA-ig-secret-token-0123456789"
    IG_ID = "17841400000000001"

    def __init__(self, tmp_path: Path, clock: FakeClock) -> None:
        from insia_agents.db import Workspace

        self.home = tmp_path / "ws"
        self.clock = clock
        self.workspace = Workspace(self.home)
        self.renders: list[int] = []
        self.services: list = []

    FakeClock = FakeClock
    fake_jpeg = staticmethod(fake_jpeg)

    def renderer(self, page_html: str, count: int) -> list[bytes]:
        self.renders.append(count)
        return [fake_jpeg(tag=bytes([n])) for n in range(count)]

    def settings(self, env: dict | None = None, **kwargs):
        from insia_agents.publishers import PublishSettings

        return PublishSettings.from_env(env or {}, self.home, **kwargs)

    def service(self, env: dict | None = None, *, transport=None, instagram: bool = False, renderer=True,
                workspace=None, **kwargs):
        from insia_agents.publishers import FakeTransport, PublishService

        env = dict(env or {})
        if instagram:
            env.setdefault("INSIA_PUBLISH_INSTAGRAM", "1")
            env.setdefault("INSIA_MEDIA_BASE_URL", "https://media.example.com")
            env.setdefault("INSIA_PUBLISH_SKIP_SELF_CHECK", "1")
            kwargs.setdefault("media_port", 0)
        service = PublishService(self.settings(env, **kwargs), workspace or self.workspace,
                                 transport=transport if transport is not None else FakeTransport(), clock=self.clock,
                                 renderer=self.renderer if renderer is True else (renderer or None))
        self.services.append(service)
        return service

    def item(self, channel: str = "linkedin", *, content: str | None = None, hashtags=None, approve: bool = True,
             title: str = "테스트 콘텐츠") -> str:
        from insia_agents.models import Draft

        if content is None:
            content = LINKEDIN_BODY if channel == "linkedin" else INSTAGRAM_CONTENT
        if hashtags is None:
            hashtags = ["#창업", "#마케팅", "#1인기업"] if channel == "linkedin" else ["#1인창업", "#AI마케팅", "#SNS운영"]
        item = self.workspace.create_item(channel, title)
        self.workspace.add_version(item.id, Draft(channel=channel, round=0, title=title, content=content,
                                                  hashtags=list(hashtags)), source="human")
        if approve:
            self.workspace.set_item_status(item.id, "approved", force=True)
        return item.id

    def connect_linkedin(self, service, *, sub: str | None = None, token: str | None = None, days: int = 60) -> None:
        from insia_agents.models import PublishConnection

        now = self.clock.now()
        sub = sub or self.LI_SUB
        service.store.set_many("linkedin", {
            "client_id": "86client", "client_secret": "li-client-secret-value", "access_token": token or self.LI_TOKEN,
            "scope": "openid profile w_member_social", "sub": sub,
            "expires_at": (now + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
            "issued_at": now.isoformat().replace("+00:00", "Z")})
        self.workspace.save_publish_connection(PublishConnection(platform="linkedin", account_id=sub, status="connected"))

    def connect_instagram(self, service, *, user_id: str | None = None, token: str | None = None, days: int = 60,
                          issued_hours_ago: float = 0, estimated: bool = False, refreshed: bool = False) -> None:
        from insia_agents.models import PublishConnection

        now = self.clock.now()
        user_id = user_id or self.IG_ID
        issued = now - timedelta(hours=issued_hours_ago)
        service.store.set_many("instagram", {
            "access_token": token or self.IG_TOKEN, "user_id": user_id, "username": "insia.kr",
            "account_type": "BUSINESS", "expires_at": (now + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
            "issued_at": issued.isoformat().replace("+00:00", "Z"),
            "refreshed_at": issued.isoformat().replace("+00:00", "Z") if refreshed else None,
            "expires_estimated": "1" if estimated else "0"})
        self.workspace.save_publish_connection(PublishConnection(platform="instagram", account_id=user_id,
                                                                 account_name="@insia.kr", status="connected"))

    def close(self) -> None:
        for service in self.services:
            try:
                service.shutdown(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self.workspace.close()


@pytest.fixture
def publish_kit(tmp_path, fake_clock):
    kit = PublishKit(tmp_path, fake_clock)
    yield kit
    kit.close()
