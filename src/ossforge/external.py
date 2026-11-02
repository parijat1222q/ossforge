"""Extra detectors: ruff bug rules (if installed) and a few high-signal JS/TS regexes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from .audit import SKIP_DIRS, Finding

# F: pyflakes (undefined names, unused results), B: bugbear (real bug patterns)
RUFF_SELECT = "F821,F811,F601,F632,B006,B015,B018,B023,B025,B904,PLW0120,PLW0602"

JS_PATTERNS = [
    (re.compile(r"\.forEach\(\s*async\b"), "async-foreach", "async callback in forEach is not awaited", "bug"),
    (re.compile(r"new Promise\(\s*async\b"), "async-promise-executor", "async Promise executor swallows errors", "bug"),
    (re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"), "empty-catch", "empty catch block hides failures", "bug"),
]
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs"}


def ruff_findings(root: Path, python: str = "") -> list[Finding]:
    py = python or sys.executable
    try:
        p = subprocess.run(
            [py, "-m", "ruff", "check", "--select", RUFF_SELECT, "--output-format", "json", "--no-cache",
             "--exit-zero", str(root)],
            capture_output=True, text=True, encoding="utf-8", timeout=300,
        )
        data = json.loads(p.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    out = []
    for d in data:
        try:
            rel = str(Path(d["filename"]).resolve().relative_to(root.resolve())).replace("\\", "/")
        except (ValueError, KeyError):
            continue
        if set(Path(rel).parts) & (SKIP_DIRS | {"tests", "test", "docs", "examples"}):
            continue
        out.append(Finding(rel, d["location"]["row"], f"ruff-{d['code']}", d["message"], "bug"))
    return out


def js_findings(root: Path, max_files: int = 2000) -> list[Finding]:
    out: list[Finding] = []
    n = 0
    for p in root.rglob("*"):
        # Findings must describe regular source files in this checkout, never
        # dereference aliases into unrelated files (or open FIFOs/devices).
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            continue
        if p.suffix not in JS_EXT:
            continue
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        if set(Path(rel).parts) & SKIP_DIRS:
            continue
        if ".test." in p.name or ".spec." in p.name or "__tests__" in Path(rel).parts:
            continue
        n += 1
        if n > max_files:
            break
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        for rx, kind, detail, sev in JS_PATTERNS:
            for m in rx.finditer(text):
                out.append(Finding(rel, text.count("\n", 0, m.start()) + 1, kind, detail, sev))
    return out
