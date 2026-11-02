"""Static bug / inefficiency candidate finder for Python source.

These are *candidates*. A finding becomes an issue/PR only after it is read in
context and confirmed with a failing test (see Agent.md quality bar). Detectors
are chosen for a low false-positive rate; noisy style checks are left to linters.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "build", "dist", "__pycache__", "site-packages"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    detail: str
    severity: str  # "bug" | "perf"


def _is_mutable(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "dict", "set"}
        and not node.args
        and not node.keywords
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.out: list[Finding] = []
        self.loop_depth = 0
        self.list_vars: list[set[str]] = [set()]

    def add(self, node: ast.AST, kind: str, detail: str, sev: str) -> None:
        self.out.append(Finding(self.path, node.lineno, kind, detail, sev))

    def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in [*node.args.defaults, *[k for k in node.args.kw_defaults if k]]:
            if _is_mutable(d):
                self.add(d, "mutable-default", f"{node.name}(): mutable default argument is shared across calls", "bug")
        self.list_vars.append(set())
        saved, self.loop_depth = self.loop_depth, 0
        self.generic_visit(node)
        self.loop_depth = saved
        self.list_vars.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, (ast.List, ast.ListComp)) or (
            isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "list"
        ):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.list_vars[-1].add(t.id)
        else:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.list_vars[-1].discard(t.id)
        self.generic_visit(node)

    def _loop(self, node: ast.AST) -> None:
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_For = _loop
    visit_While = _loop
    visit_ListComp = _loop
    visit_SetComp = _loop
    visit_DictComp = _loop
    visit_GeneratorExp = _loop

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        only_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
        if node.type is None:
            self.add(node, "bare-except", "bare except also swallows KeyboardInterrupt/SystemExit", "bug")
        elif only_pass and isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
            self.add(node, "swallowed-exception", "except Exception: pass hides failures", "bug")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, c in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(c, ast.Constant) and c.value is None:
                self.add(node, "eq-none", "compare to None with `is`/`is not`", "bug")
            if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(c, ast.Constant) and isinstance(c.value, (str, int, float, bytes)) and not isinstance(c.value, bool):
                self.add(node, "is-literal", "`is` with a literal depends on interning", "bug")
            if (
                self.loop_depth
                and isinstance(op, (ast.In, ast.NotIn))
                and isinstance(c, ast.Name)
                and c.id in self.list_vars[-1]
            ):
                self.add(node, "list-membership-in-loop", f"`in {c.id}` on a list inside a loop is O(n) per check; use a set", "perf")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            self.loop_depth
            and isinstance(f, ast.Attribute)
            and f.attr == "pop"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == 0
        ):
            self.add(node, "pop0-in-loop", "list.pop(0) in a loop is O(n^2); use collections.deque", "perf")
        self.generic_visit(node)


def scan_source(source: str, path: str = "<src>") -> list[Finding]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    v = _Visitor(path)
    v.visit(tree)
    return v.out


def scan_tree(root: Path, max_files: int = 2000) -> list[Finding]:
    out: list[Finding] = []
    n = 0
    for p in root.rglob("*.py"):
        # Findings must describe regular source files in this checkout, never
        # dereference aliases into unrelated files (or open FIFOs/devices).
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            continue
        parts = set(p.relative_to(root).parts)
        if parts & SKIP_DIRS or any(x in {"tests", "test", "docs", "examples"} for x in parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        n += 1
        if n > max_files:
            break
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out += scan_source(src, str(p.relative_to(root)).replace("\\", "/"))
    return out
