"""CLI subcommands on a temporary workspace (INSIA_HOME=tmp) with the mock backend."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from insia_agents import cli
from insia_agents.backends.mock_backend import template_draft, template_research
from insia_agents.cli import describe_event, import_run_id, main, profile_from_data
from insia_agents.db import Workspace, pipeline_item_id
from insia_agents.models import Brief, PlannedSlot, Profile, TeamMember, UsageRecord

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample-run"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    monkeypatch.setenv("INSIA_HOME", str(home))
    monkeypatch.setenv("INSIA_TODAY", "2026-09-28")  # a Monday
    for name in ("INSIA_ACCESS_TOKEN", "INSIA_MAX_COST_USD", "INSIA_PORT", "INSIA_MAX_DOCUMENT_CHARS", "INSIA_DEBUG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # relative outputs/ and template files stay in tmp
    return home


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def open_ws(home: Path) -> Workspace:
    return Workspace(home)


FULL_PROFILE = Profile(
    company_name="인시아랩", service_name="INSIA: 스마트에이전트", one_liner="1인 창업자를 위한 AI 콘텐츠 비서",
    description="첫 줄\n둘째 줄에는 \"따옴표\"와 # 기호", stage="예비창업", target_customers="1인 창업자",
    differentiators=["출처 기반 작성", "사람 최종 승인: 자동 게시 없음"], traction=["베타 사용자 120명 (2026-08 기준)"],
    team=[TeamMember(role="대표", name="홍길동", background="마케팅 8년"), TeamMember(role="개발자", hiring=True)],
    banned_words=["최고", "무조건"], required_phrases=["#광고"], default_hashtags=["#1인창업"], brand_colors=["#0F766E"],
    contact="hello@example.com", tone="친근한 전문가 톤",
)


def _same_profile(a: Profile, b: Profile) -> bool:
    return a.model_dump(exclude={"updated_at"}) == b.model_dump(exclude={"updated_at"})


# ---------------------------------------------------------------------------
# Parser, help, errors
# ---------------------------------------------------------------------------


def test_help_is_korean_and_usage_errors_exit_2(capsys):
    code, out, _ = run(capsys, "--help")
    assert code == 0 and "사용법" in out and "처음이라면" in out and "run-due" in out
    code, out, err = run(capsys, "items", "approve")
    assert code == 2 and "꼭 필요한 값이 빠졌어요" in err
    code, _, err = run(capsys, "items", "list", "--status", "bogus")
    assert code == 2 and "고를 수 없어요" in err
    code, _, err = run(capsys, "plan-week", "--start", "2026/10/05")
    assert code == 2 and "YYYY-MM-DD" in err and "argument" not in err
    code, _, err = run(capsys, "items", "list", "--channel", "tiktok")
    assert code == 2 and "알 수 없는 채널" in err
    assert run(capsys)[0] == 2


def test_describe_event_labels_jobs_and_resume():
    started = {"type": "run.started", "data": {"kind": "review", "mode": "mock", "model": "m", "channels": ["linkedin"]}}
    assert describe_event(started).startswith("재검수 시작 · mock 모드")
    resumed = {"type": "run.started", "data": {"resumed": True, "mode": "live", "model": "m", "channels": [], "budget_usd": 3}}
    assert describe_event(resumed).startswith("이어서 실행 시작") and "$3.00" in describe_event(resumed)
    done = {"type": "run.completed", "data": {"kind": "revise", "duration_s": 3.2, "version": 3, "cost_usd": 0.1234}}
    assert describe_event(done) == "수정 요청 완료 · 3.2초 · v3 · $0.12".replace("$0.12", "비용 $0.12")
    human = {"type": "draft.created", "data": {"channel": "linkedin", "round": 0, "chars": 10, "chars_no_space": 8,
                                               "source": "human", "version": 4}}
    assert "직접 수정 v4" in describe_event(human)


def test_home_option_overrides_env_temporarily(tmp_path, capsys, monkeypatch):
    other = tmp_path / "other"
    code, out, _ = run(capsys, "--home", other, "docs", "list")
    assert code == 0 and (other / "insia.db").is_file()
    code, _, _ = run(capsys, "docs", "list", "--home", tmp_path / "third")
    assert code == 0 and (tmp_path / "third" / "insia.db").is_file()
    assert Path(cli.os.environ["INSIA_HOME"]) == tmp_path / "ws"  # restored


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


def test_profile_show_empty_and_json(capsys):
    code, out, _ = run(capsys, "profile", "show")
    assert code == 0 and "아직 회사 프로필이 없어요" in out
    code, out, _ = run(capsys, "profile", "show", "--json")
    assert code == 0 and json.loads(out) == Profile().model_dump()


def test_profile_export_import_roundtrip_json(env, tmp_path, capsys):
    with open_ws(env) as ws:
        ws.save_profile(FULL_PROFILE)
    code, out, _ = run(capsys, "profile", "show", "--json")
    shown = Profile.model_validate(json.loads(out))  # not wrapped
    assert _same_profile(shown, FULL_PROFILE)
    code, out, _ = run(capsys, "profile", "show")
    assert code == 0 and "인시아랩" in out and "채운 항목" in out and "개발자 · 채용 예정" in out
    assert run(capsys, "profile", "export", "--out", "p.json")[0] == 0
    assert run(capsys, "profile", "export", "--out", "p.json")[0] == 2  # no silent overwrite
    code, _, _ = run(capsys, "--home", tmp_path / "ws2", "profile", "import", "p.json")
    assert code == 0
    with open_ws(tmp_path / "ws2") as ws2:
        assert _same_profile(ws2.get_profile(), FULL_PROFILE)
    code, out, _ = run(capsys, "profile", "export")
    assert _same_profile(Profile.model_validate(json.loads(out)), FULL_PROFILE)


def test_profile_yaml_template_roundtrip(env, tmp_path, capsys):
    pytest.importorskip("yaml")
    with open_ws(env) as ws:
        ws.save_profile(FULL_PROFILE)
    code, out, _ = run(capsys, "profile", "edit-template", "--out", "t.yaml")
    assert code == 0 and "profile import" in out
    text = (tmp_path / "t.yaml").read_text(encoding="utf-8")
    assert text.startswith("# INSIA 회사·브랜드 프로필") and "# 금지 표현" in text
    assert run(capsys, "profile", "edit-template", "--out", "t.yaml")[0] == 2
    code, out, _ = run(capsys, "--home", tmp_path / "ws2", "profile", "import", "t.yaml")
    assert code == 0 and "프로필을 저장했어요" in out
    with open_ws(tmp_path / "ws2") as ws2:
        assert _same_profile(ws2.get_profile(), FULL_PROFILE)
    # a blank YAML template imports as an empty profile (commented list/team examples are ignored)
    assert run(capsys, "profile", "edit-template", "--blank", "--out", "blank.yaml")[0] == 0
    assert run(capsys, "--home", tmp_path / "ws3", "profile", "import", "blank.yaml")[0] == 0
    with open_ws(tmp_path / "ws3") as ws3:
        assert _same_profile(ws3.get_profile(), Profile())


def test_profile_json_template_coercion_and_merge(env, tmp_path, capsys):
    assert run(capsys, "profile", "edit-template", "--format", "json", "--blank", "--out", "t.json")[0] == 0
    data = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert data["_company_name"].startswith("회사 이름") and data["team"][0]["role"] == ""
    data.update({"company_name": "인시아랩", "stage": 2026, "differentiators": "출처 기반\n- 사람 승인",
                 "default_hashtags": "AI 마케팅, #창업", "brand_colors": ["0f766e"], "foo": "무시",
                 "team": [{"role": "대표", "name": "홍길동", "hiring": "아니오"}, {"role": "개발자", "hiring": "예"}]})
    (tmp_path / "t.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    code, out, err = run(capsys, "profile", "import", "t.json")
    assert code == 0 and "foo" in err and "알 수 없는 항목" in err
    with open_ws(env) as ws:
        saved = ws.get_profile()
    assert saved.stage == "2026" and saved.differentiators == ["출처 기반", "사람 승인"]
    assert saved.default_hashtags == ["#AI마케팅", "#창업"] and saved.brand_colors == ["#0F766E"]
    assert [(m.role, m.hiring) for m in saved.team] == [("대표", False), ("개발자", True)]
    (tmp_path / "tone.json").write_text(json.dumps({"profile": {"tone": "친근한 톤"}}, ensure_ascii=False), encoding="utf-8")
    code, out, _ = run(capsys, "profile", "import", "tone.json", "--merge")
    assert code == 0 and "합치기" in out
    with open_ws(env) as ws:
        merged = ws.get_profile()
    assert merged.tone == "친근한 톤" and merged.company_name == "인시아랩"


def test_profile_import_errors_are_korean(tmp_path, capsys, monkeypatch):
    (tmp_path / "bad.json").write_text('{"company_name": "a",}', encoding="utf-8")
    code, _, err = run(capsys, "profile", "import", "bad.json")
    assert code == 2 and "JSON 형식이 올바르지 않아요" in err and "번째 줄" in err
    (tmp_path / "color.json").write_text('{"brand_colors": ["빨강"]}', encoding="utf-8")
    code, _, err = run(capsys, "profile", "import", "color.json")
    assert code == 2 and "#RRGGBB" in err
    (tmp_path / "team.json").write_text('{"team": [{"name": "홍길동"}]}', encoding="utf-8")
    code, _, err = run(capsys, "profile", "import", "team.json")
    assert code == 2 and "역할" in err
    assert run(capsys, "profile", "import", "missing.json")[0] == 2
    (tmp_path / "p.yaml").write_text('company_name: "a"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_yaml_module", lambda: None)
    code, _, err = run(capsys, "profile", "import", "p.yaml")
    assert code == 2 and "PyYAML" in err and "pip install" in err


def test_profile_from_data_accepts_wrapped_and_none():
    profile, ignored = profile_from_data({"profile": {"company_name": "A", "updated_at": "x"}})
    assert profile.company_name == "A" and ignored == []
    assert profile_from_data(None)[0] == Profile()


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------


def test_docs_add_list_show_rm_text_and_markdown(env, tmp_path, capsys):
    (tmp_path / "intro.txt").write_bytes("회사 소개\r\n\r\n\r\n\r\n인시아랩은 1인 창업자를 돕습니다.  \r\n".encode("cp949"))
    (tmp_path / "news.md").write_text("# 보도자료\n\n베타 출시", encoding="utf-8")
    code, out, _ = run(capsys, "docs", "add", "intro.txt", "--title", "회사 소개서")
    assert code == 0 and "u1" in out and "회사 소개서" in out
    code, out, _ = run(capsys, "docs", "add", "news.md", "--json")
    added = json.loads(out)
    assert code == 0 and added["added"] and added["document"]["id"] == "u2" and added["document"]["kind"] == "markdown"
    code, out, _ = run(capsys, "docs", "add", "intro.txt")
    assert code == 0 and "이미 있어요" in out  # duplicate skipped
    code, out, _ = run(capsys, "docs", "list", "--json")
    docs = json.loads(out)
    assert [d["id"] for d in docs] == ["u1", "u2"]
    assert docs[0]["text"] == "회사 소개\n\n인시아랩은 1인 창업자를 돕습니다." and docs[0]["filename"] == "intro.txt"
    code, out, _ = run(capsys, "docs", "list")
    assert "회사 소개서" in out and "자료 2개" in out
    code, out, _ = run(capsys, "docs", "show", "u2")
    assert code == 0 and "베타 출시" in out
    assert (env / "uploads" / "u1_intro.txt").is_file()
    code, out, _ = run(capsys, "docs", "rm", "u1")
    assert code == 0 and "지웠어요" in out and not (env / "uploads" / "u1_intro.txt").exists()
    code, _, err = run(capsys, "docs", "rm", "u1")
    assert code == 1 and "찾을 수 없어요" in err
    code, out, _ = run(capsys, "docs", "add", "intro.txt", "--force")
    assert code == 0 and "u3" in out  # ids are never reused


def test_docs_add_refuses_empty_and_unsupported(tmp_path, capsys):
    (tmp_path / "empty.md").write_text("  \n\n", encoding="utf-8")
    code, _, err = run(capsys, "docs", "add", "empty.md")
    assert code == 2 and "글자가 없어요" in err
    (tmp_path / "plan.hwp").write_bytes(b"x")
    code, _, err = run(capsys, "docs", "add", "plan.hwp")
    assert code == 2 and "PDF나 DOCX" in err
    (tmp_path / "shot.png").write_bytes(b"x")
    assert run(capsys, "docs", "add", "shot.png")[0] == 2
    code, _, err = run(capsys, "docs", "add", "nothing.txt")
    assert code == 2 and "찾을 수 없어요" in err


def _text_pdf(path: Path, text: str = "Hello INSIA PDF") -> None:
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))


def test_docs_add_pdf(env, tmp_path, capsys, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    _text_pdf(tmp_path / "deck.pdf")
    code, out, _ = run(capsys, "docs", "add", "deck.pdf")
    assert code == 0 and "(PDF)" in out
    with open_ws(env) as ws:
        doc = ws.get_document("u1")
    assert doc.kind == "pdf" and "Hello INSIA PDF" in doc.text
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(tmp_path / "scan.pdf", "wb") as handle:
        writer.write(handle)
    code, _, err = run(capsys, "docs", "add", "scan.pdf")
    assert code == 2 and "PDF에서 글자를 찾지 못했어요" in err
    monkeypatch.setitem(sys.modules, "pypdf", None)  # not installed
    code, _, err = run(capsys, "docs", "add", "deck.pdf", "--force")
    assert code == 1 and "pypdf" in err and "pip install" in err


def test_docs_add_docx(env, tmp_path, capsys, monkeypatch):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("회사 소개", level=1)
    document.add_paragraph("인시아랩은 콘텐츠 비서를 만듭니다.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "항목", "내용"
    table.cell(1, 0).text, table.cell(1, 1).text = "고객", "1인 창업자"
    document.save(str(tmp_path / "intro.docx"))
    code, out, _ = run(capsys, "docs", "add", "intro.docx")
    assert code == 0 and "(Word)" in out
    with open_ws(env) as ws:
        text = ws.get_document("u1").text
    assert "## 회사 소개" in text and "항목 | 내용" in text and "고객 | 1인 창업자" in text
    monkeypatch.setitem(sys.modules, "docx", None)
    code, _, err = run(capsys, "docs", "add", "intro.docx", "--force")
    assert code == 1 and "python-docx" in err


# ---------------------------------------------------------------------------
# run / items / review / revise
# ---------------------------------------------------------------------------


def _mock_run(capsys, *extra):
    code, out, err = run(capsys, "run", "--topic", "동네 빵집 온라인 주문", "--mode", "mock", "--speed", "0", "--no-save",
                         "--quiet", *extra)
    assert code == 0, err
    return out


def test_run_writes_into_workspace_with_profile_docs_and_budget(env, tmp_path, capsys):
    with open_ws(env) as ws:
        ws.save_profile(FULL_PROFILE)
        ws.add_document("회사 소개서", "인시아랩은 1인 창업자를 돕습니다.")
        ws.add_document("보도자료", "베타 출시")
    out = _mock_run(capsys, "--channels", "linkedin", "--docs", "u2", "--max-cost-usd", "2")
    assert "보관함 it_" in out and "비용: $0" in out and "insia items list" in out
    with open_ws(env) as ws:
        runs = ws.list_runs()
        detail = ws.get_run(runs[0]["run_id"])
        items = ws.list_items()
    assert len(items) == 1 and items[0].channel == "linkedin"
    assert detail["options"]["doc_ids"] == ["u2"] and detail["options"]["use_profile"] is True
    assert detail["options"]["max_cost_usd"] == 2 and detail["profile"]["company_name"] == "인시아랩"
    _mock_run(capsys, "--channels", "instagram", "--no-profile")  # default --docs all
    with open_ws(env) as ws:
        detail = ws.get_run(ws.list_runs()[0]["run_id"])
    assert detail["options"]["use_profile"] is False and detail["options"]["doc_ids"] == ["u1", "u2"]
    code, _, err = run(capsys, "run", "--topic", "t", "--channels", "linkedin", "--mode", "mock", "--speed", "0", "--docs", "u9")
    assert code == 2 and "u9" in err
    _mock_run(capsys, "--channels", "linkedin", "--no-workspace")
    with open_ws(env) as ws:
        assert len(ws.list_items()) == 2  # --no-workspace stored nothing


def test_items_flow_list_show_approve_schedule_publish_archive_export(env, tmp_path, capsys):
    _mock_run(capsys, "--channels", "linkedin", "--max-rounds", "0")  # R0 template misses the format → needs_changes
    with open_ws(env) as ws:
        item = ws.list_items()[0]
    assert item.status == "needs_changes" and item.passed is False
    short = item.id.split("_", 1)[1]  # a unique part of the id is enough
    code, out, _ = run(capsys, "items", "list")
    assert code == 0 and item.id in out and "수정 필요" in out
    code, out, _ = run(capsys, "items", "list", "--json", "--channel", "li")
    assert [i["id"] for i in json.loads(out)] == [item.id]
    code, out, _ = run(capsys, "items", "show", short)
    assert code == 0 and "버전" in out and "본문 v1" in out and "형식 미충족" in out
    code, out, _ = run(capsys, "items", "show", short, "--json")
    assert json.loads(out)["versions"][0]["review"]["passed"] is False
    code, _, err = run(capsys, "items", "approve", short)
    assert code == 1 and "--force" in err and "통과하지 못했어요" in err
    code, _, err = run(capsys, "items", "publish", short, "--url", "https://blog.example.com/1")
    assert code == 1 and "먼저 승인해 주세요" in err
    code, out, _ = run(capsys, "items", "approve", short, "--force")
    assert code == 0 and "승인했어요" in out
    code, out, _ = run(capsys, "items", "schedule", short, "--date", "tomorrow")
    assert code == 0 and "2026-09-29(화)" in out
    code, _, err = run(capsys, "items", "publish", short, "--url", "ftp://x")
    assert code == 1 and "http" in err
    code, out, _ = run(capsys, "items", "publish", short, "--url", "https://www.linkedin.com/posts/1")
    assert code == 0 and "게시 완료" in out
    with open_ws(env) as ws:
        published = ws.get_item(item.id).item
    assert published.status == "published" and published.published_url.endswith("/1") and published.published_at
    code, out, _ = run(capsys, "items", "export", short, "--out", "exports")
    assert code == 0 and "붙여넣기용 텍스트" in out
    code, out, _ = run(capsys, "items", "export", short, "--format", "md", "--out", "exports")
    assert code == 0
    files = sorted(p.suffix for p in (tmp_path / "exports").iterdir())
    assert files == [".md", ".txt"]
    code, _, err = run(capsys, "items", "export", short, "--format", "html")
    assert code == 1 and "html" in err
    code, out, _ = run(capsys, "items", "archive", short, "--note", "지난 캠페인")
    assert code == 0 and "보관했어요" in out
    assert json.loads(run(capsys, "items", "list", "--json")[1]) == []
    assert len(json.loads(run(capsys, "items", "list", "--json", "--all")[1])) == 1
    code, out, _ = run(capsys, "items", "restore", short)
    assert code == 0 and "초안으로" in out
    code, _, err = run(capsys, "items", "show", "no-such-item")
    assert code == 1 and "찾을 수 없어요" in err


def test_ambiguous_item_id_is_a_usage_error(capsys):
    _mock_run(capsys, "--channels", "linkedin,instagram")
    code, _, err = run(capsys, "items", "show", "it_")
    assert code == 2 and "콘텐츠가 2개예요" in err


def test_review_and_revise_commands(env, capsys):
    _mock_run(capsys, "--channels", "linkedin")
    with open_ws(env) as ws:
        item = ws.list_items()[0]
        before = len(ws.get_item(item.id).versions)
    code, out, _ = run(capsys, "review", item.id, "--mode", "mock")
    assert code == 0 and "재검수 시작" in out and "재검수:" in out and "비용: $0" in out
    code, out, _ = run(capsys, "revise", item.id, "-i", "첫 문장을 더 짧게", "--mode", "mock")
    assert code == 0 and f"수정본 v{before + 1}" in out and "수정 요청 시작" in out
    with open_ws(env) as ws:
        versions = ws.get_item(item.id).versions
        kinds = [r["kind"] for r in ws.list_runs()]
    assert len(versions) == before + 1 and versions[-1].instructions == "첫 문장을 더 짧게"
    assert versions[-1].review is not None and {"review", "revise"} <= set(kinds)
    code, _, err = run(capsys, "review", "missing", "--mode", "mock")
    assert code == 1 and "찾을 수 없어요" in err


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


def test_plan_week_calendar_list_move_skip(env, capsys):
    code, out, _ = run(capsys, "plan-week", "--theme", "AI 콘텐츠 자동화", "--start", "2026-10-05", "--mode", "mock",
                       "--blog", "1", "--linkedin", "1")
    assert code == 0 and "슬롯 ID" in out and "2026-10-05(월) ~ 2026-10-11(일)" in out and "run-due" in out
    with open_ws(env) as ws:
        slots = ws.list_slots()
    assert sorted(s.channel for s in slots) == ["linkedin", "naver_blog"]
    code, out, _ = run(capsys, "plan-week", "--start", "2026-10-05", "--mode", "mock", "--instagram", "1", "--json")
    week = json.loads(out)
    assert code == 0 and [s["channel"] for s in week["slots"]] == ["instagram"]
    code, out, _ = run(capsys, "plan-week", "--start", "2026-10-05", "--mode", "mock", "--linkedin", "1", "--replace")
    assert code == 0 and "건너뜀으로 바꿨어요" in out
    code, out, _ = run(capsys, "calendar", "list", "--from", "2026-10-05", "--to", "2026-10-11", "--json")
    listed = json.loads(out)
    assert len(listed) == 1 and listed[0]["channel"] == "linkedin"
    assert len(json.loads(run(capsys, "calendar", "list", "--from", "2026-10-05", "--to", "2026-10-11", "--json", "--all")[1])) == 4
    slot_id = listed[0]["id"]
    code, out, _ = run(capsys, "calendar", "move", slot_id, "--date", "2026-10-09")
    assert code == 0 and "2026-10-09(금)" in out
    code, out, _ = run(capsys, "calendar", "skip", slot_id)
    assert code == 0 and "건너뛰기" in out
    with open_ws(env) as ws:
        assert ws.get_slot(slot_id).status == "skipped" and ws.get_slot(slot_id).date == "2026-10-09"
    code, out, _ = run(capsys, "calendar", "list")
    assert code == 0 and "계획된 게시물이 없어요" in out  # the default window starts this Monday (2026-09-28)
    code, _, err = run(capsys, "plan-week", "--start", "2026-10-05", "--days", "40", "--mode", "mock")
    assert code == 2 and "1~31" in err


def test_plan_week_defaults_to_next_monday_and_dashboard_counts():
    assert cli._next_monday("2026-09-28") == "2026-09-28"
    assert cli._next_monday("2026-09-29") == "2026-10-05"
    assert cli._next_monday("2026-10-04") == "2026-10-05"
    assert cli.DEFAULT_PLAN_COUNTS == {"naver_blog": 2, "linkedin": 2, "instagram": 2}


def test_run_due_with_nothing_due_exits_0_even_without_api_key(capsys):
    code, out, _ = run(capsys, "run-due")
    assert code == 0 and "만들 초안이 없어요" in out


def _slot(day: str, channel: str, topic: str) -> PlannedSlot:
    return PlannedSlot(date=day, channel=channel, topic=topic, angle="체크리스트", keywords=["AI 마케팅"], goal="문의")


def test_run_due_generates_drafts_for_due_slots(env, capsys):
    with open_ws(env) as ws:
        ws.add_slots([_slot("2026-09-28", "linkedin", "오늘 올릴 글"), _slot("2026-09-29", "instagram", "내일 올릴 글"),
                      _slot("2026-10-05", "naver_blog", "다음 주 글")])
    code, _, err = run(capsys, "run-due")
    assert code == 1 and "ANTHROPIC_API_KEY" in err  # auto mode without a key never makes demo drafts silently
    code, out, _ = run(capsys, "run-due", "--dry-run", "--until", "2026-09-29")
    assert code == 0 and "슬롯 2개" in out and "내일 올릴 글" in out
    code, out, _ = run(capsys, "run-due", "--mode", "mock", "--until", "tomorrow", "--limit", "1", "--quiet")
    assert code == 0 and "초안 1개를 만들었어요" in out and "남은 슬롯 1개" in out
    code, out, _ = run(capsys, "run-due", "--mode", "mock", "--until", "2026-09-29", "--quiet")
    assert code == 0 and "초안 1개를 만들었어요" in out
    with open_ws(env) as ws:
        slots = {s.topic: s for s in ws.list_slots()}
        items = {i.id: i for i in ws.list_items()}
    for topic, day in (("오늘 올릴 글", "2026-09-28"), ("내일 올릴 글", "2026-09-29")):
        slot = slots[topic]
        assert slot.status == "drafted" and slot.item_id in items and items[slot.item_id].scheduled_at == day
    assert slots["다음 주 글"].status == "planned"
    code, out, _ = run(capsys, "run-due", "--mode", "mock")
    assert code == 0 and "없어요" in out
    code, out, _ = run(capsys, "calendar", "generate", slots["다음 주 글"].id, "--mode", "mock", "--quiet")
    assert code == 0 and "보관함 it_" in out


# ---------------------------------------------------------------------------
# resume / usage / runs
# ---------------------------------------------------------------------------


def test_resume_command(env, capsys):
    run_id = "20260928-010203-abcd"
    with open_ws(env) as ws:
        ws.create_run(run_id, Brief(topic="이어서 할 실행", channels=["linkedin"]), mode="mock")
        assert ws.mark_interrupted() == 1
    code, out, err = run(capsys, "resume", "abcd", "--mode", "mock", "--no-save", "--quiet")
    assert code == 0, err
    assert "링크드인" in out and "누적 비용" in out
    with open_ws(env) as ws:
        assert ws.get_run(run_id)["status"] == "completed"
        assert ws.get_item(pipeline_item_id(run_id, "linkedin")) is not None
    code, _, err = run(capsys, "resume", run_id, "--mode", "mock")
    assert code == 1 and "이미 모든 채널을 마친" in err
    code, _, err = run(capsys, "resume", "nope")
    assert code == 1 and "찾을 수 없어요" in err


def test_usage_and_runs_commands(env, tmp_path, capsys):
    _mock_run(capsys, "--channels", "linkedin")
    with open_ws(env) as ws:
        run_id = ws.list_runs()[0]["run_id"]
        ws.record_usage(UsageRecord(run_id=run_id, task="draft", model="claude-opus-5", input_tokens=1000, cost_usd=0.5,
                                    created_at="2026-09-28T01:00:00Z"))
    code, out, _ = run(capsys, "usage")
    assert code == 0 and "사용량 · 2026-09-01" in out and "$0.50" in out and "예산 상한: 없음" in out
    code, out, _ = run(capsys, "usage", "--json", "--since", "2026-09-28", "--until", "2026-09-28")
    summary = json.loads(out)
    assert summary["total_usd"] == 0.5 and summary["runs"][0]["run_id"] == run_id
    assert json.loads(run(capsys, "usage", "--json", "--since", "2026-10-01")[1])["total_usd"] == 0
    code, out, _ = run(capsys, "runs", "list", "--json")
    assert code == 0 and json.loads(out)[0]["run_id"] == run_id
    code, out, _ = run(capsys, "runs", "list")
    assert run_id in out and "실행" in out
    code, out, _ = run(capsys, "runs", "show", run_id)
    assert code == 0 and "동네 빵집" in out and pipeline_item_id(run_id, "linkedin") in out
    assert json.loads(run(capsys, "runs", "show", run_id, "--json")[1])["plan"]
    code, out, _ = run(capsys, "runs", "export", run_id, "--out", "zips")
    assert code == 0
    archive = next((tmp_path / "zips").glob("*.zip"))
    with zipfile.ZipFile(archive) as zf:
        assert any(name.endswith("sources.md") for name in zf.namelist())


# ---------------------------------------------------------------------------
# import-run
# ---------------------------------------------------------------------------


def _versions(ws: Workspace, run_id: str) -> dict[str, int]:
    return {i.channel: len(ws.get_item(i.id).versions) for i in ws.list_items() if i.run_id == run_id}


def test_import_run_sample_folder(env, capsys):
    code, out, err = run(capsys, "import-run", SAMPLE)
    assert code == 0, err
    run_id = import_run_id(SAMPLE)
    assert run_id == "cc-sample-run" and "가져왔어요" in out and "R0 65점 → R1 81점" in out
    with open_ws(env) as ws:
        assert _versions(ws, run_id) == {"bizplan": 2, "naver_blog": 2, "linkedin": 1, "instagram": 1}
        for item in ws.list_items():
            detail = ws.get_item(item.id)
            assert all(v.review is not None and v.source == "agent" for v in detail.versions)
            assert item.passed is True and item.status == "draft" and detail.brief is not None
        stored = ws.get_run(run_id)
        events = ws.list_events(run_id)
    assert stored["kind"] == "import" and stored["status"] == "completed" and stored["plan"] and stored["research"]
    assert stored["options"]["source_folder"] == str(SAMPLE.resolve())
    assert events[0]["type"] == "run.started" and events[-1]["type"] == "run.completed"
    assert sum(e["type"] == "channel.completed" for e in events) == 4
    assert events[-1]["data"]["items"]["linkedin"] == "it_cc-sample-run_linkedin"
    # idempotent: nothing changes on a second import
    code, out, _ = run(capsys, "import-run", SAMPLE, "--json")
    report = json.loads(out)
    assert code == 0 and report["created"] is False
    assert all(v["versions_added"] == 0 and v["reviews_updated"] == 0 for v in report["items"].values())
    with open_ws(env) as ws:
        assert _versions(ws, run_id) == {"bizplan": 2, "naver_blog": 2, "linkedin": 1, "instagram": 1}
        assert len(ws.list_events(run_id)) == len(events)
        assert len(ws.list_runs()) == 1


def test_import_run_updates_changed_files_and_reports_problems(env, tmp_path, capsys):
    folder = tmp_path / "outputs" / "2026-09-28-테스트"
    shutil.copytree(SAMPLE, folder)
    (folder / "plan.json").unlink()
    plan = json.loads((SAMPLE / "plan.json").read_text(encoding="utf-8"))
    (folder / "plan.md").write_text("# 계획\n\n요약…\n\n```json\n" + json.dumps(plan, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    assert run(capsys, "import-run", folder)[0] == 0
    run_id = import_run_id(folder)
    assert run_id.startswith("cc-2026-09-28-") and run_id != "cc-2026-09-28-"
    with open_ws(env) as ws:
        assert ws.get_run(run_id)["plan"]["summary"] == plan["summary"]
    # change one draft, add an unreviewed follow-up round and a broken file
    linkedin = json.loads((folder / "drafts" / "linkedin.r0.json").read_text(encoding="utf-8"))
    linkedin["title"] = "제목을 바꿨어요"
    (folder / "drafts" / "linkedin.r0.json").write_text(json.dumps(linkedin, ensure_ascii=False), encoding="utf-8")
    instagram = json.loads((folder / "drafts" / "instagram.r0.json").read_text(encoding="utf-8"))
    instagram["round"] = 1
    (folder / "drafts" / "instagram.r1.json").write_text(json.dumps(instagram, ensure_ascii=False), encoding="utf-8")
    (folder / "drafts" / "naver_blog.r2.json").write_text("{not json", encoding="utf-8")
    code, out, err = run(capsys, "import-run", folder)
    assert code == 1 and "문제:" in err and "naver_blog.r2.json" in err
    assert "검수 전" in out and "다시 가져왔어요" in out
    with open_ws(env) as ws:
        counts = _versions(ws, run_id)
        linkedin_item = ws.get_item(pipeline_item_id(run_id, "linkedin"))
        instagram_item = ws.get_item(pipeline_item_id(run_id, "instagram"))
    assert counts == {"bizplan": 2, "naver_blog": 2, "linkedin": 2, "instagram": 2}
    assert linkedin_item.item.title == "제목을 바꿨어요" and linkedin_item.versions[-1].review is not None
    assert instagram_item.versions[-1].review is None and instagram_item.item.status == "draft"


def test_import_run_best_earlier_round_and_human_edits_survive_reimport(env, tmp_path, capsys):
    from insia_agents.actions import edit_item

    folder = tmp_path / "run-best-r0"
    shutil.copytree(SAMPLE, folder)
    review = json.loads((folder / "reviews" / "naver_blog.r1.json").read_text(encoding="utf-8"))
    for item in review["rubric"]:
        item["score"] = 0  # R1 now scores far below R0 → R0 is the final version
    (folder / "reviews" / "naver_blog.r1.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    code, out, _ = run(capsys, "import-run", folder)
    assert code == 0 and "현재 코드 기준으로 점수를 다시 계산: R1 83→10점" in out  # reviews are re-finalized by code
    run_id = import_run_id(folder)
    blog_id = pipeline_item_id(run_id, "naver_blog")
    with open_ws(env) as ws:
        detail = ws.get_item(blog_id)
        assert [v.draft.round for v in detail.versions] == [0, 1, 0]  # the best round is the newest version
        assert detail.item.score == detail.versions[0].review.score
        ws.set_item_status(pipeline_item_id(run_id, "linkedin"), "approved")
        edit_item(ws, blog_id, "사람이 고친 제목", detail.versions[-1].draft.content, ["#태그"])
    assert run(capsys, "import-run", folder)[0] == 0
    with open_ws(env) as ws:
        detail = ws.get_item(blog_id)
        assert len(detail.versions) == 4 and detail.versions[-1].source == "human"
        assert detail.item.title == "사람이 고친 제목"
        assert ws.get_item(pipeline_item_id(run_id, "linkedin")).item.status == "approved"


def test_import_run_input_errors(env, tmp_path, capsys):
    code, _, err = run(capsys, "import-run", tmp_path / "nowhere")
    assert code == 2 and "찾을 수 없어요" in err
    empty = tmp_path / "empty-run"
    empty.mkdir()
    code, _, err = run(capsys, "import-run", empty)
    assert code == 2 and "brief.json" in err
    (empty / "brief.json").write_text(Brief(topic="t").model_dump_json(), encoding="utf-8")
    code, _, err = run(capsys, "import-run", empty)
    assert code == 2 and "초안이 없어요" in err
    with open_ws(env) as ws:
        ws.create_run("taken", Brief(topic="t"))
    code, _, err = run(capsys, "import-run", SAMPLE, "--run-id", "taken")
    assert code == 2 and "--run-id" in err
    code, _, err = run(capsys, "import-run", SAMPLE, "--run-id", "../bad")
    assert code == 2


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_with_profile_options(env, tmp_path, capsys):
    brief = Brief(topic="테스트 주제", keywords=["AI 에이전트"])
    research = template_research(brief, "2026-09-28")
    run_dir = tmp_path / "run"
    (run_dir / "drafts").mkdir(parents=True)
    (run_dir / "brief.json").write_text(brief.model_dump_json(), encoding="utf-8")
    draft = run_dir / "drafts" / "linkedin.r1.json"
    draft.write_text(template_draft(brief, research, "linkedin", 1).model_dump_json(), encoding="utf-8")
    assert run(capsys, "check", draft)[0] == 0
    banned = Profile(banned_words=["홍보"])
    (tmp_path / "p.json").write_text(json.dumps({"profile": banned.model_dump()}, ensure_ascii=False), encoding="utf-8")
    code, out, _ = run(capsys, "check", draft, "--profile", tmp_path / "p.json", "--json")
    failed = {c["id"] for c in json.loads(out) if not c["passed"]}
    assert code == 1 and failed == {"banned_words"}
    (run_dir / "profile.json").write_text(banned.model_dump_json(), encoding="utf-8")
    code, out, _ = run(capsys, "check", draft)
    assert code == 1 and "프로필:" in out and "금지 표현" in out
    assert run(capsys, "check", draft, "--no-profile")[0] == 0
    (run_dir / "profile.json").unlink()
    with open_ws(env) as ws:
        ws.save_profile(banned)
    code, out, _ = run(capsys, "check", draft, "--workspace-profile")
    assert code == 1 and "워크스페이스" in out
    code, _, err = run(capsys, "check", draft, "--profile", "p.json", "--no-profile")
    assert code == 2 and "함께 쓸 수 없어요" in err


# ---------------------------------------------------------------------------
# serve / healthcheck / doctor
# ---------------------------------------------------------------------------


class FakeServer:
    url = "http://0.0.0.0:8953"
    web_root = None
    token_required = True
    public_hosts = ("insia.example.com",)

    def serve_forever(self, poll_interval=0.5):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def test_serve_requires_a_token_off_loopback_or_behind_a_domain(capsys, monkeypatch):
    code, _, err = run(capsys, "serve", "--host", "0.0.0.0", "--port", "8952")
    assert code == 2 and "접근 토큰" in err and "INSIA_ACCESS_TOKEN" in err
    code, _, err = run(capsys, "serve", "--public-host", "insia.example.com", "--port", "8952")
    assert code == 2 and "도메인" in err and "접근 토큰" in err  # loopback bind behind a reverse proxy
    monkeypatch.setenv("INSIA_PUBLIC_HOSTS", "insia.example.com")
    assert run(capsys, "serve", "--port", "8952")[0] == 2


def test_serve_passes_token_hosts_and_proxy_to_make_server(capsys, monkeypatch):
    from insia_agents import server as server_module

    calls = []

    def fake_make_server(settings, host="127.0.0.1", port=8765, web_dir=None, *, token=None, public_hosts=(),
                         trust_proxy=False, quiet=False):
        calls.append({"host": host, "port": port, "token": token, "public_hosts": public_hosts,
                      "trust_proxy": trust_proxy, "quiet": quiet, "home": str(settings.home)})
        return FakeServer()

    monkeypatch.setattr(server_module, "make_server", fake_make_server)
    code, out, _ = run(capsys, "serve", "--host", "0.0.0.0", "--port", "8953", "--token", "abcdefghijklmnop",
                       "--public-host", "Insia.example.com", "--public-host", "x.example.com", "--trust-proxy")
    assert code == 0 and "서버를 종료해요" in out and "접근 토큰: 켜짐" in out and "abcdefghijklmnop" not in out
    assert calls[-1] == {"host": "0.0.0.0", "port": 8953, "token": "abcdefghijklmnop",
                         "public_hosts": ("insia.example.com", "x.example.com"), "trust_proxy": True, "quiet": True,
                         "home": calls[-1]["home"]}
    monkeypatch.setenv("INSIA_ACCESS_TOKEN", "from-the-environment")
    assert run(capsys, "serve", "--host", "0.0.0.0", "--port", "8953", "--verbose")[0] == 0
    assert calls[-1]["token"] == "from-the-environment" and calls[-1]["quiet"] is False

    def failing(*args, **kwargs):
        raise ValueError("접근 토큰이 너무 짧아요")

    monkeypatch.setattr(server_module, "make_server", failing)
    code, _, err = run(capsys, "serve", "--port", "8953")
    assert code == 1 and "서버를 시작하지 못했어요" in err and "너무 짧아요" in err

    def busy(*args, **kwargs):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(server_module, "make_server", busy)
    code, _, err = run(capsys, "serve", "--port", "8953")
    assert code == 1 and "다른 포트" in err

    def old_make_server(settings, host="127.0.0.1", port=8765, web_dir=None, heartbeat=15.0, quiet=True):
        return FakeServer()

    monkeypatch.setattr(server_module, "make_server", old_make_server)
    code, _, err = run(capsys, "serve", "--port", "8953", "--token", "abcdefghijklmnop")
    assert code == 2 and "지원하지 않아요" in err


class _HealthHandler(BaseHTTPRequestHandler):
    status = 200
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_healthcheck(capsys, monkeypatch):
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 8954), _HealthHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("INSIA_ACCESS_TOKEN", "secret-token-123")
        code, out, _ = run(capsys, "healthcheck", "--port", "8954")
        assert code == 0 and "정상" in out
        assert _HealthHandler.seen[-1] == ("/api/health", "Bearer secret-token-123")
        _HealthHandler.status = 401  # alive, the token just did not match
        assert run(capsys, "healthcheck", "--port", "8954", "--quiet") == (0, "", "")
        _HealthHandler.status = 500
        code, _, err = run(capsys, "healthcheck", "--port", "8954")
        assert code == 1 and "HTTP 500" in err
    finally:
        httpd.shutdown()
        httpd.server_close()
        _HealthHandler.status = 200
    code, _, err = run(capsys, "healthcheck", "--port", "8955", "--timeout", "2")
    assert code == 1 and "연결하지 못했어요" in err


def test_doctor(capsys):
    code, out, _ = run(capsys, "doctor")
    assert code == 0 and "INSIA 점검" in out and "API 키" in out and "회사 프로필" in out
    code, out, _ = run(capsys, "doctor", "--json")
    report = json.loads(out)
    assert code == 0 and report["ok"] is True and any(c["label"] == "워크스페이스" for c in report["checks"])


def test_run_budget_stop_prints_resume_hint(capsys, monkeypatch):
    from insia_agents import pipeline

    def over_budget(brief, settings, *, run_id=None, **kwargs):
        raise pipeline.BudgetExceeded(pipeline.budget_message(1.0, 1.2, []), cap=1.0, spent=1.2)

    monkeypatch.setattr(pipeline, "execute_run", over_budget)
    code, _, err = run(capsys, "run", "--topic", "t", "--channels", "linkedin", "--mode", "mock", "--max-cost-usd", "1")
    assert code == 1 and "예산 상한 $1.00를 넘어" in err and "insia resume" in err and "--max-cost-usd 2" in err


def test_revise_instructions_file_and_show_version(env, tmp_path, capsys):
    _mock_run(capsys, "--channels", "linkedin")
    with open_ws(env) as ws:
        item_id = ws.list_items()[0].id
    (tmp_path / "지시.txt").write_text("사례를 하나 더 넣어 주세요\n", encoding="utf-8")
    code, _, err = run(capsys, "revise", item_id, "--instructions-file", "지시.txt", "--mode", "mock", "--quiet")
    assert code == 0, err
    with open_ws(env) as ws:
        versions = ws.get_item(item_id).versions
    assert versions[-1].instructions == "사례를 하나 더 넣어 주세요"
    code, out, _ = run(capsys, "items", "show", item_id, "--version", "1")
    assert code == 0 and "본문 v1" in out
    code, _, err = run(capsys, "items", "show", item_id, "--version", "99")
    assert code == 1 and "v99" in err


def test_run_due_keeps_going_after_a_failed_slot(env, capsys, monkeypatch):
    from insia_agents import actions
    from insia_agents.backends.base import BackendError

    with open_ws(env) as ws:
        first, second = ws.add_slots([_slot("2026-09-28", "linkedin", "실패할 글"), _slot("2026-09-28", "instagram", "성공할 글")])
    real = actions.generate_slot

    def flaky(ws, slot_id, **kwargs):
        if slot_id == first.id:
            raise BackendError("API 인증에 실패했어요 (401)")
        return real(ws, slot_id, **kwargs)

    monkeypatch.setattr(actions, "generate_slot", flaky)
    code, out, err = run(capsys, "run-due", "--mode", "mock", "--quiet")
    assert code == 1 and "초안 1개를 만들었어요 · 실패 1개" in out and "401" in err
    with open_ws(env) as ws:
        assert ws.get_slot(first.id).status == "planned" and ws.get_slot(second.id).status == "drafted"


def test_profile_template_goes_to_the_workspace_when_cwd_is_read_only(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)  # like /app inside the Docker image
    code, out, _ = run(capsys, "profile", "edit-template", "--format", "json")
    assert code == 0 and (env / "profile.json").is_file() and not (tmp_path / "profile.json").exists()
