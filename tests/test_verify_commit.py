import os
import subprocess
from pathlib import Path

from ossforge import identity, verify
from ossforge.config import Config


def sh(cwd, *c):
    return subprocess.run(c, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path, monkeypatch):
    g = tmp_path / "gc"
    g.write_text("[user]\n name = Default\n email = d@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(g))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    r = tmp_path / "r"
    r.mkdir()
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "config", "core.symlinks", "true")
    (r / "m.py").write_text("def f():\n    return 1\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "init")
    return r


def test_verify_reverts_files_the_fix_added(tmp_path, monkeypatch):
    """A fix that adds a new source file must still be reverted for the 'without fix' run."""
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, "git", "rev-parse", "HEAD")
    sh(r, "git", "checkout", "-qb", "fix")
    (r / "m.py").write_text("from helper import val\n\ndef f():\n    return val\n")
    (r / "helper.py").write_text("val = 2\n")
    (r / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "fix")
    import sys
    res = verify.verify_fix(r, base, Path(sys.executable), ["test_m.py"])
    assert res.failed_without_fix and res.passed_with_fix
    assert (r / "helper.py").read_text() == "val = 2\n"  # restored


def test_verify_reports_when_revert_is_impossible(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    res = verify.verify_fix(r, "deadbeef", Path("python3"), ["x"])
    assert not res.ok


def test_commit_gate_and_artifacts_not_staged(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    cfg = Config(github_user="me", git_name="Owner", git_email="o@example.invalid")
    monkeypatch.setattr(identity, "check_login", lambda c: [])
    (r / "m.py").write_text("def f():\n    return 2\n")
    (r / "pkg.egg-info").mkdir()
    (r / "pkg.egg-info" / "PKG-INFO").write_text("x")
    try:
        identity.commit(cfg, str(r), "fix: f")
        raise AssertionError("default identity must be refused")
    except identity.IdentityError as e:
        assert "effective git author" in str(e)
    assert sh(r, "git", "log", "--oneline").count("\n") == 0  # nothing committed
    assert not identity.apply_local(cfg, str(r))
    identity.commit(cfg, str(r), "fix: f")
    assert sh(r, "git", "log", "-1", "--format=%an|%ae|%cn|%ce") == "Owner|o@example.invalid|Owner|o@example.invalid"
    assert "egg-info" not in sh(r, "git", "show", "--stat", "--format=", "HEAD")


def test_verify_refuses_dirty_tree_before_commands(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'm.py').write_text('changed\n')
    res = verify.verify_fix(r, base, Path('python3'), ['test_m.py'])
    assert not res.ok and not res.evidence and 'dirty' in res.log[0]


def test_native_node_verification_and_receipt(tmp_path, monkeypatch):
    import shutil
    import sys
    import pytest
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    r = _repo(tmp_path, monkeypatch)
    (r / 'm.js').write_text('module.exports = 1;\n')
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'js base')
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'm.js').write_text('module.exports = 2;\n')
    (r / 'test_m.js').write_text("require('node:assert/strict').equal(require('./m'), 2);\n")
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'js fix')
    result = verify.verify_fix(r, base, Path(sys.executable), [], test_command=['node', 'test_m.js'],
                               check_commands=[['node', '--check', 'm.js']])
    assert result.ok and result.evidence['commands'] == [['node', 'test_m.js'], ['node', '--check', 'm.js']]
    assert (r / 'm.js').read_text() == 'module.exports = 2;\n'


def test_collection_error_is_not_failing_regression(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'm.py').write_text('value = 2\n')
    (r / 'test_m.py').write_text('def test_x():\n    assert True\n')
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'fix')
    calls = iter([(2, 'collection error'), (0, 'pass'), (0, 'lint')])
    original = verify._run
    monkeypatch.setattr(verify, '_run', lambda cmd, *a, **k: original(cmd, *a, **k) if cmd[0] == 'git' else next(calls))
    monkeypatch.setattr(verify, 'changed_files', lambda *a: ['m.py', 'test_m.py'])
    assert not verify.verify_fix(r, base, Path(sys.executable), ['test_m.py']).ok


def test_native_missing_command_refuses_and_restores(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'm.py').write_text('value = 2\n')
    (r / 'test_m.py').write_text('def test_x():\n    assert True\n')
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'fix')
    res = verify.verify_fix(r, base, Path(sys.executable), [], test_command=['nonexistent-ossforge-command'])
    assert not res.ok and not res.evidence
    assert (r / 'm.py').read_text() == 'value = 2\n'
    assert not sh(r, 'git', 'status', '--porcelain')


def test_cli_native_mode_does_not_pip_install(tmp_path, monkeypatch, capsys):
    from ossforge import cli, queue, submit
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {tmp_path}/state\n')
    q = queue.Queue(tmp_path / 'state' / 'queue.json')
    q.add(queue.Item('x', 'org/lib', 'a.js', 1, 'bug', 'd', clone=str(tmp_path)))
    q.save()
    body = tmp_path / 'body.md'
    body.write_text('details')
    monkeypatch.setattr(verify, 'venv_python', lambda *a: (_ for _ in ()).throw(AssertionError('no pip mode')))
    monkeypatch.setattr(submit, '_run', lambda *a: 'base')
    seen = []
    def fake(*a, **kw):
        seen.append(kw)
        return verify.VerifyResult(True, True, True, ['a.js'], ['test_a.js'], evidence={'schema': 1})
    monkeypatch.setattr(verify, 'verify_fix', fake)
    args = ['--config', str(cfg), 'verify', 'x', '--branch', 'fix', '--test-command', 'node --test test_a.js',
            '--check-command', 'node --check a.js', '--issue-title', 't', '--pr-title', 't',
            '--issue-body-file', str(body), '--pr-body-file', str(body)]
    assert cli.main(args) == 0
    assert seen[0]['test_command'] == ['node', '--test', 'test_a.js']
    assert seen[0]['check_commands'] == [['node', '--check', 'a.js']]
    assert queue.Queue(q.path).items['x'].verification == {'schema': 1}
    assert 'verified: x' in capsys.readouterr().out


def test_native_verifier_restores_source_deletion_after_baseline(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    (r / 'legacy.py').write_text('obsolete = True\n')
    sh(r, 'git', 'add', '.')
    sh(r, 'git', 'commit', '-qm', 'legacy baseline')
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'legacy.py').unlink()
    (r / 'test_absence.py').write_text("from pathlib import Path\nassert not Path('legacy.py').exists()\n")
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'remove obsolete source')
    result = verify.verify_fix(r, base, Path(sys.executable), [], test_command=[sys.executable, 'test_absence.py'])
    assert result.ok, result.log
    assert not (r / 'legacy.py').exists()
    assert not sh(r, 'git', 'status', '--porcelain')


def test_native_verifier_restores_renamed_source(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    sh(r, 'git', 'mv', 'm.py', 'renamed.py')
    (r / 'test_rename.py').write_text("from pathlib import Path\nassert Path('renamed.py').exists() and not Path('m.py').exists()\n")
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'rename source')
    result = verify.verify_fix(r, base, Path(sys.executable), [], test_command=[sys.executable, 'test_rename.py'])
    assert result.ok, result.log
    assert not (r / 'm.py').exists() and (r / 'renamed.py').exists()
    assert not sh(r, 'git', 'status', '--porcelain')


def test_default_python_verification_does_not_lint_deleted_source(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'm.py').unlink()
    (r / 'test_absence.py').write_text("from pathlib import Path\n\n\ndef test_removed():\n    assert not Path('m.py').exists()\n")
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'remove source')
    result = verify.verify_fix(r, base, Path(sys.executable), ['test_absence.py'])
    assert result.ok, result.log


def test_timed_out_verification_kills_descendant_processes(tmp_path):
    import sys
    import time
    marker = tmp_path / 'late-write'
    child = f"import time;from pathlib import Path;time.sleep(1.5);Path({str(marker)!r}).write_text('late')"
    parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",sys.argv[1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(30)'
    rc, _ = verify._run([sys.executable, '-c', parent, child], tmp_path, timeout=0.2)
    assert rc == 124
    time.sleep(1.7)
    assert not marker.exists(), 'timed-out test left a descendant running after baseline restoration'


def test_added_executable_source_keeps_mode_after_verification(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'helper.py').write_text('value = 2\n')
    (r / 'helper.py').chmod(0o755)
    (r / 'm.py').write_text('from helper import value\n')
    (r / 'test_exec.py').write_text('from m import value\nassert value == 2\n')
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'update-index', '--chmod=+x', 'helper.py')
    sh(r, 'git', 'commit', '-qm', 'executable helper')
    result = verify.verify_fix(r, base, Path(sys.executable), [], test_command=[sys.executable, 'test_exec.py'])
    # The baseline import failure is not a qualified upstream regression. This
    # control specifically verifies restoration independently of test acceptance.
    assert sh(r, 'git', 'ls-files', '--stage', 'helper.py').startswith('100755')
    if os.name != 'nt':
        assert (r / 'helper.py').stat().st_mode & 0o111
    assert not sh(r, 'git', 'status', '--porcelain'), result.log


def test_added_source_symlink_is_not_replaced_with_regular_file(tmp_path, monkeypatch):
    import sys
    r = _repo(tmp_path, monkeypatch)
    base = sh(r, 'git', 'rev-parse', 'HEAD')
    (r / 'alias.py').symlink_to('m.py')
    (r / 'm.py').write_text('def f():\n    return 2\n')
    (r / 'test_alias.py').write_text('from alias import f\nassert f() == 2\n')
    sh(r, 'git', 'add', '-A')
    sh(r, 'git', 'commit', '-qm', 'source alias')
    result = verify.verify_fix(r, base, Path(sys.executable), [], test_command=[sys.executable, 'test_alias.py'])
    assert (r / 'alias.py').is_symlink(), result.log
    assert not sh(r, 'git', 'status', '--porcelain')


def test_commit_ignored_build_directories_do_not_break_staging(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    cfg = Config(github_user='me', git_name='Owner', git_email='o@example.invalid')
    monkeypatch.setattr(identity, 'check_login', lambda c: [])
    assert not identity.apply_local(cfg, str(r))
    (r / '.gitignore').write_text('build/\n.pytest_cache/\n')
    (r / 'build').mkdir()
    (r / 'build' / 'object.o').write_bytes(b'object')
    (r / '.pytest_cache').mkdir()
    (r / '.pytest_cache' / 'cache').write_text('cache')
    (r / 'm.py').write_text('def f():\n    return 2\n')
    identity.commit(cfg, str(r), 'fix: real source')
    files = sh(r, 'git', 'show', '--name-only', '--format=', 'HEAD').splitlines()
    assert 'm.py' in files
    assert not any(f.startswith(('build/', '.pytest_cache/')) for f in files)


def test_commit_explicit_file_list_keeps_deletions_and_literal_names(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    cfg = Config(github_user='me', git_name='Owner', git_email='o@example.invalid')
    monkeypatch.setattr(identity, 'check_login', lambda c: [])
    assert not identity.apply_local(cfg, str(r))
    (r / 'm.py').unlink()
    (r / 'literal[1].py').write_text('value = 1\n')
    identity.commit(cfg, str(r), 'fix: rename source')
    assert not sh(r, 'git', 'status', '--porcelain')
    assert 'literal[1].py' in sh(r, 'git', 'ls-tree', '--name-only', 'HEAD')
    assert 'm.py' not in sh(r, 'git', 'ls-tree', '--name-only', 'HEAD').splitlines()
