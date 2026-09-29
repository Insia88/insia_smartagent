"""AST guard for the publishing surface (DESIGN.md 0-4 invariants 1-2, 10-1).

* On the server/CLI side only ``server_publish.py`` imports ``insia_agents.publishers``; in ``cli.py`` only the
  ``cmd_publish_*`` / ``_publish_*`` functions do (never at module level, never ``run-due`` / calendar / serve).
* ``HumanConfirmation`` is constructed in exactly four places: the publish and resolve handlers of
  ``server_publish.py`` and ``cmd_publish_send`` / ``cmd_publish_resolve`` of ``cli.py`` — anywhere else in the
  package (tests excluded) is a failure, including aliases (``HC = HumanConfirmation``), ``__new__``, ``replace``
  and ``getattr`` tricks.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("no_network")  # loopback only (tests/conftest.py)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "insia_agents"
PUBLISHERS = PACKAGE / "publishers"
# Python outside the package that ships with the repository (tests excluded): scanned for HumanConfirmation too.
EXTRA_DIRS = (ROOT / "scripts", ROOT / "examples")
ALLOWED_CONFIRMATIONS = {("server_publish.py", "_h_publish_item"), ("server_publish.py", "_h_publish_resolve"),
                         ("cli.py", "cmd_publish_send"), ("cli.py", "cmd_publish_resolve")}


def _sources() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _repo_sources() -> list[Path]:
    """The package plus the repository's other shipped Python (scripts/, examples/); never tests/."""
    extra = [p for d in EXTRA_DIRS if d.is_dir() for p in d.rglob("*.py") if "__pycache__" not in p.parts]
    return _sources() + sorted(extra)


@functools.lru_cache(maxsize=None)
def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _tree(path: Path) -> ast.AST:
    return ast.parse(_text(path), filename=str(path))


@functools.lru_cache(maxsize=None)
def _nodes(path: Path) -> tuple[ast.AST, ...]:
    """Every node of a file, walked once (the files are big: the tests share this)."""
    return tuple(ast.walk(_tree(path)))


@functools.lru_cache(maxsize=None)
def _stacks(path: Path) -> dict[int, tuple[str, ...]]:
    """``id(node) → names of the enclosing functions (outermost first)`` for every import and call of a file, in
    one pass over the tree."""
    found: dict[int, tuple[str, ...]] = {}

    def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, stack + (child.name,))
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                found[id(child)] = stack
            visit(child, stack)

    visit(_tree(path), ())
    return found


def _enclosing_functions(path: Path, node: ast.AST) -> list[str]:
    """Innermost first (like walking up the parents)."""
    return list(reversed(_stacks(path).get(id(node), ())))


# A file is parsed only when "publishers" could be part of an import (the text is cheap to scan, the tree is not):
# ``from X import …`` / ``import …`` with the name on the line or inside the parentheses, or a dynamic import.
_IMPORT_STATEMENT = re.compile(r"^[ \t]*(?:from[ \t]+(\S+)[ \t]+)?import[ \t]+(\([^)]*\)|[^\n]*)", re.M)


def _may_import_publishers(text: str) -> bool:
    if "publishers" not in text:
        return False
    if "import_module" in text or "__import__" in text:
        return True
    return any("publishers" in (match.group(1) or "") + match.group(2) for match in _IMPORT_STATEMENT.finditer(text))


def _module_of(path: Path) -> str:
    rel = path.relative_to(PACKAGE.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _publisher_imports(path: Path, tree: ast.AST, nodes: tuple[ast.AST, ...] | None = None) -> list[ast.AST]:
    """Import statements (and import_module/__import__ calls) that reach ``insia_agents.publishers``."""
    package = _module_of(path) if path.name == "__init__.py" else _module_of(path).rpartition(".")[0]
    hits: list[ast.AST] = []
    for node in nodes if nodes is not None else ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "insia_agents.publishers" or alias.name.startswith("insia_agents.publishers.")
                   for alias in node.names):
                hits.append(node)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            names = [f"{target}.{alias.name}" for alias in node.names]
            if any(t == "insia_agents.publishers" or t.startswith("insia_agents.publishers.")
                   for t in [target, *names]):
                hits.append(node)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and any(
                    isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "publishers" in arg.value
                    for arg in node.args):
                hits.append(node)
    return hits


def _where(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}"


def test_only_server_publish_and_cli_publish_functions_import_the_publishers():
    offenders = []
    for path in _sources():
        if path.is_relative_to(PUBLISHERS):
            continue  # the package itself
        if not _may_import_publishers(_text(path)):  # every import form names the package on its statement
            continue
        if path.name == "server_publish.py" and path.parent == PACKAGE:
            continue
        hits = _publisher_imports(path, _tree(path), _nodes(path))
        if path.name == "cli.py" and path.parent == PACKAGE:
            hits = [node for node in hits if not any(
                name.startswith(("cmd_publish_", "_publish_")) for name in _enclosing_functions(path, node))]
        offenders += [_where(path, node) for node in hits]
    assert offenders == [], f"publishers imported outside server_publish.py / cli.py publish functions: {offenders}"


def test_server_py_reaches_publishing_only_through_server_publish():
    text = _text(PACKAGE / "server.py")
    assert re.search(r"^from \.server_publish import", text, re.M)
    assert not _may_import_publishers(text)  # not even an import-shaped line names the package


def _confirmation_names(nodes: tuple[ast.AST, ...] | list[ast.AST]) -> set[str]:
    """Every local name bound to the ``HumanConfirmation`` class: imports (``as`` aliases) and assignments."""
    names = {"HumanConfirmation"}
    relevant = [node for node in nodes if isinstance(node, (ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.NamedExpr))]
    changed = True
    while changed:
        changed = False
        for node in relevant:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "HumanConfirmation" and (alias.asname or alias.name) not in names:
                        names.add(alias.asname or alias.name)
                        changed = True
            elif node.value is not None:
                value = node.value
                refers = (isinstance(value, ast.Name) and value.id in names) or (
                    isinstance(value, ast.Attribute) and value.attr == "HumanConfirmation")
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if refers and isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        changed = True
    return names


def _confirmation_constructions(tree: ast.AST, nodes: tuple[ast.AST, ...] | None = None) -> list[ast.AST]:
    nodes = nodes if nodes is not None else tuple(ast.walk(tree))
    names = _confirmation_names(nodes)
    found: list[ast.AST] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            found.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "HumanConfirmation":
            found.append(node)
        elif isinstance(func, ast.Attribute) and func.attr in ("__new__", "__call__") and (
                (isinstance(func.value, ast.Name) and func.value.id in names)
                or (isinstance(func.value, ast.Attribute) and func.value.attr == "HumanConfirmation")):
            found.append(node)
        elif any(isinstance(arg, ast.Name) and arg.id in names for arg in node.args) and (
                (isinstance(func, ast.Attribute) and func.attr in ("__new__", "replace", "copy", "deepcopy"))
                or (isinstance(func, ast.Name) and func.id in ("replace", "copy", "deepcopy"))):
            found.append(node)
        elif isinstance(func, ast.Name) and func.id in ("getattr",) and any(
                isinstance(arg, ast.Constant) and arg.value == "HumanConfirmation" for arg in node.args):
            found.append(node)
    return found


def test_human_confirmation_is_built_only_by_the_publish_and_resolve_entry_points():
    seen, offenders = set(), []
    for path in _repo_sources():
        if path.is_relative_to(PUBLISHERS) and path.name == "base.py":
            continue  # the class definition (it constructs nothing)
        if "HumanConfirmation" not in _text(path):  # an alias or getattr still has to name the class once
            continue
        for node in _confirmation_constructions(_tree(path), _nodes(path)):
            functions = _enclosing_functions(path, node)
            where = (path.name if path.parent == PACKAGE else "", functions[0] if functions else "")
            if where in ALLOWED_CONFIRMATIONS:
                seen.add(where)
            else:
                offenders.append(_where(path, node))
    assert offenders == [], f"HumanConfirmation constructed outside the allowed entry points: {offenders}"
    assert seen == ALLOWED_CONFIRMATIONS  # the four entry points exist (renaming one needs this test updated)


@pytest.mark.parametrize("snippet", [
    "from insia_agents.publishers import HumanConfirmation\nHumanConfirmation('cli', 'x', 'pv', 'h')\n",
    "from .publishers import HumanConfirmation as HC\ndef run_due():\n    HC('cli', 'x', 'pv', 'h')\n",
    "from .publishers import base\nConfirm = base.HumanConfirmation\ndef later():\n    Confirm('cli', 'x', 'pv', 'h')\n",
    "from .publishers.base import HumanConfirmation\nobj = HumanConfirmation.__new__(HumanConfirmation)\n",
    "import dataclasses\nfrom .publishers import HumanConfirmation\nx = dataclasses.replace(y, via='cli')\n"
    "z = HumanConfirmation('cli','x','pv','h')\n",
    "from insia_agents import publishers\nc = publishers.HumanConfirmation(via='cli', requested_by='x', preview_id='pv',"
    " preview_hash='h')\n",
    "from .publishers import HumanConfirmation\nmake = getattr(mod, 'HumanConfirmation')\n",
])
def test_the_confirmation_guard_catches_aliases_and_tricks(snippet):
    assert _confirmation_constructions(ast.parse(snippet))


@pytest.mark.parametrize("snippet", [
    "from insia_agents.publishers import PublishService\n",
    "from . import publishers\n",
    "import insia_agents.publishers.service as svc\n",
    "import importlib\nimportlib.import_module('insia_agents.publishers')\n",
    "def cmd_run_due(args):\n    from .publishers.service import PublishService\n",
    "from .publishers.base import HumanConfirmation\n",
    "__import__('insia_agents.publishers.service')\n",
])
def test_the_import_guard_catches_every_import_form(snippet):
    fake = PACKAGE / "planner.py"  # resolves relative imports as a module of the package would
    assert _publisher_imports(fake, ast.parse(snippet))
    assert _may_import_publishers(snippet)  # and the text pre-filter never skips such a file


def test_the_text_prefilter_also_sees_names_inside_parentheses():
    assert _may_import_publishers("from . import (\n    config,\n    publishers,\n)\n")
    assert not _may_import_publishers('"""The publishers package is never imported here."""\nimport json\n')


# Commands that may reach a publish helper: the publish commands themselves, and doctor (read-only checks).
PUBLISH_HELPER_CALLERS = ("cmd_doctor",)


def test_cli_publish_imports_are_all_inside_publish_functions():
    path = PACKAGE / "cli.py"
    hits = _publisher_imports(path, _tree(path), _nodes(path))
    assert hits, "cli.py should reach the publishers from its publish commands"
    for node in hits:
        functions = _enclosing_functions(path, node)
        assert functions and any(name.startswith(("cmd_publish_", "_publish_")) for name in functions), _where(path, node)
    # no other command (run-due, calendar, plan-week, serve, items …) calls a publish command or helper
    commands = [node for node in _nodes(path) if isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")]
    assert {"cmd_run_due", "cmd_plan_week", "cmd_calendar_generate", "cmd_serve", "cmd_items_publish",
            "cmd_items_schedule", "cmd_publish_send"} <= {node.name for node in commands}
    for node in commands:
        if node.name.startswith("cmd_publish_") or node.name in PUBLISH_HELPER_CALLERS:
            continue
        called = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not {name for name in called if name.startswith(("cmd_publish_", "_publish_"))}, node.name
    # doctor's helper only reads: it asks the service for its report and closes it
    helper = next(node for node in commands + [n for n in _nodes(path) if isinstance(n, ast.FunctionDef)]
                  if node.name == "_publish_doctor_checks")
    methods = {n.func.attr for n in ast.walk(helper) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "service"}
    assert methods == {"doctor_report", "shutdown"}


def test_no_yes_flag_and_no_schedule_option_on_publish_commands():
    from insia_agents.cli import build_parser

    parser = build_parser()
    publish = next(a for a in parser._subparsers._group_actions[0].choices.items() if a[0] == "publish")[1]
    for name, sub in publish._subparsers._group_actions[0].choices.items():
        options = {opt for action in sub._actions for opt in action.option_strings}
        assert not options & {"--yes", "-y", "--force", "--at", "--when", "--schedule", "--cron", "--delay"}, name
    for name in ("run-due", "plan-week", "calendar"):
        text = parser._subparsers._group_actions[0].choices[name].format_help()
        assert "--publish" not in text
