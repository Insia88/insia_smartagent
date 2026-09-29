"""Workspace side of API publishing (package A): migration 3, the partial unique index, owner-token-conditional
writes, recovery, and the item lock on every internal write path (DESIGN.md 5-1 … 5-5)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import insia_agents.db as db
from insia_agents.db import AttemptTakenOverError, ItemLockedError, PublishStateError, Workspace, pipeline_item_id
from insia_agents.models import Brief, ChannelResult, ContentItem, Draft, PublishConnection, Review

pytestmark = pytest.mark.usefixtures("no_network")

NOW = datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc)
HASH = "sha256:" + "a" * 64


def _draft(round_: int = 0, content: str = "본문") -> Draft:
    return Draft(channel="linkedin", round=round_, title="제목", content=content, hashtags=["#a", "#b", "#c"])


def _review(round_: int = 0) -> Review:
    return Review(channel="linkedin", round=round_, score=90, passed=True, rubric=[], issues=[], summary="좋아요")


def _approved(ws: Workspace) -> str:
    item = ws.create_item("linkedin", "제목")
    ws.add_version(item.id, _draft(), source="human")
    ws.set_item_status(item.id, "approved", force=True)
    return item.id


def _connect(ws: Workspace, account: str = "sub1") -> None:
    ws.save_publish_connection(PublishConnection(platform="linkedin", account_id=account, status="connected"))


def _begin(ws: Workspace, item_id: str, *, now: datetime = NOW, digest: str = HASH):
    preview = ws.create_publish_preview(item_id, ws.get_item(item_id).item.version, "linkedin", "sub1",
                                        {"schema": 1}, digest, created_via="dashboard", requested_by="d", now=now)
    return ws.begin_publish_attempt(preview.id, digest, via="dashboard", requested_by="d", now=now)


def test_version_2_workspace_migrates_to_3_and_keeps_its_items(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    full = list(db.MIGRATIONS)
    assert len(full) == 3
    monkeypatch.setattr(db, "MIGRATIONS", full[:2])
    with Workspace(home) as old:  # a workspace written by the INSIA before API publishing
        assert old.schema_version == 2
        with old.transaction() as conn:
            conn.execute("INSERT INTO items (id, channel, title, status, version, approved_version, published_at, "
                         "published_url, created_at, updated_at) VALUES ('it_old', 'linkedin', '옛 콘텐츠', 'published', 1, 1, "
                         "'2026-09-01T00:00:00.000Z', 'https://www.linkedin.com/feed/update/urn:li:share:1/', "
                         "'2026-09-01T00:00:00.000Z', '2026-09-01T00:00:00.000Z')")
            conn.execute("INSERT INTO versions (id, item_id, version, source, draft, created_at) VALUES "
                         "('v_old', 'it_old', 1, 'human', ?, '2026-09-01T00:00:00.000Z')", (_draft().model_dump_json(),))
    monkeypatch.setattr(db, "MIGRATIONS", full)
    with Workspace(home) as ws:
        assert ws.schema_version == 3
        item = ws.get_item("it_old").item
        assert (item.status, item.published_via, item.published_external_id) == ("published", "", "")
        assert item.published_url == "https://www.linkedin.com/feed/update/urn:li:share:1/" and item.version == 1
        dumped = item.model_dump()
        assert set(dumped) - {"published_via", "published_external_id"} == set(ContentItem.model_fields) - {
            "published_via", "published_external_id"}
        tables = {r[0] for r in ws._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"publish_connections", "publish_previews", "publish_attempts"} <= tables
        assert [i.id for i in ws.published_history()] == ["it_old"]  # hand-published records stay in the history
    assert ContentItem(id="x", channel="linkedin", title="t").published_via == ""


def test_partial_unique_index_allows_one_live_attempt(tmp_path):
    with Workspace(tmp_path / "ws") as ws:
        item_id = _approved(ws)
        insert = ("INSERT INTO publish_attempts (id, item_id, version, platform, payload_hash, status, created_at, updated_at) "
                  "VALUES (?, ?, 1, 'linkedin', 'h', ?, 'now', 'now')")
        with ws.transaction() as conn:
            conn.execute(insert, ("pa_1", item_id, "failed"))
            conn.execute(insert, ("pa_2", item_id, "abandoned"))
            conn.execute(insert, ("pa_3", item_id, "sending"))
        for status in ("sending", "unknown", "published"):
            with pytest.raises(sqlite3.IntegrityError):
                with ws.transaction() as conn:
                    conn.execute(insert, (f"pa_x_{status}", item_id, status))
        with ws.transaction() as conn:
            conn.execute(insert, ("pa_4", item_id, "failed"))
            conn.execute("UPDATE publish_attempts SET version = 2 WHERE id = 'pa_4'")


def test_begin_checks_everything_in_one_transaction(tmp_path):
    with Workspace(tmp_path / "ws") as ws:
        item_id = _approved(ws)
        preview = ws.create_publish_preview(item_id, 1, "linkedin", "sub1", {"schema": 1}, HASH, created_via="cli",
                                            confirm_code_hash="b" * 64, requested_by="cli:me@host", now=NOW)
        cases = [(dict(via="dashboard", preview_hash=HASH), "preview_via"),
                 (dict(via="cli", preview_hash="sha256:" + "c" * 64), "hash_mismatch"),
                 (dict(via="cli", preview_hash=HASH), "not_connected")]
        for kwargs, reason in cases:
            with pytest.raises(PublishStateError) as caught:
                ws.begin_publish_attempt(preview.id, requested_by="x", now=NOW, **kwargs)
            assert caught.value.reason == reason
        _connect(ws, "someone-else")
        with pytest.raises(PublishStateError) as caught:
            ws.begin_publish_attempt(preview.id, HASH, via="cli", requested_by="x", now=NOW)
        assert caught.value.reason == "account_changed"
        _connect(ws)
        with pytest.raises(PublishStateError) as caught:
            ws.begin_publish_attempt(preview.id, HASH, via="cli", requested_by="x", now=NOW + timedelta(minutes=31))
        assert caught.value.reason == "preview_expired"
        attempt, token = ws.begin_publish_attempt(preview.id, HASH, via="cli", requested_by="cli:me@host", now=NOW)
        assert attempt.status == "sending" and len(token) == 32 and token not in attempt.model_dump_json()
        with pytest.raises(PublishStateError) as caught:
            ws.begin_publish_attempt(preview.id, HASH, via="cli", requested_by="x", now=NOW)
        assert caught.value.reason == "preview_used"
        assert ws.check_confirm_code(preview.id, "anything") is False and ws.active_publish_attempt(item_id).id == attempt.id
        ws.release_publish_worker(attempt.id)


def test_every_write_path_is_locked_while_sending_or_unknown(tmp_path):
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        run_id = "20260928-0300-lock"
        brief = Brief(topic="잠금", channels=["linkedin"])
        ws.create_run(run_id, brief)
        item_id = pipeline_item_id(run_id, "linkedin")
        ws.ensure_item(item_id, "linkedin", "제목", run_id=run_id, brief=brief)
        stored = ws.add_run_version(item_id, _draft(), run_id=run_id)
        ws.set_item_status(item_id, "approved", force=True)
        attempt, token = _begin(ws, item_id)
        version_id = stored.version.id
        result = ChannelResult(channel="linkedin", final=_draft(1, "새 본문"), drafts=[_draft(), _draft(1, "새 본문")],
                               reviews=[_review(), _review(1)], passed=True, rounds=1)
        blocked = {
            "add_version": lambda: ws.add_version(item_id, _draft(1, "고침"), source="human"),
            "add_run_version": lambda: ws.add_run_version(item_id, _draft(1, "다음 라운드"), run_id=run_id),
            "add_job_version": lambda: ws.add_job_version(item_id, _draft(1, "수정 요청 결과"), base_version=1),
            "upsert_item_from_result": lambda: ws.upsert_item_from_result(run_id, result, brief),
            "attach_review": lambda: ws.attach_review(version_id, _review()),
            "set_item_status(published)": lambda: ws.set_item_status(item_id, "published"),
            "set_item_status(archived)": lambda: ws.set_item_status(item_id, "archived"),
            "set_item_status(same)": lambda: ws.set_item_status(item_id, "approved", note="메모"),
        }
        for name, call in blocked.items():
            with pytest.raises(ItemLockedError) as caught:
                call()
            assert caught.value.attempt_id == attempt.id and caught.value.code == "item_locked", name
            assert caught.value.http_status == 409
        updated = ws.update_item(item_id, title="새 제목", scheduled_at="2026-10-01")  # not locked (DESIGN.md 4-3)
        assert updated.title == "새 제목" and updated.version == 1 and updated.status == "approved"
        ws.claim_publish_write(attempt.id, token, now=NOW)
        ws.finish_publish_failure(attempt.id, token, status="unknown", error="응답 없음", now=NOW)
        with pytest.raises(ItemLockedError) as caught:
            ws.add_version(item_id, _draft(1, "고침"), source="human")
        assert caught.value.status == "unknown" and "정리" in str(caught.value)
        ws.resolve_publish_attempt(attempt.id, "not_published", resolved_by="d")
        ws.add_version(item_id, _draft(1, "이제 고칠 수 있어요"), source="human")  # unlocked again
        assert ws.get_item(item_id).item.version == 2


def test_owner_token_conditions_and_recovery(tmp_path):
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        item_id = _approved(ws)
        attempt, token = _begin(ws, item_id)
        with pytest.raises(AttemptTakenOverError):
            ws.update_publish_attempt(attempt.id, "not-the-token", step="check", now=NOW)
        ws.update_publish_attempt(attempt.id, token, step="check", state_patch={"is_ai_generated": False}, now=NOW)
        with pytest.raises(db.WorkspaceError):
            ws.update_publish_attempt(attempt.id, token, state_patch={"access_token": "x"}, now=NOW)
        assert ws.heartbeat_publish_attempt(attempt.id, token, now=NOW + timedelta(seconds=30))
        assert ws.recover_publish_attempts(now=NOW + timedelta(hours=1)) == []  # held by this process
        ws.release_publish_worker(attempt.id)
        assert ws.recover_publish_attempts(now=NOW + timedelta(seconds=40)) == [attempt.id]  # our pid, no worker → gone
        recovered = ws.get_publish_attempt(attempt.id)
        assert recovered.status == "failed" and recovered.error_code == "interrupted"
        assert not ws.heartbeat_publish_attempt(attempt.id, token, now=NOW)
        for call in (lambda: ws.claim_publish_write(attempt.id, token, now=NOW),
                     lambda: ws.finish_publish_success(attempt.id, token, via="linkedin_api", now=NOW),
                     lambda: ws.finish_publish_failure(attempt.id, token, status="failed", now=NOW)):
            with pytest.raises(AttemptTakenOverError):
                call()
        assert ws.get_publish_attempt(attempt.id).status == "failed"  # the recovery's record stands


@pytest.mark.parametrize("steps, status", [
    (["check"], "failed"), (["media", "self_check", "children 2/7", "polling"], "failed"), (["carousel"], "failed"),
    (["write"], "unknown"), (["write", "permalink"], "unknown"),  # the permalink read comes after the post exists
])
def test_recovery_decides_by_the_last_step(tmp_path, steps, status):
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        item_id = _approved(ws)
        attempt, token = _begin(ws, item_id)
        for step in steps:
            if step == "write":
                ws.claim_publish_write(attempt.id, token, now=NOW)
            else:
                ws.update_publish_attempt(attempt.id, token, step=step, now=NOW)
        ws.release_publish_worker(attempt.id)
        with ws.transaction() as conn:  # another host: only the missing heartbeat decides
            conn.execute("UPDATE publish_attempts SET owner_host = 'other-host'")
        assert ws.recover_publish_attempts(now=NOW + timedelta(seconds=db.PUBLISH_STALE_AFTER_SECONDS - 1)) == []
        assert ws.recover_publish_attempts(now=NOW + timedelta(seconds=db.PUBLISH_STALE_AFTER_SECONDS + 1)) == [attempt.id]
        recovered = ws.get_publish_attempt(attempt.id)
        assert (recovered.status, recovered.step, recovered.error_code) == (status, steps[-1], "interrupted")
        assert (recovered.finished_at == "") == (status == "unknown")
        if status == "unknown":  # the post may exist: the item stays frozen and no second attempt can start
            with pytest.raises(ItemLockedError):
                ws.set_item_status(item_id, "published")
            with pytest.raises((ItemLockedError, PublishStateError)):
                _begin(ws, item_id, digest="sha256:" + "e" * 64)
        else:
            ws.set_item_status(item_id, "scheduled", scheduled_at="2026-10-01")  # not sent: the item is free again


def test_success_is_committed_before_the_item_and_never_lost(tmp_path):
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        item_id = _approved(ws)
        attempt, token = _begin(ws, item_id)
        ws.claim_publish_write(attempt.id, token, now=NOW)
        with ws.transaction() as conn:  # something moved the item meanwhile (e.g. an older INSIA wrote to it)
            conn.execute("UPDATE items SET status = 'draft' WHERE id = ?", (item_id,))
        done, item = ws.finish_publish_success(attempt.id, token, external_id="urn:li:share:1",
                                               permalink="https://www.linkedin.com/feed/update/urn:li:share:1/",
                                               via="linkedin_api", now=NOW)
        assert done.status == "published" and item is None and "게시 완료 표시" in done.state["item_update_error"]
        assert ws.get_item(item_id).item.status == "draft"
        with pytest.raises(PublishStateError) as caught:  # the unique index still blocks a second post
            _begin(ws, item_id, digest="sha256:" + "d" * 64)
        assert caught.value.reason in ("not_publishable", "already_published")


def test_finish_success_moves_the_item_and_fake_posts_skip_the_planner_history(tmp_path):
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        item_id = _approved(ws)
        attempt, token = _begin(ws, item_id)
        done, item = ws.finish_publish_success(attempt.id, token, external_id="urn:li:share:2", permalink="", via="fake",
                                               now=NOW)
        assert item.status == "published" and item.published_via == "fake" and item.published_url == ""
        assert ws.published_history() == []
        assert ws.finished_media_tokens() == []
        assert done.status == "published"
        with pytest.raises(PublishStateError):  # only 'unknown' attempts can be resolved
            ws.resolve_publish_attempt(attempt.id, "published", resolved_by="d", via="linkedin_api")


def test_a_late_success_reopens_a_closed_attempt_as_published(tmp_path):
    """A re-check closed the attempt as ``failed`` (Instagram's ``FINISHED``) while the worker's retry still went
    through: the confirmed post wins (the rule is the same for every platform)."""
    url = "https://www.linkedin.com/feed/update/urn:li:share:5/"
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        item_id = _approved(ws)
        attempt, token = _begin(ws, item_id)
        ws.claim_publish_write(attempt.id, token, now=NOW)
        ws.finish_publish_failure(attempt.id, token, status="unknown", error_code="timeout", error="확인 필요", now=NOW)
        ws.reconcile_publish_attempt(attempt.id, status="failed", error_code="FINISHED", error="게시되지 않았어요", now=NOW)
        with pytest.raises(db.WorkspaceError):
            ws.record_late_publish_success(attempt.id, external_id="urn:li:share:5", via="", now=NOW)
        done, item = ws.record_late_publish_success(attempt.id, external_id="urn:li:share:5", permalink=url,
                                                    via="linkedin_api", now=NOW)
        assert (done.status, done.error, done.resolved_by, done.external_id) == \
            ("published", "", "system:late_answer", "urn:li:share:5")
        assert done.state["late_success"]["status_before"] == "failed"
        assert (item.status, item.published_external_id, item.published_url) == ("published", "urn:li:share:5", url)
        with pytest.raises(PublishStateError):  # the unique index blocks a second post of this version again
            _begin(ws, item_id, digest="sha256:" + "f" * 64)


def _post(ws: Workspace, item_id: str, urn: str, permalink: str, *, now: datetime):
    attempt, token = _begin(ws, item_id, now=now, digest="sha256:" + urn[-1] * 64)
    ws.claim_publish_write(attempt.id, token, now=now)
    return ws.finish_publish_success(attempt.id, token, external_id=urn, permalink=permalink, via="linkedin_api",
                                     now=now)


def _edited_and_approved(ws: Workspace, item_id: str) -> None:
    """보관 → 복원 → 고쳐서 v2 → 승인: the way an item a person already posted comes back for another post."""
    ws.set_item_status(item_id, "archived")
    ws.set_item_status(item_id, "draft")
    ws.add_version(item_id, _draft(1, "다시 올리려고 고친 본문"), source="human")
    ws.set_item_status(item_id, "approved", force=True)


def test_an_api_post_of_a_new_version_replaces_the_older_posts_record(tmp_path, monkeypatch):
    """Final review F1 (follow-up to F1-4): once v2 is posted through the API, the item's publish fields are v2's
    post — its time, its id and its address or none, never v1's — so the address a person fills in later lands on
    the item (also over a v1 address an item kept before this fix). Details arriving later for the older post (a
    filled-in address, a late answer) leave the newer post's record alone; 게시 완료 표시 after a failed item update
    records the v2 post, not v1's address."""
    link = "https://www.linkedin.com/feed/update/urn:li:share:{}/".format
    later = NOW + timedelta(days=3)
    with Workspace(tmp_path / "ws") as ws:
        _connect(ws)
        # A: v1 posted with an address; v2 posted without one (the platform gave none)
        a = _approved(ws)
        _post(ws, a, "urn:li:share:1", link(1), now=NOW)
        _edited_and_approved(ws, a)
        v2, item = _post(ws, a, "urn:li:share:2", "", now=later)
        assert (item.status, item.version, item.published_via, item.published_external_id, item.published_url,
                item.published_at) == ("published", 2, "linkedin_api", "urn:li:share:2", "", db._fmt(later))
        with ws.transaction() as conn:  # what the code before this fix kept: v1's address on v2's record
            conn.execute("UPDATE items SET published_url = ? WHERE id = ?", (link(1), a))
        ws.set_publish_permalink(v2.id, link(2), by="d")
        assert ws.get_item(a).item.published_url == link(2)
        # B: v1 posted without an address, v2 with one; v1's address and a late answer for v1 arrive afterwards
        b = _approved(ws)
        v1, _ = _post(ws, b, "urn:li:share:3", "", now=NOW)
        _edited_and_approved(ws, b)
        _post(ws, b, "urn:li:share:4", link(4), now=later)
        ws.set_publish_permalink(v1.id, link(3), by="d")
        ws.record_late_publish_success(v1.id, external_id="urn:li:share:3", permalink=link(3), via="linkedin_api",
                                       now=later)
        assert ws.get_publish_attempt(v1.id).permalink == link(3)
        item = ws.get_item(b).item
        assert (item.published_url, item.published_external_id, item.published_at) == \
            (link(4), "urn:li:share:4", db._fmt(later))
        # D: v2's API post could not move the item; 게시 완료 표시 then records the v2 post (no v1 address or time)
        d = _approved(ws)
        _post(ws, d, "urn:li:share:5", link(5), now=NOW)
        _edited_and_approved(ws, d)
        monkeypatch.setattr(ws, "_mark_item_published_via_api", lambda *args, **kwargs: db.PUBLISH_ITEM_UPDATE_ERROR)
        v2, item = _post(ws, d, "urn:li:share:6", "", now=later)
        monkeypatch.undo()
        assert item is None and ws.get_item(d).item.published_url == link(5)
        item = ws.set_item_status(d, "published")
        assert (item.published_via, item.published_external_id, item.published_url, item.published_at) == \
            ("linkedin_api", "urn:li:share:6", "", db._fmt(later))


def test_review_and_revise_refuse_a_publish_locked_item_before_any_paid_call(tmp_path, settings):
    """actions.review_item / revise_item check the publish lock first: no run row, no backend call."""
    from insia_agents import actions

    class Boom:  # any backend use would be a paid call on a result that could never be stored
        name = "boom"
        model = "boom"

        def __getattr__(self, attr):
            raise AssertionError(f"backend used while the item is locked: {attr}")

    ws = Workspace(tmp_path / "ws")
    try:
        item_id = _approved(ws)
        _connect(ws)
        attempt, _token = _begin(ws, item_id)
        runs_before = len(ws.list_runs())
        for call in (lambda: actions.review_item(ws, item_id, settings=settings, backend=Boom()),
                     lambda: actions.revise_item(ws, item_id, "짧게", settings=settings, backend=Boom())):
            with pytest.raises(ItemLockedError) as info:
                call()
            assert info.value.attempt_id == attempt.id and info.value.status == "sending"
        assert len(ws.list_runs()) == runs_before
    finally:
        ws.close()
