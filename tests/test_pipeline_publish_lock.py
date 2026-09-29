"""The pipeline meets an item that is being published through the API (package A, DESIGN.md 5-4): only that
channel is not stored (``channel.store_skipped``), the other channels are, and the run still completes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from insia_agents.backends.mock_backend import MockBackend
from insia_agents.db import Workspace, pipeline_item_id
from insia_agents.events import EventBus, SimClock
from insia_agents.models import Brief, PublishConnection, ReviewIssue
from insia_agents.pipeline import STORE_SKIPPED_MESSAGE, resume_run, run_pipeline

pytestmark = pytest.mark.usefixtures("no_network")


class Crash(BaseException):
    """The process dying (not an Exception, so nothing catches it)."""


class FailFirstReview(MockBackend):
    def review(self, brief, research, draft, format_checks):
        review = super().review(brief, research, draft, format_checks)
        if draft.round == 0:
            issue = ReviewIssue(severity="critical", location="본문", problem="강제 실패", fix="고치기")
            review = review.model_copy(update={"issues": [*review.issues, issue]})
        return review


class CrashOnRevise(FailFirstReview):
    def revise(self, *args, **kwargs):
        raise Crash("kill -9")


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    yield workspace
    workspace.close()


def test_resume_skips_only_the_channel_being_published(settings, ws):
    run_id = "run-publish-lock"
    brief = Brief(topic="게시 중인 콘텐츠", channels=["linkedin", "naver_blog"])
    with pytest.raises(Crash):  # both R0 drafts are stored and reviewed, then the process dies while revising
        run_pipeline(brief, CrashOnRevise(settings), EventBus(run_id, clock=SimClock(0)), settings, workspace=ws)
    ws.update_run(run_id, status="interrupted")
    linkedin = pipeline_item_id(run_id, "linkedin")
    blog = pipeline_item_id(run_id, "naver_blog")
    assert ws.get_item(linkedin) is not None and ws.get_item(blog) is not None
    # meanwhile a person approved the LinkedIn draft and started an API publish of it
    ws.set_item_status(linkedin, "approved", force=True)
    ws.save_publish_connection(PublishConnection(platform="linkedin", account_id="sub1", status="connected"))
    now = datetime.now(timezone.utc)
    preview = ws.create_publish_preview(linkedin, ws.get_item(linkedin).item.version, "linkedin", "sub1", {"schema": 1},
                                        "sha256:" + "e" * 64, created_via="dashboard", requested_by="d", now=now)
    attempt, _token = ws.begin_publish_attempt(preview.id, preview.payload_hash, via="dashboard", requested_by="d", now=now)
    before_linkedin = [v.id for v in ws.get_item(linkedin).versions]
    before_blog = len(ws.get_item(blog).versions)

    result = resume_run(run_id, settings, ws, backend=FailFirstReview(settings), out_dir=None)

    assert {r.channel for r in result.results} == {"linkedin", "naver_blog"}  # the run record keeps both
    run = ws.get_run(run_id)
    assert run["status"] == "completed" and run["progress"]["channels"] == {"linkedin": "completed", "naver_blog": "completed"}
    assert [v.id for v in ws.get_item(linkedin).versions] == before_linkedin  # nothing written to the locked item
    assert ws.get_item(linkedin).item.status == "approved" and ws.active_publish_attempt(linkedin).id == attempt.id
    assert len(ws.get_item(blog).versions) > before_blog  # the other channel was stored as usual
    events = ws.list_events(run_id)
    skipped = [e for e in events if e["type"] == "channel.store_skipped"]
    assert len(skipped) == 1 and skipped[0]["data"]["channel"] == "linkedin"
    assert skipped[0]["data"]["attempt_id"] == attempt.id and skipped[0]["data"]["message"] == STORE_SKIPPED_MESSAGE
    assert any(e["type"] == "log" and e["data"].get("level") == "warn" and STORE_SKIPPED_MESSAGE in e["data"]["message"]
               for e in events)
    completed = events[-1]
    assert completed["type"] == "run.completed" and completed["data"]["store_skipped"] == ["linkedin"]
    assert completed["data"]["warnings"] == [f"링크드인: {STORE_SKIPPED_MESSAGE}"]
    ws.release_publish_worker(attempt.id)
