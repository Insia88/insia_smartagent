"""Channel specifications: rubric weights and deterministic format checks.

The 검수 에이전트 (reviewer) scores every rubric item except ``format``; the
``format`` item is always computed here, in code, from ``check_format`` so the
same draft gets the same format score in live mode, mock mode and in Claude
Code runs (``python -m insia_agents check <draft.json>``).

Platform rules change. Edit the numbers in ``CHANNELS`` (and the matching
channel guide in ``prompts/channels/``) when a platform updates its policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Brief, ChannelId, Draft, FormatCheck, Profile, Review, RubricScore
from .prompt_loader import blind_leaks


@dataclass(frozen=True)
class RubricItem:
    id: str
    label: str
    max: int
    guide: str


@dataclass(frozen=True)
class ChannelSpec:
    id: ChannelId
    label: str
    short: str
    color: str
    rubric: tuple[RubricItem, ...]
    limits: dict[str, int] = field(default_factory=dict)

    @property
    def rubric_total(self) -> int:
        return sum(item.max for item in self.rubric)


FORMAT_ITEM_ID = "format"

BIZPLAN_HEADINGS: tuple[tuple[str, ...], ...] = (
    ("문제인식", "문제 인식"),
    ("실현가능성", "실현 가능성"),
    ("성장전략", "성장 전략"),
    ("팀구성", "팀 구성"),
)

CHANNELS: dict[ChannelId, ChannelSpec] = {
    "bizplan": ChannelSpec(
        id="bizplan",
        label="사업계획서",
        short="사업계획서",
        color="#3B5BDB",
        rubric=(
            RubricItem("problem", "문제인식", 20, "시장·고객의 페인포인트가 근거와 함께 구체적인가"),
            RubricItem("solution", "실현가능성", 20, "솔루션·차별성·개발 및 사업화 계획이 구체적이고 실행 가능한가"),
            RubricItem("scaleup", "성장전략", 20, "시장규모(TAM·SAM·SOM), 비즈니스 모델, 자금·로드맵이 논리적인가"),
            RubricItem("team", "팀 구성", 10, "대표자·팀 역량과 보유 인프라가 과제 수행과 연결되는가"),
            RubricItem("evidence", "근거·출처", 20, "모든 수치에 출처와 기준 시점이 있고 리서치 팩과 일치하는가"),
            RubricItem(FORMAT_ITEM_ID, "형식(자동)", 10, "PSST 4개 섹션, 분량 — 코드가 자동 채점"),
        ),
        limits={"min_chars_no_space": 3000, "max_chars_no_space": 15000},
    ),
    "naver_blog": ChannelSpec(
        id="naver_blog",
        label="네이버 블로그",
        short="블로그",
        color="#03C75A",
        rubric=(
            RubricItem("search_intent", "검색 의도·키워드", 20, "메인 키워드가 제목·도입부·소제목에 자연스럽게 배치되고 검색 의도에 답하는가"),
            RubricItem("originality", "경험·독창성", 20, "직접 경험·고유 정보·구체 사례가 있는가 (복붙형 정보글이 아닌가)"),
            RubricItem("readability", "가독성", 20, "짧은 문단, 소제목, 이미지 자리로 모바일에서 읽기 쉬운가"),
            RubricItem("accuracy", "정확성·출처", 20, "사실 주장이 리서치 팩과 일치하고 출처가 표기되었는가"),
            RubricItem("cta", "마무리·행동 유도", 10, "요약과 이웃추가·댓글·문의 등 다음 행동이 명확한가"),
            RubricItem(FORMAT_ITEM_ID, "형식(자동)", 10, "분량, 소제목, 이미지 자리, 태그 수 — 코드가 자동 채점"),
        ),
        limits={
            "min_chars_no_space": 1500,
            "max_chars_no_space": 3000,
            "min_headings": 3,
            "min_images": 3,
            "min_tags": 5,
            "max_tags": 10,
            "max_title_chars": 40,
        },
    ),
    "linkedin": ChannelSpec(
        id="linkedin",
        label="링크드인",
        short="링크드인",
        color="#0A66C2",
        rubric=(
            RubricItem("hook", "훅(첫 2줄)", 25, "'더 보기' 전에 보이는 첫 2줄이 멈춰 읽게 만드는가"),
            RubricItem("insight", "인사이트·전문성", 25, "데이터나 경험에서 나온 관점이 있고 독자에게 쓸모 있는가"),
            RubricItem("structure", "구조·스캔성", 15, "한두 문장 문단, 줄바꿈, 목록으로 훑어 읽히는가"),
            RubricItem("accuracy", "정확성·출처", 15, "사실 주장이 리서치 팩과 일치하는가"),
            RubricItem("cta", "대화 유도", 10, "댓글을 부르는 질문이나 명확한 다음 행동으로 끝나는가"),
            RubricItem(FORMAT_ITEM_ID, "형식(자동)", 10, "분량, 첫 2줄 길이, 해시태그 수, 본문 링크 — 코드가 자동 채점"),
        ),
        limits={
            "min_chars": 1300,
            "max_chars": 2000,
            "hard_max_chars": 3000,
            "max_hook_chars": 210,
            "min_hashtags": 3,
            "max_hashtags": 5,
        },
    ),
    "instagram": ChannelSpec(
        id="instagram",
        label="인스타그램",
        short="인스타",
        color="#E1306C",
        rubric=(
            RubricItem("hook", "훅(1번 슬라이드·캡션 첫 줄)", 25, "피드에서 스크롤을 멈추게 하는 첫 장과 첫 줄인가"),
            RubricItem("slide_flow", "캐러셀 흐름", 25, "장당 한 메시지로 끝까지 넘기게 만드는 흐름인가"),
            RubricItem("visual_direction", "비주얼 지시", 15, "장별 비주얼·레이아웃 지시와 대체 텍스트가 구체적인가"),
            RubricItem("accuracy", "정확성", 15, "사실 주장이 리서치 팩과 일치하는가"),
            RubricItem("cta", "저장·공유 유도", 10, "저장·공유·댓글·프로필 방문 등 행동 유도가 있는가"),
            RubricItem(FORMAT_ITEM_ID, "형식(자동)", 10, "슬라이드 수, 캡션 길이, 첫 줄 길이, 해시태그 수 — 코드가 자동 채점"),
        ),
        limits={
            "min_slides": 7,
            "max_slides": 10,
            "max_caption_chars": 2200,
            "max_hook_chars": 125,
            "min_hashtags": 3,
            "max_hashtags": 5,
        },
    ),
}


# ---------------------------------------------------------------------------
# Text measurement helpers (the single definition of "글자수")
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_H2 = re.compile(r"^##\s+\S", re.MULTILINE)
_IMAGE_SLOT = re.compile(r"\[이미지")
_SLIDE = re.compile(r"^###\s*슬라이드\s*\d+", re.MULTILINE)
_URL = re.compile(r"https?://", re.IGNORECASE)


def chars_with_space(text: str) -> int:
    """공백 포함 글자수: every character, trimmed at both ends."""
    return len(text.strip())


def chars_no_space(text: str) -> int:
    """공백 제외 글자수: every non-whitespace character."""
    return len(_WS.sub("", text))


def first_lines(text: str, n: int = 2) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[:n])


def section(text: str, heading: str) -> str:
    """Return the body under ``## <heading>`` up to the next ``## `` heading."""
    pattern = re.compile(rf"^##\s*{re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^##\s+\S", rest, re.MULTILINE)
    return (rest[: nxt.start()] if nxt else rest).strip()


def count_slides(text: str) -> int:
    return len(_SLIDE.findall(text))


def _check(cid: str, label: str, passed: bool, value: object, expected: str) -> FormatCheck:
    return FormatCheck(id=cid, label=label, passed=bool(passed), value=str(value), expected=expected)


def _range(value: int, lo: int, hi: int) -> bool:
    return lo <= value <= hi


# ---------------------------------------------------------------------------
# Deterministic format checks
# ---------------------------------------------------------------------------


def check_format(draft: Draft, brief: Brief | None = None, profile: Profile | None = None) -> list[FormatCheck]:
    """Run the channel's deterministic checks. Same input → same output.

    With a company ``profile`` three brand checks are added: no banned words
    (every channel), required phrases present (SNS channels only), and — for
    the business plan — no real team member names (블라인드 규정).
    """
    spec = CHANNELS[draft.channel]
    lim = spec.limits
    content = draft.content
    tags = [t for t in draft.hashtags if t.strip()]
    checks: list[FormatCheck] = []

    if draft.channel == "bizplan":
        n = chars_no_space(content)
        checks.append(_check(
            "length", "분량(공백 제외)", _range(n, lim["min_chars_no_space"], lim["max_chars_no_space"]),
            f"{n:,}자", f"{lim['min_chars_no_space']:,}~{lim['max_chars_no_space']:,}자",
        ))
        heading_text = " ".join(m.group(2).replace(" ", "") for m in _HEADING_LINE.finditer(content))
        for variants in BIZPLAN_HEADINGS:
            key = variants[0]
            found = any(v.replace(" ", "") in heading_text for v in variants)
            checks.append(_check(f"psst_{key}", f"'{variants[-1]}' 섹션", found, "있음" if found else "없음", "제목(#)으로 포함"))

    elif draft.channel == "naver_blog":
        n = chars_no_space(content)
        checks.append(_check(
            "length", "분량(공백 제외)", _range(n, lim["min_chars_no_space"], lim["max_chars_no_space"]),
            f"{n:,}자", f"{lim['min_chars_no_space']:,}~{lim['max_chars_no_space']:,}자",
        ))
        h = len(_H2.findall(content))
        checks.append(_check("headings", "소제목(##) 수", h >= lim["min_headings"], f"{h}개", f"{lim['min_headings']}개 이상"))
        imgs = len(_IMAGE_SLOT.findall(content))
        checks.append(_check("images", "이미지 자리 [이미지: …]", imgs >= lim["min_images"], f"{imgs}개", f"{lim['min_images']}개 이상"))
        checks.append(_check(
            "tags", "태그 수", _range(len(tags), lim["min_tags"], lim["max_tags"]),
            f"{len(tags)}개", f"{lim['min_tags']}~{lim['max_tags']}개",
        ))
        t = chars_with_space(draft.title)
        checks.append(_check("title_length", "제목 길이", 0 < t <= lim["max_title_chars"], f"{t}자", f"{lim['max_title_chars']}자 이하"))
        if brief and brief.keywords:
            kw = brief.keywords[0].strip()
            in_title = kw.replace(" ", "") in draft.title.replace(" ", "")
            checks.append(_check("title_keyword", "제목에 메인 키워드", in_title, "포함" if in_title else "없음", f"'{kw}' 포함"))

    elif draft.channel == "linkedin":
        n = chars_with_space(content)
        checks.append(_check(
            "length", "분량(공백 포함)", _range(n, lim["min_chars"], lim["max_chars"]),
            f"{n:,}자", f"{lim['min_chars']:,}~{lim['max_chars']:,}자 (최대 {lim['hard_max_chars']:,}자)",
        ))
        hook = chars_with_space(first_lines(content, 2))
        checks.append(_check("hook_length", "첫 2줄 길이", 0 < hook <= lim["max_hook_chars"], f"{hook}자", f"{lim['max_hook_chars']}자 이하"))
        checks.append(_check(
            "hashtags", "해시태그 수", _range(len(tags), lim["min_hashtags"], lim["max_hashtags"]),
            f"{len(tags)}개", f"{lim['min_hashtags']}~{lim['max_hashtags']}개",
        ))
        has_url = bool(_URL.search(content))
        checks.append(_check("no_link", "본문 외부 링크 없음", not has_url, "링크 있음" if has_url else "없음", "링크는 첫 댓글로"))

    elif draft.channel == "instagram":
        slides = count_slides(content)
        checks.append(_check(
            "slides", "캐러셀 슬라이드 수", _range(slides, lim["min_slides"], lim["max_slides"]),
            f"{slides}장", f"{lim['min_slides']}~{lim['max_slides']}장",
        ))
        caption = section(content, "캡션")
        c = chars_with_space(caption)
        checks.append(_check("caption_length", "캡션 길이", 0 < c <= lim["max_caption_chars"], f"{c:,}자", f"1~{lim['max_caption_chars']:,}자"))
        hook = chars_with_space(first_lines(caption, 1))
        checks.append(_check("hook_length", "캡션 첫 줄 길이", 0 < hook <= lim["max_hook_chars"], f"{hook}자", f"{lim['max_hook_chars']}자 이하"))
        checks.append(_check(
            "hashtags", "해시태그 수", _range(len(tags), lim["min_hashtags"], lim["max_hashtags"]),
            f"{len(tags)}개", f"{lim['min_hashtags']}~{lim['max_hashtags']}개",
        ))

    if profile is not None:
        checks.extend(profile_checks(draft, profile))

    return checks


def _norm_space(text: str) -> str:
    return _WS.sub("", text).lower()


def profile_checks(draft: Draft, profile: Profile) -> list[FormatCheck]:
    """Brand checks derived from the company profile (deterministic)."""
    # hashtags count too: they are part of what gets posted (the paste text ends with them)
    text = f"{draft.title}\n{draft.content}\n{' '.join(draft.hashtags)}"
    flat = _norm_space(text)
    checks: list[FormatCheck] = []

    banned = [w.strip() for w in profile.banned_words if w.strip()]
    if banned:
        hits = [w for w in banned if _norm_space(w) and _norm_space(w) in flat]
        checks.append(_check("banned_words", "금지 표현 없음", not hits, ", ".join(hits) if hits else "없음", "프로필의 금지 표현을 쓰지 않음"))

    if draft.channel != "bizplan":
        required = [w.strip() for w in profile.required_phrases if w.strip()]
        if required:
            missing = [w for w in required if _norm_space(w) not in flat]
            checks.append(_check("required_phrases", "필수 문구 포함", not missing, "누락: " + ", ".join(missing) if missing else "모두 포함", "프로필의 필수 문구를 넣음"))

    if draft.channel == "bizplan":
        names = [m.name.strip() for m in profile.team if m.name.strip() and len(m.name.strip()) >= 2]
        exposed = [n for n in names if _norm_space(n) in flat]
        # school/employer names from the team backgrounds, written in a career context ("카카오 출신")
        leaks = blind_leaks(f"{draft.title}\n{draft.content}", [m.background for m in profile.team],
                            keep=[profile.company_name, profile.service_name])
        found = ([f"실명 {len(exposed)}개"] if exposed else []) + ([f"학교·직장명 {len(leaks)}개({', '.join(leaks)})"] if leaks else [])
        checks.append(_check("blind_names", "블라인드(실명 미노출)", not found, " · ".join(found) + " 노출" if found else "노출 없음", "팀원 실명은 ○○로 가림"))

    return checks


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

DEFAULT_PASS_SCORE = 80


def format_score(checks: list[FormatCheck], max_points: int) -> int:
    if not checks:
        return max_points
    passed = sum(1 for c in checks if c.passed)
    return round(max_points * passed / len(checks))


def finalize_review(review: Review, draft: Draft, brief: Brief | None = None, pass_score: int = DEFAULT_PASS_SCORE,
                    profile: Profile | None = None) -> Review:
    """Make a review deterministic: recompute format, clamp scores, total, verdict.

    - Rubric items are matched to the channel spec by id; unknown ids are dropped,
      missing ids are added with 0 points and a comment saying so.
    - The ``format`` item is always replaced by the code-computed score.
    - ``score`` = round(100 * sum(score) / sum(max)).
    - ``passed`` = score >= pass_score and no critical issue.
    """
    spec = CHANNELS[draft.channel]
    checks = check_format(draft, brief, profile)
    given = {item.id: item for item in review.rubric}
    rubric: list[RubricScore] = []
    for item in spec.rubric:
        if item.id == FORMAT_ITEM_ID:
            failed = [c.label for c in checks if not c.passed]
            comment = "모든 형식 기준 충족" if not failed else "미충족: " + ", ".join(failed)
            rubric.append(RubricScore(id=item.id, label=item.label, score=format_score(checks, item.max), max=item.max, comment=comment))
            continue
        got = given.get(item.id)
        if got is None:
            rubric.append(RubricScore(id=item.id, label=item.label, score=0, max=item.max, comment="검수 결과 누락"))
        else:
            rubric.append(RubricScore(id=item.id, label=item.label, score=max(0, min(item.max, got.score)), max=item.max, comment=got.comment))
    total = sum(r.score for r in rubric)
    score = round(100 * total / spec.rubric_total)
    has_critical = any(i.severity == "critical" for i in review.issues)
    return review.model_copy(update={
        "channel": draft.channel,
        "round": draft.round,
        "rubric": rubric,
        "format_checks": checks,
        "score": score,
        "passed": score >= pass_score and not has_critical,
    })
