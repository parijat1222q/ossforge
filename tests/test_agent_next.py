from pathlib import Path

import pytest

from ossforge import agent_next, driver, evidence
from ossforge.config import Config
from ossforge.queue import Item, Queue


def fixture(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    q = Queue(tmp_path / 'q.json')
    item = Item('a', 'owner/repo', 'x.py', 1, 'bug', 'candidate', status='verified',
                clone=str(tmp_path), branch='fix', verification={'schema': 1})
    q.add(item)
    monkeypatch.setattr(agent_next.submit, '_base', lambda *a: 'base')
    monkeypatch.setattr(evidence, 'check', lambda *a, **kw: None)
    return cfg, q, item


def test_verified_work_not_mistaken_for_no_work(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    assert driver.task(q)['status'] == 'no_work'  # actual old starter-prompt dead end
    out = agent_next.next_step(cfg, q, 'custom config.yaml')
    assert out['status'] == 'verified_ready'
    assert out['next_argv'] == ['ossforge', '--config', 'custom config.yaml', 'submit', 'a']
    assert '--yes' not in out['next_argv'] and out['local_only']
    assert not q.path.exists()  # read-only checkpoint, no implicit state writes


def test_stop_precedes_any_work_or_remote_probe(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    Path(cfg.kill_file).touch()
    monkeypatch.setattr(agent_next.submit, '_base', lambda *a: pytest.fail('no work under STOP'))
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'stopped'


def test_partial_effect_requires_remote_reconciliation_before_retry(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    item.submitted = {'issue_url': 'https://github.com/owner/repo/issues/1', 'fork_pushed': True}
    monkeypatch.setattr(agent_next.submit, '_base', lambda *a: pytest.fail('partial must not auto retry'))
    out = agent_next.next_step(cfg, q, 'c')
    assert out['status'] == 'reconcile_remote' and out['saved_progress'] == item.submitted
    assert '--yes' not in out['after_reconciliation']


def test_stale_receipt_preserves_work(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(evidence, 'check', lambda *a, **kw: (_ for _ in ()).throw(evidence.StaleEvidence('tree changed')))
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'inspect_local'
    assert item.status == 'verified'


def test_completed_url_not_republished_and_no_work_is_not_global_absence(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    item.status = 'submitted'
    item.submitted = {'pr': 'https://github.com/owner/repo/pull/2'}
    out = agent_next.next_step(cfg, q, 'c')
    assert out['status'] == 'no_work' and out['completed'][0]['saved_pr'] == item.submitted['pr']
    assert out['next_argv'][-3:] == ['loop', '--once', '--prepare-only']


def test_clean_and_dirty_candidate_paths(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    item.status = 'clean'
    monkeypatch.setattr(evidence, 'capture', lambda *a: {'branch': 'main', 'tree': 'tree'})
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'work_candidate'
    monkeypatch.setattr(evidence, 'capture', lambda *a: (_ for _ in ()).throw(evidence.StaleEvidence('dirty')))
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'inspect_local'


def test_completed_progress_on_verified_item_is_not_a_new_submit(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    item.submitted = {'pr_url': 'https://github.com/owner/repo/pull/2'}
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'no_work'


def test_real_cli_checkpoint_config_stop_and_no_network(tmp_path):
    import json
    import os
    import subprocess
    import sys
    import yaml
    cfg = tmp_path / 'custom config.yaml'
    stop = tmp_path / 'STOP'
    cfg.write_text(yaml.safe_dump({'state_dir': str(tmp_path / 'state'), 'kill_file': str(stop)}))
    stop.touch()
    env = dict(os.environ)
    # No gh executable is available. A checkpoint must not need a remote API.
    env['PATH'] = str(tmp_path)
    entry = [sys.executable, '-c',
             "from importlib.metadata import distribution; "
             "e = next(e for e in distribution('ossforge').entry_points "
             "if e.group == 'console_scripts' and e.name == 'ossforge'); "
             "raise SystemExit(e.load()())"]
    p = subprocess.run([*entry, '--config', str(cfg), 'agent-next'],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['status'] == 'stopped'
    stop.unlink()
    p = subprocess.run([*entry, '--config', str(cfg), 'agent-next'],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['status'] == 'no_work' and str(cfg) in out['next_argv']
    p = subprocess.run([*entry, '--config', str(cfg), 'agent-next', '--scope', 'research'],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 1 and 'REFUSED: research checkpoint scope requires' in p.stderr
    assert 'Traceback' not in p.stderr


def test_no_work_honors_recorded_cooldown_without_search(tmp_path, monkeypatch):
    import time
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    cfg.path('ratelimit_until').parent.mkdir(parents=True)
    cfg.path('ratelimit_until').write_text(str(time.time() + 120))
    out = agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'c.yaml')
    assert out['status'] == 'wait_for_cooldown'
    assert out['retry_after_seconds'] > 0
    assert 'next_argv' not in out


def test_incomplete_discovery_is_not_clean_no_find(tmp_path):
    import json
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    cfg.path('run.json').parent.mkdir(parents=True)
    cfg.path('run.json').write_text(json.dumps({'status': 'discovery_budget_exhausted'}))
    out = agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'c.yaml')
    assert out['status'] == 'review_incomplete_discovery'
    assert out['next_argv'][-1] == '--prepare-only'
    assert 'partial' in out['reason']


def test_unreadable_cooldown_does_not_invite_new_discovery(tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    cfg.path('ratelimit_until').parent.mkdir(parents=True)
    cfg.path('ratelimit_until').write_text('corrupt')
    out = agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'c.yaml')
    assert out['status'] == 'inspect_local'
    assert 'next_argv' not in out


def test_existing_candidate_remains_actionable_during_cooldown(tmp_path, monkeypatch):
    import time
    cfg, q, item = fixture(tmp_path, monkeypatch)
    cfg.path('ratelimit_until').parent.mkdir(parents=True)
    cfg.path('ratelimit_until').write_text(str(time.time() + 100))
    item.status = 'clean'
    monkeypatch.setattr(evidence, 'capture', lambda *a: {'branch': 'main', 'tree': 'tree'})
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'work_candidate'


@pytest.mark.parametrize('terminal', [True, False])
def test_explicit_research_scope_preserves_preparation(tmp_path, monkeypatch, terminal):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'), repo_first=True)
    queue = Queue(cfg.path('queue.json'))
    status = 'awaiting_owner_review' if terminal else 'local_investigation'
    prep = {'id': 'local-preparation:org/one', 'repo': 'org/one', 'status': status}
    monkeypatch.setattr(agent_next.preparation, '_load', lambda *a: dict(prep))
    monkeypatch.setattr(agent_next.preparation, 'task', lambda *a, **kw: dict(prep))
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {'lead': {'status': 'investigating'}})
    monkeypatch.setattr(agent_next.repo_research, 'checkpoint', lambda *a: {'status': 'repo_investigation'})
    normal = agent_next.next_step(cfg, queue, 'cfg')
    assert normal['status'] == ('awaiting_owner_review' if terminal else 'local_preparation')
    chosen = agent_next.next_step(cfg, queue, 'cfg', 'research')
    if terminal:
        assert normal['research_checkpoint_argv'][-2:] == ['--scope', 'research']
        assert chosen['status'] == 'repo_investigation'
        assert chosen['preserved_preparation']['publication_hold'] is True
    else:
        assert chosen['status'] == 'local_preparation'
    assert not cfg.path('queue.json').exists()
    assert prep['status'] == status


def test_explicit_research_scope_cannot_hide_bad_preparation(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'), repo_first=True)
    monkeypatch.setattr(agent_next.preparation, '_load', lambda *a: {'status': 'awaiting_owner_review'})
    monkeypatch.setattr(agent_next.preparation, 'task', lambda *a, **kw: (_ for _ in ()).throw(ValueError('stale receipt')))
    assert agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'cfg', 'research')['status'] == 'inspect_local'


@pytest.mark.parametrize('kind', ['stop', 'partial', 'verified'])
def test_explicit_research_scope_preserves_priority(tmp_path, monkeypatch, kind):
    cfg, queue, item = fixture(tmp_path, monkeypatch)
    cfg.repo_first = True
    monkeypatch.setattr(agent_next.preparation, '_load', lambda *a: pytest.fail('earlier gate must win'))
    if kind == 'stop':
        Path(cfg.kill_file).touch()
    elif kind == 'partial':
        item.submitted = {'issue_url': 'https://github.com/owner/repo/issues/1'}
    assert agent_next.next_step(cfg, queue, 'cfg', 'research')['status'] == {
        'stop': 'stopped', 'partial': 'reconcile_remote', 'verified': 'verified_ready'}[kind]


def test_research_scope_does_not_renew_expired_session(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'), repo_first=True)
    prep = {'id': 'local-preparation:org/one', 'repo': 'org/one', 'status': 'awaiting_owner_review'}
    monkeypatch.setattr(agent_next.preparation, '_load', lambda *a: prep)
    monkeypatch.setattr(agent_next.preparation, 'task', lambda *a, **kw: prep)
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {'lead': None})
    monkeypatch.setattr(agent_next.research_session, 'status', lambda *a: {'status': 'search_budget_exhausted'})
    out = agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'cfg', 'research')
    assert out['status'] == 'search_budget_exhausted'
    assert out['preserved_preparation']['publication_hold']
    assert not cfg.path('research_session.json').exists()


def test_scoped_multistep_continuation_keeps_preserved_hold(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'), repo_first=True)
    queue = Queue(cfg.path('queue.json'))
    prep = {'id': 'local-preparation:org/one', 'repo': 'org/one', 'status': 'awaiting_owner_review'}
    monkeypatch.setattr(agent_next.preparation, '_load', lambda *a: prep)
    monkeypatch.setattr(agent_next.preparation, 'task', lambda *a, **kw: prep)
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {'lead': {'status': 'investigating'}})
    monkeypatch.setattr(agent_next.repo_research, 'checkpoint', lambda *a: {'status': 'repo_investigation'})
    scope = 'research'
    for _ in range(3):
        out = agent_next.next_step(cfg, queue, 'cfg', scope)
        assert out['status'] == 'repo_investigation'
        assert out['continue_argv'] == ['ossforge', '--config', 'cfg', 'agent-next', '--scope', 'research']
        scope = out['continue_argv'][-1]
        assert out['preserved_preparation']['publication_hold']
    assert agent_next.next_step(cfg, queue, 'cfg')['status'] == 'awaiting_owner_review'


def test_research_scope_requires_repo_first(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    with pytest.raises(ValueError, match='requires repo_first'):
        agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'cfg', 'research')


@pytest.mark.parametrize('kind', ['review', 'held'])
def test_earlier_review_and_held_checkpoints_retain_explicit_scope(tmp_path, monkeypatch, kind):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'), repo_first=True)
    queue = Queue(cfg.path('queue.json'))
    if kind == 'review':
        monkeypatch.setattr(agent_next.qualification, 'pending', lambda *a: [{'repo': 'org/a', 'number': 2}])
    else:
        queue.add(Item('held', 'org/a', 'x.py', 1, 'bug', 'hypothesis', status='held'))
    out = agent_next.next_step(cfg, queue, 'cfg', 'research')
    assert out['status'] == ('review_needed' if kind == 'review' else 'held_work')
    assert out['continue_argv'][-2:] == ['--scope', 'research']


def test_completed_selected_investigation_stops_without_reproposing(tmp_path, monkeypatch):
    cfg, queue, item = fixture(tmp_path, monkeypatch)
    cfg.repo_first = True
    item.status = 'submitted'
    item.research_id = 'investigation-one'
    item.submitted = {'issue': 'https://github.com/owner/repo/issues/1',
                      'pr': 'https://github.com/owner/repo/pull/2'}
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {
        'lead': {'repo': item.repo, 'clone': item.clone, 'status': 'investigating', 'research_id': 'investigation-one'}})
    monkeypatch.setattr(agent_next.repo_research, 'checkpoint', lambda *a: pytest.fail('no reproposal route'))
    monkeypatch.setattr(agent_next.research_session, 'status', lambda *a: pytest.fail('no renewed search'))
    monkeypatch.setattr(agent_next.submit, '_base', lambda *a: pytest.fail('no remote probe'))
    out = agent_next.next_step(cfg, queue, 'cfg')
    assert out['status'] == 'contribution_submitted' and out['stop_session']
    assert out['completed'][0]['saved_pr'] == item.submitted['pr']
    assert 'next_argv' not in out
    assert not queue.path.exists()


def test_different_investigation_not_stopped_by_prior_submission(tmp_path, monkeypatch):
    cfg, queue, item = fixture(tmp_path, monkeypatch)
    cfg.repo_first = True
    item.status = 'submitted'
    item.submitted = {'pr': 'https://github.com/owner/repo/pull/2'}
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {
        'lead': {'repo': 'other/repo', 'clone': item.clone, 'status': 'investigating'}})
    monkeypatch.setattr(agent_next.repo_research, 'checkpoint', lambda *a: {'status': 'repo_investigation'})
    assert agent_next.next_step(cfg, queue, 'cfg')['status'] == 'repo_investigation'


@pytest.mark.parametrize('change', [
    {'research_id': 'different'}, {'research_id': ''}, {'clone': ''},
    {'status': 'repo_review_needed'}])
def test_same_repo_clone_different_or_unbound_lead_not_completed(tmp_path, monkeypatch, change):
    cfg, queue, item = fixture(tmp_path, monkeypatch)
    cfg.repo_first = True
    item.status = 'submitted'
    item.research_id = 'one'
    item.submitted = {'pr': 'https://github.com/owner/repo/pull/2'}
    lead = {'repo': item.repo, 'clone': item.clone, 'status': 'investigating', 'research_id': 'one'} | change
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {'lead': lead})
    monkeypatch.setattr(agent_next.repo_research, 'checkpoint', lambda *a: {'status': 'repo_investigation'})
    monkeypatch.setattr(agent_next.research_session, 'status', lambda *a: {'status': 'search_session_active'})
    assert agent_next.next_step(cfg, queue, 'cfg')['status'] == 'repo_investigation'


@pytest.mark.parametrize('other', ['review', 'held', 'partial', 'stop'])
def test_submission_completion_priority_preserves_recovery(tmp_path, monkeypatch, other):
    cfg, queue, item = fixture(tmp_path, monkeypatch)
    cfg.repo_first = True
    item.status = 'submitted'
    item.research_id = 'one'
    item.submitted = {'pr': 'https://github.com/owner/repo/pull/2'}
    monkeypatch.setattr(agent_next.repo_research, 'load', lambda *a: {'lead': {
        'repo': item.repo, 'clone': item.clone, 'status': 'investigating', 'research_id': 'one'}})
    if other == 'review':
        monkeypatch.setattr(agent_next.qualification, 'pending', lambda *a: pytest.fail('completed session wins'))
    elif other == 'held':
        queue.add(Item('held', 'other/repo', 'x.py', 1, 'bug', 'candidate', status='held'))
    elif other == 'partial':
        queue.add(Item('partial', 'other/repo', 'x.py', 1, 'bug', 'candidate', status='verified', submitted={'issue': 'saved'}))
    else:
        Path(cfg.kill_file).touch()
    assert agent_next.next_step(cfg, queue, 'cfg')['status'] == {
        'review': 'contribution_submitted', 'held': 'contribution_submitted',
        'partial': 'reconcile_remote', 'stop': 'stopped'}[other]
