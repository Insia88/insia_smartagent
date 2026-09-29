"""Deterministic grounding checks: number claims, their evidence, citations, placeholders.

A *number claim* is a figure with a unit that states a fact: ``%``/``%p``,
``원``/``달러`` (with 천·만·억·조), ``명``, ``개``/``개사``/``개소``/``곳``,
``건``, ``배``, ``년``/``개월`` durations, including approximations with
``여`` (``3천여 곳``, ``5만여 명``: the value and anything up to the next step,
3,000~3,999). The unit must end the word or take a particle (``30%의``,
``3년간``, ``1만 원대``): ``3원칙``, ``4년제``, ``20개년`` are words, not
figures. Calendar years (``2024년``, ``'24년``) are reference dates, not
claims. Lines that only cite (``출처: …``, ``- [s1] …``), hashtag lines,
image slots (``[이미지: …]``) and carousel visual directions (``- 비주얼: …``)
are skipped.

Every claim gets one status (first match wins):

1. ``supported``: the same value (same unit family, rounding-consistent with
   the written precision: ``116만 개`` matches ``116.3만개``, ``60%`` does not
   match ``59.1%``) appears in the run's research pack (finding claims and
   notes, source titles — never ``gaps``, which lists what was *not*
   verified), the company profile or a user document (the brief is a
   request, not evidence: a number the user only heard of stays unsupported).
   A value the user's own document or profile leaves open (it says the value
   is ``검토 중``, ``확정 전``, ``미정``, ``가정``, or a ``목표``/``예정`` figure) is
   *tentative*: it supports only a sentence that keeps it open (an
   assumption marker below or the same ``검토 중``/``확정 전`` wording); stated
   as settled fact it is ``unsupported`` with ``tentative: true``. Research
   findings are never tentative (unverified items belong in ``gaps``);
2. ``flagged``: its sentence says ``확인 필요`` (the writer marked it as unverified);
3. ``assumed``: the sentence (a table row counts as one sentence, plus a
   ``※`` note right after the paragraph or table) says 가정·예시·가상·시나리오,
   or a plan word is attached to the figure itself: ``목표`` right before it
   (``목표 매출 3억 원``, ``1차년도 목표: …``; not ``목표 시장``/``목표 고객``,
   which name a market) or ``목표``/``예정``/``계획`` within the next three
   words (``2,000개 확보 목표``, ``5만 원으로 책정할 예정``). A plan word
   elsewhere in the sentence (``12월 출시 예정인 신제품은 고객 5,000명이 …``)
   does not make the figure an assumption. Markers are matched as words:
   ``가정용``, ``가정에서``, ``가정간편식``, ``가상화폐`` are not assumptions;
4. ``unsupported``: none of the above — a likely invented number.

Schedule points (``출시 2년 차``) and small whole counts up to 10
(``브리프 1건``, ``4개 채널``, ``대표 포함 3명``) are wording, not claims, and
are skipped (a limitation: an invented "3곳 중 1곳" is missed). A claim is *cited* when its paragraph
(or table row) carries a source: ``[s#]``/``[u#]``,
``출처``, ``자사 자료``, ``…에 따르면``, a report title in 「」, ``같은 조사``
(continuing the previous citation), or an institution/publisher name (the
research pack's publishers, common Korean public bodies, names ending in
공단·협회·중앙회·위원회·연구원·진흥원 …). Platform names (네이버, 인스타그램,
링크드인, Meta …) are what SNS posts talk about, so they never count as a
citation on their own. It is *dated* when the paragraph has a year or ``기준``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..models import Brief, Draft, Profile, ResearchPack, UserDocument

# ---------------------------------------------------------------------------
# Number mentions
# ---------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT = r"천만|백만|십만|조|억|만|천"
_UNIT = r"%p|%포인트|퍼센트포인트|%|퍼센트|원|달러|명|개사|개월|개국|개소|개|곳|건|배|년"
# A unit ends the word or takes a particle/ending: "30%의", "3년간", "1만 원대", "5명당", "2배로" count;
# "3원칙", "4년제", "20개년", "2배달" are other words that happen to start with a unit.
_UNIT_TAIL = ("이|가|은|는|을|를|의|에|으|로|과|와|도|만|씩|대|쯤|당|째|간|나|면|라|인|입|예|였|뿐|차|치|다|요|"
              "부터|까지|보다|수준|정도|짜리|미만|초과|안팎|내외|규모|어치|동안|마다|밖에|조차|남짓|넘")
_UNIT_END = rf"(?=[^가-힣]|$|(?:{_UNIT_TAIL}))"
_GROUPS = rf"(?:{_NUM})\s?(?:{_MULT})(?:\s?(?:{_NUM})\s?(?:{_MULT})?)*|(?:{_NUM})"
_MENTION = re.compile(rf"(?<![\d.,A-Za-z])(?P<expr>{_GROUPS})(?P<approx>여)?\s?(?P<unit>{_UNIT}){_UNIT_END}")
# "10만~20만 원", "3~5개": the first endpoint borrows the unit of the second
_RANGE_HEAD = re.compile(rf"(?<![\d.,A-Za-z])(?P<expr>{_GROUPS})\s?[~∼～–]\s?(?={_GROUPS}(?:여)?\s?(?:{_UNIT}){_UNIT_END})")
_GROUP = re.compile(rf"(?P<num>{_NUM})\s?(?P<mult>{_MULT})?")

MULTIPLIERS = {"천": 1e3, "만": 1e4, "십만": 1e5, "백만": 1e6, "천만": 1e7, "억": 1e8, "조": 1e12}
UNIT_FAMILY = {
    "%": "percent", "퍼센트": "percent",
    "%p": "percent_point", "%포인트": "percent_point", "퍼센트포인트": "percent_point",
    "원": "krw", "달러": "usd",
    "명": "people",
    "개": "count", "개사": "count", "개소": "count", "곳": "count", "개국": "count",
    "건": "cases", "배": "times", "년": "years", "개월": "months",
}

# Hedges that cover the whole sentence (table row) and its ※ note.
ASSUMPTION_MARKERS = ("가정", "예시", "가상", "시나리오")
# Plan words that hedge only the figure they are attached to (see ``hedged``).
FORWARD_MARKERS = ("목표", "예정", "계획")
FLAG_MARKERS = ("확인 필요", "확인필요")
# How the user's own materials leave a value open ("요금은 월 29,000원을 검토 중이며 확정 전이에요").
TENTATIVE_SOURCE_MARKERS = ("검토 중", "검토중", "검토하", "확정 전", "확정되지", "미확정", "미정", "잠정")

# Markers are matched as words. Built-in ones list the compounds that mean something else.
_MARKER_WORDS = {
    "가정": r"가정(?!용|에서|의|식|집|간편식|폭력|법원|주부|환경|교육|방문|경제|내|형|학|사|통신|생활)"
            r"(?!\s(?:간편식|용품|내|방문|배달|배송|경제|폭력|법원|환경|교육|주부|형편|생활|의\s?달))",
    "예시": r"예시(?!적)",
    "가상": r"가상(?!화|자산|현실|공간|인간|계좌|머신|서버|통화|세계)(?!\s(?:화폐|자산|현실|공간|인간|계좌|머신|서버|통화|세계))",
    "시나리오": r"시나리오",
    "목표": r"목표(?!\s?(?:시장|고객|층|대상|사용자|이용자|타깃|타겟|국가|지역|업종|페르소나|소비자|세그먼트))",
    "예정": r"예정",
    "계획": r"계획(?!서)",  # 사업계획서 is the document, not a plan figure
}
# A custom marker (a case's ``markers``) must end the word or take a particle.
_MARKER_TAIL = r"(?=[^가-힣]|$|(?:이|가|은|는|을|를|의|에|으로|로|과|와|도|만|하|한|해|했|할|함|된|되|치|입니|예요|대로))"
_marker_cache: dict[str, re.Pattern[str]] = {}


def marker_pattern(marker: str) -> re.Pattern[str]:
    """Word-aware pattern for one marker (``가정`` does not match ``가정용``)."""
    pattern = _marker_cache.get(marker)
    if pattern is None:
        body = _MARKER_WORDS.get(marker) or (re.escape(marker) + _MARKER_TAIL)
        pattern = _marker_cache[marker] = re.compile(body)
    return pattern


def has_marker(text: str, markers: Iterable[str]) -> bool:
    return any(marker_pattern(m).search(text or "") for m in markers)


_TARGET_BEFORE = re.compile(r"^목표(?:치|액)?(?:는|은)?[:：]?$")  # "목표 매출 3억 원", "1차년도 목표: 매출 …", "매출 목표 | 1.2억 원"
_FORWARD_BEFORE_CHARS = 16
_FORWARD_AFTER_CHARS = 18
_FORWARD_WORDS = 3
_CLAUSE_BREAK = re.compile(r"[\d,;·.!?]")

_CITATION = re.compile(
    r"\[(?:s|u)\d+\]|출처|자사\s?자료|자사\s?집계|\(자사|사용자\s?제공|에\s?따르면|[「『]"  # markers, cited report titles
    r"|같은\s?(?:조사|자료|보고서|설문|실태조사|발표)"  # a sentence continuing the previous citation
    r"|[가-힣]{2,}(?:공단|협회|연합회|중앙회|위원회|연구소|연구원|진흥원|유통원|정보원|개발원|상공회의소)")  # institutions
_DATED = re.compile(r"(?:19|20)\d{2}|['’‘]\d{2}\s?년|기준|○○\s?년")
_INSTITUTIONS = (
    "통계청", "KOSIS", "국가통계포털", "중소벤처기업부", "중기부", "소상공인시장진흥공단", "소진공", "중소기업중앙회",
    "중기중앙회", "소상공인연합회", "대한상공회의소", "대한상의", "과학기술정보통신부", "과기정통부", "한국은행",
    "창업진흥원", "정보통신정책연구원", "소프트웨어정책연구소", "SPRi", "한국지능정보사회진흥원", "NIA",
    "한국인터넷진흥원", "KISA", "공정거래위원회", "고용노동부", "산업통상자원부", "문화체육관광부", "국세청",
    "금융감독원", "방송통신위원회", "방송미디어통신위원회", "한국방송광고진흥공사", "한국무역협회", "K-Startup",
    "OECD", "World Bank",
)
# Platform names are the topic of SNS posts ("인스타그램 팔로워가 30% 늘었어요"), not a source: they never count as a
# citation, even when a research source was published by the platform (cite it with [s#], 출처 or 「보고서」).
_PLATFORM_NAMES = {"네이버", "naver", "인스타그램", "instagram", "링크드인", "linkedin", "meta", "메타", "페이스북", "facebook",
                   "유튜브", "youtube", "카카오", "kakao", "google", "구글", "threads", "스레드", "틱톡", "tiktok"}
_PUBLISHER_NOISE = {"게시", "소관", "수행", "게재", "post", "by", "quoting", "대한민국", "정책브리핑", "뉴스", "공식블로그",
                    "blog", "the", "and", "search", "tech", "for", "developers", "네이트"} | _PLATFORM_NAMES

_SKIP_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\[(?:s|u)\d+\]|출처\s*[:：])|^\s*#[^\s#]|^\s*[-*]?\s*비주얼\s*[:：]")
_IMAGE_SLOT = re.compile(r"\[이미지[^\]]*\]")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_NOTE_LINE = re.compile(r"^\s*(?:[-*>]\s*)?※")


def _group_value(text: str, *, approx: bool = False) -> tuple[float, float]:
    """(value, precision) of ``"6조 3,009억"`` / ``"116.3만"`` / ``"1,073"``.

    With ``approx`` (``3천여``, ``100여``) the precision is the step the ``여`` covers: 3,000~3,999, 100~199."""
    total = 0.0
    precision = 1.0
    for match in _GROUP.finditer(text):
        raw = match.group("num").replace(",", "")
        mult = MULTIPLIERS.get(match.group("mult") or "", 1.0)
        total += float(raw) * mult
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        precision = (10 ** -decimals) * mult
        if approx and not decimals:
            digits = raw.rstrip("0")
            precision = mult * 10 ** (len(raw) - len(digits)) if digits else mult
    return total, precision


@dataclass
class Mention:
    text: str
    value: float
    unit: str
    family: str
    precision: float
    line: int
    sentence: str
    context: str
    kind: str = "claim"  # claim | date
    status: str = ""  # flagged | supported | assumed | unsupported (claims only)
    origins: list[str] = field(default_factory=list)
    soft_origins: list[str] = field(default_factory=list)  # origins where the source itself leaves the value open
    tentative: bool = False  # every origin is soft (the source says 검토 중·확정 전·가정 …)
    approx: bool = False  # "3천여 곳": value .. value + precision
    assumed: bool = False
    cited: bool = False
    dated: bool = False
    start: int = 0  # span of the figure within ``sentence``
    end: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sentence"] = _clip(self.sentence, 160)
        for key in ("context", "start", "end"):
            data.pop(key, None)
        return data

    @property
    def firm_origins(self) -> list[str]:
        return [o for o in self.origins if o not in self.soft_origins]


def _clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _is_calendar_year(match: re.Match[str], text: str, value: float, expr: str) -> bool:
    if match.group("unit") != "년":
        return False
    before = text[max(0, match.start() - 1):match.start()]
    if before in ("'", "’", "‘"):
        return True
    digits = expr.replace(",", "")
    return digits.isdigit() and len(digits) == 4 and 1900 <= value <= 2100


_ORDINAL_AFTER = re.compile(r"\s?차(?![가-힣])|\s?차[이의에]")  # "출시 2년 차", "협약 3개월 차에"
_TRIVIAL_FAMILIES = frozenset({"count", "people", "cases"})
SMALL_COUNT = 10  # whole counts up to this (개·곳·명·건) are how sentences are built, rarely statistics


def _mentions_in(text: str, *, line: int = 0, sentence: str = "", context: str = "") -> list[Mention]:
    found: list[Mention] = []
    for match in _MENTION.finditer(text):
        expr, unit = match.group("expr"), match.group("unit")
        approx = bool(match.group("approx"))
        value, precision = _group_value(expr, approx=approx)
        if unit in ("년", "개월") and _ORDINAL_AFTER.match(text, match.end()):
            continue  # a point in a schedule, not a duration claim
        if UNIT_FAMILY[unit] in _TRIVIAL_FAMILIES and precision == 1.0 and value <= SMALL_COUNT and value.is_integer():
            continue  # "브리프 1건", "대표자 1명", "4개 채널", "3개 에이전트": wording, not a statistic
        mention = Mention(text=match.group(0).strip(), value=value, unit=unit, family=UNIT_FAMILY[unit],
                          precision=precision, line=line, sentence=sentence or text, context=context or text,
                          approx=approx, start=match.start(), end=match.end())
        if _is_calendar_year(match, text, value, expr):
            mention.kind = "date"
        found.append(mention)
    for match in _RANGE_HEAD.finditer(text):  # the first endpoint of a range takes the second one's unit
        tail = _MENTION.match(text, match.end()) or _MENTION.search(text, match.end())
        if tail is None or tail.start() > match.end() + 1:
            continue
        unit = tail.group("unit")
        if unit in ("년", "개월") and _ORDINAL_AFTER.match(text, tail.end()):
            continue  # "출시 2~3년 차"
        expr = match.group("expr")
        value, precision = _group_value(expr)
        if UNIT_FAMILY[unit] in _TRIVIAL_FAMILIES and precision == 1.0 and value <= SMALL_COUNT and value.is_integer():
            continue  # "3~5개"
        head = Mention(text=expr.strip() + unit, value=value, unit=unit, family=UNIT_FAMILY[unit],
                       precision=precision, line=line, sentence=sentence or text, context=context or text,
                       start=match.start(), end=tail.end())
        digits = expr.replace(",", "")
        if unit == "년" and digits.isdigit() and len(digits) == 4 and 1900 <= value <= 2100:
            head.kind = "date"  # "2023~2024년"
        found.append(head)
    return found


def _split_sentences(text: str) -> list[str]:
    return [s for line in (text or "").splitlines() for s in _SENTENCE_END.split(line) if s.strip()]


def _source_is_open(mention: Mention, origin: str) -> bool:
    """Does the evidence sentence itself leave this value open?

    Only the user's own materials (documents, profile) can: when they call the value an assumption, a plan figure
    or undecided (``검토 중``, ``확정 전``, ``미정``). Research findings are the evidence base itself (unverified items
    go to ``gaps``, which never count), and a published forecast is a fact about that forecast."""
    if not origin.startswith(("document:", "profile")):
        return False
    return hedged(mention) or has_marker(mention.sentence, FLAG_MARKERS) or \
        any(m in mention.sentence for m in TENTATIVE_SOURCE_MARKERS)


def _values(texts: Iterable[tuple[str, str]]) -> dict[str, list[tuple[float, str, bool]]]:
    """Evidence numbers by unit family: ``{family: [(value, origin, open)]}`` (dates skipped)."""
    table: dict[str, list[tuple[float, str, bool]]] = {}
    for origin, text in texts:
        for sentence in _split_sentences(text):
            for mention in _mentions_in(sentence):
                if mention.kind == "claim":
                    table.setdefault(mention.family, []).append((mention.value, origin, _source_is_open(mention, origin)))
    return table


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def profile_texts(profile: Profile | None) -> list[str]:
    if profile is None:
        return []
    texts: list[str] = []
    for name, value in profile.model_dump(exclude={"updated_at", "team"}).items():
        if isinstance(value, str) and value.strip():
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(str(v) for v in value if str(v).strip())
    for member in profile.team:
        texts.extend(t for t in (member.role, member.background) if t.strip())
    return texts


@dataclass
class Evidence:
    """Where a number may come from: the research pack, the company profile and user documents
    (the brief only with ``include_brief``). ``publishers`` are names that count as a citation."""

    values: dict[str, list[tuple[float, str, bool]]]
    publishers: list[str]

    @classmethod
    def build(cls, research: ResearchPack | None, *, profile: Profile | None = None,
              documents: Iterable[UserDocument | Any] = (), brief: Brief | None = None,
              include_brief: bool = False) -> "Evidence":
        """Facts may come only from the research pack, the company profile and user documents (CLAUDE.md);
        the brief is a request, so its numbers (hearsay in the notes, say) are not evidence unless
        ``include_brief`` is set."""
        texts: list[tuple[str, str]] = []
        publishers: list[str] = []
        if research is not None:
            for finding in research.findings:
                texts.append((f"research:{finding.id}", f"{finding.claim}\n{finding.note}"))
            for source in research.sources:
                origin = "document" if source.origin == "user" else "research"
                texts.append((f"{origin}:{source.id}", source.title))
                publishers.extend(_publisher_tokens(source.publisher))
                if source.origin == "user":
                    publishers.extend(_publisher_tokens(source.title))
        texts.extend(("profile", text) for text in profile_texts(profile))
        for doc in documents:
            doc_id = getattr(doc, "id", "") or "doc"
            texts.append((f"document:{doc_id}", getattr(doc, "text", "") or ""))
        if brief is not None and include_brief:
            texts.extend(("brief", t) for t in (brief.topic, brief.goal, brief.audience, brief.notes, brief.tone,
                                                 " ".join(brief.keywords)) if t)
        if profile is not None:
            publishers.extend(t for t in (profile.company_name.strip(), profile.service_name.strip()) if len(t) >= 2)
        return cls(values=_values(texts), publishers=sorted(set(publishers), key=len, reverse=True))

    def match(self, mention: Mention) -> tuple[list[str], list[str]]:
        """(firm origins, open origins) of the evidence values this figure matches."""
        eps = 1e-9 * max(1.0, abs(mention.value))
        if mention.approx:  # "3천여 곳" covers 3,000 up to (not including) 4,000
            def hit(value: float) -> bool:
                return mention.value - eps <= value < mention.value + mention.precision - eps
        else:
            tolerance = mention.precision / 2 + eps

            def hit(value: float) -> bool:
                return abs(value - mention.value) <= tolerance
        firm: list[str] = []
        soft: list[str] = []
        for value, origin, is_open in self.values.get(mention.family, ()):
            if hit(value):
                (soft if is_open else firm).append(origin)
        firm = list(dict.fromkeys(firm))
        return firm, [o for o in dict.fromkeys(soft) if o not in firm]

    def origins(self, mention: Mention) -> list[str]:
        firm, soft = self.match(mention)
        return firm + soft


def _publisher_tokens(publisher: str) -> list[str]:
    tokens = []
    for token in re.split(r"[()\[\],·:/&]|\s{1,}", publisher or ""):
        token = token.strip()
        if len(token) >= 2 and token.lower() not in _PUBLISHER_NOISE:
            tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# Draft analysis
# ---------------------------------------------------------------------------


def _blocks(lines: list[str]) -> list[str]:
    """For each line, the ``※`` note lines right after its paragraph/table (at most one blank line between)."""
    notes = [""] * len(lines)
    i = 0
    while i < len(lines):
        start = i
        if lines[i].lstrip().startswith("|"):
            while i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|"):
                i += 1
        end = i
        j = end + 1
        if j < len(lines) and not lines[j].strip():
            j += 1
        collected = []
        while j < len(lines) and _NOTE_LINE.match(lines[j]):
            collected.append(lines[j])
            j += 1
        if collected:
            text = "\n".join(collected)
            for k in range(start, end + 1):
                notes[k] = text
        i = end + 1
    return notes


def _has(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _forward_attached(mention: Mention, markers: Iterable[str]) -> bool:
    """A plan word attached to the figure: ``목표`` just before it, or ``목표/예정/계획`` in the next three words.

    Windows stop at another number or a clause break, so "12월 출시 예정인 신제품은 고객 5,000명이 사전 신청했어요"
    does not hedge 5,000명."""
    sentence = mention.sentence
    if sentence.lstrip().startswith("|"):  # a table row's label cells ("| 매출 목표 | 1.2억 원 | 3.9억 원 |") cover the row
        for cell in sentence.strip().strip("|").split("|"):
            if re.search(r"\d", cell):
                break
            if has_marker(cell, markers):
                return True
    after = _CLAUSE_BREAK.split(sentence[mention.end:mention.end + _FORWARD_AFTER_CHARS], maxsplit=1)[0]
    words = after.replace("|", " ").split()[:_FORWARD_WORDS]
    for marker in markers:
        pattern = marker_pattern(marker)
        if any(pattern.match(w) or (i == 0 and pattern.search(w)) for i, w in enumerate(words)):
            return True
    if "목표" in markers:
        before = _CLAUSE_BREAK.split(sentence[max(0, mention.start - _FORWARD_BEFORE_CHARS):mention.start])[-1]
        tokens = before.replace("|", " ").split()
        for index in range(max(0, len(tokens) - _FORWARD_WORDS), len(tokens)):
            # "목표 시장", "목표 고객" name a market or a customer group, not a target figure
            if _TARGET_BEFORE.match(tokens[index]) and marker_pattern("목표").match(" ".join(tokens[index:])):
                return True
    return False


def hedged(mention: Mention, markers: Iterable[str] = ASSUMPTION_MARKERS + FORWARD_MARKERS) -> bool:
    """Is this figure marked as an assumption/plan by ``markers``?

    Whole-sentence markers (가정, 예시 …; a case's custom markers) count anywhere in the sentence, its table row or
    the ``※`` note after it; plan words (목표, 예정, 계획) only when attached to the figure (``_forward_attached``) or
    when a ``※`` note starts with them."""
    markers = tuple(markers)
    forward = tuple(m for m in markers if m in FORWARD_MARKERS)
    anywhere = tuple(m for m in markers if m not in FORWARD_MARKERS)
    if anywhere and has_marker(mention.context, anywhere):
        return True
    if not forward:
        return False
    note = mention.context[len(mention.sentence):]
    if note and re.search(r"※\s*(?:" + "|".join(forward) + ")", note):
        return True
    return _forward_attached(mention, forward)


def analyze_numbers(draft: Draft, evidence: Evidence) -> list[Mention]:
    """Every number mention in the draft's body (and title), with status, origins, citation and date flags."""
    lines = [draft.title, ""] + draft.content.splitlines()
    notes = _blocks(lines)
    mentions: list[Mention] = []
    for index, raw in enumerate(lines):
        if not raw.strip() or _SKIP_LINE.search(raw):
            continue
        line = _IMAGE_SLOT.sub(" ", raw)
        is_row = line.lstrip().startswith("|")
        sentences = [line] if is_row else [s for s in _SENTENCE_END.split(line) if s.strip()]
        for sentence in sentences:
            context = sentence + ("\n" + notes[index] if notes[index] else "")
            for mention in _mentions_in(sentence, line=max(0, index - 1), sentence=sentence, context=context):
                if mention.kind != "claim":
                    mentions.append(mention)
                    continue
                firm, soft = evidence.match(mention)
                mention.origins, mention.soft_origins = firm + soft, soft
                mention.tentative = bool(soft) and not firm
                mention.assumed = hedged(mention)
                # a value the source leaves open is supported only when the draft keeps it open too
                relayed = mention.tentative and (mention.assumed or _has(context, TENTATIVE_SOURCE_MARKERS))
                if firm or relayed:
                    mention.status = "supported"
                elif has_marker(context, FLAG_MARKERS):
                    mention.status = "flagged"
                elif mention.assumed:
                    mention.status = "assumed"
                else:
                    mention.status = "unsupported"
                # a source or date anywhere in the same paragraph (or table row) counts as "next to" the number
                mention.cited = bool(_CITATION.search(line)) or _has(line, _INSTITUTIONS) or _has(line, evidence.publishers)
                mention.dated = bool(_DATED.search(line))
                mentions.append(mention)
    return mentions


def grounding_summary(mentions: list[Mention]) -> dict[str, Any]:
    claims = [m for m in mentions if m.kind == "claim"]
    counts = {status: sum(1 for m in claims if m.status == status) for status in ("supported", "assumed", "flagged", "unsupported")}
    factual = [m for m in claims if m.status in ("supported", "unsupported")]  # claims that need a source
    cited = sum(1 for m in factual if m.cited)
    dated = sum(1 for m in factual if m.dated)
    return {
        "claims": len(claims),
        **counts,
        "dates": sum(1 for m in mentions if m.kind == "date"),
        "factual": len(factual),
        "cited": cited,
        "dated": dated,
        "citation_coverage": round(cited / len(factual), 3) if factual else None,
        "date_coverage": round(dated / len(factual), 3) if factual else None,
    }


# ---------------------------------------------------------------------------
# Placeholders and plain text search
# ---------------------------------------------------------------------------

_PH_CIRCLE = re.compile(r"○+")
_PH_BRACKET = re.compile(r"\[(?!(?:s|u)\d+\])(?!이미지)(?!데모\])([^\[\]\n]{1,80})\](?!\()")
_HANGUL = re.compile(r"[가-힣]")


def placeholders(draft: Draft) -> dict[str, Any]:
    """``○○`` runs and bracket placeholders (``[대표자 성명]``, ``[확인 필요: …]``); citations, image slots and ``[데모]`` excluded."""
    text = f"{draft.title}\n{draft.content}"
    brackets = [m.group(0) for m in _PH_BRACKET.finditer(text) if _HANGUL.search(m.group(1))]
    outside = _PH_BRACKET.sub(" ", text)
    circles = _PH_CIRCLE.findall(outside)
    examples = list(dict.fromkeys(brackets))[:5] + (["○○"] if circles else [])
    return {"count": len(brackets) + len(circles), "brackets": len(brackets), "circles": len(circles), "examples": examples}


def _flat(text: str) -> str:
    return re.sub(r"\s", "", text or "").lower()


def draft_text(draft: Draft) -> str:
    """What gets posted: title, body and hashtags."""
    return f"{draft.title}\n{draft.content}\n{' '.join(draft.hashtags)}"


def contains(draft: Draft, value: str) -> bool:
    """Spacing- and case-insensitive search over title, body and hashtags (like the brand checks)."""
    needle = _flat(value)
    return bool(needle) and needle in _flat(draft_text(draft))
