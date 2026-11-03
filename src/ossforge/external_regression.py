"""Opt-in frozen external regression. Freshness evidence, not a sandbox."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import policy_review, evidence

KIND = 'python_external_regression'
SHA = re.compile(r'[0-9a-f]{64}')
LIMIT = 65536


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise ValueError('external regression requires absolute regular non-symlink files')
    if path.stat().st_size > LIMIT:
        raise ValueError('external regression exceeds 64 KiB')
    data = path.read_bytes()
    if not data:
        raise ValueError('external regression is empty')
    return data


def policy_binding(cfg, repo: str) -> str:
    why = policy_review.check(cfg, repo)
    if why:
        raise ValueError(why)
    rec = policy_review._load(cfg).get(repo.lower(), {})
    scope = rec.get('external_regression')
    if not isinstance(scope, dict) or set(scope) != {'path', 'sha', 'reason'} or not scope.get('reason', '').strip():
        raise ValueError('external regression requires explicit source-backed policy review')
    if not any(d['path'] == scope['path'] and d['sha'] == scope['sha'] for d in rec.get('docs', [])):
        raise ValueError('external regression policy document binding missing')
    return digest(json.dumps({'docs': rec['docs'], 'scope': scope, 'base': rec.get('contribution_base')}, sort_keys=True).encode())


def interpreter(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute() or not p.is_file():
        raise ValueError('external regression needs an absolute Python interpreter')
    # Virtualenv interpreter symlinks are common; bind the resolved executable too.
    resolved = p.resolve(strict=True)
    run = subprocess.run([str(p), '--version'], capture_output=True, text=True, timeout=10)
    version = run.stdout.strip()
    if run.returncode or not version.startswith('Python 3.'):
        raise ValueError('external regression interpreter is not Python 3')
    return {'path': str(p), 'resolved': str(resolved), 'sha256': digest(resolved.read_bytes()), 'version': version}


def artifact(cfg, sha: str) -> Path:
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError('invalid external regression artifact identity')
    root = cfg.path('external_regressions').absolute()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise ValueError('external regression state path contains symlinks')
    return root / (sha + '.py')


def runner(data: bytes, clone: Path, sources: list[str]) -> bytes:
    # An import observer, not a sandbox. Exclude site-packages so an installed
    # package cannot stand in for the checkout under test.
    ast.parse(data.decode('utf-8'))
    prefix = r"""import sys as _sys, pathlib as _path, hashlib as _hash, json as _json
_root = _path.Path.cwd().resolve()
_expected_root = __OSSFORGE_ROOT__
_sources = __OSSFORGE_SOURCES__
if str(_root) != _expected_root:
    raise RuntimeError('external regression cwd differs from checkout')
_sys.path[:] = [str(_root)] + [p for p in _sys.path if p and 'site-packages' not in p and 'dist-packages' not in p and _path.Path(p).resolve() != _path.Path(__file__).parent.resolve()]
try:
    exec(compile(__OSSFORGE_SCRIPT__, '<external-regression>', 'exec'), {'__name__': '__main__'})
finally:
    _loaded = {}
    for _module in list(_sys.modules.values()):
        _file = getattr(_module, '__file__', None)
        if _file:
            _resolved = _path.Path(_file).resolve()
            if _resolved.is_relative_to(_root) and _resolved.is_file():
                _loaded[str(_resolved.relative_to(_root))] = _hash.sha256(_resolved.read_bytes()).hexdigest()
    if not all(_source in _loaded for _source in _sources):
        _sys.stderr.write('external regression did not import every modified checkout source\n')
        _sys.exit(86)
    print('OSSFORGE_EXTERNAL_IMPORTS=' + _json.dumps({p: _loaded[p] for p in _sources}, sort_keys=True))
"""
    pieces = prefix.split('__OSSFORGE_ROOT__')
    tail = pieces[1].split('__OSSFORGE_SOURCES__')
    rest = tail[1].split('__OSSFORGE_SCRIPT__')
    return (pieces[0] + repr(str(clone)) + tail[0] + repr(sources) + rest[0] + repr(data.decode('utf-8')) + rest[1]).encode()


def freeze(cfg, repo: str, clone: Path, command: list[str], base: str) -> dict:
    if not isinstance(command, list) or len(command) != 2:
        raise ValueError('external regression command must be [absolute Python, absolute script]')
    source = Path(command[1])
    data = regular(source)
    root = clone.resolve(strict=True)
    if source.is_relative_to(root):
        raise ValueError('external regression must be outside source checkout')
    sources = evidence._git(clone, 'diff', '--name-only', '--no-renames', base + '..HEAD').splitlines()
    if not sources or any(not p.endswith('.py') for p in sources):
        raise ValueError('external regression initially supports existing Python source changes only')
    for rel in sources:
        evidence._git(clone, 'cat-file', '-e', base + ':' + rel)
    data = runner(data, root, sources)
    sha = digest(data)
    dest = artifact(cfg, sha)
    if dest.is_relative_to(root):
        raise ValueError('external regression state must be outside source checkout')
    binding = policy_binding(cfg, repo)
    runtime = interpreter(command[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        if regular(dest) != data:
            raise ValueError('external regression artifact collision')
    else:
        with dest.open('xb') as out:
            out.write(data)
    return {'schema': 1, 'repo': repo, 'sha256': sha, 'size': len(data), 'policy': binding,
            'interpreter': runtime, 'pycache': 'fresh_per_run_prefix', 'cwd': str(root), 'command': [runtime['path'], str(dest)]}


def check(cfg, clone: Path, rec: dict) -> Path:
    if not isinstance(rec, dict) or set(rec) != {'schema','repo','sha256','size','policy','interpreter','pycache','cwd','command'} or type(rec['schema']) is not int or rec['schema'] != 1:
        raise ValueError('external regression receipt malformed')
    if rec['pycache'] != 'fresh_per_run_prefix':
        raise ValueError('external regression requires fresh per-run bytecode cache')
    path = artifact(cfg, rec['sha256'])
    root = clone.resolve(strict=True)
    if rec['cwd'] != str(root) or path.is_relative_to(root):
        raise ValueError('external regression source root changed')
    data = regular(path)
    if digest(data) != rec['sha256'] or len(data) != rec['size']:
        raise ValueError('external regression artifact changed')
    if interpreter(rec['interpreter']['path']) != rec['interpreter']:
        raise ValueError('external regression interpreter changed')
    if rec['command'] != [rec['interpreter']['path'], str(path)]:
        raise ValueError('external regression command changed')
    if policy_binding(cfg, rec['repo']) != rec['policy']:
        raise ValueError('external regression policy changed')
    return path
