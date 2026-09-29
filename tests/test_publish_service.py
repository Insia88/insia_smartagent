"""PublishService (package A): previews, confirmed sends, ownership/heartbeat/recovery, outcomes, resolve,
confirm codes and the fake mode (DESIGN.md 1-7, 4-0, 5-3, 5-5, 8-2, 12-1). Offline and without real waiting."""

from __future__ import annotations

import tempfile
import threading
import time
from datetime import timedelta

import pytest

from insia_agents.db import AttemptTakenOverError, ItemLockedError, NotFoundError, _parse_ts
from insia_agents.models import Draft
from insia_agents.publishers import (
    AlreadyPublishedError,
    AttemptStateError,
    ConfirmationMismatchError,
    ConfirmCodeError,
    FakeTransport,
    HumanConfirmation,
    InvalidInputError,
    NotPublishableError,
    PreviewExpiredError,
    PublishDisabledError,
    PublisherBusyError,
    PublishService,
    PublishSettings,
    Response,
    json_response,
)
from insia_agents.publishers import service as service_module
from insia_agents.publishers.base import confirm_code_hash

pytestmark = pytest.mark.usefixtures("no_network")

POSTS = r"/rest/posts$"


def confirm(preview, via="dashboard", code=""):
    return HumanConfirmation(via=via, requested_by="dashboard@127.0.0.1" if via == "dashboard" else "cli:me@host",
                             preview_id=preview.preview_id, preview_hash=preview.preview_hash, confirm_code=code)


def linkedin(kit, fake=None, **kwargs):
    fake = fake or FakeTransport()
    service = kit.service(transport=fake, **kwargs)
    kit.connect_linkedin(service)
    fake.add("GET", r"/v2/userinfo$", json_response(200, {"sub": kit.LI_SUB, "name": "홍길동"}), repeat=True)
    return service, fake


def created(urn="urn:li:share:1"):
    return Response(201, {"x-restli-id": urn})


# ---------------------------------------------------------------------------
# previews
# ---------------------------------------------------------------------------


def test_preview_refuses_items_that_are_not_the_approved_latest_version(publish_kit):
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    draft_id = publish_kit.item(approve=False)
    with pytest.raises(NotPublishableError) as caught:
        service.preview(draft_id, via="dashboard", requested_by="d")
    assert caught.value.blocked_by == "not_approved"
    changed = publish_kit.item()
    ws.add_version(changed, Draft(channel="linkedin", round=1, title="t", content="고친 본문"), source="human")
    with pytest.raises(NotPublishableError) as caught:
        service.preview(changed, via="dashboard", requested_by="d")
    assert caught.value.blocked_by == "not_approved"  # a new version drops the approval
    archived = publish_kit.item()
    ws.set_item_status(archived, "archived")
    with pytest.raises(NotPublishableError) as caught:
        service.preview(archived, via="dashboard", requested_by="d")
    assert caught.value.blocked_by == "archived"
    done = publish_kit.item()
    ws.set_item_status(done, "published")
    with pytest.raises(NotPublishableError) as caught:
        service.preview(done, via="dashboard", requested_by="d")
    assert caught.value.blocked_by == "published" and caught.value.http_status == 409
    blog = publish_kit.workspace.create_item("naver_blog", "블로그")
    with pytest.raises(NotPublishableError):
        service.preview(blog.id, via="dashboard", requested_by="d")
    with pytest.raises(NotFoundError):
        service.preview("it_nope", via="dashboard", requested_by="d")
    assert fake.calls("POST") == []


def test_expired_reused_forged_and_wrong_surface_previews_send_nothing(publish_kit):
    service, fake = linkedin(publish_kit)
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    forged = HumanConfirmation(via="dashboard", requested_by="d", preview_id=preview.preview_id,
                               preview_hash="sha256:" + "0" * 64)
    with pytest.raises(ConfirmationMismatchError):
        service.send(forged, background=False)
    with pytest.raises(ConfirmationMismatchError):  # a dashboard preview cannot go through the CLI
        service.send(confirm(preview, via="cli", code="AAAAAA"), background=False)
    with pytest.raises(ConfirmationMismatchError):
        service.send(confirm(preview), item_id="it_other", background=False)
    publish_kit.clock.advance(1801)
    with pytest.raises(PreviewExpiredError):
        service.send(confirm(preview), background=False)
    cli_preview = service.preview(item_id, via="cli", requested_by="cli:me@host", issue_confirm_code=True)
    with pytest.raises(ConfirmationMismatchError):  # a CLI preview cannot go through the dashboard path
        service.send(confirm(cli_preview), background=False)
    assert fake.calls("POST") == [] and publish_kit.workspace.list_publish_attempts() == []
    fresh = service.preview(item_id, via="dashboard", requested_by="d")
    fake.add("POST", POSTS, created())
    assert service.send(confirm(fresh), background=False).status == "published"
    with pytest.raises((PreviewExpiredError, AlreadyPublishedError)):
        service.send(confirm(fresh), background=False)
    assert len(fake.calls("POST")) == 1


def test_a_new_approved_version_after_the_preview_is_refused(publish_kit):
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    ws.add_version(item_id, Draft(channel="linkedin", round=1, title="t", content="고친 본문"), source="human")
    ws.set_item_status(item_id, "approved", force=True)  # v2 approved: the confirmed preview showed v1
    with pytest.raises(NotPublishableError) as caught:
        service.send(confirm(preview), background=False)
    assert caught.value.blocked_by == "version_changed" and fake.calls("POST") == []


def test_account_changed_between_preview_and_send(publish_kit):
    service, fake = linkedin(publish_kit)
    preview = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    publish_kit.connect_linkedin(service, sub="anotherMember")
    with pytest.raises(ConfirmationMismatchError):
        service.send(confirm(preview), background=False)
    assert fake.calls("POST") == []


def test_secrets_account_changing_right_before_sending_sends_nothing(publish_kit):
    service, fake = linkedin(publish_kit)
    preview = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    real = service.linkedin_token
    service.linkedin_token = lambda: (real()[0], "switchedMember")  # reconnected in another process meanwhile
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "failed" and attempt.error_code == "account_changed" and fake.calls("POST") == []


def test_success_updates_the_five_item_fields_and_failure_leaves_the_item(publish_kit):
    service, fake = linkedin(publish_kit)
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    fake.add("POST", POSTS, json_response(403, {"code": "ACCESS_DENIED"}))
    failed = service.send(confirm(preview), background=False)
    item = publish_kit.workspace.get_item(item_id).item
    assert failed.status == "failed" and item.status == "approved" and item.published_via == ""
    preview = service.preview(item_id, via="dashboard", requested_by="d")  # "다시 시도" = a new preview
    fake.add("POST", POSTS, created("urn:li:share:77"))
    attempt = service.send(confirm(preview), background=False)
    item = publish_kit.workspace.get_item(item_id).item
    assert attempt.status == "published"
    assert (item.status, item.published_via, item.published_external_id) == ("published", "linkedin_api", "urn:li:share:77")
    assert item.published_url == "https://www.linkedin.com/feed/update/urn:li:share:77/" and item.published_at
    with pytest.raises(NotPublishableError):
        service.preview(item_id, via="dashboard", requested_by="d")


def test_a_failed_item_update_never_loses_the_success(publish_kit, monkeypatch):
    service, fake = linkedin(publish_kit)
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    fake.add("POST", POSTS, created("urn:li:share:88"))

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(publish_kit.workspace, "_mark_item_published_via_api", broken)
    attempt = service.send(confirm(preview), background=False)
    item = publish_kit.workspace.get_item(item_id).item
    assert attempt.status == "published" and attempt.external_id == "urn:li:share:88"
    assert "게시 완료 표시" in attempt.state["item_update_error"] and item.status == "approved"
    assert service.attempt_json(attempt)["item_update_error"]
    assert service.item_block(item)["blocked_by"] == "published_attempt"
    with pytest.raises(AlreadyPublishedError):
        service.preview(item_id, via="dashboard", requested_by="d")


# ---------------------------------------------------------------------------
# concurrency and ownership
# ---------------------------------------------------------------------------


def test_two_concurrent_sends_make_one_attempt(publish_kit):
    fake = FakeTransport()
    fake.add("POST", POSTS, created("urn:li:share:9"))
    one, _ = linkedin(publish_kit, fake)
    two = publish_kit.service(transport=fake)  # a second process on the same workspace
    item_id = publish_kit.item()
    previews = [one.preview(item_id, via="dashboard", requested_by="d"), two.preview(item_id, via="dashboard", requested_by="d")]
    barrier = threading.Barrier(2)
    results: list[object] = []

    def go(service, preview):
        barrier.wait()
        try:
            results.append(service.send(confirm(preview), background=False))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=go, args=pair) for pair in zip((one, two), previews)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(results) == 2 and len(errors) == 1 and isinstance(errors[0], (ItemLockedError, AlreadyPublishedError))
    assert len(publish_kit.workspace.list_publish_attempts(item_id=item_id)) == 1 and len(fake.calls("POST")) == 1


def test_recovered_attempt_makes_the_original_worker_send_nothing(publish_kit):
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    preview = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    real = service.linkedin_token

    def taken_over():
        attempt = ws.list_publish_attempts()[0]
        ws.release_publish_worker(attempt.id)  # as seen from another process: the worker looks gone
        assert ws.recover_publish_attempts(now=publish_kit.clock.now() + timedelta(seconds=200)) == [attempt.id]
        return real()

    service.linkedin_token = taken_over
    attempt = service.send(confirm(preview), background=False)
    assert fake.calls("POST") == []  # claim_write failed → nothing sent
    assert attempt.status == "failed" and attempt.error_code == "interrupted"


def test_a_late_answer_after_a_takeover_is_still_recorded(publish_kit):
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    preview = service.preview(publish_kit.item(), via="dashboard", requested_by="d")

    def answer(request):
        attempt = ws.list_publish_attempts()[0]
        ws.release_publish_worker(attempt.id)
        ws.recover_publish_attempts(now=publish_kit.clock.now() + timedelta(seconds=200))  # write step → unknown
        return created("urn:li:share:99")

    fake.add("POST", POSTS, answer)
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "published" and attempt.external_id == "urn:li:share:99"
    assert attempt.resolved_by == "system:late_answer"


@pytest.mark.parametrize("resolved_as", ["not_published", "published"])
def test_a_late_answer_after_a_person_resolved_the_attempt_is_not_dropped(publish_kit, caplog, resolved_as):
    """Stalled for more than 180 s after the POST: recovery → unknown, a person resolves it, then the 201 arrives."""
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")

    def answer(request):
        attempt = ws.list_publish_attempts()[0]
        ws.release_publish_worker(attempt.id)
        ws.recover_publish_attempts(now=publish_kit.clock.now() + timedelta(seconds=200))
        ws.resolve_publish_attempt(attempt.id, resolved_as, resolved_by="dashboard@127.0.0.1", via="linkedin_api")
        return created("urn:li:share:77")

    fake.add("POST", POSTS, answer)
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "published" and attempt.external_id == "urn:li:share:77"
    assert attempt.state["late_success"]["status_before"] == ("abandoned" if resolved_as == "not_published" else "published")
    item = ws.get_item(item_id).item
    assert item.status == "published" and item.published_via == "linkedin_api"
    assert item.published_external_id == "urn:li:share:77"
    with pytest.raises((ItemLockedError, AlreadyPublishedError, NotPublishableError)):  # no second post of this version
        service.send(confirm(service.preview(item_id, via="dashboard", requested_by="d")), background=False)
    if resolved_as == "not_published":  # the person's decision was overridden: that is logged
        assert attempt.resolved_by == "system:late_answer"
        assert any(r.levelname == "WARNING" and attempt.id in r.getMessage() and "urn:li:share:77" in r.getMessage()
                   for r in caplog.records)
    else:
        assert attempt.resolved_by == "dashboard@127.0.0.1"
    assert len(fake.calls("POST")) == 1


def test_a_late_answer_blocked_by_a_newer_attempt_is_kept_and_logged(publish_kit, caplog):
    """Resolved as "not published" and already sent again: the old attempt cannot become published (the new one holds
    the version), so it keeps the post id and tells the person to check for a duplicate."""
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    newer = {}

    def answer(request):
        attempt = ws.list_publish_attempts()[0]
        ws.release_publish_worker(attempt.id)
        ws.recover_publish_attempts(now=publish_kit.clock.now() + timedelta(seconds=200))
        ws.resolve_publish_attempt(attempt.id, "not_published", resolved_by="dashboard@127.0.0.1")
        again = service.preview(item_id, via="dashboard", requested_by="d")
        newer["attempt"], _ = ws.begin_publish_attempt(again.preview_id, again.preview_hash, via="dashboard",
                                                       requested_by="d", now=publish_kit.clock.now())
        return created("urn:li:share:78")

    fake.add("POST", POSTS, answer)
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "abandoned" and "두 번 올라갔는지" in attempt.error
    late = attempt.state["late_success"]
    assert (late["external_id"], late["status_before"], late["resolved_by_before"]) == \
        ("urn:li:share:78", "abandoned", "dashboard@127.0.0.1")
    assert ws.get_publish_attempt(newer["attempt"].id).status == "sending"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(attempt.id in m and "urn:li:share:78" in m and "중복" in m for m in warnings)


def _begin(kit, service):
    preview = service.preview(kit.item(), via="dashboard", requested_by="d")
    attempt, token = kit.workspace.begin_publish_attempt(preview.preview_id, preview.preview_hash, via="dashboard",
                                                          requested_by="d", now=kit.clock.now())
    return attempt, token


def test_recovery_by_step_and_heartbeat(publish_kit):
    service, _ = linkedin(publish_kit)
    ws = publish_kit.workspace
    early, _ = _begin(publish_kit, service)
    writing, token = _begin(publish_kit, service)
    ws.claim_publish_write(writing.id, token, now=publish_kit.clock.now())
    now = publish_kit.clock.now()
    assert ws.recover_publish_attempts(now=now) == []  # this process's workers hold them
    for attempt in (early, writing):
        ws.release_publish_worker(attempt.id)
    with ws.transaction() as conn:  # owned by another host: only the heartbeat decides
        conn.execute("UPDATE publish_attempts SET owner_host = 'other-host'")
    assert ws.recover_publish_attempts(now=now + timedelta(seconds=179)) == []
    closed = ws.recover_publish_attempts(now=now + timedelta(seconds=181))
    assert sorted(closed) == sorted([early.id, writing.id])
    assert ws.get_publish_attempt(early.id).status == "failed"
    assert ws.get_publish_attempt(writing.id).status == "unknown"  # the post may exist
    with pytest.raises(ItemLockedError):  # an unknown attempt keeps the item frozen
        ws.set_item_status(writing.item_id, "published")


def _heartbeat_at(ws, attempt_id):
    with ws.transaction() as conn:
        return _parse_ts(conn.execute("SELECT heartbeat_at FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()[0])


def _wait_for_beat(ws, attempt_id, moment, *, seconds=3.0):
    """Real-time wait until the heartbeat thread has written ``moment`` (the fake clock does not move by itself)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        beat = _heartbeat_at(ws, attempt_id)
        if beat is not None and beat >= moment:
            return True
        time.sleep(0.005)
    return False


def _as_another_host(ws, attempt_id):
    ws.release_publish_worker(attempt_id)  # as seen from another process on another host: only time decides
    with ws.transaction() as conn:
        conn.execute("UPDATE publish_attempts SET owner_host = 'other-host' WHERE id = ?", (attempt_id,))


@pytest.mark.parametrize("beating", [True, False])
def test_the_heartbeat_thread_alone_keeps_a_blocked_send_alive(publish_kit, monkeypatch, beating):
    """The worker hangs inside one call for 200 s (no ``step`` refreshes the attempt): only the heartbeat thread
    keeps it from looking gone. Without the thread (the control) recovery takes the attempt."""
    if not beating:
        monkeypatch.setattr(service_module._Heartbeat, "start", lambda self: None)
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    preview = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    seen = {}

    def hanging_post(request):
        publish_kit.clock.advance(200)  # past PUBLISH_STALE_AFTER_SECONDS (180 s)
        attempt = ws.list_publish_attempts()[0]
        seen["beat"] = _wait_for_beat(ws, attempt.id, publish_kit.clock.now(), seconds=3.0 if beating else 0.0)
        _as_another_host(ws, attempt.id)
        seen["recovered"] = ws.recover_publish_attempts(now=publish_kit.clock.now())
        return created("urn:li:share:5")

    fake.add("POST", POSTS, hanging_post)
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "published" and attempt.external_id == "urn:li:share:5"
    if beating:
        assert seen == {"beat": True, "recovered": []} and attempt.resolved_by == ""
    else:
        assert seen == {"beat": False, "recovered": [attempt.id]}
        assert attempt.resolved_by == "system:late_answer"  # recovered as unknown, then the late 201 was recorded


def test_a_failed_heartbeat_stops_the_thread_and_the_worker(publish_kit):
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    attempt, token = _begin(publish_kit, service)
    guard = service_module._WorkerGuard(service, attempt.id, token)
    beat = service_module._Heartbeat(service, guard)
    publish_kit.clock.advance(100)
    beat.start()
    try:
        assert _wait_for_beat(ws, attempt.id, publish_kit.clock.now())  # it beats on its own, every interval
        _as_another_host(ws, attempt.id)
        # seen from a day later the last beat is stale: recovery closes the attempt and clears the owner token
        assert ws.recover_publish_attempts(now=publish_kit.clock.now() + timedelta(days=1)) == [attempt.id]
        beat.thread.join(2)
        assert not beat.thread.is_alive() and guard.lost.is_set()  # a failed beat ends the thread and marks the loss
    finally:
        beat.stop()
    with pytest.raises(AttemptTakenOverError):
        guard.claim_write()  # the worker stops at its next step and sends nothing
    assert fake.calls("POST") == [] and ws.get_publish_attempt(attempt.id).status == "failed"


def test_a_long_instagram_poll_keeps_the_attempt_alive(publish_kit):
    """~110 s of Instagram processing: every poll round records its step, which refreshes the attempt too."""
    fake = FakeTransport()
    service = publish_kit.service(transport=fake, instagram=True)
    publish_kit.connect_instagram(service)
    ws = publish_kit.workspace
    fake.add("GET", r"/v25\.0/me$", json_response(200, {"user_id": publish_kit.IG_ID, "username": "a"}), repeat=True)
    fake.add("GET", r"/content_publishing_limit$", json_response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}),
             repeat=True)
    item_id = publish_kit.item("instagram")
    preview = service.preview(item_id, options={"is_ai_generated": False}, via="dashboard", requested_by="d")
    ids = iter(range(1000, 1100))
    fake.add("POST", r"/media$", lambda r: json_response(200, {"id": str(next(ids))}), repeat=True)
    polls = {"n": 0, "recovered": None}

    def status(request):
        polls["n"] += 1
        if polls["n"] == 6 * 7:  # six rounds in: ~110 s of Instagram processing
            attempt = ws.list_publish_attempts()[0]
            ws.release_publish_worker(attempt.id)
            with ws.transaction() as conn:
                conn.execute("UPDATE publish_attempts SET owner_host = 'other-host'")
            polls["recovered"] = ws.recover_publish_attempts(now=publish_kit.clock.now())
        done = polls["n"] > 7 * 7
        return json_response(200, {"status_code": "FINISHED" if done else "IN_PROGRESS"})

    fake.add("GET", r"/v25\.0/1\d{3}$", status, repeat=True)
    fake.add("POST", r"/media_publish$", json_response(200, {"id": "5"}))
    fake.add("GET", r"/v25\.0/5$", json_response(200, {"id": "5", "permalink": "https://www.instagram.com/p/q/"}))
    attempt = service.send(confirm(preview), background=False)
    assert polls["recovered"] == [] and attempt.status == "published"
    assert sum(publish_kit.clock.sleeps) >= 110


def test_one_worker_per_platform_and_background_sends(publish_kit):
    service, fake = linkedin(publish_kit)
    release = threading.Event()

    def slow(request):
        release.wait(5)
        return created("urn:li:share:10")

    fake.add("POST", POSTS, slow)
    first = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    second = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    attempt = service.send(confirm(first), background=True)
    assert attempt.status == "sending"
    with pytest.raises(PublisherBusyError):
        service.send(confirm(second), background=True)
    release.set()
    service.shutdown(timeout=5)
    assert service.get_attempt(attempt.id).status == "published"
    assert service.attempt_json(service.get_attempt(attempt.id))["progress"] == {"done": 1, "total": 1}


@pytest.mark.parametrize("where, status", [("before", "failed"), ("after", "unknown")])
def test_ctrl_c_closes_the_attempt(publish_kit, where, status):
    service, fake = linkedin(publish_kit)
    preview = service.preview(publish_kit.item(), via="cli", requested_by="cli:me@host", issue_confirm_code=True)
    if where == "before":
        def interrupted():
            raise KeyboardInterrupt
        service.linkedin_token = interrupted
    else:
        def cut(request):
            raise KeyboardInterrupt
        fake.add("POST", POSTS, cut)
    with pytest.raises(KeyboardInterrupt):
        service.send(confirm(preview, via="cli", code=preview.confirm_code), background=False)
    attempt = publish_kit.workspace.list_publish_attempts()[0]
    assert attempt.status == status and attempt.error_code == "interrupted"


# ---------------------------------------------------------------------------
# human confirmation, confirm codes, resolve
# ---------------------------------------------------------------------------


def test_send_and_resolve_need_a_human_confirmation(publish_kit):
    service, fake = linkedin(publish_kit)
    preview = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    with pytest.raises(TypeError):
        service.send({"preview_id": preview.preview_id, "preview_hash": preview.preview_hash})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        service.resolve(None, "published")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HumanConfirmation(via="dashboard", requested_by="d", preview_id="pv", preview_hash="h", confirm_code="ABC")
    assert fake.calls("POST") == []


def test_confirm_codes_are_random_hashed_and_single_try(publish_kit):
    service, fake = linkedin(publish_kit)
    item_id = publish_kit.item()
    one = service.preview(item_id, via="cli", requested_by="cli:me@host", issue_confirm_code=True)
    two = service.preview(item_id, via="cli", requested_by="cli:me@host", issue_confirm_code=True)
    assert one.confirm_code and two.confirm_code and one.confirm_code != two.confirm_code
    assert one.preview_hash == two.preview_hash  # same content, different codes
    assert "confirm_code" not in one.to_json() and one.confirm_code not in repr(one)
    plain = service.preview(item_id, via="cli", requested_by="cli:me@host")
    assert plain.confirm_code is None
    raw = publish_kit.workspace.db_path.read_bytes() + (publish_kit.home / "insia.db-wal").read_bytes()
    assert one.confirm_code.encode() not in raw and confirm_code_hash(one.confirm_code).encode() in raw
    with pytest.raises(ConfirmCodeError) as caught:
        service.send(confirm(one, via="cli", code="WRONG1"), background=False)
    assert caught.value.exit_code == 2
    with pytest.raises(PreviewExpiredError):  # one try: the preview is burnt
        service.send(confirm(one, via="cli", code=one.confirm_code), background=False)
    with pytest.raises(ConfirmCodeError):  # `insia publish preview` previews have no code and cannot be sent
        service.send(confirm(plain, via="cli", code="ABCDEF"), background=False)
    assert fake.calls("POST") == []
    fake.add("POST", POSTS, created())
    assert service.send(confirm(two, via="cli", code=two.confirm_code.lower()), background=False).status == "published"


def _unknown_attempt(kit, service, fake):
    item_id = kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    fake.add("POST", POSTS, json_response(503, {}))
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "unknown"
    return item_id, attempt


def test_resolve_both_ways(publish_kit):
    service, fake = linkedin(publish_kit)
    item_id, attempt = _unknown_attempt(publish_kit, service, fake)
    person = HumanConfirmation(via="dashboard", requested_by="dashboard@1.2.3.4", preview_id=attempt.id, preview_hash="")
    with pytest.raises(InvalidInputError):
        service.resolve(person, "published", url="https://evil.example/post")
    resolved, item = service.resolve(person, "published", url="https://www.linkedin.com/feed/update/urn:li:share:5/")
    assert resolved.status == "published" and "dashboard@1.2.3.4" in resolved.resolved_by
    assert item.status == "published" and item.published_via == "linkedin_api"
    assert item.published_url == "https://www.linkedin.com/feed/update/urn:li:share:5/"
    with pytest.raises(AttemptStateError):
        service.resolve(person, "not_published")
    other_item, other = _unknown_attempt(publish_kit, service, fake)
    person = HumanConfirmation(via="dashboard", requested_by="d", preview_id=other.id, preview_hash="")
    abandoned, untouched = service.resolve(person, "not_published")
    assert abandoned.status == "abandoned" and untouched is None
    assert publish_kit.workspace.get_item(other_item).item.status == "approved"
    with pytest.raises(AttemptStateError):
        service.check_attempt(other.id)


def test_permalink_can_be_filled_in_once(publish_kit):
    service, fake = linkedin(publish_kit)
    item_id = publish_kit.item()
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    fake.add("POST", POSTS, Response(201, {}))
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "published" and attempt.permalink == ""
    assert service.attempt_json(attempt)["permalink_missing"] is True
    with pytest.raises(InvalidInputError):
        service.set_permalink(attempt.id, "http://www.linkedin.com/x", by="d")
    updated = service.set_permalink(attempt.id, "https://www.linkedin.com/feed/update/urn:li:share:3/", by="d")
    assert updated.permalink.endswith("urn:li:share:3/")
    assert publish_kit.workspace.get_item(item_id).item.published_url == updated.permalink
    with pytest.raises(AttemptStateError):
        service.set_permalink(attempt.id, "https://www.linkedin.com/feed/update/urn:li:share:4/", by="d")


def test_disconnect_refuses_while_an_attempt_is_open(publish_kit):
    service, fake = linkedin(publish_kit)
    _, attempt = _unknown_attempt(publish_kit, service, fake)
    with pytest.raises(ItemLockedError):
        service.disconnect("linkedin")
    person = HumanConfirmation(via="dashboard", requested_by="d", preview_id=attempt.id, preview_hash="")
    service.resolve(person, "not_published")
    block = service.disconnect("linkedin", forget_app=True)
    assert block["state"] == "not_configured" and "권한 있는 서비스" in block["revoke_hint"]
    assert service.store.get("linkedin") == {}


# ---------------------------------------------------------------------------
# status, readiness, fake mode
# ---------------------------------------------------------------------------


def test_unconfigured_workspace_adds_nothing(publish_kit):
    service = publish_kit.service()
    item_id = publish_kit.item()
    item = publish_kit.workspace.get_item(item_id).item
    assert service.configured() is False and service.item_block(item) is None and service.summary_line() == ""
    assert not (publish_kit.home / "credentials").exists() and not (publish_kit.home / "publish").exists()
    status = service.status()
    assert status["configured"] is False and status["platforms"]["linkedin"]["state"] == "not_configured"
    assert status["platforms"]["instagram"]["state"] == "disabled"
    assert service.health_summary() == {"enabled": True, "configured": False, "fake": False,
                                        "linkedin": "not_configured", "instagram": "disabled"}
    off = publish_kit.service({"INSIA_PUBLISH": "0"})
    assert off.item_block(item) is None and off.health_summary()["enabled"] is False
    with pytest.raises(PublishDisabledError):
        off.preview(item_id, via="dashboard", requested_by="d")


def test_status_blocks_hold_no_secret(publish_kit):
    service, _ = linkedin(publish_kit)
    service.save_linkedin_app(client_secret="li-client-secret-value")
    text = str(service.status()) + str(service.doctor_report()) + str(service.health_summary())
    for secret in (publish_kit.LI_TOKEN, "li-client-secret-value", "86client"):
        assert secret not in text
    block = service.platform_status("linkedin")
    assert block["app"]["client_id_set"] and block["account"]["id_hint"] == "…taQ" and block["token"]["days_left"] == 60
    item = publish_kit.workspace.get_item(publish_kit.item()).item
    info = service.item_block(item)
    assert info["platform"] == "linkedin" and info["available"] is True and info["blocked_by"] == ""
    publish_kit.clock.advance(51 * 86400)
    assert service.readiness("linkedin").state == "expiring"
    publish_kit.clock.advance(10 * 86400)
    assert service.readiness("linkedin").state == "needs_reconnect"


def test_instagram_needs_a_public_url(publish_kit):
    service = publish_kit.service({"INSIA_PUBLISH_INSTAGRAM": "1"})
    publish_kit.connect_instagram(service)
    ready = service.readiness("instagram")
    assert ready.state == "unavailable" and "public_url_missing" in ready.blockers and "공개 HTTPS" in ready.reason
    assert "공개 주소 없음(수동 게시)" in service.summary_line()


def test_fake_mode_only_in_a_temporary_workspace(publish_kit, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "system-tmp"))  # the workspace is not under it
    refused = PublishSettings.from_env({"INSIA_PUBLISH_FAKE": "1"}, publish_kit.home)
    assert refused.enabled is False and refused.fake is False and "임시 워크스페이스" in refused.disabled_reason
    service = PublishService(refused, publish_kit.workspace, clock=publish_kit.clock)
    item_id = publish_kit.item()
    with pytest.raises(PublishDisabledError):
        service.preview(item_id, via="dashboard", requested_by="d")
    assert publish_kit.workspace.get_item(item_id).item.status == "approved"
    allowed = PublishSettings.from_env({"INSIA_PUBLISH_FAKE": "1", "INSIA_PUBLISH_FAKE_ALLOW": str(publish_kit.home)},
                                       publish_kit.home)
    assert allowed.enabled and allowed.fake
    service = PublishService(allowed, publish_kit.workspace, clock=publish_kit.clock)  # the in-memory fake platform
    service.save_linkedin_app(client_id="fakeapp", client_secret="fake-secret-1")
    start = service.linkedin_connect()
    state = start.authorize_url.split("state=")[1].split("&")[0]
    service.linkedin_complete(f"code=fake-code-1&state={state}")
    preview = service.preview(item_id, via="dashboard", requested_by="d")
    attempt = service.send(confirm(preview), background=False)
    item = publish_kit.workspace.get_item(item_id).item
    assert attempt.status == "published" and item.published_via == "fake" and "example.invalid" in item.published_url
    assert publish_kit.workspace.published_history() == []  # the planner ignores fake posts
    assert service.status()["fake"] is True


def test_instagram_check_resolves_an_unknown_attempt_read_only(publish_kit):
    fake = FakeTransport()
    service = publish_kit.service(transport=fake, instagram=True)
    publish_kit.connect_instagram(service)
    fake.add("GET", r"/v25\.0/me$", json_response(200, {"user_id": publish_kit.IG_ID, "username": "a"}), repeat=True)
    fake.add("GET", r"/content_publishing_limit$", json_response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}),
             repeat=True)
    item_id = publish_kit.item("instagram")
    preview = service.preview(item_id, options={"is_ai_generated": False}, via="dashboard", requested_by="d")
    ids = iter(range(1000, 1100))
    fake.add("POST", r"/media$", lambda r: json_response(200, {"id": str(next(ids))}), repeat=True)
    platform = {"published": False}  # what Instagram knows about the carousel container 1007

    def status(request):
        done = platform["published"] and request.url.endswith("/1007")
        return json_response(200, {"status_code": "PUBLISHED" if done else "FINISHED"})

    fake.add("GET", r"/v25\.0/1\d{3}$", status, repeat=True)
    fake.add("POST", r"/media_publish$", json_response(502, {}))  # the answer is lost; the one check says FINISHED
    attempt = service.send(confirm(preview), background=False)
    assert attempt.status == "unknown" and len(fake.calls("POST", r"/media_publish$")) == 1
    token = publish_kit.workspace.publish_attempt_media_token(attempt.id)
    assert (publish_kit.home / "publish" / "public" / token).is_dir()  # kept for 24 h while unknown
    platform["published"] = True  # it did go up after all
    fake.add("GET", r"/17841400000000001/media$", json_response(200, {"data": [
        {"id": "555", "permalink": "https://www.instagram.com/p/late/", "timestamp": attempt.created_at}]}))
    checked = service.check_attempt(attempt.id)
    assert checked.status == "published" and checked.external_id == "555" and checked.resolved_by == "instagram_check"
    assert len(fake.calls("POST", r"/media_publish$")) == 1  # the check never publishes
    item = publish_kit.workspace.get_item(item_id).item
    assert item.status == "published" and item.published_url == "https://www.instagram.com/p/late/"
    assert not (publish_kit.home / "publish" / "public" / token).exists()


def test_cleanup_and_the_background_job(publish_kit):
    service, fake = linkedin(publish_kit)
    ws = publish_kit.workspace
    stale = service.preview(publish_kit.item(), via="dashboard", requested_by="d")
    service.media.write_staging(stale.preview_id, [b"\xff\xd8x"])
    publish_kit.clock.advance(1801)
    assert service.cleanup() == {"staging": 1, "public": 0}
    closed: list[list[str]] = []
    service.start_background()
    assert ws.publish_recovery_hook == service._after_recovery
    attempt, _ = _begin(publish_kit, service)
    ws.release_publish_worker(attempt.id)
    ws.publish_recovery_hook = lambda ids: closed.append(ids)
    assert ws._publish_recovery_tick() == [attempt.id] and closed == [[attempt.id]]  # the watcher's publish part
    ws.publish_recovery_hook = service._after_recovery
    service.shutdown(timeout=2)
    assert ws.publish_recovery_hook is None and not service._bg_thread.is_alive()
