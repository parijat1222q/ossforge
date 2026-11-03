"""Local receipt binding test results to an exact source tree and base revision.

This is a stale-evidence guard, not a signature, sandbox or claim of correctness.
A commit-message-only rewrite (including approved DCO signoff) preserves the tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class StaleEvidence(ValueError):
    pass


def _git(clone: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=clone, capture_output=True, text=True)
    if p.returncode:
        raise StaleEvidence("verification evidence: cannot inspect git state; re-run verify")
    return p.stdout.strip()


def capture(clone: Path, base: str, commands: list[list[str]]) -> dict:
    if _git(clone, "status", "--porcelain"):
        raise StaleEvidence("verification evidence: work tree is dirty; commit intended changes and re-run verify")
    branch = _git(clone, "symbolic-ref", "--short", "HEAD")
    return {"schema": 1, "base": _git(clone, "rev-parse", base),
            "tree": _git(clone, "rev-parse", "HEAD^{tree}"), "branch": branch,
            "commands": commands}


def check(clone: Path, base: str, branch: str, receipt: dict) -> None:
    if not receipt or receipt.get("schema") != 1 or not receipt.get("commands"):
        raise StaleEvidence("verification evidence missing or unsupported; re-run verify")
    current = capture(clone, base, receipt["commands"])
    if any(current[k] != receipt.get(k) for k in ("base", "tree", "branch")) or current["branch"] != branch:
        raise StaleEvidence("verification evidence no longer matches tree, base or branch; re-run verify")

    if 'verification_kind' in receipt and receipt['verification_kind'] != 'python_native_replay':
        raise StaleEvidence('unknown verification kind; re-run supported verification')
    if receipt.get('verification_kind') == 'python_native_replay' or 'native_replay' in receipt:
        from . import native_replay
        if receipt.get('verification_kind') != 'python_native_replay' or not receipt.get('native_replay'):
            raise StaleEvidence('native replay dependency evidence missing; re-run verification')
        try:
            native_replay.check_dependencies(clone, receipt['native_replay'])
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
            raise StaleEvidence('native replay evidence stale: ' + str(exc)) from exc
