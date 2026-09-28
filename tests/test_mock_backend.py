from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from insia_agents.backends.mock_backend import (
    MockBackend,
    SampleRun,
    josa,
    short_name,
    template_draft,
    template_plan,
    template_research,
    template_review,
)
from insia_agents.channels import check_format
from insia_agents.models import ALL_CHANNELS, Brief, Draft, Finding, ResearchPack, Review, Source

REPO = Path(__file__).resolve().parents[1]
REAL_SAMPLE = REPO / "examples" / "sample-run"

BRIEFS = [
    Brief(topic="테스트 주제"),
    Brief(topic="가", audience="나", keywords=["다"]),
    Brief(topic="반려동물 수제간식 온라인 쇼핑몰 창업을 위한 브랜드 런칭과 정부지원사업 신청 준비 전체 과정",
          audience="반려동물을 키우는 20~40대 1인 가구와 동네 펫샵 운영자",
          keywords=["반려동물 수제간식 브랜드 런칭 마케팅 전략 가이드북 완전판", "펫푸드"]),
    Brief(topic="1인 창업자를 위한 AI 콘텐츠 에이전트 'INSIA 스마트에이전트'", keywords=["AI 마케팅 자동화", "1인 창업"]),
]
STAT = re.compile(r"\d[\d,.]*\s*(%|퍼센트|만\s*(곳|개|명|원)|억|조\s*원)")


@pytest.mark.parametrize("brief", BRIEFS, ids=["short", "tiny", "long", "sample-like"])
def test_template_loop_is_realistic(brief):
    research = template_research(brief, "2026-09-28")
    for channel in ALL_CHANNELS:
        first = template_draft(brief, research, channel, 0)
        revised = template_draft(brief, research, channel, 1)
        first_checks = {c.id: c.passed for c in check_format(first, brief)}
        revised_checks = check_format(revised, brief)
        assert all(c.passed for c in revised_checks), (channel, [c for c in revised_checks if not c.passed])
        if channel == "linkedin":
            assert not first_checks["hook_length"] and not first_checks["hashtags"]
        if channel == "naver_blog":
            assert not first_checks["headings"] and not first_checks["images"]
        if channel in ("bizplan", "instagram"):
            assert all(first_checks.values())
        for draft in (first, revised):
            assert "[데모]" in draft.content or "[데모]" in draft.title
            assert not STAT.search(draft.content), STAT.search(draft.content)
    for finding in research.findings:
        assert "○○" in finding.claim and not STAT.search(finding.claim)


def test_template_reviews_drive_the_loop(brief):
    research = template_research(brief, "2026-09-28")
    outcomes = {}
    for channel in ALL_CHANNELS:
        for rnd in (0, 1):
            draft = template_draft(brief, research, channel, rnd)
            review = template_review(brief, research, draft, check_format(draft, brief))
            outcomes[(channel, rnd)] = review
    assert not outcomes[("bizplan", 0)].passed and outcomes[("bizplan", 0)].needs_research
    assert not outcomes[("naver_blog", 0)].passed and not outcomes[("linkedin", 0)].passed
    assert outcomes[("instagram", 0)].passed
    assert all(outcomes[(c, 1)].passed for c in ALL_CHANNELS)
    assert all(not outcomes[(c, 1)].needs_research for c in ALL_CHANNELS)


def test_plan_covers_requested_channels_only():
    brief = Brief(topic="t", channels=["instagram"])
    plan = template_plan(brief)
    assert [o.channel for o in plan.outlines] == ["instagram"]
    assert 3 <= len(plan.questions) <= 6
    assert all(q.channels == ["instagram"] for q in plan.questions)


def test_korean_helpers():
    assert josa("서비스", "은/는") == "서비스는"
    assert josa("주제", "이/가") == "주제가"
    assert josa("에이전트 서비스", "을/를") == "에이전트 서비스를"
    assert josa("책", "은/는") == "책은"
    assert short_name("1인 창업자를 위한 서비스 'INSIA 스마트에이전트'") == "INSIA 스마트에이전트"
    assert len(short_name("가" * 100)) <= 24


# ---------------------------------------------------------------------------
# Replay of a recorded run (fixture sample, independent of examples/sample-run)
# ---------------------------------------------------------------------------


def _write(path: Path, model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sample_dir(tmp_path) -> Path:
    root = tmp_path / "sample-run"
    brief = Brief(topic="기록된 샘플 주제", channels=["linkedin", "instagram"], keywords=["AI 에이전트"])
    _write(root / "brief.json", brief)
    _write(root / "plan.json", template_plan(brief))
    research = template_research(brief, "2026-09-28")
    _write(root / "research.json", research)
    r0 = template_draft(brief, research, "linkedin", 0).model_copy(update={"title": "기록 R0"})
    r1 = template_draft(brief, research, "linkedin", 1).model_copy(update={"title": "기록 R1"})
    _write(root / "drafts" / "linkedin.r0.json", r0)
    _write(root / "drafts" / "linkedin.r1.json", r1)
    for rnd, draft in ((0, r0), (1, r1)):
        review = template_review(brief, research, draft, check_format(draft, brief))
        update = {"summary": f"기록된 검수 R{rnd}", "needs_research": ["기록된 추가 질문"] if rnd == 0 else []}
        _write(root / "reviews" / f"linkedin.r{rnd}.json", review.model_copy(update=update))
    followup = ResearchPack(
        findings=[Finding(id="f1", question_id="q5", claim="기록된 추가 근거", source_ids=["s1"], confidence="medium")],
        sources=[Source(id="s1", title="추가 출처", url="https://www.k-startup.go.kr/x", publisher="창업진흥원", tier=1)],
        gaps=[],
    )
    _write(root / "followups" / "linkedin.r0.json", followup)
    (root / "meta.json").write_text(json.dumps({"model": "claude-opus-5"}), encoding="utf-8")
    return root


def test_sample_run_reuses_last_round(sample_dir):
    sample = SampleRun.load(sample_dir)
    assert sample is not None and sample.complete()
    assert sample.draft("linkedin", 2).title == "기록 R1"
    assert sample.review("linkedin", 5).summary == "기록된 검수 R1"
    assert sample.draft("instagram", 0) is None


def test_replay_uses_recorded_files(settings, sample_dir):
    from insia_agents.pipeline import execute_run

    backend = MockBackend(replace(settings, sample_dir=sample_dir))
    brief = SampleRun.load(sample_dir).brief
    assert backend.replaying(brief)
    assert "재생" in backend.prepare(brief) and backend.model == "claude-opus-5"
    result, bus = execute_run(brief, settings, backend=backend)
    linkedin = next(r for r in result.results if r.channel == "linkedin")
    assert [d.title for d in linkedin.drafts] == ["기록 R0", "기록 R1"]
    assert linkedin.reviews[0].summary == "기록된 검수 R0"
    # format score is recomputed by code even for recorded reviews
    assert linkedin.reviews[0].format_checks and not all(c.passed for c in linkedin.reviews[0].format_checks)
    added = [f for f in result.research.findings if f.claim == "기록된 추가 근거"]
    assert len(added) == 1 and added[0].id == f"f{len(result.research.findings)}"
    assert added[0].source_ids == [result.research.sources[-1].id] and result.research.sources[-1].title == "추가 출처"
    instagram = next(r for r in result.results if r.channel == "instagram")
    assert "[데모]" in instagram.final.title  # no recorded draft → template
    assert bus.events[0]["data"]["model"] == "claude-opus-5"


def test_other_topics_use_templates(settings, sample_dir):
    backend = MockBackend(replace(settings, sample_dir=sample_dir))
    other = Brief(topic="전혀 다른 주제")
    assert not backend.replaying(other)
    assert "[데모]" in backend.prepare(other) and backend.model == "mock-template"


@pytest.mark.skipif(not (REAL_SAMPLE / "research.json").is_file(), reason="examples/sample-run is not recorded yet")
def test_real_sample_replays(settings):
    from insia_agents.pipeline import execute_run

    real = replace(settings, sample_dir=REAL_SAMPLE)
    brief = Brief.model_validate_json((REAL_SAMPLE / "brief.json").read_text(encoding="utf-8"))
    backend = MockBackend(real)
    assert backend.replaying(brief)
    result, bus = execute_run(brief, real, backend=backend)
    assert {r.channel for r in result.results} == set(brief.channels)
    assert bus.events[-1]["type"] == "run.completed"
    for channel_result in result.results:
        for draft in channel_result.drafts:
            assert isinstance(draft, Draft)
        for review in channel_result.reviews:
            assert isinstance(review, Review)
