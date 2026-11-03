"""Opt-in offline native environment setup, not automatic dependency resolution.

Recipes are operator-reviewed execution requests, never trusted repository instructions.
Only a new environment directory is writable; setup does not verify a fix or publish.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

from . import evidence, isolation, preparation, safety, policy_review
from .config import Config
from .queue import Queue


class Invalid(ValueError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    if path.stat().st_size > 262144:
        raise Invalid('environment recipe exceeds 256 KiB')
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise Invalid('environment recipe must be UTF-8 JSON') from exc
    if not isinstance(value, dict):
        raise Invalid('environment recipe must be an object')
    return value


def _plain_path(raw: str) -> Path:
    if not isinstance(raw, str):
        raise Invalid('environment paths must be absolute non-symlink paths')
    path = Path(raw)
    if not path.is_absolute() or not path.exists() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise Invalid('environment paths must be existing absolute non-symlink paths')
    return path.resolve()


def _recipe(path: Path, clone: Path) -> tuple[dict, Path]:
    return _validate_recipe(_read(path), clone)


def _validate_recipe(data: dict, clone: Path) -> tuple[dict, Path]:
    if set(data) != {'schema', 'note', 'sources', 'wheelhouse', 'wheels', 'commands'} or type(data['schema']) is not int or data['schema'] != 1:
        raise Invalid('environment recipe must match exact schema 1 keys')
    if not isinstance(data['note'], str) or not data['note'].strip() or len(data['note']) > 4000:
        raise Invalid('explain the actual setup sources, scope and remaining prerequisites')
    sources = data['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
        raise Invalid('cite 1..20 actual source files with sha256')
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'path', 'sha256'}:
            raise Invalid('each setup source needs exact path and sha256')
        rel = Path(source['path']) if isinstance(source['path'], str) else Path('/')
        p = clone / rel
        if (rel.is_absolute() or '..' in rel.parts or not p.is_file() or p.is_symlink()
                or any(parent.is_symlink() for parent in p.parents)
                or not p.resolve().is_relative_to(clone) or _digest(p) != source['sha256']):
            raise Invalid('setup source changed or is not a real file inside the checkout')
    wheelhouse = _plain_path(data['wheelhouse'])
    # Use the verification mount validator for broad/private/checkout overlap checks.
    isolation.wrap(['/usr/bin/true'], clone, 'bwrap', [str(wheelhouse)])
    wheels = data['wheels']
    if not isinstance(wheels, dict) or not wheels or len(wheels) > 500:
        raise Invalid('explicit nonempty wheel filename/sha256 manifest required')
    actual = {}
    for p in wheelhouse.iterdir():
        if not p.is_file() or p.is_symlink() or p.suffix != '.whl':
            raise Invalid('wheelhouse must contain only regular .whl files')
        actual[p.name] = _digest(p)
    if actual != wheels:
        raise Invalid('wheelhouse content no longer matches manifest')
    commands = data['commands']
    if not isinstance(commands, list) or not 1 <= len(commands) <= 20:
        raise Invalid('recipe needs 1..20 explicit setup argv lists')
    for cmd in commands:
        if (not isinstance(cmd, list) or not cmd or cmd[0] != '{python}'
                or any(not isinstance(v, str) or not v or '\x00' in v for v in cmd)):
            raise Invalid('setup commands must be nonempty argv starting with {python}; no shell')
        for value in cmd:
            if re.search(r'\{(?!python\}|environment\}|clone\}|wheelhouse\})', value):
                raise Invalid('unknown environment recipe placeholder')
    return data, wheelhouse


def _item(queue: Queue, item_id: str, *, inspect: bool = False):
    item = queue.items.get(item_id)
    allowed = {'clean', 'verified'} if inspect else {'clean'}
    if not item or item.status not in allowed or not item.clone or item.submitted:
        raise Invalid('environment setup needs clean unpublished queued work')
    clone = _plain_path(str(Path(item.clone).absolute()))
    preparation.public_guard(clone)
    return item, clone


def _wrap(command: list[str], clone: Path, wheelhouse: Path, folder: Path) -> list[str]:
    args = isolation.wrap(command, clone, 'bwrap', [str(wheelhouse)])
    # The generated folder is the only writable host mount. Never accept it from a recipe.
    pos = args.index('--')
    return args[:pos] + ['--bind', str(folder), str(folder)] + args[pos:]


def prepare(cfg: Config, queue: Queue, item_id: str, recipe_file: Path, timeout: int = 600) -> dict:
    safety.kill_switch(cfg)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        raise Invalid('setup whole-process timeout must be 1..1800 seconds')
    item, clone = _item(queue, item_id)
    why = policy_review.check(cfg, item.repo)
    if why:
        raise Invalid(why)
    if policy_review.publication_route(cfg, item.repo) not in {'issue_pr', 'pr_only'}:
        raise Invalid('setup requires resolved contribution prerequisites')
    base = evidence._git(clone, 'merge-base', 'HEAD', 'origin/HEAD')
    snapshot = evidence.capture(clone, base, [])
    recipe, wheelhouse = _recipe(recipe_file, clone)
    parent = Path(cfg.work_dir).absolute() / 'environments'
    if parent.is_symlink() or any(p.is_symlink() for p in parent.parents):
        raise Invalid('environment root must not have symlink ancestors')
    # Check kernel support before creating a setup workspace.
    probe = subprocess.run(isolation.wrap(['/usr/bin/true'], clone, 'bwrap', []), capture_output=True, timeout=10)
    if probe.returncode:
        raise Invalid('requested setup isolation unavailable; no host fallback')
    resolved = parent.resolve()
    if (resolved == clone or resolved.is_relative_to(clone) or clone.is_relative_to(resolved)
            or resolved == wheelhouse or resolved.is_relative_to(wheelhouse) or wheelhouse.is_relative_to(resolved)):
        raise Invalid('environment output must be separate from source and wheelhouse')
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / uuid.uuid4().hex
    folder.mkdir()
    runtime = folder / 'venv'
    expanded = []
    substitutions = {'{python}': str(runtime / 'bin/python'), '{environment}': str(folder),
                     '{clone}': str(clone), '{wheelhouse}': str(wheelhouse)}
    for cmd in recipe['commands']:
        expanded.append([_expand(v, substitutions) for v in cmd])
    commands = [['/usr/bin/python3', '-m', 'venv', str(runtime)], *expanded]
    # A single private worker owns the whole timeout, including all setup commands.
    worker = folder / 'worker.py'
    worker.write_text('''import json,subprocess,sys
from pathlib import Path
folder=Path(sys.argv[1]);commands=json.loads((folder/'commands.json').read_text())
for i,command in enumerate(commands):
 with (folder/f'step-{i}.log').open('w') as log:
  rc=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT).returncode
 if rc:sys.exit(rc)
''')
    (folder / 'commands.json').write_text(json.dumps(commands))
    argv = _wrap(['/usr/bin/python3', str(worker), str(folder)], clone, wheelhouse, folder)
    from .verify import _run
    rc, output = _run(argv, clone, timeout)
    if rc:
        return {'status': 'environment_failed', 'id': item_id, 'exit_code': rc, 'logs': str(folder),
                'reason': output, 'warning': 'setup failure is not bug evidence; preserve logs, no host fallback'}
    current = evidence.capture(clone, base, [])
    if current != snapshot:
        raise Invalid('checkout changed during setup; environment receipt not saved')
    rec = {'schema': 1, 'status': 'environment_prepared', 'id': item_id, 'repo': item.repo,
           'clone': str(clone), 'checkout': snapshot, 'recipe': recipe, 'environment': str(folder),
           'python': str(runtime / 'bin/python'), 'receipt_file': str(folder / 'receipt.json'), 'isolation': 'bwrap-offline-setup',
           'warning': 'setup only, not fix verification, automatic dependency resolution or assistant isolation'}
    rec['runtime_sha256'] = _runtime_digest(runtime)
    (folder / 'receipt.json').write_text(json.dumps(rec, indent=2))
    return rec


def _expand(value: str, substitutions: dict) -> str:
    for old, new in substitutions.items():
        value = value.replace(old, new)
    return value


def _runtime_digest(runtime: Path) -> str:
    digest = hashlib.sha256()
    if not runtime.is_dir() or runtime.is_symlink():
        raise Invalid('prepared runtime missing or replaced')
    for path in sorted(runtime.rglob('*')):
        if path.is_dir() and not path.is_symlink():
            continue
        digest.update(str(path.relative_to(runtime)).encode())
        digest.update(str(path.lstat().st_mode).encode())
        if path.is_symlink():
            target = path.resolve(strict=True)
            if target.is_relative_to(runtime):
                digest.update(str(target.relative_to(runtime)).encode())
                continue
            if not str(target).startswith('/usr/') or not target.is_file():
                raise Invalid('runtime symlink target is not a system interpreter')
            digest.update(str(target).encode())
            digest.update(_digest(target).encode())
        elif path.is_file():
            digest.update(_digest(path).encode())
        else:
            raise Invalid('runtime contains unsupported special file')
    return digest.hexdigest()


def status(cfg: Config, queue: Queue, item_id: str, receipt_file: Path) -> dict:
    item, clone = _item(queue, item_id, inspect=True)
    rec = _read(receipt_file)
    if rec.get('schema') != 1 or rec.get('status') != 'environment_prepared' or rec.get('id') != item_id or rec.get('repo') != item.repo or rec.get('clone') != str(clone):
        raise Invalid('environment receipt does not match queued work')
    folder = _plain_path(rec.get('environment'))
    root = (Path(cfg.work_dir).absolute() / 'environments').resolve()
    if folder.parent != root or receipt_file.resolve() != folder / 'receipt.json':
        raise Invalid('environment receipt is outside the selected workflow')
    if rec.get('python') != str(folder / 'venv/bin/python'):
        raise Invalid('prepared interpreter path changed')
    snapshot = rec.get('checkout')
    if (not isinstance(snapshot, dict) or any(not isinstance(snapshot.get(k), str) or not snapshot[k]
                                             for k in ('base', 'tree', 'branch')) or not isinstance(rec.get('recipe'), dict)):
        raise Invalid('environment receipt is incomplete; preserve it')
    now = evidence.capture(clone, snapshot['base'], [])
    current_base = evidence._git(clone, 'merge-base', 'HEAD', 'origin/HEAD')
    if now['base'] != current_base:
        raise Invalid('setup base changed; prepare a new environment')
    _validate_recipe(rec['recipe'], clone)
    runtime = folder / 'venv'
    if _runtime_digest(runtime) != rec.get('runtime_sha256'):
        raise Invalid('prepared runtime changed; inspect and prepare again')
    return rec
