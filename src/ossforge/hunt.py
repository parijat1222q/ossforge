"""Whole-repo hunt: clone, honour policy, scan (builtin + ruff + JS), rank, drop duplicates, enqueue."""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

from . import audit as audit_mod
from . import dedupe, external, identity, policy
from .config import Config
from .outcomes import Stats, rank
from .queue import Item, Queue, item_id

TOP_PER_REPO = 8


def clone(cfg: Config, repo: str, *, contribution_base: dict | None = None) -> Path:
    from . import contribution_base as cb
    selected = cb.validate(contribution_base, repo) if contribution_base is not None else {}
    dest = Path(cfg.work_dir) / repo.replace("/", "__")
    if not dest.exists():
        try:
            subprocess.run(["git", "clone", "--depth", "50", *(["--branch", selected["branch"], "--single-branch"] if selected else []), "--", f"https://github.com/{repo}", str(dest)],
                           check=True, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            # Only this attempt created the destination. Never reuse its partial checkout.
            if dest.exists():
                shutil.rmtree(dest)
            raise RuntimeError("clone timed out after 300 seconds; partial checkout removed, retry later") from exc
        identity.apply_local(cfg, str(dest))  # fresh clones commit as the owner from the start (local config only)
    else:
        # Existing paths can be interrupted clones, unrelated repositories, or
        # directories inside a parent repository. Never scan them as this target.
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=dest,
                               capture_output=True, text=True, timeout=30)
        if probe.returncode or Path(probe.stdout.strip()).resolve() != dest.resolve():
            raise RuntimeError("existing checkout is not the target Git root; preserve it and inspect before retry")
        index = subprocess.run(["git", "rev-parse", "--git-path", "index"], cwd=dest,
                               capture_output=True, text=True, timeout=30)
        index_path = Path(index.stdout.strip())
        if not index_path.is_absolute():
            index_path = dest / index_path
        if index.returncode or not index.stdout.strip() or not index_path.is_file():
            raise RuntimeError("existing checkout index missing; preserve incomplete checkout and inspect before retry")
        changes = subprocess.run(["git", "status", "--porcelain"], cwd=dest,
                                 capture_output=True, text=True, timeout=30)
        if changes.returncode or changes.stdout.strip():
            raise RuntimeError("existing checkout is dirty or incomplete; preserve it and inspect before retry")
        problems = identity.check_upstream(str(dest), repo)
        if problems:
            raise RuntimeError("; ".join(problems) + "; existing checkout preserved")
    if selected:
        from . import submit
        probe = Item("clone", repo, "", 0, "", "", clone=str(dest), contribution_base=selected)
        submit._item_base(probe)
        if submit._run(["git", "rev-parse", "HEAD"], str(dest)) != selected["revision"]:
            raise RuntimeError("existing checkout differs from pinned contribution base; preserve it")
    from .preparation import public_guard
    public_guard(dest)
    return dest


def policy_of(dest: Path) -> policy.PolicyResult:
    files = {p.relative_to(dest).as_posix(): p.read_text(encoding="utf-8", errors="replace")
             for pattern in policy.POLICY_FILES for p in dest.glob(pattern)
             if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(dest.resolve())}
    return policy.scan_files(files)


def snippet(dest: Path, path: str, line: int, radius: int = 4) -> str:
    try:
        lines = (dest / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lo, hi = max(0, line - 1 - radius), min(len(lines), line + radius)
    return "\n".join(f"{n + 1:5d}{'>' if n + 1 == line else ' '} {lines[n]}" for n in range(lo, hi))


def hunt(cfg: Config, repo: str, q: Queue, stats: Stats, work: str = "fix") -> dict:
    dest = clone(cfg, repo)
    pol = policy_of(dest)
    if pol.anti_ai:
        return {"repo": repo, "anti_ai": pol.source, "clean": 0, "duplicate": 0}
    if pol.agent_review:
        return {"repo": repo, "policy_review_needed": pol.source, "clean": 0, "duplicate": 0}
    found = audit_mod.scan_tree(dest) + external.ruff_findings(dest) + external.js_findings(dest)
    uniq = {(f.path, f.line, f.kind): f for f in found}
    ranked = rank(list(uniq.values()), stats)[:TOP_PER_REPO * 3]
    clean = dup = 0
    for score, f in ranked:
        if clean >= TOP_PER_REPO:
            break
        it = Item(item_id(repo, f.path, f.kind, f.line), repo, f.path, f.line, f.kind, f.detail,
                  clone=str(dest), work=work, rank=score, snippet=snippet(dest, f.path, f.line))
        if it.id in q.items:
            continue
        ms = dedupe.find_matches(repo, f"{f.kind} {f.path}", f.detail, f.path, [f.kind.replace("-", " ")])
        hits = dedupe.recent_commit_hits(repo, f.path, sorted(dedupe.tokens(f.detail))[:6])
        if dedupe.is_duplicate(ms) or hits:
            it.status = "duplicate"
            it.notes = [f"#{m.number} ({m.state}) {m.title} [{m.reason}, {m.score}]" for m in ms[:3]] + hits[:1]
            dup += 1
        else:
            it.status = "clean"
            clean += 1
        q.add(it)
    q.save()
    return {"repo": repo, "anti_ai": None, "clean": clean, "duplicate": dup}
