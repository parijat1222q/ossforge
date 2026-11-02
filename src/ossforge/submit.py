"""Submit a VERIFIED item: issue + PR together.

Every gate is re-checked at the write boundary. Progress is saved after each remote
step so a failure (e.g. issue created, PR failed) resumes instead of duplicating.
`execute=False` (default) only reports what would happen.
"""

from __future__ import annotations

import subprocess
import fcntl
from collections.abc import Callable
from pathlib import Path

from . import repo_access, duplicate_reviews, admission, evidence, contributing, dedupe, gh, identity, policy, policy_review, review, safety, vet, predictor
from .config import Config
from .limiter import DailyLimiter
from .queue import Item

DISCLOSURE = "\n\n---\n_This issue/PR was prepared with AI assistance and reviewed by a human before submission._"


class Refused(Exception):
    pass


def _run(cmd: list[str], cwd: str | None = None) -> str:
    if cmd[0] == "gh" and cmd[1:3] in (["issue", "create"], ["pr", "create"], ["pr", "comment"], ["repo", "fork"]):
        gh.pace_write()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        raise Refused(f"{' '.join(cmd[:3])}...: {p.stderr.strip()}")
    return p.stdout.strip()


def _base(cwd: str) -> str:
    return _run(["git", "merge-base", "HEAD", "origin/HEAD"], cwd)


def _identity_gate(cfg: Config, cwd: str, base: str, repo: str, fork: bool) -> None:
    problems = identity.check_login(cfg) + identity.check_worktree(cfg, cwd, base) + identity.check_upstream(cwd, repo)
    if fork:
        problems += identity.check_fork(cfg, cwd, repo)
    try:
        identity.require(problems)
    except identity.IdentityError as e:
        raise Refused(str(e)) from e


def _repo_access_gate(it: Item) -> None:
    if it.submitted.get("pr_url") or it.submitted.get("pr"):
        return
    try:
        reason = repo_access.pr_restriction(it.repo)
    except gh.GhError as exc:
        raise Refused(f"PR creation access unavailable: {exc}") from exc
    if reason:
        raise Refused(reason)


def _upstream_gate(it: Item) -> None:
    if it.submitted.get("pr_url"):
        return
    # Inspect the live default tip without fetching/resetting an active patch checkout.
    # A changed branch needs the operator to refresh/rebase/test, not an automatic rewrite.
    problems = identity.check_upstream(it.clone, it.repo)
    if problems:
        raise Refused("; ".join(problems))
    try:
        probe = subprocess.run(["git", "ls-remote", "origin", "HEAD"], cwd=it.clone,
                               capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("upstream default tip unavailable; not publishing") from exc
    if probe.returncode:
        raise Refused("upstream default tip unavailable; not publishing")
    output = probe.stdout.split()
    local = _run(["git", "rev-parse", "origin/HEAD"], it.clone)
    if len(output) != 2 or output[1] != "HEAD" or output[0] != local or _base(it.clone) != local:
        raise Refused("upstream default branch changed or could not be verified; fetch, review/rebase and re-run verification before publishing")


def _evidence_gate(it: Item) -> None:
    try:
        evidence.check(Path(it.clone), _base(it.clone), it.branch, it.verification)
    except evidence.StaleEvidence as e:
        raise Refused(str(e)) from e


def _admission_gate(it: Item, cfg: Config) -> dict:
    if not cfg.maintainer_admission:
        return {}
    base = _base(it.clone)
    paths = _run(["git", "diff", "--name-only", "--no-renames", f"{base}..HEAD"], it.clone).splitlines()
    numbers = _run(["git", "diff", "--numstat", "--no-renames", f"{base}..HEAD"], it.clone)
    try:
        return admission.check(it.repo, it.consent_issue, paths, numbers)
    except admission.Denied as e:
        raise Refused(f"maintainer admission: {e}") from e


def _ensure_signoff(cwd: str) -> None:
    base = _base(cwd)
    unsigned = _run(["git", "rev-list", "--grep=^Signed-off-by:", "--invert-grep", f"{base}..HEAD"], cwd)
    if unsigned:
        _run(["git", "rebase", "--signoff", base], cwd)


def _policy_files(repo: str) -> dict[str, str]:
    """Fail closed: if the repo's policy docs cannot be read, nothing is published."""
    try:
        return vet.policy_texts(repo, fresh=True)
    except gh.GhError as e:
        raise Refused(f"could not read {repo} policy docs ({e}); not publishing") from e


def _disclosure(cfg: Config, repo: str) -> tuple[bool, policy.Disclosure]:
    try:
        return policy.decide_disclosure(cfg.disclosure, _policy_files(repo))
    except policy.NeedsReview as e:
        raise Refused(f"needs your review: {e}") from e


def cla_status(repo: str, pr: int) -> str:
    """Read-only: link to the CLA bot's comment. Signing is always the contributor's own act."""
    for c in gh.api(f"repos/{repo}/issues/{pr}/comments", fresh=True) or []:
        if "cla" in c.get("body", "").lower():
            return c["html_url"]
    return ""


def _source_work_gate(it: Item) -> None:
    if it.submitted.get("pr_url"):
        return
    number = it.source_issue
    saved = it.submitted.get("issue_url", "")
    if not number and saved:
        prefix = f"https://github.com/{it.repo}/issues/"
        suffix = saved.removeprefix(prefix)
        if not saved.startswith(prefix) or not suffix.isdigit():
            raise Refused("saved issue URL is not an exact issue in the target repository")
        number = int(suffix)
    if not number:
        return
    try:
        source = gh.api(f"repos/{it.repo}/issues/{number}", fresh=True)
        if source.get("state") != "open" or "pull_request" in source:
            raise Refused("source issue is closed or is a pull request; reconsider the candidate")
        if source.get("html_url") != f"https://github.com/{it.repo}/issues/{number}":
            raise Refused("source issue URL does not match the target")
        comments = gh.api(f"repos/{it.repo}/issues/{number}/comments", per_page=100, fresh=True) or []
        timeline = gh.api(f"repos/{it.repo}/issues/{number}/timeline", per_page=100, fresh=True) or []
        refs = gh.search_issues(f"repo:{it.repo} is:pr #{number} in:body,title", per_page=100, fresh=True, require_complete=True)
        if any(len(rows) >= 100 for rows in (comments, timeline, refs)):
            raise Refused("source activity sample saturated; review complete activity before publication")
        if source.get("assignees") or any(vet.comment_claims_issue(c.get("body", "")) for c in comments):
            raise Refused("source issue assigned or claimed; review active work before publication")
        linked = [e.get("source", {}).get("issue", {}) for e in timeline if e.get("event") == "cross-referenced"]
        if any("pull_request" in r for r in refs + linked):
            raise Refused("source issue has a referenced PR (open/draft/closed); review competing or completed work")
    except gh.GhError as exc:
        raise Refused("source activity unavailable; cannot establish absence of competing work") from exc


def _duplicate_gate(it: Item, cfg: Config | None = None) -> None:
    if it.submitted.get("pr_url"):
        return
    try:
        changed = _run(["git", "diff", "--name-only", f"{_base(it.clone)}..HEAD"], it.clone).splitlines()
        paths = [p for p in changed if not predictor.is_test_path(p)]
        if not paths or len(paths) > 10:
            raise Refused("duplicate check needs 1..10 verified changed source paths; review larger changes separately")
        matches = {}
        query_results = {}  # fresh for this gate, never reused at another write boundary
        for path in paths:
            for match in dedupe.find_matches(it.repo, it.issue_title, it.issue_body, path, [], strict=True, query_results=query_results):
                matches[match.number] = match
        ms = list(matches.values())
        own_url = it.submitted.get("issue_url", "")
        # Only the exact saved target URL may be excluded when resuming our own issue.
        ms = [m for m in ms if m.number != it.source_issue and not (
            own_url == f"https://github.com/{it.repo}/issues/{m.number}" and m.url == own_url and not m.is_pr)]
        if cfg is not None:
            ms = [m for m in ms if not duplicate_reviews.unchanged(cfg.path("duplicate_reviews.json"), it, m.number)]
        if dedupe.is_duplicate(ms):
            raise Refused("duplicate found: " + ", ".join(f"#{m.number}({m.state})" for m in ms[:3]))
        for path in paths:
            recent = dedupe.recent_commit_hits(it.repo, path, sorted(dedupe.tokens(it.issue_title))[:6], strict=True)
            if cfg is not None:
                recent = [url for url in recent if not duplicate_reviews.unchanged_commit(
                    cfg.path("duplicate_reviews.json"), it, url)]
            if recent:
                raise Refused(f"recent commit already touches this: {recent[0]}")
    except (dedupe.Incomplete, gh.GhError, ValueError) as exc:
        raise Refused(str(exc)) from exc


def preflight(it: Item, limiter: DailyLimiter, cfg: Config, history: list[dict]) -> None:
    try:
        safety.kill_switch(cfg)
    except safety.Blocked as e:
        raise Refused(str(e)) from e
    if it.status != "verified":
        raise Refused(f"status is {it.status!r}, must be 'verified'")
    if not (it.issue_title and it.issue_body and it.pr_title and it.pr_body and it.branch and it.clone):
        raise Refused("missing issue/PR text, branch or clone path")
    if limiter.remaining() <= 0 and not it.submitted.get("pr_url"):
        raise Refused("daily PR cap reached")
    why = safety.repo_allowed(cfg, it.repo, history)
    if why and not it.submitted.get("issue_url"):
        raise Refused(why)
    _repo_access_gate(it)
    _upstream_gate(it)
    _source_work_gate(it)
    files = _policy_files(it.repo)
    pol = policy.scan_files(files)
    if pol.anti_ai:
        raise Refused(f"anti-AI policy in {pol.source}")
    why = policy_review.check(cfg, it.repo)
    if why:
        raise Refused(why)
    _disclosure(cfg, it.repo)  # surfaces an unclear or conflicting disclosure policy in the dry run
    _duplicate_gate(it, cfg)
    cwd = str(Path(it.clone))
    base = _base(cwd)
    _identity_gate(cfg, cwd, base, it.repo, fork=False)
    _evidence_gate(it)
    _admission_gate(it, cfg)
    changed = _run(["git", "diff", "--name-only", f"{base}..HEAD"], cwd).splitlines()
    diff = _run(["git", "diff", f"{base}..HEAD"], cwd)
    bad = safety.check_diff(changed, diff)
    if bad:
        raise Refused("diff blocked: " + "; ".join(bad))
    found = review.review_branch(it.repo, cwd, base, it.pr_body, _run, gh)
    if review.blocking(found):
        raise Refused("self-review: " + "; ".join(review.blocking(found)))
    it.notes = [n for n in it.notes if not n.startswith("review: ")] + [f"review: {f.msg}" for f in found if f.level == "warn"]
    conv = contributing.parse(next((t for k, t in files.items() if "CONTRIBUTING" in k), ""))
    if conv.requires_assignment:
        raise Refused("repo requires assignment first: " + "; ".join(conv.notes))
    msg = _run(["git", "log", "-1", "--format=%B"], cwd)
    signed = bool(_run(["git", "rev-list", "--grep=^Signed-off-by:", f"{base}..HEAD"], cwd))
    if cfg.signoff_for(it.repo):
        pass  # sign-off approved for this repo; it is added right before the push
    elif signed:
        raise Refused("a commit carries Signed-off-by but sign-off is not approved for this repo; "
                      f"add {it.repo!r} to signoff_repos after reading its DCO, or remove the trailer")
    elif conv.dco:
        raise Refused(f"{it.repo} asks for a DCO Signed-off-by. That is your personal attestation: "
                      f"read the DCO, then add {it.repo!r} to signoff_repos to approve it for this repo")
    problems = [p for p in contributing.commit_message_ok(msg, conv) if "Signed-off-by" not in p]
    if problems:
        raise Refused("commit: " + "; ".join(problems))


def submit(it: Item, limiter: DailyLimiter, cfg: Config, audit: safety.Audit, execute: bool = False,
           save: Callable[[dict], None] | None = None) -> dict:
    """Serialize same-state writes and recover progress before checking or writing."""
    if not execute:
        return _submit(it, limiter, cfg, audit, execute, save)
    from .queue import Queue
    cfg.path("publication.lock").parent.mkdir(parents=True, exist_ok=True)
    with cfg.path("publication.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("another publication holds this state directory; retry after it finishes") from exc
        try:
            q = Queue(cfg.path("queue.json"))
            stored = q.items.get(it.id)
            if stored:
                it.submitted = dict(stored.submitted)
                if stored.status == "submitted":
                    it.status = "verified"  # recheck gates, then return saved URLs without new writes
            def persist(progress):
                it.submitted = dict(progress)
                # Reload to preserve unrelated queue updates; the publication lock owns this item's writes.
                current = Queue(cfg.path("queue.json"))
                if it.id in current.items:
                    current.set(it.id, submitted=dict(progress))
                    current.save()
                if save:
                    save(progress)
            return _submit(it, limiter, cfg, audit, execute, persist)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _submit(it: Item, limiter: DailyLimiter, cfg: Config, audit: safety.Audit, execute: bool = False,
            save: Callable[[dict], None] | None = None) -> dict:
    try:
        history = safety.effective_history(audit.read())
    except safety.Blocked as exc:
        raise Refused(str(exc)) from exc
    preflight(it, limiter, cfg, history)
    plan = {"repo": it.repo, "issue": it.issue_title, "pr": it.pr_title, "branch": it.branch,
            "resume": bool(it.submitted.get("issue_url")),
            "disclosure": "add" if _disclosure(cfg, it.repo)[0] else "no"}
    plan["maintainer_admission"] = _admission_gate(it, cfg)
    if not execute:
        return {"dry_run": True, **plan}

    prog = dict(it.submitted)
    add_note, disc = _disclosure(cfg, it.repo)
    note = DISCLOSURE if add_note else ""
    cwd = str(Path(it.clone))
    _identity_gate(cfg, cwd, _base(cwd), it.repo, fork=False)  # re-check at the write boundary
    _evidence_gate(it)  # exact tested content again immediately before the first external write
    _admission_gate(it, cfg)  # current upstream consent, never cached
    # A PR-only resume makes no issue/fork write here. Keep its fresh checks at
    # the PR write boundary below, instead of repeating a no-effect checkpoint.
    _repo_access_gate(it)
    if not (prog.get("issue_url") and prog.get("fork_pushed")):
        _upstream_gate(it)
        _source_work_gate(it)
        _duplicate_gate(it, cfg)
    me = gh.api("user", fresh=True)["login"]

    def progress(**kw) -> None:
        prog.update(kw)
        if save:
            save(dict(prog))

    if not prog.get("issue_url") and it.source_issue:
        source = gh.api(f"repos/{it.repo}/issues/{it.source_issue}", fresh=True)
        if source.get("state") != "open" or "pull_request" in source:
            raise Refused("source issue changed before publication; reconsider it")
        url = source.get("html_url")
        if url != f"https://github.com/{it.repo}/issues/{it.source_issue}":
            raise Refused("source issue URL does not match the target")
        progress(issue_url=url, existing_issue=True)
    if not prog.get("issue_url"):
        url = _run(["gh", "issue", "create", "-R", it.repo, "-t", it.issue_title, "-b", it.issue_body + note])
        progress(issue_url=url)
        audit.log("issue_opened", repo=it.repo, url=url, item=it.id)
    num = prog["issue_url"].rsplit("/", 1)[-1]

    if not prog.get("fork_pushed"):
        _repo_access_gate(it)
        gh.pace_write()
        subprocess.run(["gh", "repo", "fork", it.repo, "--clone=false"], capture_output=True)
        fork = f"https://github.com/{me}/{it.repo.split('/')[1]}.git"
        have = subprocess.run(["git", "remote"], cwd=cwd, capture_output=True, text=True).stdout.split()
        _run(["git", "remote", "set-url" if "fork" in have else "add", "fork", fork], cwd)
        if cfg.signoff_for(it.repo):
            _ensure_signoff(cwd)
        _identity_gate(cfg, cwd, _base(cwd), it.repo, fork=True)  # the destination is the operator's own fork
        _admission_gate(it, cfg)
        _evidence_gate(it)  # signoff may rewrite commits, but must not change the tested tree
        _run(["git", "push", "-u", "fork", it.branch], cwd)
        progress(fork_pushed=True)

    if not prog.get("pr_url"):
        _repo_access_gate(it)
        if limiter.remaining() <= 0:
            raise Refused("daily PR cap reached; existing issue/fork progress retained for a later day")
        _upstream_gate(it)
        _source_work_gate(it)
        _duplicate_gate(it, cfg)
        body = f"{it.pr_body}\n\nCloses #{num}{note}"
        _admission_gate(it, cfg)
        draft = ["--draft"] if cfg.maintainer_admission else []
        url = _run(["gh", "pr", "create", *draft, "-R", it.repo, "--head", f"{me}:{it.branch}", "-t", it.pr_title, "-b", body], cwd)
        progress(pr_url=url)
        limiter.record()
        audit.log("pr_opened", repo=it.repo, pr=int(url.rsplit("/", 1)[-1]), url=url, item=it.id, state="open")
    return {"dry_run": False, "issue": prog["issue_url"], "pr": prog["pr_url"],
            "disclosure_added": add_note, "disclosure_policy": disc.status, "disclosure_source": disc.source}
