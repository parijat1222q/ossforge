"""Explicit Python/pytest editable-source replay, not generic native verification."""
from __future__ import annotations

import json
import hashlib
import fcntl
from contextlib import contextmanager
import re
import subprocess
import uuid
from pathlib import Path

from . import environment, evidence, policy_review, predictor, safety, verify
from .config import Config
from .queue import Queue


class Invalid(ValueError):
    pass


def _spec(path: Path, clone: Path, base: str) -> dict:
    spec = environment._read(path)
    if set(spec) != {'schema', 'modules', 'tests', 'expected_failures'} or type(spec['schema']) is not int or spec['schema'] != 1:
        raise Invalid('replay needs exact schema 1 keys: modules, tests, expected_failures')
    modules = spec['modules']
    if not isinstance(modules, dict) or not 1 <= len(modules) <= 30:
        raise Invalid('map every modified Python source file to its actual module name')
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in modules.items()):
        raise Invalid('module mappings must be text names and paths')
    tracked = evidence._git(clone, 'ls-files', '-z').split('\x00')
    if any(p.lower().endswith(('.pyc', '.pyo')) or '__pycache__' in Path(p).parts for p in tracked if p):
        raise Invalid('tracked Python bytecode/cache inputs unsupported; preserve checkout and use a reviewed source-only tree')
    changed = evidence._git(clone, 'diff', '--name-only', '--no-renames', base + '..HEAD').splitlines()
    sources = {p for p in changed if not predictor.is_test_path(p)}
    tests = {p for p in changed if predictor.is_test_path(p)}
    if not sources or not tests or set(modules.values()) != sources or len(set(modules.values())) != len(modules):
        raise Invalid('module mapping must cover every changed non-test source exactly once; regression test required')
    for name, rel in modules.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_]\w*(\.[A-Za-z_]\w*)*', name):
            raise Invalid('module name must be dotted Python identifiers')
        if (not isinstance(rel, str) or not rel.endswith('.py') or Path(rel).is_absolute()
                or '..' in Path(rel).parts or not (clone / rel).is_file()
                or (clone / rel).is_symlink() or any(p.is_symlink() for p in (clone / rel).parents)):
            raise Invalid('only existing regular Python source files are supported')
        probe = subprocess.run(['git', 'cat-file', '-e', f'{base}:{rel}'], cwd=clone, capture_output=True)
        if probe.returncode:
            raise Invalid('added/deleted sources need a separately reviewed replay method')
    args = spec['tests']
    if not isinstance(args, list) or not 1 <= len(args) <= 30 or any(not isinstance(a, str) or not a or '\x00' in a or a.startswith('-') for a in args):
        raise Invalid('tests must be explicit pytest node selections, not arbitrary pytest options')
    if len(set(args)) != len(args):
        raise Invalid('pytest selections must be distinct')
    for arg in args:
        rel = Path(arg.split('::')[0])
        if rel.is_absolute() or '..' in rel.parts or not (clone / rel).is_file() or (clone / rel).is_symlink() or any(p.is_symlink() for p in (clone / rel).parents):
            raise Invalid('pytest selections must be regular test files inside the checkout')
    expected = spec['expected_failures']
    if not isinstance(expected, list) or not 1 <= len(expected) <= 30 or any(not isinstance(v, str) for v in expected) or len(set(expected)) != len(expected):
        raise Invalid('name exact expected baseline AssertionError node IDs')
    for value in expected:
        if not isinstance(value, str) or '::' not in value or '\x00' in value:
            raise Invalid('expected failures must be exact pytest node IDs')
        if value.split('::')[0] not in tests:
            raise Invalid('expected failure must come from a changed regression test file')
    return spec


def _observe(clone: Path, rec: dict, spec: dict, folder: Path, phase: str, timeout: int) -> dict:
    observer = Path(__file__).with_name('pytest_observer.py')
    nonce = uuid.uuid4().hex
    inputs = {'nonce': nonce, 'modules': {name: {'path': str(clone / rel), 'sha256': environment._digest(clone / rel)} for name, rel in spec['modules'].items()}}
    input_file = folder / (phase + '-input.json')
    output_file = folder / (phase + '-observation.json')
    input_file.write_text(json.dumps(inputs))
    command = [rec['python'], str(observer), str(input_file), str(output_file), '-q', '-p', 'no:cacheprovider', *spec['tests']]
    wrapped = environment._setup_wrap(command, clone, [rec['environment'], str(observer), str(folder)])
    pos = wrapped.index('--')
    wrapped = wrapped[:pos] + ['--bind', str(folder), str(folder)] + wrapped[pos:]
    rc, output = verify._run(wrapped, clone, timeout)
    (folder / (phase + '.log')).write_text(output)
    if not output_file.is_file() or output_file.is_symlink():
        raise Invalid(f'{phase} produced no observation, rc={rc}; inspect {folder}')
    observation = environment._read(output_file)
    if (observation.get('schema') != 1 or observation.get('nonce') != nonce
            or observation.get('problems') != [] or observation.get('modules') != inputs['modules']
            or observation.get('exit_code') != rc):
        raise Invalid(f'{phase} import/runtime/test observation refused, rc={rc}; inspect {folder}')
    observation['process_exit'] = rc
    return observation


def _compare(baseline: dict, fixed: dict, expected: list[str]) -> None:
    if baseline.get('process_exit') != 1 or fixed.get('process_exit') != 0:
        raise Invalid('need actual baseline assertion failure (1) and fixed pass (0)')
    if not baseline.get('collected') or baseline['collected'] != fixed.get('collected'):
        raise Invalid('baseline/fixed must collect the same nonempty test IDs')
    for phase in (baseline, fixed):
        reports = phase.get('reports')
        if not isinstance(reports, list) or not reports:
            raise Invalid('test phase reports missing')
        calls = [r['nodeid'] for r in reports if r.get('when') == 'call']
        if sorted(calls) != sorted(phase['collected']):
            raise Invalid('every collected test must execute once without skips')
        if any(r.get('outcome') not in {'passed', 'failed'} for r in reports):
            raise Invalid('skipped or unsupported test outcomes cannot establish replay')
    failures = [r for r in baseline['reports'] if r.get('outcome') == 'failed']
    if (sorted(r['nodeid'] for r in failures) != sorted(expected)
            or any(r.get('when') != 'call' or r.get('assertion') is not True for r in failures)):
        raise Invalid('baseline must fail only the named regression assertions, not setup/collection/tool errors')
    if any(r.get('outcome') != 'passed' for r in fixed['reports']):
        raise Invalid('fixed replay has non-passing reports')


def replay(cfg: Config, queue: Queue, item_id: str, receipt_file: Path, spec_file: Path, timeout: int = 600) -> dict:
    safety.kill_switch(cfg)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        raise Invalid('replay timeout must be 1..1800 seconds per phase')
    item, clone = environment._item(queue, item_id)
    why = policy_review.check(cfg, item.repo)
    if why:
        raise Invalid(why)
    if policy_review.publication_route(cfg, item.repo) not in {'issue_pr', 'pr_only'}:
        raise Invalid('replay requires resolved contribution prerequisites')
    rec = environment.status(cfg, queue, item_id, receipt_file)
    receipt_digest = environment._digest(receipt_file)
    base = rec['checkout']['base']
    spec = _spec(spec_file, clone, base)
    root = Path(cfg.work_dir).absolute() / 'replays'
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise Invalid('replay output must not have symlink ancestors')
    root.mkdir(parents=True, exist_ok=True)
    folder = root / uuid.uuid4().hex
    folder.mkdir()
    observer = Path(__file__).with_name('pytest_observer.py')
    observer_digest = environment._digest(observer)
    sources = list(spec['modules'].values())
    try:
        p = subprocess.run(['git', 'checkout', base, '--', *sources], cwd=clone, capture_output=True)
        if p.returncode:
            raise Invalid('could not restore baseline source; no replay evidence')
        baseline = _observe(clone, rec, spec, folder, 'baseline', timeout)
    finally:
        p = subprocess.run(['git', 'restore', '--source=HEAD', '--staged', '--worktree', '--', *sources], cwd=clone, capture_output=True)
        if p.returncode:
            raise Invalid('could not restore fixed source; inspect checkout before continuing')
    environment.status(cfg, queue, item_id, receipt_file)
    fixed = _observe(clone, rec, spec, folder, 'fixed', timeout)
    _compare(baseline, fixed, spec['expected_failures'])
    environment.status(cfg, queue, item_id, receipt_file)
    if environment._digest(receipt_file) != receipt_digest:
        raise Invalid('setup receipt changed during replay')
    if environment._digest(observer) != observer_digest:
        raise Invalid('pytest observer changed during replay')
    result = {'schema': 1, 'status': 'python_replay_passed', 'id': item_id, 'setup_receipt': str(receipt_file),
              'setup_receipt_sha256': environment._digest(receipt_file), 'spec': spec,
              'observer_sha256': observer_digest, 'baseline': baseline, 'fixed': fixed,
              'logs': str(folder), 'warning': 'Python pytest editable-source replay only; no queue promotion or publication. Semantic test review and other native checks remain separate.'}
    (folder / 'result.json').write_text(json.dumps(result, indent=2))
    return result


def dependencies(clone: Path, rec: dict, result: dict) -> dict:
    """Bind the actual replay artifacts and prepared runtime for later checks."""
    folder = environment._plain_path(result['logs'])
    paths = [Path(result['setup_receipt']), Path(__file__).with_name('pytest_observer.py'),
             folder / 'result.json', folder / 'baseline-input.json', folder / 'baseline-observation.json',
             folder / 'fixed-input.json', folder / 'fixed-observation.json']
    return {'schema': 1, 'clone': str(clone), 'checkout': rec['checkout'],
            'setup_receipt': result['setup_receipt'], 'replay_result': str(folder / 'result.json'),
            'runtime': str(Path(rec['environment']) / 'venv'), 'runtime_sha256': rec['runtime_sha256'],
            'system_inputs': rec['system_inputs'], 'recipe': rec['recipe'],
            'files': {str(environment._plain_path(str(p))): environment._digest(p) for p in paths}}


def check_dependencies(clone: Path, manifest: dict) -> None:
    """Read-only stale-input checks used by every evidence/publication gate."""
    keys = {'schema', 'clone', 'checkout', 'setup_receipt', 'replay_result', 'runtime', 'runtime_sha256', 'system_inputs', 'recipe', 'files'}
    if (not isinstance(manifest, dict) or set(manifest) != keys or manifest.get('schema') != 1
            or manifest.get('clone') != str(clone) or not isinstance(manifest['files'], dict)
            or len(manifest['files']) != 7 or not isinstance(manifest['checkout'], dict)):
        raise Invalid('native replay dependency manifest missing or unsupported')
    snapshot = manifest['checkout']
    if environment._source_snapshot(clone, snapshot['base']) != snapshot:
        raise Invalid('native replay source snapshot changed')
    if environment._system_inputs() != manifest['system_inputs']:
        raise Invalid('native replay public CA input changed')
    environment._validate_recipe(manifest['recipe'], clone)
    runtime = environment._plain_path(manifest['runtime'])
    if environment._runtime_digest(runtime) != manifest['runtime_sha256']:
        raise Invalid('native replay runtime changed')
    for raw, digest in manifest['files'].items():
        path = environment._plain_path(raw)
        if not path.is_file() or environment._digest(path) != digest:
            raise Invalid('native replay dependency changed: ' + raw)
    observer = str(Path(__file__).with_name('pytest_observer.py'))
    result_path = environment._plain_path(manifest['replay_result'])
    result = environment._read(result_path)
    folder = result_path.parent
    expected_files = {str(Path(manifest['setup_receipt'])), observer, str(result_path),
                      *(str(folder / (phase + suffix)) for phase in ('baseline', 'fixed')
                        for suffix in ('-input.json', '-observation.json'))}
    if set(manifest['files']) != expected_files:
        raise Invalid('native replay artifact dependency set changed')
    setup = environment._read(Path(manifest['setup_receipt']))
    if (result.get('status') != 'python_replay_passed' or result.get('setup_receipt') != manifest['setup_receipt']
            or result.get('setup_receipt_sha256') != manifest['files'][manifest['setup_receipt']]
            or result.get('observer_sha256') != manifest['files'][observer]
            or setup.get('checkout') != snapshot or setup.get('clone') != str(clone)
            or str(Path(setup.get('environment', '')) / 'venv') != str(runtime)
            or setup.get('runtime_sha256') != manifest['runtime_sha256']
            or setup.get('system_inputs') != manifest['system_inputs'] or setup.get('recipe') != manifest['recipe']):
        raise Invalid('native replay result/setup dependency binding changed')
    spec = result['spec']
    # Revalidate semantics from the actual hashed files, not a second summary.
    phase_records = []
    for phase, revision in [('baseline', snapshot['base']), ('fixed', 'HEAD')]:
        inputs = environment._read(folder / (phase + '-input.json'))
        observed = environment._read(folder / (phase + '-observation.json'))
        modules = {}
        for name, rel in spec['modules'].items():
            if revision == 'HEAD':
                digest = environment._digest(clone / rel)
            else:
                blob = subprocess.run(['git', 'show', f'{revision}:{rel}'], cwd=clone, capture_output=True, check=True).stdout
                digest = hashlib.sha256(blob).hexdigest()
            modules[name] = {'path': str(clone / rel), 'sha256': digest}
        rc = 1 if phase == 'baseline' else 0
        if (not isinstance(inputs.get('nonce'), str) or not inputs['nonce']
                or inputs.get('modules') != modules or observed.get('schema') != 1
                or observed.get('nonce') != inputs['nonce'] or observed.get('modules') != modules
                or observed.get('problems') != [] or observed.get('exit_code') != rc):
            raise Invalid('native replay actual input/observation binding changed: ' + phase)
        observed['process_exit'] = rc
        if observed != result[phase]:
            raise Invalid('native replay embedded observation differs from actual artifact: ' + phase)
        phase_records.append(observed)
    if environment._read(folder / 'baseline-input.json')['nonce'] == environment._read(folder / 'fixed-input.json')['nonce']:
        raise Invalid('native replay phase nonces must differ')
    _compare(*phase_records, spec['expected_failures'])


@contextmanager
def publication_locked(cfg: Config):
    """Exclude replay source restoration from same-state publication effects."""
    cfg.path('publication.lock').parent.mkdir(parents=True, exist_ok=True)
    with cfg.path('publication.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Invalid('another publication holds this state directory; preserve pending work') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)



def complete(cfg: Config, queue: Queue, proposal: dict, receipt_file: Path, spec_file: Path, timeout: int = 600) -> dict:
    """Run replay now, then normal checks and promote only bound current evidence."""
    from . import driver, submit
    item, clone = environment._item(queue, proposal['id'])
    if proposal.get('outcome') == 'hold':
        raise Invalid('native replay completion requires a fix proposal, not hold')
    base = submit._base(str(clone))
    snapshot = evidence.capture(clone, base, [])
    if proposal['base'] != base or proposal['branch'] != snapshot['branch']:
        raise driver.Invalid('native replay proposal base/branch changed')
    rec = environment.status(cfg, queue, item.id, receipt_file)
    py = rec['python']
    # A different interpreter or shell must not silently bypass this runtime.
    for command in proposal['check_commands']:
        if command[0] != py:
            raise Invalid('native replay check commands must use the prepared Python interpreter')
    spec = _spec(spec_file, clone, base)
    planned_command = [py, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *spec['tests']]
    if proposal['test_command'] != planned_command or proposal['failure_code'] != 1:
        raise Invalid('proposal test command must match the explicit observed pytest selections')
    result = replay(cfg, queue, item.id, receipt_file, spec_file, timeout)
    expected_command = [py, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *result['spec']['tests']]
    if proposal['test_command'] != expected_command or proposal['failure_code'] != 1:
        raise Invalid('proposal test command must match the explicit observed pytest selections')
    manifest = dependencies(clone, rec, result)
    check_dependencies(clone, manifest)
    logs = []
    for command in proposal['check_commands']:
        wrapped = environment._setup_wrap(command, clone, [rec['environment']])
        rc, output = verify._run(wrapped, clone, timeout)
        logs.append({'command': command, 'exit_code': rc, 'output_tail': output})
        if rc:
            raise Invalid('native replay check failed; queue remains clean: ' + output[-500:])
        check_dependencies(clone, manifest)
    current = evidence.capture(clone, base, [expected_command, *proposal['check_commands']])
    if any(current[k] != snapshot[k] for k in ('base', 'tree', 'branch')):
        raise Invalid('native replay source changed before promotion')
    current['verification_kind'] = 'python_native_replay'
    current['native_replay'] = manifest
    check_dependencies(clone, manifest)
    queue.set(item.id, status='verified', branch=proposal['branch'], verification=current,
              issue_title=proposal['issue_title'], issue_body=proposal['issue_body'],
              pr_title=proposal['pr_title'], pr_body=proposal['pr_body'], consent_issue=proposal['consent_issue'])
    queue.save()
    return {'schema': 1, 'status': 'verified', 'id': item.id, 'evidence': current,
            'checks': logs, 'replay_logs': result['logs'],
            'next': 'submit remains separate; dependency checks repeat before public effects'}
