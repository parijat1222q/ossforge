"""Write-boundary safety: kill switch, protected paths, secret scan, per-repo limits,
and an append-only audit log (evidence receipt per action).

Protected-path idea adapted from ContribAI's governance-file protection.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
import time
from pathlib import Path

from .config import Config
from . import policy

PROTECTED_GLOBS = (
    ".github/workflows/*", ".github/CODEOWNERS", "CODEOWNERS", "LICENSE*", "COPYING*", "SECURITY.md",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "GOVERNANCE.md", ".github/FUNDING.yml", "AGENTS.md",
    "CLAUDE.md", ".github/dependabot.yml",
)

SECRET_PATTERNS = {
    "aws-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "slack-token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "generic-secret": re.compile(r"(?i)(?:api[_-]?key|secret|passwd|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
}

MAX_CHANGED_LINES = 400
MAX_CHANGED_FILES = 10


class Blocked(Exception):
    pass


def kill_switch(cfg: Config) -> None:
    if Path(cfg.kill_file).exists():
        raise Blocked(f"kill switch present: {cfg.kill_file}")


def protected(path: str) -> bool:
    # Protect the governance documents we actually read at contribution time.
    # README files and PR templates remain editable ordinary documentation.
    policy_paths = (p for p in policy.POLICY_FILES
                    if "README" not in p and "PULL_REQUEST_TEMPLATE" not in p.upper())
    return any(fnmatch.fnmatch(path, g) for g in (*PROTECTED_GLOBS, *policy_paths))


def check_diff(changed_files: list[str], diff_text: str) -> list[str]:
    """Return a list of violations; empty means the diff may leave the machine."""
    out = [f"protected path: {f}" for f in changed_files if protected(f)]
    if len(changed_files) > MAX_CHANGED_FILES:
        out.append(f"{len(changed_files)} files changed (max {MAX_CHANGED_FILES})")
    added = [ln for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    if len(added) > MAX_CHANGED_LINES:
        out.append(f"{len(added)} added lines (max {MAX_CHANGED_LINES})")
    for ln in added:
        for name, rx in SECRET_PATTERNS.items():
            if rx.search(ln):
                out.append(f"possible secret ({name}) in added line")
                break
    return out


def repo_allowed(cfg: Config, repo: str, history: list[dict], now: float | None = None) -> str | None:
    """history: audit/outcome records. Returns a reason string if the repo is off-limits."""
    now = now or time.time()
    mine = [h for h in history if h.get("repo") == repo]
    open_prs = [h for h in mine if h.get("event") == "pr_opened" and h.get("state", "open") == "open"]
    if len(open_prs) >= cfg.max_open_prs_per_repo:
        return f"{len(open_prs)} PR(s) already open here (limit {cfg.max_open_prs_per_repo})"
    for h in mine:
        if h.get("state") == "closed_unmerged" and now - h.get("ts", 0) < cfg.cooldown_days * 86400:
            return f"cooldown after unmerged close ({cfg.cooldown_days}d)"
    return None


class Audit:
    """Append-only JSONL. One line per outbound action, enough to reconstruct what/why/when."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def log(self, event: str, **data) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "event": event, **data}) + "\n")

    def read(self) -> list[dict]:
        try:
            records = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise Blocked("audit history unreadable; recover it before publishing") from exc
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("event"), str) or not record["event"]:
                raise Blocked("audit history invalid; recover it before publishing")
            if record["event"] in ("pr_opened", "pr_state"):
                if (not isinstance(record.get("repo"), str) or not record["repo"]
                        or type(record.get("pr")) is not int or record["pr"] <= 0):
                    raise Blocked("audit PR history invalid; recover it before publishing")
                if record.get("state", "open") not in ("open", "merged", "closed_unmerged"):
                    raise Blocked("audit PR state invalid; recover it before publishing")
                if record["event"] == "pr_state" and "state" not in record:
                    raise Blocked("audit PR state missing; recover it before publishing")
                if record["event"] == "pr_opened" and (type(record.get("ts")) not in (int, float)
                        or not math.isfinite(record["ts"]) or record["ts"] < 0):
                    raise Blocked("audit PR timestamp invalid; recover it before publishing")
        return records

    def update_state(self, repo: str, pr: int, state: str) -> None:
        """Outcome changes are recorded as new events; the latest wins when read by `current_state`."""
        self.log("pr_state", repo=repo, pr=pr, state=state)


def effective_history(records: list[dict]) -> list[dict]:
    """Fold pr_state events onto pr_opened events so callers see current state."""
    latest = {(r["repo"], r["pr"]): r["state"] for r in records if r.get("event") == "pr_state"}
    out = []
    for r in records:
        if r.get("event") == "pr_opened":
            r = {**r, "state": latest.get((r["repo"], r["pr"]), "open")}
        out.append(r)
    return out
