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
              'spec':{'modules':{'value':'value.py'},'expected_failures':['tests/test.py::test_case']},'baseline':baseline,'fixed':fixed}
    for phase in ['baseline','fixed']:
        modules = {'value':{'path':str(source/'value.py'),'sha256':environment._digest(source/'value.py')}}
        inputs = {'nonce':phase,'modules':modules}
        observation = result[phase]
        observation.update(schema=1,nonce=phase,modules=modules,problems=[],exit_code=observation['process_exit'])
        (folder / (phase+'-input.json')).write_text(json.dumps(inputs))
        (folder / (phase+'-observation.json')).write_text(json.dumps({k:v for k,v in observation.items() if k != 'process_exit'}))
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


def test_native_cli_lock_contention_refuses_cleanly(setup, tmp_path, capsys):  # noqa: F811
    from ossforge import cli, repo_research
    cfg, q, source, recipe, data = setup
    config = tmp_path / 'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    with repo_research.locked(cfg):
        assert cli.main(['--config', str(config), 'agent-verify', 'unused.json',
                         '--environment-receipt', 'unused.json', '--replay-spec', 'unused.json']) == 1
    assert 'another run holds' in capsys.readouterr().err
    assert q.items['i'].status == 'clean'


def test_native_cli_reloads_queue_after_acquiring_lock(setup, tmp_path, monkeypatch, capsys):  # noqa: F811
    from contextlib import contextmanager
    from ossforge import cli, driver, repo_research
    from ossforge.queue import Queue
    cfg, q, source, recipe, data = setup
    config = tmp_path / 'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    @contextmanager
    def locked(cfg):
        fresh = Queue(cfg.path('queue.json'))
        fresh.set('i', status='held', hold_reason='newer decision')
        fresh.save()
        yield
    monkeypatch.setattr(repo_research, 'locked', locked)
    monkeypatch.setattr(driver, 'parse', lambda *a:{})
    def complete(cfg, q, *a):
        assert q.items['i'].status == 'held'
        return {'status':'verified'}
    monkeypatch.setattr(native_replay, 'complete', complete)
    assert cli.main(['--config', str(config), 'agent-verify', 'unused.json',
                     '--environment-receipt', 'unused.json', '--replay-spec', 'unused.json']) == 0
    assert 'verified' in capsys.readouterr().out


@pytest.fixture
def completion(bound_manifest, setup, monkeypatch):  # noqa: F811
    cfg, q, source, recipe, data = setup
    source, runtime, setup_file, folder, manifest = bound_manifest
    rec = json.loads(setup_file.read_text())
    rec['python'] = str(runtime / 'bin/python')
    setup_file.write_text(json.dumps(rec))
    result_file = folder / 'result.json'
    result = json.loads(result_file.read_text())
    result['setup_receipt_sha256'] = environment._digest(setup_file)
    result['spec']['tests'] = ['tests/test.py']
    result_file.write_text(json.dumps(result))
    proposal = {'id':'i','base':rec['checkout']['base'],'branch':rec['checkout']['branch'],
                'test_command':[rec['python'],'-m','pytest','-q','-p','no:cacheprovider','tests/test.py'],
                'check_commands':[[rec['python'],'-m','ruff','check']], 'failure_code':1,
                'issue_title':'fixture','issue_body':'private test','pr_title':'fixture','pr_body':'private test','consent_issue':0}
    monkeypatch.setattr(environment, 'status', lambda *a:rec)
    monkeypatch.setattr(native_replay, '_spec', lambda *a:result['spec'])
    monkeypatch.setattr(native_replay, 'replay', lambda *a:result)
    monkeypatch.setattr(environment, '_setup_wrap', lambda command,*a:command)
    monkeypatch.setattr(native_replay.verify, '_run', lambda *a:(0,'check passed'))
    return cfg,q,source,runtime,setup_file,recipe,proposal


def test_completion_promotes_only_current_bound_evidence(completion):
    cfg,q,source,runtime,receipt,spec,proposal = completion
    result = native_replay.complete(cfg,q,proposal,receipt,spec)
    assert result['status'] == q.items['i'].status == 'verified'
    evidence.check(source,proposal['base'],proposal['branch'],q.items['i'].verification)


@pytest.mark.parametrize('mode', ['base','branch','hold','interpreter','testcommand','checkfail','runtime'])
def test_completion_refusal_never_promotes(completion, monkeypatch, mode):
    cfg,q,source,runtime,receipt,spec,proposal = completion
    if mode in {'base','branch'}:
        proposal[mode] = 'wrong'
    elif mode == 'hold':
        proposal['outcome'] = 'hold'
    elif mode == 'interpreter':
        proposal['check_commands'][0][0] = '/other/python'
    elif mode == 'testcommand':
        proposal['test_command'].append('other_test.py')
    elif mode == 'checkfail':
        monkeypatch.setattr(native_replay.verify, '_run', lambda *a:(1,'failed required check'))
    else:
        def run(*a):
            (runtime/'input').write_text('changed during check')
            return 0,'apparent success'
        monkeypatch.setattr(native_replay.verify, '_run', run)
    with pytest.raises(ValueError):
        native_replay.complete(cfg,q,proposal,receipt,spec)
    assert q.items['i'].status == 'clean' and not q.items['i'].verification


@pytest.mark.parametrize('change', ['nonce','modules','exit','summary','same_nonce'])
def test_actual_artifact_semantics_cannot_be_replaced_by_passing_summary(bound_manifest, change):
    source,runtime,receipt,folder,manifest = bound_manifest
    raw = folder/'baseline-observation.json'
    data = json.loads(raw.read_text())
    if change == 'nonce':
        data['nonce'] = 'wrong'
    elif change == 'modules':
        data['modules']['value']['sha256'] = 'wrong'
    elif change == 'exit':
        data['exit_code'] = 0
    elif change == 'summary':
        data['reports'][0]['assertion'] = False
    else:
        raw = folder/'fixed-input.json'
        data = json.loads(raw.read_text())
        data['nonce'] = 'baseline'
    raw.write_text(json.dumps(data))
    # Even a refreshed outer hash must not make a mismatched artifact agree.
    manifest['files'][str(raw)] = environment._digest(raw)
    with pytest.raises(ValueError):
        native_replay.check_dependencies(source,manifest)


@pytest.mark.parametrize('exception', [RuntimeError('symlink loop'), __import__('subprocess').CalledProcessError(1,['git'])])
def test_dependency_operational_errors_become_stale_evidence(bound_manifest, monkeypatch, exception):
    source,runtime,receipt,folder,manifest = bound_manifest
    base = environment.evidence._git(source,'rev-parse','HEAD')
    rec = evidence.capture(source,base,[['native-pytest']])
    rec.update(verification_kind='python_native_replay',native_replay=manifest)
    def check(*a):
        raise exception
    monkeypatch.setattr(native_replay,'check_dependencies',check)
    with pytest.raises(evidence.StaleEvidence):
        evidence.check(source,base,rec['branch'],rec)


def test_unknown_verification_kind_refuses_while_legacy_remains_supported(bound_manifest):
    source,runtime,receipt,folder,manifest = bound_manifest
    base = environment.evidence._git(source,'rev-parse','HEAD')
    rec = evidence.capture(source,base,[['legacy-check']])
    evidence.check(source,base,rec['branch'],rec)
    rec['verification_kind'] = 'future-or-misspelled-native'
    with pytest.raises(evidence.StaleEvidence,match='unknown verification kind'):
        evidence.check(source,base,rec['branch'],rec)


def test_native_completion_excludes_existing_publication(setup, tmp_path, capsys):  # noqa: F811
    import fcntl
    from ossforge import cli
    cfg,q,source,recipe,data = setup
    config = tmp_path/'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    cfg.path('publication.lock').parent.mkdir(parents=True,exist_ok=True)
    with cfg.path('publication.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert cli.main(['--config',str(config),'agent-verify','unused.json','--environment-receipt','unused.json','--replay-spec','unused.json']) == 1
    assert 'another publication holds' in capsys.readouterr().err
    assert q.items['i'].status == 'clean'
