"""Shared pieces for the exporters.

- ``ExportFile`` (the result every exporter returns) and the exporter errors.
- File naming: ``<YYYY-MM-DD>_<channel>_<slug>.<ext>``.
- Picking the version to export and the date used in file names.
- A small markdown block parser tuned to the channel formats in the build
  spec: 개조식 markers (□ ○ - ※ →), pipe tables with a separator row,
  ``< 표 제목 >`` captions and ``[이미지: …]`` slots. It is intentionally not a
  full CommonMark parser: drafts are produced by our own prompts.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import quote

from ..config import KST, today_kst
from ..models import ContentItemDetail, DraftVersion

CONTENT_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "zip": "application/zip",
    "json": "application/json; charset=utf-8",
    "png": "image/png",
}

EXTRA_HINT = 'pip install "insia-smartagent[{extra}]"'


class ExportError(ValueError):
    """Export failed. ``str(exc)`` is a Korean message that can be shown to the user."""


class MissingDependencyError(ExportError):
    """An optional extra (python-docx for ``[export]``, playwright for ``[render]``) is missing."""

    def __init__(self, message: str, *, package: str, extra: str) -> None:
        super().__init__(message)
        self.package = package
        self.extra = extra


@dataclass(frozen=True)
class ExportFile:
    """One downloadable file. ``notes`` carries Korean warnings worth showing
    next to the download (e.g. "PNG 대신 slides.html을 넣었어요")."""

    filename: str
    content_type: str
    data: bytes
    notes: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.data)

    def content_disposition(self, disposition: str = "attachment") -> str:
        """``Content-Disposition`` value with an ASCII fallback and an RFC 5987
        UTF-8 ``filename*`` so Korean names survive every browser."""
        return (f'{disposition}; filename="{ascii_filename(self.filename)}"; '
                f"filename*=UTF-8''{quote(self.filename, safe='')}")

    def save(self, directory: str | Path) -> Path:
        """Write the file into ``directory`` (created if needed) and return its path."""
        name = Path(self.filename).name
        if not name or name in {".", ".."}:
            raise ExportError(f"저장할 수 없는 파일 이름이에요: {self.filename!r}")
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_bytes(self.data)
        return path


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

_SLUG_UNSAFE = re.compile(r"[^\w-]|_", re.UNICODE)
_DASHES = re.compile(r"-{2,}")


def slugify(text: str, max_len: int = 40, fallback: str = "초안") -> str:
    """File-name-safe slug. Korean is kept; anything that is not a letter,
    digit or ``-`` becomes ``-`` (so ``_`` stays free as the field separator)."""
    text = unicodedata.normalize("NFC", text or "")
    text = _SLUG_UNSAFE.sub("-", text)
    text = _DASHES.sub("-", text).strip("-.")
    if len(text) > max_len:
        cut = text[:max_len]
        dash = cut.rfind("-")
        if dash >= max_len // 2:
            cut = cut[:dash]
        text = cut.strip("-.")
    return text or fallback


def export_filename(day: str, channel: str, title: str, ext: str) -> str:
    return f"{day}_{channel}_{slugify(title)}.{ext}"


def ascii_filename(filename: str) -> str:
    """ASCII-only fallback for the legacy ``filename=`` parameter."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    folded = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    folded = _DASHES.sub("-", re.sub(r"[^A-Za-z0-9._-]", "-", folded)).strip("-._")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)
    folded = folded or "insia-export"
    return f"{folded}.{ext}" if ext else folded


# ---------------------------------------------------------------------------
# Version and date selection
# ---------------------------------------------------------------------------


def select_version(detail: ContentItemDetail, version: int | None = None) -> DraftVersion:
    """The version to export: ``version`` when given, else the item's current
    version (``item.version``), else the highest version number."""
    if not detail.versions:
        raise ExportError("내보낼 초안이 아직 없어요. 초안을 만든 뒤 다시 시도해 주세요.")
    if version is not None:
        for candidate in detail.versions:
            if candidate.version == version:
                return candidate
        have = ", ".join(f"v{v.version}" for v in sorted(detail.versions, key=lambda v: v.version))
        raise ExportError(f"v{version} 버전을 찾을 수 없어요. 있는 버전: {have}")
    current = [v for v in detail.versions if v.version == detail.item.version]
    if current:
        return current[-1]
    return max(detail.versions, key=lambda v: (v.version, v.created_at))


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def to_kst_date(value: str | None) -> str | None:
    """``YYYY-MM-DD`` in Korea time for an ISO date or timestamp; None if unparsable."""
    value = (value or "").strip()
    if not value:
        return None
    if _DATE_ONLY.match(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        match = _DATE_PREFIX.match(value)
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.date().isoformat()
    return parsed.astimezone(KST).date().isoformat()


def item_date(detail: ContentItemDetail, version: DraftVersion | None = None) -> str:
    """Date used in file names: published → scheduled → created → version → today (KST)."""
    item = detail.item
    for value in (item.published_at, item.scheduled_at, item.created_at, version.created_at if version else ""):
        day = to_kst_date(value)
        if day:
            return day
    return today_kst()


# ---------------------------------------------------------------------------
# Markdown blocks
# ---------------------------------------------------------------------------

BlockKind = Literal["heading", "para", "item", "table", "image", "caption", "rule", "quote"]


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    level: int = 0  # heading level (1-6) or item indent level (0 = no indent)
    marker: str = ""  # item marker as written (□, -, ※, 1. …)
    lines: list[str] = field(default_factory=list)  # para lines
    header: list[str] = field(default_factory=list)  # table header cells
    rows: list[list[str]] = field(default_factory=list)  # table body rows
    aligns: list[str] = field(default_factory=list)  # "", "left", "center", "right" per column


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_IMAGE = re.compile(r"^\[\s*이미지\s*[:：]?\s*(.*?)\s*\]$")
_CAPTION = re.compile(r"^<\s*([^<>]+?)\s*>$")
_TAG_LIKE = re.compile(r"^/?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<marker>[□■◆◇○●◦▪•※→▶►]|[-*·](?=\s)|\d{1,2}[.)](?=\s))"
    r"\s*(?P<text>.*)$"
)
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE_SEP_CHARS = re.compile(r"^[\s|:\-]+$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")

TOP_LEVEL_MARKERS = frozenset("□■◆◇")


def _indent_width(prefix: str) -> int:
    return sum(4 if ch == "\t" else 1 for ch in prefix)


def is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and "-" in stripped and "|" in stripped and bool(_TABLE_SEP_CHARS.match(stripped))


def split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT.split(stripped)]


def _aligns(separator: str, ncols: int) -> list[str]:
    out: list[str] = []
    for cell in split_table_row(separator):
        cell = cell.strip()
        if cell.startswith(":") and cell.endswith(":"):
            out.append("center")
        elif cell.endswith(":"):
            out.append("right")
        elif cell.startswith(":"):
            out.append("left")
        else:
            out.append("")
    return (out + [""] * ncols)[:ncols]


def parse_blocks(text: str) -> list[Block]:
    """Split channel markdown into blocks (see module docstring)."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("para", lines=list(para)))
            para.clear()

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        if not stripped:
            flush()
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and is_table_separator(lines[i + 1]):
            flush()
            header = split_table_row(stripped)
            separator = lines[i + 1]
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_table_row(lines[i]))
                i += 1
            ncols = max([len(header), *(len(r) for r in rows)])
            header = (header + [""] * ncols)[:ncols]
            rows = [(r + [""] * ncols)[:ncols] for r in rows]
            blocks.append(Block("table", header=header, rows=rows, aligns=_aligns(separator, ncols)))
            continue

        indent = _indent_width(raw[: len(raw) - len(raw.lstrip())])
        heading = _HEADING.match(stripped) if indent < 4 else None
        if heading:
            flush()
            blocks.append(Block("heading", text=heading.group(2), level=len(heading.group(1))))
            i += 1
            continue

        image = _IMAGE.match(stripped)
        if image:
            flush()
            blocks.append(Block("image", text=image.group(1)))
            i += 1
            continue

        caption = _CAPTION.match(stripped)
        if caption and not _TAG_LIKE.match(caption.group(1)):
            flush()
            blocks.append(Block("caption", text=caption.group(1)))
            i += 1
            continue

        if _RULE.match(stripped):
            flush()
            blocks.append(Block("rule"))
            i += 1
            continue

        quote_match = _QUOTE.match(stripped)
        if quote_match:
            flush()
            blocks.append(Block("quote", text=quote_match.group(1)))
            i += 1
            continue

        item = _ITEM.match(raw)
        if item and item.group("text").strip():
            flush()
            blocks.append(Block(
                "item",
                text=item.group("text").strip(),
                level=_indent_width(item.group("indent")) // 2,
                marker=item.group("marker"),
            ))
            i += 1
            continue

        # An indented plain line right after an item continues that item.
        if indent >= 2 and not para and blocks and blocks[-1].kind == "item":
            blocks[-1].text += "\n" + stripped
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush()
    return blocks


# ---------------------------------------------------------------------------
# Inline segments
# ---------------------------------------------------------------------------

SegmentKind = Literal["text", "url", "placeholder"]


@dataclass(frozen=True)
class Segment:
    text: str
    bold: bool = False
    kind: SegmentKind = "text"


_BOLD = re.compile(r"\*\*(.+?)\*\*")
# URLs, or bracketed fill-in placeholders such as [대표자 성명] / [확인 필요: …] / [○].
# Citations like [s12] and markdown links [text](url) are not placeholders.
_INLINE = re.compile(
    r"(?P<url>https?://[^\s<>\"'「」]+)"
    r"|(?P<ph>\[(?!s\d+\])[^\[\]\n]{1,80}\](?!\())"
)
_URL_TRAIL = ".,;:!?)」』]>"


def inline_segments(text: str) -> Iterator[Segment]:
    """Split a line into bold / URL / placeholder segments."""
    pos = 0
    for match in _BOLD.finditer(text):
        if match.start() > pos:
            yield from _plain_segments(text[pos:match.start()], False)
        yield from _plain_segments(match.group(1), True)
        pos = match.end()
    if pos < len(text):
        yield from _plain_segments(text[pos:], False)


def _plain_segments(text: str, bold: bool) -> Iterator[Segment]:
    pos = 0
    for match in _INLINE.finditer(text):
        start, end = match.span()
        if match.group("url"):
            url = match.group("url")
            trimmed = url.rstrip(_URL_TRAIL)
            # keep a closing parenthesis that belongs to the URL, e.g. …/Foo_(bar)
            while trimmed != url and url[len(trimmed)] == ")" and trimmed.count("(") > trimmed.count(")"):
                trimmed += ")"
            end = start + len(trimmed)
            if start > pos:
                yield Segment(text[pos:start], bold)
            yield Segment(trimmed, bold, "url")
        else:
            if start > pos:
                yield Segment(text[pos:start], bold)
            yield Segment(match.group("ph"), bold, "placeholder")
        pos = end
    if pos < len(text):
        yield Segment(text[pos:], bold)


def strip_inline(text: str) -> str:
    """Plain text of a markdown line (bold markers removed)."""
    return _BOLD.sub(r"\1", text)


def find_placeholders(text: str) -> list[str]:
    """Distinct fill-in placeholders in order of appearance (image slots excluded)."""
    seen: list[str] = []
    for match in _INLINE.finditer(text or ""):
        ph = match.group("ph")
        if ph and not _IMAGE.match(ph) and ph not in seen:
            seen.append(ph)
    return seen


_HASHTAG_LINE = re.compile(r"^#\S+(?:\s+#\S+)*$")


def is_hashtag_line(line: str) -> bool:
    return bool(_HASHTAG_LINE.match(line.strip()))


def ensure_hashtag_line(text: str, hashtags: list[str]) -> str:
    """Make ``text`` end with a line containing every tag in ``hashtags``.

    - already there → unchanged;
    - the last line is a hashtag-only line missing some tags → the missing
      tags are added to that line (nothing the author wrote is dropped);
    - otherwise the tags are appended as a new last line.
    """
    body = (text or "").rstrip()
    tags = [t.strip() for t in hashtags if t and t.strip()]
    if not tags:
        return body
    lines = body.split("\n") if body else []
    last = lines[-1].strip() if lines else ""
    present = set(last.split())
    if all(tag in present for tag in tags):
        return body
    if last and is_hashtag_line(last):
        missing = [t for t in tags if t not in present]
        lines[-1] = last + " " + " ".join(missing)
        return "\n".join(lines)
    return f"{body}\n\n{' '.join(tags)}" if body else " ".join(tags)


def content_section(text: str, name: str) -> str:
    """Body under a ``## <name>…`` heading, up to the next ``## `` heading."""
    pattern = re.compile(rf"^##\s*{re.escape(name)}[^\n]*$", re.MULTILINE)
    match = pattern.search(text or "")
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^##\s+\S", rest, re.MULTILINE)
    return (rest[: nxt.start()] if nxt else rest).strip()
