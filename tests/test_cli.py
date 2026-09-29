from __future__ import annotations

import json

import pytest

from insia_agents.backends.mock_backend import template_draft, template_research
from insia_agents.cli import main
from insia_agents.models import Brief


@pytest.fixture(autouse=True)
def tmp_workspace(tmp_path, monkeypatch):
    """`insia run` writes into the workspace by default: never the repo's workspace/."""
    monkeypatch.setenv("INSIA_HOME", str(tmp_path / "ws"))
    monkeypatch.delenv("INSIA_MAX_COST_USD", raising=False)
    return tmp_path / "ws"


def test_run_mock_writes_outputs_and_trace(tmp_path, capsys):
    trace = tmp_path / "demo" / "demo-run.json"
    code = main(["run", "--mode", "mock", "--topic", "테스트 주제", "--channels", "linkedin,instagram", "--speed", "0",
                 "--out", str(tmp_path / "out"), "--record", str(trace), "--keywords", "AI 에이전트,1인 창업"])
    output = capsys.readouterr().out
    assert code == 0
    assert "실행 시작" in output and "링크드인 완료" in output and "채널별 결과" in output
    data = json.loads(trace.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["meta"]["mode"] == "mock" and data["meta"]["brief"]["topic"] == "테스트 주제"
    assert data["events"][0]["type"] == "run.started" and data["events"][-1]["type"] == "run.completed"
    run_dirs = list((tmp_path / "out").iterdir())
    assert len(run_dirs) == 1 and (run_dirs[0] / "linkedin.md").is_file()


def test_run_from_brief_file_no_save(tmp_path, capsys):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(Brief(topic="파일 브리프", channels=["instagram"]).model_dump_json(), encoding="utf-8")
    code = main(["run", "--brief", str(brief_path), "--mode", "mock", "--speed", "0", "--no-save", "--quiet",
                 "--out", str(tmp_path / "out")])
    output = capsys.readouterr().out
    assert code == 0 and "인스타그램 완료" in output and "검색" not in output
    assert not (tmp_path / "out").exists()


def test_run_accepts_brief_with_utf8_bom(tmp_path, capsys):
    brief_path = tmp_path / "brief.json"
    brief_path.write_bytes(b"\xef\xbb\xbf" + Brief(topic="BOM 브리프", channels=["linkedin"]).model_dump_json().encode("utf-8"))
    code = main(["run", "--brief", str(brief_path), "--mode", "mock", "--speed", "0", "--no-save", "--quiet"])
    assert code == 0, capsys.readouterr().err
    assert "링크드인 완료" in capsys.readouterr().out


def test_run_record_to_directory_is_a_usage_error(tmp_path, capsys):
    folder = tmp_path / "traces"
    folder.mkdir()
    code = main(["run", "--mode", "mock", "--topic", "t", "--channels", "linkedin", "--speed", "0", "--no-save",
                 "--record", str(folder)])
    captured = capsys.readouterr()
    assert code == 2 and "--record" in captured.err and "Traceback" not in captured.err
    assert "실행 시작" not in captured.out  # rejected before the run starts


def test_run_record_write_failure_reports_error(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    code = main(["run", "--mode", "mock", "--topic", "t", "--channels", "linkedin", "--speed", "0", "--no-save",
                 "--record", str(blocker / "trace.json")])
    captured = capsys.readouterr()
    assert code == 1
    assert "트레이스를 저장하지 못했어요" in captured.err
    assert "채널별 결과" in captured.out  # the finished run's summary is still shown


def test_run_usage_errors(capsys):
    assert main(["run", "--mode", "mock"]) == 2
    assert main(["run", "--mode", "mock", "--topic", "t", "--channels", "tiktok"]) == 2
    assert main(["run", "--brief", "/nonexistent/brief.json"]) == 2
    err = capsys.readouterr().err
    assert "tiktok" in err and "브리프 파일" in err


def test_check_command(tmp_path, capsys):
    brief = Brief(topic="테스트 주제", keywords=["AI 에이전트"])
    research = template_research(brief, "2026-09-28")
    (tmp_path / "brief.json").write_text(brief.model_dump_json(), encoding="utf-8")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    good = drafts / "linkedin.r1.json"
    bad = drafts / "linkedin.r0.json"
    good.write_text(template_draft(brief, research, "linkedin", 1).model_dump_json(), encoding="utf-8")
    bad.write_text(template_draft(brief, research, "linkedin", 0).model_dump_json(), encoding="utf-8")
    assert main(["check", str(good)]) == 0
    out = capsys.readouterr().out
    assert "4/4개 통과" in out and "brief.json" in out
    assert main(["check", str(bad), "--json"]) == 1
    checks = json.loads(capsys.readouterr().out)
    assert {c["id"] for c in checks if not c["passed"]} == {"hook_length", "hashtags"}
    assert main(["check", str(tmp_path / "missing.json")]) == 2
    # UTF-8 BOM (Windows editors / PowerShell 5.1) in both the draft and the brief
    (tmp_path / "brief.json").write_bytes(b"\xef\xbb\xbf" + brief.model_dump_json().encode("utf-8"))
    bom = drafts / "linkedin.bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + good.read_bytes())
    capsys.readouterr()
    assert main(["check", str(bom)]) == 0
    assert "4/4개 통과" in capsys.readouterr().out


def test_sample_brief_and_help(capsys):
    assert main(["sample-brief"]) == 0
    assert json.loads(capsys.readouterr().out)["topic"]
    assert main([]) == 2
