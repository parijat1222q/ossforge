import copy
import json
from pathlib import Path

import pytest

from ossforge import environment, evidence, native_replay
from test_environment import setup, available  # noqa: F401


@pytest.fixture
def bound_manifest(setup, monkeypatch):  # noqa: F811
    # Deterministic dependency guard fixture. Actual pytest/namespace replay is
    # independently exercised in test_native_replay and the known-source replay.
    cfg, q, source, recipe, data = setup
    runtime = recipe.parent / 'runtime'
    runtime.mkdir()
    (runtime / 'input').write_text('prepared')
    folder = recipe.parent / 'replay'
    folder.mkdir()
    setup_file = recipe.parent / 'setup.json'
    rec = {'checkout': environment._source_snapshot(source, environment.evidence._git(source, 'rev-parse', 'HEAD')),
           'clone': str(source), 'environment': str(runtime.parent / 'prepared'),
           'runtime_sha256': environment._runtime_digest(runtime), 'system_inputs': environment._system_inputs(), 'recipe': data}
    # The runtime's deterministic place must agree with setup binding.
    prepared = runtime.parent / 'prepared'
    prepared.mkdir()
    runtime.rename(prepared / 'venv')
    runtime = prepared / 'venv'
    rec['runtime_sha256'] = environment._runtime_digest(runtime)
    setup_file.write_text(json.dumps(rec))
    baseline = {'process_exit': 1, 'collected': ['tests/test.py::test_case'], 'reports': [{'nodeid':'tests/test.py::test_case','when':'call','outcome':'failed','assertion':True}]}
    fixed = copy.deepcopy(baseline)
    fixed['process_exit'] = 0
    fixed['reports'][0]['outcome'] = 'passed'
    result = {'status':'python_replay_passed','setup_receipt':str(setup_file),'setup_receipt_sha256':environment._digest(setup_file),'logs':str(folder),
              'observer_sha256':environment._digest(Path(native_replay.__file__).with_name('pytest_observer.py')),
              'spec':{'expected_failures':['tests/test.py::test_case']},'baseline':baseline,'fixed':fixed}
    for phase in ['baseline','fixed']:
        for suffix in ['-input.json','-observation.json']:
            (folder / (phase+suffix)).write_text('{}')
    (folder/'result.json').write_text(json.dumps(result))
    manifest = native_replay.dependencies(source, rec, result)
    return source, runtime, setup_file, folder, manifest


def test_dependencies_current(bound_manifest):
    source, runtime, setup_file, folder, manifest = bound_manifest
    native_replay.check_dependencies(source, manifest)


@pytest.mark.parametrize('change', ['runtime', 'setup', 'observation', 'result', 'recipe', 'ca', 'missingobserver'])
def test_dependencies_refuse_mutation(bound_manifest, change, monkeypatch):
    source, runtime, setup_file, folder, manifest = bound_manifest
    if change == 'runtime':
        (runtime/'input').write_text('changed')
    elif change == 'setup':
        setup_file.write_text('{}')
    elif change == 'observation':
        (folder/'baseline-observation.json').write_text('{"changed":true}')
    elif change == 'result':
        (folder/'result.json').write_text('{}')
    elif change == 'recipe':
        wheel = Path(manifest['recipe']['wheelhouse'])/'controlled.whl'
        wheel.write_text('changed')
    elif change == 'ca':
        monkeypatch.setattr(environment, '_system_inputs', lambda:{'different':'hash'})
    else:
        del manifest['files'][str(Path(native_replay.__file__).with_name('pytest_observer.py'))]
    with pytest.raises((ValueError, OSError)):
        native_replay.check_dependencies(source, manifest)


def test_evidence_consumers_recheck_native_dependencies(bound_manifest):
    source, runtime, setup_file, folder, manifest = bound_manifest
    base = environment.evidence._git(source, 'rev-parse', 'HEAD')
    rec = evidence.capture(source, base, [['native-pytest']])
    rec.update(verification_kind='python_native_replay', native_replay=manifest)
    evidence.check(source, base, rec['branch'], rec)
    (runtime/'input').write_text('stale')
    with pytest.raises(evidence.StaleEvidence, match='runtime changed'):
        evidence.check(source, base, rec['branch'], rec)


def test_native_evidence_cannot_drop_dependency_manifest(bound_manifest):
    source, runtime, setup_file, folder, manifest = bound_manifest
    base = environment.evidence._git(source, 'rev-parse', 'HEAD')
    rec = evidence.capture(source, base, [['native-pytest']])
    rec['verification_kind'] = 'python_native_replay'
    with pytest.raises(evidence.StaleEvidence, match='dependency evidence missing'):
        evidence.check(source, base, rec['branch'], rec)


@pytest.mark.parametrize('boundary', ['issue', 'fork', 'pr'])
def test_native_dependency_drift_stops_at_each_public_write(boundary, tmp_path, monkeypatch):
    from ossforge import submit
    from ossforge.config import Config
    from ossforge.queue import Item
    from ossforge.limiter import DailyLimiter
    from ossforge.safety import Audit
    item = Item('a','owner/repo','a.py',1,'bug','candidate',status='verified',clone=str(tmp_path),branch='fix',issue_title='bug',issue_body='repro',pr_title='fix',pr_body='tests')
    if boundary == 'fork':
        item.submitted = {'issue_url':'https://github.com/owner/repo/issues/1'}
    elif boundary == 'pr':
        item.submitted = {'issue_url':'https://github.com/owner/repo/issues/1','fork_pushed':True}
    cfg = Config(state_dir=str(tmp_path/'state'))
    changed = False
    effects = []
    monkeypatch.setattr(submit, 'preflight', lambda *a:None)
    monkeypatch.setattr(submit, '_publication_mode', lambda *a:'issue_pr')
    monkeypatch.setattr(submit, '_disclosure', lambda *a:(False,None))
    monkeypatch.setattr(submit, '_admission_gate', lambda *a:{})
    monkeypatch.setattr(submit, '_identity_gate', lambda *a,**k:None)
    monkeypatch.setattr(submit, '_base', lambda *a:'base')
    monkeypatch.setattr(submit, '_upstream_gate', lambda *a:None)
    monkeypatch.setattr(submit, '_source_work_gate', lambda *a:None)
    monkeypatch.setattr(submit, '_duplicate_gate', lambda *a:None)
    monkeypatch.setattr(submit.gh, 'pace_write', lambda:None)
    def check(*a):
        if changed:
            raise submit.Refused('native replay runtime changed')
    monkeypatch.setattr(submit, '_evidence_gate', check)
    calls = 0
    def access(*a):
        nonlocal calls, changed
        calls += 1
        if boundary in {'fork','pr'} and calls == 2:
            changed = True
    monkeypatch.setattr(submit, '_repo_access_gate', access)
    def api(*a,**k):
        nonlocal changed
        if boundary == 'issue':
            changed = True
        return {'login':'owner'}
    monkeypatch.setattr(submit.gh, 'api', api)
    monkeypatch.setattr(submit, '_run', lambda *a:effects.append(a))
    monkeypatch.setattr(submit.subprocess, 'run', lambda *a,**k:effects.append(a))
    with pytest.raises(submit.Refused, match='runtime changed'):
        submit._submit(item,DailyLimiter(tmp_path/'daily',2),cfg,Audit(tmp_path/'audit'),True)
    assert effects == []
