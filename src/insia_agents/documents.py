"""User files → text: Korean text encodings, JSON/YAML, PDF and Word extraction.

Moved out of ``cli.py`` so the server and scripts can read user files the same
way (``cli`` re-exports every name). Errors carry Korean messages:
``UsageError`` = the file or its content is the problem (CLI exit code 2),
``CommandError`` = an optional package is missing (exit code 1).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import CommandError, UsageError

DOCS_EXTRA_HINT = 'pip install "insia-smartagent[docs]"'


# ---------------------------------------------------------------------------
# Text files (Korean encodings)
# ---------------------------------------------------------------------------


def read_text_file(path: Path, what: str = "파일") -> str:
    """UTF-8 (with or without BOM), then CP949/EUC-KR (Windows 메모장 'ANSI' 저장)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise UsageError(f"{what}을(를) 찾을 수 없어요: {path}") from None
    except IsADirectoryError:
        raise UsageError(f"{what} 자리에 폴더를 적었어요: {path}") from None
    except OSError as exc:
        raise UsageError(f"{what}을(를) 읽을 수 없어요: {path} ({exc.strerror or exc})") from None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UsageError(f"{what}의 글자 인코딩을 알 수 없어요: {path}. UTF-8로 다시 저장해 주세요.")


def _parse_json_text(text: str, path: Path) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"JSON 형식이 올바르지 않아요: {path} ({exc.lineno}번째 줄 {exc.colno}번째 글자 근처: {exc.msg}). "
                         "쉼표와 따옴표를 확인해 주세요.") from None


def _yaml_module() -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None
    return yaml


def _parse_yaml_text(text: str, path: Path) -> Any:
    yaml = _yaml_module()
    if yaml is None:
        raise UsageError(f"YAML 파일을 읽으려면 PyYAML이 필요해요. 설치: {DOCS_EXTRA_HINT} (또는 pip install pyyaml). "
                         "설치 없이 하려면 JSON 양식을 쓰세요: insia profile edit-template --format json")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{mark.line + 1}번째 줄 근처" if mark is not None else "위치 모름"
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise UsageError(f"YAML 형식이 올바르지 않아요: {path} ({where}: {problem}). "
                         "콜론(:)이나 #이 들어간 글은 \"큰따옴표\"로 감싸 주세요.") from None


def load_structured_file(path: Path, what: str = "파일") -> Any:
    """JSON or YAML (by extension; unknown extensions try JSON first)."""
    text = read_text_file(path, what)
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _parse_yaml_text(text, path)
    if suffix == ".json":
        return _parse_json_text(text, path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _parse_yaml_text(text, path)


# ---------------------------------------------------------------------------
# User documents: text extraction
# ---------------------------------------------------------------------------

DOC_EXTENSIONS = {".txt": "text", ".text": "text", ".csv": "text", ".tsv": "text", ".log": "text", ".json": "text",
                  ".md": "markdown", ".markdown": "markdown", ".pdf": "pdf", ".docx": "docx"}
UNSUPPORTED_DOCS = {
    ".hwp": "한글(HWP) 파일은 바로 읽을 수 없어요. 한글에서 '다른 이름으로 저장'으로 PDF나 DOCX로 바꾼 뒤 다시 올려 주세요.",
    ".hwpx": "한글(HWPX) 파일은 바로 읽을 수 없어요. 한글에서 '다른 이름으로 저장'으로 PDF나 DOCX로 바꾼 뒤 다시 올려 주세요.",
    ".doc": "예전 Word(.doc) 파일은 읽을 수 없어요. Word에서 .docx로 저장한 뒤 다시 올려 주세요.",
    ".ppt": "발표 자료는 PDF로 내보낸 뒤 올려 주세요.",
    ".pptx": "발표 자료는 PDF로 내보낸 뒤 올려 주세요.",
    ".xls": "엑셀 파일은 CSV(쉼표로 구분)로 저장한 뒤 올려 주세요.",
    ".xlsx": "엑셀 파일은 CSV(쉼표로 구분)로 저장한 뒤 올려 주세요.",
    ".key": "Keynote 파일은 PDF로 내보낸 뒤 올려 주세요.",
    ".pages": "Pages 파일은 PDF나 DOCX로 내보낸 뒤 올려 주세요.",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff"}


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf_text(path: Path) -> tuple[str, list[str]]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        raise CommandError(f"PDF를 읽으려면 pypdf가 필요해요. 설치: {DOCS_EXTRA_HINT} (또는 pip install pypdf)") from None
    notes: list[str] = []
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:  # noqa: BLE001 - e.g. AES without the cryptography package
                unlocked = 0
            if not unlocked:
                raise UsageError("암호가 걸린 PDF라 읽을 수 없어요. 암호를 푼 PDF로 저장하거나 글자를 복사해 .txt로 올려 주세요.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except UsageError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types for broken files
        raise UsageError(f"PDF를 읽지 못했어요: {path.name} ({type(exc).__name__}: {exc})") from None
    empty = sum(1 for page in pages if not page.strip())
    if pages and empty and empty < len(pages):
        notes.append(f"{len(pages)}쪽 중 {empty}쪽에서 글자를 찾지 못했어요 (이미지로 된 쪽일 수 있어요).")
    return "\n\n".join(pages), notes


def _docx_text(path: Path) -> str:
    try:
        import docx  # type: ignore[import-not-found]
        from docx.table import Table  # type: ignore[import-not-found]
        from docx.text.paragraph import Paragraph  # type: ignore[import-not-found]
    except ImportError:
        raise CommandError(f"Word(.docx) 파일을 읽으려면 python-docx가 필요해요. 설치: {DOCS_EXTRA_HINT} "
                           "(또는 pip install python-docx)") from None
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise UsageError(f"Word 파일을 읽지 못했어요: {path.name} ({type(exc).__name__}: {exc})") from None
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            if text and (style.startswith("Heading") or style.startswith("제목") or style == "Title"):
                text = f"## {text}"
            lines.append(text)
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = " ".join(cell.text.split())
                    if not cells or cells[-1] != value:  # merged cells repeat
                        cells.append(value)
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
    return "\n".join(lines)


def extract_document(path: Path) -> tuple[str, str, list[str]]:
    """``(text, kind, notes)`` for a user file. Refuses empty extraction (Korean ``UsageError``)."""
    if not path.exists():
        raise UsageError(f"파일을 찾을 수 없어요: {path}")
    if path.is_dir():
        raise UsageError(f"폴더가 아니라 파일을 지정해 주세요: {path}")
    suffix = path.suffix.lower()
    if suffix in UNSUPPORTED_DOCS:
        raise UsageError(UNSUPPORTED_DOCS[suffix])
    if suffix in IMAGE_EXTENSIONS:
        raise UsageError("이미지 속 글자는 읽을 수 없어요. 글자를 옮겨 적은 .txt 파일이나 글자가 있는 PDF를 올려 주세요.")
    kind = DOC_EXTENSIONS.get(suffix)
    if kind is None:
        supported = ", ".join(sorted(DOC_EXTENSIONS))
        raise UsageError(f"읽을 수 없는 파일 형식이에요: {path.name} (가능: {supported})")
    notes: list[str] = []
    if kind == "pdf":
        text, notes = _pdf_text(path)
    elif kind == "docx":
        text = _docx_text(path)
    else:
        text = read_text_file(path, "자료 파일")
    text = clean_text(text)
    if not text:
        if kind == "pdf":
            raise UsageError("PDF에서 글자를 찾지 못했어요. 스캔한 이미지 PDF일 수 있어요. 글자를 복사해 .txt로 저장해 올리거나, "
                             "글자 인식(OCR)을 거친 PDF를 올려 주세요.")
        raise UsageError(f"파일에 글자가 없어요: {path.name}")
    return text, kind, notes
