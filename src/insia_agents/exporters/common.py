"""Shared pieces for the exporters.

- ``ExportFile`` (the result every exporter returns) and the exporter errors.
- File naming: ``<YYYY-MM-DD>_<channel>_<slug>_v<N>.<ext>``.
- Picking the version to export and the date used in file names.
- Text cleanup for XML/HTML outputs (``clean_draft``): characters XML 1.0
  forbids (NUL, ESC, the vertical tab PowerPoint uses as a line break, the
  form feed PDF copies carry …) are removed or turned into line breaks.
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
from typing import Collection, Iterator, Literal
from urllib.parse import quote

from ..config import KST, today_kst
from ..models import ContentItemDetail, Draft, DraftVersion

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

    def save(self, directory: str | Path, *, overwrite: bool = False) -> Path:
        """Write the file into ``directory`` (created if needed) and return its path.

        An existing file is never replaced silently: a byte-identical file is
        reused as is, anything else (another version, a file the founder
        edited in place) is kept and the new file is saved as
        ``<name> (2).<ext>``, ``<name> (3).<ext>`` …. ``overwrite=True``
        replaces the file instead.
        """
        name = Path(self.filename).name
        if not name or name in {".", ".."}:
            raise ExportError(f"저장할 수 없는 파일 이름이에요: {self.filename!r}")
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        if overwrite:
            path.write_bytes(self.data)
            return path
        stem, suffix = path.stem, path.suffix
        for counter in range(2, 1002):
            try:
                with open(path, "xb") as handle:  # exclusive create: never clobbers a file
                    handle.write(self.data)
                return path
            except FileExistsError:
                if path.is_file() and path.stat().st_size == len(self.data) and path.read_bytes() == self.data:
                    return path
            path = target_dir / f"{stem} ({counter}){suffix}"
        raise ExportError(f"같은 이름의 파일이 너무 많아 저장하지 못했어요: {target_dir / name}")


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


def export_filename(day: str, channel: str, title: str, ext: str, version: int | None = None) -> str:
    """``<day>_<channel>_<slug>[_v<N>].<ext>``. Item exports always pass the
    version so exporting v1 never lands on the file of v2."""
    suffix = f"_v{version}" if version is not None else ""
    return f"{day}_{channel}_{slugify(title)}{suffix}.{ext}"


def ascii_filename(filename: str) -> str:
    """ASCII-only fallback for the legacy ``filename=`` parameter."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    folded = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    folded = _DASHES.sub("-", re.sub(r"[^A-Za-z0-9._-]", "-", folded))
    folded = re.sub(r"[-_]*_[-_]*", "_", folded).strip("-._")  # "…_linkedin_-_v2" → "…_linkedin_v2"
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
# Text cleanup (XML / HTML outputs)
# ---------------------------------------------------------------------------

# Characters XML 1.0 forbids (lxml/python-docx raise ValueError on them).
# \x0b (PowerPoint's soft line break) and \x0c (page break in text copied
# from PDF/Word) are handled first: they become line breaks.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_SOFT_BREAK = re.compile("[\x0b\x0c]")


def xml_safe(text: str, *, soft_break: str = "\n") -> str:
    """``text`` without characters XML 1.0 forbids; ``\\x0b``/``\\x0c`` become ``soft_break``."""
    if not text:
        return text or ""
    if not _XML_ILLEGAL.search(text):
        return text
    return _XML_ILLEGAL.sub("", _SOFT_BREAK.sub(soft_break, text))


def clean_markdown(text: str) -> str:
    """Channel markdown without XML-illegal characters. Soft breaks become
    real line breaks, except inside a table row where they become ``<br>``
    (a line break inside the cell) so the row is not split in two."""
    if not text or not _XML_ILLEGAL.search(text):
        return text or ""
    lines = []
    for line in text.split("\n"):
        in_table = line.lstrip().startswith("|")
        lines.append(xml_safe(line, soft_break="<br>" if in_table else "\n"))
    return "\n".join(lines)


def clean_draft(draft: Draft) -> Draft:
    """A copy of ``draft`` that is safe to put into XML/HTML (see ``xml_safe``)."""
    title = xml_safe(draft.title or "", soft_break=" ")
    content = clean_markdown(draft.content or "")
    hashtags = [xml_safe(tag, soft_break="") for tag in draft.hashtags]
    if title == draft.title and content == draft.content and hashtags == list(draft.hashtags):
        return draft
    return draft.model_copy(update={"title": title, "content": content, "hashtags": hashtags})


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
# Bracketed text that is NOT a fill-in placeholder:
# - citations [s12], [s2, s5], [s2·s5], [s3~s5];
# - document labels [별첨 1], [표 2];
# - markdown checkboxes [ ], [x];
# - ad disclosures the founder must keep, only in their literal form: the
#   bracket holds nothing but disclosure words ([광고], [협찬], [유료 광고 포함],
#   [광고·협찬], [AD], [Sponsored]). "[광고 예산]", "[스폰서 이름]" or
#   "[광고 문구: …]" are fill-ins and stay placeholders; a longer disclosure
#   sentence is protected through ``Profile.required_phrases`` (``keep=``);
# - blog labels that are just the word ([TIP], [팁], [공지], [이벤트]).
_DISCLOSURE_WORD = r"(?:유료\s*광고|광고|협찬|제휴|스폰서(?:십)?|체험단|(?i:ads?|sponsored|paid|partnership|affiliate))"
_DISCLOSURE_TAIL = rf"(?:{_DISCLOSURE_WORD}|포함|제공)"
_NOT_PLACEHOLDER = (
    r"(?!\s*s\d+(?:\s*[,·/~\-]\s*s?\d+)*\s*\])"
    r"(?!\s*(?:별첨|붙임|첨부|참고|표|그림)\s*\d)"
    r"(?!\s*[xX✓✔]?\s*\])"
    rf"(?!\s*{_DISCLOSURE_WORD}(?:\s*(?:[·・/,&+]|및|또는)?\s*{_DISCLOSURE_TAIL})*\s*\])"
    r"(?!\s*(?:팁|공지|이벤트|(?i:tip|pr|notice))\s*\d*\s*\])"
)
# URLs, or bracketed fill-in placeholders such as [대표자 성명] / [확인 필요: …] / [○].
# Markdown links [text](url) are not placeholders. A URL never runs into a square
# bracket (outside an IPv6 host), so "…kosis.kr[확인 필요: 기준연도]" keeps its
# placeholder, nor into CJK punctuation or full-width forms (，。「」). Korean
# particles glued to it are handled in ``_url_length`` ("…go.kr에서" and
# "…go.kr/에서" are not part of the link).
_INLINE = re.compile(
    r"(?P<url>https?://(?:\[[0-9A-Fa-f:.]+\])?[^\s<>\"'\[\]　-〿＀-￯]*)"
    rf"|(?P<ph>\[{_NOT_PLACEHOLDER}[^\[\]\n]{{1,80}}\](?!\())"
)
_URL_TRAIL = ".,;:!?)」』]>"
_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏ꥠ-꥿가-힯]")
_URL_SEGMENT_START = frozenset("/=?&#-_+~(")
# A Hangul run that is nothing but a particle or copula ending. Right after a
# slash at the end of a URL ("…go.kr/에서", "…/path/을") it is sentence text:
# browsers copy root URLs with a trailing slash.
_PARTICLES = frozenset((
    "이", "가", "은", "는", "을", "를", "의", "에", "도", "로", "와", "과", "만", "나", "랑", "께", "요",
    "에서", "에게", "에는", "에도", "에만", "에선", "으로", "이나", "이랑", "하고", "처럼", "까지", "부터", "보다",
    "마다", "이며", "이고", "이다", "이죠", "이요", "이야", "에요", "예요", "라는", "라고", "인데",
    "에서는", "에서도", "에서의", "에서만", "에서요", "으로는", "으로도", "으로의", "으로요", "이라는", "이라고",
    "이에요", "입니다", "인데요", "이지만",
))
# Endings that practically never end a Korean noun, so they can be split off a
# Hangul path segment at the end of a URL ("…/wiki/소상공인에서"). One-syllable
# particles that often end nouns (이, 가, 의, 도, 과 …) are left alone.
_PARTICLE_SUFFIXES = (
    "에서는", "에서도", "에서의", "에서만", "으로는", "으로도", "으로의", "이라는", "이라고", "이에요", "입니다",
    "에서", "으로", "에게", "에는", "까지", "부터", "처럼", "를",
)


def _has_final_consonant(syllable: str) -> bool:
    code = ord(syllable) - 0xAC00
    return 0 <= code <= 11171 and code % 28 != 0


def _particle_suffix_length(run: str) -> int:
    """Length of a particle glued to the end of a Hangul path segment, or 0."""
    for suffix in _PARTICLE_SUFFIXES:
        if len(run) > len(suffix) and run.endswith(suffix):
            return len(suffix)
    if len(run) > 1:
        # 을/은 follow a final consonant (소상공인을), 는 a vowel (블로그는)
        if run[-1] in "을은" and _has_final_consonant(run[-2]):
            return 1
        if run[-1] == "는" and not _has_final_consonant(run[-2]):
            return 1
    return 0


def _url_length(url: str) -> int:
    """Length of the real URL at the start of ``url``.

    Korean attaches particles straight to a URL. The URL ends before Hangul
    glued to the host or to other URL characters ("…go.kr에서", "…/path를"),
    before a bare particle after a trailing slash ("…go.kr/에서") and before a
    particle glued to a Hangul segment at the very end ("…/wiki/소상공인에서").
    Hangul that starts a path or query segment ("…/wiki/소상공인",
    "?query=소상공인") and internationalized host labels ("http://한국.kr",
    "…/도메인.한국") are kept.
    """
    scheme_end = url.find("://") + 3
    host_end = next((i for i in range(scheme_end, len(url)) if url[i] in "/?#"), len(url))
    index = scheme_end
    while index < len(url):
        if not _HANGUL.match(url[index]):
            index += 1
            continue
        end = index
        while end < len(url) and _HANGUL.match(url[end]):
            end += 1
        run, previous = url[index:end], url[index - 1]
        if index < host_end:
            if previous in "/." and url[end:end + 1] == ".":
                index = end  # a Hangul domain label: "한국" in http://한국.kr
                continue
            if previous == "." and run.startswith("한국") and (run == "한국" or run[2:] in _PARTICLES):
                index += 2  # the .한국 top-level domain, maybe followed by a particle
                if index < end:
                    return index
                continue
            return index  # "…go.kr에서"
        if previous not in _URL_SEGMENT_START:
            return index  # glued to the URL: "…/path를"
        if not url[end:].rstrip(_URL_TRAIL):  # the run ends the URL
            if previous == "/" and run in _PARTICLES:
                return index  # "…go.kr/에서": keep the slash, drop the particle
            cut = _particle_suffix_length(run)
            if cut:
                return end - cut  # "…/wiki/소상공인에서"
        index = end
    return len(url)


def _trim_url(url: str) -> str:
    """The URL without trailing punctuation or particles (see ``_url_length``)."""
    url = url[:_url_length(url)]
    trimmed = url.rstrip(_URL_TRAIL)
    # keep a closing bracket that belongs to the URL: …/Foo_(bar), http://[::1]
    while trimmed != url:
        closing = url[len(trimmed)]
        opening = {")": "(", "]": "["}.get(closing)
        if opening is None or trimmed.count(opening) <= trimmed.count(closing):
            break
        trimmed += closing
    return trimmed


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
    pos = 0  # start of the text not yielded yet
    scan = 0  # where the next search starts
    while True:
        match = _INLINE.search(text, scan)
        if match is None:
            break
        start = match.start()
        if match.group("url"):
            url = _trim_url(match.group("url"))
            if len(url) <= url.find("://") + 3:  # nothing left but the scheme
                scan = match.end()
                continue
            end = start + len(url)
            if start > pos:
                yield Segment(text[pos:start], bold)
            yield Segment(url, bold, "url")
        else:
            end = match.end()
            if start > pos:
                yield Segment(text[pos:start], bold)
            yield Segment(match.group("ph"), bold, "placeholder")
        pos = scan = end
    if pos < len(text):
        yield Segment(text[pos:], bold)


def strip_inline(text: str) -> str:
    """Plain text of a markdown line (bold markers removed)."""
    return _BOLD.sub(r"\1", text)


def is_kept_phrase(placeholder: str, keep: Collection[str] = ()) -> bool:
    """True when ``placeholder`` is (part of) a phrase that must stay in the
    text, e.g. a ``Profile.required_phrases`` entry such as "[광고]"."""
    return any(placeholder in phrase for phrase in keep if phrase)


def find_placeholders(text: str, keep: Collection[str] = ()) -> list[str]:
    """Distinct fill-in placeholders in order of appearance (image slots and
    phrases in ``keep`` — e.g. the profile's required phrases — excluded)."""
    seen: list[str] = []
    for match in _INLINE.finditer(text or ""):
        ph = match.group("ph")
        if ph and not _IMAGE.match(ph) and ph not in seen and not is_kept_phrase(ph, keep):
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
