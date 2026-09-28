#!/usr/bin/env python3
"""Build the claude.ai Artifact version of the dashboard.

Output: ``dist/artifact/``
  index.html   page content WITHOUT <!doctype>/<html>/<head>/<body> (the publisher
               adds the skeleton). Keeps <title> and the Google Fonts <link>s,
               inlines web/styles.css and web/app.js, and embeds the manifest and
               the demo trace as JSON <script> blocks so the page works even
               where fetch() of sibling files is unavailable.
  demo/        demo-run.json (preferred) or sample-trace.json, copied as-is
  assets/      manifest.json + only the manifest-referenced files the page uses

Assets are added in priority order while the folder stays under the size
budget (default 15.5 MB, hard limit 16 MB). Anything dropped or missing is set
to null in the artifact's manifest so the page falls back gracefully.

The output folder is deleted and rebuilt on every run. Outside dist/ the script
only deletes an empty folder or one that holds nothing but an earlier build
(index.html with the embedded trace, demo/, assets/); anything else needs --force.

Usage:
  python3 scripts/build_artifact.py [--out dist/artifact] [--all-assets] [--budget-mb 15.5] [--force]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
HARD_LIMIT = 16 * 1000 * 1000  # the artifact limit is "16MB"; stay under the decimal value to be safe

AGENTS = ("orchestrator", "researcher", "reviewer")
CHANNELS = ("bizplan", "naver_blog", "linkedin", "instagram")

# (manifest path, used by the page?) in priority order
def asset_slots(manifest: dict) -> list[tuple[tuple[str, ...], bool]]:
    slots: list[tuple[tuple[str, ...], bool]] = []
    slots += [(("agents", a, "image"), True) for a in AGENTS]
    slots += [(("channels", c, "icon"), True) for c in CHANNELS]
    slots += [(("hero", "studio"), True)]
    slots += [(("agents", a, "video"), True) for a in AGENTS]
    slots += [(("agents", a, "model"), True) for a in AGENTS]
    # referenced by the manifest but not rendered by the dashboard
    slots += [(("hero", "image"), False)]
    slots += [(("agents", a, "cutout"), False) for a in AGENTS]
    slots += [(("hero", "video"), False)]
    return slots


def get_in(d: dict, path: tuple[str, ...]):
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def set_in(d: dict, path: tuple[str, ...], value) -> None:
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def safe_rel(p: str) -> PurePosixPath | None:
    """Accept only plain relative paths inside web/assets."""
    if not isinstance(p, str) or not p or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", p):
        return None
    rel = PurePosixPath(p[2:] if p.startswith("./") else p)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


def section(html: str, name: str) -> str:
    m = re.search(rf"<!--\s*BUILD:{name}-START\s*-->(.*?)<!--\s*BUILD:{name}-END\s*-->", html, re.S)
    if not m:
        raise SystemExit(f"web/index.html에 BUILD:{name} 표시가 없어요.")
    return m.group(1).strip()


def json_for_script(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    # never let embedded data close the <script> element or open an HTML comment.
    # \u003c / \u003e / \u0026 are valid JSON escapes, so JSON.parse still returns the original text.
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def load_trace() -> tuple[Path, dict]:
    for name in ("demo-run.json", "sample-trace.json"):
        path = WEB / "demo" / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  경고: {path.relative_to(ROOT)} JSON 오류 ({exc}) — 건너뜀")
            continue
        if isinstance(data, dict) and isinstance(data.get("events"), list) and data["events"]:
            return path, data
        print(f"  경고: {path.relative_to(ROOT)}에 이벤트가 없어요 — 건너뜀")
    raise SystemExit("web/demo/demo-run.json 또는 sample-trace.json이 필요해요.")


BUILD_ENTRIES = {"index.html", "demo", "assets"}


def previous_build(out: Path) -> bool:
    """True when ``out`` holds only what an earlier run of this script wrote."""
    index = out / "index.html"
    if not index.is_file() or not {p.name for p in out.iterdir()} <= BUILD_ENTRIES:
        return False
    return 'id="insia-trace"' in index.read_text(encoding="utf-8", errors="replace")


def human(n: int) -> str:
    return f"{n / 1_000_000:.2f} MB" if n >= 100_000 else f"{n / 1000:.1f} KB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="대시보드를 claude.ai 아티팩트 폴더로 빌드합니다.")
    ap.add_argument("--out", default=str(ROOT / "dist" / "artifact"), help="출력 폴더 (기본: dist/artifact)")
    ap.add_argument("--budget-mb", type=float, default=15.5, help="폴더 전체 용량 예산 MB (기본 15.5)")
    ap.add_argument("--all-assets", action="store_true", help="대시보드가 쓰지 않는 컷아웃·히어로 에셋도 포함")
    ap.add_argument("--force", action="store_true", help="dist/ 밖의 출력 폴더에 이전 빌드가 아닌 파일이 있어도 지우고 다시 만들기")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    # the folder is wiped before each build: only allow dist/** or a folder named artifact*
    in_dist = (ROOT / "dist") in out.parents
    if not in_dist and not out.name.startswith("artifact"):
        raise SystemExit(f"출력 폴더는 dist/ 아래이거나 이름이 artifact로 시작해야 해요: {out}")
    if out == ROOT or out in ROOT.parents:
        raise SystemExit(f"프로젝트 폴더나 그 상위 폴더는 출력 폴더로 쓸 수 없어요: {out}")
    if out.exists():
        if not out.is_dir():
            raise SystemExit(f"출력 경로가 폴더가 아니에요: {out}")
        # outside dist/, a folder like ~/artifacts may hold unrelated files: wipe only an earlier build
        if not in_dist and not args.force and any(out.iterdir()) and not previous_build(out):
            raise SystemExit(
                f"{out}에 이 스크립트가 만들지 않은 파일이 있어 지우지 않았어요. "
                "빈 폴더나 새 폴더를 고르거나, 폴더 내용을 지워도 된다면 --force를 붙여 주세요."
            )
    budget = int(min(args.budget_mb * 1_000_000, HARD_LIMIT))

    html = (WEB / "index.html").read_text(encoding="utf-8")
    head = section(html, "HEAD")
    body = section(html, "BODY")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for label, text, tag in (("styles.css", css, "</style"), ("app.js", js, "</script")):
        if tag in text.lower():
            raise SystemExit(f"{label} 안에 '{tag}'가 있어 인라인할 수 없어요.")

    manifest_path = WEB / "assets" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"version": 1, "agents": {}, "channels": {}, "hero": {}}
    trace_path, trace = load_trace()

    if out.exists():
        shutil.rmtree(out)
    (out / "demo").mkdir(parents=True)
    (out / "assets").mkdir(parents=True)

    # demo traces: copy the ones that exist (the page prefers demo-run.json; the chosen one is also embedded)
    demo_bytes = 0
    for name in ("demo-run.json", "sample-trace.json"):
        src = WEB / "demo" / name
        if src.is_file():
            shutil.copy2(src, out / "demo" / name)
            demo_bytes += src.stat().st_size

    def page_html(man: dict) -> str:
        parts = [
            head,
            "<style>\n" + css.strip() + "\n</style>",
            body,
            '<script type="application/json" id="insia-manifest">' + json_for_script(man) + "</script>",
            '<script type="application/json" id="insia-trace">' + json_for_script(trace) + "</script>",
            "<script>\n" + js.strip() + "\n</script>",
        ]
        return "\n".join(parts) + "\n"

    out_manifest = json.loads(json.dumps(manifest))
    used: list[tuple[str, int]] = []
    dropped: list[tuple[str, str]] = []
    copied: dict[str, int] = {}

    # size of everything except assets (the page grows slightly as manifest paths change; keep a margin)
    base = len(page_html(out_manifest).encode("utf-8")) + demo_bytes + len(json.dumps(out_manifest, ensure_ascii=False).encode("utf-8")) + 4096
    total = base
    for path, used_by_page in asset_slots(manifest):
        ref = get_in(manifest, path)
        key = ".".join(path)
        if ref in (None, ""):
            continue
        rel = safe_rel(ref)
        if rel is None:
            dropped.append((key, f"허용되지 않는 경로 {ref!r}"))
            set_in(out_manifest, path, None)
            continue
        if not used_by_page and not args.all_assets:
            dropped.append((key, "대시보드에서 쓰지 않음 (--all-assets로 포함)"))
            set_in(out_manifest, path, None)
            continue
        src = WEB / "assets" / Path(*rel.parts)
        if not src.is_file():
            dropped.append((key, f"파일 없음: web/assets/{rel}"))
            set_in(out_manifest, path, None)
            continue
        size = src.stat().st_size
        if str(rel) in copied:
            used.append((f"{key} → {rel} (공유)", 0))
            continue
        if total + size > budget:
            dropped.append((key, f"용량 예산 초과 ({human(size)})"))
            set_in(out_manifest, path, None)
            continue
        dest = out / "assets" / Path(*rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied[str(rel)] = size
        total += size
        used.append((f"{key} → {rel}", size))

    (out / "assets" / "manifest.json").write_text(json.dumps(out_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    page = page_html(out_manifest)
    for bad in (r"<!doctype", r"<html[\s>]", r"</html>", r"<head[\s>]", r"</head>", r"<body[\s>]", r"</body>"):
        if re.search(bad, page, re.I):
            raise SystemExit(f"아티팩트 index.html에 래퍼 태그가 남아 있어요: {bad}")
    if "<title>" not in page[:8192]:
        raise SystemExit("<title>이 파일 앞 8KB 안에 있어야 해요.")
    (out / "index.html").write_text(page, encoding="utf-8")

    files = sorted(p for p in out.rglob("*") if p.is_file())
    grand = sum(p.stat().st_size for p in files)
    print(f"아티팩트 빌드: {out.relative_to(ROOT) if ROOT in out.parents else out}")
    print(f"  데모 기록: web/demo/{trace_path.name} ({len(trace['events'])}개 이벤트, 페이지에 내장)")
    print(f"  index.html: {human((out / 'index.html').stat().st_size)}")
    for label, size in used:
        print(f"  + {label} ({human(size)})")
    for label, why in dropped:
        print(f"  - {label}: {why}")
    print(f"  파일 {len(files)}개, 합계 {human(grand)} (예산 {human(budget)}, 한도 {human(HARD_LIMIT)})")
    if grand > HARD_LIMIT:
        print("  오류: 16MB 한도를 넘었어요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
