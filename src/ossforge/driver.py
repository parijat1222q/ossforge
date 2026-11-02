"""Public, model-neutral task/result contract for a local external coding driver.

No provider calls. Results are proposals, never trusted verification evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import evidence, submit, verify
from .queue import Queue


class Invalid(ValueError):
    pass


def task(queue: Queue, item_id: str | None = None) -> dict:
    candidates = sorted((i for i in queue.by_status('clean') if i.clone), key=lambda i: -i.rank)
    item = queue.items.get(item_id) if item_id else (candidates[0] if candidates else None)
    if not item or item.status != 'clean' or not item.clone:
        return {'schema': 1, 'status': 'no_work', 'reason': 'no clean cloned candidate; run discovery/hunt or review the queue'}
    clone = Path(item.clone)
    base = submit._base(str(clone))
    evidence.capture(clone, base, [])  # refuse dirty/detached candidates before spending a driver attempt
    return {'schema': 1, 'status': 'ready', 'id': item.id, 'repo': item.repo,
            'clone': str(clone.resolve()), 'base': base, 'candidate': item.detail,
            'path': item.path, 'line': item.line, 'notes': item.notes, 'source_issue': item.source_issue,
            'requirements': ['candidate is not proof of a bug', 'read repository contribution policy',
                             'check issues, pull requests and recent commits for duplicates',
                             'commit a regression test and minimal source fix on a new local branch',
                             'run repository-native checks; do not sign legal agreements or merge upstream'],
            'result_schema': {'schema': 1, 'id': item.id, 'base': base, 'branch': 'local-branch',
                              'test_command': ['test-executable', 'regression-test'],
                              'check_commands': [['lint-executable']], 'failure_code': 1,
                              'issue_title': 'specific problem', 'issue_body': 'reproduction and evidence',
                              'pr_title': 'specific fix', 'pr_body': 'fix and test evidence', 'consent_issue': 0},
            'next': 'agent-verify RESULT.json; then submit ID (dry run) or explicitly submit ID --yes'}


KEYS = {'schema', 'id', 'base', 'branch', 'test_command', 'check_commands', 'failure_code',
        'issue_title', 'issue_body', 'pr_title', 'pr_body', 'consent_issue'}


def parse(path: Path) -> dict:
    if path.stat().st_size > 262144:
        raise Invalid('result exceeds 256 KiB')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise Invalid('result must be UTF-8 JSON') from exc
    if not isinstance(data, dict) or set(data) != KEYS or type(data['schema']) is not int or data['schema'] != 1:
        raise Invalid('result must match exact schema 1 keys')
    for key in ('id', 'base', 'branch', 'issue_title', 'issue_body', 'pr_title', 'pr_body'):
        if not isinstance(data[key], str) or not data[key].strip() or '\x00' in data[key]:
            raise Invalid(f'{key} must be nonempty text without NUL')
    if type(data['failure_code']) is not int or data['failure_code'] not in (1, 101):
        raise Invalid('failure_code must be 1 or 101; inspect baseline failure, not just exit code')
    if type(data['consent_issue']) is not int or data['consent_issue'] < 0:
        raise Invalid('consent_issue must be a nonnegative integer')
    if not isinstance(data['check_commands'], list) or len(data['check_commands']) > 20:
        raise Invalid('check_commands must be a list of at most 20 argv lists')
    for command in [data['test_command'], *data['check_commands']]:
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v or '\x00' in v for v in command):
            raise Invalid('commands must be nonempty string argv lists, never shell strings')
    return data


def complete(queue: Queue, result: dict, *, isolation_mode: str = "none", readonly_paths: list[str] | None = None) -> dict:
    item = queue.items.get(result['id'])
    if not item or item.status != 'clean' or not item.clone:
        raise Invalid('result must target an existing clean cloned candidate')
    clone = Path(item.clone)
    base = submit._base(str(clone))
    if base != result['base']:
        raise Invalid('base changed; refresh task and recheck the patch')
    current = evidence.capture(clone, base, [])
    if current['branch'] != result['branch']:
        raise Invalid('result branch does not match active checkout')
    outcome = verify.verify_fix(clone, base, Path('unused'), [], test_command=result['test_command'],
                                check_commands=result['check_commands'], failure_code=result['failure_code'], isolation_mode=isolation_mode, readonly_paths=readonly_paths)
    if not outcome.ok:
        return {'schema': 1, 'status': 'verification_failed', 'id': item.id, 'log': outcome.log}
    queue.set(item.id, status='verified', branch=result['branch'], verification=outcome.evidence,
              issue_title=result['issue_title'], issue_body=result['issue_body'],
              pr_title=result['pr_title'], pr_body=result['pr_body'], consent_issue=result['consent_issue'])
    queue.save()
    return {'schema': 1, 'status': 'verified', 'id': item.id, 'evidence': outcome.evidence,
            'next': 'submit remains separate and rechecks policy, identity, duplicates and publication limits'}
