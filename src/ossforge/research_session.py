"""Explicit session-local repository search budget, independent of issue cursors.

This accounts elapsed wall time, including interruptions. Resuming never starts a
new allowance. It is not a whole-contribution timeout or publication authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict

from .config import Config


class Invalid(ValueError):
    pass


def _identity(cfg: Config) -> str:
    # Changing selection/policy/identity settings requires a new explicit session.
    # Deliberately omit transient I/O knobs and authentication secrets.
    keys = ('languages', 'preferred_repos', 'skills', 'min_stars', 'min_repo_score',
            'github_user', 'work_kind', 'repo_first', 'work_dir', 'state_dir',
            'discovery_strategy', 'discovery_sampling', 'own_orgs')
    raw = asdict(cfg)
    payload = {k: raw[k] for k in keys}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load(cfg: Config) -> dict | None:
    try:
        data = json.loads(cfg.path('research_session.json').read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Invalid('search session unreadable; preserve it before retry') from exc
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise Invalid('search session invalid; preserve it before retry')
    for key in ('started_at', 'deadline', 'budget_seconds'):
        value = data.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise Invalid('search session timing invalid; preserve it before retry')
    if (data['deadline'] != data['started_at'] + data['budget_seconds']
            or not isinstance(data.get('config_identity'), str)):
        raise Invalid('search session timing or identity invalid; preserve it before retry')
    return data


def start(cfg: Config, budget_seconds: float, *, new: bool = False) -> dict:
    """Start once. Existing sessions must be inspected, never silently replaced."""
    if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise Invalid('search budget must be a finite positive number of seconds')
    previous = load(cfg)
    if previous is not None:
        if not new or time.time() < previous['deadline']:
            raise Invalid('search session already exists; resume it, do not extend its deadline')
        with cfg.path('research_session_history.jsonl').open('a') as history:
            history.write(json.dumps(previous) + '\n')
    now = time.time()
    data = {'schema': 1, 'started_at': now, 'budget_seconds': budget_seconds,
            'deadline': now + budget_seconds, 'config_identity': _identity(cfg)}
    path = cfg.path('research_session.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    return status(cfg)


def status(cfg: Config) -> dict:
    data = load(cfg)
    if data is None:
        return {'status': 'search_session_needed'}
    if data['config_identity'] != _identity(cfg):
        raise Invalid('search session configuration changed; inspect before continuing')
    now = time.time()
    if now < data['started_at']:
        raise Invalid('clock precedes search session start; inspect before continuing')
    remaining = max(0., data['deadline'] - now)
    return {**data, 'status': 'search_budget_exhausted' if not remaining else 'search_session_active',
            'remaining_seconds': remaining,
            'scope': 'repository search includes selection, refresh and initial begin; only already-started investigations with a preserved checkout and native verification are separate'}


def allowance(cfg: Config) -> float:
    state = status(cfg)
    if state['status'] != 'search_session_active':
        raise Invalid(state['status'])
    return min(cfg.discovery_read_budget_seconds, state['remaining_seconds'])


def validate_existing(cfg: Config) -> None:
    """Validate recorded scope/integrity without requiring remaining search time.

    Legacy investigations may have no session record. This never starts one or
    authorizes a new search; allowance() still owns new repository reads.
    """
    if cfg.repo_first and load(cfg) is not None:
        status(cfg)
