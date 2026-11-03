"""Verification: a candidate is only real if a new test FAILS without the fix and PASSES with it.

Default commands use a scrubbed environment, which is hygiene, not isolation.
Optional bwrap mode confines native test/check subprocesses with no network,
a private home and read-only checkout plus explicitly prepared runtime mounts.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence, predictor, isolation

SAFE_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "PYTHONIOENCODING")


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "CI": "1", **(extra or {})})
    return env


def _run(cmd: list[str], cwd: Path, timeout: int = 600, env: dict | None = None) -> tuple[int, str]:
    try:
        with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace",
                              env=env or scrubbed_env(), start_new_session=True) as proc:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # A test runner can spawn workers. Kill the entire timed-out group
                # before restoring source files or running the patched test.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                return 124, f"timeout after {timeout}s; test process group terminated"
            return proc.returncode, (out + err)[-4000:]
    except OSError as e:
        return 127, str(e)


def venv_python(work: Path, repo: str) -> Path:
    v = work / "venvs" / repo.replace("/", "__")
    py = v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(v)], check=True, capture_output=True)
        subprocess.run([str(py), "-m", "pip", "install", "-q", "pytest", "ruff"], capture_output=True)
    return py


def install_project(py: Path, clone: Path, extras: list[str] | None = None) -> tuple[int, str]:
    targets = extras or ["."]
    cmd = [str(py), "-m", "pip", "install", "-q"]
    for t in targets:
        cmd += ["-e", t]
    return _run(cmd, clone, timeout=900)


def changed_files(clone: Path, base: str) -> list[str]:
    _, out = _run(["git", "diff", "--name-only", f"{base}..HEAD"], clone)
    return [x for x in out.splitlines() if x.strip()]


@dataclass
class VerifyResult:
    failed_without_fix: bool = False
    passed_with_fix: bool = False
    lint_ok: bool = True
    fix_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failed_without_fix and self.passed_with_fix and self.lint_ok and bool(self.fix_files) and bool(self.test_files)


def verify_fix(clone: Path, base: str, py: Path, test_args: list[str], timeout: int = 600,
               *, test_command: list[str] | None = None, check_commands: list[list[str]] | None = None,
               failure_code: int = 1, isolation_mode: str = "none", readonly_paths: list[str] | None = None, contribution_base: dict | None = None) -> VerifyResult:
    """The branch at HEAD holds fix + test. Revert only the non-test files to base, expect failure,
    restore, expect success, then lint the touched files."""
    r = VerifyResult()
    if failure_code not in (1, 101) or test_command == [] or any(not c for c in check_commands or []):
        r.log.append("test command must be nonempty and failure_code must be 1 or 101")
        return r
    try:
        initial = evidence.capture(clone, base, [], **({"contribution_base": contribution_base} if contribution_base else {}))
    except evidence.StaleEvidence as e:
        r.log.append(str(e))
        return r
    files = changed_files(clone, base)
    r.test_files = [f for f in files if predictor.is_test_path(f)]
    r.fix_files = [f for f in files if not predictor.is_test_path(f)]
    if not r.fix_files or not r.test_files:
        r.log.append("branch must change both a source file and a test file")
        return r
    pytest = test_command if test_command is not None else [str(py), "-m", "pytest", "-q", "-x", *test_args]

    # Validate before changing source state; requested containment never falls back.
    def run_native(command, seconds):
        try:
            wrapped = isolation.wrap(command, clone, isolation_mode, [] if readonly_paths is None else readonly_paths)
        except (isolation.Unavailable, OSError) as exc:
            return 127, str(exc)
        return _run(wrapped, clone, seconds)
    try:
        isolation.wrap(pytest, clone, isolation_mode, [] if readonly_paths is None else readonly_paths)
    except (isolation.Unavailable, OSError) as exc:
        r.log.append(str(exc))
        return r

    if isolation_mode != "none":
        rc_probe, out_probe = run_native(['/usr/bin/true'], 10)
        if rc_probe:
            r.log.append(f"requested verification isolation unavailable rc={rc_probe}: {out_probe[-300:]}")
            return r

    # Files the fix added do not exist at base: `git checkout base -- <them>` would fail for the whole batch and
    # silently leave the fix in place. Revert existing files from base; move new files aside and put them back.
    in_base = [f for f in r.fix_files if _run(["git", "cat-file", "-e", f"{base}:{f}"], clone)[0] == 0]
    added = [f for f in r.fix_files if f not in in_base]
    reverted = True
    if test_command is None and failure_code != 1:
        r.log.append("pytest baseline must fail with exit 1, not a collection/tool error")
        return r
    try:
        if in_base:
            rc0, out0 = _run(["git", "checkout", base, "--", *in_base], clone)
            if rc0:
                reverted = False
                r.log.append(f"could not revert fix files to base, no 'without fix' evidence: {out0[-200:]}")
        for f in added:
            if (clone / f).is_file() or (clone / f).is_symlink():
                (clone / f).unlink()
        if reverted:
            rc, out = run_native(pytest, timeout)
            r.failed_without_fix = rc == failure_code
            r.log.append(f"without fix rc={rc}: {out[-300:]}")
    finally:
        # Git restores tracked content and metadata, including executable bits,
        # symlinks, additions and deletions. Byte copies lose that metadata.
        rc_restore, out_restore = _run(["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", *r.fix_files], clone)
        if rc_restore:
            r.failed_without_fix = False
            r.log.append(f"could not restore committed fix; inspect worktree before retry: {out_restore[-200:]}")

    rc, out = run_native(pytest, timeout)
    r.passed_with_fix = rc == 0
    r.log.append(f"with fix rc={rc}: {out[-300:]}")

    checks = list(check_commands or [])
    pyfiles = [f for f in files if f.endswith(".py") and (clone / f).is_file()]
    if test_command is None and pyfiles:
        checks.append([str(py), "-m", "ruff", "check", *pyfiles])
    for command in checks:
        rc, out = run_native(command, 120)
        r.lint_ok = r.lint_ok and rc == 0
        r.log.append(f"check {command[0]} rc={rc}: {out[-200:]}")

    if r.ok:
        try:
            r.evidence = evidence.capture(clone, base, [pytest, *checks], **({"contribution_base": contribution_base} if contribution_base else {}))
            r.evidence['verification_isolation'] = isolation_mode
            r.evidence['readonly_paths'] = list([] if readonly_paths is None else readonly_paths)
            if any(initial[k] != r.evidence[k] for k in ("base", "tree", "branch")):
                raise evidence.StaleEvidence("verification evidence: git state changed during tests; re-run verify")
        except evidence.StaleEvidence as e:
            r.passed_with_fix = False
            r.log.append(str(e))
    return r
