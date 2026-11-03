"""Explicit same-selected-source continuation. No search renewal or publication.

Caller holds run.lock, then publication.lock. Old source is never written.
Pending preparation and final archive/lead/token outcome each replace one durable
state file. External editors are unsupported; byte snapshots catch stale inputs.
"""
from __future__ import annotations

from urllib.parse import quote

import base64
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

from . import (gh, repo_access, repo_suitability, evidence, identity, preparation, repo_research as research,
               research_session, safety, policy_review)
from .config import Config
from .queue import Queue

Invalid = research.Invalid
TOKEN = re.compile(r'[A-Za-z0-9_-]{1,80}')
SHA = re.compile(r'[0-9a-f]{40}')


def _bytes(path: Path) -> str | None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Invalid('symlink state input; preserve it')
    try:
        return base64.b64encode(path.read_bytes()).decode('ascii')
    except FileNotFoundError:
        return None


def _raw_digest(value) -> str | None:
    if value is None:
        return None
    _raw_review(value)
    return hashlib.sha256(base64.b64decode(value)).hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()



def _config_identity(cfg: Config) -> str:
    return _digest({'search_identity': research_session._identity(cfg), 'git_name': cfg.git_name,
                    'git_email': cfg.git_email, 'kill_file': str(Path(cfg.kill_file).absolute()),
                    'policy_knobs': [cfg.verification_isolation, cfg.verification_readonly_paths]})

def _plain(path: Path) -> Path:
    path = path.absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise Invalid('continuation destination has symlink or traversal; preserve it')
    return path


def _inputs(cfg: Config) -> dict:
    """Immutable-at-promotion relevant state, including terminal remote effects."""
    names = ['queue.json', 'research_session.json', 'research_source_review.json',
             'policy_reviews.json', 'local_preparation.json', 'run.json', 'audit.jsonl',
             'daily.json', 'driver-attempts.json', 'ratelimit_until']
    result = {n: _bytes(cfg.path(n)) for n in names}
    for folder in ('runs', 'driver-jobs'):
        root = _plain(cfg.path(folder))
        if root.exists():
            # Task/result receipts belong to running or completed local work.
            # A bounded directory walk refuses huge/unknown state, never truncates.
            paths = list(root.rglob('*'))
            if len(paths) > 2000:
                raise Invalid('continuation journal coverage exceeds bound; inspect it')
            for p in sorted(paths):
                _plain(p)
                if p.is_file():
                    result[str(p.relative_to(Path(cfg.state_dir)))] = _bytes(p)
    return result



def _queue_items(cfg):
    from . import discovery
    for item in Queue(cfg.path('queue.json')).items.values():
        if not isinstance(item.repo, str) or not discovery._REPO.fullmatch(item.repo) or not isinstance(item.submitted, dict):
            raise Invalid('queue repository/publication progress type invalid')
        for key in ('pr_url', 'pr', 'issue_url', 'issue'):
            if key in item.submitted and (not isinstance(item.submitted[key], str) or not item.submitted[key].startswith('https://')):
                raise Invalid('queue publication URL type invalid; inspect progress')
        yield item

def _preconditions(cfg: Config, lead: dict) -> None:
    safety.kill_switch(cfg)
    research_session.validate_existing(cfg)
    if not lead or lead.get('status') != 'investigating' or not lead.get('clone'):
        raise Invalid('another investigation requires an already selected investigating source')
    prep = preparation._load(cfg)
    if prep and prep['repo'].lower() == lead['repo'].lower():
        raise Invalid('same-source local preparation remains held; preserve it')
    for item in _queue_items(cfg):
        if not isinstance(item.submitted, dict):
            raise Invalid('publication progress invalid; inspect queue')
        if item.submitted and not (item.submitted.get('pr_url') or item.submitted.get('pr')):
            raise Invalid('partial publication needs reconciliation before continuation')
        if item.repo.lower() == lead['repo'].lower() and item.status in {'found', 'clean', 'verified', 'held'}:
            raise Invalid('same-source active work must be resolved before continuation')
    # The current journal can represent unresolved activity. Historical journals
    # are preserved evidence, not perpetual holds on unrelated later sessions.
    path = cfg.path('run.json')
    if path.exists():
        raw = _bytes(path)
        try:
            record = json.loads(base64.b64decode(raw).decode('utf-8'))
        except (OSError, ValueError, UnicodeError) as exc:
            raise Invalid('run journal unreadable; preserve it') from exc
        _run_record(record)
        if record['status'] in {'running', 'interrupted', 'failed', 'driver_timeout', 'driver_error', 'driver_failed'}:
            ack = research.load(cfg).get('continuation_run_review')
            if not _valid_run_review(ack, raw, cfg):
                raise Invalid('unresolved current run journal; inspect before explicit reconciliation')


def _target(cfg: Config, lead: dict) -> dict:
    # One same-source bounded read phase. No discovery, health scoring or
    # acceptance resampling, and never research_session.allowance().
    with gh.read_budget(cfg.discovery_read_budget_seconds):
        restriction = repo_access.pr_restriction(lead['repo'])
        if restriction:
            raise Invalid(restriction)
        info = gh.api(f"repos/{lead['repo']}", fresh=True)
        if (not isinstance(info, dict) or type(info.get('id')) is not int or info['id'] <= 0
                or str(info.get('full_name', '')).lower() != lead['repo'].lower()
                or info.get('html_url') != f"https://github.com/{info.get('full_name')}"
                or not isinstance(info.get('default_branch'), str)):
            raise Invalid('same-source stable repository identity unavailable')
        default_branch = info['default_branch']
        recorded = policy_review._load(cfg).get(lead['repo'].lower(), {})
        name = policy_review.reviewed_branch(recorded)
        branch = name or default_branch
        if ((lead.get('contribution_base') or {}).get('branch') if lead.get('contribution_base') else None) != name:
            raise Invalid('reviewed contribution branch changed; preserve selected source')
        if subprocess.run(['git', 'check-ref-format', '--branch', branch], capture_output=True).returncode:
            raise Invalid('selected default branch malformed')
        tip = gh.api(f"repos/{lead['repo']}/commits/{quote(branch, safe='')}", fresh=True)
        if (not isinstance(tip, dict) or not SHA.fullmatch(str(tip.get('sha', '')))
                or tip.get('html_url') != f"https://github.com/{info['full_name']}/commit/{tip['sha']}"):
            raise Invalid('selected source revision unavailable')
        old_ident = lead.get('repository_identity', {})
        if not isinstance(old_ident, dict):
            raise Invalid('recorded stable repository identity malformed')
        old_id = old_ident.get('id')
        if old_id is not None and old_id != info['id']:
            raise Invalid('stable repository identity changed; preserve selected source')
        # Legacy sources have no historical stable-ID assertion. Bind today's
        # public identity only after checking the old source's actual origin.
        if identity.check_upstream(lead['clone'], lead['repo']):
            raise Invalid('old selected source origin identity changed')
        recorded = policy_review._load(cfg).get(lead['repo'].lower(), {})
        extras = recorded.get('extra_paths', [])
        snapshot = policy_review.snapshot(lead['repo'], extra_paths=extras, **({'ref': name} if name is not None else {}))
        if name is not None and (snapshot.get('contribution_base') != recorded['contribution_base'] or tip['sha'] != recorded['contribution_base']['revision']):
            raise Invalid('reviewed contribution branch moved; re-review before continuation')
        if snapshot.get('anti_ai') or snapshot.get('agent_review'):
            raise Invalid('contribution policy restriction; preserve selected source')
        why = policy_review.check(cfg, lead['repo'], snapshot)
        if why:
            raise Invalid(why)
        route = recorded.get('publication_route')
        if route not in {'issue_pr', 'pr_only'}:
            raise Invalid('coding prerequisites unresolved')
        suitability = repo_suitability.assess(lead['repo'], tip['sha'], snapshot)
        if suitability.get('status') != 'native_candidate':
            raise Invalid('same-source native suitability unresolved')
    blocked = safety.repo_allowed(cfg, lead['repo'], safety.effective_history(safety.Audit(cfg.path('audit.jsonl')).read()))
    if blocked:
        raise Invalid(blocked)
    result = {**lead, 'revision': tip['sha'], 'revision_url': tip['html_url'], 'default_branch': default_branch,
            'contribution_base': recorded.get('contribution_base'),
            'policy': snapshot, 'code_suitability': suitability,
            'repository_identity': {'id': info['id'], 'full_name': info['full_name'], 'url': info['html_url'],
                'provenance': 'current same-source check; not a historical certification of legacy work' if old_id is None else 'matches recorded stable repository id'}}

    if result.get('contribution_base') is None:
        result.pop('contribution_base', None)
    return result


def _pin(lead: dict) -> dict:
    return {k: lead[k] for k in ('repo', 'revision', 'default_branch', 'policy', 'repository_identity')} | ({'contribution_base': lead['contribution_base']} if lead.get('contribution_base') else {})


def _git(root: Path, *args) -> str:
    try:
        p = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Invalid('continuation Git inspection failed; preserve checkout') from exc
    if p.returncode:
        raise Invalid('continuation Git inspection failed; preserve checkout: ' + p.stderr[-500:])
    return p.stdout.strip()


def _marker_read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise Invalid('clone ownership marker unreadable; preserve it') from exc
    if not isinstance(value, dict):
        raise Invalid('clone ownership marker invalid; preserve it')
    return value


def _clone(cfg: Config, target: dict, dest: Path) -> None:
    """Only a new independent clone. Timeout keeps exact partial destination."""
    _plain(dest)
    if dest.exists():
        return  # Validate exact existing partial/completed destination, no reset.
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(['git', 'clone', '--depth', '50', '--branch', (target.get('contribution_base') or {}).get('branch', target['default_branch']),
                        '--single-branch', '--', f"https://github.com/{target['repo']}", str(dest)],
                       check=True, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Invalid(f'clone incomplete; preserve {dest} and retry this exact token after inspection') from exc
    problems = identity.apply_local(cfg, str(dest))
    if problems:
        raise Invalid('new checkout identity unresolved: ' + '; '.join(problems))


def _validate_clone(cfg: Config, target: dict, dest: Path, old: dict) -> dict:
    _plain(dest)
    old_root = Path(old['clone']).absolute()
    if (not dest.is_dir() or not (dest / '.git').is_dir() or (dest / '.git').is_symlink()
            or dest == old_root or dest.is_relative_to(old_root) or old_root.is_relative_to(dest)):
        raise Invalid('continuation needs an independent Git directory separate from old source')
    if Path(_git(dest, 'rev-parse', '--show-toplevel')).resolve() != dest.resolve():
        raise Invalid('continuation checkout is not the exact Git root')
    if (_git(dest, 'rev-parse', '--is-shallow-repository') not in {'true', 'false'}
            or _git(dest, 'rev-parse', '--git-common-dir') != '.git'):
        raise Invalid('linked/shared Git checkout unsupported')
    if (dest / '.git/objects/info/alternates').exists() or (dest / '.git/objects/info/http-alternates').exists():
        raise Invalid('shared object storage unsupported; preserve checkout')
    if any(p.is_symlink() for p in (dest / '.git').rglob('*')):
        raise Invalid('symlink Git storage unsupported; preserve checkout')
    remote = _git(dest, 'config', '--get', 'remote.origin.url')
    if remote != f"https://github.com/{target['repo']}":
        raise Invalid('continuation origin does not match exact selected repository')
    if (_git(dest, 'rev-parse', 'HEAD') != target['revision']
            or _git(dest, 'symbolic-ref', '--short', 'HEAD') != (target.get('contribution_base') or {}).get('branch', target['default_branch'])):
        raise Invalid('continuation SHA/branch differs from pinned target; never reset')
    problems = identity.check_worktree(cfg, str(dest))
    if problems:
        raise Invalid('checkout identity differs: ' + '; '.join(problems))
    preparation.public_guard(dest)
    return evidence.capture(dest, target['revision'], [], **({'contribution_base': target['contribution_base']} if target.get('contribution_base') else {}))


def _raw_review(value) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise Invalid('archived raw review type invalid')
    try:
        base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise Invalid('archived raw review encoding invalid') from exc


def _lead(value, *, target=False) -> None:
    from . import discovery
    if (not isinstance(value, dict) or not isinstance(value.get('repo'), str)
            or not discovery._REPO.fullmatch(value['repo'])
            or not isinstance(value.get('revision'), str) or not SHA.fullmatch(value['revision'])
            or not isinstance(value.get('default_branch'), str) or not value['default_branch']
            or not isinstance(value.get('clone'), str) or not value['clone']
            or not isinstance(value.get('base'), str) or not SHA.fullmatch(value['base'])
            or not isinstance(value.get('checkout'), dict)):
        raise Invalid('continuation nested source identity invalid')
    from . import contribution_base
    contribution_base.validate(value.get('contribution_base'), value['repo'], explicit='contribution_base' in value)
    if value.get('contribution_base') and value['contribution_base']['revision'] != value['revision']:
        raise Invalid('contribution base pin differs from selected revision')
    if target:
        ident = value.get('repository_identity')
        if (not isinstance(ident, dict) or type(ident.get('id')) is not int or ident['id'] <= 0
                or str(ident.get('full_name', '')).lower() != value['repo'].lower()
                or ident.get('url') != f"https://github.com/{ident.get('full_name')}"
                or not isinstance(value.get('policy'), dict)):
            raise Invalid('continuation stable target identity invalid')


def _request(value, token) -> None:
    if (not isinstance(value, dict) or set(value) != {'config_identity', 'token'}
            or value.get('token') != token or not isinstance(value.get('config_identity'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['config_identity'])):
        raise Invalid('continuation request identity invalid')


def _history(data: dict) -> dict:
    outcomes = data.get('continuation_outcomes', {})
    if not isinstance(outcomes, dict):
        raise Invalid('continuation history invalid; preserve it')
    for token, saved in outcomes.items():
        keys = {'schema', 'request', 'old_lead', 'old_review', 'old_review_sha256', 'old_identity',
                'target', 'destination', 'outcome', 'fingerprint'}
        if (not isinstance(token, str) or not TOKEN.fullmatch(token) or not isinstance(saved, dict)
                or set(saved) != keys or saved.get('schema') != 1):
            raise Invalid('completed continuation record invalid')
        _request(saved['request'], token)
        _lead(saved['old_lead'])
        _lead(saved['target'], target=True)
        _raw_review(saved['old_review'])
        if (saved['old_review_sha256'] != _raw_digest(saved['old_review'])
                or saved['fingerprint'] != _digest({k:v for k,v in saved.items() if k != 'fingerprint'})
                or saved['old_identity'] != (saved['old_lead'].get('research_id') or 'legacy-unbound')
                or not isinstance(saved['destination'], str) or not Path(saved['destination']).is_absolute() or '..' in Path(saved['destination']).parts
                or not isinstance(saved['outcome'], dict)):
            raise Invalid('completed continuation archive fingerprint invalid')
        out = saved['outcome']
        if (set(out) != {'status', 'transition_token', 'research_id', 'repo', 'clone', 'base', 'warning'}
                or out['status'] != 'repo_investigation' or out['transition_token'] != token
                or not isinstance(out['research_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', out['research_id'])
                or out['repo'] != saved['target']['repo'] or out['clone'] != saved['destination']
                or out['base'] != saved['target']['revision'] or not isinstance(out['warning'], str)):
            raise Invalid('completed continuation outcome identity invalid')
    abandoned = data.get('continuation_abandoned', {})
    if not isinstance(abandoned, dict):
        raise Invalid('abandoned transition history invalid')
    for token, record in abandoned.items():
        if (not isinstance(token, str) or not TOKEN.fullmatch(token) or not isinstance(record, dict)
                or set(record) != {'schema', 'token', 'request', 'pending', 'observed', 'reason', 'outcome', 'fingerprint'}
                or record.get('schema') != 1 or record.get('token') != token
                or not isinstance(record.get('reason'), str) or not record['reason'].strip()
                or record.get('fingerprint') != _digest({k:v for k,v in record.items() if k != 'fingerprint'})
                or not isinstance(record.get('pending'), dict) or not isinstance(record.get('observed'), dict)
                or record.get('outcome') != {'status': 'research_continuation_abandoned', 'transition_token': token,
                    'destination': record['pending'].get('destination'), 'reason': record['reason']}):
            raise Invalid('abandoned transition archive invalid')
        _request(record['request'], token)
        _validate_pending(record['pending'])
        if record['pending']['token'] != token or record['pending']['request'] != record['request']:
            raise Invalid('abandoned pending request mismatch')
        observed = record['observed']
        if set(observed) != {'lead', 'review', 'inputs', 'destination_status'} or not isinstance(observed['inputs'], dict) or observed['destination_status'] not in ('missing', 'unknown-preserved'):
            raise Invalid('abandoned observed state invalid')
        _lead(observed['lead'])
        _raw_review(observed['review'])
        for key, raw in observed['inputs'].items():
            if not isinstance(key, str) or Path(key).is_absolute() or '..' in Path(key).parts:
                raise Invalid('abandoned input path invalid')
            _raw_review(raw)
    tokens = set(outcomes) & set(abandoned)
    p = data.get('continuation_pending')
    if p is not None:
        if not isinstance(p, dict) or not isinstance(p.get('token'), str):
            raise Invalid('pending token invalid')
        if p['token'] in outcomes or p['token'] in abandoned:
            tokens.add(p['token'])
    if tokens:
        raise Invalid('transition token overlaps pending/completed/abandoned states')
    review = data.get('continuation_run_review')
    history = data.get('continuation_run_review_history', [])
    if not isinstance(history, list):
        raise Invalid('run inspection history invalid')
    for r in ([review] if review is not None else []) + history:
        if not _run_review_shape(r):
            raise Invalid('run inspection archive invalid')
    return outcomes


def _validate_pending(value, cfg=None, current=None):
    keys = {'schema', 'token', 'request', 'old_lead', 'old_review', 'old_review_sha256', 'inputs',
            'research_id', 'target', 'destination', 'phase', 'fingerprint'}
    if (not isinstance(value, dict) or set(value) != keys or value.get('schema') != 1
            or not isinstance(value.get('token'), str) or not TOKEN.fullmatch(value['token'])
            or value.get('phase') not in ('planned', 'clone_started', 'clone_validated')):
        raise Invalid('pending continuation journal invalid; preserve it')
    _request(value['request'], value['token'])
    _lead(value['old_lead'])
    _lead(value['target'], target=True)
    _raw_review(value['old_review'])
    if (value['old_review_sha256'] != _raw_digest(value['old_review'])
            or value.get('fingerprint') != _digest({k:v for k,v in value.items() if k not in {'fingerprint', 'phase'}})
            or (current is not None and value['old_lead'] != current) or not isinstance(value['research_id'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['research_id'])
            or (cfg is not None and value['destination'] != str(Path(cfg.work_dir).absolute() / 'investigations' / value['research_id']))
            or not isinstance(value['destination'], str) or not Path(value['destination']).is_absolute()
            or '..' in Path(value['destination']).parts or not isinstance(value['inputs'], dict) or value['target']['repo'] != value['old_lead']['repo']):
        raise Invalid('pending continuation identity changed; preserve it')
    for key, raw in value['inputs'].items():
        if not isinstance(key, str) or Path(key).is_absolute() or '..' in Path(key).parts:
            raise Invalid('pending input path invalid')
        _raw_review(raw)
    return value


def pending(cfg: Config, *, require_current: bool = True) -> dict | None:
    data = research.load(cfg)
    _history(data)
    value = data.get('continuation_pending')
    if value is None:
        return None
    _validate_pending(value, cfg, data['lead'] if require_current else None)
    return value


def another(cfg: Config, token: str) -> dict:
    # A single cooperative deadline across all fresh checks, never three renewed
    # budgets. Clone time counts; late completion remains pending, not promoted.
    with gh.read_budget(cfg.discovery_read_budget_seconds):
        return _another(cfg, token)


def _another(cfg: Config, token: str) -> dict:
    """Caller owns common run and publication exclusion through completion."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise Invalid('transition token must be 1..80 letters, digits, underscore or hyphen; not a path')
    data = research.load(cfg)
    request = {'config_identity': _config_identity(cfg), 'token': token}
    outcomes = _history(data)
    abandoned = data.get('continuation_abandoned', {}).get(token)
    if abandoned:
        if abandoned['request'] != request:
            raise Invalid('abandoned token belongs to another request')
        return abandoned['outcome']
    if token in outcomes:
        saved = outcomes[token]
        if saved.get('request') != request:
            raise Invalid('completed token belongs to a different request/configuration')
        return saved['outcome']  # No live read or allocation on a committed retry.
    p = pending(cfg)
    if p and (p['token'] != token or p['request'] != request):
        raise Invalid('another or changed continuation request is pending; retry its exact token')
    lead = data['lead']
    _lead(lead)
    _preconditions(cfg, lead)
    if not p:
        target = _target(cfg, lead)
        fresh_id = uuid.uuid4().hex
        p = {'schema': 1, 'token': token, 'request': request, 'old_lead': lead,
             'old_review': _bytes(cfg.path('research_source_review.json')),
             'old_review_sha256': _raw_digest(_bytes(cfg.path('research_source_review.json'))), 'inputs': _inputs(cfg),
             'research_id': fresh_id, 'target': target,
             'destination': str(Path(cfg.work_dir).absolute() / 'investigations' / fresh_id), 'phase': 'planned'}
        p['fingerprint'] = _digest({k: v for k, v in p.items() if k != 'phase'})
        data['continuation_pending'] = p
        research._save(cfg, data)  # Durable identity BEFORE directory creation.
    if p['inputs'] != _inputs(cfg):
        raise Invalid('continuation input state changed; preserve pending checkpoint')
    current = _target(cfg, lead)
    if _pin(current) != _pin(p['target']):
        raise Invalid('selected tip/branch/policy changed during continuation; do not repin pending token')
    safety.kill_switch(cfg)
    dest = _plain(Path(p['destination']))
    marker = _plain(dest.with_name(dest.name + '.owner.json'))
    owner = {'schema': 1, 'token': token, 'fingerprint': p['fingerprint'], 'destination': str(dest)}
    if p['phase'] == 'planned':
        if dest.exists():
            raise Invalid('unexpected destination before clone ownership; preserve it')
        if marker.exists():
            if _marker_read(marker) != owner:
                raise Invalid('clone ownership marker differs')
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            import os
            with marker.open('x') as out:
                out.write(json.dumps(owner))
                out.flush()
                os.fsync(out.fileno())
            fd = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    else:
        if not marker.is_file() or _marker_read(marker) != owner:
            raise Invalid('pending checkout lacks matching ownership marker; preserve it')
    p['phase'] = 'clone_started'
    research._save(cfg, data)
    _clone(cfg, p['target'], dest)
    safety.kill_switch(cfg)
    checkout = _validate_clone(cfg, p['target'], dest, lead)
    if research.load(cfg) != data:
        raise Invalid('research state changed during clone; preserve pending checkpoint')
    p['phase'] = 'clone_validated'
    research._save(cfg, data)
    # Bracket slow clone with fresh preconditions and same exact pin checks.
    _preconditions(cfg, lead)
    if (_pin(_target(cfg, lead)) != _pin(p['target']) or p['inputs'] != _inputs(cfg)):
        raise Invalid('source or input state changed before promotion; preserve pending checkout')
    if research.load(cfg) != data:
        raise Invalid('research state changed before commit; preserve pending checkpoint')
    checkout = _validate_clone(cfg, p['target'], dest, lead)
    safety.kill_switch(cfg)
    outcome = {'status': 'repo_investigation', 'transition_token': token, 'research_id': p['research_id'],
               'repo': lead['repo'], 'clone': str(dest), 'base': p['target']['revision'],
               'warning': 'new source context only; no bug, contribution, publication or renewed search permission'}
    new_lead = {k: v for k, v in p['target'].items() if k not in {'clone', 'base', 'checkout', 'hints', 'scan', 'native_review', 'next'}}
    new_lead.update(status='investigating', research_id=p['research_id'], clone=str(dest),
                    base=p['target']['revision'], checkout=checkout)
    outcomes[token] = {'schema': 1, 'request': request, 'old_lead': lead, 'old_review': p['old_review'],
                       'old_review_sha256': p['old_review_sha256'], 'target': p['target'], 'destination': str(dest),
                       'old_identity': lead.get('research_id') or 'legacy-unbound', 'outcome': outcome}
    outcomes[token]['fingerprint'] = _digest(outcomes[token])
    data.update(lead=new_lead, continuation_outcomes=outcomes)
    del data['continuation_pending']
    gh.read_remaining()  # Cooperative deadline immediately before promotion.
    research._save(cfg, data)  # Sole commit: archive + active pointer + token outcome.
    return outcome


def guard(cfg: Config) -> None:
    if pending(cfg):
        raise Invalid('source continuation pending; retry its exact token, preserve old state')


def abandon(cfg: Config, token: str, reason: str) -> dict:
    """Preserve current state, archive pending context, never restore old inputs."""
    safety.kill_switch(cfg)
    if not isinstance(token, str) or not TOKEN.fullmatch(token) or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000 or '\x00' in reason:
        raise Invalid('abandon needs exact token and bounded source-backed reason')
    data = research.load(cfg)
    _history(data)
    existing = data.get('continuation_abandoned', {}).get(token)
    if existing:
        if existing['reason'] != reason.strip() or existing['request']['config_identity'] != _config_identity(cfg):
            raise Invalid('abandoned retry request differs')
        return existing['outcome']
    p = pending(cfg, require_current=False)
    if not p or p['token'] != token or p['request']['config_identity'] != _config_identity(cfg):
        raise Invalid('no exact valid pending transition to abandon')
    dest = Path(p['destination'])
    # Do not walk/adopt a changed destination; record only bounded status.
    status = 'missing' if not dest.exists() and not dest.is_symlink() else 'unknown-preserved'
    outcome = {'status': 'research_continuation_abandoned', 'transition_token': token,
               'destination': str(dest), 'reason': reason.strip()}
    record = {'schema': 1, 'token': token, 'request': p['request'], 'pending': p,
              'observed': {'lead': data['lead'], 'review': _bytes(cfg.path('research_source_review.json')),
                           'inputs': _inputs(cfg), 'destination_status': status},
              'reason': reason.strip(), 'outcome': outcome}
    record['fingerprint'] = _digest(record)
    data.setdefault('continuation_abandoned', {})[token] = record
    del data['continuation_pending']
    research._save(cfg, data)
    return outcome


def _run_record(record) -> None:
    if (not isinstance(record, dict) or record.get('schema') != 1
            or not isinstance(record.get('id'), str) or not TOKEN.fullmatch(record['id'])
            or not isinstance(record.get('status'), str) or not isinstance(record.get('events'), list)
            or any(not isinstance(e, dict) or not isinstance(e.get('message'), str) for e in record['events'])):
        raise Invalid('current run journal invalid; preserve it')


def _run_review_shape(review) -> bool:
    if not isinstance(review, dict) or not isinstance(review.get('raw'), str):
        return False
    try:
        _raw_review(review['raw'])
        _run_record(json.loads(base64.b64decode(review['raw']).decode('utf-8')))
    except (Invalid, ValueError, UnicodeError):
        return False
    return (isinstance(review, dict) and set(review) == {'schema', 'raw', 'digest', 'config_identity', 'reason', 'fingerprint'}
            and review.get('schema') == 1 and isinstance(review.get('raw'), str) and review.get('digest') == _raw_digest(review['raw'])
            and isinstance(review.get('config_identity'), str)
            and isinstance(review.get('reason'), str) and bool(review['reason'].strip())
            and review.get('fingerprint') == _digest({k:v for k,v in review.items() if k != 'fingerprint'}))


def _valid_run_review(review, raw, cfg) -> bool:
    return _run_review_shape(review) and review['raw'] == raw and review['config_identity'] == _config_identity(cfg)


def reconcile_run(cfg: Config, reason: str) -> dict:
    safety.kill_switch(cfg)
    research_session.validate_existing(cfg)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000 or '\x00' in reason:
        raise Invalid('run reconciliation needs bounded inspection reason')
    data = research.load(cfg)
    if pending(cfg):
        raise Invalid('abandon pending transition before reconciling run state')
    lead = data['lead']
    _lead(lead)
    # Other blockers cannot be acknowledged away. Reuse preconditions while
    # excluding only the current journal condition that this route reviews.
    raw = _bytes(cfg.path('run.json'))
    if raw is None:
        raise Invalid('no current run journal to reconcile')
    try:
        record = json.loads(base64.b64decode(raw).decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise Invalid('current run journal unreadable') from exc
    _run_record(record)
    messages = [e['message'] for e in record['events']]
    started = any(m.startswith('driver: starting ') for m in messages)
    exited = any(m.startswith('driver exited ') for m in messages)
    if record['status'] == 'running' or (started and not exited):
        raise Invalid('driver lifetime unresolved; journal review cannot establish child termination')
    _target(cfg, lead)
    # No unverified changed source is hidden by a journal acknowledgement.
    if evidence.capture(Path(lead['clone']), lead['base'], [], **({'contribution_base': lead['contribution_base']} if lead.get('contribution_base') else {})) != lead['checkout']:
        raise Invalid('selected checkout changed; preserve it before run reconciliation')
    review = {'schema': 1, 'raw': raw, 'digest': _raw_digest(raw), 'config_identity': _config_identity(cfg), 'reason': reason.strip()}
    review['fingerprint'] = _digest(review)
    original = data.get('continuation_run_review')
    data['continuation_run_review'] = review
    # Validate everything else against the tentative exact acknowledgement,
    # without publishing it or touching the current journal.
    real_load = research.load
    # Other operational holds remain independent of the journal assertion.
    prep = preparation._load(cfg)
    if prep and prep['repo'].lower() == lead['repo'].lower():
        raise Invalid('same-source preparation remains held')
    for item in _queue_items(cfg):
        if item.submitted and not (item.submitted.get('pr_url') or item.submitted.get('pr')):
            raise Invalid('partial publication needs reconciliation separately')
        if item.repo.lower() == lead['repo'].lower() and item.status in {'found', 'clean', 'verified', 'held'}:
            raise Invalid('same-source active work remains')
    safety.kill_switch(cfg)
    if _bytes(cfg.path('run.json')) != raw or real_load(cfg).get('lead') != lead:
        raise Invalid('run/source changed during inspection; repeat reconciliation')
    if original:
        data.setdefault('continuation_run_review_history', []).append(original)
    research._save(cfg, data)
    return {'status': 'current_run_inspection_recorded', 'run_id': record['id'],
            'warning': 'local inspection assertion only; not remote reconciliation, child termination or source certification'}
