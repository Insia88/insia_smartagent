"""No automatic publishing (package A, DESIGN.md 0-4 invariants 1–3): the scheduled / agent code paths never
reach the publishing package — checked on the source (AST) and by running ``run-due`` with a transport that
fails the test if anything calls it. The server/CLI surface (only ``server_publish.py`` and ``cmd_publish_*``
import it, ``HumanConfirmation`` is built only there) is ``tests/test_no_autopublish_surface.py`` (package B)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "insia_agents"
PKG = "insia_agents"
TARGET = f"{PKG}.publishers"
# Names that only exist to publish: referencing them (even through an alias) is a violation.
FORBIDDEN_NAMES = frozenset({"publishers", "HumanConfirmation", "PublishService", "server_publish"})
# Modules that run without a person confirming a post: pipeline, planner, jobs, import, agents, backends.
GUARDED = ["pipeline.py", "planner.py", "actions.py", "importer.py", "agents/*.py", "backends/*.py"]
# cli.py commands that run on a schedule or for the calendar (cron, run-due) must not touch publishing either.
SCHEDULED_COMMANDS = ("cmd_run_due", "cmd_plan_week", "cmd_calendar_list", "cmd_calendar_generate", "cmd_calendar_skip",
                      "cmd_calendar_move", "cmd_run", "cmd_resume", "cmd_review", "cmd_revise", "cmd_import_run")


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE.parent).with_suffix("")
    return ".".join(rel.parts)


def _resolve(module: str | None, level: int, current: str) -> str:
    if level == 0:
        return module or ""
    base = current.split(".")[:-level]  # the module's package, then up
    return ".".join([*base, module] if module else base)


def violations(source: str, module: str) -> list[str]:
    """Every way ``source`` (module ``module``) could reach the publishing package, as readable strings."""
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == TARGET or alias.name.startswith(TARGET + ".") or alias.name.endswith(".server_publish"):
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve(node.module, node.level, module)
            names = [alias.name for alias in node.names]
            if resolved == TARGET or resolved.startswith(TARGET + ".") or resolved.endswith("server_publish"):
                found.append(f"line {node.lineno}: from {resolved} import {', '.join(names)}")
            elif resolved in (PKG, PKG.split(".")[0]) and set(names) & {"publishers", "server_publish"}:
                found.append(f"line {node.lineno}: from {resolved} import {', '.join(names)}")
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES - {"publishers", "server_publish"}:
                    found.append(f"line {node.lineno}: imports {alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"line {node.lineno}: name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"line {node.lineno}: attribute .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                TARGET in node.value or node.value in ("publishers", ".publishers", "server_publish")):
            found.append(f"line {node.lineno}: string {node.value!r}")
    return found


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for pattern in GUARDED:
        files.extend(sorted(PACKAGE.glob(pattern)))
    return files


def test_guarded_modules_exist():
    names = {_module_name(p) for p in _guarded_files()}
    assert {f"{PKG}.pipeline", f"{PKG}.planner", f"{PKG}.actions", f"{PKG}.importer"} <= names
    assert any(n.startswith(f"{PKG}.agents.") for n in names) and any(n.startswith(f"{PKG}.backends.") for n in names)


@pytest.mark.parametrize("path", _guarded_files(), ids=lambda p: str(p.relative_to(PACKAGE)))
def test_scheduled_and_agent_code_never_reaches_publishing(path):
    assert violations(path.read_text(encoding="utf-8"), _module_name(path)) == []


def test_scheduled_cli_commands_never_reach_publishing():
    tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    present = [name for name in SCHEDULED_COMMANDS if name in functions]
    assert "cmd_run_due" in present and "cmd_plan_week" in present
    for name in present:
        source = ast.unparse(functions[name])
        found = violations(source, f"{PKG}.cli")
        calls = [n.func.id for n in ast.walk(functions[name]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and (n.func.id.startswith("_publish") or n.func.id.startswith("cmd_publish"))]
        assert found == [] and calls == [], f"{name}: {found or calls}"


@pytest.mark.parametrize("injected", [
    "from .publishers import PublishService",
    "from .publishers.linkedin import LinkedInPublisher",
    "from . import publishers",
    "from insia_agents import publishers",
    "import insia_agents.publishers as p",
    "import insia_agents.publishers.service",
    "from insia_agents.publishers.base import HumanConfirmation as HC",
    "HC = HumanConfirmation",
    "import importlib\nmod = importlib.import_module('insia_agents.publishers')",
    "mod = __import__('insia_agents.publishers', fromlist=['x'])",
    "from .server_publish import build_publish_service",
    "def later():\n    from .publishers import service\n    return service",
])
def test_the_guard_catches_an_import_slipped_into_the_planner(injected):
    # planner.py itself is clean (test above), so checking the added lines as part of the planner module is the
    # same as checking the whole edited file — the full concatenation is checked once below
    assert violations(injected + "\n", f"{PKG}.planner"), injected


def test_the_guard_fails_on_the_edited_planner_file():
    planner = (PACKAGE / "planner.py").read_text(encoding="utf-8")
    edited = planner + "\n\nfrom .publishers import PublishService\n"
    assert violations(planner, f"{PKG}.planner") == [] and violations(edited, f"{PKG}.planner")


def test_agent_subpackage_relative_imports_are_resolved():
    assert violations("from ..publishers import http\n", f"{PKG}.agents.orchestrator")
    assert violations("from .. import publishers\n", f"{PKG}.backends.mock_backend")
    assert violations("from ..db import Workspace\nfrom .common import x\n", f"{PKG}.agents.orchestrator") == []


def test_run_due_sends_nothing_even_when_publishing_is_connected(tmp_path, monkeypatch, capsys):
    from insia_agents import cli
    from insia_agents.db import Workspace
    from insia_agents.models import Draft, PlannedSlot, PublishConnection
    from insia_agents.publishers import http, service
    from insia_agents.publishers.store import CredentialStore

    home = tmp_path / "ws"
    monkeypatch.setenv("INSIA_HOME", str(home))
    monkeypatch.setenv("INSIA_TODAY", "2026-09-28")
    monkeypatch.setenv("INSIA_PUBLISH_FAKE", "1")  # even the fake platform must stay untouched
    monkeypatch.setenv("INSIA_PUBLISH_INSTAGRAM", "1")
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    def forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} was called by a scheduled command")
        return fail

    for cls in (http.UrllibTransport, http.FakePlatformTransport, http.FakeTransport):
        monkeypatch.setattr(cls, "request", forbidden(f"{cls.__name__}.request"))
    for method in ("send", "preview", "resolve", "refresh_tokens", "__init__"):
        monkeypatch.setattr(service.PublishService, method, forbidden(f"PublishService.{method}"))

    with Workspace(home) as ws:
        item = ws.create_item("linkedin", "예정일이 오늘인 승인된 글")
        ws.add_version(item.id, Draft(channel="linkedin", round=0, title="t", content="본문", hashtags=["#a"]),
                       source="human")
        ws.set_item_status(item.id, "approved", force=True)
        ws.set_item_status(item.id, "scheduled", scheduled_at="2026-09-28")
        ws.save_publish_connection(PublishConnection(platform="linkedin", account_id="sub1", status="connected"))
        ws.add_slots([PlannedSlot(date="2026-09-28", channel="linkedin", topic="오늘 만들 초안", angle="체크리스트",
                                  keywords=["AI"], goal="문의")])
    CredentialStore(home / "credentials").set_many("linkedin", {"access_token": "AQUv-connected-token-1", "sub": "sub1"})

    code = cli.main(["run-due", "--mode", "mock", "--until", "2026-09-28", "--quiet"])
    capsys.readouterr()
    assert code == 0 and calls == []
    with Workspace(home) as ws:
        assert ws.get_item(item.id).item.status == "scheduled"  # a due date never means "publish now"
        assert ws.list_publish_attempts() == [] and len(ws.list_items()) == 2
