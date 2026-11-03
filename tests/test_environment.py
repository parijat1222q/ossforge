import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ossforge import cli, environment, policy_review
from ossforge.config import Config
from ossforge.queue import Item, Queue


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-qb', 'main')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    (source / 'SETUP.md').write_text('Use Python venv; controlled fixture, no dependencies.\n')
    (source / 'value.py').write_text('value=1\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'base')
    base = git(source, 'rev-parse', 'HEAD')
    git(source, 'update-ref', 'refs/remotes/origin/main', base)
    git(source, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    (wheels / 'controlled.whl').write_bytes(b'not installed; manifest fixture only')
    def h(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    recipe = {'schema': 1, 'note': 'Read SETUP.md; no installer command required for this fixture',
              'sources': [{'path': 'SETUP.md', 'sha256': h(source / 'SETUP.md')}],
              'wheelhouse': str(wheels), 'wheels': {'controlled.whl': h(wheels / 'controlled.whl')},
              'commands': [['{python}', '-c', 'import os,socket;from pathlib import Path;assert os.environ["HOME"]=="/tmp/home";assert not Path("/home/sandbox").exists();assert Path("{clone}/SETUP.md").is_file();Path("{environment}/ok").write_text("done")']]}
    recipe_file = tmp_path / 'recipe.json'
    recipe_file.write_text(json.dumps(recipe))
    cfg = Config(state_dir=str(tmp_path / 'state'), work_dir=str(tmp_path / 'work'), kill_file=str(tmp_path / 'STOP'))
    q = Queue(cfg.path('queue.json'))
    q.add(Item('i', 'org/lib', 'value.py', 1, 'bug', 'fixture', status='clean', clone=str(source)))
    q.save()
    monkeypatch.setattr(policy_review, 'check', lambda *a: '')
    monkeypatch.setattr(policy_review, 'publication_route', lambda *a: 'issue_pr')
    return cfg, q, source, recipe_file, recipe


def available(source):
    if not shutil.which('bwrap'):
        pytest.skip('bubblewrap unavailable')
    p = subprocess.run(environment.isolation.wrap(['/usr/bin/true'], source, 'bwrap', []), capture_output=True)
    if p.returncode:
        pytest.skip('kernel namespaces unavailable')


def test_public_cli_native_setup_and_status_preserve_checkout(setup, capsys, tmp_path):
    cfg, q, source, recipe_file, recipe = setup
    available(source)
    config = tmp_path / 'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    assert cli.main(['--config', str(config), 'environment-prepare', 'i', str(recipe_file)]) == 0
    rec = json.loads(capsys.readouterr().out)
    folder = Path(rec['environment'])
    assert (folder / 'ok').read_text() == 'done'
    assert git(source, 'status', '--porcelain') == ''
    receipt = folder / 'receipt.json'
    assert cli.main(['--config', str(config), 'environment-status', 'i', str(receipt)]) == 0
    capsys.readouterr()
    # A later genuine source fix does not invalidate unchanged setup guides/base.
    (source / 'value.py').write_text('value=2\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'fix')
    assert environment.status(cfg, q, 'i', receipt)['status'] == 'environment_prepared'
    (folder / 'venv/changed').write_text('changed')
    with pytest.raises(environment.Invalid, match='runtime changed'):
        environment.status(cfg, q, 'i', receipt)


@pytest.mark.parametrize('change', ['schema', 'source', 'wheel', 'symlink', 'shell', 'overlap'])
def test_recipe_refuses_before_setup(setup, change, monkeypatch):
    cfg, q, source, path, data = setup
    monkeypatch.setattr(environment.isolation.shutil, 'which', lambda *a: '/usr/bin/bwrap')
    if change == 'schema':
        data['extra'] = True
    elif change == 'source':
        data['sources'][0]['sha256'] = 'bad'
    elif change == 'wheel':
        data['wheels']['controlled.whl'] = 'bad'
    elif change == 'symlink':
        (source / 'alias').symlink_to(source / 'SETUP.md')
        data['sources'][0]['path'] = 'alias'
    elif change == 'shell':
        data['commands'] = ['python -m pip install foo']
    else:
        data['wheelhouse'] = str(source)
    path.write_text(json.dumps(data))
    original = environment.subprocess.run
    def run(command, *a, **k):
        assert command[0] == 'git', 'no setup process before validation'
        return original(command, *a, **k)
    monkeypatch.setattr(environment.subprocess, 'run', run)
    with pytest.raises((ValueError, environment.isolation.Unavailable)):
        environment.prepare(cfg, q, 'i', path)
    assert not (Path(cfg.work_dir) / 'environments').exists()


def test_missing_isolation_and_policy_prerequisite_no_fallback(setup, monkeypatch):
    cfg, q, source, path, _ = setup
    monkeypatch.setattr(policy_review, 'publication_route', lambda *a: 'issue_first')
    with pytest.raises(environment.Invalid, match='prerequisite'):
        environment.prepare(cfg, q, 'i', path)
    monkeypatch.setattr(policy_review, 'publication_route', lambda *a: 'issue_pr')
    monkeypatch.setattr(environment.isolation.shutil, 'which', lambda *a: None)
    with pytest.raises(environment.isolation.Unavailable):
        environment.prepare(cfg, q, 'i', path)
    assert not (Path(cfg.work_dir) / 'environments').exists()


def test_failure_retains_logs_without_receipt(setup):
    cfg, q, source, path, data = setup
    available(source)
    data['commands'] = [['{python}', '-c', 'raise SystemExit(3)']]
    path.write_text(json.dumps(data))
    out = environment.prepare(cfg, q, 'i', path)
    assert out['status'] == 'environment_failed' and out['exit_code'] == 3
    assert not (Path(out['logs']) / 'receipt.json').exists()
    assert q.items['i'].status == 'clean' and git(source, 'status', '--porcelain') == ''


def test_setup_source_change_invalidates_receipt(setup):
    cfg, q, source, path, data = setup
    available(source)
    out = environment.prepare(cfg, q, 'i', path)
    receipt = Path(out['receipt_file'])
    (source / 'SETUP.md').write_text('changed setup instructions\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'changed setup')
    with pytest.raises(environment.Invalid, match='setup source changed'):
        environment.status(cfg, q, 'i', receipt)


def test_setup_timeout_does_not_save_receipt(setup):
    cfg, q, source, path, data = setup
    available(source)
    data['commands'] = [['{python}', '-c', 'import time;time.sleep(30)']]
    path.write_text(json.dumps(data))
    out = environment.prepare(cfg, q, 'i', path, timeout=1)
    assert out['status'] == 'environment_failed' and out['exit_code'] == 124
    assert not (Path(out['logs']) / 'receipt.json').exists()


def test_agent_verify_receipt_selects_containment_not_host_fallback(setup, tmp_path, monkeypatch, capsys):
    cfg, q, source, path, data = setup
    available(source)
    prepared = environment.prepare(cfg, q, 'i', path)
    config = tmp_path / 'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    result = {'schema': 1, 'id': 'i', 'base': git(source, 'rev-parse', 'HEAD'), 'branch': 'main',
              'test_command': [prepared['python'], '-c', 'assert True'], 'check_commands': [],
              'failure_code': 1, 'issue_title': 'fixture', 'issue_body': 'fixture', 'pr_title': 'fixture',
              'pr_body': 'fixture', 'consent_issue': 0}
    result_file = tmp_path / 'result.json'
    result_file.write_text(json.dumps(result))
    seen = []
    monkeypatch.setattr(cli.driver, 'complete', lambda q, result, **kw: seen.append(kw) or {'status': 'verified'})
    assert cli.main(['--config', str(config), 'agent-verify', str(result_file), '--environment-receipt', prepared['receipt_file']]) == 0
    assert seen == [{'isolation_mode': 'bwrap', 'readonly_paths': [str(Path(prepared['python']).parent.parent)]}]
    capsys.readouterr()
    result['test_command'][0] = '/usr/bin/python3'
    result_file.write_text(json.dumps(result))
    assert cli.main(['--config', str(config), 'agent-verify', str(result_file), '--environment-receipt', prepared['receipt_file']]) == 1
    assert len(seen) == 1


def test_actual_prepared_environment_to_native_fix_verification(setup, tmp_path, capsys):
    cfg, q, source, path, data = setup
    available(source)
    prepared = environment.prepare(cfg, q, 'i', path)
    base = git(source, 'rev-parse', 'HEAD')
    git(source, 'checkout', '-qb', 'fix/value')
    (source / 'value.py').write_text('value=2\n')
    (source / 'test_value.py').write_text('from value import value\nassert value==2\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'controlled fix and regression')
    result = {'schema': 1, 'id': 'i', 'base': base, 'branch': 'fix/value',
              'test_command': [prepared['python'], 'test_value.py'], 'check_commands': [],
              'failure_code': 1, 'issue_title': 'controlled fixture', 'issue_body': 'not public',
              'pr_title': 'controlled fixture', 'pr_body': 'not public', 'consent_issue': 0}
    result_file = tmp_path / 'result.json'
    result_file.write_text(json.dumps(result))
    config = tmp_path / 'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    assert cli.main(['--config', str(config), 'agent-verify', str(result_file), '--environment-receipt', prepared['receipt_file']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'
    saved = Queue(cfg.path('queue.json')).items['i']
    assert saved.verification['verification_isolation'] == 'bwrap'
    assert saved.status == 'verified' and git(source, 'status', '--porcelain') == ''
