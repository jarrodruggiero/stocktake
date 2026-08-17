"""Rank the prose in a tree: long comment blocks and long docstrings.

    python tools/prose.py app          # blocks of 4+ comment / 8+ docstring lines
    python tools/prose.py tests 8 999  # comment blocks of 8+ lines only

Used for the comment pass — work highest-first. See docs/contributing/style.md
for what earns a comment, and decisions.md for where the long reasoning goes.
"""
import ast
import sys
from pathlib import Path

MIN_COMMENT = int(sys.argv[2]) if len(sys.argv) > 2 else 4
MIN_DOC = int(sys.argv[3]) if len(sys.argv) > 3 else 8

targets = sorted(Path(sys.argv[1]).rglob("*.py"))
rows = []
for p in targets:
    text = p.read_text()
    lines = text.splitlines()
    # comment runs
    run, start = 0, 0
    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("#"):
            if run == 0:
                start = i
            run += 1
        else:
            if run >= MIN_COMMENT:
                rows.append((run, str(p), start, "comment"))
            run = 0
    if run >= MIN_COMMENT:
        rows.append((run, str(p), start, "comment"))
    # docstrings
    try:
        tree = ast.parse(text)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                n = len(doc.splitlines())
                if n >= MIN_DOC:
                    name = getattr(node, "name", "<module>")
                    line = getattr(node, "lineno", 1)
                    rows.append((n, str(p), line, f"docstring {name}"))
rows.sort(reverse=True)
print(f"{len(rows)} blocks >= {MIN_COMMENT} comment lines / {MIN_DOC} docstring lines")
print(f"total prose in them: {sum(r[0] for r in rows)} lines\n")
for n, f, line, kind in rows[:40]:
    print(f"{n:4d}  {f}:{line}  {kind}")
