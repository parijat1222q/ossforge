"""Opt-in local command orchestration. Driver commands are operator-trusted code."""
from __future__ import annotations

import json
import math
import subprocess
import time
from datetime import date
from pathlib import Path

from . import processes, contribution_base as cb, policy_review, preparation, identity, driver, loop, runs, safety, submit, verify, evidence
from .config import Config
from .limiter import DailyLimiter
from .queue import Queue


def invoke(command: list[str], cwd: Path, task_file: Path, result_file: Path, timeout: float) -> int:
    """No shell; kill the driver's process group on timeout. This is not isolation."""
    env = verify.scrubbed_env({'OSSFORGE_TASK_FILE': str(task_file.resolve()),
                               'OSSFORGE_RESULT_FILE': str(result_file.resolve())})
    with processes.launched(command, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as (proc, job):
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            processes.kill_tree(proc, job)
            proc.wait()
            raise


def run(cfg: Config, command: list[str], *, timeout: float = 900, attempts: int = 2,
        execute: bool = False, discover: bool = True, progress=None) -> dict:
    if not command or any(not isinstance(v, str) or not v for v in command):
        raise driver.Invalid('driver command must be nonempty argv')
    if not math.isfinite(timeout) or not 1 <= timeout <= 3600 or type(attempts) is not int or not 1 <= attempts <= 20:
        raise driver.Invalid('driver timeout must be 1..3600 seconds and daily attempts 1..20')
    try:
        with runs.Run(Path(cfg.state_dir), progress, before_journal=lambda: _pending_guard(cfg)) as journal:
            return _run(cfg, command, timeout, attempts, execute, discover, journal)
    except runs.Busy as exc:
        return {'status': 'busy', 'reason': str(exc)}


def _run(cfg, command, timeout, attempts, execute, discover, journal):
    from . import research_continuation
    research_continuation.guard(cfg)
    safety.kill_switch(cfg)
    # Publication is a separate explicit CLI choice, never inherited from loop config.
    from dataclasses import replace
    if discover:
        loop._run_once(replace(cfg, auto_submit=False, patch_command=''), journal)
    q = Queue(cfg.path('queue.json'))
    pending = q.by_status('verified')
    job = driver.task(q) if not pending else {'status': 'resume_verified', 'id': pending[0].id}
    if job['status'] == 'no_work':
        journal.note(job['reason'])
        if journal.data.get('outcome') in ('discovery_budget_exhausted', 'rate_limited', 'halted'):
            return {'schema': 1, 'status': 'discovery_incomplete',
                    'reason': journal.data['outcome'], 'candidate_status': 'no_work'}
        return job
    item_id = job['id']
    item = q.items[item_id]
    if job['status'] != 'resume_verified':
        ledger_path = cfg.path('driver-attempts.json')
        try:
            ledger = json.loads(ledger_path.read_text())
        except FileNotFoundError:
            ledger = {'day': date.today().isoformat(), 'count': 0}
        except (OSError, ValueError) as exc:
            raise driver.Invalid('invalid attempt ledger; inspect it before continuing') from exc
        today = date.today().isoformat()
        if not isinstance(ledger, dict) or not isinstance(ledger.get('day'), str):
            raise driver.Invalid('invalid attempt ledger; inspect it before continuing')
        try:
            stored = date.fromisoformat(ledger['day'])
        except ValueError as exc:
            raise driver.Invalid('invalid attempt ledger date') from exc
        if stored > date.today():
            raise driver.Invalid('attempt ledger is from a future date; inspect host clock/state')
        if type(ledger.get('count')) is not int or ledger['count'] < 0:
            raise driver.Invalid('invalid attempt count')
        count = ledger['count'] if stored == date.today() else 0
        if count >= attempts:
            journal.note('daily driver attempt limit reached; candidate stays queued')
            journal.data['outcome'] = 'attempt_limit'
            return {'status': 'attempt_limit', 'id': item_id}
        # Reserve before invocation: crashes/timeouts consume an attempt too.
        runs.Run._write(ledger_path, {'day': today, 'count': count + 1})
        folder = cfg.path('driver-jobs') / journal.data['id']
        folder.mkdir(parents=True)
        task_file, result_file = folder / 'task.json', folder / 'result.json'
        job['assigned_head'] = evidence._git(Path(job['clone']), 'rev-parse', 'HEAD')
        job['assigned_checkout'] = evidence.capture(Path(job['clone']), job['base'], [], **({'contribution_base': item.contribution_base} if item.contribution_base else {}))
        task_file.write_text(json.dumps(job, indent=2), encoding='utf-8')
        journal.note(f'driver: starting {item_id}; timeout {timeout}s')
        started = time.monotonic()
        try:
            rc = invoke(command, Path(job['clone']), task_file, result_file, timeout)
        except subprocess.TimeoutExpired:
            journal.data['outcome'] = 'driver_timeout'
            journal.note('driver timeout; inspect local checkout before retry')
            return {'status': 'driver_timeout', 'id': item_id}
        except OSError as exc:
            journal.data['outcome'] = 'driver_error'
            journal.note(f'driver launch failed: {type(exc).__name__}')
            return {'status': 'driver_error', 'id': item_id}
        journal.note(f'driver exited {rc} after {time.monotonic() - started:.2f}s')
        if rc:
            journal.data['outcome'] = 'driver_failed'
            return {'status': 'driver_failed', 'id': item_id, 'exit_code': rc}
        result = driver.parse(result_file)
        cb.check(result, cb.of(item))
        cb.check(job, cb.of(item))
        if result.get('outcome') == 'hold' and any(result.get(k) != job['hold_result_schema'][k] for k in ('branch', 'tree')):
            raise driver.Invalid('hold result does not match assigned snapshot')
        if result['id'] != item_id or result['base'] != job['base']:
            raise driver.Invalid('driver result does not match assigned item/base')
        if result.get('commit_message'):
            _commit_result(cfg, q, job, result, journal)
            result = {k: v for k, v in result.items() if k not in {'commit_message', 'commit_paths'}}
        safety.kill_switch(cfg)
        checked = driver.complete(q, result, isolation_mode=cfg.verification_isolation,
                                  readonly_paths=cfg.verification_readonly_paths)
        journal.note(f'verification: {checked["status"]}')
        if checked['status'] != 'verified':
            journal.data['outcome'] = checked['status']
            return checked
    item = q.items[item_id]
    if job['status'] == 'resume_verified':
        evidence.check(Path(item.clone), submit._item_base(item), item.branch, item.verification, cfg=cfg, **({'contribution_base': item.contribution_base} if item.contribution_base else {}))
    if not execute:
        journal.note('verified patch retained; publication not enabled')
        return {'status': 'verified_not_submitted', 'id': item_id}
    safety.kill_switch(cfg)
    limiter = DailyLimiter(cfg.path('daily.json'), cfg.cap)
    audit = safety.Audit(cfg.path('audit.jsonl'))
    try:
        result = submit.submit(item, limiter, cfg, audit, execute=True,
                               save=lambda p: (q.set(item_id, submitted=p), q.save()))
    except submit.Refused as exc:
        journal.data['outcome'] = 'publication_refused'
        journal.note(f'publication refused: {exc}')
        return {'status': 'publication_refused', 'id': item_id, 'reason': str(exc)}
    q.set(item_id, status='submitted', submitted=result)
    q.save()
    journal.note(f'submitted: {result["pr"]}')
    return {'status': 'submitted', 'id': item_id, 'result': result}


def _commit_result(cfg, queue, job, result, journal):
    """Parent-only commit, after child exit, under the runner's run.lock."""
    safety.kill_switch(cfg)
    current = Queue(cfg.path('queue.json'))
    item = current.items.get(job['id'])
    assigned = queue.items.get(job['id'])
    from dataclasses import asdict
    if not item or not assigned or asdict(item) != asdict(assigned) or item.status != 'clean' or item.submitted:
        raise driver.Invalid('assigned queue changed before commit; preserve patch')
    cb.check(job, cb.of(item))
    cb.check(result, cb.of(item))
    clone = Path(job['clone'])
    if (clone.is_symlink() or any(p.is_symlink() for p in clone.parents)
            or Path(evidence._git(clone, 'rev-parse', '--show-toplevel')).resolve() != clone
            or Path(item.clone).resolve() != clone):
        raise driver.Invalid('assigned Git root changed before commit')
    preparation.public_guard(clone)
    default_probe = subprocess.run(['git', 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'], cwd=clone, capture_output=True, text=True)
    if default_probe.returncode and (not item.contribution_base or default_probe.returncode != 1):
        raise driver.Invalid('default branch guard unavailable; preserve patch')
    default_branch = default_probe.stdout.strip().removeprefix('origin/') if not default_probe.returncode else None
    if (submit._item_base(item) != job['base'] or result['base'] != job['base']
            or evidence._git(clone, 'symbolic-ref', '--short', 'HEAD') != result['branch']
            or (item.contribution_base and result['branch'] == item.contribution_base['branch'])
            or result['branch'] == default_branch
            or evidence._git(clone, 'rev-parse', 'HEAD') != job['assigned_head']
            or evidence._git(clone, 'rev-parse', 'HEAD^{tree}') != job['assigned_checkout']['tree']):
        raise driver.Invalid('assigned HEAD/base/tree or result branch changed before commit')
    identity.require(identity.check_upstream(str(clone), item.repo))
    why = policy_review.check(cfg, item.repo)
    if why:
        raise driver.Invalid(why)
    if policy_review.publication_route(cfg, item.repo) not in {'issue_pr', 'pr_only'}:
        raise driver.Invalid('coding prerequisites unresolved before commit')
    paths = result['commit_paths']
    # Explicit regular source paths only. No automatic whole-tree staging.
    for rel in paths:
        p = clone / rel
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(clone):
            raise driver.Invalid('commit path is not an existing regular source file')
    changed = set(evidence._git(clone, 'diff', '--name-only', 'HEAD').splitlines())
    changed.update(evidence._git(clone, 'ls-files', '--others', '--exclude-standard').splitlines())
    if not changed or changed != set(paths):
        raise driver.Invalid('commit paths must exactly match intended changed/untracked files; no empty or unrelated commit')
    import fnmatch
    if any(any(fnmatch.fnmatch(p, pat) for pat in identity.ARTIFACTS) for p in paths):
        raise driver.Invalid('generated artifact cannot be committed by runner')
    safety.kill_switch(cfg)
    commit = identity.commit(cfg, str(clone), result['commit_message'], paths)
    journal.data['driver_commit'] = {'id': item.id, 'head': commit, 'base': job['base'],
                                      'branch': result['branch'], 'paths': paths}
    journal.note('parent identity-checked commit completed; verification still pending')


def _pending_guard(cfg):
    from . import research_continuation
    research_continuation.guard(cfg)
