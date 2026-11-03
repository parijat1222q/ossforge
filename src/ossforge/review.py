"""Self-review of your own branch before it leaves the machine.

Maintainers reject PRs for ordinary reasons: no test, stray debug code, unrelated churn, the PR
template ignored, a changelog the repo expects. This checks those from the diff and the repo's own
files. ``warn`` findings are shown for a human to judge; an ``error`` level exists for hard gates and blocks `submit`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import predictor

DEBUG_RES = (
    (re.compile(r"^\+\s*breakpoint\(\)"), "breakpoint() left in"),
    (re.compile(r"^\+\s*(?:import pdb|pdb\.set_trace\(\))"), "pdb left in"),
    (re.compile(r"^\+\s*console\.(?:log|debug)\("), "console.log left in"),
    (re.compile(r"^\+\s*(?:#|//)\s*(?:TODO|FIXME|XXX|HACK)\b"), "new TODO/FIXME comment"),
    (re.compile(r"^\+\s*print\((?:f?['\"](?:DEBUG|here|HERE|xxx))"), "debug print left in"),
)
CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".c", ".cc", ".cpp", ".h", ".cs", ".php")
CHANGELOG_NAMES = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "NEWS.md", "CHANGELOG.rst", "CHANGES.rst")
NEWS_DIRS = ("changes/", "changelog.d/", "newsfragments/", "news/", "docs/changes/")


@dataclass
class Finding:
    level: str  # error | warn
    msg: str


def template_headings(template: str) -> list[str]:
    return [m.group(1).strip().lower() for m in re.finditer(r"^#{1,4}\s+(.+?)\s*$", template or "", re.M)]


def review_diff(
    changed: list[str],
    diff: str,
    *,
    pr_body: str = "",
    pr_template: str = "",
    repo_files: set[str] | None = None,
    whitespace_only: set[str] | None = None,
    max_files: int = 6,
) -> list[Finding]:
    out: list[Finding] = []
    tests = [f for f in changed if predictor.is_test_path(f)]
    src = [f for f in changed if not predictor.is_test_path(f) and f.lower().endswith(CODE_EXTS)]
    if src and not tests:
        # a warning, not a block: docs/config/refactors, or code already covered by existing tests, are legitimate.
        # `verify` is what proves a fix with a test that fails before and passes after.
        out.append(Finding("warn", "code changed but no test file changed: fine only if existing tests already "
                                   "cover it (refactor) or it is not behaviour; otherwise add one"))
    for ln in diff.splitlines():
        if ln.startswith("+++"):
            continue
        for rx, msg in DEBUG_RES:
            if rx.search(ln):
                f = Finding("warn", f"{msg}: {ln[1:].strip()[:70]}")
                if f not in out:
                    out.append(f)
    if whitespace_only:
        out.append(Finding("warn", "whitespace-only changes in: " + ", ".join(sorted(whitespace_only)[:4])))
    if len(changed) > max_files:
        out.append(Finding("warn", f"{len(changed)} files changed; a focused PR is easier to merge"))
    heads = template_headings(pr_template)
    if heads:
        body = (pr_body or "").lower()
        missing = [h for h in heads if h not in body]
        if missing:
            out.append(Finding("warn", "PR body is missing template sections: " + ", ".join(missing[:5])))
    if repo_files:
        has_cl = any(f in repo_files for f in CHANGELOG_NAMES) or any(any(r.startswith(d) for r in repo_files) for d in NEWS_DIRS)
        touched = any(f in CHANGELOG_NAMES or any(f.startswith(d) for d in NEWS_DIRS) for f in changed)
        if has_cl and src and not touched:
            out.append(Finding("warn", "repo keeps a changelog or news fragments; none touched"))
    return out


def blocking(findings: list[Finding]) -> list[str]:
    return [f.msg for f in findings if f.level == "error"]


PR_TEMPLATE_PATHS = (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE", "PULL_REQUEST_TEMPLATE")


def review_branch(repo: str, clone: str, base: str, pr_body: str, run, gh_mod) -> list[Finding]:
    """Gather the inputs from a local clone plus the repo's own files, then run review_diff."""
    changed = run(["git", "diff", "--name-only", f"{base}..HEAD"], clone).splitlines()
    diff = run(["git", "diff", f"{base}..HEAD"], clone)
    all_changed = set(changed)
    nows = set(run(["git", "diff", "-w", "--name-only", f"{base}..HEAD"], clone).splitlines())
    whitespace_only = all_changed - nows
    template = ""
    for p in PR_TEMPLATE_PATHS:
        t = gh_mod.file_text(repo, p)
        if t:
            template = t
            break
    present = set(gh_mod.existing_files(repo, CHANGELOG_NAMES))
    return review_diff(changed, diff, pr_body=pr_body, pr_template=template, repo_files=present,
                       whitespace_only=whitespace_only)
