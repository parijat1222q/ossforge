"""Source-backed policy review that must exist before anything is published to a third-party repo.

`snapshot` reads the repo's policy-bearing docs (fixed list of locations, read-only) and keeps their URLs and blob
SHAs. The owner reads them and records the review with a note on any restrictions. `check` then requires, at
submit time, a record whose doc SHAs still match. A changed doc, a missing record or a record marked prohibited
stops the submission. Missing or empty scan results mean "review needed", never "no policy": the scan covers a
fixed list of files and cannot see rules kept elsewhere (project docs sites, wikis, linked pages).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from . import gh, policy, contribution_base
from .config import Config


def snapshot(repo: str, extra_paths: list[str] | None = None, ref: str | None = None) -> dict:
    if ref is not None:
        contribution_base.branch(ref)
        tip = gh.api(contribution_base.commit_path(repo, ref), fresh=True)
        if not isinstance(tip, dict) or not contribution_base.SHA.fullmatch(str(tip.get('sha', ''))):
            raise ValueError('contribution branch tip unavailable')
        selected = {'repo': repo, 'branch': ref, 'revision': tip['sha']}
    else:
        selected = None
    kwargs = {'ref': selected['revision']} if selected else {}
    docs = []
    extra_paths = extra_paths or []
    for path in extra_paths:
        parts = path.split("/")
        if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts) or any(c in path for c in "*?[\\"):
            raise ValueError("extra policy path must be an exact repository-relative file")
    paths = gh.existing_files(repo, policy.POLICY_FILES, fresh=True, strict=True, **kwargs)
    for p in sorted(set(paths + extra_paths)):
        d = gh.api(f"repos/{repo}/contents/{p}", fresh=True, **kwargs)
        if not isinstance(d, dict) or d.get("encoding") != "base64" or not d.get("sha") or not d.get("html_url"):
            raise gh.GhError(f"listed policy document could not be read: {p}")
        text = base64.b64decode(d["content"]).decode("utf-8", "replace")
        docs.append({"path": p, "url": d["html_url"], "sha": d["sha"], "text": text})
    sc = policy.scan_files({d["path"]: d["text"] for d in docs})
    return {**({"ref": ref, "contribution_base": selected} if selected else {}), "repo": repo, "docs": docs, "scanned_locations": list(policy.POLICY_FILES), "anti_ai": sc.anti_ai, "agent_review": sc.agent_review,
            "extra_paths": extra_paths, "ai_policy_docs": policy.ai_policy_docs({d["path"]: d["text"] for d in docs})}


def _file(cfg: Config):
    return cfg.path("policy_reviews.json")


def _load(cfg: Config) -> dict:
    try:
        return json.loads(_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(cfg: Config, repo: str, snap: dict, note: str, prohibited: bool = False,
           publication_route: str | None = None, local_preparation: str = "unresolved", contribution_branch: str | None = None, external_regression_doc: str | None = None) -> dict:
    if contribution_branch is not None:
        contribution_base.branch(contribution_branch)
        if snap.get("ref") != contribution_branch or not note.strip():
            raise ValueError("contribution branch needs a matching source-ref snapshot and review note")
        selected = contribution_base.validate(snap.get("contribution_base"), repo, explicit=True)
        if selected["branch"] != contribution_branch:
            raise ValueError("contribution branch identity mismatch")
    elif "ref" in snap:
        raise ValueError("explicit policy ref needs an explicit contribution branch")
    if local_preparation not in {"allowed", "forbidden", "unresolved"}:
        raise ValueError("unsupported local preparation scope")
    if local_preparation != "unresolved" and not note.strip():
        raise ValueError("local preparation decision needs a source-backed note")
    if publication_route not in (None, "issue_pr", "pr_only", "issue_first", "unresolved"):
        raise ValueError("unsupported publication route")
    if publication_route is not None and not note.strip():
        raise ValueError("publication route needs a source-backed review note")
    rec = {"repo": repo, "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": note,
           "prohibited": prohibited, "docs": [{k: d[k] for k in ("path", "url", "sha")} for d in snap["docs"]],
           "scanned_locations": snap["scanned_locations"], "extra_paths": snap.get("extra_paths", [])}
    if contribution_branch is not None:
        rec["contribution_base"] = selected
    rec["local_preparation"] = local_preparation
    if publication_route is not None:
        rec["publication_route"] = publication_route
    if external_regression_doc is not None:
        matches = [d for d in snap['docs'] if d['path'] == external_regression_doc]
        if not matches or not note.strip() or prohibited:
            raise ValueError('external regression review needs a current cited policy document and reason')
        rec['external_regression'] = {'path': external_regression_doc, 'sha': matches[0]['sha'], 'reason': note}
    allr = _load(cfg)
    allr[repo.lower()] = rec
    _file(cfg).parent.mkdir(parents=True, exist_ok=True)
    _file(cfg).write_text(json.dumps(allr, indent=1), encoding="utf-8")
    return rec


def check(cfg: Config, repo: str, snap: dict | None = None) -> str:
    """Empty string when a current, non-prohibiting review exists; otherwise the reason it does not."""
    rec = _load(cfg).get(repo.lower())
    if not rec:
        return (f"no policy review recorded for {repo}. Run `ossforge policy-review {repo}`, read the documents it "
                "prints (and the project's own contribution pages, which the scan cannot see), then record the review")
    if rec.get("prohibited"):
        return f"policy review for {repo} is recorded as prohibited: {rec.get('note', '')}"
    branch = reviewed_branch(rec)
    kwargs = {"ref": branch} if branch is not None else {}
    snap = snap or (snapshot(repo, extra_paths=rec["extra_paths"], **kwargs) if rec.get("extra_paths") else snapshot(repo, **kwargs))
    if snap.get("contribution_base") != rec.get("contribution_base") or snap.get("ref") != branch:
        return "policy review contribution branch changed; inspect selected source and review again"
    now = {d["path"]: d["sha"] for d in snap["docs"]}
    was = {d["path"]: d["sha"] for d in rec["docs"]}
    if now != was:
        changed = sorted(set(now) ^ set(was) | {p for p in now if p in was and now[p] != was[p]})
        return f"policy documents changed since the review ({', '.join(changed)}); review {repo} again"
    return ""


def publication_route(cfg: Config, repo: str) -> str | None:
    """A reviewed policy decision, not instructions or consent extracted from prose."""
    rec = _load(cfg).get(repo.lower()) or {}
    route = rec.get("publication_route")
    if route is None:
        return None
    why = check(cfg, repo)
    if why:
        raise ValueError(why)
    if route not in {"issue_pr", "pr_only", "issue_first", "unresolved"}:
        raise ValueError("publication route invalid; review policy again")
    if route in {"issue_first", "unresolved"}:
        raise ValueError(f"publication route {route}: review issue-first prerequisites or unresolved rules before submission")
    return route


def preparation_review(cfg: Config, repo: str, snap: dict) -> dict | None:
    """Current source-scoped decision, never an exception to public policy.

    A license can permit independent modification without a community policy
    explicitly discussing local development. The review must explain that scope.
    """
    rec = _load(cfg).get(repo.lower())
    if not rec or rec.get('local_preparation') != 'allowed' or not rec.get('note', '').strip():
        return None
    if snap.get('contribution_base') != rec.get('contribution_base'):
        return None
    if not snap.get('docs') or {d['path']: d['sha'] for d in snap['docs']} != {d['path']: d['sha'] for d in rec['docs']}:
        return None
    return rec


def reviewed_branch(record: dict) -> str | None:
    if 'contribution_base' not in record:
        return None
    return contribution_base.validate(record['contribution_base'], record.get('repo'), explicit=True)['branch']


def current_snapshot(cfg: Config, repo: str) -> dict:
    rec = _load(cfg).get(repo.lower(), {})
    name = reviewed_branch(rec)
    kwargs = {"ref": name} if name is not None else {}
    extras = rec.get("extra_paths", [])
    return snapshot(repo, extra_paths=extras, **kwargs) if extras else snapshot(repo, **kwargs)
