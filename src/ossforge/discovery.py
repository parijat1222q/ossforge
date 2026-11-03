"""Bounded, read-only discovery planning and temporary result rotation.

Repository affinity selects where to look, never whether an issue is valid.
The existing scout gates remain authoritative for every fetched issue.
"""
from __future__ import annotations

import re
import hashlib
import json
import time
from dataclasses import dataclass

from . import discovery_resume, gh, issue_reviews, scout
from .config import Config
from .outcomes import Stats

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


# Description/topic evidence only changes order within one bounded search page.
# A language label can come from helper scripts in a documentation collection.
# Keep unknown/custom layouts and every collection available for later review.
_RESOURCE_DESCRIPTION = re.compile(
    r"\b(?:curated|collective|definitive)\s+(?:list|collection)\b"
    r"|\b(?:freely available|free)\s+programming books\b"
    r"|\b(?:list|collection) of (?:free |public |learning |programming )*(?:apis|books|resources|tutorials)\b"
    r"|\b(?:prep(?:are)? for|preparation for) (?:the )?.{0,30}interview\b", re.I)


def _resource_collection_hint(repository: dict) -> bool:
    """Weak metadata hint, not a suitability verdict or exclusion rule."""
    description = repository.get('description')
    topics = repository.get('topics')
    return (isinstance(description, str) and bool(_RESOURCE_DESCRIPTION.search(description))
            or isinstance(topics, list) and 'awesome-list' in topics)


@dataclass
class Plan:
    strategy: str
    repos: list[str]
    query: str


def discover_broad_repos(cfg: Config, language: str | None = None, cursor: int = 0) -> list[str]:
    """Take five repos per broad turn; page over the first990 ranked results.

    GitHub search only exposes the first1000 results.33 full pages of30
    keep requests within that limit; rotation then wraps, not exhaustive search.
    """
    if cfg.min_stars <= 0:
        return []
    query = f"stars:>={cfg.min_stars} fork:false archived:false"
    if language:
        query += f" language:{language}"
    try:
        page = (cursor // 6) % 33 + 1
        repos = gh.search_repositories(query, per_page=30, sort="stars", page=page)
        if not repos and page > 1:
            repos = gh.search_repositories(query, per_page=30, sort="stars", page=1)
    except gh.RateLimited:
        raise
    except (gh.GhError, OSError):
        return []
    candidates = [r for r in repos if isinstance(r, dict)
                  and isinstance(r.get("full_name"), str) and _REPO.fullmatch(r["full_name"])]
    if cfg.repo_first:
        # Stable partition BEFORE slicing the five-repository window. Preserve
        # star order within both groups and retain all thirty page entries.
        candidates.sort(key=_resource_collection_hint)
    names = [r["full_name"] for r in candidates]
    if not names:
        return []
    window_size = 5
    start = ((cursor % 6) * window_size) % len(names)
    return [names[(start + i) % len(names)] for i in range(min(window_size, len(names)))]


def plan(cfg: Config, stats: Stats, language: str | None, label: str,
         strategy: str, cursor: int, broad_repos: list[str] | None = None) -> Plan:
    if strategy not in {"rotate", "broad", "history", "preferred", "breadth-first"}:
        raise ValueError("strategy must be rotate, broad, history, preferred or breadth-first")
    preferred = sorted({r.lower() for r in cfg.preferred_repos if _REPO.fullmatch(r)})
    history = sorted(r.lower() for r in stats.d["repos"] if _REPO.fullmatch(r) and stats.repo_boost(r) > 0)
    pools = {"preferred": preferred, "history": history}
    enabled = ["broad"] + [s for s in ("preferred", "history") if pools[s]]
    epoch_size = 6 + len(enabled) - 1
    if strategy == "breadth-first":
        offset = cursor % epoch_size
        chosen = "broad" if offset < 6 else enabled[offset - 5]
    else:
        chosen = enabled[cursor % len(enabled)] if strategy == "rotate" else strategy
    repos = pools.get(chosen, [])
    # Five repositories bound query length and request cost. Rotating windows eventually cover the list.
    window = cursor // epoch_size if strategy == "breadth-first" else (cursor // len(enabled) if strategy == "rotate" else cursor)
    if chosen == "broad" and broad_repos:
        repos = [broad_repos[(window * 5 + i) % len(broad_repos)] for i in range(min(5, len(broad_repos)))]
    elif repos:
        repos = [repos[(window * 5 + i) % len(repos)] for i in range(min(5, len(repos)))]
    query = scout.build_query(language, label, cfg.min_stars)
    if repos:
        query += " " + " ".join(f"repo:{r}" for r in repos)
    return Plan(chosen, repos, query)


def recent_reason(data: dict, repo: str, number: int, updated_at: str | None,
                  days: int, now: float | None = None) -> str:
    entry = data.get(f"{repo.lower()}#{number}")
    now = time.time() if now is None else now
    if days > 0 and updated_at and isinstance(entry, dict) and entry.get("updated_at") == updated_at:
        seen = entry.get("seen_at")
        if isinstance(seen, (int, float)) and 0 <= now - seen < days * 86400:
            return "recently surfaced unchanged issue"
    return ""


def discover(cfg: Config, stats: Stats, language: str | None = None, label: str = "help wanted",
             limit: int = 20, strategy: str = "rotate", include_recent: bool = False,
             skip_repos: set[str] | None = None) -> list[scout.Candidate]:
    if cfg.discovery_sampling not in {"combined", "per-repo"}:
        raise ValueError("discovery_sampling must be combined or per-repo")
    if cfg.discovery_sampling == "per-repo" and (len(cfg.languages) != 1 or len(cfg.labels) != 1):
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL.update(fetched=0, kept=0, approved=0,
                                stopped="per-repo experiment requires exactly one configured language and label; no remote work started")
        return []
    state_path = cfg.path("discovery.json")
    state = issue_reviews.load(state_path)
    cursor_key = "breadth_cursor" if strategy == "breadth-first" else "cursor"
    source_cursor_key = cursor_key
    if cfg.discovery_sampling == "per-repo":
        cursor_key = "scoped_" + cursor_key
    cursor = state.get(cursor_key, state.get(source_cursor_key, 0))
    cursor = cursor if isinstance(cursor, int) and cursor >= 0 else 0
    selected = plan(cfg, stats, language, label, strategy, cursor)
    recent = state.get("recent", {})
    recent = recent if isinstance(recent, dict) else {}
    try:
        if selected.strategy == "broad" and not selected.repos:
            # Count broad turns, not global turns interleaved with affinity.
            preferred = any(_REPO.fullmatch(r) for r in cfg.preferred_repos)
            history = any(_REPO.fullmatch(r) and stats.repo_boost(r) > 0 for r in stats.d["repos"])
            stride = 1 + int(preferred) + int(history) if strategy == "rotate" else 1
            if strategy == "breadth-first":
                epoch_size = 6 + int(preferred) + int(history)
                broad_cursor = (cursor // epoch_size) * 6 + cursor % epoch_size
            else:
                broad_cursor = cursor // stride
            broad_repos = discover_broad_repos(cfg, language, broad_cursor)
            if broad_repos:
                selected.repos = broad_repos
                selected.query = scout.build_query(language, label, cfg.min_stars) + " " + " ".join(f"repo:{r}" for r in broad_repos)
    except (gh.RateLimited, gh.ReadBudgetExceeded) as e:
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL.update(fetched=0, kept=0, approved=0, stopped=f"GitHub read deadline or rate limit while searching ({e}); nothing was fetched")
        return []
    pool = list(selected.repos)
    sample_index = 0
    sample_key = ""
    if cfg.discovery_sampling == "per-repo":
        if not pool:
            scout.LAST_FUNNEL.clear()
            scout.LAST_FUNNEL.update(fetched=0, kept=0, approved=0,
                                    stopped="per-repo sampling requires a confirmed repository pool; no unscoped fallback")
            return []
        sample_key = hashlib.sha256(json.dumps([strategy, cursor, language, label, limit, pool]).encode()).hexdigest()
        saved_sample = state.get("scoped_sample", {})
        if isinstance(saved_sample, dict) and saved_sample.get("key") == sample_key:
            value = saved_sample.get("index", 0)
            sample_index = value if type(value) is int and 0 <= value < len(pool) else 0
        selected.repos = [pool[sample_index]]
        selected.query = scout.build_query(language, label, cfg.min_stars) + " repo:" + selected.repos[0]
    # Do not abandon another scope's unfinished batch by changing sampling mode.
    # Finish that public run with its original config before switching the experiment.
    pending_batches = issue_reviews.load(cfg.path("discovery-resume.json"))
    active_key = discovery_resume._key(selected.query, limit)
    if any(
            key != active_key and isinstance(value, dict) and value.get("remaining")
            and (cfg.discovery_sampling == "per-repo" or value.get("sampling") == "per-repo")
            for key, value in pending_batches.items()):
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL.update(fetched=0, kept=0, approved=0,
                                stopped="unfinished discovery scope exists; finish it with its original config before changing query scope")
        return []
    if selected.strategy != "broad" and not selected.repos:
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL.update(fetched=0, kept=0, approved=0, stopped="")
        result = []
    else:
        result = scout.scout(cfg, stats, language, label, limit, skip_repos=skip_repos,
                             query=selected.query, recent={} if include_recent else recent)
    # A stopped pass does not consume its strategy turn or hide partial results on retry.
    if not scout.LAST_FUNNEL.get("stopped"):
        now = time.time()
        recent = {k: v for k, v in recent.items() if isinstance(v, dict)
                  and isinstance(v.get("seen_at"), (int, float))
                  and 0 <= now - v["seen_at"] < max(0, cfg.recent_issue_days) * 86400}
        for c in result:
            if c.verdict.decision == "approve":
                recent[f"{c.repo.lower()}#{c.number}"] = {"updated_at": c.updated_at, "seen_at": now}
        advance = cfg.discovery_sampling != "per-repo" or sample_index + 1 >= len(pool)
        update = {**state, cursor_key: cursor + int(advance), "recent": recent}
        if cfg.discovery_sampling == "per-repo":
            update["scoped_sample"] = {"key": sample_key, "index": 0 if advance else sample_index + 1}
        issue_reviews._save(state_path, update)
    for c in result:
        c.why.append(f"discovery source: {selected.strategy}")
    scout.LAST_FUNNEL.update(strategy=selected.strategy, query=selected.query, repos=selected.repos,
                            sampling=cfg.discovery_sampling, sample_index=sample_index, repository_pool=pool)
    return result
