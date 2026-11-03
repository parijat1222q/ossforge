"""Post an approved critique comment on someone else's PR. Gated; never automatic."""

from __future__ import annotations

import subprocess

from . import gh, identity, policy, policy_review, safety, vet
from .config import Config

NOTE = "\n\n_This review was prepared with AI assistance._"


class Refused(Exception):
    pass


def post(cfg: Config, audit: safety.Audit, repo: str, pr: int, body: str, execute: bool = False) -> str:
    try:
        safety.kill_switch(cfg)
    except safety.Blocked as e:
        raise Refused(str(e)) from e
    bad = identity.check_login(cfg)
    if bad:
        raise Refused("identity check failed: " + "; ".join(bad) + ". Nothing was posted.")
    if any(r.get("event") == "critique_posted" and r["repo"] == repo and r["pr"] == pr for r in audit.read()):
        raise Refused("already critiqued this PR once; a second comment is noise")
    try:
        files = vet.policy_texts(repo)
    except gh.GhError as e:
        raise Refused(f"could not read policy docs ({e}); not posting") from e
    why = policy_review.check(cfg, repo)
    if why:
        raise Refused(why)
    pol = policy.scan_files(files)
    if pol.anti_ai:
        raise Refused(f"anti-AI policy in {pol.source}")
    if "<describe" in body or "<" in body and ">" in body and "placeholder" in body.lower():
        raise Refused("draft still contains placeholders")
    try:
        add_note, _ = policy.decide_disclosure(cfg.disclosure, files)
    except policy.NeedsReview as e:
        raise Refused(f"needs your review: {e}") from e
    text = body + (NOTE if add_note else "")
    if not execute:
        return "dry-run:\n" + text
    gh.pace_write()
    p = subprocess.run(["gh", "pr", "comment", str(pr), "-R", repo, "-b", text], capture_output=True, text=True)
    if p.returncode:
        raise Refused(p.stderr.strip())
    audit.log("critique_posted", repo=repo, pr=pr)
    return p.stdout.strip()
