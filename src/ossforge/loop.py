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

from . import discovery, digest, gh, hunt, patrol, safety, scout, submit, runs
from .config import Config
from .limiter import DailyLimiter
from .outcomes import Stats
from .queue import Item, Queue

MAX_REPOS_PER_PASS = 4


def run_once(cfg: Config, progress=None) -> list[str]:
    try:
        with runs.Run(Path(cfg.state_dir), progress) as run:
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
    notes = Notes(run)
    try:
        safety.kill_switch(cfg)
    except safety.Blocked as e:
        run.data["outcome"] = "halted"
        notes.append(f"halted: {e}")
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
    notes.append("patrol: checking existing contributions")
    actions.extend(patrol.patrol(audit, stats))
    notes.append(f"patrol: {sum(a.kind not in ('idle',) for a in actions)} open item(s) need attention")

    history = safety.effective_history(audit.read())
    blocked = {r["repo"] for r in history if r.get("repo") and safety.repo_allowed(cfg, r["repo"], history)}

    repos: list[str] = []
    issues: list[scout.Candidate] = []
    feats: list[scout.Candidate] = []
    day = int(time.time() // 86400)
    try:
        with gh.read_budget(cfg.discovery_read_budget_seconds):
            for lang in cfg.languages:
                # one label per pass (rotating) keeps the request burst small; features alternate days
                label = cfg.labels[day % len(cfg.labels)]
                notes.append(f"discovery: {lang} / {label} / {cfg.discovery_strategy}")
                for c in (scout.scout(cfg, stats, lang, label, 12, skip_repos=blocked) if cfg.discovery_strategy == "broad" else
                          discovery.discover(cfg, stats, lang, label, 12, cfg.discovery_strategy, skip_repos=blocked)):
                    if c.verdict.decision == "approve":
                        issues.append(c)
                        if c.repo not in repos:
                            repos.append(c.repo)
                notes.append("discovery funnel: " + scout.explain_funnel(scout.LAST_FUNNEL))
                if scout.LAST_FUNNEL.get("stopped"):
                    raise gh.ReadBudgetExceeded(str(scout.LAST_FUNNEL["stopped"]))
                if day % 2:
                    feats += [c for c in scout.scout_features(cfg, stats, lang, 8, blocked) if c.verdict.decision == "approve"]
                    notes.append("feature funnel: " + scout.explain_funnel(scout.LAST_FUNNEL))
                    if scout.LAST_FUNNEL.get("stopped"):
                        raise gh.ReadBudgetExceeded(str(scout.LAST_FUNNEL["stopped"]))
    except gh.ReadBudgetExceeded as exc:
        notes.run.data["outcome"] = "discovery_budget_exhausted"
        notes.append(f"discovery stopped: {exc}")
    notes.append(f"scout: {len(repos)} healthy, open-to-AI repo(s) with approved issues")

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
            notes.append(f"issue work {repo}: skipped (anti-AI in {pol.source})")
            continue
        for c in (item for item in issues if item.repo == repo):
            # Search/vet approval is only a work lead, not verified correctness.
            q.add(Item(f"{c.repo}#{c.number}:issue", c.repo, "", c.number, "issue", c.title,
                       status="clean", clone=str(clone), work="fix", rank=c.score,
                       notes=[c.url], source_issue=c.number))
        q.save()  # preserve each successful handoff before the next clone
        notes.append(f"issue work {repo}: handed off to driver; source issues must be rechecked before coding")
    for c in feats[:3]:
        clone = hunt.clone(cfg, c.repo)
        pol = hunt.policy_of(clone)
        if pol.anti_ai:
            notes.append(f"feature {c.repo}#{c.number}: skipped (anti-AI in {pol.source})")
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
