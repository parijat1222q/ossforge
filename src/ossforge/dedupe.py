"""Duplicate guard: search ALL issues and PRs (open and closed) before filing anything."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import gh


class Incomplete(RuntimeError):
    """Publication evidence could not be established, not an empty result."""


STOP = {"the", "a", "an", "in", "of", "to", "is", "for", "and", "or", "on", "with", "when", "not", "be", "fix", "bug", "py"}


def tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    out: set[str] = set()
    for w in words:
        out.update(p.lower() for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", w) if len(p) > 2)
        out.add(w.lower())
    return out - STOP


def evidence_terms(title: str, body: str) -> list[str]:
    """Prefer title keywords, then meaningful body words when the title has none."""
    return sorted(tokens(title))[:6] or sorted(tokens(body), key=lambda t: (-len(t), t))[:6]


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


@dataclass
class Match:
    number: int
    title: str
    url: str
    state: str
    is_pr: bool
    score: float
    reason: str


def find_matches(repo: str, title: str, body: str, path: str, symbols: list[str], threshold: float = 0.25, *, strict: bool = False, query_results: dict | None = None) -> list[Match]:
    """Searches by file path, symbols, and title keywords; scores each hit locally."""
    terms = evidence_terms(title, body)
    meaningful_symbols = [s for s in symbols[:3] if tokens(s)]
    if strict and not terms and not meaningful_symbols:
        raise Incomplete("insufficient semantic duplicate evidence; review title/body/symbols before publication")
    queries = [f'repo:{repo} "{path}"'] if path else []
    queries += [f"repo:{repo} {s}" for s in meaningful_symbols]
    if terms:
        queries.append(f"repo:{repo} " + " ".join(terms))
    seen: dict[int, Match] = {}
    probe = f"{title}\n{body}"
    for q in queries:
        try:
            # Caller-owned, scoped to one gate only; each write boundary gets a new map.
            key = (repo, q, strict)
            if query_results is not None and key in query_results:
                items = query_results[key]
            else:
                items = gh.search_issues(q, per_page=100 if strict else 20, fresh=strict, require_complete=strict)
                if query_results is not None:
                    query_results[key] = items
            if strict and len(items) >= 100:
                raise Incomplete("duplicate search reached its result limit; narrow and review before publication")
        except gh.GhError as exc:
            if strict:
                raise Incomplete("duplicate search unavailable; cannot establish no competing work") from exc
            continue
        for it in items:
            n = it["number"]
            if n in seen:
                continue
            hay = f"{it['title']}\n{(it.get('body') or '')[:2000]}"
            sc = similarity(probe, hay)
            # A bare file-name mention only counts when the text is at least loosely related;
            # otherwise an unrelated docs issue would block a real finding.
            mentions_path = bool(path) and path in hay and sc >= 0.08
            if mentions_path:
                sc = max(sc, threshold)
            if sc >= threshold:
                seen[n] = Match(
                    n, it["title"], it["html_url"], it["state"], "pull_request" in it, round(sc, 2),
                    "mentions file" if mentions_path else "similar text",
                )
    return sorted(seen.values(), key=lambda m: -m.score)


def recent_commit_hits(repo: str, path: str, keywords: list[str], days: int = 45, *, strict: bool = False) -> list[str]:
    """Commits touching `path` recently whose message shares keywords with the finding:
    someone may already have fixed it on main."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        commits = gh.api(f"repos/{repo}/commits", path=path, since=since, per_page=100 if strict else 20, fresh=strict) or []
    except gh.GhError as exc:
        if strict:
            raise Incomplete("recent commit check unavailable") from exc
        return []
    if strict and len(commits) >= 100:
        raise Incomplete("recent commit sample saturated; review a narrower change history")
    kw = {k.lower() for k in keywords}
    return [c["html_url"] for c in commits if kw & tokens(c["commit"]["message"])]


def is_duplicate(matches: list[Match], hard: float = 0.45) -> bool:
    """Any open item or any strongly similar closed item blocks filing (a closed
    'wontfix' is as informative as an open duplicate)."""
    return any(m.state == "open" or m.score >= hard for m in matches)
