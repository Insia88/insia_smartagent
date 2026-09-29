"""Business-plan blind check (``channels.profile_checks``): team members' real
names and, since finding 30, the schools and employers in their backgrounds."""

from __future__ import annotations

from insia_agents.channels import check_format
from insia_agents.models import Draft, Profile, TeamMember

PROFILE = Profile(company_name="인시아", service_name="INSIA", team=[
    TeamMember(role="대표", name="김민수", background="카카오 출신 PM 7년, 고려대 졸업"),
    TeamMember(role="CTO", name="이영희", background="네이버 검색광고팀 10년, 토스 PO 3년"),
])


def _blind(content: str, channel: str = "bizplan", profile: Profile = PROFILE):
    draft = Draft(channel=channel, round=0, title="사업계획서", content=content, hashtags=[], used_finding_ids=[],
                  change_log=[])
    return next((c for c in check_format(draft, profile=profile) if c.id == "blind_names"), None)


def test_a_team_employer_or_school_in_the_plan_fails_the_blind_check():
    """Verifier's repro: the old mock line passed with '노출 없음'."""
    check = _blind("| 대표 | ○○○ | 대표 | 카카오 출신 PM 7년, 고려대 졸업 (자사 자료) |")
    assert not check.passed and check.value == "학교·직장명 2개(고려대, 카카오) 노출"
    check = _blind("□ (팀 구성) CTO는 네이버 검색광고팀 10년, 토스 PO 3년 경력을 보유")
    assert not check.passed and check.value == "학교·직장명 2개(네이버, 토스) 노출"
    both = _blind("대표 김민수 — 카카오 출신 PM 7년")
    assert not both.passed and both.value == "실명 1개 · 학교·직장명 1개(카카오) 노출"
    assert both.label == "블라인드(실명 미노출)"  # same id and label as before (dashboard, exports)


def test_platform_mentions_and_masked_text_pass():
    content = ("## 3. 성장전략\n네이버 블로그와 카카오톡 채널, 토스 결제를 연동해요. 네이버 검색광고 단가는 [확인 필요].\n"
               "## 4. 팀 구성\n| 대표 | ○○○ | 대표 | ○○ 출신 PM 7년, ○○대 졸업 (자사 자료) |\n"
               "| CTO | ○○○ | CTO | ○○ 검색광고팀 10년, ○○ PO 3년 (자사 자료) |\n인시아 대표가 직접 운영해요.")
    check = _blind(content)
    assert check.passed and check.value == "노출 없음"


def test_the_blind_check_is_for_business_plans_only():
    assert _blind("카카오 출신 PM 7년", channel="linkedin") is None
