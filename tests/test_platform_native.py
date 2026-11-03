"""Runs on real Windows and Linux. No substituted lock backend or fake CLI."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ossforge import locking, repo_research, runs, native_replay, verify, processes
from ossforge.config import Config


def child(code, *args):
    return subprocess.Popen([sys.executable, '-c', code, *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def wait_file(path, proc):
    deadline = time.monotonic() + 15
    while not path.exists():
        if proc.poll() is not None:
            pytest.fail(str(proc.communicate()))
        if time.monotonic() >= deadline:
            proc.kill()
            pytest.fail('child readiness deadline')
        time.sleep(0.02)


@pytest.mark.parametrize('filename', ['run.lock', 'publication.lock', 'ratelimit.lock'])
def test_real_exclusive_lock_same_process_and_subprocess(tmp_path, filename):
    path = tmp_path / filename
    with path.open('a+') as first, path.open('a+') as second:
        first.seek(0, 2)
        locking.acquire(first)
        with pytest.raises(BlockingIOError):
            locking.acquire(second)
        proc = child('''from ossforge import locking
import sys
with open(sys.argv[1],'a+') as f:
 try:locking.acquire(f)
 except BlockingIOError:sys.exit(23)
 sys.exit(99)
''', path)
        assert proc.communicate(timeout=15)[1] == ''
        assert proc.returncode == 23
        locking.release(first)
        locking.acquire(second)
        locking.release(second)
    with path.open('a+') as new:
        locking.acquire(new)
        locking.release(new)


def test_blocking_budget_waits_past_msvcrt_ten_second_cap(tmp_path):
    ready = tmp_path / 'ready'
    path = tmp_path / 'ratelimit.lock'
    proc = child('''from ossforge import gh
from pathlib import Path
import sys,time
gh._COOLDOWN_FILE=Path(sys.argv[1])/'cooldown.json'
with gh._shared_lock():
 Path(sys.argv[2]).write_text('ready')
 time.sleep(11.5)
''', tmp_path, ready)
    try:
        wait_file(ready, proc)
        from ossforge import gh
        old = gh._COOLDOWN_FILE
        gh._COOLDOWN_FILE = tmp_path / 'cooldown.json'
        try:
            began = time.monotonic()
            with gh._shared_lock():
                assert time.monotonic() - began >= 10
        finally:
            gh._COOLDOWN_FILE = old
        assert path.exists()
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_real_run_and_research_share_lock_without_journal_mutation(tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'state'))
    ready = tmp_path / 'ready'
    proc = child('''from ossforge.runs import Run
from pathlib import Path
import sys,time
with Run(Path(sys.argv[1])):
 Path(sys.argv[2]).write_text('ready')
 time.sleep(30)
''', cfg.state_dir, ready)
    try:
        wait_file(ready, proc)
        before = cfg.path('run.json').read_bytes()
        with pytest.raises(runs.Busy, match='another run'):
            with runs.Run(Path(cfg.state_dir)):
                pytest.fail('entered competing run')
        with pytest.raises(repo_research.Invalid, match='another run'):
            with repo_research.locked(cfg):
                pytest.fail('entered competing research')
        assert cfg.path('run.json').read_bytes() == before
    finally:
        proc.kill()
        proc.wait()
    # OS releases ownership even on process death; journal is reconciled normally.
    with runs.Run(Path(cfg.state_dir)):
        pass
    assert list(cfg.path('runs').glob('*.json'))


def test_publication_lock_and_exception_release(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    with native_replay.publication_locked(cfg):
        with pytest.raises(native_replay.Invalid, match='another publication'):
            with native_replay.publication_locked(cfg):
                pass
    with pytest.raises(RuntimeError):
        with native_replay.publication_locked(cfg):
            raise RuntimeError('fixture')
    with native_replay.publication_locked(cfg):
        pass


def test_real_research_state_replacement(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    with repo_research.locked(cfg):
        repo_research._save(cfg, {'schema': 1, 'cursor': 1, 'lead': None})
        repo_research._save(cfg, {'schema': 1, 'cursor': 2, 'lead': None})
    assert repo_research.load(cfg)['cursor'] == 2
    assert len(list(tmp_path.glob('repo_research.json*'))) == 1


def test_actual_cli_help_checkpoint_and_stopped_state(tmp_path):
    config = tmp_path / 'config.yaml'
    stop = tmp_path / 'STOP'
    stop.touch()
    config.write_text(json.dumps({'state_dir': str(tmp_path / 'state'), 'kill_file': str(stop)}))
    for args in [['--help'], ['--config', str(config), '--json', 'agent-next']]:
        proc = subprocess.run([sys.executable, '-m', 'ossforge.cli', *args], capture_output=True,
                              text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        assert 'stopped' in proc.stdout if 'agent-next' in args else 'usage:' in proc.stdout
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('runner', ['verify', 'driver'])
def test_native_timeout_kills_parent_and_grandchild_before_return(tmp_path, runner):
    # Child would overwrite source after timeout if tree cleanup were incomplete.
    sentinel = tmp_path / 'source.py'
    sentinel.write_text('original')
    ready = tmp_path / 'ready'
    grandchild = "import time,sys;from pathlib import Path;Path(sys.argv[2]).write_text('ready');time.sleep(3);Path(sys.argv[1]).write_text('escaped')"
    parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]);time.sleep(30)'
    command = [sys.executable, '-c', parent, grandchild, str(sentinel), str(ready)]
    if runner == 'verify':
        code, message = verify._run(command, tmp_path, timeout=1.5)
        assert code == 124, message
    else:
        from ossforge import agent_run
        with pytest.raises(subprocess.TimeoutExpired):
            agent_run.invoke(command, tmp_path, tmp_path / 'task', tmp_path / 'result', 1.5)
    assert ready.exists(), 'target did not start before timeout'
    sentinel.write_text('restored')
    time.sleep(3.2)
    assert sentinel.read_text() == 'restored'


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object native fail-closed probe')
def test_job_assignment_failure_does_not_start_target(tmp_path):
    sentinel = tmp_path / 'started'
    launcher = Path(processes.__file__).with_name('windows_launcher.py')
    proc = subprocess.run([sys.executable, str(launcher), '0', '0', sys.executable, '-c',
                           'from pathlib import Path;import sys;Path(sys.argv[1]).touch()', str(sentinel)],
                          capture_output=True, timeout=15)
    assert proc.returncode != 0
    assert not sentinel.exists()


def test_crossprocess_publication_excludes_submit_and_preserves_partial_state(tmp_path):
    from ossforge import submit, safety
    from ossforge.limiter import DailyLimiter
    from ossforge.queue import Item, Queue
    cfg = Config(state_dir=str(tmp_path / 'state'))
    queue = Queue(cfg.path('queue.json'))
    item = Item('i', 'org/repo', 'm.py', 1, 'bug', 'fixture', status='verified',
                submitted={'issue_url': 'https://github.com/org/repo/issues/1'})
    queue.add(item)
    queue.save()
    before = queue.path.read_bytes()
    ready = tmp_path / 'ready'
    proc = child('''from ossforge import native_replay
from ossforge.config import Config
from pathlib import Path
import sys,time
with native_replay.publication_locked(Config(state_dir=sys.argv[1])):
 Path(sys.argv[2]).write_text('ready')
 time.sleep(30)
''', cfg.state_dir, ready)
    try:
        wait_file(ready, proc)
        with pytest.raises(submit.Refused, match='another publication'):
            submit.submit(item, DailyLimiter(cfg.path('daily.json')), cfg,
                          safety.Audit(cfg.path('audit.jsonl')), execute=True)
        assert queue.path.read_bytes() == before
        assert not cfg.path('audit.jsonl').exists()
    finally:
        proc.kill()
        proc.wait()
    with native_replay.publication_locked(cfg):
        pass


def test_actual_host_fail_pass_verification_and_restoration(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-qb', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (tmp_path / 'value.py').write_text('value=1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (tmp_path / 'value.py').write_text('value=2\n')
    (tmp_path / 'test_value.py').write_text('from value import value\nassert value == 2\n')
    git('add', '.')
    git('commit', '-qm', 'fix plus regression')
    result = verify.verify_fix(tmp_path, base, Path(sys.executable), [],
                               test_command=[sys.executable, 'test_value.py'])
    assert result.ok, result.log
    assert git('status', '--porcelain') == ''
    assert (tmp_path / 'value.py').read_text() == 'value=2\n'


def test_failed_research_replace_preserves_old_state(tmp_path, monkeypatch):
    from ossforge import durability
    cfg = Config(state_dir=str(tmp_path))
    repo_research._save(cfg, {'schema': 1, 'cursor': 1, 'lead': None})
    before = cfg.path('repo_research.json').read_bytes()
    def fail(*args):
        raise OSError('fixture write failure')
    monkeypatch.setattr(durability, 'replace', fail)
    with pytest.raises(OSError, match='fixture'):
        repo_research._save(cfg, {'schema': 1, 'cursor': 2, 'lead': None})
    assert cfg.path('repo_research.json').read_bytes() == before


def test_requested_linux_isolation_refuses_other_platform_before_launch(tmp_path, monkeypatch):
    from ossforge import isolation
    monkeypatch.setattr(isolation.sys, 'platform', 'win32')
    with pytest.raises(isolation.Unavailable, match='require Linux; no host fallback'):
        isolation.wrap([sys.executable, '-c', 'raise SystemExit(99)'], tmp_path, 'bwrap', [])


def test_unconfirmed_cleanup_never_restores_source(tmp_path, monkeypatch):
    # Control the exceptional API path; native parent/grandchild cleanup above
    # proves the actual success path on each OS. No fixture proves Windows APIs.
    source = tmp_path / 'value.py'
    source.write_text('fixed')
    monkeypatch.setattr(verify.evidence, 'capture', lambda *a, **k: {'branch': 'fix', 'tree': 'tree'})
    monkeypatch.setattr(verify, 'changed_files', lambda *a: ['value.py', 'test_value.py'])
    calls = []
    def run(command, *args):
        calls.append(command)
        if command[:2] == ['git', 'cat-file']:
            return 0, ''
        if command[:2] == ['git', 'checkout']:
            source.write_text('baseline')
            return 0, ''
        if command[:2] == ['git', 'restore']:
            pytest.fail('restoration under potentially live workers')
        raise processes.CleanupUnconfirmed('fixture unconfirmed worker')
    monkeypatch.setattr(verify, '_run', run)
    with pytest.raises(processes.CleanupUnconfirmed, match='left at baseline'):
        verify.verify_fix(tmp_path, 'base', Path(sys.executable), [], test_command=['native-test'])
    assert source.read_text() == 'baseline'
    assert not any(command[:2] == ['git', 'restore'] for command in calls)


@pytest.mark.skipif(os.name != 'nt', reason='real Windows assignment failure before readiness')
def test_assigned_launcher_failure_before_ready_never_starts_target(tmp_path, monkeypatch):
    original = Path(processes.__file__).with_name('windows_launcher.py').read_text()
    # Execute actual assignment/CloseHandle calls, then fail the actual SetEvent
    # call with invalid handle zero. No API mock or target replacement.
    marker = tmp_path / 'assigned'
    altered = original.replace('    if not api.SetEvent(ready):',
                               '    from pathlib import Path\n    Path(' + repr(str(marker)) + ').touch()\n    ready = 0\n    if not api.SetEvent(ready):')
    (tmp_path / 'windows_launcher.py').write_text(altered)
    monkeypatch.setattr(processes, '__file__', str(tmp_path / 'processes.py'))
    target = tmp_path / 'target'
    with pytest.raises(OSError, match='exited before assignment'):
        with processes.launched([sys.executable, '-c', 'from pathlib import Path;import sys;Path(sys.argv[1]).touch()',
                                  str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE):
            pytest.fail('yielded failed launcher')
    assert marker.exists(), 'actual assignment did not occur'
    assert not target.exists()


@pytest.mark.skipif(os.name != 'nt', reason='real Windows preassignment timeout cleanup')
def test_readiness_timeout_kills_launcher_before_late_assignment(tmp_path, monkeypatch):
    original = Path(processes.__file__).with_name('windows_launcher.py').read_text()
    marker = tmp_path / 'late-assignment'
    altered = original.replace('    api = ctypes.WinDLL',
                               '    import time\n    from pathlib import Path\n    time.sleep(2)\n    Path(' + repr(str(marker)) + ').touch()\n    api = ctypes.WinDLL')
    (tmp_path / 'windows_launcher.py').write_text(altered)
    monkeypatch.setattr(processes, '__file__', str(tmp_path / 'processes.py'))
    monkeypatch.setattr(processes, 'ASSIGNMENT_TIMEOUT', 0.3)
    target = tmp_path / 'target'
    with pytest.raises(OSError, match='readiness timed out'):
        with processes.launched([sys.executable, '-c', 'from pathlib import Path;import sys;Path(sys.argv[1]).touch()',
                                  str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE):
            pytest.fail('yielded before assignment')
    time.sleep(2.2)
    assert not marker.exists(), 'launcher survived and reached late assignment'
    assert not target.exists()
