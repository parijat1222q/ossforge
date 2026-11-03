import hashlib
import json
import os
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
    reason = ''
    if not shutil.which('bwrap'):
        reason = 'bubblewrap unavailable'
    else:
        try:
            p = subprocess.run(environment._setup_wrap(['/usr/bin/python3', '-c', environment.PROBE], source, []), capture_output=True, timeout=30)
            if p.returncode:
                reason = 'system venv probe failed: ' + (p.stdout + p.stderr).decode()
        except (OSError, subprocess.TimeoutExpired) as exc:
            reason = str(exc)
    if reason:
        if os.environ.get('OSSFORGE_REQUIRE_NATIVE_SETUP') == '1':
            pytest.fail(reason)
        pytest.skip(reason)


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
    assert out['status'] == 'environment_failed' and out['exit_code'] == 3, out
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
    with pytest.raises(environment.Invalid, match='setup source snapshot changed'):
        environment.status(cfg, q, 'i', receipt)


def test_setup_timeout_does_not_save_receipt(setup):
    cfg, q, source, path, data = setup
    available(source)
    data['commands'] = [['{python}', '-c', 'import time;time.sleep(30)']]
    path.write_text(json.dumps(data))
    out = environment.prepare(cfg, q, 'i', path, timeout=1)
    assert out['status'] == 'environment_failed' and out['exit_code'] == 124
    assert not (Path(out['logs']) / 'receipt.json').exists()




def test_stale_noneditable_copy_cannot_keep_current_receipt(setup):
    cfg, q, source, path, data = setup
    available(source)
    data['commands'] = [['{python}', '-c', 'from pathlib import Path;Path("{environment}/venv/installed_copy.py").write_text(Path("{clone}/value.py").read_text())']]
    path.write_text(json.dumps(data))
    out = environment.prepare(cfg, q, 'i', path)
    installed = Path(out['environment']) / 'venv/installed_copy.py'
    assert installed.read_text() == 'value=1\n'
    (source / 'value.py').write_text('value=2\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'later fix')
    assert installed.read_text() == 'value=1\n'  # stale installed copy really exists
    with pytest.raises(environment.Invalid, match='source snapshot changed'):
        environment.status(cfg, q, 'i', Path(out['receipt_file']))


@pytest.mark.parametrize('ignored', [True, False])
def test_untracked_setup_inputs_refused_even_when_ignored(setup, ignored):
    cfg, q, source, path, data = setup
    if ignored:
        (source / '.gitignore').write_text('local-input\n')
        git(source, 'add', '.')
        git(source, 'commit', '-qm', 'ignore local input')
    (source / 'local-input').write_text('could affect arbitrary setup argv')
    with pytest.raises(environment.Invalid, match='untracked or ignored'):
        environment.prepare(cfg, q, 'i', path)
    assert not (Path(cfg.work_dir) / 'environments').exists()


@pytest.mark.parametrize('flag', ['--skip-worktree', '--assume-unchanged'])
def test_hidden_tracked_modification_refused_before_setup(setup, flag):
    cfg, q, source, path, data = setup
    git(source, 'update-index', flag, 'value.py')
    (source / 'value.py').write_text('hidden change=2\n')
    assert git(source, 'status', '--porcelain') == ''
    with pytest.raises(environment.Invalid, match='hidden index flags'):
        environment.prepare(cfg, q, 'i', path)
    assert not (Path(cfg.work_dir) / 'environments').exists()


@pytest.mark.parametrize('flag', ['--skip-worktree', '--assume-unchanged'])
def test_hidden_tracked_modification_invalidates_existing_receipt(setup, flag):
    cfg, q, source, path, data = setup
    available(source)
    out = environment.prepare(cfg, q, 'i', path)
    git(source, 'update-index', flag, 'value.py')
    (source / 'value.py').write_text('hidden change=2\n')
    assert git(source, 'status', '--porcelain') == ''
    with pytest.raises(environment.Invalid, match='hidden index flags'):
        environment.status(cfg, q, 'i', Path(out['receipt_file']))


def test_missing_system_venv_refused_before_output_no_fallback(setup, monkeypatch):
    cfg, q, source, path, data = setup
    original = environment.subprocess.run
    def run(command, *a, **kw):
        if command[0] == 'git':
            return original(command, *a, **kw)
        assert command[-2:] == ['-c', environment.PROBE]
        return subprocess.CompletedProcess(command, 1, '', 'ModuleNotFoundError: ensurepip')
    monkeypatch.setattr(environment.isolation.shutil, 'which', lambda *a: '/usr/bin/bwrap')
    monkeypatch.setattr(environment.subprocess, 'run', run)
    with pytest.raises(environment.Invalid, match='system Python venv.*no host fallback'):
        environment.prepare(cfg, q, 'i', path)
    assert not (Path(cfg.work_dir) / 'environments').exists()


@pytest.mark.parametrize('error', [OSError('missing system python'), subprocess.TimeoutExpired('probe', 30)])
def test_probe_launch_or_timeout_is_clean_refusal(setup, monkeypatch, error):
    cfg, q, source, path, data = setup
    original = environment.subprocess.run
    def run(command, *a, **kw):
        if command[0] == 'git':
            return original(command, *a, **kw)
        raise error
    monkeypatch.setattr(environment.isolation.shutil, 'which', lambda *a: '/usr/bin/bwrap')
    monkeypatch.setattr(environment.subprocess, 'run', run)
    with pytest.raises(environment.Invalid, match='probe unavailable; no host fallback'):
        environment.prepare(cfg, q, 'i', path)
    assert not (Path(cfg.work_dir) / 'environments').exists()


def test_failed_worker_output_not_lost_to_empty_step_log(setup, monkeypatch):
    cfg, q, source, path, data = setup
    available(source)
    from ossforge import verify
    def run(command, cwd, timeout):
        # The generated worker has not started, so no step file exists.
        return 124, 'timeout after 1s; test process group terminated'
    monkeypatch.setattr(verify, '_run', run)
    out = environment.prepare(cfg, q, 'i', path)
    assert out['exit_code'] == 124 and 'timeout after 1s' in out['reason']
    assert not (Path(out['logs']) / 'receipt.json').exists()


def test_setup_ca_is_narrow_readonly_and_receipted(setup, monkeypatch):
    cfg, q, source, path, data = setup
    available(source)
    ca = environment.CA_BUNDLE
    if not ca.exists():
        if os.environ.get('OSSFORGE_REQUIRE_NATIVE_SETUP') == '1':
            pytest.fail('this regression needs the public system CA fixture')
        pytest.skip('public system CA fixture unavailable')
    data['commands'] = [['{python}', '-c', """import ssl
from pathlib import Path
ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')
assert not Path('/etc/passwd').exists()
p = Path('/etc/ssl/certs/ca-certificates.crt')
try:
    p.write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('CA writable')
"""]]
    path.write_text(json.dumps(data))
    out = environment.prepare(cfg, q, 'i', path)
    assert out['status'] == 'environment_prepared', out
    assert out['system_inputs'] == {str(ca): hashlib.sha256(ca.read_bytes()).hexdigest()}
    receipt = Path(out['receipt_file'])
    assert environment.status(cfg, q, 'i', receipt) == out
    monkeypatch.setattr(environment, '_system_inputs', lambda: {str(ca): 'changed'})
    with pytest.raises(environment.Invalid, match='system CA input changed'):
        environment.status(cfg, q, 'i', receipt)


def test_setup_ca_refuses_symlink_and_noncertificate(tmp_path, monkeypatch):
    ca = tmp_path / 'ca.crt'
    ca.write_text('-----BEGIN PRIVATE KEY-----\nnot public\n-----END PRIVATE KEY-----')
    monkeypatch.setattr(environment, 'CA_BUNDLE', ca)
    with pytest.raises(environment.Invalid, match='only public certificate'):
        environment._system_inputs()
    link = tmp_path / 'link'
    link.symlink_to(ca)
    monkeypatch.setattr(environment, 'CA_BUNDLE', link)
    with pytest.raises(environment.Invalid, match='non-symlink'):
        environment._system_inputs()


@pytest.mark.parametrize('error', [UnicodeError('bad encoding'), OSError('unreadable')])
def test_ca_read_errors_are_clean_refusals(tmp_path, monkeypatch, error):
    ca = tmp_path / 'ca.crt'
    ca.write_text('placeholder')
    monkeypatch.setattr(environment, 'CA_BUNDLE', ca)
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(Path, 'read_text', fail)
    with pytest.raises(environment.Invalid, match='cannot be read safely'):
        environment._system_inputs()


def test_explicit_base_actual_setup_receipt_and_same_sha_swap_refusal(setup):  # noqa: F811
    from ossforge import submit
    cfg,q,source,recipe_file,recipe=setup
    available(source)
    base=git(source,'rev-parse','origin/HEAD')
    git(source,'update-ref','refs/remotes/origin/ABCD',base)
    selected={'repo':q.items['i'].repo,'branch':'ABCD','revision':base}
    q.items['i'].contribution_base=selected
    q.save()
    out=environment.prepare(cfg,q,'i',recipe_file)
    rec=environment.status(cfg,q,'i',Path(out['receipt_file']))
    assert rec['contribution_base']==rec['checkout']['contribution_base']==selected
    git(source,'update-ref','refs/remotes/origin/other',base)
    q.items['i'].contribution_base={**selected,'branch':'other'}
    with pytest.raises(ValueError,match='identity'):
        environment.status(cfg,q,'i',Path(out['receipt_file']))
    assert submit._item_base(q.items['i'])==base  # same SHA is not enough for receipt reuse
