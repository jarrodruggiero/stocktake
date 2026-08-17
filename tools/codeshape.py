"""Fingerprint the CODE in a tree, ignoring comments and docstrings.

A comment pass must not touch a non-comment line. This makes
that checkable instead of hoped for: snapshot before, compare after. It caught a
531-line duplicated region that the suite also caught — but only because that
region happened to be executed.

    python codeshape.py save   # write the snapshot
    python codeshape.py check  # compare against it
"""
import ast
import hashlib
import json
import sys
from pathlib import Path

SNAP = Path(__file__).with_name("codeshape.json")  # gitignored


class Strip(ast.NodeTransformer):
    """Remove docstrings so prose edits inside them are invisible here."""

    def _strip(self, node):
        self.generic_visit(node)
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip
    visit_ClassDef = _strip


def shape() -> dict:
    out = {}
    for base in ("app", "tests"):
        for p in sorted(Path(base).rglob("*.py")):
            tree = Strip().visit(ast.parse(p.read_text()))
            ast.fix_missing_locations(tree)
            out[str(p)] = hashlib.sha256(ast.dump(tree).encode()).hexdigest()[:16]
    return out


if __name__ == "__main__":
    now = shape()
    if sys.argv[1] == "save":
        SNAP.write_text(json.dumps(now, indent=1))
        print(f"snapshot: {len(now)} files")
    else:
        was = json.loads(SNAP.read_text())
        changed = sorted(k for k in set(was) | set(now) if was.get(k) != now.get(k))
        if changed:
            print("CODE CHANGED in:")
            for c in changed:
                print("  ", c, "(added)" if c not in was else "(removed)" if c not in now else "")
            sys.exit(1)
        print(f"code unchanged across {len(now)} files")
