"""Quality evaluation set: case files, deterministic graders, mock runner, compare, live refusals.

Offline only: the runner uses the mock backend (or an injected fake backend with priced usage) in a temporary
workspace, and live mode is only exercised up to its refusals — no network, no API key.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

import pytest

from insia_agents.backends.mock_backend import MockBackend
from insia_agents.channels import check_format
from insia_agents.cli import main
from insia_agents.evals.cases import Assertion, CaseError, EvalCase, load_case, load_cases
from insia_agents.evals.compare import compare_summaries
from insia_agents.evals.estimate import load_measured, measured_estimate, model_estimate
from insia_agents.evals.graders import apply_assertions, grade_channel
from insia_agents.evals.grounding import Evidence, analyze_numbers, grounding_summary, placeholders
from insia_agents.errors import UsageError
from insia_agents.evals.runner import EvalOptions, run_eval
from insia_agents.models import (ALL_CHANNELS, Brief, ChannelResult, Draft, Finding, Profile, ResearchPack, Review,
                                 Source, TeamMember, UsageRecord)

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases"


@pytest.fixture(autouse=True)
def user_home(tmp_path, monkeypatch):
    """The eval must never write into the user's workspace: point INSIA_HOME at an empty folder and check it later."""
    home = tmp_path / "user-workspace"
    home.mkdir()
    monkeypatch.setenv("INSIA_HOME", str(home))
    monkeypatch.delenv("INSIA_MAX_COST_USD", raising=False)
    return home


# ---------------------------------------------------------------------------
# Case files
# ---------------------------------------------------------------------------


def test_case_files_validate_and_cover_every_channel():
    cases = load_cases(CASES)
    assert 10 <= len(cases) <= 14
    covered = {channel for case in cases for channel in case.channels}
    assert covered == set(ALL_CHANNELS)
    assert all(case.must for case in cases)
    ids = {case.id for case in cases}
    for wanted in ("sample-insia-all", "bizplan-blind-team", "sns-banned-required", "facts-unavailable-surf",
                   "pricing-assumption", "very-short-brief", "instagram-carousel-limits", "naver-long-keyword"):
        assert wanted in ids
    sample = next(c for c in cases if c.id == "sample-insia-all")
    recorded = Brief.model_validate_json((ROOT / "examples" / "sample-run" / "brief.json").read_text(encoding="utf-8"))
    assert sample.brief == recorded and sample.source == "sample-run"
    assert any(c.profile is None for c in cases) and any(c.documents for c in cases)
    blind = next(c for c in cases if c.id == "bizplan-blind-team")
    names = [m.name for m in blind.profile.team if m.name]  # type: ignore[union-attr]
    not_contains = [v for a in blind.must if a.type == "not_contains" for v in a.values]
    assert names and set(names) <= set(not_contains)


def _write_case(tmp_path: Path, data: dict, name: str | None = None) -> Path:
    path = tmp_path / f"{name or data['id']}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_case_validation_names_every_problem(tmp_path):
    base = {"id": "bad-case", "title": "잘못된 케이스", "brief": {"topic": "주제", "channels": ["linkedin"]},
            "must": [{"type": "checks_pass", "checks": ["slides"]},
                     {"type": "not_contains", "channels": ["bizplan"], "values": ["김민수"]}]}
    with pytest.raises(CaseError) as info:
        load_case(_write_case(tmp_path, base, name="other-name"))
    message = str(info.value)
    assert "파일 이름" in message and "slides" in message and "브리프에 없는 채널" in message
    with pytest.raises(CaseError, match="value"):
        load_case(_write_case(tmp_path, {**base, "must": [{"type": "min_score"}]}))
    with pytest.raises(CaseError, match="must"):
        load_case(_write_case(tmp_path, {**base, "must": []}))
    with pytest.raises(CaseError, match="없는 케이스"):
        load_cases(CASES, ["no-such-case"])


# ---------------------------------------------------------------------------
# Graders on hand-made drafts
# ---------------------------------------------------------------------------


RESEARCH = ResearchPack(
    findings=[Finding(id="f1", question_id="q1", claim="2024년 기준 소상공인 기업체 수는 613.4만개, 디지털 기술 활용률은 27.2%다.",
                      source_ids=["s1"], confidence="high"),
              Finding(id="f2", question_id="q2", claim="경쟁 서비스 월 요금은 29,000원이다 (2026년 1월 기준).", source_ids=["s2"],
                      confidence="medium")],
    sources=[Source(id="s1", title="2024년 기준 소상공인실태조사", url="https://www.mss.go.kr", publisher="중소벤처기업부", tier=1),
             Source(id="s2", title="요금 비교 기사", url="https://news.example.com", publisher="서울경제", tier=2)],
    gaps=["마케팅 애로 비율 45%는 원문을 확인하지 못함"],
)


def _case(channel: str, *, profile: Profile | None = None, must: list[dict] | None = None, keywords=None) -> EvalCase:
    return EvalCase(id="t-case", title="테스트", brief=Brief(topic="테스트 주제", channels=[channel], keywords=keywords or []),
                    profile=profile, must=[Assertion.model_validate(m) for m in (must or [{"type": "all_checks_pass"}])])


def _grade(case: EvalCase, draft: Draft, research: ResearchPack = RESEARCH) -> dict:
    review = Review(channel=draft.channel, round=0, score=85, passed=True, rubric=[], issues=[], summary="-")
    result = ChannelResult(channel=draft.channel, final=draft, drafts=[draft], reviews=[review], passed=True, rounds=0)
    grade = grade_channel(case, result, research, Evidence.build(research, profile=case.profile))
    apply_assertions(case, grade, "mock")
    return grade


def _outcome(grade: dict, key_prefix: str) -> dict:
    return next(o for o in grade["must"] if o["key"].startswith(key_prefix))


def test_blind_leak_fails_checks_and_not_contains():
    profile = Profile(company_name="상세메이트", team=[TeamMember(role="대표", name="김민수",
                                                                   background="서울대학교 경영학과 졸업, 현대카드 마케팅 6년")])
    case = _case("bizplan", profile=profile, must=[{"type": "checks_pass", "checks": ["blind_names"]},
                                                   {"type": "not_contains", "values": ["김민수", "서울대"]}])
    leaked = Draft(channel="bizplan", round=0, title="사업계획서",
                   content="## 4. 팀 구성\n\n- 대표 김민수: 서울대학교 경영학과 졸업, 현대카드 출신 마케터")
    grade = _grade(case, leaked)
    assert not _outcome(grade, "checks_pass")["passed"] and "블라인드" in _outcome(grade, "checks_pass")["detail"]
    assert not _outcome(grade, "not_contains")["passed"] and "김민수" in _outcome(grade, "not_contains")["detail"]
    masked = leaked.model_copy(update={"content": "## 4. 팀 구성\n\n- 대표 ○○○: ○○ 경영학과 졸업, 마케팅 6년 (자사 자료)"})
    grade = _grade(case, masked)
    assert all(o["passed"] for o in grade["must"])


def test_unsupported_number_found_and_rounding_consistent():
    case = _case("linkedin", must=[{"type": "max_unsupported_numbers", "value": 0}])
    draft = Draft(channel="linkedin", round=0, title="-", content=(
        "중소벤처기업부 조사에 따르면 소상공인은 613만 곳이에요.\n\n"
        "디지털 기술을 쓰는 곳은 27%예요.\n\n"
        "마케팅이 가장 큰 애로라는 답은 45%였어요.\n\n"  # only in research.gaps: not evidence
        "우리 고객은 3배 늘었어요.\n\n"
        "출시 2년 차에는 가격 월 19,000원(가정)으로 시작해요."))
    mentions = analyze_numbers(draft, Evidence.build(RESEARCH))
    status = {m.text: m.status for m in mentions if m.kind == "claim"}
    assert status["613만 곳"] == "supported" and status["27%"] == "supported"
    assert status["45%"] == "unsupported" and status["3배"] == "unsupported"
    assert status["19,000원"] == "assumed" and "2년" not in status  # "2년 차" is a schedule point
    grade = _grade(case, draft)
    outcome = _outcome(grade, "max_unsupported_numbers")
    assert not outcome["passed"] and outcome["value"] == 2 and "45%" in outcome["detail"]
    # 60% does not round from 59.1%, 15% does from 15.4%
    research = ResearchPack(findings=[Finding(id="f1", question_id="q1", claim="응답률 59.1%, 증가율 15.4%", source_ids=["s1"],
                                              confidence="high")], sources=RESEARCH.sources[:1])
    two = analyze_numbers(Draft(channel="linkedin", round=0, title="-", content="응답은 60%, 증가는 15%예요."),
                          Evidence.build(research))
    assert [(m.text, m.status) for m in two] == [("60%", "unsupported"), ("15%", "supported")]


def test_banned_word_and_required_phrase():
    profile = Profile(banned_words=["최고", "무조건"], required_phrases=["#광고"])
    case = _case("instagram", profile=profile, must=[{"type": "checks_pass", "checks": ["banned_words", "required_phrases"]}])
    slides = "\n\n".join(f"### 슬라이드 {i} — 제목\n- 문구: 내용" for i in range(1, 9))
    bad = Draft(channel="instagram", round=0, title="동네 최 고 필라테스",
                content=f"## 캐러셀\n\n{slides}\n\n## 캡션\n\n체험 수업 안내예요.", hashtags=["#필라테스", "#운동", "#체험"])
    outcome = _outcome(_grade(case, bad), "checks_pass")
    assert not outcome["passed"] and "최고" in outcome["detail"] and "#광고" in outcome["detail"]
    good = bad.model_copy(update={"title": "퇴근길 필라테스", "content": bad.content + "\n\n#광고"})
    assert _outcome(_grade(case, good), "checks_pass")["passed"]


def test_placeholders_counted_and_not_numbers():
    case = _case("naver_blog", must=[{"type": "min_placeholders", "value": 1}, {"type": "max_unsupported_numbers", "value": 0}])
    invented = Draft(channel="naver_blog", round=0, title="비수기 매출", content="온라인 강습으로 매출이 30% 늘었어요.")
    grade = _grade(case, invented)
    assert not _outcome(grade, "min_placeholders")["passed"] and not _outcome(grade, "max_unsupported")["passed"]
    honest = invented.model_copy(update={"content": "온라인 강습으로 매출이 ○○% 늘었다는 이야기가 있어요 "
                                                    "[확인 필요: 사례 출처]. [이미지: 서핑 보드] 출처는 [s1]."})
    grade = _grade(case, honest)
    assert grade["placeholders"]["count"] == 2 and grade["grounding"]["claims"] == 0
    assert all(o["passed"] for o in grade["must"])


def test_assumption_marked_for_money_outside_research():
    case = _case("bizplan", must=[{"type": "assumption_marked", "units": ["원"], "markers": ["가정"]}])
    table = ("| 요금제 | 월 요금 |\n|---|---|\n| 베이직 | 19,000원 |\n\n※ 가정: 시장 검증 전 제안 가격\n\n"
             "경쟁 서비스는 월 29,000원이에요 [s2].")
    assert _outcome(_grade(case, Draft(channel="bizplan", round=0, title="-", content=table)), "assumption_marked")["passed"]
    bare = "우리 요금은 월 19,000원이에요. 경쟁 서비스는 월 29,000원이에요 [s2]."
    outcome = _outcome(_grade(case, Draft(channel="bizplan", round=0, title="-", content=bare)), "assumption_marked")
    assert not outcome["passed"] and "19,000원" in outcome["detail"] and "29,000원" not in outcome["detail"]


EMPTY = ResearchPack(findings=[], sources=[])


def _statuses(text: str, research: ResearchPack = RESEARCH, **evidence) -> dict[str, str]:
    draft = Draft(channel="naver_blog", round=0, title="-", content=text)
    return {m.text: m.status for m in analyze_numbers(draft, Evidence.build(research, **evidence)) if m.kind == "claim"}


def test_markers_are_words_and_plan_words_must_attach_to_the_figure():
    empty = EMPTY
    # not assumptions: another word that starts with 가정, a plan word naming a market, a plan word about something else
    assert _statuses("가정용 정수기 렌탈은 월 19,900원이에요.", empty) == {"19,900원": "unsupported"}
    assert _statuses("가정에서 먹기 좋은 유산균, 한 통에 29,000원이에요.", empty) == {"29,000원": "unsupported"}
    assert _statuses("가정간편식 시장은 5조 원이에요.", empty) == {"5조 원": "unsupported"}
    assert _statuses("목표 시장 규모는 3조 원이고 연평균 12% 성장해요.", empty) == {"3조 원": "unsupported", "12%": "unsupported"}
    assert _statuses("목표 고객은 전국 카페 사장님 12만 명이에요.", empty) == {"12만 명": "unsupported"}
    assert _statuses("12월 출시 예정인 신제품은 고객 5,000명이 사전 신청했어요.", empty) == {"5,000명": "unsupported"}
    assert _statuses("고객 5,000명이 사전 신청했고 출시 예정이에요.", empty) == {"5,000명": "unsupported"}
    # assumptions: an assumption word anywhere in the sentence/row/※ note, or a plan word attached to the figure
    for text in ("월 19,000원(가정)으로 시작해요.", "요금은 월 19,000원으로 가정했어요.", "목표 매출은 3억 원이에요.",
                 "1차년도 목표: 매출 1.2억 원", "출시 3년 차 유료 구독 2,000개 확보 목표", "월 3만 원 수준으로 책정할 예정이에요.",
                 "사업비 5,000만 원 집행 계획", "보수적 시나리오에서 월 매출 300만 원이에요."):
        assert set(_statuses(text, empty).values()) == {"assumed"}, text
    assert _statuses("| 매출 목표 | 1.2억 원 | 3.9억 원 |", empty) == {"1.2억 원": "assumed", "3.9억 원": "assumed"}
    assert _statuses("| 베이직 | 19,000원 |\n\n※ 가정: 시장 검증 전 제안 가격", empty) == {"19,000원": "assumed"}
    # the same word rules apply to assumption_marked
    case = _case("naver_blog", must=[{"type": "assumption_marked", "units": ["원"], "markers": ["가정"]}])
    for text, ok in (("가정용 정수기 렌탈은 월 19,900원이에요.", False), ("가정에서 한 통에 29,000원이에요.", False),
                     ("한 통에 29,000원(가정)이에요.", True)):
        outcome = _outcome(_grade(case, Draft(channel="naver_blog", round=0, title="-", content=text), empty), "assumption_marked")
        assert outcome["passed"] is ok, text


def test_number_forms_approximations_and_compound_words():
    research = ResearchPack(findings=[Finding(id="f1", question_id="q1", claim="가맹점은 3,120곳, 누적 고객은 54,300명이다.",
                                              source_ids=["s1"], confidence="high")], sources=RESEARCH.sources[:1])
    # "여" approximations are figures, matched against the range they cover
    assert _statuses("가맹점 3천여 곳, 고객 5만여 명이 쓰고 있어요.", research) == {"3천여 곳": "supported", "5만여 명": "supported"}
    assert _statuses("가맹점 4천여 곳이 쓰고 있어요.", research) == {"4천여 곳": "unsupported"}
    assert _statuses("20여 년 동안 100여 개 매장을 열었어요.", EMPTY) == {"20여 년": "unsupported", "100여 개": "unsupported"}
    # a unit that starts a longer word is not a figure; particles and endings after a unit are fine
    assert _statuses("3원칙을 지켜요. 4년제 대학을 나왔어요. 20개년 계획이에요.", EMPTY) == {}
    found = _statuses("30%의 고객이 10년간 1만 원대 요금을 냈고 전국 30개소에서 2배로 늘었어요.", EMPTY)
    assert set(found) == {"30%", "10년", "1만 원", "30개소", "2배"}


def test_platform_names_are_not_citations():
    research = ResearchPack(findings=[], sources=[Source(id="s1", title="Instagram 공지", url="https://about.instagram.com",
                                                        publisher="Instagram (Meta)", tier=2)])
    def cited(text: str) -> bool:
        draft = Draft(channel="linkedin", round=0, title="-", content=text)
        return analyze_numbers(draft, Evidence.build(research))[0].cited
    assert not cited("인스타그램 팔로워가 30% 늘었어요.")
    assert not cited("Instagram 도달률이 30% 늘었어요.") and not cited("네이버 검색 유입이 30% 늘었어요.")
    assert cited("인스타그램 도달률이 30% 늘었어요 [s1].") and cited("인스타그램 공식 발표에 따르면 도달률이 30% 늘었어요.")


def test_value_the_users_document_leaves_open_must_stay_an_assumption():
    """user-docs-traction: the document says the 29,000원 price is under review; stating it as settled must fail."""
    from insia_agents.models import UserDocument

    case = load_case(CASES / "user-docs-traction.json")
    doc = case.documents[0]
    documents = [UserDocument(id="d1", title=doc.title, text=doc.text, kind=doc.kind)]
    evidence = Evidence.build(EMPTY, profile=case.profile, documents=documents)
    body = ("## 1. 문제 인식\n\n- 꽃집 예약 누락\n\n## 2. 실현 가능성\n\n- {price}\n\n## 3. 성장 전략\n\n"
            "- 베타 사용 꽃집 38곳 (자사 자료)\n\n## 4. 팀 구성\n\n- 대표: 플로리스트 8년\n\n" + "내용 " * 800)

    def grade(price: str) -> dict:
        draft = Draft(channel="bizplan", round=0, title="플로노트 사업계획서", content=body.format(price=price))
        review = Review(channel="bizplan", round=0, score=85, passed=True, rubric=[], issues=[], summary="-")
        result = ChannelResult(channel="bizplan", final=draft, drafts=[draft], reviews=[review], passed=True, rounds=0)
        g = grade_channel(case, result, EMPTY, evidence)
        apply_assertions(case, g, "live")
        return g

    settled = grade("요금은 월 29,000원 단일 요금제로 판매해요.")
    marked = _outcome(settled, "assumption_marked")
    assert not marked["passed"] and "29,000원" in marked["detail"] and "확정 전" in marked["detail"]
    numbers = {n["text"]: n for n in settled["grounding"]["numbers"]}
    assert numbers["29,000원"]["status"] == "unsupported" and numbers["29,000원"]["tentative"]
    assert numbers["38곳"]["status"] == "supported" and not numbers["38곳"]["tentative"]
    assumed = grade("요금은 월 29,000원 단일 요금제(가정, 확정 전)로 검토 중이에요.")
    assert _outcome(assumed, "assumption_marked")["passed"]
    assert {n["text"]: n["status"] for n in assumed["grounding"]["numbers"]}["29,000원"] == "supported"


def test_facts_unavailable_makes_placeholders_a_must():
    case = load_case(CASES / "facts-unavailable-surf.json")
    assert not case.facts_available
    implied = [a for a in case.assertions("must") if a.note.startswith("facts_available")]
    assert [(a.type, a.value, a.channels) for a in implied] == [("min_placeholders", 1, [])]
    assert not any(a.type == "min_placeholders" for a in case.should)
    invented = Draft(channel="linkedin", round=0, title="-", content="겨울에도 온라인 강습으로 버텼어요.")
    grade = _grade(case, invented, EMPTY)
    placeholder = next(o for o in grade["must"] if o["type"] == "min_placeholders")
    assert not placeholder["passed"] and placeholder["note"].startswith("facts_available")
    honest = invented.model_copy(update={"content": "겨울 매출은 [확인 필요: 월 매출]만큼 줄었어요."})
    assert next(o for o in _grade(case, honest, EMPTY)["must"] if o["type"] == "min_placeholders")["passed"]
    # an explicit must for one channel is kept; the flag covers only the other channels
    custom = EvalCase(id="t-facts", title="t", facts_available=False,
                      brief=Brief(topic="t", channels=["linkedin", "naver_blog"]),
                      must=[Assertion(type="min_placeholders", value=2, channels=["linkedin"])])
    assert [(a.type, a.channels) for a in custom.implied()] == [("min_placeholders", ["naver_blog"]),
                                                                ("max_unsupported_numbers", [])]
    assert EvalCase(id="t-ok", title="t", brief=Brief(topic="t", channels=["linkedin"]),
                    must=custom.must).implied() == []


def test_dates_citations_and_empty_draft():
    draft = Draft(channel="linkedin", round=0, title="-", content=(
        "2024년 기준 소상공인은 613.4만개예요 [s1].\n\n'24년 조사에서 디지털 활용은 27.2%예요.\n\n#소상공인 #데이터 #창업"))
    mentions = analyze_numbers(draft, Evidence.build(RESEARCH))
    summary = grounding_summary(mentions)
    assert summary["dates"] == 2 and summary["claims"] == 2 and summary["cited"] == 1 and summary["citation_coverage"] == 0.5
    empty = Draft(channel="linkedin", round=0, title="", content="")
    checks = check_format(empty, None, None)
    assert not all(c.passed for c in checks)
    grade = _grade(_case("linkedin"), empty)
    assert not grade["must"][0]["passed"] and placeholders(empty)["count"] == 0


# ---------------------------------------------------------------------------
# Runner (mock), compare, CLI
# ---------------------------------------------------------------------------


# Environment a real user may have; the module-scoped run below must not see any of it.
LEAKY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "INSIA_MODE", "INSIA_MODEL", "INSIA_OUT_DIR", "INSIA_TODAY",
             "INSIA_MAX_COST_USD", "INSIA_PROMPTS_DIR", "INSIA_SAMPLE_DIR", "INSIA_FALLBACKS")


@pytest.fixture(scope="module")
def mock_result(tmp_path_factory):
    """One mock eval over three cases, shared by several tests.

    Module-scoped fixtures run before the function-scoped autouse ones (``user_home``, ``isolated_env``), so this
    one isolates itself: ``INSIA_HOME`` points at a folder that does not exist yet (the eval must neither create nor
    write it), ``HOME`` is empty, credentials and ``INSIA_*`` are cleared, and the working directory is an empty
    folder (relative defaults such as ``./workspace`` and ``./outputs`` would land there)."""
    root = tmp_path_factory.mktemp("eval")
    user_home = root / "user-workspace"
    cwd = root / "cwd"
    cwd.mkdir()
    (root / "home").mkdir()
    out = root / "mock-a"
    lines: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        for name in LEAKY_ENV:
            mp.delenv(name, raising=False)
        mp.setenv("INSIA_HOME", str(user_home))
        mp.setenv("HOME", str(root / "home"))
        mp.chdir(cwd)
        summary = run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=out,
                                       case_ids=["sample-insia-all", "bizplan-blind-team", "sns-banned-required"]),
                           printer=lines.append)
        seen_home = user_home.exists()
        cwd_files = sorted(p.name for p in cwd.iterdir())
    return out, summary, lines, {"home_created": seen_home, "cwd_files": cwd_files, "user_home": user_home}


def test_mock_runner_writes_expected_files(mock_result):
    out, summary, lines, isolation = mock_result
    assert summary["exit_code"] == 0 and summary["mode"] == "mock"
    for name in ("summary.json", "report.md", "cases/sample-insia-all.json", "cases/bizplan-blind-team.json",
                 "runs/sample-insia-all/result.json", "runs/sns-banned-required/events.jsonl"):
        assert (out / name).is_file(), name
    totals = summary["totals"]
    assert totals["cases"] == 3 and totals["channels"] == 7 and totals["error_channels"] == 0
    assert totals["must_passed"] == totals["must_total"] > 10 and totals["cost_usd"] == 0
    data = json.loads((out / "cases" / "sample-insia-all.json").read_text(encoding="utf-8"))
    blog = next(g for g in data["reps"][0]["channels"] if g["channel"] == "naver_blog")
    assert blog["draft"]["content"] and blog["grounding"]["numbers"] and blog["checks"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# INSIA 품질 평가 결과") and "mock 모드" in report and "필수 조건을 모두 통과" in report
    assert any("결과 폴더" in line for line in lines)
    # the user's workspace (INSIA_HOME, set before the run) was never created, and nothing landed in the working folder
    assert isolation["home_created"] is False and not isolation["user_home"].exists()
    assert isolation["cwd_files"] == []
    assert summary["plan"]["scope"] == [["sample-insia-all", c] for c in ALL_CHANNELS] + \
        [["bizplan-blind-team", "bizplan"], ["sns-banned-required", "instagram"], ["sns-banned-required", "linkedin"]]


def test_mock_runs_are_deterministic(mock_result, tmp_path):
    out, first, _, _ = mock_result
    again = run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=tmp_path / "b", case_ids=["bizplan-blind-team"]),
                     printer=lambda _line: None)
    before = next(c for c in first["cases"] if c["id"] == "bizplan-blind-team")
    after = again["cases"][0]
    assert before["channels"] == after["channels"] and before["must"] == after["must"]


def test_compare_detects_regression_and_score_drop(mock_result, tmp_path, capsys):
    out, summary, _, _ = mock_result
    same = compare_summaries(summary, copy.deepcopy(summary))
    assert same["exit_code"] == 0 and not same["regressions"]

    worse = copy.deepcopy(summary)
    channel = next(c for case in worse["cases"] if case["id"] == "bizplan-blind-team" for c in case["channels"])
    blind = next(o for o in channel["must"] if o["key"] == "checks_pass:blind_names")
    blind.update({"passed": False, "detail": "실패: 블라인드(실명 미노출)(실명 1개 노출)"})
    result = compare_summaries(summary, worse)
    assert result["exit_code"] == 1 and result["regressions"][0]["key"] == "checks_pass:blind_names"

    lower = copy.deepcopy(summary)
    for case in lower["cases"]:
        for c in case["channels"]:
            c["score"] = c["score"] - 10
    assert compare_summaries(summary, lower)["score_drop_exceeded"]
    assert compare_summaries(summary, lower, max_score_drop=12)["exit_code"] == 0

    missing = copy.deepcopy(summary)
    missing["cases"] = [c for c in missing["cases"] if c["id"] != "sns-banned-required"]
    assert compare_summaries(summary, missing)["missing"]

    worse_dir = tmp_path / "worse"
    worse_dir.mkdir()
    (worse_dir / "summary.json").write_text(json.dumps(worse, ensure_ascii=False), encoding="utf-8")
    assert main(["eval", "compare", str(out), str(worse_dir)]) == 1
    text = capsys.readouterr().out
    assert "회귀" in text and "블라인드" in text
    assert main(["eval", "compare", str(out), str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["exit_code"] == 0


def test_run_with_baseline_reports_regressions(mock_result, tmp_path):
    out, summary, _, _ = mock_result
    fake = copy.deepcopy(summary)
    for case in fake["cases"]:
        for c in case["channels"]:
            c["score"] = 100
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "summary.json").write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
    result = run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=tmp_path / "now", case_ids=["bizplan-blind-team"],
                                  baseline=base_dir), printer=lambda _line: None)
    assert result["exit_code"] == 1 and result["baseline"]["score_drop_exceeded"]
    report = (tmp_path / "now" / "report.md").read_text(encoding="utf-8")
    top = report.split("## 먼저 볼 것", 1)[1].split("##", 1)[0]
    # the top of the report must not say "all passed" when the comparison failed (here: only a score drop)
    assert "기준 대비 실패" in top and "평균 검수 점수" in top and "모두 통과" not in top
    assert "기준 대비 변화" in report


def test_subset_run_against_full_baseline_compares_only_what_it_ran(mock_result, tmp_path, capsys):
    """A cheap --case/--channels pilot against the last full baseline passes when nothing it covered got worse."""
    out, _, _, _ = mock_result
    result = run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=tmp_path / "one", case_ids=["bizplan-blind-team"],
                                  baseline=out), printer=lambda _line: None)
    cmp = result["baseline"]
    assert result["exit_code"] == 0 and cmp["exit_code"] == 0 and not cmp["missing"]
    assert {(i["case"], i["channel"]) for i in cmp["out_of_scope"]} == \
        {("sample-insia-all", c) for c in ALL_CHANNELS} | {("sns-banned-required", "instagram"), ("sns-banned-required", "linkedin")}
    top = (tmp_path / "one" / "report.md").read_text(encoding="utf-8").split("## 먼저 볼 것", 1)[1].split("##", 1)[0]
    assert "회귀도 없어요" in top
    # --channels narrows the scope the same way (via the CLI, which reads summary.json's plan)
    assert main(["eval", "run", "--mode", "mock", "--case", "sns-banned-required", "--channels", "linkedin",
                 "--baseline", str(out), "--out", str(tmp_path / "li")]) == 0
    assert "회귀 없음" in capsys.readouterr().out
    assert main(["eval", "compare", str(out), str(tmp_path / "li")]) == 0
    assert "비교 범위" in capsys.readouterr().out
    # a channel the run planned but could not grade still fails the comparison
    broken = json.loads((tmp_path / "li" / "summary.json").read_text(encoding="utf-8"))
    broken["cases"][0]["channels"][0].update({"status": "error", "error": "API 오류"})
    assert compare_summaries(json.loads((out / "summary.json").read_text(encoding="utf-8")), broken)["missing"]


def test_cli_list_and_mock_run(tmp_path, capsys):
    assert main(["eval", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {"id", "channels", "must"} <= set(listed[0])
    assert main(["eval", "run", "--mode", "mock", "--case", "very-short-brief", "--out", str(tmp_path / "r")]) == 0
    assert "필수 조건" in capsys.readouterr().out
    assert main(["eval", "run", "--mode", "mock", "--case", "very-short-brief", "--out", str(tmp_path / "r")]) == 2
    assert "이미 있어요" in capsys.readouterr().err


@pytest.mark.usefixtures("no_network")
def test_live_refuses_without_budget_or_credentials(tmp_path, capsys):
    assert main(["eval", "run", "--mode", "live", "--case", "very-short-brief", "--out", str(tmp_path / "x")]) == 2
    assert "--max-cost-usd" in capsys.readouterr().err
    assert main(["eval", "run", "--mode", "live", "--case", "very-short-brief", "--max-cost-usd", "3",
                 "--out", str(tmp_path / "x")]) == 1
    assert "자격 증명" in capsys.readouterr().err
    assert main(["eval", "run", "--mode", "mock", "--judge", "--out", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x").exists()
    assert main(["eval", "run", "--mode", "live", "--dry-run", "--case", "sample-insia-all"]) == 0
    printed = capsys.readouterr().out
    assert "예상 비용" in printed and "sample-insia-all" in printed


class PricedMock(MockBackend):
    """Mock content with priced usage, to exercise the live runner's budget path without a network."""

    def _usage(self, agent, task, prompt, output):  # noqa: ANN001
        if self.on_usage is not None:
            self.on_usage(UsageRecord(agent=agent, task=task, model="claude-opus-5", input_tokens=1000, output_tokens=100,
                                      cost_usd=0.4))


@pytest.mark.usefixtures("no_network")
def test_live_runner_stops_at_the_budget_and_skips_the_rest(tmp_path):
    lines: list[str] = []
    summary = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=tmp_path / "live", max_cost_usd=2.0,
                                   case_ids=["very-short-brief", "instagram-carousel-limits", "naver-long-keyword"]),
                       printer=lines.append, backend_factory=PricedMock)
    statuses = [c["status"] for c in summary["cases"]]
    assert statuses[0] in ("budget_stopped", "partial", "ok") and statuses[-1] == "skipped"
    assert summary["exit_code"] == 1 and summary["stopped"]
    assert 2.0 <= summary["totals"]["cost_usd"] < 3.0  # the call in flight when the cap was crossed still counts
    assert any("예상 비용" in line for line in lines)
    skipped = summary["cases"][-1]["channels"][0]
    assert skipped["must"] and all(o.get("error") for o in skipped["must"])


def test_estimates_scale_and_use_measured_costs(tmp_path):
    one = model_estimate("a", ["linkedin"], model="claude-opus-5", max_rounds=2, context_chars=500)
    four = model_estimate("b", list(ALL_CHANNELS), model="claude-opus-5", max_rounds=2, context_chars=500)
    assert 0 < one.typical_usd < four.typical_usd <= four.max_usd
    summary = {"kind": "insia-eval", "mode": "live", "cases": [
        {"id": "a", "status": "ok", "reps": 1, "cost_usd": 1.2, "channels": [{"status": "ok"}]},
        {"id": "b", "status": "ok", "reps": 1, "cost_usd": 4.8, "channels": [{"status": "ok"}] * 3}]}
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    measured = load_measured(tmp_path)
    assert measured_estimate("a", ["linkedin"], measured, max_rounds=2).typical_usd == pytest.approx(1.2)
    assert measured_estimate("c", ["linkedin", "instagram"], measured, max_rounds=2).typical_usd == pytest.approx(3.0)


def test_resume_reruns_changed_channel_sets_and_reuses_finished_runs(tmp_path, capsys):
    out = tmp_path / "res"
    assert main(["eval", "run", "--mode", "mock", "--case", "sns-banned-required", "--channels", "linkedin",
                 "--out", str(out)]) == 0
    # the channel set changed (all channels now): the earlier run is not reused and nothing crashes
    assert main(["eval", "run", "--mode", "mock", "--case", "sns-banned-required", "--resume", "--out", str(out)]) == 0
    first = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert first["resumed"]["reused_runs"] == 0
    assert [c["channel"] for c in first["cases"][0]["channels"]] == ["instagram", "linkedin"]
    assert all(c["status"] == "ok" for c in first["cases"][0]["channels"])
    # same case, channels and mode: reused as is
    assert main(["eval", "run", "--mode", "mock", "--case", "sns-banned-required", "--resume", "--out", str(out)]) == 0
    again = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert again["resumed"]["reused_runs"] == 1 and again["cases"][0]["channels"] == first["cases"][0]["channels"]
    # a changed case file is run again
    cases = tmp_path / "cases"
    cases.mkdir()
    data = json.loads((CASES / "sns-banned-required.json").read_text(encoding="utf-8"))
    data["should"] = data["should"][:1]
    (cases / "sns-banned-required.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert main(["eval", "run", "--mode", "mock", "--cases", str(cases), "--resume", "--out", str(out)]) == 0
    assert "끝난 실행 0번" in capsys.readouterr().out


@pytest.mark.usefixtures("no_network")
def test_resume_counts_what_the_folder_already_spent_toward_the_cap(tmp_path):
    out = tmp_path / "live"
    first = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=out, max_cost_usd=50.0,
                                 case_ids=["very-short-brief"]), printer=lambda _line: None, backend_factory=PricedMock)
    spent = first["totals"]["cost_usd"]
    assert first["exit_code"] == 0 and spent > 0
    lines: list[str] = []
    resumed = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=out, max_cost_usd=spent, resume=True,
                                   case_ids=["very-short-brief", "naver-long-keyword"]),
                       printer=lines.append, backend_factory=PricedMock)
    # the cap covers the whole folder: the reused run already used all of it, so the new case never starts
    assert [c["status"] for c in resumed["cases"]] == ["ok", "skipped"]
    assert resumed["resumed"] == {"reused_runs": 1, "prior_cost_usd": pytest.approx(spent)}
    assert resumed["totals"]["cost_usd"] == pytest.approx(spent) and resumed["stopped"] and resumed["exit_code"] == 1
    assert any("이미 쓴 비용" in line for line in lines)


class UnpricedMock(MockBackend):
    """Reports tokens from a model the price table does not know (its cost is metered as $0)."""

    served = "claude-new-6"

    def _usage(self, agent, task, prompt, output):  # noqa: ANN001
        if self.on_usage is not None:
            self.on_usage(UsageRecord(agent=agent, task=task, model=self.served, input_tokens=200_000, output_tokens=20_000))


class FallbackToUnpriced(UnpricedMock):
    served = "claude-mystery-9"  # the requested model is priced, but responses come from an unpriced one


@pytest.mark.usefixtures("no_network")
def test_live_refuses_an_unpriced_model_and_stops_on_unpriced_usage(tmp_path, capsys, monkeypatch):
    options = dict(mode="live", cases_dir=CASES, max_cost_usd=0.5, case_ids=["very-short-brief", "naver-long-keyword"])
    with pytest.raises(UsageError, match="가격을 몰라서"):
        run_eval(EvalOptions(model="claude-new-6", out_dir=tmp_path / "a", **options), printer=lambda _line: None,
                 backend_factory=UnpricedMock)
    assert not (tmp_path / "a").exists()
    assert main(["eval", "run", "--mode", "live", "--model", "claude-new-6", "--case", "very-short-brief",
                 "--max-cost-usd", "1", "--out", str(tmp_path / "b")]) == 2
    assert "INSIA_PRICE_CLAUDE_NEW_6_INPUT" in capsys.readouterr().err
    assert main(["eval", "run", "--mode", "live", "--model", "claude-new-6", "--dry-run", "--case", "very-short-brief"]) == 0
    assert "시작하지 않아요" in capsys.readouterr().out
    # with a price the refusal is gone (the next check is credentials)
    monkeypatch.setenv("INSIA_PRICE_CLAUDE_NEW_6_INPUT", "5")
    monkeypatch.setenv("INSIA_PRICE_CLAUDE_NEW_6_OUTPUT", "25")
    assert main(["eval", "run", "--mode", "live", "--model", "claude-new-6", "--case", "very-short-brief",
                 "--max-cost-usd", "1", "--out", str(tmp_path / "b")]) == 1
    assert "자격 증명" in capsys.readouterr().err
    # responses from an unpriced model (a fallback, say): the eval stops after that case and skips the rest
    summary = run_eval(EvalOptions(out_dir=tmp_path / "c", **options), printer=lambda _line: None,
                       backend_factory=FallbackToUnpriced)
    assert [c["status"] for c in summary["cases"]][1] == "skipped"
    assert summary["cases"][0]["unpriced_models"] == ["claude-mystery-9"]
    assert "가격을 모르는 모델" in summary["stopped"] and summary["exit_code"] == 1


class HangingMock(MockBackend):
    """Blocks inside the first call until the test releases it (a hung API call)."""

    release = threading.Event()

    def _usage(self, agent, task, prompt, output):  # noqa: ANN001
        self.release.wait(10)


@pytest.mark.usefixtures("no_network")
def test_live_timeout_stops_a_hung_run(tmp_path):
    HangingMock.release.clear()
    started = time.monotonic()
    try:
        summary = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=tmp_path / "t", max_cost_usd=5.0,
                                       timeout_s=0.2, case_ids=["very-short-brief"]),
                           printer=lambda _line: None, backend_factory=HangingMock)
    finally:
        HangingMock.release.set()
    assert time.monotonic() - started < 3
    case = summary["cases"][0]
    assert case["status"] == "timeout" and case["failures"][0]["class"] == "timeout"
    assert case["channels"][0]["must"] and all(o.get("error") for o in case["channels"][0]["must"])
    assert summary["exit_code"] == 1
    with pytest.raises(UsageError, match="timeout"):
        run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=tmp_path / "u", timeout_s=0), printer=lambda _line: None)
