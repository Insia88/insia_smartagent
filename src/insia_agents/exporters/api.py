"""Public export entry points: ``export_item`` and ``export_run_zip``.

Pure functions over the data models — no DB writes. ``export_run_zip`` only
reads through the Workspace API (``get_run``, ``list_items``, ``get_item`` and,
when present, ``get_profile``).
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ..channels import CHANNELS
from ..config import today_kst
from ..models import ALL_CHANNELS, Brief, ContentItemDetail, Draft, DraftVersion, Profile, ResearchPack, Review
from .common import (CONTENT_TYPES, ExportError, ExportFile, MissingDependencyError, export_filename, item_date,
                     select_version, slugify, to_kst_date)

CHANNEL_FORMATS: dict[str, tuple[str, ...]] = {
    # first = the recommended format for that channel
    "bizplan": ("docx", "md", "txt", "zip"),
    "naver_blog": ("html", "md", "txt", "docx", "zip"),
    "linkedin": ("txt", "md", "docx", "zip"),
    "instagram": ("zip", "txt", "md", "docx"),
}

FORMAT_LABELS: dict[str, str] = {
    "md": "마크다운 (.md)",
    "txt": "붙여넣기용 텍스트 (.txt)",
    "html": "네이버 블로그 붙여넣기 (.html)",
    "docx": "Word·한글 문서 (.docx)",
    "zip": "묶음 파일 (.zip)",
}

STATUS_LABELS = {
    "draft": "초안", "needs_changes": "수정 필요", "approved": "승인됨",
    "scheduled": "게시 예정", "published": "게시 완료", "archived": "보관됨",
}


def formats_for(channel: str) -> tuple[str, ...]:
    """Export formats available for a channel (recommended first)."""
    return CHANNEL_FORMATS.get(channel, ("md",))


def format_label(fmt: str, channel: str | None = None) -> str:
    if fmt == "zip" and channel == "instagram":
        return "캐러셀 이미지 묶음 (.zip)"
    return FORMAT_LABELS.get(fmt, fmt)


def capabilities() -> dict[str, bool]:
    """Which optional extras are importable (for health checks and the UI)."""
    return {
        "docx": importlib.util.find_spec("docx") is not None,
        "png": importlib.util.find_spec("playwright") is not None,
    }


# ---------------------------------------------------------------------------
# Item export
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ctx:
    detail: ContentItemDetail
    version: DraftVersion
    draft: Draft
    profile: Profile | None
    day: str

    @property
    def channel(self) -> str:
        return self.detail.item.channel

    @property
    def label(self) -> str:
        spec = CHANNELS.get(self.channel)  # type: ignore[call-overload]
        return spec.label if spec else self.channel

    @property
    def review(self) -> Review | None:
        return self.version.review

    def filename(self, ext: str) -> str:
        return export_filename(self.day, self.channel, self.draft.title or self.detail.item.title, ext)

    def meta(self) -> str:
        parts = [f"{self.label} 초안", f"v{self.version.version}", self.day]
        if self.review is not None:
            parts.append(f"검수 {self.review.score}점({'통과' if self.review.passed else '미통과'})")
        return " · ".join(parts)


def _coerce_profile(profile: Profile | dict | None) -> Profile | None:
    if profile is None or isinstance(profile, Profile):
        return profile
    try:
        return Profile.model_validate(profile)
    except ValidationError:
        return None


def export_item(detail: ContentItemDetail, fmt: str, profile: Profile | None = None, *,
                version: int | None = None) -> ExportFile:
    """Export one content item.

    ``fmt``: ``md`` | ``txt`` | ``html`` | ``docx`` | ``zip`` (see ``formats_for``).
    ``version``: a specific version number (default: the item's current version).
    Raises ``ExportError`` (Korean message) for unsupported formats or empty
    items, and ``MissingDependencyError`` when python-docx is not installed.
    """
    fmt = (fmt or "").strip().lower().lstrip(".")
    channel = detail.item.channel
    allowed = formats_for(channel)
    if fmt not in FORMAT_LABELS:
        raise ExportError(f"지원하지 않는 형식이에요: {fmt or '(비어 있음)'}. 가능한 형식: {', '.join(allowed)}")
    spec = CHANNELS.get(channel)  # type: ignore[call-overload]
    label = spec.label if spec else channel
    if fmt not in allowed:
        raise ExportError(f"{label} 콘텐츠는 {fmt} 형식으로 내보낼 수 없어요. 가능한 형식: {', '.join(allowed)}")
    chosen = select_version(detail, version)
    ctx = _Ctx(detail=detail, version=chosen, draft=chosen.draft, profile=_coerce_profile(profile),
               day=item_date(detail, chosen))
    return _EXPORTERS[fmt](ctx)


def _export_md(ctx: _Ctx) -> ExportFile:
    from .text import markdown_text

    return ExportFile(ctx.filename("md"), CONTENT_TYPES["md"], markdown_text(ctx.draft).encode("utf-8"))


def _export_txt(ctx: _Ctx) -> ExportFile:
    from .text import paste_text

    return ExportFile(ctx.filename("txt"), CONTENT_TYPES["txt"], paste_text(ctx.draft).encode("utf-8"))


def _export_html(ctx: _Ctx) -> ExportFile:
    from .naver_html import naver_preview_page

    page = naver_preview_page(ctx.draft, brief=ctx.detail.brief, profile=ctx.profile, review=ctx.review,
                              meta=ctx.meta())
    return ExportFile(ctx.filename("html"), CONTENT_TYPES["html"], page.encode("utf-8"))


def _export_docx(ctx: _Ctx) -> ExportFile:
    from .docx_writer import build_docx, exposed_names

    data = build_docx(ctx.draft, meta=ctx.meta(), profile=ctx.profile, channel_label=ctx.label)
    notes: tuple[str, ...] = ()
    if ctx.channel == "bizplan":
        exposed = exposed_names(ctx.draft, ctx.profile)
        if exposed:
            notes = (f"팀원 실명 {len(exposed)}개가 본문에 있어요 ({', '.join(exposed)}). 제출 전에 ○○로 가려 주세요.",)
    return ExportFile(ctx.filename("docx"), CONTENT_TYPES["docx"], data, notes)


def _export_zip(ctx: _Ctx) -> ExportFile:
    if ctx.channel == "instagram":
        from .instagram import build_carousel_zip

        data, notes = build_carousel_zip(ctx.draft, ctx.profile, meta=ctx.meta())
        return ExportFile(ctx.filename("zip"), CONTENT_TYPES["zip"], data, notes)
    # Other channels: every other format of the item in one archive.
    buffer = io.BytesIO()
    notes: list[str] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for fmt in formats_for(ctx.channel):
            if fmt == "zip":
                continue
            try:
                exported = _EXPORTERS[fmt](ctx)
            except MissingDependencyError as exc:
                notes.append(str(exc))
                continue
            archive.writestr(exported.filename, exported.data)
            notes.extend(exported.notes)
    return ExportFile(ctx.filename("zip"), CONTENT_TYPES["zip"], buffer.getvalue(), tuple(notes))


_EXPORTERS = {"md": _export_md, "txt": _export_txt, "html": _export_html, "docx": _export_docx, "zip": _export_zip}


# ---------------------------------------------------------------------------
# Run export
# ---------------------------------------------------------------------------

RUN_PRIMARY_FORMATS: dict[str, tuple[str, ...]] = {
    "bizplan": ("docx", "md"),
    "naver_blog": ("html", "md"),
    "linkedin": ("txt", "md"),
    "instagram": ("zip", "md"),
}


def _as_dict(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _model(cls: type[BaseModel], value: Any) -> Any:
    if value is None or value == {} or value == "":
        return None
    if isinstance(value, cls):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    try:
        return cls.model_validate(_as_dict(value))
    except ValidationError:
        return None


def _profile_is_empty(profile: Profile) -> bool:
    return not any(value for key, value in profile.model_dump().items() if key != "updated_at")


def _run_profile(workspace: Any, run: dict) -> Profile | None:
    current = None
    getter = getattr(workspace, "get_profile", None)
    if callable(getter):
        try:
            current = _model(Profile, getter())
        except Exception:  # noqa: BLE001 — a profile problem must not block the export
            current = None
    if current is not None and not _profile_is_empty(current):
        return current
    return _model(Profile, run.get("profile"))


def _run_details(workspace: Any, run_id: str, run: dict) -> list[ContentItemDetail]:
    ids: list[str] = []
    try:
        items = workspace.list_items(limit=10_000)
    except TypeError:
        items = workspace.list_items()
    for item in items or []:
        item = _as_dict(item)
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", "")
        item_run = item.get("run_id") if isinstance(item, dict) else getattr(item, "run_id", "")
        if item_id and item_run == run_id and item_id not in ids:
            ids.append(item_id)
    for channel in ALL_CHANNELS:
        candidate = f"it_{run_id}_{channel}"
        if candidate not in ids:
            ids.append(candidate)
    parent = run.get("parent_item_id") or ""
    if parent and parent not in ids:
        ids.append(parent)

    details: list[ContentItemDetail] = []
    for item_id in ids:
        detail = _model(ContentItemDetail, workspace.get_item(item_id))
        if detail is not None and detail.versions:
            details.append(detail)
    order = {channel: index for index, channel in enumerate(ALL_CHANNELS)}
    details.sort(key=lambda d: (order.get(d.item.channel, 99), d.item.id))
    return details


_TIER_TITLES = {1: "Tier 1 · 공식·공공 자료", 2: "Tier 2 · 언론·리서치", 3: "Tier 3 · 기타"}
_CONFIDENCE = {"high": "높음", "medium": "보통", "low": "낮음"}


def sources_markdown(research: ResearchPack | None, *, topic: str = "", run_id: str = "", day: str = "") -> str:
    """Human-readable source list (and findings/gaps) for fact-checking."""
    title = f"# 출처 목록 — {topic}" if topic else "# 출처 목록"
    lines = [title, ""]
    if research is None:
        lines.append("이 실행에는 저장된 리서치 기록이 없어요.")
        return "\n".join(lines) + "\n"
    meta = [p for p in (f"실행 {run_id}" if run_id else "", f"{day} 기준" if day else "") if p]
    meta.append(f"출처 {len(research.sources)}개 · 근거 {len(research.findings)}개")
    lines += [" · ".join(meta), ""]

    def source_line(source) -> str:
        line = f"- [{source.id}] {source.title}"
        if source.origin == "user":
            return f"{line} — 사용자 제공 자료 ({source.url})"
        detail = ", ".join(p for p in (source.publisher, source.published) if p)
        if detail:
            line += f" — {detail}"
        if source.url:
            line += f" <{source.url}>"
        if source.accessed:
            line += f" (확인 {source.accessed})"
        return line

    web = [s for s in research.sources if s.origin != "user"]
    for tier in (1, 2, 3):
        group = [s for s in web if s.tier == tier]
        if group:
            lines += [f"## {_TIER_TITLES[tier]} ({len(group)})", ""]
            lines += [source_line(s) for s in group]
            lines.append("")
    user = [s for s in research.sources if s.origin == "user"]
    if user:
        lines += [f"## 사용자 제공 자료 ({len(user)})", ""]
        lines += [source_line(s) for s in user]
        lines.append("")
    if research.findings:
        lines += ["## 근거 (findings)", ""]
        for finding in research.findings:
            refs = ", ".join(f"[{sid}]" for sid in finding.source_ids)
            lines.append(f"- [{finding.id}] {finding.claim} — {refs}, 신뢰도 {_CONFIDENCE.get(finding.confidence, finding.confidence)}")
        lines.append("")
    if research.gaps:
        lines += ["## 확인하지 못한 부분", ""]
        lines += [f"- {gap}" for gap in research.gaps]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_run_zip(workspace: Any, run_id: str) -> ExportFile:
    """Everything a run produced in one zip: each channel's paste/upload-ready
    files (+ markdown and the latest review), ``research.json``, ``sources.md``,
    ``brief.json`` and a README."""
    raw = workspace.get_run(run_id)
    if not raw:
        raise ExportError(f"실행 기록을 찾을 수 없어요: {run_id}")
    run = _as_dict(raw)
    brief = _model(Brief, run.get("brief"))
    research = _model(ResearchPack, run.get("research"))
    profile = _run_profile(workspace, run)
    details = _run_details(workspace, run_id, run)
    day = (to_kst_date(run.get("started_at")) or to_kst_date(run.get("created_at"))
           or to_kst_date(run.get("finished_at")) or today_kst())
    root = f"{day}_run_{slugify(run_id, max_len=80, fallback='run')}"
    topic = brief.topic if brief else ""

    notes: list[str] = []
    summary: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def put(name: str, data: bytes | str) -> None:
            archive.writestr(f"{root}/{name}", data)

        for detail in details:
            channel = detail.item.channel
            spec = CHANNELS.get(channel)  # type: ignore[call-overload]
            label = spec.label if spec else channel
            if detail.brief is None and brief is not None:
                detail = detail.model_copy(update={"brief": brief})
            written: list[str] = []
            for fmt in RUN_PRIMARY_FORMATS.get(channel, ("md",)):
                try:
                    exported = export_item(detail, fmt, profile)
                except MissingDependencyError as exc:
                    notes.append(f"{label}: {exc}")
                    continue
                except ExportError as exc:
                    notes.append(f"{label} {fmt}: {exc}")
                    continue
                notes.extend(f"{label}: {n}" for n in exported.notes)
                if fmt == "zip" and channel == "instagram":
                    with zipfile.ZipFile(io.BytesIO(exported.data)) as inner:
                        for info in inner.infolist():
                            put(f"{channel}/{info.filename}", inner.read(info))
                            written.append(f"{channel}/{info.filename}")
                else:
                    put(f"{channel}/{exported.filename}", exported.data)
                    written.append(f"{channel}/{exported.filename}")
            current = select_version(detail)
            if current.review is not None:
                put(f"{channel}/review.json", json.dumps(current.review.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
            score = (f"검수 {current.review.score}점({'통과' if current.review.passed else '미통과'})"
                     if current.review is not None else "검수 전")
            status = STATUS_LABELS.get(detail.item.status, detail.item.status)
            main_file = next((w for w in written if not w.endswith(".md")), written[0] if written else "")
            summary.append(f"- {label}: \"{current.draft.title}\" — v{current.version}, {score}, {status}"
                           + (f" → {main_file}" if main_file else ""))

        if brief is not None:
            put("brief.json", json.dumps(brief.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
        if research is not None:
            put("research.json", json.dumps(research.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
        put("sources.md", sources_markdown(research, topic=topic, run_id=run_id, day=day))

        readme = ["INSIA 실행 결과 묶음", "", f"실행 ID: {run_id}"]
        if topic:
            readme.append(f"주제: {topic}")
        readme.append(f"날짜: {day}")
        if run.get("status"):
            readme.append(f"상태: {run.get('status')}")
        cost = run.get("cost_usd")
        if isinstance(cost, (int, float)) and cost > 0:
            readme.append(f"API 비용: 약 ${cost:,.2f}")
        readme += ["", "채널별 결과"]
        readme += summary or ["- 저장된 채널 결과가 없어요."]
        readme += [
            "",
            "함께 들어 있는 파일",
            "- sources.md : 출처·근거 목록 (사실 확인용)",
        ]
        if research is not None:
            readme.append("- research.json : 리서치 원자료")
        if brief is not None:
            readme.append("- brief.json : 이 실행의 브리프")
        readme += ["", "모든 파일은 AI 초안이에요. 게시·제출 전에 사람이 검토하고 승인해 주세요."]
        if notes:
            readme += ["", "참고"] + [f"- {n}" for n in notes]
        put("README.txt", "\n".join(readme) + "\n")

    return ExportFile(f"{root}.zip", CONTENT_TYPES["zip"], buffer.getvalue(), tuple(notes))
