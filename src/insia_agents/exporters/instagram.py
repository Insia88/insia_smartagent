"""Instagram carousel export: 1080×1350 slide images + caption + alt text.

Slides come from the ``## 캐러셀`` section (``### 슬라이드 N — 제목`` followed
by ``- 문구:`` / ``- 비주얼:`` / ``- 대체텍스트:`` / optional ``- 출처:``). The
문구 is the on-image text: the part before the first `` / `` is the big
headline, the rest are smaller lines. Colors come from ``profile.brand_colors``
(first = main color) or the INSIA palette.

Labels are matched leniently (``- **문구:**``, ``문구:``, ``1. 문구:``, ``- 헤드라인:``,
``- 대체 텍스트(alt):``, ``비주얼 지시사항:``); design directions and memos never
end up on the image, nor does an unknown label written like the slide's field
lines (see ``_parse_slide_body``). The zip notes list slides without
문구/대체텍스트, skipped labels and slides whose text did not fit.

PNG rendering uses Playwright + Chromium when available (optional extra
``[render]``; ``INSIA_CHROMIUM`` overrides the browser executable,
``INSIA_RENDER=0`` turns rendering off). Otherwise the zip carries
``slides.html`` with print-ready 1080×1350 pages and a README note.
"""

from __future__ import annotations

import html
import io
import os
import re
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..models import Draft, Profile
from .common import EXTRA_HINT, ExportError, clean_draft, content_section, strip_inline, xml_safe
from .text import instagram_caption

SLIDE_WIDTH = 1080
SLIDE_HEIGHT = 1350
INSIA_PALETTE = ("#0B1220", "#6D5EF5", "#F5C451")  # dark base, main, accent
FONT_STACK = ('"Pretendard", "Noto Sans KR", "Noto Sans CJK KR", "Apple SD Gothic Neo", "Malgun Gothic", '
              '"맑은 고딕", "NanumGothic", "나눔고딕", "NanumBarunGothic", "WenQuanYi Zen Hei", sans-serif')
RENDER_TIMEOUT_MS = 45_000  # per browser step
RENDER_DEADLINE_S = 150  # whole render; a hung browser must never block an export request
JPEG_QUALITY = 90  # Instagram API publishing (render_images(..., image_type="jpeg"))
# Without a browser we cannot measure; text longer than this is flagged (the
# channel guide asks for 25 어절 or fewer per slide).
LONG_TEXT_WORDS = 80
LONG_SOURCE_WORDS = 120


class RenderUnavailable(RuntimeError):
    """PNG rendering is not possible here; ``str(exc)`` is a Korean reason."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _plain(text: str) -> str:
    """On-image text: markdown bold markers never show up literally."""
    return strip_inline(text or "").replace("**", "").strip()


@dataclass(frozen=True)
class Slide:
    number: int
    title: str
    text: str  # 문구 (on-image text)
    visual: str = ""
    alt: str = ""
    source: str = ""
    skipped: tuple[str, ...] = ()  # labelled lines the parser did not recognize (kept off the image)

    @property
    def parts(self) -> list[str]:
        """Headline first, then the smaller lines (split on " / " or line breaks)."""
        raw = self.text or self.title
        parts = [_plain(p) for p in re.split(r"\s+/\s+|\n", raw)]
        parts = [p for p in parts if p]
        return parts or [_plain(self.title) or f"슬라이드 {self.number}"]


_SLIDE_HEADING = re.compile(r"^###\s*슬라이드\s*(\d+)\s*(?:[—–\-:|·]\s*)?(.*)$", re.MULTILINE)
# "- 문구: …", "- **문구:** …", "- **문구**: …", "**문구:** …", "문구: …", "1. 문구: …" (label up to 20 chars)
# A "*" is a bullet only when a space follows ("**문구:**" is bold, not a bullet).
_FIELD = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-•]\s*|\*\s+|\d{1,2}[.)]\s*)?"
                    r"(?P<label>[^:：\n]{1,20})[:：](?P<gap>\s?)(?P<value>.*)$")
_LIST_MARKER = re.compile(r"^[-*•]\s+")
# Parenthesized notes, markdown, emoji and punctuation around a label: "📌 문구", "대체 텍스트(alt)".
_LABEL_NOISE = re.compile(r"\(.*?\)|（.*?）|[\W_]")
_TEXT, _SUB, _HEADING_TEXT, _SKIP = "text", "sub", "heading", ""
_LABELS = {
    # on-image headline / main copy
    **dict.fromkeys(("문구", "카피", "텍스트", "화면문구", "이미지문구", "헤드라인", "메인문구", "메인카피", "제목문구",
                     "헤드카피", "큰문구", "이미지텍스트", "화면텍스트", "카드문구", "슬라이드문구", "문안",
                     "text", "copy", "headline"), _TEXT),
    # smaller on-image lines under the headline
    **dict.fromkeys(("서브문구", "보조문구", "서브카피", "서브헤드", "서브", "부제", "부제목", "작은문구", "본문",
                     "subtext", "subhead", "subheadline", "body", "cta"), _SUB),
    # "- 제목:" is the headline only on a slide without 문구 (otherwise it is the slide's own title)
    **dict.fromkeys(("제목", "타이틀", "title"), _HEADING_TEXT),
    # design directions (never on the image; listed in README.txt)
    **dict.fromkeys(("비주얼", "디자인", "레이아웃", "비주얼지시", "디자인지시", "이미지", "이미지지시", "배경", "색상",
                     "아이콘", "그래픽", "사진", "레이아웃메모", "디자인메모", "visual", "design", "layout", "image"),
                    "visual"),
    **dict.fromkeys(("대체텍스트", "대체문구", "대체", "이미지설명", "alt", "alttext"), "alt"),
    **dict.fromkeys(("출처", "근거", "자료출처", "데이터출처", "source", "sources"), "source"),
    # notes for whoever makes the slide (never on the image)
    **dict.fromkeys(("톤", "톤앤매너", "tone", "해시태그", "캡션"), _SKIP),
}
# Labels that describe how to make a slide, wherever they appear (even
# unbulleted or indented under 문구): "비주얼 지시사항", "디자이너 메모".
_META_PREFIXES = (("비주얼", "visual"), ("디자인", "visual"), ("레이아웃", "visual"), ("배경", "visual"),
                  ("색상", "visual"), ("컬러", "visual"), ("폰트", "visual"), ("서체", "visual"),
                  ("visual", "visual"), ("design", "visual"), ("layout", "visual"),
                  ("대체", "alt"), ("접근성", "alt"), ("alt", "alt"))
_META_SUFFIXES = (("지시사항", "visual"), ("지시", "visual"), ("디렉션", "visual"),
                  ("direction", "visual"), ("directions", "visual"),
                  ("메모", _SKIP), ("노트", _SKIP), ("note", _SKIP), ("notes", _SKIP), ("memo", _SKIP))
_TOP, _NESTED = 0, 1


def _normal_label(label: str) -> str:
    return _LABEL_NOISE.sub("", label).lower()


def _field_key(label: str) -> str | None:
    """Field a label belongs to: text/sub/heading/visual/alt/source, "" (a note
    that never goes on the image) or None (unknown)."""
    name = _normal_label(label)
    if not name:
        return None
    if name in _LABELS:
        return _LABELS[name]
    for prefix, key in _META_PREFIXES:
        if name.startswith(prefix):
            return key
    for suffix, key in _META_SUFFIXES:
        if name.endswith(suffix):
            return key
    return None


def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def _structure(field_match: re.Match, base_indent: int = 0) -> tuple[str, int]:
    """(marker kind, depth) of a labelled line: "- 문구:" → ("bullet", top)."""
    marker = (field_match.group("marker") or "").strip()
    kind = "bullet" if marker[:1] in ("-", "*", "•") else "number" if marker else "plain"
    depth = _NESTED if _indent(field_match.string) - base_indent >= 2 else _TOP
    return kind, depth


def _label_like(field_match: re.Match) -> str | None:
    """The normalized label when ``라벨: 값`` reads like a field label, or None
    when it is more likely content ("1단계: 목표", "오후 2:00", "https://…", "Q: …")."""
    value = field_match.group("value")
    # a label is followed by a space ("메모: …"); "2:00" or "https://" are not labels
    if value.startswith("//") or (value and not field_match.group("gap") and not value.startswith("*")):
        return None
    label = _LABEL_NOISE.sub("", field_match.group("label"))
    if len(label) < 2 or len(label) > 12 or any(ch.isdigit() for ch in label):
        return None
    return label


def _field_value(label: str, value: str) -> str:
    """Value after the label, without the bold that wrapped the label
    (``- **문구:** …`` or ``- **문구: …**``)."""
    value = value.strip()
    label = label.strip()
    if label.startswith("*") and not label.endswith("*"):
        value = re.sub(r"^\*+\s*", "", value)
        if value.endswith("**") and value.count("**") % 2 == 1:
            value = value[:-2].rstrip()
    return value


def _parse_slide_body(body: str) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """Fields, loose lines (before any label) and skipped labels of one slide.

    A labelled line is a field when its label is known (``_LABELS``, or a
    design/memo label such as "비주얼 지시사항", "디자이너 메모"). An unknown
    label on a line shaped like the slide's field lines — same marker (``-``,
    ``1.`` or none) and depth — is a sibling field the parser does not know:
    it is skipped (never drawn on the image) and reported. Other lines are
    content of the field above ("  - 목표: …" nested under 문구).
    """
    lines = [line for line in body.splitlines() if line.strip()]
    matches = []
    for line in lines:
        field_match = _FIELD.match(line)
        if field_match and field_match.group("value").startswith("//"):
            field_match = None  # "https://…" is not a label
        matches.append(field_match)
    base = min((_indent(line) for line in lines), default=0)
    # shapes of the slide's field lines ("- 문구:", "**문구:**", "1. 문구:"); nested lines are content
    field_shapes = {shape for shape in (_structure(m, base) for m in matches
                                        if m and _field_key(m.group("label")) is not None) if shape[1] == _TOP}
    if not field_shapes:  # no known label at all: only top-level "- 라벨:" lines read as fields
        field_shapes = {("bullet", _TOP)}

    fields: dict[str, list[str]] = {_TEXT: [], _SUB: [], _HEADING_TEXT: [], "visual": [], "alt": [], "source": []}
    current: str | None = None  # field that plain follow-up lines belong to; "" = a skipped field
    loose: list[str] = []
    skipped: list[str] = []
    for line, field_match in zip(lines, matches):
        key = _field_key(field_match.group("label")) if field_match else None
        if key is None and field_match and _structure(field_match, base) in field_shapes and _label_like(field_match):
            key = _SKIP  # "메모: …" next to 문구/비주얼: a field the parser does not know
        if key is not None:
            current = key
            if key == _SKIP:
                label = _LABEL_NOISE.sub("", field_match.group("label"))
                if label and label not in skipped:
                    skipped.append(label)
                continue
            value = _field_value(field_match.group("label"), field_match.group("value"))
            if value:
                fields[key].append(value)
            continue
        if current in (_TEXT, _SUB, _HEADING_TEXT):
            fields[current].append(_LIST_MARKER.sub("", line.strip()))  # one more line on the image
        elif current:
            fields[current].append(line.strip())
        elif current is None:
            loose.append(line.strip().lstrip("-*• ").strip())
        # current == "": follow-up of a skipped field — skipped with it
    return fields, loose, skipped


def parse_carousel(content: str) -> list[Slide]:
    section = content_section(content or "", "캐러셀") or (content or "")
    matches = list(_SLIDE_HEADING.finditer(section))
    slides: list[Slide] = []
    for index, match in enumerate(matches):
        body = section[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(section)]
        fields, loose, skipped = _parse_slide_body(body)
        headline = fields[_TEXT]
        if not headline:
            headline = fields[_HEADING_TEXT]  # "- 제목:" stands in for a missing 문구
        elif fields[_HEADING_TEXT] and "제목" not in skipped:
            skipped.append("제목")  # 문구 is the headline; say that 제목 was left off the image
        lines = headline + fields[_SUB]
        text = "\n".join(lines).strip() if lines else " / ".join(loose).strip()
        slides.append(Slide(
            number=int(match.group(1)),
            title=match.group(2).strip(),
            text=text,
            visual=" ".join(fields["visual"]).strip(),
            alt=" ".join(fields["alt"]).strip(),
            source=" ".join(fields["source"]).strip(),
            skipped=tuple(skipped),
        ))
    return slides


def normalize_handle(handle: str) -> str:
    handle = (handle or "").strip()
    if not handle:
        return ""
    handle = re.sub(r"^https?://(www\.)?instagram\.com/", "", handle, flags=re.IGNORECASE).strip("/ ")
    handle = handle.lstrip("@").split("/")[0].split("?")[0]
    return f"@{handle}" if handle else ""


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_hex(value: str) -> str | None:
    match = _HEX.match((value or "").strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return f"#{digits.upper()}"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """``a`` blended toward ``b`` by ``t`` (0 = a, 1 = b)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _rgb(hex_color)
    return f"rgba({r},{g},{b},{alpha:.2f})"


@dataclass(frozen=True)
class Palette:
    dark: str
    primary: str
    accent: str

    @classmethod
    def from_profile(cls, profile: Profile | None) -> "Palette":
        colors = [c for c in (normalize_hex(v) for v in (profile.brand_colors if profile else [])) if c]
        if not colors:
            return cls(*INSIA_PALETTE)
        primary = colors[0]
        if len(colors) > 1:
            accent = colors[1]
        else:
            gold = INSIA_PALETTE[2]
            accent = gold if contrast(gold, primary) >= 2.0 else mix(primary, "#FFFFFF", 0.6)
        darks = [c for c in colors if luminance(c) < 0.03]
        dark = darks[0] if darks else mix(primary, "#05070D", 0.84)
        return cls(dark=dark, primary=primary, accent=accent)

    def theme(self, variant: str) -> dict[str, str]:
        """CSS variables for a ``cover`` (main-color gradient) or ``inner`` (dark) slide."""
        if variant == "cover":
            mid = mix(self.primary, self.dark, 0.35)
            bg = (f"linear-gradient(160deg, {self.primary} 0%, {mid} 55%, {mix(self.primary, self.dark, 0.7)} 100%)")
        else:
            mid = self.dark
            bg = (f"radial-gradient(1100px 900px at 100% 0%, {rgba(self.primary, 0.42)} 0%, transparent 60%), "
                  f"radial-gradient(900px 700px at 0% 100%, {rgba(self.primary, 0.14)} 0%, transparent 62%), "
                  f"{self.dark}")
        fg = "#FFFFFF" if contrast("#FFFFFF", mid) >= contrast("#111827", mid) else "#111827"
        accent = self.accent
        if contrast(accent, mid) < 1.8:
            accent = mix(accent, fg, 0.55)
            if contrast(accent, mid) < 1.8:
                accent = fg
        pill_fg = mid if contrast(mid, fg) >= 3 else ("#111827" if fg == "#FFFFFF" else "#FFFFFF")
        return {
            "--bg": bg,
            "--fg": fg,
            "--fg-sub": rgba(fg, 0.86),
            "--fg-muted": rgba(fg, 0.62),
            "--accent": accent,
            "--chip": rgba(fg, 0.14),
            "--pill-bg": fg,
            "--pill-fg": pill_fg,
        }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_EMPHASIS = re.compile(r"(\d[\d,.]*\s?(?:%p|%|명|개|원|만|억|배|년|자|장|시간|분|곳|단계|위|가지)?)")

_CSS = f"""
@page {{ size: {SLIDE_WIDTH}px {SLIDE_HEIGHT}px; margin: 0; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ background: #202124; }}
body {{ font-family: {FONT_STACK}; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }}
.toolbar {{ max-width: {SLIDE_WIDTH}px; margin: 24px auto; padding: 16px 20px; border-radius: 12px; background: #fff;
  color: #1F2328; font-size: 15px; line-height: 1.6; }}
.toolbar b {{ font-weight: 700; }}
.slide {{ position: relative; width: {SLIDE_WIDTH}px; height: {SLIDE_HEIGHT}px; overflow: hidden; display: flex;
  flex-direction: column; padding: 84px 96px 76px; color: var(--fg); background: var(--bg); margin: 0 auto 40px;
  break-after: page; page-break-after: always; }}
body.render .slide {{ margin: 0; }}
.top {{ display: flex; justify-content: space-between; align-items: center; min-height: 56px; gap: 24px; }}
.brand {{ font-size: 28px; font-weight: 700; letter-spacing: 0.01em; color: var(--fg-sub); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }}
.counter {{ margin-left: auto; font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--fg);
  background: var(--chip); border-radius: 999px; padding: 8px 22px; }}
.body {{ flex: 1; min-height: 0; display: flex; flex-direction: column; justify-content: center;
  justify-content: safe center; overflow: hidden; }}
.slide.overflow {{ outline: 8px solid #F04438; outline-offset: -8px; }}
body.render .slide.overflow {{ outline: none; }}
.fit-warning {{ color: #B42318; font-weight: 700; }}
.bar {{ width: 96px; height: 10px; border-radius: 5px; background: var(--accent); margin-bottom: 44px; flex: none; }}
.headline {{ font-weight: 800; line-height: 1.24; letter-spacing: -0.02em; word-break: keep-all;
  overflow-wrap: anywhere; text-wrap: balance; }}
.headline em {{ font-style: normal; color: var(--accent); }}
.sub {{ margin-top: 40px; font-size: 42px; line-height: 1.5; font-weight: 500; color: var(--fg-sub);
  word-break: keep-all; overflow-wrap: anywhere; text-wrap: pretty; }}
.sub + .sub {{ margin-top: 20px; }}
.pill {{ display: inline-block; align-self: flex-start; margin-top: 44px; padding: 20px 36px; border-radius: 999px;
  background: var(--pill-bg); color: var(--pill-fg); font-size: 36px; font-weight: 700; line-height: 1.35;
  word-break: keep-all; }}
.bottom {{ margin-top: 36px; flex: none; }}
.source {{ font-size: 22px; line-height: 1.5; color: var(--fg-muted); margin-bottom: 18px; word-break: keep-all;
  overflow-wrap: anywhere; }}
.foot {{ display: flex; justify-content: space-between; align-items: center; min-height: 40px; font-size: 28px;
  font-weight: 600; color: var(--fg-muted); }}
.swipe {{ font-size: 44px; line-height: 1; color: var(--accent); }}
@media print {{ html, body {{ background: none; }} .toolbar {{ display: none; }} .slide {{ margin: 0; }} }}
"""

# Shrinks fonts until each slide's text fits (headline 60→44→38px, lines
# 42→30→26px, source 22→16px) and returns the 1-based numbers of slides that
# still overflow. Starts from the original sizes on every call, so running
# again after web fonts load gives the same result as a single run.
_FIT_JS = """
window.__insiaFit = function () {
  var overflow = [];
  document.querySelectorAll('.slide').forEach(function (slide, index) {
    var body = slide.querySelector('.body');
    var head = slide.querySelector('.headline');
    var subs = Array.prototype.slice.call(slide.querySelectorAll('.sub, .pill'));
    var source = slide.querySelector('.source');
    [head].concat(subs, source ? [source] : []).forEach(function (el) {
      if (el.dataset.baseSize === undefined) { el.dataset.baseSize = el.style.fontSize || ''; }
      el.style.fontSize = el.dataset.baseSize;
    });
    function fits() {
      return body.scrollHeight <= body.clientHeight + 1 && head.scrollWidth <= head.clientWidth + 1 &&
        slide.scrollHeight <= slide.clientHeight + 1;
    }
    function setSubs(size) { subs.forEach(function (s) { s.style.fontSize = size + 'px'; }); }
    var hs = parseFloat(getComputedStyle(head).fontSize);
    var ss = subs.length ? parseFloat(getComputedStyle(subs[0]).fontSize) : 0;
    var fs = source ? parseFloat(getComputedStyle(source).fontSize) : 0;
    var guard = 0;
    while (!fits() && guard++ < 160) {
      if (hs > 60) { hs -= 4; head.style.fontSize = hs + 'px'; }
      else if (ss > 30) { ss -= 2; setSubs(ss); }
      else if (hs > 44) { hs -= 2; head.style.fontSize = hs + 'px'; }
      else if (source && fs > 16) { fs -= 1; source.style.fontSize = fs + 'px'; }
      else if (ss > 26) { ss -= 2; setSubs(ss); }
      else if (hs > 38) { hs -= 2; head.style.fontSize = hs + 'px'; }
      else break;
    }
    if (source) {  // a long source line must not squeeze the message itself
      var g2 = 0;
      while (body.clientHeight < 300 && fs > 16 && g2++ < 10) { fs -= 1; source.style.fontSize = fs + 'px'; }
    }
    var over = !fits();
    slide.classList.toggle('overflow', over);
    if (over) { overflow.push(index + 1); }
  });
  var warning = document.querySelector('.fit-warning');
  if (warning) {
    warning.textContent = overflow.length ? ' 빨간 테두리 슬라이드(' + overflow.join(', ') +
      ')는 글이 길어 잘렸어요. 문구나 출처를 줄여 다시 내보내 주세요.' : '';
  }
  window.__insiaOverflow = overflow;
  return overflow;
};
if (document.readyState !== 'loading') { window.__insiaFit(); }
else { document.addEventListener('DOMContentLoaded', window.__insiaFit); }
if (document.fonts && document.fonts.ready) { document.fonts.ready.then(window.__insiaFit); }
"""


def _headline_size(text: str, cover: bool) -> int:
    n = len(text)
    size = 104 if n <= 12 else 92 if n <= 20 else 80 if n <= 32 else 68 if n <= 48 else 60
    return size + (8 if cover and n <= 32 else 0)


def _emphasize(text: str) -> str:
    """Escape ``text`` and wrap numbers (with their unit) in ``<em>`` for the accent color."""
    pieces = _EMPHASIS.split(text)  # odd indexes are the captured numbers
    return "".join(f"<em>{html.escape(p)}</em>" if i % 2 else html.escape(p) for i, p in enumerate(pieces))


def _slide_html(slide: Slide, index: int, total: int, palette: Palette, brand: str, handle: str) -> str:
    cover = index == 0 or index == total - 1
    theme = palette.theme("cover" if cover else "inner")
    style = "; ".join(f"{k}: {v}" for k, v in theme.items())
    parts = slide.parts
    headline, subs = parts[0], parts[1:]
    pill = ""
    if index == total - 1 and total > 1 and len(subs) >= 2:
        pill = f'<p class="pill">{html.escape(subs[-1])}</p>'
        subs = subs[:-1]
    subs_html = "".join(f'<p class="sub">{html.escape(s)}</p>' for s in subs)
    brand_html = f'<span class="brand">{html.escape(brand)}</span>' if brand else ""
    source = _plain(slide.source)
    source_html = f'<p class="source">출처: {html.escape(source)}</p>' if source else ""
    handle_html = f'<span class="handle">{html.escape(handle)}</span>' if handle else "<span></span>"
    swipe_html = '<span class="swipe" aria-hidden="true">→</span>' if index == 0 and total > 1 else ""
    size = _headline_size(headline, cover)
    return (
        f'<section class="slide {"cover" if cover else "inner"}" data-index="{index + 1}" style="{html.escape(style)}" '
        f'aria-label="슬라이드 {index + 1}/{total}">'
        f'<div class="top">{brand_html}<span class="counter">{index + 1}/{total}</span></div>'
        f'<div class="body"><div class="bar"></div>'
        f'<h1 class="headline" style="font-size: {size}px">{_emphasize(headline)}</h1>{subs_html}{pill}</div>'
        f'<div class="bottom">{source_html}<div class="foot">{handle_html}{swipe_html}</div></div>'
        "</section>"
    )


def slides_html(slides: list[Slide], profile: Profile | None = None, *, title: str = "",
                render_mode: bool = False) -> str:
    """One HTML page with every slide at 1080×1350 (render mode hides the help box)."""
    palette = Palette.from_profile(profile)
    brand = xml_safe(((profile.service_name or profile.company_name) if profile else "") or "", soft_break=" ")
    handle = xml_safe(normalize_handle(profile.instagram_handle) if profile else "", soft_break="")
    total = len(slides)
    sections = "\n".join(_slide_html(s, i, total, palette, brand.strip(), handle) for i, s in enumerate(slides))
    toolbar = "" if render_mode else (
        '<div class="toolbar"><b>인스타그램 캐러셀 슬라이드</b> · '
        f"{total}장, 장마다 {SLIDE_WIDTH}×{SLIDE_HEIGHT}px(세로 4:5)<br>"
        "PNG로 저장하려면: 크롬에서 인쇄(Ctrl+P) → 대상 'PDF로 저장' → 여백 '없음'으로 PDF를 만든 뒤 장별로 변환하거나, "
        "각 장을 캡처하세요. 문구와 출처는 게시 전에 꼭 확인하세요.<span class=\"fit-warning\" role=\"status\"></span></div>"
    )
    return (
        "<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width={SLIDE_WIDTH}\">\n"
        f"<title>{html.escape(title or '인스타그램 캐러셀')} · 슬라이드</title>\n"
        f"<style>{_CSS}</style>\n</head>\n"
        f"<body class=\"{'render' if render_mode else 'preview'}\">\n{toolbar}\n{sections}\n"
        f"<script>{_FIT_JS}</script>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# PNG rendering (Playwright, optional)
# ---------------------------------------------------------------------------


# Draws "한" and an unassigned code point (U+0378, always the missing-glyph box)
# with the slide font stack; identical pixels mean no Hangul font is installed
# and the PNGs would show empty boxes instead of text.
_HANGUL_CHECK_JS = """() => {
  const canvas = document.createElement('canvas'); canvas.width = 80; canvas.height = 80;
  const ctx = canvas.getContext('2d');
  const font = '56px ' + getComputedStyle(document.body).fontFamily;
  const sig = (ch) => { ctx.clearRect(0, 0, 80, 80); ctx.font = font; ctx.fillStyle = '#000';
    ctx.textBaseline = 'top'; ctx.fillText(ch, 8, 8); return Array.from(ctx.getImageData(0, 0, 80, 80).data).join(','); };
  const hangul = sig('한');
  return hangul !== sig('\\u0378') && hangul !== sig(' ');
}"""


class RenderedSlides(list):
    """PNG bytes, one per slide. ``overflow`` lists the 1-based slides whose
    text still did not fit after shrinking the fonts (clipped on the image)."""

    overflow: tuple[int, ...] = ()


def _render_sync(page_html: str, count: int, executable: str | None, image_type: str = "png",
                 quality: int | None = None) -> list[bytes]:
    from playwright.sync_api import sync_playwright

    shot: dict[str, object] = {"type": image_type, "timeout": RENDER_TIMEOUT_MS, "animations": "disabled"}
    if image_type == "jpeg":
        shot["quality"] = int(quality if quality is not None else JPEG_QUALITY)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, headless=True, timeout=RENDER_TIMEOUT_MS)
        try:
            page = browser.new_page(viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT}, device_scale_factor=1)
            page.set_content(page_html, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            page.evaluate("() => (document.fonts ? document.fonts.ready.then(() => true) : true)")
            if not page.evaluate(_HANGUL_CHECK_JS):
                raise RenderUnavailable(f"이 컴퓨터에 한글 글꼴이 없어 {'PNG' if image_type == 'png' else '카드 이미지'} 글자가 깨져요 "
                                        "(리눅스·도커라면 fonts-noto-cjk 같은 한글 글꼴을 설치하세요)")
            overflow = page.evaluate("() => (window.__insiaFit ? window.__insiaFit() : [])")
            slides = page.locator("section.slide")
            found = slides.count()
            if found != count:
                raise RenderUnavailable(f"슬라이드 {count}장 중 {found}장만 그려졌어요")
            rendered = RenderedSlides(slides.nth(i).screenshot(**shot) for i in range(count))
            if isinstance(overflow, list):
                rendered.overflow = tuple(int(n) for n in overflow if isinstance(n, (int, float)))
            return rendered
        finally:
            browser.close()


def _run_with_deadline(fn, *args):
    """Run ``fn`` in a daemon thread (the Playwright sync API refuses to run in
    an asyncio thread, and a hung browser must not block the caller)."""
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=target, name="insia-slide-render", daemon=True)
    worker.start()
    worker.join(RENDER_DEADLINE_S)
    if worker.is_alive():
        raise RenderUnavailable(f"슬라이드를 그리는 데 {RENDER_DEADLINE_S}초가 넘게 걸려 멈췄어요")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]


def render_pngs(page_html: str, count: int) -> list[bytes]:
    """Render every ``section.slide`` of ``page_html`` to PNG bytes.

    Raises ``RenderUnavailable`` (Korean reason) when rendering is disabled,
    Playwright is missing, or the browser cannot start.
    """
    return render_images(page_html, count, image_type="png")


def render_images(page_html: str, count: int, *, image_type: str = "png", quality: int | None = None) -> list[bytes]:
    """``render_pngs`` with a choice of format: ``"png"`` or ``"jpeg"`` (quality ``JPEG_QUALITY`` = 90 by default).

    Chromium's JPEG screenshots have no alpha channel, are sRGB and baseline, at the same 1080×1350, so
    Instagram's API publishing needs no imaging library. The result carries ``overflow`` like ``render_pngs``.
    Same ``RenderUnavailable`` errors; the Instagram API path never falls back to ``slides.html``.
    """
    if image_type not in ("png", "jpeg"):
        raise ValueError(f"image_type must be 'png' or 'jpeg', not {image_type!r}")
    if quality is not None and not 1 <= int(quality) <= 100:
        raise ValueError("quality must be between 1 and 100")
    if (os.environ.get("INSIA_RENDER") or "").strip().lower() in {"0", "false", "no", "off"}:
        raise RenderUnavailable("PNG 렌더링이 꺼져 있어요 (INSIA_RENDER=0)" if image_type == "png"
                                else "카드 이미지 렌더링이 꺼져 있어요 (INSIA_RENDER=0)")
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError as exc:
        raise RenderUnavailable(
            f"PNG를 만들려면 Playwright가 필요해요. `{EXTRA_HINT.format(extra='render')}` 설치 후 "
            "`playwright install chromium`을 실행하세요"
        ) from exc

    executable = (os.environ.get("INSIA_CHROMIUM") or "").strip() or None
    if executable and not Path(executable).exists():
        raise RenderUnavailable(f"INSIA_CHROMIUM에 지정한 브라우저를 찾을 수 없어요: {executable}")

    try:
        if image_type == "png":  # the original call shape (tests replace _render_sync with a 3-argument fake)
            return _run_with_deadline(_render_sync, page_html, count, executable)
        return _run_with_deadline(_render_sync, page_html, count, executable, image_type, quality)
    except RenderUnavailable:
        raise
    except PlaywrightError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        hint = "" if executable else " (브라우저가 없다면 `playwright install chromium` 또는 INSIA_CHROMIUM 설정)"
        raise RenderUnavailable(f"브라우저로 슬라이드를 그리지 못했어요: {first[:200]}{hint}") from exc
    except Exception as exc:  # noqa: BLE001 — never fail the export over rendering; fall back to slides.html
        raise RenderUnavailable(f"브라우저로 슬라이드를 그리지 못했어요: {str(exc).strip()[:200] or exc.__class__.__name__}") from exc


# ---------------------------------------------------------------------------
# Zip package
# ---------------------------------------------------------------------------


def _alt_text(slides: list[Slide]) -> str:
    blocks = []
    for index, slide in enumerate(slides, start=1):
        alt = _plain(slide.alt) or "(대체 텍스트가 없어요. 게시 전에 이 장의 내용을 한두 문장으로 적어 주세요.)"
        blocks.append(f"[slide-{index:02d}.png] 슬라이드 {index}\n{alt}")
    return "\n\n".join(blocks) + "\n"


def _numbers(indexes: list[int]) -> str:
    return ", ".join(str(i) for i in indexes)


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text or "") if w and w != "/"])


def slide_warnings(slides: list[Slide], *, overflow: tuple[int, ...] = (), rendered: bool = True) -> list[str]:
    """Korean notes about slides that need a look before posting.

    ``overflow``: 1-based slides whose text did not fit when rendered. Without
    a rendering (``rendered=False``) very long 문구/출처 are flagged instead.
    """
    notes: list[str] = []
    no_text = [i for i, s in enumerate(slides, start=1) if not s.text.strip()]
    if no_text:
        notes.append(f"문구(- 문구:)를 찾지 못한 슬라이드: {_numbers(no_text)} — 장 제목을 대신 넣었어요. "
                     "이미지를 확인해 주세요.")
    no_alt = [i for i, s in enumerate(slides, start=1) if not _plain(s.alt)]
    if no_alt:
        notes.append(f"대체텍스트(- 대체텍스트:)를 찾지 못한 슬라이드: {_numbers(no_alt)} — alt-text.txt에 직접 적어 주세요.")
    skipped = [f"슬라이드 {i}({', '.join(s.skipped)})" for i, s in enumerate(slides, start=1) if s.skipped]
    if skipped:
        notes.append(f"이미지에 넣지 않은 항목이 있어요: {'; '.join(skipped)}. "
                     "이미지에 넣을 글이면 '- 문구:' 줄에 ' / '로 이어 적어 주세요.")
    if overflow:
        notes.append(f"글이 길어 잘린 슬라이드: {_numbers(list(overflow))} — 문구나 출처를 줄인 뒤 다시 내보내 주세요.")
    elif not rendered:
        long = [i for i, s in enumerate(slides, start=1)
                if _word_count(s.text) > LONG_TEXT_WORDS or _word_count(s.source) > LONG_SOURCE_WORDS]
        if long:
            notes.append(f"글이 길어 잘릴 수 있는 슬라이드: {_numbers(long)} (문구는 25어절 이하 권장) — "
                         "slides.html에서 빨간 테두리가 없는지 확인해 주세요.")
    return notes


def _readme(draft: Draft, slides: list[Slide], *, rendered: bool, reason: str, meta: str,
            warnings: list[str] | None = None) -> str:
    total = len(slides)
    lines = [
        "인스타그램 캐러셀 업로드 묶음",
        "",
        f"제목: {draft.title.strip()}",
    ]
    if meta:
        lines.append(meta)
    lines += ["", "들어 있는 파일"]
    if rendered:
        lines.append(f"- slide-01.png ~ slide-{total:02d}.png : 캐러셀 이미지 {total}장 ({SLIDE_WIDTH}×{SLIDE_HEIGHT}, 세로 4:5)")
    else:
        lines.append(f"- slides.html : 캐러셀 {total}장 ({SLIDE_WIDTH}×{SLIDE_HEIGHT}, 세로 4:5). 브라우저로 열어 확인하세요")
    lines += [
        "- caption.txt : 캡션 (해시태그 포함, 그대로 붙여 넣기)",
        "- alt-text.txt : 장별 대체 텍스트",
        "",
    ]
    if warnings:
        lines += ["먼저 확인할 부분"] + [f"- {w}" for w in warnings] + [""]
    if not rendered:
        lines += [
            "PNG 이미지를 만들지 못해서 slides.html을 대신 넣었어요.",
            f"이유: {reason}",
            "slides.html을 크롬으로 열고 인쇄(Ctrl+P) → 'PDF로 저장' → 여백 '없음'으로 저장하면 한 장에 한 슬라이드씩 나와요.",
            "PNG를 바로 받으려면 이 PC에 `pip install \"insia-smartagent[render]\"`와 `playwright install chromium`을 실행한 뒤 다시 내보내세요.",
            "",
        ]
    lines += [
        "올리는 순서",
        "1. 인스타그램 새 게시물에서 여러 장 선택으로 slide-01부터 순서대로 고르세요.",
        "2. 비율은 세로 4:5로 두세요.",
        "3. caption.txt 내용을 캡션 칸에 붙여 넣으세요.",
        "4. 고급 설정 → 대체 텍스트 작성에서 alt-text.txt의 장별 문구를 넣으세요.",
        "",
        "게시 전 확인",
        "- 이미지 속 문구·숫자·출처가 맞는지 직접 확인하세요.",
        "- 사람이 최종 승인한 뒤에만 게시하세요.",
        "",
        "디자인 메모 (디자이너·캔바에서 다시 만들 때 참고)",
    ]
    for index, slide in enumerate(slides, start=1):
        lines.append(f"- 슬라이드 {index} ({slide.title or '제목 없음'}): {slide.visual or '비주얼 지시 없음'}")
    return "\n".join(lines).rstrip() + "\n"


def build_carousel_zip(draft: Draft, profile: Profile | None = None, *, meta: str = "") -> tuple[bytes, tuple[str, ...]]:
    """Zip bytes (slides + caption.txt + alt-text.txt + README.txt) and Korean notes."""
    draft = clean_draft(draft)  # no invisible control characters on the images or in the text files
    slides = parse_carousel(draft.content or "")
    if not slides:
        raise ExportError("캐러셀 슬라이드(### 슬라이드 N)를 찾지 못해 이미지를 만들 수 없어요. "
                          "txt나 md로 내보내거나 초안의 ## 캐러셀 형식을 확인해 주세요.")
    caption = instagram_caption(draft) + "\n"
    notes: list[str] = []
    pngs: list[bytes] | None = None
    reason = ""
    try:
        pngs = render_pngs(slides_html(slides, profile, title=draft.title, render_mode=True), len(slides))
    except RenderUnavailable as exc:
        reason = str(exc)
        notes.append(f"PNG 대신 slides.html을 넣었어요 — {reason}")
    overflow = tuple(getattr(pngs, "overflow", ()) or ())
    warnings = slide_warnings(slides, overflow=overflow, rendered=pngs is not None)
    notes.extend(warnings)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if pngs is not None:
            for index, png in enumerate(pngs, start=1):
                archive.writestr(f"slide-{index:02d}.png", png, compress_type=zipfile.ZIP_STORED)
        else:
            archive.writestr("slides.html", slides_html(slides, profile, title=draft.title))
        archive.writestr("caption.txt", caption)
        archive.writestr("alt-text.txt", _alt_text(slides))
        archive.writestr("README.txt", _readme(draft, slides, rendered=pngs is not None, reason=reason, meta=meta,
                                               warnings=warnings))
    return buffer.getvalue(), tuple(notes)
