"""Instagram carousel export: 1080×1350 slide images + caption + alt text.

Slides come from the ``## 캐러셀`` section (``### 슬라이드 N — 제목`` followed
by ``- 문구:`` / ``- 비주얼:`` / ``- 대체텍스트:`` / optional ``- 출처:``). The
문구 is the on-image text: the part before the first `` / `` is the big
headline, the rest are smaller lines. Colors come from ``profile.brand_colors``
(first = main color) or the INSIA palette.

PNG rendering uses Playwright + Chromium when available (optional extra
``[render]``; ``INSIA_CHROMIUM`` overrides the browser executable,
``INSIA_RENDER=0`` turns rendering off). Otherwise the zip carries
``slides.html`` with print-ready 1080×1350 pages and a README note.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..models import Draft, Profile
from .common import EXTRA_HINT, ExportError, content_section
from .text import instagram_caption

SLIDE_WIDTH = 1080
SLIDE_HEIGHT = 1350
INSIA_PALETTE = ("#0B1220", "#6D5EF5", "#F5C451")  # dark base, main, accent
FONT_STACK = ('"Pretendard", "Noto Sans KR", "Noto Sans CJK KR", "Apple SD Gothic Neo", "Malgun Gothic", '
              '"맑은 고딕", "NanumGothic", "나눔고딕", "NanumBarunGothic", "WenQuanYi Zen Hei", sans-serif')
RENDER_TIMEOUT_MS = 45_000


class RenderUnavailable(RuntimeError):
    """PNG rendering is not possible here; ``str(exc)`` is a Korean reason."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slide:
    number: int
    title: str
    text: str  # 문구 (on-image text)
    visual: str = ""
    alt: str = ""
    source: str = ""

    @property
    def parts(self) -> list[str]:
        """Headline first, then the smaller lines (split on " / " or line breaks)."""
        raw = self.text or self.title
        parts = [p.strip() for p in re.split(r"\s+/\s+|\n", raw) if p.strip()]
        return parts or [self.title or f"슬라이드 {self.number}"]


_SLIDE_HEADING = re.compile(r"^###\s*슬라이드\s*(\d+)\s*(?:[—–\-:|·]\s*)?(.*)$", re.MULTILINE)
_FIELD = re.compile(r"^\s*[-*•]\s*([^:：\n]{1,12})[:：]\s?(.*)$")
_LABELS = {
    "문구": "text", "카피": "text", "텍스트": "text", "화면문구": "text", "이미지문구": "text",
    "비주얼": "visual", "디자인": "visual", "레이아웃": "visual",
    "대체텍스트": "alt", "대체문구": "alt", "alt": "alt", "alttext": "alt",
    "출처": "source", "source": "source",
}


def parse_carousel(content: str) -> list[Slide]:
    section = content_section(content or "", "캐러셀") or (content or "")
    matches = list(_SLIDE_HEADING.finditer(section))
    slides: list[Slide] = []
    for index, match in enumerate(matches):
        body = section[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(section)]
        fields: dict[str, list[str]] = {"text": [], "visual": [], "alt": [], "source": []}
        current: str | None = None
        loose: list[str] = []
        for line in body.splitlines():
            if not line.strip():
                continue
            field_match = _FIELD.match(line)
            key = _LABELS.get(re.sub(r"\s", "", field_match.group(1)).lower()) if field_match else None
            if field_match and key:
                current = key
                fields[key].append(field_match.group(2).strip())
            elif current:
                fields[current].append(line.strip())
            else:
                loose.append(line.strip().lstrip("-*• ").strip())
        text = " ".join(fields["text"]).strip() or " / ".join(loose).strip()
        slides.append(Slide(
            number=int(match.group(1)),
            title=match.group(2).strip(),
            text=text,
            visual=" ".join(fields["visual"]).strip(),
            alt=" ".join(fields["alt"]).strip(),
            source=" ".join(fields["source"]).strip(),
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
.body {{ flex: 1; min-height: 0; display: flex; flex-direction: column; justify-content: center; }}
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

_FIT_JS = """
window.__insiaFit = function () {
  document.querySelectorAll('.slide').forEach(function (slide) {
    var body = slide.querySelector('.body');
    var head = slide.querySelector('.headline');
    var subs = slide.querySelectorAll('.sub, .pill');
    var source = slide.querySelector('.source');
    var hs = parseFloat(getComputedStyle(head).fontSize);
    var ss = subs.length ? parseFloat(getComputedStyle(subs[0]).fontSize) : 0;
    var guard = 0;
    while ((body.scrollHeight > body.clientHeight + 1 || head.scrollWidth > head.clientWidth + 1) && guard++ < 80) {
      if (hs > 60) { hs -= 4; head.style.fontSize = hs + 'px'; }
      else if (ss > 30) { ss -= 2; subs.forEach(function (s) { s.style.fontSize = ss + 'px'; }); }
      else if (hs > 44) { hs -= 2; head.style.fontSize = hs + 'px'; }
      else break;
    }
    if (source) {
      var fs = 22, g2 = 0;
      while (body.clientHeight < 300 && fs > 16 && g2++ < 10) { fs -= 1; source.style.fontSize = fs + 'px'; }
    }
  });
  return true;
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
    source_html = f'<p class="source">출처: {html.escape(slide.source)}</p>' if slide.source else ""
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
    brand = ((profile.service_name or profile.company_name) if profile else "") or ""
    handle = normalize_handle(profile.instagram_handle) if profile else ""
    total = len(slides)
    sections = "\n".join(_slide_html(s, i, total, palette, brand.strip(), handle) for i, s in enumerate(slides))
    toolbar = "" if render_mode else (
        '<div class="toolbar"><b>인스타그램 캐러셀 슬라이드</b> · '
        f"{total}장, 장마다 {SLIDE_WIDTH}×{SLIDE_HEIGHT}px(세로 4:5)<br>"
        "PNG로 저장하려면: 크롬에서 인쇄(Ctrl+P) → 대상 'PDF로 저장' → 여백 '없음'으로 PDF를 만든 뒤 장별로 변환하거나, "
        "각 장을 캡처하세요. 문구와 출처는 게시 전에 꼭 확인하세요.</div>"
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


def _render_sync(page_html: str, count: int, executable: str | None) -> list[bytes]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, headless=True, timeout=RENDER_TIMEOUT_MS)
        try:
            page = browser.new_page(viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT}, device_scale_factor=1)
            page.set_content(page_html, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            page.evaluate("() => (document.fonts ? document.fonts.ready.then(() => true) : true)")
            page.evaluate("() => window.__insiaFit && window.__insiaFit()")
            slides = page.locator("section.slide")
            found = slides.count()
            if found != count:
                raise RenderUnavailable(f"슬라이드 {count}장 중 {found}장만 그려졌어요")
            return [slides.nth(i).screenshot(type="png", timeout=RENDER_TIMEOUT_MS, animations="disabled")
                    for i in range(count)]
        finally:
            browser.close()


def render_pngs(page_html: str, count: int) -> list[bytes]:
    """Render every ``section.slide`` of ``page_html`` to PNG bytes.

    Raises ``RenderUnavailable`` (Korean reason) when rendering is disabled,
    Playwright is missing, or the browser cannot start.
    """
    if (os.environ.get("INSIA_RENDER") or "").strip().lower() in {"0", "false", "no", "off"}:
        raise RenderUnavailable("PNG 렌더링이 꺼져 있어요 (INSIA_RENDER=0)")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RenderUnavailable(
            f"PNG를 만들려면 Playwright가 필요해요. `{EXTRA_HINT.format(extra='render')}` 설치 후 "
            "`playwright install chromium`을 실행하세요"
        ) from exc
    from playwright.sync_api import Error as PlaywrightError

    executable = (os.environ.get("INSIA_CHROMIUM") or "").strip() or None
    if executable and not Path(executable).exists():
        raise RenderUnavailable(f"INSIA_CHROMIUM에 지정한 브라우저를 찾을 수 없어요: {executable}")

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    try:
        if in_loop:  # the sync API refuses to run inside an asyncio loop thread
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_render_sync, page_html, count, executable).result()
        return _render_sync(page_html, count, executable)
    except RenderUnavailable:
        raise
    except PlaywrightError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        hint = "" if executable else " (브라우저가 없다면 `playwright install chromium` 또는 INSIA_CHROMIUM 설정)"
        raise RenderUnavailable(f"브라우저로 슬라이드를 그리지 못했어요: {first[:200]}{hint}") from exc
    except OSError as exc:
        raise RenderUnavailable(f"브라우저를 실행하지 못했어요: {exc}") from exc


# ---------------------------------------------------------------------------
# Zip package
# ---------------------------------------------------------------------------


def _alt_text(slides: list[Slide]) -> str:
    blocks = []
    for index, slide in enumerate(slides, start=1):
        alt = slide.alt or "(대체 텍스트가 없어요. 게시 전에 이 장의 내용을 한두 문장으로 적어 주세요.)"
        blocks.append(f"[slide-{index:02d}.png] 슬라이드 {index}\n{alt}")
    return "\n\n".join(blocks) + "\n"


def _readme(draft: Draft, slides: list[Slide], *, rendered: bool, reason: str, meta: str) -> str:
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

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if pngs is not None:
            for index, png in enumerate(pngs, start=1):
                archive.writestr(f"slide-{index:02d}.png", png, compress_type=zipfile.ZIP_STORED)
        else:
            archive.writestr("slides.html", slides_html(slides, profile, title=draft.title))
        archive.writestr("caption.txt", caption)
        archive.writestr("alt-text.txt", _alt_text(slides))
        archive.writestr("README.txt", _readme(draft, slides, rendered=pngs is not None, reason=reason, meta=meta))
    return buffer.getvalue(), tuple(notes)
