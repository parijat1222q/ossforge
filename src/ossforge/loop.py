"""Continuous research loop. One pass = patrol -> scout -> hunt -> (draft) -> (submit verified).

Safe by construction: kill switch every pass, daily cap enforced by DailyLimiter,
and nothing is submitted unless an item is already `verified` AND auto_submit is on.
"""

from __future__ import annotations

import os
import math
import subprocess
import time
from pathlib import Path

from . import qualification, discovery, digest, gh, hunt, patrol, safety, scout, submit, runs, vet, radar
from .config import Config
from .limiter import DailyLimiter
from .outcomes import Stats
from .queue import Item, Queue

MAX_REPOS_PER_PASS = 4


def run_once(cfg: Config, progress=None) -> list[str]:
    try:
        with runs.Run(Path(cfg.state_dir), progress, before_journal=lambda: _pending_guard(cfg)) as run:
            return _run_once(cfg, run)
    except runs.Busy as exc:
        return [f"halted: {exc}"]


class Notes(list):
    def __init__(self, run):
        super().__init__()
        self.run = run

    def append(self, text):
        super().append(text)
        self.run.note(text)


def _run_once(cfg: Config, run) -> list[str]:
    from . import research_continuation
    research_continuation.guard(cfg)
    notes = Notes(run)
    try:
        safety.kill_switch(cfg)
    except safety.Blocked as e:
        run.data["outcome"] = "halted"
        notes.append(f"halted: {e}")
        return notes
    if cfg.discovery_sampling == "per-repo" and (len(cfg.languages) != 1 or len(cfg.labels) != 1):
        run.data["outcome"] = "halted"
        notes.append("halted: per-repo experiment requires exactly one configured language and label; no remote work started")
        return notes
    gh.configure(cfg.path("cache"), cfg.cache_ttl_seconds)
    q = Queue(cfg.path("queue.json"))
    audit = safety.Audit(cfg.path("audit.jsonl"))
    stats = Stats(cfg.path("stats.json"))
    limiter = DailyLimiter(cfg.path("daily.json"), cfg.cap)

    actions: list[patrol.Action] = []
    try:
        _pass(cfg, q, audit, stats, limiter, notes, actions)
    except gh.RateLimited as e:
        run.data["outcome"] = "rate_limited"
        notes.append(f"stopped early: GitHub rate limit ({str(e)[:80]}). Progress is saved; the next pass continues after the cooldown.")
    digest.write(cfg.path("digest.md"), digest.render(q, audit, actions, notes))
    return notes


def _pass(cfg: Config, q: Queue, audit: safety.Audit, stats: Stats, limiter: DailyLimiter, notes: list[str],
          actions: list[patrol.Action]) -> None:
    if cfg.work_kind not in {"fix", "both"}:
        raise ValueError("work_kind must be fix or both")
    notes.append("patrol: checking existing contributions")
    actions.extend(patrol.patrol(audit, stats))
    notes.append(f"patrol: {sum(a.kind not in ('idle',) for a in actions)} open item(s) need attention")

    history = safety.effective_history(audit.read())
    blocked = {r["repo"] for r in history if r.get("repo") and safety.repo_allowed(cfg, r["repo"], history)}

    repos: list[str] = []
    issues: list[scout.Candidate] = []
    feats: list[scout.Candidate] = []
    # Persisted leads are not approvals. Recover them before another search,
    # rechecking current health/discussion/policy before any clone attempt.
    pending = [i for i in q.by_status("found") if i.kind == "issue" and i.source_issue and not i.clone]
    with gh.read_budget(cfg.discovery_read_budget_seconds):
        for item in pending[:MAX_REPOS_PER_PASS]:
            try:
                health = radar.analyze(item.repo)
                verdict = vet.vet_issue(item.repo, item.source_issue, repo_ok=health.score >= cfg.min_repo_score)
            except (gh.RateLimited, gh.ReadBudgetExceeded) as exc:
                notes.run.data["outcome"] = "discovery_budget_exhausted"
                notes.append(f"issue recovery stopped: {exc}; leads preserved")
                break
            except (gh.GhError, OSError) as exc:
                notes.append(f"issue recovery {item.id}: current review unavailable ({exc}); preserved")
                continue
            if verdict.decision != "approve":
                q.set(item.id, status="held", hold_reason="current recovery review: " + "; ".join(verdict.reasons))
                q.save()
                continue
            issues.append(scout.Candidate(item.repo, item.source_issue, item.detail,
                                          item.notes[0] if item.notes else "", health.score, verdict, score=item.rank))
            if item.repo not in repos:
                repos.append(item.repo)
    day = int(time.time() // 86400)
    try:
        with gh.read_budget(cfg.discovery_read_budget_seconds):
            for lang in ([] if pending else cfg.languages):
                # one label per pass (rotating) keeps the request burst small; features alternate days
                label = cfg.labels[day % len(cfg.labels)]
                notes.append(f"discovery: {lang} / {label} / {cfg.discovery_strategy}")
                for c in (scout.scout(cfg, stats, lang, label, 12, skip_repos=blocked) if cfg.discovery_strategy == "broad" and cfg.discovery_sampling == "combined" else
                          discovery.discover(cfg, stats, lang, label, 12, cfg.discovery_strategy, skip_repos=blocked)):
                    if c.verdict.references:
                        qualification.record(cfg.path("qualification.json"), c)
                    if c.verdict.decision == "approve":
                        issues.append(c)
                        if c.repo not in repos:
                            repos.append(c.repo)
                notes.append("discovery funnel: " + scout.explain_funnel(scout.LAST_FUNNEL))
                if scout.LAST_FUNNEL.get("stopped"):
                    raise gh.ReadBudgetExceeded(str(scout.LAST_FUNNEL["stopped"]))
                if cfg.work_kind == "both" and day % 2:
                    feats += [c for c in scout.scout_features(cfg, stats, lang, 8, blocked) if c.verdict.decision == "approve"]
                    notes.append("feature funnel: " + scout.explain_funnel(scout.LAST_FUNNEL))
                    if scout.LAST_FUNNEL.get("stopped"):
                        raise gh.ReadBudgetExceeded(str(scout.LAST_FUNNEL["stopped"]))
    except gh.ReadBudgetExceeded as exc:
        notes.run.data["outcome"] = "discovery_budget_exhausted"
        notes.append(f"discovery stopped: {exc}")
    notes.append(f"scout: {len(repos)} healthy, open-to-AI repo(s) with approved issues")

    # Save all issue identities before the first potentially slow clone. A
    # stopped process must not lose the successfully discovered work leads.
    for c in issues:
        q.add(Item(f"{c.repo}#{c.number}:issue", c.repo, "", c.number, "issue", c.title,
                   status="found", work="fix", rank=c.score, notes=[c.url], source_issue=c.number))
    q.save()
    for repo in repos[:MAX_REPOS_PER_PASS]:
        # A vetted issue is already a work lead. Do not block its handoff on an
        # unrelated full-tree static hunt and its separate duplicate searches.
        try:
            clone = hunt.clone(cfg, repo)
            pol = hunt.policy_of(clone)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            notes.append(f"issue work {repo}: checkout unavailable; inspect before retry ({exc})")
            continue
        if pol.anti_ai:
            for c in (item for item in issues if item.repo == repo):
                item = q.items[f"{c.repo}#{c.number}:issue"]
                if item.status == "found" and not item.clone:
                    q.set(item.id, status="held", hold_reason=f"local anti-AI policy in {pol.source}")
            q.save()
            notes.append(f"issue work {repo}: skipped (anti-AI in {pol.source})")
            continue
        if pol.agent_review:
            for c in (item for item in issues if item.repo == repo):
                item = q.items[f"{c.repo}#{c.number}:issue"]
                if item.status == "found" and not item.clone:
                    q.set(item.id, status="held", hold_reason=f"agent contribution policy needs review in {pol.source}")
            q.save()
            notes.append(f"issue work {repo}: policy review needed in {pol.source}; no coding handoff")
            continue
        for c in (item for item in issues if item.repo == repo):
            # Search/vet approval is only a work lead, not verified correctness.
            item = q.items[f"{c.repo}#{c.number}:issue"]
            if item.status == "found" and not item.clone:
                q.set(item.id, status="clean", clone=str(clone))
        q.save()  # preserve each successful handoff before the next clone
        notes.append(f"issue work {repo}: handed off to driver; source issues must be rechecked before coding")
    for c in feats[:3]:
        clone = hunt.clone(cfg, c.repo)
        pol = hunt.policy_of(clone)
        if pol.anti_ai:
            notes.append(f"feature {c.repo}#{c.number}: skipped (anti-AI in {pol.source})")
            continue
        if pol.agent_review:
            notes.append(f"feature {c.repo}#{c.number}: agent policy review needed in {pol.source}; no coding handoff")
            continue
        it = Item(f"{c.repo}#{c.number}:feature", c.repo, "", c.number, "feature", c.title,
                  status="clean", work="feature", clone=str(clone), source_issue=c.number, rank=c.score, notes=[c.url])
        q.add(it)
    q.save()

    if cfg.patch_command:
        for it in sorted(q.by_status("clean"), key=lambda i: -i.rank)[:1]:
            subprocess.run(cfg.patch_command, shell=True, env={**os.environ, "OSSFORGE_ITEM": it.id})
            notes.append(f"patch_command ran for {it.id}; run `ossforge verify` to confirm it")

    if cfg.auto_submit:
        for it in q.by_status("verified"):
            if limiter.remaining() <= 0:
                break
            try:
                res = submit.submit(it, limiter, cfg, audit, execute=True,
                                    save=lambda p, i=it: (q.set(i.id, submitted=p), q.save()))
                q.set(it.id, status="submitted", submitted=res)
                notes.append(f"submitted {res['pr']}")
            except submit.Refused as e:
                notes.append(f"refused {it.id}: {e}")
        q.save()



def run_forever(cfg: Config, interval_s: int) -> None:
    if not math.isfinite(interval_s) or interval_s < 1:
        raise ValueError("loop interval must be finite and at least one second")
    while True:
        run_once(cfg, progress=lambda text: print(text, flush=True))
        if Path(cfg.kill_file).exists():
            return
        time.sleep(interval_s)


def _pending_guard(cfg):
    from . import research_continuation
    research_continuation.guard(cfg)
