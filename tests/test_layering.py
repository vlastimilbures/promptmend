"""The package's layering (#408): the commands print over the services, never the other way
round, and the services, setup's core and doctor (with its --timing) reach no Typer, Click or
Textual module, nor the Typer wiring (cli.py, trigger.py) that would bring Typer along."""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

import promptmend

PACKAGE = Path(promptmend.__file__).parent
# Typer-free: setup's core, the settings services and checks, doctor, its --timing and the Try
# tab's run.
TYPER_FREE = ("setup_flow", "settings_service", "settings_checks", "doctor", "timing", "try_run")
# What a Typer-free module never imports: the frameworks, and the modules that load them.
TYPER_MODULES = ("typer", "click", "textual", "promptmend.cli", "promptmend.trigger")
COMMANDS = "promptmend.commands"
_DYNAMIC = ("import_module", "__import__")


def _absolute(path: Path, node: ast.ImportFrom) -> str:
    package = ["promptmend", *path.relative_to(PACKAGE).parts[:-1]]
    base = package[: len(package) - node.level + 1] if node.level else []
    return ".".join([*base, *(node.module or "").split(".")]).strip(".")


def _static(path: Path, nodes: Iterable[ast.AST]) -> set[str]:
    """The modules ``nodes`` import, as absolute names; ``from . import x`` counts as
    ``promptmend.x`` too."""
    names = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(path, node)
            names.add(module)
            names |= {f"{module}.{alias.name}" for alias in node.names}
    return names


def _dynamic(nodes: Iterable[ast.AST]) -> list[str]:
    """The text of each importlib.import_module() or __import__() call's first argument (an
    f-string's literal parts), whatever the call is reached through."""
    texts = []
    for node in nodes:
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _DYNAMIC:
            continue
        arg = node.args[0]
        parts = arg.values if isinstance(arg, ast.JoinedStr) else [arg]
        texts.append(
            "".join(
                p.value for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)
            )
        )
    return texts


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _all_nodes(path: Path) -> list[ast.AST]:
    return list(ast.walk(_tree(path)))


def _names_commands(names: Iterable[str]) -> bool:
    return any(n == COMMANDS or n.startswith(f"{COMMANDS}.") for n in names)


def test_only_commands_and_tui_import_the_commands() -> None:
    """No module outside commands/ and tui/ imports promptmend.commands: not at module level,
    not inside a function, not through importlib.import_module() or __import__(). cli.py wires
    the commands into the Typer app, each loaded when it runs: it may do so inside a function
    only, never at module level (tests/test_trigger_contract.py's FORBIDDEN catches that
    too)."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] in ("commands", "tui"):
            continue
        tree = _tree(path)
        # cli.py: only what runs on import; elsewhere every node, functions too.
        if relative == Path("cli.py"):
            nodes = [n for top in tree.body if not _is_def(top) for n in ast.walk(top)]
        else:
            nodes = list(ast.walk(tree))
        dynamic = any("commands" in text for text in _dynamic(nodes))
        if _names_commands(_static(path, nodes)) or dynamic:
            offenders.append(relative.as_posix())
    assert offenders == []


def _is_def(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))


def test_the_walk_sees_lazy_and_dynamic_imports() -> None:
    # The walk above would pass on an empty set: show it finds cli.py's lazy imports.
    nodes = _all_nodes(PACKAGE / "cli.py")
    assert "promptmend.commands.ui.on_a_terminal" in _static(PACKAGE / "cli.py", nodes)
    assert any(text.startswith(".commands.") for text in _dynamic(nodes))
    calls = ast.parse(
        "import importlib\n"
        "def f(name):\n"
        "    importlib.import_module('promptmend.commands.x')\n"
        "    __import__(f'promptmend.commands.{name}')\n"
    )
    assert _dynamic(ast.walk(calls)) == ["promptmend.commands.x", "promptmend.commands."]


@pytest.mark.parametrize("module", TYPER_FREE)
def test_typer_free_modules_import_no_typer_module(module: str) -> None:
    """Statically, anywhere in the file (inside functions too), and dynamically."""
    path = PACKAGE / f"{module}.py"
    nodes = _all_nodes(path)
    names = _static(path, nodes)
    bad = {n for n in names for m in TYPER_MODULES if n == m or n.startswith(f"{m}.")}
    assert bad == set()
    assert not _names_commands(names)
    assert [t for t in _dynamic(nodes) if _dynamic_target(t) in _DYNAMIC_BAD] == []


# The first part of a dynamic import's module name, relative to promptmend.
_DYNAMIC_BAD = {"typer", "click", "textual", "cli", "trigger", "commands"}


def _dynamic_target(text: str) -> str:
    return text.lstrip(".").removeprefix("promptmend.").split(".")[0]


@pytest.mark.parametrize("module", TYPER_FREE)
def test_typer_free_modules_load_no_typer_module(module: str) -> None:
    code = (
        f"import sys, promptmend.{module}; "
        f"bad = {(*TYPER_MODULES, COMMANDS)!r}; "
        "print(sorted(m for m in sys.modules "
        "if any(m == b or m.startswith(b + '.') for b in bad)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == "[]"
