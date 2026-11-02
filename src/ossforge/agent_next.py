"""Read-only local checkpoints for an external IDE that is already the coding agent.

No model calls, discovery, verification or publication. Remote outcomes require
live reconciliation; saved URLs are progress records, not current remote truth.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import driver, evidence, submit
from .config import Config
from .queue import Queue


def next_step(cfg: Config, queue: Queue, config_path: str) -> dict:
    prefix = ["ossforge", "--config", config_path]
    def result(status, **kw):
        return {"schema": 1, "status": status, "local_only": True,
                "publication": "requires owner authorization; never inferred from this checkpoint", **kw}
    if Path(cfg.kill_file).exists():
        return result("stopped", reason="kill switch is set; do not resume without the owner's instruction")
    # A partial remote effect takes priority over new work. Do not blindly retry.
    for item in queue.items.values():
        progress = item.submitted
        pr = progress.get("pr_url") or progress.get("pr")
        if progress and not pr:
            return result("reconcile_remote", id=item.id, repo=item.repo, clone=item.clone,
                          saved_progress=progress, reason="partial publication: inspect current remote issue/fork/PR before any retry",
                          after_reconciliation=prefix + ["submit", item.id],
                          warning="the suggested submit command is dry-run only; it does not reconcile an unknown remote outcome")
    for item in queue.by_status("verified"):
        if item.submitted.get("pr_url") or item.submitted.get("pr"):
            continue
        try:
            evidence.check(Path(item.clone), submit._base(item.clone), item.branch, item.verification)
        except (evidence.StaleEvidence, submit.Refused, OSError) as exc:
            return result("inspect_local", id=item.id, clone=item.clone, reason=str(exc),
                          warning="preserve work; inspect changes and rerun verification, never reset automatically")
        return result("verified_ready", id=item.id, repo=item.repo, clone=item.clone,
                      next_argv=prefix + ["submit", item.id],
                      reason="local content receipt is current; dry-run still rechecks live policy, identity, duplicate and upstream state")
    try:
        task = driver.task(queue)
    except (evidence.StaleEvidence, submit.Refused, OSError) as exc:
        return result("inspect_local", reason=str(exc), warning="preserve the checkout; do not overwrite or reset it")
    if task["status"] == "ready":
        return result("work_candidate", task=task,
                      next_argv=prefix + ["agent-task", "--item", task["id"]])
    # Local actionable work wins; never propose another remote search during a
    # known cooldown or disguise an interrupted read phase as a clean no-find.
    try:
        until = float(cfg.path("ratelimit_until").read_text())
        if not math.isfinite(until) or until < 0:
            raise ValueError("invalid cooldown")
    except FileNotFoundError:
        until = 0
    except (OSError, ValueError):
        return result("inspect_local", reason="cooldown record unreadable; inspect it before new remote discovery")
    remaining = until - time.time()
    if remaining > 0:
        return result("wait_for_cooldown", retry_after_seconds=math.ceil(remaining),
                      retry_at_epoch=until, reason="recorded GitHub cooldown; do not search or change accounts to bypass it")
    try:
        last = json.loads(cfg.path("run.json").read_text())
        if not isinstance(last, dict):
            raise ValueError("invalid journal")
    except FileNotFoundError:
        last = {}
    except (OSError, ValueError):
        return result("inspect_local", reason="run journal unreadable; preserve it and inspect before discovery")
    if last.get("status") in ("discovery_budget_exhausted", "rate_limited", "interrupted", "failed", "running"):
        return result("review_incomplete_discovery", reason="previous run is partial or incomplete, not a clean no-find",
                      prior_status=last["status"], next_argv=prefix + ["--json", "discover", "--strategy", "rotate"],
                      warning="inspect the run journal; retry only after its prerequisite or cooldown is resolved")
    held = queue.by_status("held")
    if held:
        return result("held_work", reason="unresolved held work is excluded from coding selection, not rejected or verified",
                      held=[{"id": i.id, "repo": i.repo, "reason": i.hold_reason} for i in held],
                      next_argv=prefix + ["--json", "discover", "--strategy", "rotate"],
                      warning="independent discovery may continue; unhold only after its prerequisite is resolved")
    completed = [i for i in queue.items.values() if i.submitted.get("pr_url") or i.submitted.get("pr")]
    return result("no_work", reason="no locally actionable candidate; not proof that no opportunity exists",
                  completed=[{"id": i.id, "saved_pr": i.submitted.get("pr_url") or i.submitted.get("pr")} for i in completed],
                  next_argv=prefix + ["--json", "discover", "--strategy", "rotate"],
                  warning="discovery reads live GitHub state but does not clone or enqueue; review leads then hunt a policy-compatible target")
