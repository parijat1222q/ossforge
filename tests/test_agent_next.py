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
    monkeypatch.setattr(evidence, 'check', lambda *a: None)
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
    monkeypatch.setattr(evidence, 'check', lambda *a: (_ for _ in ()).throw(evidence.StaleEvidence('tree changed')))
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'inspect_local'
    assert item.status == 'verified'


def test_completed_url_not_republished_and_no_work_is_not_global_absence(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    item.status = 'submitted'
    item.submitted = {'pr': 'https://github.com/owner/repo/pull/2'}
    out = agent_next.next_step(cfg, q, 'c')
    assert out['status'] == 'no_work' and out['completed'][0]['saved_pr'] == item.submitted['pr']
    assert out['next_argv'][-3:] == ['discover', '--strategy', 'rotate']


def test_clean_and_dirty_candidate_paths(tmp_path, monkeypatch):
    cfg, q, item = fixture(tmp_path, monkeypatch)
    item.status = 'clean'
    monkeypatch.setattr(evidence, 'capture', lambda *a: {})
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
    p = subprocess.run([str(Path(sys.executable).with_name('ossforge')), '--config', str(cfg), 'agent-next'],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['status'] == 'stopped'
    stop.unlink()
    p = subprocess.run([str(Path(sys.executable).with_name('ossforge')), '--config', str(cfg), 'agent-next'],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['status'] == 'no_work' and str(cfg) in out['next_argv']


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
    assert out['next_argv'][-1] == 'rotate'
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
    monkeypatch.setattr(evidence, 'capture', lambda *a: {})
    assert agent_next.next_step(cfg, q, 'c')['status'] == 'work_candidate'
