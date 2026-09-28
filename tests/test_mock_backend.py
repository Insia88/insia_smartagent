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


# ---------------------------------------------------------------------------
# Run context: profile, user documents, instructions, synthetic usage
# ---------------------------------------------------------------------------

from insia_agents.backends.base import RunContext  # noqa: E402
from insia_agents.models import Profile, TeamMember, UserDocument  # noqa: E402

PROFILE = Profile(
    company_name="인시아랩", service_name="INSIA", one_liner="1인 창업자를 위한 AI 콘텐츠 비서",
    target_customers="동네 카페 사장님", problem="콘텐츠 제작 시간이 부족함", differentiators=["출처 기반 작성"],
    business_model="월 구독형 SaaS", traction=["베타 사용자 120명(2026-08 기준)"],
    team=[TeamMember(role="대표", name="김철수", background="마케팅 10년"), TeamMember(role="디자이너", hiring=True)],
    banned_words=["완벽한", "업무자동화"], required_phrases=["#광고아님"], default_hashtags=["#INSIA"],
    cta="무료 체험은 프로필 링크에서", contact="hello@insia.kr", instagram_handle="@insia.ai", brand_colors=["#6D5EF5"],
)


def _profiled(settings, **context) -> MockBackend:
    backend = MockBackend(settings)
    backend.context = RunContext(**{"profile": PROFILE, "documents": [], "today": "2026-09-28", **context})
    return backend


def test_profile_values_appear_in_template_drafts(brief):
    research = template_research(brief, "2026-09-28", PROFILE)
    drafts = {(c, r): template_draft(brief, research, c, r, PROFILE) for c in ALL_CHANNELS for r in (0, 1)}
    biz = drafts[("bizplan", 1)].content
    assert "INSIA 사업계획서" in drafts[("bizplan", 1)].title
    assert "| 기업명 | 인시아랩 (자사 자료) |" in biz and "| 사업 형태 | 월 구독형 SaaS (자사 자료) |" in biz
    assert "- 실적·지표: 베타 사용자 120명(2026-08 기준) (자사 자료)" in biz
    assert "| 대표 | ○○○ | 대표 | 마케팅 10년 (자사 자료) |" in biz and "| 채용 예정 | ○○○ | 디자이너 | [요구 역량: ○○] |" in biz
    assert "김철수" not in biz and "동네 카페 사장님" in biz
    for channel in ("naver_blog", "linkedin", "instagram"):
        final = drafts[(channel, 1)]
        assert "#광고아님" in final.content and "무료 체험은 프로필 링크에서" in final.content
        assert final.hashtags[0] == "#INSIA"
    assert "계정명 @insia.ai" in drafts[("instagram", 0)].content and "브랜드 주 색(#6D5EF5) 배경" in drafts[("instagram", 0)].content
    assert "문의: hello@insia.kr" in drafts[("naver_blog", 0)].content
    assert "참고로 INSIA를 한 줄로 소개하면" in drafts[("linkedin", 1)].content


def test_profile_checks_drive_a_realistic_loop(brief):
    research = template_research(brief, "2026-09-28", PROFILE)
    for channel in ALL_CHANNELS:
        revised = template_draft(brief, research, channel, 1, PROFILE)
        failed = [c for c in check_format(revised, brief, PROFILE) if not c.passed]
        assert not failed, (channel, failed)
    blog_r0 = {c.id: c.passed for c in check_format(template_draft(brief, research, "naver_blog", 0, PROFILE), brief, PROFILE)}
    assert blog_r0["banned_words"] is False  # the template says "완벽한"; the revision removes it
    blog_r1 = template_draft(brief, research, "naver_blog", 1, PROFILE)
    assert "완벽한" not in blog_r1.content and "#업무자동화" not in blog_r1.hashtags
    assert any("금지 표현" in line for line in blog_r1.change_log)


def test_user_documents_become_user_sources_and_demo_findings(settings, brief):
    docs = [UserDocument(id="u1", title="회사 소개서", text="INSIA는 2026년에 시작한 서비스다. 두 번째 문장이다." * 40),
            UserDocument(id="u2", title="빈 자료", text="   "),
            UserDocument(id="u3", title="IR 메모", text="# 요약\n베타 사용자는 120명이다.")]
    backend = MockBackend(replace(settings, max_document_chars=500))
    backend.context = RunContext(profile=None, documents=docs, today="2026-09-28")
    notices = []
    backend.on_notice = lambda agent, level, msg: notices.append((agent, level, msg))
    plan = backend.plan(brief)
    pack = backend.research(brief, plan.questions, lambda t, d: None)
    users = [s for s in pack.sources if s.origin == "user"]
    assert [(s.id, s.url, s.tier, s.publisher, s.title) for s in users] == [
        ("s5", "user://u1", 1, "사용자 제공 자료", "회사 소개서"), ("s6", "user://u3", 1, "사용자 제공 자료", "IR 메모")]
    claims = [f for f in pack.findings if f.source_ids[0] in ("s5", "s6")]
    assert [f.claim for f in claims] == [
        "[데모] 「회사 소개서」(사용자 제공 자료)에 적힌 내용: INSIA는 2026년에 시작한 서비스다.",
        "[데모] 「IR 메모」(사용자 제공 자료)에 적힌 내용: 요약 베타 사용자는 120명이다."]
    assert all(f.confidence == "medium" and "외부 검증 전" in f.note for f in claims)
    assert {f.question_id for f in claims} <= {q.id for q in plan.questions}
    assert [(a, lvl) for a, lvl, m in notices if "max_document_chars" in m] == [("researcher", "warn")]
    # follow-ups never re-add the documents
    followup = backend.research(brief, plan.questions[:1], lambda t, d: None, existing=pack)
    assert not any(s.origin == "user" for s in followup.sources)
    # the business plan cites them as 자사 자료
    biz = template_draft(brief, pack, "bizplan", 1)
    assert "- [s5] 자사 자료, 「회사 소개서」(사용자 제공, 외부 검증 전)" in biz.content and "[s5]" in biz.content.split("## 1.")[0]
    blog = template_draft(brief, pack, "naver_blog", 1)
    assert "출처: 자사 자료 「IR 메모」(사용자 제공)" in blog.content


def test_mock_reports_free_synthetic_usage(settings, brief):
    from insia_agents.pipeline import execute_run

    backend = _profiled(settings)
    records = []
    backend.on_usage = records.append
    brief1 = brief.model_copy(update={"channels": ["linkedin"]})
    context = RunContext(profile=PROFILE, documents=[], today="2026-09-28")
    result, _ = execute_run(brief1, settings, backend=backend, context=context)
    tasks = [r.task for r in records]
    assert tasks[:2] == ["plan", "research"] and "draft" in tasks and "review" in tasks
    assert all(r.model == "mock" and r.cost_usd == 0.0 and r.input_tokens > 0 and r.output_tokens > 0 for r in records)
    assert result.results[0].final.hashtags[0] == "#INSIA"


def test_mock_revise_records_human_instructions(settings, brief):
    backend = _profiled(settings, instructions="도입부를 두 문장으로 줄여 주세요")
    research = template_research(brief, "2026-09-28")
    draft = template_draft(brief, research, "linkedin", 0)
    review = template_review(brief, research, draft, check_format(draft, brief))
    plan = template_plan(brief)
    from_context = backend.revise(brief, plan, research, draft, review)
    assert from_context.round == 1 and from_context.change_log[0].startswith("[사람 지시] 도입부를 두 문장으로 줄여 주세요")
    explicit = backend.revise(brief, plan, research, draft, review, instructions="해시태그를 3개로")
    assert explicit.change_log[0].startswith("[사람 지시] 해시태그를 3개로")
    plain = MockBackend(backend.settings).revise(brief, plan, research, draft, review)
    assert not any(line.startswith("[사람 지시]") for line in plain.change_log)


def test_replay_ignores_the_context_and_says_so(settings, sample_dir):
    backend = MockBackend(replace(settings, sample_dir=sample_dir))
    brief = SampleRun.load(sample_dir).brief
    recorded = SampleRun.load(sample_dir).research()
    backend.context = RunContext(profile=PROFILE, documents=[UserDocument(id="u1", title="자료", text="본문")], today="2026-09-28")
    assert "반영되지 않아요" in backend.prepare(brief)
    assert backend.research(brief, [], lambda t, d: None) == recorded
    assert backend.draft(brief, backend.plan(brief), recorded, "linkedin").title == "기록 R0"


@pytest.mark.skipif(not (REAL_SAMPLE / "research.json").is_file(), reason="examples/sample-run is not recorded yet")
def test_real_sample_replay_is_unchanged_by_a_profile(settings):
    real = replace(settings, sample_dir=REAL_SAMPLE)
    brief = Brief.model_validate_json((REAL_SAMPLE / "brief.json").read_text(encoding="utf-8"))
    plain, profiled = MockBackend(real), MockBackend(real)
    profiled.context = RunContext(profile=PROFILE, documents=[UserDocument(id="u1", title="자료", text="본문")], today="2026-09-28")
    for backend in (plain, profiled):
        backend.prepare(brief)
    assert plain.research(brief, [], lambda t, d: None) == profiled.research(brief, [], lambda t, d: None)
    plan = plain.plan(brief)
    for channel in brief.channels:
        assert plain.draft(brief, plan, plain.research(brief, [], lambda t, d: None), channel) == \
            profiled.draft(brief, plan, profiled.research(brief, [], lambda t, d: None), channel)
