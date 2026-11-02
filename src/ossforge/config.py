"""Settings, loaded from ossforge.yaml (all keys optional)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

HARD_DAILY_MAX = 2  # not configurable above this


@dataclass
class Config:
    verification_isolation: str = "none"  # none | bwrap: isolate native test/check subprocesses only
    verification_readonly_paths: list[str] = field(default_factory=list)  # prepared runtimes, never credentials
    maintainer_admission: bool = False  # opt-in manifest/issue-label scope plus draft-only publication
    daily_cap: int = 2
    pr_only_repos: list[str] = field(default_factory=list)  # explicitly reviewed targets: no new issue for new work
    signoff: bool = False             # add a DCO Signed-off-by to every repo (an attestation: approve it yourself)
    signoff_repos: list[str] = field(default_factory=list)  # per-repo approval of the DCO sign-off, "owner/name"
    git_name: str = ""                # expected commit author/committer name; publishing stops if it differs
    git_email: str = ""               # expected commit author/committer email
    disclosure: bool | str = "auto"   # true | false | "auto": add the AI-assistance note only where the repo asks for it
    work_kind: str = "both"  # both | fix: fix-only runs do not search feature proposals
    min_repo_score: int = 60
    min_stars: int = 500
    languages: list[str] = field(default_factory=lambda: ["python"])
    preferred_repos: list[str] = field(default_factory=list)  # affinity, not permission or a quality guarantee
    skills: list[str] = field(default_factory=list)  # deterministic ranking hints only
    recent_issue_days: int = 7  # discover hides unchanged surfaced approvals temporarily; scout is unchanged
    discovery_read_budget_seconds: float = 120  # cooperative GitHub-read phase budget, not whole-run timeout
    discovery_strategy: str = "broad"  # loop: broad preserves original behavior; rotate opts into affinity
    labels: list[str] = field(default_factory=lambda: ["help wanted", "good first issue"])
    max_open_prs_per_repo: int = 1
    cooldown_days: int = 14           # after a PR to a repo is closed unmerged
    state_dir: str = ".state"
    work_dir: str = ".work"
    kill_file: str = ".state/STOP"    # create this file to halt all writes
    patch_command: str = ""           # optional: shell command that drafts a patch for an item JSON
    auto_submit: bool = False         # loop submits verified items itself when true
    spam_keywords: list[str] = field(
        default_factory=lambda: ["bounty", "airdrop", "giveaway", "eth)", "token", "rtc", "campaign", "reward pool"]
    )
    cache_ttl_seconds: int = 900
    own_orgs: list[str] = field(default_factory=list)  # orgs whose repos are yours, ignored by `bootstrap`
    github_user: str = ""             # your GitHub login. Required before any publish; must equal the gh login

    def signoff_for(self, repo: str) -> bool:
        return self.signoff or repo.lower() in {r.lower() for r in self.signoff_repos}

    @property
    def cap(self) -> int:
        return max(0, min(self.daily_cap, HARD_DAILY_MAX))

    def path(self, name: str) -> Path:
        return Path(self.state_dir) / name


def load(path: str | Path = "ossforge.yaml") -> Config:
    p = Path(path)
    if not p.is_file():
        return Config()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
