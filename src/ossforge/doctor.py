"""`ossforge doctor`: check the setup before the first real run (read-only)."""

from __future__ import annotations

import shutil
import subprocess
import sys

from . import identity
from .config import Config


def _out(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or p.stderr).strip()


def check(cfg: Config) -> list[tuple[str, bool, str]]:
    res: list[tuple[str, bool, str]] = []
    res.append(("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
    for tool in ("git", "gh"):
        res.append((f"{tool} on PATH", shutil.which(tool) is not None, shutil.which(tool) or "missing"))
    rc, out = _out([sys.executable, "-m", "ruff", "--version"])
    res.append(("ruff via current Python", rc == 0, out if rc == 0 else
                (out + "; " if out else "") + "install ossforge[tools] in this environment"))
    if shutil.which("gh"):
        rc, out = _out(["gh", "auth", "status"])
        res.append(("gh logged in", rc == 0, out.splitlines()[0] if out else ""))
    name = _out(["git", "config", "user.name"])[1]
    email = _out(["git", "config", "user.email"])[1]
    res.append(("git identity set (this directory)", bool(name and email), f"{name} <{email}>" if name and email else
                "set user.name and user.email: new commits use them"))
    miss = identity.missing_settings(cfg)
    res.append(("expected identity configured", not miss,
                "github_user, git_name, git_email" if not miss else "set in ossforge.yaml: " + ", ".join(miss) + " (publishing stops until set)"))
    if shutil.which("gh") and not miss:
        bad = identity.check_login(cfg)
        res.append(("gh login matches github_user", not bad, "; ".join(bad) or cfg.github_user))
    if not miss:
        bad = identity.check_worktree(cfg, ".")
        res.append(("effective git author/committer match config", not bad, "; ".join(bad) or f"{cfg.git_name} <{cfg.git_email}>"))
    res.append(("daily cap within hard max", 0 < cfg.cap <= 2, str(cfg.cap)))
    res.append(("disclosure mode", str(cfg.disclosure).lower() in ("auto", "true", "false"), str(cfg.disclosure)))
    return res
