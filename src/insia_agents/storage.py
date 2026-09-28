"""Run outputs on disk: ``outputs/<run_id>/``.

Layout: ``brief.json``, ``plan.json``, ``research.json``, ``<channel>.md``
(final text incl. title and hashtags), ``<channel>.review.json`` (review of
the final draft), ``result.json`` (RunResult) and ``events.jsonl`` (written
live by the event bus).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Draft, RunResult

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def run_dir(out_dir: str | Path, run_id: str) -> Path:
    if not SAFE_ID.match(run_id) or ".." in run_id:
        raise ValueError(f"unsafe run id {run_id!r}")
    return Path(out_dir) / run_id


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_markdown(draft: Draft) -> str:
    """Final channel text as a standalone markdown file."""
    content = draft.content.strip()
    tag_line = " ".join(draft.hashtags)
    if draft.channel == "bizplan":
        text = content if content.lstrip().startswith("# ") else f"# {draft.title}\n\n{content}"
    else:
        text = f"# {draft.title}\n\n{content}"
    if tag_line:
        last = content.splitlines()[-1] if content else ""
        if not all(tag in last for tag in draft.hashtags):
            label = "태그" if draft.channel == "naver_blog" else "해시태그"
            text += f"\n\n{label}: {tag_line}"
    return text.rstrip() + "\n"


def save_run(result: RunResult, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "brief.json", result.brief.model_dump(mode="json"))
    _write_json(directory / "plan.json", result.plan.model_dump(mode="json"))
    _write_json(directory / "research.json", result.research.model_dump(mode="json"))
    for channel_result in result.results:
        channel = channel_result.channel
        (directory / f"{channel}.md").write_text(render_markdown(channel_result.final), encoding="utf-8")
        final_review = next((r for r in channel_result.reviews if r.round == channel_result.final.round), None)
        if final_review is not None:
            _write_json(directory / f"{channel}.review.json", final_review.model_dump(mode="json"))
    _write_json(directory / "result.json", result.model_dump(mode="json"))
    return directory


def load_result(directory: str | Path) -> RunResult:
    return RunResult.model_validate_json((Path(directory) / "result.json").read_text(encoding="utf-8"))
