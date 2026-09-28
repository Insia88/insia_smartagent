"""네이버 블로그 HTML: a SmartEditor-ONE-paste-friendly fragment plus a
standalone preview page with copy buttons.

SmartEditor keeps only simple structure when rich text is pasted and drops
classes and most CSS, so the fragment uses inline styles only and a small tag
set: ``<p>``, ``<b>``, ``<br>``, ``<span>``, ``<a>``, ``<table>/<tr>/<td>``.
"""

from __future__ import annotations

import html
import json
import re

from ..channels import CHANNELS, check_format, chars_no_space, chars_with_space
from ..models import Brief, Draft, FormatCheck, Profile, Review
from .common import Block, find_placeholders, inline_segments, is_hashtag_line, parse_blocks

NAVER_GREEN = "#03C75A"
FRAGMENT_START = "<!-- INSIA:FRAGMENT:START -->"
FRAGMENT_END = "<!-- INSIA:FRAGMENT:END -->"

_SPACER = "<p><br></p>"
_P = '<p style="line-height:1.8;">'
_BR_TAG = re.compile(r"&lt;br\s*/?&gt;", re.IGNORECASE)
_TD = "border:1px solid #D0D5DD;padding:6px 10px;vertical-align:top;"
_TD_HEAD = _TD + "background-color:#F2F4F7;"


def _inline(text: str) -> str:
    parts: list[str] = []
    for seg in inline_segments(text):
        escaped = html.escape(seg.text, quote=True)
        if seg.kind == "url":
            piece = f'<a href="{escaped}" target="_blank" rel="noopener">{escaped}</a>'
        elif seg.kind == "placeholder":
            piece = f'<span style="background-color:#FFF3B0;">{escaped}</span>'
        else:
            piece = escaped
        parts.append(f"<b>{piece}</b>" if seg.bold else piece)
    return _BR_TAG.sub("<br>", "".join(parts))


def _is_source_para(block: Block) -> bool:
    return bool(block.lines) and all(line.startswith("출처") for line in block.lines)


def _table(block: Block) -> str:
    rows = ['<table style="border-collapse:collapse;width:100%;">']
    rows.append("<tr>" + "".join(f'<td style="{_TD_HEAD}"><b>{_inline(c)}</b></td>' for c in block.header) + "</tr>")
    for row in block.rows:
        rows.append("<tr>" + "".join(f'<td style="{_TD}">{_inline(c)}</td>' for c in row) + "</tr>")
    rows.append("</table>")
    return "".join(rows)


def _image_box(description: str) -> str:
    desc = html.escape(description or "사진", quote=True)
    return (
        '<table style="border-collapse:collapse;width:100%;"><tr>'
        f'<td style="border:2px dashed {NAVER_GREEN};padding:24px 16px;text-align:center;'
        'background-color:#F4FBF7;color:#4B5563;">'
        f"<b>[이미지 자리]</b><br>{desc}</td></tr></table>"
    )


def _item(block: Block) -> str:
    if block.marker in {"-", "*", "•", "·"}:
        marker = "•" if block.level == 0 else "-"
    else:
        marker = html.escape(block.marker)
    indent = "&nbsp;&nbsp;&nbsp;" * block.level
    text = "<br>".join(_inline(line) for line in block.text.split("\n"))
    return f"{_P}{indent}{marker} {text}</p>"


def naver_fragment(draft: Draft) -> str:
    """Body HTML to paste into SmartEditor (title and tags go in their own fields,
    but tags are also listed at the end — SmartEditor turns ``#태그`` into tags)."""
    blocks = parse_blocks(draft.content or "")
    if blocks and blocks[0].kind == "heading" and blocks[0].level == 1 \
            and blocks[0].text.strip() == draft.title.strip():
        blocks = blocks[1:]

    out: list[str] = []
    prev: str | None = None
    last_para_lines: list[str] = []
    for block in blocks:
        if block.kind == "rule":
            prev = "rule"
            continue
        if out and not (block.kind == "item" and prev == "item"):
            out.append(_SPACER)
            if block.kind == "heading" and block.level <= 2:
                out.append(_SPACER)
        if block.kind == "heading":
            size = "1.4em" if block.level == 1 else "1.25em" if block.level == 2 else "1.1em"
            out.append(f'<p style="font-size:{size};line-height:1.6;"><b>{_inline(block.text)}</b></p>')
        elif block.kind == "para":
            body = "<br>".join(_inline(line) for line in block.lines)
            if _is_source_para(block):
                out.append(f'<p style="font-size:0.85em;line-height:1.7;color:#8A8F98;">{body}</p>')
            else:
                out.append(f"{_P}{body}</p>")
            last_para_lines = block.lines
        elif block.kind == "item":
            out.append(_item(block))
        elif block.kind == "table":
            out.append(_table(block))
        elif block.kind == "image":
            out.append(_image_box(block.text))
        elif block.kind == "caption":
            out.append(f'<p style="text-align:center;"><b>{_inline(block.text)}</b></p>')
        elif block.kind == "quote":
            out.append(f'<p style="line-height:1.8;color:#555555;">│ {_inline(block.text)}</p>')
        prev = block.kind

    tags = [t.strip() for t in draft.hashtags if t.strip()]
    last_line = last_para_lines[-1] if last_para_lines and prev == "para" else ""
    if tags and not (is_hashtag_line(last_line) and all(t in last_line.split() for t in tags)):
        out.append(_SPACER)
        out.append(f'<p style="line-height:1.8;color:{NAVER_GREEN};">{html.escape(" ".join(tags))}</p>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Standalone preview page
# ---------------------------------------------------------------------------

_PAGE_CSS = """
:root { color-scheme: light; --ink:#1F2328; --muted:#5B616B; --line:#E4E7EC; --bg:#F5F6F8; --green:#03C75A; --warn:#8A5A00; --warn-bg:#FFF4D6; --bad:#B42318; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font-family: "Nanum Gothic", "나눔고딕", "Apple SD Gothic Neo", "Malgun Gothic", "맑은 고딕", "Noto Sans KR", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 24px 16px 64px; }
.bar { position: sticky; top: 0; z-index: 2; background: #fff; border-bottom: 3px solid var(--green); }
.bar .wrap { padding-top: 14px; padding-bottom: 14px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.bar h1 { font-size: 16px; margin: 0 auto 0 0; }
button { font: inherit; font-size: 14px; border: 1px solid #C9CED6; background: #fff; color: var(--ink);
  border-radius: 8px; padding: 9px 14px; cursor: pointer; }
button.primary { background: var(--green); border-color: var(--green); color: #fff; font-weight: 700; }
button:focus-visible { outline: 3px solid #1D4ED8; outline-offset: 2px; }
#status { width: 100%; min-height: 1.4em; margin: 0; font-size: 13px; color: var(--muted); }
.panel { background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin-top: 16px; }
.panel h2 { font-size: 15px; margin: 0 0 10px; }
.panel ol, .panel ul { margin: 0; padding-left: 20px; line-height: 1.7; font-size: 14px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.stats li { border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px; font-size: 13px; font-variant-numeric: tabular-nums; }
.stats li.bad { border-color: #F3B4AE; color: var(--bad); }
.warn { background: var(--warn-bg); border-color: #F2D08A; color: var(--warn); }
.title-field { font-size: 22px; font-weight: 700; margin: 0; line-height: 1.4; }
.paper { background: #fff; border: 1px solid var(--line); border-radius: 12px; margin-top: 16px; padding: 40px 48px; font-size: 16px; }
.tags { color: var(--green); font-size: 14px; margin: 6px 0 0; }
@media (max-width: 640px) { .paper { padding: 24px 18px; } .title-field { font-size: 19px; } }
"""

_PAGE_JS = r"""
(function () {
  var status = document.getElementById('status');
  function say(msg) { status.textContent = msg; }
  function fallbackCopyText(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta); return ok;
  }
  function copyText(text, okMsg) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { say(okMsg); }, function () {
        say(fallbackCopyText(text) ? okMsg : '자동 복사가 막혔어요. 직접 드래그해서 복사해 주세요 (Ctrl+C).');
      });
    } else {
      say(fallbackCopyText(text) ? okMsg : '자동 복사가 막혔어요. 직접 드래그해서 복사해 주세요 (Ctrl+C).');
    }
  }
  function selectAndCopy(el) {
    var sel = window.getSelection(); var range = document.createRange();
    range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
    var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    sel.removeAllRanges(); return ok;
  }
  function copyRich(el, okMsg) {
    var htmlText = el.innerHTML, plain = el.innerText;
    var fail = '자동 복사가 막혔어요. 아래 본문을 드래그해 선택한 뒤 Ctrl+C로 복사해 주세요.';
    if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
      var item = new ClipboardItem({
        'text/html': new Blob([htmlText], { type: 'text/html' }),
        'text/plain': new Blob([plain], { type: 'text/plain' })
      });
      navigator.clipboard.write([item]).then(function () { say(okMsg); }, function () {
        say(selectAndCopy(el) ? okMsg : fail);
      });
    } else {
      say(selectAndCopy(el) ? okMsg : fail);
    }
  }
  var data = JSON.parse(document.getElementById('insia-data').textContent);
  document.getElementById('copy-title').addEventListener('click', function () {
    copyText(data.title, '제목을 복사했어요. 스마트에디터 제목 칸에 붙여 넣으세요.');
  });
  document.getElementById('copy-body').addEventListener('click', function () {
    copyRich(document.getElementById('insia-fragment'), '본문을 서식째 복사했어요. 스마트에디터 본문에 붙여 넣으세요.');
  });
  document.getElementById('copy-plain').addEventListener('click', function () {
    copyText(document.getElementById('insia-fragment').innerText, '본문을 텍스트로만 복사했어요.');
  });
  var tagBtn = document.getElementById('copy-tags');
  if (tagBtn) tagBtn.addEventListener('click', function () {
    copyText(data.tags, '태그를 복사했어요. 발행 설정의 태그 칸에 붙여 넣으세요.');
  });
})();
"""


def _check_items(checks: list[FormatCheck]) -> str:
    items = []
    for check in checks:
        state = "" if check.passed else ' class="bad"'
        mark = "통과" if check.passed else "확인 필요"
        items.append(f"<li{state}>{html.escape(check.label)}: {html.escape(check.value)} "
                     f"<span>({mark}, 기준 {html.escape(check.expected)})</span></li>")
    return "".join(items)


def naver_preview_page(draft: Draft, *, brief: Brief | None = None, profile: Profile | None = None,
                       review: Review | None = None, meta: str = "") -> str:
    """Standalone page: title/body/tag copy buttons, format checks, fill-in
    warnings and the paste-ready fragment between ``FRAGMENT_START/END``."""
    fragment = naver_fragment(draft)
    tags = [t.strip() for t in draft.hashtags if t.strip()]
    checks = check_format(draft, brief, profile)
    placeholders = find_placeholders(draft.content or "")
    title = draft.title.strip()
    spec = CHANNELS["naver_blog"]

    stats = [
        f"<li>본문 {chars_no_space(draft.content or ''):,}자 (공백 제외)</li>",
        f"<li>제목 {chars_with_space(title)}자</li>",
        f"<li>태그 {len(tags)}개</li>",
    ]
    if review is not None:
        stats.append(f"<li>검수 {review.score}점 ({'통과' if review.passed else '미통과'})</li>")

    warn_panel = ""
    if placeholders:
        lis = "".join(f"<li>{html.escape(p)}</li>" for p in placeholders[:20])
        more = f"<li>외 {len(placeholders) - 20}곳</li>" if len(placeholders) > 20 else ""
        warn_panel = (
            '<section class="panel warn" aria-label="채워야 할 자리">'
            f"<h2>게시 전에 채우거나 지워야 할 자리 {len(placeholders)}곳</h2>"
            f"<ul>{lis}{more}</ul></section>"
        )

    failed = [c for c in checks if not c.passed]
    check_panel = (
        '<section class="panel" aria-label="형식 점검">'
        f"<h2>형식 점검 ({len(checks) - len(failed)}/{len(checks)} 통과)</h2>"
        f'<ul class="stats">{_check_items(checks)}</ul></section>'
    ) if checks else ""

    data_json = json.dumps({"title": title, "tags": " ".join(tags)}, ensure_ascii=False).replace("</", "<\\/")
    tag_button = '<button type="button" id="copy-tags">태그 복사</button>' if tags else ""
    tag_line = f'<p class="tags">{html.escape(" ".join(tags))}</p>' if tags else ""
    meta_html = f'<p id="meta" style="margin:8px 0 0;font-size:13px;color:#5B616B;">{html.escape(meta)}</p>' if meta else ""

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title or '네이버 블로그 초안')} · {spec.label} 붙여넣기</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<header class="bar"><div class="wrap">
  <h1>{spec.label} 붙여넣기</h1>
  <button type="button" id="copy-title">제목 복사</button>
  <button type="button" class="primary" id="copy-body">본문 복사 (서식 포함)</button>
  <button type="button" id="copy-plain">본문 텍스트만 복사</button>
  {tag_button}
  <p id="status" role="status" aria-live="polite"></p>
</div></header>
<main class="wrap">
  <section class="panel" aria-label="사용 방법">
    <h2>올리는 순서</h2>
    <ol>
      <li>네이버 블로그 글쓰기(스마트에디터 ONE)를 열어요.</li>
      <li><b>제목 복사</b> → 제목 칸에, <b>본문 복사</b> → 본문에 붙여 넣어요.</li>
      <li>초록 점선 상자(이미지 자리)를 실제 사진으로 바꾸고 상자는 지워요.</li>
      <li>노란색으로 표시된 자리는 내용을 채우거나 지워요.</li>
      <li>발행 설정에서 태그를 확인하고, 사람이 최종 확인한 뒤에 발행해요.</li>
    </ol>
  </section>
  {warn_panel}
  <section class="panel" aria-label="요약">
    <ul class="stats">{''.join(stats)}</ul>
    {meta_html}
  </section>
  {check_panel}
  <section class="paper" aria-label="본문 미리보기">
    <p class="title-field">{html.escape(title)}</p>
    {tag_line}
    <hr style="border:0;border-top:1px solid #E4E7EC;margin:20px 0 8px;">
    <article id="insia-fragment">
{FRAGMENT_START}
{fragment}
{FRAGMENT_END}
    </article>
  </section>
</main>
<script type="application/json" id="insia-data">{data_json}</script>
<script>{_PAGE_JS}</script>
</body>
</html>
"""


def extract_fragment(page: str) -> str:
    """The paste-ready fragment inside a preview page (for tests and tools)."""
    start = page.find(FRAGMENT_START)
    end = page.find(FRAGMENT_END)
    if start < 0 or end < 0:
        return ""
    return page[start + len(FRAGMENT_START):end].strip()
