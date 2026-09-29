"""Instagram publishing (package A): carousel transform and limits, JPEG checks, the Graph API flow, polling,
``2207008`` / lost answers, read-only reconcile and token refresh (DESIGN.md 4-2, 14.2, 14.3; IG §3–§7).
Offline: a scripted FakeTransport, a FakeClock (no real sleeping) and a fake JPEG renderer."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from insia_agents.models import PublishAttempt
from insia_agents.publishers import (
    AccountTypeError,
    FakeTransport,
    HumanConfirmation,
    InvalidOptionsError,
    InvalidTokenError,
    TransportError,
    json_response,
)
from insia_agents.publishers.instagram import POLL_TOTAL_SECONDS, refresh_due, status_subcode
from insia_agents.publishers.media import jpeg_info

pytestmark = pytest.mark.usefixtures("no_network")

ME = r"/v25\.0/me$"
QUOTA = r"/content_publishing_limit$"
MEDIA_POST = r"/17841400000000001/media$"
PUBLISH = r"/media_publish$"


class StubGuard:
    attempt_id = "pa_ig"

    def __init__(self) -> None:
        self.steps: list[tuple[str, dict | None]] = []
        self.claims = 0
        self.media_token = ""

    def step(self, step, state_patch=None):
        self.steps.append((step, state_patch))

    def claim_write(self, step="write"):
        self.claims += 1

    def set_media_token(self, token):
        self.media_token = token


def _setup(kit, *, quota=(2, 50), me=None, transport=None):
    fake = transport or FakeTransport()
    service = kit.service(transport=fake, instagram=True)
    kit.connect_instagram(service)
    fake.add("GET", ME, json_response(200, me or {"data": [{"user_id": kit.IG_ID, "username": "insia.kr",
                                                             "account_type": "BUSINESS"}]}), repeat=True)
    fake.add("GET", QUOTA, json_response(200, {"data": [{"quota_usage": quota[0], "config": {"quota_total": quota[1],
                                                                                             "quota_duration": 86400}}]}),
             repeat=True)
    return service, fake


def _content(slides: int = 7, caption: str = "혼자 채널 셋을 운영한다면 저장해 두세요.\n\n#1인창업 #AI마케팅 #SNS운영",
             alt: str = "슬라이드 설명") -> str:
    body = "".join(f"### 슬라이드 {n} — 제목 {n}\n- 문구: 문구 {n}\n- 대체텍스트: {alt} {n}\n" for n in range(1, slides + 1))
    return f"## 캐러셀\n{body}\n## 캡션\n{caption}"


def _preview(kit, service, *, content=None, ai=False, hashtags=None):
    item_id = kit.item("instagram", content=content, hashtags=hashtags if hashtags is not None else [])
    return item_id, service.preview(item_id, options={"is_ai_generated": ai}, via="dashboard", requested_by="dashboard@t")


def test_ai_label_is_required_and_part_of_the_hash(publish_kit):
    service, _ = _setup(publish_kit)
    item_id = publish_kit.item("instagram")
    for options in (None, {}, {"is_ai_generated": 1}, {"is_ai_generated": "true"}):
        with pytest.raises(InvalidOptionsError, match="AI 정보 라벨"):
            service.preview(item_id, options=options, via="dashboard", requested_by="dashboard@t")
    assert publish_kit.renders == []  # nothing rendered before the person chose
    yes = service.preview(item_id, options={"is_ai_generated": True}, via="dashboard", requested_by="dashboard@t")
    no = service.preview(item_id, options={"is_ai_generated": False}, via="dashboard", requested_by="dashboard@t")
    assert yes.preview_hash != no.preview_hash and yes.content["options"] == {"is_ai_generated": True}


def test_clean_draft_runs_first(publish_kit):
    service, _ = _setup(publish_kit)
    content = _content(caption="캡션\x01에 제어 문자\x0b둘째 줄\n\n#1인창업 #AI마케팅 #SNS운영", alt="대체\x02텍스트")
    _, result = _preview(publish_kit, service, content=content)
    assert "\x01" not in result.content["text"] and "캡션에 제어 문자\n둘째 줄" in result.content["text"]
    assert result.slides[0]["alt"] == "대체텍스트 1" and result.can_publish


def test_caption_hashtag_mention_and_slide_limits(publish_kit):
    service, _ = _setup(publish_kit)
    tags = "#1인창업 #AI마케팅 #SNS운영"
    for caption_len, ok in ((2200, True), (2201, False)):
        caption = "가" * (caption_len - len("\n\n" + tags)) + "\n\n" + tags
        _, result = _preview(publish_kit, service, content=_content(caption=caption))
        assert result.content["chars"] == caption_len and result.can_publish is ok
    for count, mentions, code in ((5, 20, None), (6, 0, "hashtags"), (3, 21, "mentions")):
        caption = "본문 " + " ".join(f"@user{n}" for n in range(mentions)) + "\n\n" + " ".join(f"#태그{n}" for n in range(count))
        _, result = _preview(publish_kit, service, content=_content(caption=caption))
        assert [e.code for e in result.errors] == ([code] if code else [])
    for slides, ok, warn in ((1, False, False), (2, True, True), (10, True, False), (11, False, False)):
        publish_kit.renders.clear()
        _, result = _preview(publish_kit, service, content=_content(slides=slides))
        assert result.can_publish is ok
        assert any(w.code == "slides_few" for w in result.warnings) is warn
        assert publish_kit.renders == ([slides] if ok else [])  # nothing is rendered for a preview with errors


def test_jpeg_info_from_handmade_bytes(publish_kit):
    make = publish_kit.fake_jpeg
    info = jpeg_info(make())
    assert (info.width, info.height, info.baseline, info.has_mpf) == (1080, 1350, True, False)
    assert abs(info.ratio - 0.8) < 1e-9
    assert jpeg_info(make(marker=0xC2)).baseline is False  # progressive
    assert jpeg_info(make(mpf=True)).has_mpf is True  # MPO
    assert jpeg_info(make(640, 480)).width == 640
    for bad in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xe0\x00", b"\xff\xd8\xff\xd9"):
        with pytest.raises(ValueError):
            jpeg_info(bad)


def test_image_checks_block_the_preview(publish_kit, monkeypatch):
    from insia_agents.publishers import instagram

    monkeypatch.setattr(instagram, "IG_MAX_BYTES", 200)  # the 8,000,000-byte rule, without 8 MB of test data
    make = publish_kit.fake_jpeg
    images = [make(), make(1080, 1080), make(marker=0xC2), make(mpf=True), make(1080, 2100),
              make(tag=b"\x00" * 201), make()]
    service = publish_kit.service(instagram=True, renderer=lambda page, count: images[:count])
    publish_kit.connect_instagram(service)
    service.transport.add("GET", ME, json_response(200, {"user_id": publish_kit.IG_ID, "username": "x"}), repeat=True)
    service.transport.add("GET", QUOTA, json_response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}),
                          repeat=True)
    _, result = _preview(publish_kit, service)
    messages = [e.message for e in result.errors]
    assert len(messages) == 5 and not result.can_publish
    assert "슬라이드 2: 1번 슬라이드와 비율이 달라요" in messages[0]
    assert "baseline" in messages[1] and "baseline" in messages[2]
    assert "기준(4:5~1.91:1) 밖" in messages[3] and "8MB" in messages[4]


def _script_flow(fake, *, status_answers=None, publish_answer=None, permalink="https://www.instagram.com/p/abc/"):
    ids = iter(range(1000, 1100))
    fake.add("POST", MEDIA_POST, lambda r: json_response(200, {"id": str(next(ids))}), repeat=True)
    fake.add("GET", r"/v25\.0/1\d{3}$", *(status_answers or [json_response(200, {"status_code": "FINISHED"})]),
             repeat=True)
    fake.add("POST", PUBLISH, publish_answer or json_response(200, {"id": "90000001"}))
    fake.add("GET", r"/v25\.0/90000001$", json_response(200, {"id": "90000001", "permalink": permalink,
                                                              "media_type": "CAROUSEL_ALBUM"}))


def test_full_carousel_flow(publish_kit):
    ai = True
    service, fake = _setup(publish_kit, me={"user_id": publish_kit.IG_ID, "username": "insia.kr",
                                            "account_type": "MEDIA_CREATOR"})  # a flat /me answer
    item_id, preview = _preview(publish_kit, service, ai=ai)
    _script_flow(fake, status_answers=[json_response(200, {"status_code": "IN_PROGRESS"}),
                                       json_response(200, {"status_code": "FINISHED"})])
    steps: list[str] = []
    attempt = service.send(HumanConfirmation(via="dashboard", requested_by="dashboard@t", preview_id=preview.preview_id,
                                             preview_hash=preview.preview_hash), background=False, on_step=steps.append)
    assert attempt.status == "published" and attempt.external_id == "90000001"
    assert attempt.permalink == "https://www.instagram.com/p/abc/"
    assert attempt.state["is_ai_generated"] is ai and attempt.state["carousel_id"] == "1007"
    assert attempt.state["children"] == [str(n) for n in range(1000, 1007)]
    posts = fake.calls("POST", MEDIA_POST)
    children, parent = posts[:-1], posts[-1]
    assert len(children) == 7
    for n, child in enumerate(children, start=1):
        assert set(child.form) == {"image_url", "is_carousel_item", "alt_text"}  # never caption / is_ai_generated
        assert re.fullmatch(rf"https://media\.example\.com/pub/m/[0-9a-f]{{32}}/{n:02d}\.jpg", child.form["image_url"])
    assert parent.form["media_type"] == "CAROUSEL" and parent.form["children"] == ",".join(attempt.state["children"])
    assert parent.form["is_ai_generated"] == "true"  # the parent only, and only because the person chose it
    assert parent.headers["authorization"] == "Bearer ***" and "access_token" not in parent.form
    assert fake.calls("POST", PUBLISH)[0].form == {"creation_id": "1007"}
    assert steps[:3] == ["check", "media", "children 1/7"] and "write" in steps and steps[-1] == "permalink"
    item = publish_kit.workspace.get_item(item_id).item
    assert item.status == "published" and item.published_via == "instagram_api" and item.published_external_id == "90000001"
    assert not (publish_kit.home / "publish" / "public").exists() or not any((publish_kit.home / "publish" / "public").iterdir())


def test_ai_label_false_is_never_sent(publish_kit):
    service, fake = _setup(publish_kit)
    fake.add("POST", MEDIA_POST, json_response(200, {"id": "1"}), json_response(200, {"id": "2"}))
    service.instagram.client.create_carousel("tok", publish_kit.IG_ID, ["a", "b"], "캡션", False)
    service.instagram.client.create_carousel("tok", publish_kit.IG_ID, ["a", "b"], "캡션", True)
    off, on = fake.calls("POST", MEDIA_POST)
    assert "is_ai_generated" not in off.form and on.form["is_ai_generated"] == "true"
    assert off.form == {"media_type": "CAROUSEL", "children": "a,b", "caption": "캡션"}


def test_account_type_is_case_insensitive_and_professional_only(publish_kit):
    for account_type in ("BUSINESS", "Business", "MEDIA_CREATOR", ""):
        fake = FakeTransport()
        service = publish_kit.service(transport=fake, instagram=True)
        fake.add("GET", ME, json_response(200, {"data": [{"user_id": "1784", "username": "a", "account_type": account_type}]}))
        fake.add("GET", r"/refresh_access_token$", json_response(400, {"error": {"code": 10, "message": "too new"}}))
        block = service.save_instagram_token("IGAA-pasted-token-123456")
        assert block["state"] in ("connected", "unavailable") and block["account"]["id_hint"] == "…784"
    fake = FakeTransport()
    service = publish_kit.service(transport=fake, instagram=True)
    fake.add("GET", ME, json_response(200, {"user_id": "1784", "username": "a", "account_type": "PERSONAL"}))
    with pytest.raises(AccountTypeError):
        service.save_instagram_token("IGAA-pasted-token-123456")
    fake.add("GET", ME, json_response(400, {"error": {"type": "OAuthException", "code": 190, "message": "bad"}}))
    with pytest.raises(InvalidTokenError):
        service.save_instagram_token("IGAA-pasted-token-123456")


def test_pasting_a_token_refreshes_at_once(publish_kit):
    fake = FakeTransport(redact=False)
    service = publish_kit.service(transport=fake, instagram=True)
    fake.add("GET", ME, json_response(200, {"data": [{"user_id": "1784", "username": "a", "account_type": "BUSINESS"}]}),
             repeat=True)
    fake.add("GET", r"/refresh_access_token$",
             json_response(200, {"access_token": "IGAA-refreshed-token-9876", "token_type": "bearer", "expires_in": 5183944}))
    block = service.save_instagram_token("IGAA-pasted-token-123456")
    refresh = fake.calls("GET", r"/refresh_access_token$")[0]
    assert refresh.params == {"grant_type": "ig_refresh_token", "access_token": "IGAA-pasted-token-123456"}
    assert block["token"]["estimated"] is False and block["token"]["days_left"] == 59
    assert service.store.get("instagram")["access_token"] == "IGAA-refreshed-token-9876"
    assert "IGAA" not in str(block)
    fake.add("GET", r"/refresh_access_token$", json_response(400, {"error": {"type": "OAuthException", "code": 190,
                                                                             "message": "too young"}}))
    block = service.save_instagram_token("IGAA-young-token-123456")
    assert block["token"]["estimated"] is True and block["token"]["days_left"] == 60
    assert service.store.get("instagram")["expires_estimated"] == "1"


def test_refresh_due_rules(publish_kit):
    now = publish_kit.clock.now()
    iso = lambda moment: moment.isoformat()  # noqa: E731
    estimated = {"access_token": "t", "issued_at": iso(now - timedelta(hours=23)), "expires_estimated": "1",
                 "expires_at": iso(now + timedelta(days=59))}
    assert not refresh_due(estimated, now)
    assert refresh_due({**estimated, "issued_at": iso(now - timedelta(hours=25))}, now)  # estimated: at once after 24 h
    exact = {"access_token": "t", "issued_at": iso(now - timedelta(days=40)), "expires_estimated": "0",
             "expires_at": iso(now + timedelta(days=20))}
    assert not refresh_due(exact, now)
    assert refresh_due({**exact, "expires_at": iso(now + timedelta(days=14))}, now)
    assert not refresh_due({**exact, "expires_at": iso(now + timedelta(days=14)), "refreshed_at": iso(now - timedelta(hours=2))},
                           now)
    assert not refresh_due({}, now)


def test_refresh_tokens_saves_atomically_and_auth_errors_need_a_reconnect(publish_kit):
    fake = FakeTransport()
    service = publish_kit.service(transport=fake, instagram=True)
    publish_kit.connect_instagram(service, issued_hours_ago=30, estimated=True)
    fake.add("GET", r"/refresh_access_token$", json_response(200, {"access_token": "IGAA-new-token-abcdef", "expires_in": 5184000}))
    result = service.refresh_tokens()["instagram"]
    values = service.store.get("instagram")
    assert result["refreshed"] and values["access_token"] == "IGAA-new-token-abcdef" and values["expires_estimated"] == "0"
    assert values["refreshed_at"] and result["expires_at"] == values["expires_at"]
    assert service.refresh_tokens()["instagram"]["refreshed"] is False  # not due again right after
    publish_kit.clock.advance(50 * 86400)
    auth = json_response(400, {"error": {"type": "OAuthException", "code": 190, "message": "expired"}})
    fake.add("GET", r"/refresh_access_token$", auth)
    fake.add("GET", ME, auth)
    service.refresh_tokens()
    assert service.readiness("instagram").state == "needs_reconnect"



def test_publish_refresh_keeps_a_stored_token_alive_while_the_instagram_switch_is_off(publish_kit):
    """Final review #7: the weekly ``insia publish refresh`` cron whose environment lacks INSIA_PUBLISH_INSTAGRAM=1
    used to log "꺼져 있어요" and exit 0 while the 60-day token ran out. The explicit command now refreshes a stored
    token anyway (a refresh never publishes); the server's 6-hour job and every other path keep the switch."""
    fake = FakeTransport()
    publish_kit.connect_instagram(publish_kit.service(transport=fake, instagram=True), issued_hours_ago=50 * 24, days=10)
    off = publish_kit.service(transport=fake)  # INSIA_PUBLISH_INSTAGRAM not set
    assert not off.settings.instagram_enabled
    before = off.store.get("instagram")["expires_at"]
    # the maintenance job (and previews/status) stay gated: nothing is sent while the switch is off
    assert off.refresh_tokens()["instagram"] == {"refreshed": False, "expires_at": "", "estimated": False,
                                                 "message": "인스타그램 API 게시가 꺼져 있어요."}
    assert fake.requests == []
    fake.add("GET", r"/refresh_access_token$", json_response(200, {"access_token": "IGAA-new-token-abcdef",
                                                                   "expires_in": 5184000}))
    result = off.refresh_tokens(stored_when_off=True)["instagram"]
    values = off.store.get("instagram")
    assert result["refreshed"] is True and values["access_token"] == "IGAA-new-token-abcdef"
    assert values["expires_at"] > before and result["expires_at"] == values["expires_at"]
    assert "인스타그램 API 게시가 꺼져 있어요" in result["message"] and "INSIA_PUBLISH_INSTAGRAM=1" in result["message"]
    # not due again: still reported, still no publish path
    again = off.refresh_tokens(stored_when_off=True)["instagram"]
    assert again["refreshed"] is False and "아직 갱신할 때가 아니에요" in again["message"]
    # no stored token: nothing to refresh, same message as before
    off.store.set_many("instagram", {"access_token": None})
    assert off.refresh_tokens(stored_when_off=True)["instagram"]["message"] == "인스타그램 API 게시가 꺼져 있어요."

def test_quota_is_read_from_the_answer_and_blocks_when_used_up(publish_kit):
    service, _ = _setup(publish_kit, quota=(100, 100))
    _, result = _preview(publish_kit, service)
    assert result.quota == {"used": 100, "total": 100} and [e.code for e in result.errors] == ["quota"]
    service, _ = _setup(publish_kit, quota=(3, 50))
    _, result = _preview(publish_kit, service)
    assert result.quota == {"used": 3, "total": 50} and any(n["message"] == "오늘 남은 게시 한도: 47/50개" for n in result.notices)


def _attempt(kit, carousel: str = "") -> PublishAttempt:
    state = {"carousel_id": carousel} if carousel else {}
    return PublishAttempt(id="pa_ig", item_id="it", version=1, platform="instagram", account_id=kit.IG_ID,
                          preview_id="pv_" + "0" * 24, state=state, created_at="2026-01-01T00:00:00Z")


def test_polling_backoff_and_five_minute_limit(publish_kit):
    service, fake = _setup(publish_kit)
    fake.add("GET", r"/v25\.0/42$", json_response(200, {"status_code": "IN_PROGRESS"}), repeat=True)
    guard = StubGuard()
    with pytest.raises(Exception) as caught:
        service.instagram._poll("tok", ["42"], guard, "polling")
    assert caught.value.outcome.status == "failed" and "5분" in caught.value.outcome.error
    assert publish_kit.clock.sleeps == [5, 15, 30, 60, 60, 60, 60] and sum(publish_kit.clock.sleeps) <= POLL_TOTAL_SECONDS
    assert len(fake.calls("GET", r"/42$")) == 8  # right away + after each wait


@pytest.mark.parametrize("status, expected", [("Error: 2207004", 2207004), ("ERROR 2207052: media fetch failed", 2207052),
                                              ("something broke", None), ("", None)])
def test_error_subcode_is_extracted_loosely(status, expected):
    assert status_subcode(status) == expected


def test_container_error_maps_the_subcode(publish_kit):
    service, fake = _setup(publish_kit)
    fake.add("GET", r"/v25\.0/43$", json_response(200, {"status_code": "ERROR", "status": "ERROR 2207052: nope"}))
    with pytest.raises(Exception) as caught:
        service.instagram._poll("tok", ["43"], StubGuard(), "polling")
    outcome = caught.value.outcome
    assert outcome.status == "failed" and outcome.error_code == "2207052" and "가져가지 못했어요" in outcome.error


def _publish_step(kit, service, fake):
    guard = StubGuard()
    outcome = service.instagram._publish_step(_attempt(kit), "tok", kit.IG_ID, "777", guard)
    return outcome, guard


def test_2207008_after_media_publish_retries_while_finished(publish_kit):
    not_ready = json_response(400, {"error": {"code": 24, "error_subcode": 2207008, "message": "not yet"}})
    service, fake = _setup(publish_kit)
    fake.add("POST", PUBLISH, not_ready, json_response(200, {"id": "91"}))
    fake.add("GET", r"/v25\.0/777$", json_response(200, {"status_code": "FINISHED"}), repeat=True)
    fake.add("GET", r"/v25\.0/91$", json_response(200, {"id": "91", "permalink": "https://www.instagram.com/p/x/"}))
    outcome, guard = _publish_step(publish_kit, service, fake)
    assert outcome.status == "published" and outcome.external_id == "91" and guard.claims == 2
    assert publish_kit.clock.sleeps == [30]
    service, fake = _setup(publish_kit)
    fake.add("POST", PUBLISH, not_ready, not_ready, not_ready)
    fake.add("GET", r"/v25\.0/777$", json_response(200, {"status_code": "FINISHED"}), repeat=True)
    outcome, guard = _publish_step(publish_kit, service, fake)
    assert outcome.status == "failed" and "새로 다시" in outcome.error and guard.claims == 3
    assert publish_kit.clock.sleeps == [30, 30, 60]


def test_lost_media_publish_answer_is_checked_once_and_never_resent(publish_kit):
    for container, expected in (("PUBLISHED", "published"), ("FINISHED", "unknown")):
        service, fake = _setup(publish_kit)
        fake.add("POST", PUBLISH, TransportError("timed out", sent="maybe"))
        fake.add("GET", r"/v25\.0/777$", json_response(200, {"status_code": container}))
        fake.add("GET", r"/17841400000000001/media$", json_response(200, {"data": [
            {"id": "92", "permalink": "https://www.instagram.com/p/y/", "timestamp": "2026-01-01T00:00:30+0000"},
            {"id": "80", "permalink": "https://www.instagram.com/p/old/", "timestamp": "2025-12-01T00:00:00+0000"}]}))
        outcome, guard = _publish_step(publish_kit, service, fake)
        assert outcome.status == expected and len(fake.calls("POST", PUBLISH)) == 1 and guard.claims == 1
        if expected == "published":
            assert outcome.external_id == "92" and outcome.permalink == "https://www.instagram.com/p/y/"


def test_reconcile_reads_only(publish_kit):
    service, fake = _setup(publish_kit)
    fake.add("GET", r"/17841400000000001/media$", json_response(200, {"data": [
        {"id": "93", "permalink": "https://www.instagram.com/p/z/", "timestamp": "2026-01-01T00:01:00+0000"}]}), repeat=True)
    for container, status in (("PUBLISHED", "published"), ("FINISHED", "failed"), ("IN_PROGRESS", "failed"),
                              ("EXPIRED", "failed"), ("ERROR", "failed")):
        fake.add("GET", r"/v25\.0/777$", json_response(200, {"status_code": container}))
        outcome = service.instagram.reconcile(_attempt(publish_kit, "777"))
        assert outcome is not None and outcome.status == status, container
        if status == "published":
            assert outcome.external_id == "93" and outcome.permalink == "https://www.instagram.com/p/z/"
    assert fake.calls("POST") == []  # never media_publish


def test_reconcile_lookup_failure_keeps_it_unknown(publish_kit):
    service, fake = _setup(publish_kit)
    fake.add("GET", r"/v25\.0/777$", TransportError("down", sent="no"))
    assert service.instagram.reconcile(_attempt(publish_kit, "777")) is None


def test_transient_container_errors_are_retried(publish_kit):
    service, fake = _setup(publish_kit)
    transient = json_response(500, {"error": {"code": -1, "error_subcode": 2207001, "message": "server"}})
    fake.add("POST", MEDIA_POST, transient, transient, json_response(200, {"id": "5"}))
    assert service.instagram._create(lambda: service.instagram.client.create_child("t", publish_kit.IG_ID, "https://m/x", ""),
                                     "children") == "5"
    assert publish_kit.clock.sleeps == [1, 3]
    fake.add("POST", MEDIA_POST, json_response(400, {"error": {"code": 4, "error_subcode": 2207051, "fbtrace_id": "AbC"}}))
    with pytest.raises(Exception) as caught:
        service.instagram._create(lambda: service.instagram.client.create_child("t", publish_kit.IG_ID, "u", ""), "children")
    assert "스팸" in caught.value.outcome.error and caught.value.outcome.state["fbtrace_id"] == "AbC"


def test_real_jpeg_render_is_baseline_1080x1350(rendered_jpegs):
    assert len(rendered_jpegs) == 2
    for data in rendered_jpegs:
        info = jpeg_info(data)
        assert (info.width, info.height) == (1080, 1350) and info.baseline and not info.has_mpf
        assert len(data) < 8_000_000


def test_render_pngs_still_renders_png(monkeypatch):
    from insia_agents.exporters import instagram

    calls = []
    monkeypatch.setattr(instagram, "_render_sync", lambda html, count, exe, *rest: calls.append(rest) or [b"x"] * count)
    monkeypatch.delenv("INSIA_RENDER", raising=False)
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("no playwright")
    assert instagram.render_pngs("<html></html>", 2) == [b"x", b"x"]
    assert instagram.render_images("<html></html>", 1, image_type="jpeg", quality=80) == [b"x"]
    assert calls == [(), ("jpeg", 80)]
    with pytest.raises(ValueError):
        instagram.render_images("<html></html>", 1, image_type="webp")


def test_ig_response_shapes_via_first_record():
    from insia_agents.publishers import first_record

    assert first_record({"data": [{"a": 1}, {"a": 2}]}) == {"a": 1}
    assert first_record({"a": 1}) == {"a": 1} and first_record({"data": []}) == {"data": []}


def test_unknown_account_after_quota_auth_error_fails_cleanly(publish_kit):
    fake = FakeTransport()
    service = publish_kit.service(transport=fake, instagram=True)
    publish_kit.connect_instagram(service)
    fake.add("GET", QUOTA, json_response(401, {"error": {"type": "OAuthException", "code": 190}}))
    outcome = service.instagram.send(_attempt(publish_kit), {"options": {"is_ai_generated": False},
                                                             "instagram": {"caption": "c", "slides": []}}, StubGuard())
    assert outcome.status == "failed" and outcome.error_code == "reconnect"
    assert fake.calls("POST") == []


def test_platform_limits_match_the_channel_rules():
    """DESIGN.md 9: publisher constants (platform limits) and channels.py (INSIA rules) must move together."""
    from insia_agents.channels import CHANNELS
    from insia_agents.publishers.instagram import IG_MAX_CAPTION, IG_MAX_HASHTAGS
    from insia_agents.publishers.linkedin import LINKEDIN_MAX_CHARS

    assert CHANNELS["linkedin"].limits["hard_max_chars"] == LINKEDIN_MAX_CHARS == 3000
    assert CHANNELS["instagram"].limits["max_caption_chars"] == IG_MAX_CAPTION == 2200
    assert CHANNELS["instagram"].limits["max_hashtags"] == IG_MAX_HASHTAGS == 5
