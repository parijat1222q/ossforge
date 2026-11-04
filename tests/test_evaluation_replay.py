import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('ossforge_developer_evaluation', Path(__file__).resolve().parents[1]/'scripts/evaluate.py')
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def test_counts_keep_skips_and_failures_separate(tmp_path):
    report = tmp_path/'results.xml'
    report.write_text('<testsuites><testsuite tests="5" failures="1" errors="1" skipped="2"/></testsuites>')
    assert evaluation.summarize(report) == {'tests': 5, 'failures': 1, 'errors': 1, 'skipped': 2, 'passed': 1}


def test_stages_only_select_existing_public_test_paths():
    assert set(evaluation.STAGES) == {'selection', 'budget_checkpoint', 'policy_duplicates', 'verification', 'onboarding'}
    for paths in evaluation.STAGES.values():
        assert paths
        assert all(path.startswith('tests/test_') and (evaluation.ROOT/path).is_file() for path in paths)


def test_no_passing_cases_cannot_be_green(monkeypatch, capsys):
    from types import SimpleNamespace

    def run(command, **kwargs):
        path = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('--junitxml=')))
        path.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="2"/></testsuites>')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(evaluation.subprocess, 'run', run)
    assert evaluation.main(['--stage', 'verification']) == 1
    assert '"status": "failed"' in capsys.readouterr().out


def test_nonzero_stage_reports_failure_and_output(monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr(evaluation.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='assertion failed', stderr=''))
    assert evaluation.main(['--stage', 'selection']) == 1
    output = capsys.readouterr()
    assert 'assertion failed' in output.err
    assert '"live_ide_proof": false' in output.out


def test_timeout_is_error_not_success(monkeypatch, capsys):
    import subprocess

    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 180)
    monkeypatch.setattr(evaluation.subprocess, 'run', run)
    assert evaluation.main(['--stage', 'selection']) == 1
    assert '"status": "error"' in capsys.readouterr().out
