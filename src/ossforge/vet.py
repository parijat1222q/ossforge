"""Issue vetting: is this issue free, clear, and in a repo that accepts outside PRs?

Ported in spirit from oss-scout issue-eligibility/issue-vetting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import contributing, gh, policy, repo_access

CLAIM_RE = re.compile(
    r"(i'?m (working|taking)|i'?ll (take|work|do|fix|submit)|working on (this|it)|"
    r"can i (take|work|have)|assign (this )?to me|claiming|i'?ve started|"
    r"i(?: have|'ve) (?:a |the )?(?:working )?(?:fix|patch|solution) (?:ready|on my fork)|"
    r"i(?: would|'d) like to (?:work on|take\b)|i have started (?:working|implementing|fixing)|"
    r"i (?:have|posted|pushed) a (?:working )?fix on my fork)",
    re.I,
)


def comment_claims_issue(body: str) -> bool:
    text = (body or "").replace("’", "'")
    if CLAIM_RE.search(text):
        return True
    # First-person completed work plus a concrete fork destination. Suggestions
    # and unprepared plans are not enough to infer an existing implementation.
    return bool(re.search(
        r"\bi(?: have|'ve)? (?:"
        r"(?:prepared|pushed) (?:a |this |the )?(?:fix|patch)\b[^.!?\n]{0,100}\b(?:to|on) my fork|"
        r"have (?:this |the |a )(?:fix|patch) pushed to my fork)", text, re.I))


def resolution_evidence(comments: list[dict]) -> str:
    """Conservative triage hint, not proof that a claimed fix is correct or shipped."""
    # A later/current failure report makes automatic resolution triage ambiguous.
    texts = [re.sub(r"```.*?```", "", c.get("body") or "", flags=re.S).lower() for c in comments]
    if any(re.search(r"(?:still|again) (?:fails?|broken|reproduc|happens)|not (?:yet )?(?:fixed|resolved|merged)", t)
           for t in texts):
        return ""
    for comment, text in zip(comments, texts):
        if comment.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}:
            continue
        for sentence in re.split(r"\.(?:\s|$)|\n", text):
            match = re.search(r"\b(?:was|is|has been) (?:already )?(?:fixed|resolved|merged)\b", sentence)
            if not match or re.search(r"\b(?:not|never|will|would|could|should|if|once)\b", sentence[:match.end()]):
                continue
            if re.search(r"(?:#\d+|github\.com/[^ /]+/[^ /]+/(?:pull|commit)/[a-z0-9]+)", sentence):
                return "maintainer-associated comment reports resolved work; inspect referenced fix/current source before reconsidering"
    return ""


def clear_requirements(body: str) -> bool:
    b = body or ""
    return len(b) >= 80 and bool(
        re.search(r"(expected|actual|steps|reproduc|traceback|error|should|propos|feature)", b, re.I)
    )


@dataclass
class Verdict:
    issue: str
    decision: str  # "approve" | "skip" | "anti_ai"
    reasons: list[str] = field(default_factory=list)


def decide(
    *,
    assigned: bool,
    claimed: bool,
    open_pr_count: int,
    clear: bool,
    has_contributing: bool,
    anti_ai: policy.PolicyResult,
    repo_ok: bool,
    needs_assignment: bool = False,
) -> tuple[str, list[str]]:
    """Pure decision. Anti-AI always wins; every skip reason is reported."""
    if anti_ai.anti_ai:
        return "anti_ai", [f"anti-AI policy in {anti_ai.source}: {', '.join(anti_ai.matched[:2])}"]
    skip = []
    if assigned:
        skip.append("assigned")
    if claimed:
        skip.append("claimed in comments")
    if open_pr_count:
        skip.append(f"{open_pr_count} open PR(s) reference it")
    if not clear:
        skip.append("requirements unclear")
    if not repo_ok:
        skip.append("repo health too low")
    if needs_assignment:
        skip.append("repo requires assignment before a PR (comment on the issue first)")
    if skip:
        return "skip", skip
    reasons = ["unassigned, unclaimed, no competing PR"]
    if not has_contributing:
        reasons.append("no CONTRIBUTING.md (proceed carefully)")
    return "approve", reasons


def policy_texts(repo: str, *, fresh: bool = False) -> dict[str, str]:
    """Policy/contributing docs that exist, fetched after one listing per folder (not 9 blind probes)."""
    out = {}
    paths = gh.existing_files(repo, policy.POLICY_FILES, fresh=True, strict=True) if fresh else gh.existing_files(repo, policy.POLICY_FILES)
    for p in paths:
        t = gh.file_text(repo, p, fresh=True) if fresh else gh.file_text(repo, p)
        if fresh and t is None:
            raise gh.GhError("listed policy file could not be read")
        if t:
            out[p] = t
    return out


def vet_issue(repo: str, number: int, repo_ok: bool = True) -> Verdict:
    try:
        restricted = repo_access.pr_restriction(repo)
    except gh.GhError as exc:
        return Verdict(f"{repo}#{number}", "skip", [f"PR creation access unavailable: {exc}"])
    if restricted:
        return Verdict(f"{repo}#{number}", "skip", [restricted])
    issue = gh.api(f"repos/{repo}/issues/{number}")
    comments = gh.api(f"repos/{repo}/issues/{number}/comments", per_page=100) or []
    if len(comments) >= 100 or issue.get("comments", 0) > len(comments):
        return Verdict(f"{repo}#{number}", "skip", ["comment coverage incomplete; review remaining discussion before coding"])
    resolved = resolution_evidence(comments)
    if resolved:
        return Verdict(f"{repo}#{number}", "skip", [resolved])
    claimed = any(comment_claims_issue(c.get("body", "")) for c in comments)
    try:
        prs = gh.search_issues(f"repo:{repo} is:pr #{number} in:body,title", per_page=100, require_complete=True)
    except gh.RateLimited:
        raise
    except gh.GhError as exc:
        return Verdict(f"{repo}#{number}", "skip", [f"referenced PR coverage unavailable; review activity before coding: {exc}"])
    if any(pr.get("state") != "open" for pr in prs):
        return Verdict(f"{repo}#{number}", "skip", ["closed or merged PR reference requires review of implementation and current source before coding"])
    files = policy_texts(repo)
    pol = policy.scan_files(files)
    code, reasons = decide(
        assigned=bool(issue.get("assignees")),
        claimed=claimed,
        open_pr_count=len(prs),
        clear=clear_requirements(issue.get("body") or ""),
        has_contributing=any("CONTRIBUTING" in k for k in files),
        anti_ai=pol,
        repo_ok=repo_ok,
        needs_assignment=contributing.parse(
            next((t for k, t in files.items() if "CONTRIBUTING" in k), "")
        ).requires_assignment,
    )
    return Verdict(f"{repo}#{number}", code, reasons)
