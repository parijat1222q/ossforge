"""Explicit source-reviewed contribution base, separate from repository default.

An absent legacy identity retains old default behavior. Partial explicit identity is
never a request to fall back. This is source binding, not publication authority.
"""
from __future__ import annotations

import re
import subprocess
from urllib.parse import quote

SHA = re.compile(r'[0-9a-f]{40}')


def branch(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or value in {'HEAD', '@'} or value.startswith(('-', 'refs/', '@{'))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or subprocess.run(['git', 'check-ref-format', '--branch', value], capture_output=True).returncode):
        raise ValueError('contribution branch must be an exact valid bare branch name')
    return value


def validate(value, repo: str, *, explicit: bool = False) -> dict | None:
    if value is None and not explicit:
        return None
    if (not isinstance(value, dict) or set(value) != {'repo', 'branch', 'revision'}
            or not isinstance(value.get('repo'), str) or not isinstance(repo, str) or value['repo'].lower() != repo.lower() or not isinstance(value.get('revision'), str)
            or not SHA.fullmatch(value['revision'])):
        raise ValueError('explicit contribution base identity incomplete or malformed')
    branch(value.get('branch'))
    return dict(value)


def of(item) -> dict:
    return validate(item.contribution_base, item.repo)


def ref_name(value: dict | None) -> str:
    return 'refs/remotes/origin/' + value['branch']


def commit_path(repo: str, name: str) -> str:
    return f'repos/{repo}/commits/{quote(branch(name), safe="")}'


def bind(receipt: dict, value: dict | None) -> dict:
    if value:
        receipt['contribution_base'] = dict(value)
    return receipt


def check(receipt: dict, value: dict | None) -> None:
    if value:
        if receipt.get('contribution_base') != value:
            raise ValueError('contribution base identity changed or missing from receipt; re-run verification')
    elif 'contribution_base' in receipt:
        raise ValueError('explicit contribution base cannot downgrade to legacy receipt')
