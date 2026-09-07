"""A fixture that opens an engine has to close it.

An engine holds a connection pool — up to ten on Postgres. Undisposed, those
connections come back only when CPython collects the engine, so the suite's
connection use stops depending on what it does and starts depending on when
garbage collection happens to run. It passed here at a peak of 28 connections
and exhausted Postgres's 100 on a contributor's machine, where the OCR tests
run and the engines therefore live longer.

Nothing else notices: the leak has no failing assertion, no error message and
no effect at all until a machine is slow enough. So this reads `conftest.py`
and requires every fixture that builds one to dispose it.
"""

from __future__ import annotations

import ast
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

# The two ways a fixture here gets hold of an engine. `make_session_factory`
# builds one internally and hides it behind the sessionmaker, which is exactly
# why the missing dispose was easy to miss.
OPENS_AN_ENGINE = {"make_engine", "make_session_factory"}


def _functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _calls(node: ast.AST) -> set[str]:
    """Every plain and attribute call name inside this function."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def test_every_fixture_that_opens_an_engine_disposes_it():
    tree = ast.parse(CONFTEST.read_text())

    leaking = [
        node.name for node in _functions(tree)
        if (_calls(node) & OPENS_AN_ENGINE) and "dispose" not in _calls(node)
    ]

    assert not leaking, (
        "These build a database engine and never dispose it, so its pooled "
        "connections are released only by garbage collection: "
        f"{leaking}. Add `.dispose()` at teardown.")


def test_the_scan_finds_the_fixtures_it_is_meant_to_guard():
    """A matcher that found nothing would make the check above vacuously true,
    and this is the only thing standing between a new fixture and a leak that
    shows up on somebody else's machine."""
    tree = ast.parse(CONFTEST.read_text())

    watched = [node.name for node in _functions(tree)
               if _calls(node) & OPENS_AN_ENGINE]

    assert "session_factory" in watched
    assert len(watched) >= 2
