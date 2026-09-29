"""Text exports: markdown (any channel) and paste-ready plain text."""

from __future__ import annotations

import re

from ..models import Draft
from ..storage import render_markdown
from .common import ExportError, clean_draft, content_section, ensure_hashtag_line, parse_blocks, strip_inline

# <br> is a line break in the docx and Naver HTML exports; the paste text must match.
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _line(text: str) -> str:
    """Plain text of a markdown line: ``<br>`` → line break, bold markers removed."""
    return strip_inline(_BR.sub("\n", text))


def _cell(text: str) -> str:
    """Plain text of a table cell (kept on one line so the row stays one row)."""
    return strip_inline(_BR.sub(" / ", text)).strip()


def markdown_text(draft: Draft) -> str:
    """Same text as ``outputs/<run>/<channel>.md`` (``storage.render_markdown``)."""
    return render_markdown(draft)


def instagram_caption(draft: Draft) -> str:
    """Caption only (the ``## 캡션`` section), ending with the hashtag line."""
    content = draft.content or ""
    caption = content_section(content, "캡션")
    if not caption and "## 캐러셀" not in content:
        caption = content.strip()  # caption-only content (no carousel section)
    caption = _BR.sub("\n", caption)
    if not caption.strip():
        raise ExportError("인스타그램 캡션(## 캡션)이 비어 있어요. 초안에 캡션을 채운 뒤 다시 내보내 주세요.")
    return ensure_hashtag_line(caption, draft.hashtags)


def paste_text(draft: Draft) -> str:
    """Exact text to paste into the platform (``txt`` export).

    - linkedin: the post body, ending with the hashtag line;
    - instagram: the caption only, ending with the hashtag line;
    - naver_blog / bizplan: a plain-text version without markdown symbols
      (tables become tab-separated rows, which Hangul/Word can turn back into
      tables with "문자열을 표로" / "텍스트를 표로 변환").

    ``<br>`` becomes a line break (`` / `` inside table cells) and invisible
    control characters pasted from PowerPoint/PDF are cleaned, as in the
    docx and HTML exports.
    """
    draft = clean_draft(draft)
    if draft.channel == "linkedin":
        body = _BR.sub("\n", draft.content or "").strip()
        if not body:
            raise ExportError("링크드인 본문이 비어 있어요. 초안을 채운 뒤 다시 내보내 주세요.")
        return ensure_hashtag_line(body, draft.hashtags) + "\n"
    if draft.channel == "instagram":
        return instagram_caption(draft) + "\n"
    return plain_text(draft)


def plain_text(draft: Draft) -> str:
    content = (draft.content or "").strip()
    blocks = parse_blocks(content)
    out: list[str] = []
    first_heading_is_title = bool(blocks) and blocks[0].kind == "heading" and blocks[0].level == 1
    if not first_heading_is_title and draft.title.strip():
        out.extend([draft.title.strip(), ""])

    prev: str | None = None
    for block in blocks:
        if out and out[-1] != "" and not (block.kind == "item" and prev == "item"):
            out.append("")
        if block.kind == "heading":
            out.append(_line(block.text))
        elif block.kind == "para":
            out.extend(_line(line) for line in block.lines)
        elif block.kind == "item":
            marker = "•" if block.marker in {"*"} else block.marker
            text = _line(block.text).replace("\n", "\n" + "  " * (block.level + 1))
            out.append(f"{'  ' * block.level}{marker} {text}")
        elif block.kind == "table":
            out.append("\t".join(_cell(c) for c in block.header))
            out.extend("\t".join(_cell(c) for c in row) for row in block.rows)
        elif block.kind == "image":
            out.append(f"[이미지: {block.text}]")
        elif block.kind == "caption":
            out.append(f"< {_cell(block.text)} >")
        elif block.kind == "quote":
            out.append(_line(block.text))
        prev = block.kind

    text = "\n".join(out).strip()
    if draft.channel == "naver_blog":
        text = ensure_hashtag_line(text, draft.hashtags)
    return text + "\n"
