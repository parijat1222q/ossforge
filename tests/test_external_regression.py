import copy
import subprocess
import sys
from pathlib import Path

import pytest

from ossforge import evidence, external_regression as ext, verify
from ossforge.config import Config


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / 'clone'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (root/'value.py').write_text('def value(): return 0\n')
    git('add', '.')
    git('commit', '-m', 'base')
    base = git('rev-parse', 'HEAD')
    git('switch', '-c', 'fix')
    (root/'value.py').write_text('def value(): return 1\n')
    git('add', '.')
    git('commit', '-m', 'fix')
    cfg = Config(state_dir=str(tmp_path/'state'))
    script = tmp_path/'regression.py'
    script.write_text('from value import value\nassert value() == 1\n')
    monkeypatch.setattr(ext, 'policy_binding', lambda *a: 'policy')
    return cfg, root, base, script


def frozen(case):
    cfg, root, base, script = case
    return ext.freeze(cfg, 'org/repo', root, [sys.executable, str(script)], base)


def test_external_real_fail_pass_and_stale_replay(case):
    cfg, root, base, _ = case
    rec = frozen(case)
    out = verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert out.ok, out.log
    assert out.failed_without_fix and out.passed_with_fix
    evidence.check(root, base, 'fix', out.evidence, cfg=cfg)
    artifact = ext.artifact(cfg, rec['sha256'])
    artifact.write_text('changed')
    with pytest.raises(evidence.StaleEvidence, match='artifact changed'):
        evidence.check(root, base, 'fix', out.evidence, cfg=cfg)
    assert evidence._git(root, 'status', '--porcelain') == ''


def test_default_source_only_gate_unchanged(case):
    _, root, base, _ = case
    out = verify.verify_fix(root, base, Path('unused'), [])
    assert not out.ok and out.log == ['branch must change both a source file and a test file']


@pytest.mark.parametrize('change', ['missing', 'symlink', 'command', 'root', 'interpreter', 'policy'])
def test_external_stale_inputs_refuse(case, change, monkeypatch):
    cfg, root, _, _ = case
    rec = copy.deepcopy(frozen(case))
    path = ext.artifact(cfg, rec['sha256'])
    if change == 'missing':
        path.unlink()
    elif change == 'symlink':
        data = path.read_bytes()
        path.unlink()
        other = path.parent/'other.py'
        other.write_bytes(data)
        try:
            path.symlink_to(other)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f'Symlink creation unavailable: {exc}')
    elif change == 'command':
        rec['command'].append('-c')
    elif change == 'root':
        rec['cwd'] = str(root.parent)
    elif change == 'interpreter':
        rec['interpreter']['version'] = 'Python 0'
    else:
        monkeypatch.setattr(ext, 'policy_binding', lambda *a: 'changed')
    with pytest.raises((ValueError, OSError)):
        ext.check(cfg, root, rec)


def test_script_that_does_not_import_checkout_cannot_verify(case):
    cfg, root, base, script = case
    script.write_text('raise AssertionError("not a source regression")\n')
    rec = frozen(case)
    out = verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert not out.ok and not out.failed_without_fix
    assert evidence._git(root, 'status', '--porcelain') == ''


def test_source_overlap_and_arbitrary_command_refused(case):
    cfg, root, base, _ = case
    with pytest.raises(ValueError, match='outside'):
        ext.freeze(cfg, 'org/repo', root, [sys.executable, str(root/'value.py')], base)
    with pytest.raises(ValueError, match='command'):
        ext.freeze(cfg, 'org/repo', root, [sys.executable, '-c', 'pass'], base)


def test_policy_requires_explicit_cited_current_scope(tmp_path, monkeypatch):
    from ossforge import policy_review
    cfg = Config(state_dir=str(tmp_path/'state'))
    snap = {'docs': [{'path': 'AGENTS.md', 'url': 'https://example.invalid/AGENTS.md', 'sha': 'blob'}], 'scanned_locations': ['AGENTS.md']}
    monkeypatch.setattr(policy_review, 'check', lambda *a: '')
    with pytest.raises(ValueError, match='explicit'):
        ext.policy_binding(cfg, 'org/repo')
    policy_review.record(cfg, 'org/repo', snap, 'External regression required by source/test separation rule', external_regression_doc='AGENTS.md')
    assert ext.policy_binding(cfg, 'org/repo')
    monkeypatch.setattr(policy_review, 'check', lambda *a: 'policy documents changed')
    with pytest.raises(ValueError, match='documents changed'):
        ext.policy_binding(cfg, 'org/repo')


def test_mutation_during_baseline_restores_source_and_refuses(case):
    cfg, root, base, script = case
    script.write_text('from value import value\nfrom pathlib import Path\nPath(__file__).write_text("mutated")\nassert value() == 1\n')
    # __file__ is deliberately absent from the script namespace; normal scripts
    # cannot silently use the runner's file path. Exercise mutation through cwd.
    script.write_text('from value import value\nfrom pathlib import Path\nfor path in Path(' + repr(str(cfg.path('external_regressions'))) + ').glob("*.py"):\n    path.write_text("mutated")\nassert value() == 1\n')
    rec = frozen(case)
    with pytest.raises(ValueError, match='artifact changed'):
        verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert evidence._git(root, 'status', '--porcelain') == ''
    assert (root/'value.py').read_text() == 'def value(): return 1\n'


def test_cli_requires_explicit_mode_flag_and_cited_doc():
    from ossforge.cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['agent-verify', 'r.json']).external_regression is False
    assert parser.parse_args(['agent-verify', 'r.json', '--external-regression']).external_regression is True
    assert parser.parse_args(['policy-review', 'org/repo', '--external-regression-doc', 'AGENTS.md']).external_regression_doc == 'AGENTS.md'


def test_modified_source_during_test_cannot_count_as_baseline(case):
    cfg, root, base, script = case
    script.write_text('from value import value\nfrom pathlib import Path\nPath("value.py").write_text("def value(): return 99\\n")\nassert value() == 1\n')
    rec = frozen(case)
    with pytest.raises(ValueError, match='changed checkout source'):
        verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert (root/'value.py').read_text() == 'def value(): return 1\n'


def test_no_failing_baseline_does_not_promote(case):
    cfg, root, base, script = case
    script.write_text('from value import value\nassert value() in (0, 1)\n')
    rec = frozen(case)
    out = verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert not out.ok and not out.failed_without_fix


def test_external_receipt_cannot_drop_context_or_change_kind(case):
    cfg, root, base, _ = case
    rec = frozen(case)
    out = verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert out.ok
    with pytest.raises(evidence.StaleEvidence, match='context missing'):
        evidence.check(root, base, 'fix', out.evidence)
    altered = {**out.evidence, 'verification_kind': 'ordinary'}
    with pytest.raises(evidence.StaleEvidence):
        evidence.check(root, base, 'fix', altered, cfg=cfg)


def test_stale_timestamp_pyc_uses_current_source_each_phase(case):
    import os
    import py_compile
    cfg, root, base, _ = case
    value = root/'value.py'
    # Cache the fixed value, then give baseline exactly the same source size
    # and integer timestamp. Without a fresh prefix, baseline wrongly passes.
    os.utime(value, (1000000000, 1000000000))
    py_compile.compile(str(value), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    exclude = root/'.git'/'info'/'exclude'
    exclude.write_text(exclude.read_text() + '\n__pycache__/\n')
    rec = frozen(case)
    old_run = verify._run
    prefixes = []
    def run(command, cwd, seconds=600, env=None):
        if command == rec['command']:
            os.utime(value, (1000000000, 1000000000))
            prefixes.append(Path(env['PYTHONPYCACHEPREFIX']))
            assert prefixes[-1].is_dir() and list(prefixes[-1].iterdir()) == []
        return old_run(command, cwd, seconds, env=env)
    from unittest.mock import patch
    with patch.object(verify, '_run', side_effect=run):
        out = verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert out.ok, out.log
    assert len(prefixes) == 2 and prefixes[0] != prefixes[1]
    assert all(not p.exists() for p in prefixes)


def test_v1_receipt_and_bwrap_no_fallback(case):
    cfg, root, base, _ = case
    rec = frozen(case)
    old = {k:v for k,v in rec.items() if k != 'pycache'}
    with pytest.raises(ValueError, match='malformed'):
        ext.check(cfg, root, old)
    out = verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg, isolation_mode='bwrap')
    assert not out.ok and 'no fallback' in out.log[0]


def test_unconfirmed_workers_preserve_cache_and_baseline(case, monkeypatch):
    from ossforge import processes
    cfg, root, base, _ = case
    rec = frozen(case)
    caches = []
    old_run = verify._run
    def interrupted(command, cwd, seconds=600, env=None):
        if command == rec['command']:
            caches.append(Path(env['PYTHONPYCACHEPREFIX']))
            raise processes.CleanupUnconfirmed('worker still alive')
        return old_run(command, cwd, seconds, env=env)
    monkeypatch.setattr(verify, '_run', interrupted)
    with pytest.raises(processes.CleanupUnconfirmed):
        verify.verify_fix(root, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert caches[0].exists()
    assert (root/'value.py').read_text() == 'def value(): return 0\n'
    import shutil
    shutil.rmtree(caches[0])


def test_literal_placeholder_names_do_not_change_runner(case):
    cfg, root, base, script = case
    renamed = root.parent/'CLONE_SOURCES_SCRIPT'
    root.rename(renamed)
    script.write_text('from value import value\nassert "CLONE SOURCES SCRIPT" == "CLONE SOURCES SCRIPT"\nassert value() == 1\n')
    rec = ext.freeze(cfg, 'org/repo', renamed, [sys.executable, str(script)], base)
    out = verify.verify_fix(renamed, base, Path('unused'), [], test_command=rec['command'], external=rec, cfg=cfg)
    assert out.ok, out.log
