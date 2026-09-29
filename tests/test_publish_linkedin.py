"""LinkedIn publishing (package A): little-text transform, validation, the exact request, the error table (DESIGN.md
4-1, 14.3; LI §5, §8, §10). Offline: every request goes to a scripted FakeTransport."""

from __future__ import annotations

import pytest

from insia_agents.models import PublishAttempt
from insia_agents.publishers import FakeTransport, HumanConfirmation, Response, TransportError, json_response
from insia_agents.publishers.linkedin import (
    LITTLE_RESERVED,
    POSTS_URL,
    build_commentary,
    little_escape,
    split_text,
    sunset_warning,
    version_sunset,
    visible_text,
)

pytestmark = pytest.mark.usefixtures("no_network")


class StubGuard:
    attempt_id = "pa_test"

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.claims = 0

    def step(self, step, state_patch=None):
        self.steps.append(step)

    def claim_write(self, step="write"):
        self.claims += 1


def _attempt(kit) -> PublishAttempt:
    return PublishAttempt(id="pa_test", item_id="it_x", version=1, platform="linkedin", account_id=kit.LI_SUB)


def _payload(commentary="안녕하세요", visibility="PUBLIC") -> dict:
    return {"schema": 1, "platform": "linkedin", "options": {"visibility": visibility},
            "linkedin": {"commentary": commentary, "text": commentary}}


def test_little_escape_every_reserved_character_and_keeps_korean():
    assert LITTLE_RESERVED == frozenset("|{}@[]()<>#\\*_~") and len(LITTLE_RESERVED) == 15
    for ch in LITTLE_RESERVED:
        assert little_escape(f"가{ch}나") == f"가\\{ch}나"
    assert little_escape("(출처: 통계청) [확인 필요: ○○] ~*_ a\\b") == "\\(출처: 통계청\\) \\[확인 필요: ○○\\] \\~\\*\\_ a\\\\b"
    assert little_escape("평범한 한국어 문장, 숫자 30% 그대로!") == "평범한 한국어 문장, 숫자 30% 그대로!"


def test_hashtags_plain_by_default_and_template_mode():
    body, tags, had_bold = split_text("**굵은** 첫 줄 (괄호)\n\n둘째 줄\n\n#창업 #AI_마케팅 #1인기업\n")
    assert body == "굵은 첫 줄 (괄호)\n\n둘째 줄" and tags == ["창업", "AI_마케팅", "1인기업"] and had_bold
    assert visible_text(body, tags).endswith("\n\n#창업 #AI_마케팅 #1인기업")
    plain = build_commentary(body, tags)  # DESIGN.md 14.3: '#' stays hashtag syntax, the word is escaped
    assert plain == "굵은 첫 줄 \\(괄호\\)\n\n둘째 줄\n\n#창업 #AI\\_마케팅 #1인기업"
    template = build_commentary(body, ["창업"], "template")
    assert template.endswith("\n\n{hashtag|\\#|창업}")


def test_version_sunset_table_and_warning():
    from datetime import date

    assert version_sunset("202609") == "2027-09-15" and version_sunset("202612") == "2027-12-15"
    assert sunset_warning("202609", date(2027, 6, 1)) == ""
    assert "2027-09-15" in sunset_warning("202609", date(2027, 8, 1))


def _connected(kit, transport=None):
    fake = transport or FakeTransport()
    service = kit.service(transport=fake)
    kit.connect_linkedin(service)
    fake.add("GET", r"/v2/userinfo$", json_response(200, {"sub": kit.LI_SUB, "name": "홍길동"}), repeat=True)
    return service, fake


def test_3000_character_boundary(publish_kit):
    service, _ = _connected(publish_kit)
    tags_line = "#창업 #마케팅 #1인기업"
    for extra, ok in ((0, True), (1, True), (2, False)):
        body = "가" * (3000 - len("\n\n" + tags_line) - 1 + extra)  # visible length 2,999 / 3,000 / 3,001
        result = service.preview(publish_kit.item(content=body), via="dashboard", requested_by="dashboard@t")
        assert result.content["chars"] == 2999 + extra
        assert result.can_publish is ok
        assert any(e.code == "too_long" for e in result.errors) is (not ok)


def test_placeholder_blocks_and_format_checks_warn(publish_kit):
    service, _ = _connected(publish_kit)
    item_id = publish_kit.item(content="대표 경험: [대표 경험: ○○]을 적어요. https://example.com 도 있어요.")
    result = service.preview(item_id, via="dashboard", requested_by="dashboard@t")
    assert not result.can_publish and [e.code for e in result.errors] == ["placeholder"]
    codes = {w.code for w in result.warnings}
    assert "forced_approval" in codes and "format_no_link" in codes
    assert result.to_json()["can_publish"] is False and "confirm_code" not in result.to_json()


def test_request_body_headers_and_case_insensitive_restli_id(publish_kit):
    service, fake = _connected(publish_kit, FakeTransport(redact=False))
    item_id = publish_kit.item()
    preview = service.preview(item_id, options={"visibility": "CONNECTIONS"}, via="dashboard", requested_by="dashboard@t")
    assert preview.request_preview[0]["body"]["author"] == "urn:li:person:…taQ"  # the id is masked in the preview
    fake.add("POST", r"/rest/posts$", Response(201, {"X-RestLi-Id": "urn:li:share:7243000000000000000"}))
    attempt = service.send(HumanConfirmation(via="dashboard", requested_by="dashboard@t", preview_id=preview.preview_id,
                                             preview_hash=preview.preview_hash), background=False)
    post = fake.calls("POST", r"/rest/posts$")[0]
    assert post.url == POSTS_URL
    assert post.headers["linkedin-version"] == "202609" and post.headers["x-restli-protocol-version"] == "2.0.0"
    assert post.headers["authorization"] == f"Bearer {publish_kit.LI_TOKEN}"
    assert post.json_body == {
        "author": "urn:li:person:782bbtaQ", "commentary": preview.content["text"].replace("(", "\\(").replace(")", "\\)"),
        "visibility": "CONNECTIONS",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    assert attempt.status == "published" and attempt.external_id == "urn:li:share:7243000000000000000"
    assert attempt.permalink == "https://www.linkedin.com/feed/update/urn:li:share:7243000000000000000/"


def _run(kit, *answers):
    service, fake = _connected(kit)
    for answer in answers:
        fake.add("POST", r"/rest/posts$", answer)
    guard = StubGuard()
    outcome = service.linkedin.send(_attempt(kit), _payload("본문 \\(괄호\\)"), guard)
    return outcome, guard, fake, service


def test_201_without_restli_id_is_published_without_an_id(publish_kit):
    outcome, guard, _, _ = _run(publish_kit, Response(201, {}, b""))
    assert outcome.status == "published" and outcome.external_id == "" and outcome.permalink == ""
    assert outcome.state.get("permalink_missing") is True and guard.claims == 1 and guard.steps == ["check"]


def test_409_is_retried_once_with_a_new_claim(publish_kit):
    conflict = json_response(409, {"code": "CONFLICT", "message": "Retry the request."})
    outcome, guard, fake, _ = _run(publish_kit, conflict, Response(201, {"x-restli-id": "urn:li:ugcPost:55"}))
    assert outcome.status == "published" and outcome.external_id == "urn:li:ugcPost:55" and guard.claims == 2
    outcome, guard, fake, _ = _run(publish_kit, conflict, conflict)
    assert outcome.status == "failed" and "충돌" in outcome.error and guard.claims == 2
    assert len(fake.calls("POST")) == 2


ERROR_TABLE = [
    (json_response(422, {"message": "Content is a DUPLICATE of urn:li:share:6844785523593134080"}), "published", ""),
    (json_response(422, {"message": "Content is a duplicate of urn:li:person:782bbtaQ"}), "failed", "받지 않았어요"),
    (json_response(422, {"code": "UNPROCESSABLE_ENTITY", "message": "Something is wrong"}), "failed", "UNPROCESSABLE_ENTITY"),
    (json_response(400, {"code": "FIELD_LENGTH_TOO_LONG"}), "failed", "한도를 넘었어요"),
    (json_response(400, {"code": "VERSION_MISSING"}), "failed", "INSIA 오류"),
    (json_response(400, {"code": "INVALID_VALUE_FOR_FIELD"}), "failed", "INVALID_VALUE_FOR_FIELD"),
    (json_response(403, {"code": "ACCESS_DENIED"}), "failed", "Share on LinkedIn"),
    (json_response(426, {"code": "NONEXISTENT_VERSION"}), "failed", "202609"),
    (json_response(429, {"code": "TOO_MANY_REQUESTS"}), "failed", "오전 9시"),
    (json_response(500, {}), "unknown", "올라갔을 수도"),
    (json_response(503, {}), "unknown", "올라갔을 수도"),
    (TransportError("timed out", sent="maybe"), "unknown", "올라갔을 수도"),
    (TransportError("refused", sent="no"), "failed", "인터넷 연결"),
]


def test_error_table(publish_kit):
    service, fake = _connected(publish_kit)
    for answer, status, needle in ERROR_TABLE:
        fake.add("POST", r"/rest/posts$", answer)
        before = len(fake.calls("POST"))
        guard = StubGuard()
        outcome = service.linkedin.send(_attempt(publish_kit), _payload("본문 \\(괄호\\)"), guard)
        assert outcome.status == status, (answer, outcome)
        assert needle in outcome.error
        assert guard.claims == 1 and len(fake.calls("POST")) == before + 1  # never retried (only 409 is)
        if status == "published":
            assert outcome.external_id == "urn:li:share:6844785523593134080" and outcome.state.get("duplicate") is True


def test_401_fails_and_asks_for_a_reconnect(publish_kit):
    outcome, _, _, service = _run(publish_kit, json_response(401, {"code": "EMPTY_ACCESS_TOKEN"}))
    assert outcome.status == "failed" and "다시 연결" in outcome.error
    assert service.readiness("linkedin").state == "needs_reconnect"
    assert publish_kit.workspace.get_publish_connection("linkedin").status == "needs_reconnect"


def test_account_changed_before_sending_sends_nothing(publish_kit):
    service, fake = _connected(publish_kit)
    publish_kit.connect_linkedin(service, sub="someoneElse")  # reconnected as another member meanwhile
    outcome = service.linkedin.send(_attempt(publish_kit), _payload(), StubGuard())
    assert outcome.status == "failed" and outcome.error_code == "account_changed" and fake.calls("POST") == []


def test_reconcile_is_not_possible_for_linkedin(publish_kit):
    service, _ = _connected(publish_kit)
    assert service.linkedin.reconcile(_attempt(publish_kit), StubGuard()) is None
