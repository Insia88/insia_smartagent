from __future__ import annotations

from insia_agents.channels import CHANNELS, check_format, chars_no_space, finalize_review
from insia_agents.models import Brief, Draft, Review, ReviewIssue, RubricScore


def _by_id(checks):
    return {c.id: c for c in checks}


def _linkedin(hook: str, tags: list[str], body_len: int = 1500, link: bool = False) -> Draft:
    filler = ("짧은 문단으로 관점을 전합니다. " * 200)[:body_len]
    content = f"{hook}\n\n{filler}\n\n{'자세히: https://example.com' if link else ''}\n\n여러분은 어떠세요?\n\n{' '.join(tags)}"
    return Draft(channel="linkedin", round=0, title="t", content=content, hashtags=tags)


def test_linkedin_checks():
    ok = _by_id(check_format(_linkedin("첫 줄\n둘째 줄", ["#a", "#b", "#c"])))
    assert all(c.passed for c in ok.values())
    bad = _by_id(check_format(_linkedin("가" * 150 + "\n" + "나" * 100, ["#a"] * 6, link=True)))
    assert not bad["hook_length"].passed
    assert not bad["hashtags"].passed
    assert not bad["no_link"].passed
    short = _by_id(check_format(_linkedin("훅\n둘째", ["#a", "#b", "#c"], body_len=200)))
    assert not short["length"].passed


def test_naver_blog_checks():
    brief = Brief(topic="t", keywords=["AI 마케팅 자동화"])
    body = "도입부예요.\n\n" + "\n\n".join(f"## 소제목 {i}\n[이미지: 장면 {i}]\n" + "본문 문장이에요. " * 70 for i in range(3))
    draft = Draft(channel="naver_blog", round=0, title="AI 마케팅 자동화 시작하기", content=body, hashtags=[f"#t{i}" for i in range(6)])
    checks = _by_id(check_format(draft, brief))
    assert 1500 <= chars_no_space(body) <= 3000
    assert all(c.passed for c in checks.values()), checks
    worse = draft.model_copy(update={"title": "다른 제목", "hashtags": ["#a"]})
    checks = _by_id(check_format(worse, brief))
    assert not checks["title_keyword"].passed and not checks["tags"].passed


def test_instagram_checks():
    slides = "\n\n".join(f"### 슬라이드 {i} — 제목\n- 문구: 짧게\n- 비주얼: 배경\n- 대체텍스트: 설명" for i in range(1, 9))
    content = f"## 캐러셀\n\n{slides}\n\n## 캡션\n\n짧은 첫 줄이에요.\n\n저장해 두세요.\n#a #b #c"
    draft = Draft(channel="instagram", round=0, title="t", content=content, hashtags=["#a", "#b", "#c"])
    assert all(c.passed for c in check_format(draft))
    six = content.replace("### 슬라이드 7", "### 장면 7").replace("### 슬라이드 8", "### 장면 8")
    checks = _by_id(check_format(draft.model_copy(update={"content": six})))
    assert not checks["slides"].passed


def test_bizplan_checks():
    content = "# 계획\n\n## 1. 문제 인식\n## 2. 실현 가능성\n## 3. 성장전략\n## 4. 팀 구성\n" + "개조식 문장임. " * 500
    checks = _by_id(check_format(Draft(channel="bizplan", round=0, title="계획", content=content)))
    assert all(c.passed for c in checks.values())
    missing = content.replace("## 4. 팀 구성", "## 4. 기타")
    assert not _by_id(check_format(Draft(channel="bizplan", round=0, title="계획", content=missing)))["psst_팀구성"].passed


def test_finalize_review_recomputes_format_and_verdict():
    draft = _linkedin("가" * 150 + "\n" + "나" * 100, ["#a"])  # 2 of 4 format checks fail
    spec = CHANNELS["linkedin"]
    rubric = [RubricScore(id=i.id, label=i.label, score=i.max, max=i.max, comment="") for i in spec.rubric if i.id != "format"]
    rubric.append(RubricScore(id="format", label="형식", score=10, max=10, comment="LLM이 준 점수"))
    rubric.append(RubricScore(id="unknown", label="?", score=99, max=5, comment=""))
    review = Review(channel="linkedin", round=0, score=100, passed=True, rubric=rubric, issues=[], summary="")
    final = finalize_review(review, draft)
    fmt = next(r for r in final.rubric if r.id == "format")
    assert fmt.score == 5 and "첫 2줄" in fmt.comment
    assert [r.id for r in final.rubric] == [i.id for i in spec.rubric]
    assert final.score == 95 and final.passed
    critical = review.model_copy(update={"issues": [ReviewIssue(severity="critical", problem="출처 없는 수치", fix="삭제")]})
    assert not finalize_review(critical, draft).passed


def test_finalize_review_clamps_and_fills_missing():
    draft = Draft(channel="instagram", round=1, title="t", content="## 캐러셀\n\n## 캡션\n\n")
    review = Review(channel="bizplan", round=0, score=0, passed=True,
                    rubric=[RubricScore(id="hook", label="훅", score=999, max=25, comment="")], issues=[], summary="")
    final = finalize_review(review, draft, pass_score=10)
    assert final.channel == "instagram" and final.round == 1
    assert next(r for r in final.rubric if r.id == "hook").score == 25
    assert next(r for r in final.rubric if r.id == "slide_flow").comment == "검수 결과 누락"
    assert 0 <= final.score <= 100
