import yaml

from ossforge import agent_next, cli, evidence, loop, scout, vet
from ossforge.config import Config
from ossforge.queue import Queue


def test_checkpoint_suggested_public_command_reaches_task_without_hooks(tmp_path, monkeypatch):
    cfgpath = tmp_path / 'custom config.yaml'
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'), work_kind='fix')
    cfgpath.write_text(yaml.safe_dump({'state_dir': cfg.state_dir, 'kill_file': cfg.kill_file, 'work_kind': 'fix',
                                     'auto_submit': True, 'patch_command': 'must-not-run'}))
    q = Queue(cfg.path('queue.json'))
    first = agent_next.next_step(cfg, q, str(cfgpath))
    assert first['status'] == 'no_work'
    clone = tmp_path / 'clone'
    clone.mkdir()
    candidate = scout.Candidate('org/lib', 42, 'Controlled lead', 'https://example.invalid/42',
                                80, vet.Verdict('org/lib#42', 'approve'))
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: [candidate])
    monkeypatch.setattr(loop.hunt, 'clone', lambda *a: clone)
    monkeypatch.setattr(loop.hunt, 'policy_of', lambda *a: loop.hunt.policy.PolicyResult(False, '', []))
    monkeypatch.setattr(loop.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('patch hook ran')))
    monkeypatch.setattr(loop.submit, 'submit', lambda *a, **k: (_ for _ in ()).throw(AssertionError('publish ran')))
    assert cli.main(first['next_argv'][1:]) == 0
    saved = Queue(cfg.path('queue.json'))
    monkeypatch.setattr(agent_next.submit, '_base', lambda *a: 'base')
    monkeypatch.setattr(evidence, 'capture', lambda *a: {})
    second = agent_next.next_step(cfg, saved, str(cfgpath))
    assert second['status'] == 'work_candidate'
    assert second['task']['source_issue'] == 42
    assert second['task']['clone'] == str(clone)
    assert saved.items['org/lib#42:issue'].status == 'clean'
    assert not saved.items['org/lib#42:issue'].verification


def test_prepare_only_always_bounded_and_overrides_hooks(tmp_path, monkeypatch):
    path = tmp_path / 'c.yaml'
    path.write_text('auto_submit: true\npatch_command: should-not-run\n')
    calls = []
    monkeypatch.setattr(loop, 'run_once', lambda cfg, **kw: calls.append(cfg))
    monkeypatch.setattr(loop, 'run_forever', lambda *a: (_ for _ in ()).throw(AssertionError('background loop')))
    assert cli.main(['--config', str(path), 'loop', '--prepare-only']) == 0
    assert len(calls) == 1 and not calls[0].auto_submit and not calls[0].patch_command


def test_held_and_partial_checkpoints_offer_bounded_preparation(tmp_path):
    import json
    from ossforge.queue import Item
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    q = Queue(cfg.path('queue.json'))
    q.add(Item('held', 'org/lib', '', 42, 'issue', 'unverified', status='held', hold_reason='needs reproduction'))
    out = agent_next.next_step(cfg, q, 'c.yaml')
    assert out['status'] == 'held_work' and out['next_argv'][-3:] == ['loop', '--once', '--prepare-only']
    cfg.path('run.json').parent.mkdir(parents=True, exist_ok=True)
    cfg.path('run.json').write_text(json.dumps({'status': 'discovery_budget_exhausted'}))
    out = agent_next.next_step(cfg, q, 'c.yaml')
    assert out['status'] == 'review_incomplete_discovery'
    assert out['next_argv'][-3:] == ['loop', '--once', '--prepare-only']
    assert 'prerequisite' in out['warning']
    assert q.items['held'].status == 'held'
