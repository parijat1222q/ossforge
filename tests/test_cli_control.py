import pytest

from ossforge import cli


def test_stop_and_resume_ignore_corrupt_unrelated_queue(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    queue = state / 'queue.json'
    queue.write_text('{partial')
    stop = tmp_path / 'control' / 'STOP'
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {state}\nkill_file: {stop}\n')
    assert cli.main(['--config', str(cfg), 'stop']) == 0
    assert stop.read_text() == 'stopped\n'
    assert queue.read_text() == '{partial'
    assert cli.main(['--config', str(cfg), 'resume']) == 0
    assert not stop.exists() and queue.read_text() == '{partial'


@pytest.mark.parametrize('args', [
    ['environment-prepare', 'missing', 'recipe.json'], ['agent-verify', 'result.json'], ['verify', 'missing', '--branch', 'fix', '--issue-title', 'bug',
     '--issue-body-file', 'issue.txt', '--pr-title', 'fix', '--pr-body-file', 'pr.txt'],
    ['prepare', 'missing'], ['commit', 'missing', '-m', 'fix'], ['hunt', 'org/repo'], ['audit', 'org/repo'],
    ['hold', 'missing', '--reason', 'hold'], ['unhold', 'missing'], ['reject', 'missing'],
    ['research-repo', '--defer', 'hold'], ['research-session', '--seconds', '60'],
    ['policy-review', 'org/repo', '--accept', '--note', 'reviewed'],
    ['review-qualification', 'org/repo', '1', '--defer', 'hold'],
    ['submit', 'missing', '--yes'], ['loop', '--once'], ['discover'], ['bootstrap'], ['digest'],
    ['patrol'], ['scout'], ['features']])
def test_direct_mutating_commands_stop_before_state_or_execution(tmp_path, monkeypatch, capsys, args):
    state = tmp_path / 'state'
    state.mkdir()
    queue = state / 'queue.json'
    queue.write_text('{corrupt preserved')
    stop = tmp_path / 'STOP'
    stop.touch()
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {state}\nkill_file: {stop}\n')
    monkeypatch.setattr(cli, '_ctx', lambda *a: pytest.fail('STOP must precede state reads/execution'))
    assert cli.main(['--config', str(cfg), *args]) == 1
    assert 'REFUSED:' in capsys.readouterr().err
    assert queue.read_text() == '{corrupt preserved' and stop.exists()


def test_read_only_queue_remains_available_under_stop(tmp_path, capsys):
    state = tmp_path / 'state'
    stop = tmp_path / 'STOP'
    stop.touch()
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {state}\nkill_file: {stop}\n')
    assert cli.main(['--config', str(cfg), '--json', 'queue']) == 0
    assert capsys.readouterr().out.strip() == '[]'
    assert stop.exists()


def test_stopped_checkpoint_precedes_corrupt_queue_and_queue_refuses_cleanly(tmp_path, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'queue.json').write_text('{broken')
    stop = tmp_path / 'STOP'
    stop.touch()
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {state}\nkill_file: {stop}\n')
    assert cli.main(['--config', str(cfg), 'agent-next']) == 0
    assert 'stopped' in capsys.readouterr().out
    assert cli.main(['--config', str(cfg), 'queue']) == 1
    captured = capsys.readouterr()
    assert 'REFUSED:' in captured.err and 'Traceback' not in captured.err
    assert (state / 'queue.json').read_text() == '{broken'


@pytest.mark.parametrize('args', [['research-session'], ['preparation-task']])
def test_read_status_available_under_stop(tmp_path, capsys, args):
    state = tmp_path / 'state'
    stop = tmp_path / 'STOP'
    stop.touch()
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {state}\nkill_file: {stop}\n')
    assert cli.main(['--config', str(cfg), *args]) == 0
    assert 'kill switch' not in capsys.readouterr().err
    assert stop.exists()


@pytest.mark.parametrize('scope', ['default', 'research'])
def test_stopped_payload_preserves_scope_and_contract_before_ctx(tmp_path, monkeypatch, capsys, scope):
    import json
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'queue.json').write_text('{broken')
    stop = tmp_path / 'STOP'
    stop.touch()
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'state_dir: {state}\nkill_file: {stop}\nrepo_first: true\n')
    monkeypatch.setattr(cli, '_ctx', lambda *a: pytest.fail('no state load under STOP'))
    assert cli.main(['--config', str(cfg), 'agent-next', '--scope', scope]) == 0
    out = json.loads(capsys.readouterr().out)
    expected = ['ossforge', '--config', str(cfg), 'agent-next']
    if scope == 'research':
        expected += ['--scope', 'research']
    assert out['continue_argv'] == expected and out['status'] == 'stopped'
    assert 'owner authorization' in out['publication'] and out['local_only']
    assert (state / 'queue.json').read_text() == '{broken' and stop.exists()
