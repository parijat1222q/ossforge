import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ossforge import cli, isolation, queue, verify


def test_requested_unavailable_does_not_fall_back(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation.shutil, 'which', lambda *a: None)
    with pytest.raises(isolation.Unavailable, match='not installed'):
        isolation.wrap(['python', 'test.py'], tmp_path, 'bwrap', [])
    assert isolation.wrap(['python', 'test.py'], tmp_path, 'none', []) == ['python', 'test.py']


@pytest.mark.parametrize('mount', ['/', '/etc', '/tmp', '/home', '/root', '/run', '/proc', '/dev'])
def test_private_or_broad_runtime_mounts_refused(tmp_path, mount):
    with pytest.raises(isolation.Unavailable):
        isolation.wrap(['true'], tmp_path, 'bwrap', [mount])


def test_symlink_and_checkout_mounts_refused(tmp_path):
    alias = tmp_path / 'alias'
    alias.symlink_to('/usr')
    for path in [str(alias), str(tmp_path), 'relative']:
        with pytest.raises(isolation.Unavailable):
            isolation.wrap(['true'], tmp_path, 'bwrap', [path])


def _git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def _native_available(tmp_path):
    if not shutil.which('bwrap'):
        pytest.skip('bubblewrap not installed')
    p = subprocess.run(isolation.wrap(['/usr/bin/true'], tmp_path, 'bwrap', []), capture_output=True)
    if p.returncode:
        pytest.skip('kernel namespaces unavailable: ' + p.stderr.decode()[:150])


def test_public_agent_verify_isolated_fail_before_pass_after(tmp_path, monkeypatch, capsys):
    _native_available(tmp_path)
    clone = tmp_path / 'clone'
    clone.mkdir()
    _git(clone, 'init', '-qb', 'main')
    _git(clone, 'config', 'user.name', 'Test')
    _git(clone, 'config', 'user.email', 'test@example.invalid')
    (clone / 'value.py').write_text('answer = 1\n')
    _git(clone, 'add', '.')
    _git(clone, 'commit', '-qm', 'base')
    base = _git(clone, 'rev-parse', 'HEAD')
    _git(clone, 'update-ref', 'refs/remotes/origin/main', base)
    _git(clone, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    _git(clone, 'checkout', '-qb', 'fix/answer')
    private = tmp_path / 'private.txt'
    private.write_text('private fixture sentinel')
    (clone / 'value.py').write_text('answer = 2\n')
    (clone / 'test_value.py').write_text(f'''import os, socket
from pathlib import Path
from value import answer
assert not Path({str(private)!r}).exists()
assert os.environ['HOME'] == '/tmp/home'
assert 'OSSFORGE_PRIVATE_SENTINEL' not in os.environ
try:
    Path('unexpected-write').write_text('bad')
except OSError:
    pass
else:
    raise RuntimeError('source checkout is writable')
s = socket.socket()
try:
    s.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise RuntimeError('network reachable')
finally:
    s.close()
assert answer == 2
''')
    _git(clone, 'add', '.')
    _git(clone, 'commit', '-qm', 'fix and regression')
    state = tmp_path / 'state'
    q = queue.Queue(state / 'queue.json')
    q.add(queue.Item('a', 'acme/lib', 'value.py', 1, 'bug', 'controlled fixture', clone=str(clone), status='clean'))
    q.save()
    config = tmp_path / 'config.yaml'
    config.write_text(f'state_dir: {state}\nverification_isolation: bwrap\n')
    result = tmp_path / 'result.json'
    result.write_text(json.dumps(dict(schema=1, id='a', base=base, branch='fix/answer',
        test_command=['/usr/bin/python3', 'test_value.py'], check_commands=[['/usr/bin/python3', '-c', 'assert True']],
        failure_code=1, issue_title='controlled answer', issue_body='fixture only',
        pr_title='controlled fix', pr_body='fixture only', consent_issue=0)))
    monkeypatch.setenv('OSSFORGE_PRIVATE_SENTINEL', 'do-not-forward')
    assert cli.main(['--config', str(config), 'agent-verify', str(result)]) == 0, capsys.readouterr()
    saved = queue.Queue(state / 'queue.json').items['a']
    assert saved.status == 'verified'
    assert saved.verification['verification_isolation'] == 'bwrap'
    assert _git(clone, 'status', '--porcelain') == ''
    assert private.read_text() == 'private fixture sentinel'


def test_requested_isolation_validation_precedes_source_revert(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.evidence, 'capture', lambda *a: {'branch': 'fix'})
    monkeypatch.setattr(verify, 'changed_files', lambda *a: ['value.py', 'test_value.py'])
    monkeypatch.setattr(isolation.shutil, 'which', lambda *a: None)
    monkeypatch.setattr(verify, '_run', lambda *a: pytest.fail('must not run host fallback or alter source'))
    result = verify.verify_fix(tmp_path, 'base', Path('unused'), [], test_command=['python', 'test.py'], isolation_mode='bwrap')
    assert not result.ok and 'not installed' in result.log[0]


def test_kernel_unavailable_stops_before_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation.shutil, 'which', lambda *a: '/usr/bin/bwrap')
    monkeypatch.setattr(verify.evidence, 'capture', lambda *a: {'branch': 'fix'})
    monkeypatch.setattr(verify, 'changed_files', lambda *a: ['value.py', 'test_value.py'])
    calls = []
    def run(command, *args):
        calls.append(command)
        assert command[-1] == '/usr/bin/true'
        return 1, 'namespace denied'
    monkeypatch.setattr(verify, '_run', run)
    result = verify.verify_fix(tmp_path, 'base', Path('unused'), [], test_command=['python', 'test.py'], isolation_mode='bwrap')
    assert not result.ok and 'unavailable' in result.log[0] and len(calls) == 1


def test_explicit_prepared_runtime_mount_is_readonly(tmp_path):
    clone = tmp_path / 'source'
    clone.mkdir()
    _native_available(clone)
    runtime = tmp_path / 'prepared-runtime'
    runtime.mkdir()
    marker = runtime / 'dependency.txt'
    marker.write_text('prepared dependency')
    script = f'''from pathlib import Path
p = Path({str(marker)!r})
assert p.read_text() == 'prepared dependency'
try:
    p.write_text('modified')
except OSError:
    pass
else:
    raise RuntimeError('runtime is writable')
'''
    rc, out = verify._run(isolation.wrap(['/usr/bin/python3', '-c', script], clone, 'bwrap', [str(runtime)]), clone)
    assert rc == 0, out
    assert marker.read_text() == 'prepared dependency'


@pytest.mark.parametrize('paths', ['/usr', [False], None])
def test_invalid_mount_list_refused(tmp_path, paths):
    with pytest.raises(isolation.Unavailable):
        isolation.wrap(['true'], tmp_path, 'bwrap', paths)


def test_symlink_ancestor_mount_refused(tmp_path):
    real = tmp_path / 'real'
    (real / 'runtime').mkdir(parents=True)
    alias = tmp_path / 'alias'
    alias.symlink_to(real)
    source = tmp_path / 'source'
    source.mkdir()
    with pytest.raises(isolation.Unavailable):
        isolation.wrap(['true'], source, 'bwrap', [str(alias / 'runtime')])
