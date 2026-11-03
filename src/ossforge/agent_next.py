"""Read-only local checkpoints for an external IDE that is already the coding agent.

No model calls, discovery, verification or publication. Remote outcomes require
live reconciliation; saved URLs are progress records, not current remote truth.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import research_continuation, identity, preparation, research_session, repo_research, qualification, driver, evidence, submit
from .config import Config
from .queue import Queue


def next_step(cfg: Config, queue: Queue, config_path: str, scope: str = 'default') -> dict:
    if scope not in {'default', 'research'}:
        raise ValueError('unsupported checkpoint scope')
    if scope == 'research' and not cfg.repo_first:
        raise ValueError('research checkpoint scope requires repo_first: true')
    prefix = ["ossforge", "--config", config_path]
    continuation = prefix + ['agent-next'] + (['--scope', 'research'] if scope == 'research' else [])
    def result(status, **kw):
        return {"schema": 1, "status": status, "local_only": True, "continue_argv": continuation,
                "publication": "requires owner authorization; never inferred from this checkpoint", **kw}
    if Path(cfg.kill_file).exists():
        return result("stopped", reason="kill switch is set; do not resume without the owner's instruction")
    try:
        if research_continuation.pending(cfg):
            return repo_research.checkpoint(cfg, prefix) | {'continue_argv': continuation}
    except (ValueError, OSError) as exc:
        return result('inspect_local', reason=str(exc))
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
            evidence.check(Path(item.clone), submit._item_base(item), item.branch, item.verification, cfg=cfg, **({"contribution_base": item.contribution_base} if item.contribution_base else {}))
        except (ValueError, evidence.StaleEvidence, submit.Refused, OSError) as exc:
            return result("inspect_local", id=item.id, clone=item.clone, reason=str(exc),
                          warning="preserve work; inspect changes and rerun verification, never reset automatically")
        return result("verified_ready", id=item.id, repo=item.repo, clone=item.clone,
                      next_argv=prefix + ["submit", item.id],
                      reason="local content receipt is current; dry-run still rechecks live policy, identity, duplicate and upstream state")
    try:
        task = driver.task(queue)
    except (ValueError, evidence.StaleEvidence, submit.Refused, OSError) as exc:
        return result("inspect_local", reason=str(exc), warning="preserve the checkout; do not overwrite or reset it")
    if task["status"] == "ready":
        return result("work_candidate", task=task,
                      next_argv=prefix + ["agent-task", "--item", task["id"]])
    if cfg.repo_first:
        try:
            research_session.validate_existing(cfg)
            lead = repo_research.load(cfg)['lead']
        except (repo_research.Invalid, research_session.Invalid) as exc:
            return result('inspect_local', reason=str(exc))
        completed = [i for i in queue.items.values()
                     if lead and lead.get('status') == 'investigating'
                     and lead.get('research_id') and lead.get('clone')
                     and i.research_id == lead['research_id']
                     and i.repo == lead['repo'] and i.clone == lead['clone']
                     and (i.submitted.get('pr_url') or i.submitted.get('pr'))]
        if completed:
            try:
                prep = preparation._load(cfg)
                preserved = preparation.task(cfg, refresh_policy=False) if prep else None
            except (ValueError, OSError, evidence.StaleEvidence, identity.IdentityError) as exc:
                return result('inspect_local', reason=str(exc), warning='preserve local preparation before continuing')
            return result('contribution_submitted', stop_session=True,
                          completed=[{'id': i.id, 'repo': i.repo,
                                      'saved_issue': i.submitted.get('issue_url') or i.submitted.get('issue'),
                                      'saved_pr': i.submitted.get('pr_url') or i.submitted.get('pr')}
                                     for i in completed],
                          preserved_preparation=preserved,
                          warning='Saved submission progress, not live remote state or maintainer acceptance. '
                                  'Stop this contribution session; do not propose the same work or publish again. '
                                  'Other qualification/held work remains unchanged for a separately requested session.')
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
    pending = [i for i in queue.by_status("found") if i.source_issue and not i.clone]
    if pending:
        return result("recover_issue_handoff",
                      pending=[{"id": i.id, "repo": i.repo, "source_issue": i.source_issue} for i in pending],
                      next_argv=prefix + ["loop", "--once", "--prepare-only"],
                      warning="inspect interrupted checkouts first; preserve all files; recovery rechecks current vetting before cloning and never patches or publishes")
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
                      prior_status=last["status"], next_argv=prefix + ["loop", "--once", "--prepare-only"],
                      warning="inspect the run journal; retry only after its prerequisite or cooldown is resolved")
    reviews = qualification.pending(cfg.path("qualification.json"))
    if reviews:
        lead = sorted(reviews, key=lambda r: (r["repo"], r["number"]))[0]
        return result("review_needed", lead=lead,
                      review_argv=prefix + ["review-qualification", lead["repo"], str(lead["number"])],
                      next_argv=prefix + (["research-repo"] if cfg.repo_first else ["loop", "--once", "--prepare-only"]),
                      warning="Saved references are not current truth or proof of a fix. Refresh and inspect implementation, policy and discussion. No automatic approval/coding/publication; independent discovery may continue. Use review-qualification --defer REASON to pause this review.")
    held = queue.by_status("held")
    if held:
        return result("held_work", reason="unresolved held work is excluded from coding selection, not rejected or verified",
                      held=[{"id": i.id, "repo": i.repo, "reason": i.hold_reason} for i in held],
                      next_argv=prefix + (["research-repo"] if cfg.repo_first else ["loop", "--once", "--prepare-only"]),
                      warning="independent discovery may continue; unhold only after its prerequisite is resolved")
    preserved_preparation = None
    try:
        prep = preparation._load(cfg)
        if prep:
            checkpoint = preparation.task(cfg, refresh_policy=False)
            if scope == 'research' and prep['status'] == 'awaiting_owner_review' and cfg.repo_first:
                preserved_preparation = {'id': prep['id'], 'repo': prep['repo'],
                    'status': prep['status'], 'publication_hold': True,
                    'review_argv': prefix + ['preparation-task']}
            else:
                return result('local_preparation' if prep['status'] == 'local_investigation' else 'awaiting_owner_review',
                              preparation=checkpoint, next_argv=prefix + ['preparation-task'],
                              research_checkpoint_argv=prefix + ['agent-next', '--scope', 'research']
                                  if prep['status'] == 'awaiting_owner_review' and cfg.repo_first else None,
                              warning='No public queue or submission; source scope still required. '
                                      'An explicit research checkpoint preserves this completed review hold.')
    except (ValueError, OSError, evidence.StaleEvidence, identity.IdentityError) as exc:
        return result('inspect_local', reason=str(exc), warning='preserve local preparation before continuing')
    if cfg.repo_first:
        try:
            lead = repo_research.load(cfg)['lead']
            if not lead or lead['status'] != 'investigating':
                session = research_session.status(cfg)
                if session['status'] != 'search_session_active':
                    return result(session['status'], session=session, preserved_preparation=preserved_preparation,
                                  next_argv=prefix + ['research-session'],
                                  selected_repository=lead['repo'] if lead else None,
                                  investigation_started=False,
                                  warning='selection, refresh and initial begin require remaining search allowance; only already-started investigations with a preserved checkout continue after expiry. Preserve this selected lead; do not silently renew. Exhausted search is not proof of no opportunity')
            checkpoint = repo_research.checkpoint(cfg, prefix)
            checkpoint['continue_argv'] = continuation
            if preserved_preparation:
                checkpoint['preserved_preparation'] = preserved_preparation
            return checkpoint
        except (research_session.Invalid, repo_research.Invalid) as exc:
            return result("inspect_local", reason=str(exc), warning="preserve repository research state; do not reset it")
    completed = [i for i in queue.items.values() if i.submitted.get("pr_url") or i.submitted.get("pr")]
    return result("no_work", reason="no locally actionable candidate; not proof that no opportunity exists",
                  completed=[{"id": i.id, "saved_pr": i.submitted.get("pr_url") or i.submitted.get("pr")} for i in completed],
                  next_argv=prefix + ["loop", "--once", "--prepare-only"],
                  warning="bounded discovery can clone and enqueue approved issue leads, never patch or publish; leads still need current review/reproduction")
