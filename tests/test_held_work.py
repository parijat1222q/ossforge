from ossforge import agent_next, cli, driver
from ossforge.config import Config
from ossforge.queue import Item, Queue


def test_hold_preserves_task_and_checkpoint_allows_independent_discovery(tmp_path):
    cfgfile = tmp_path/'config.yaml'
    cfgfile.write_text(f'state_dir: {tmp_path}/state\n')
    cfg = Config(state_dir=str(tmp_path/'state'))
    q = Queue(cfg.path('queue.json'))
    q.add(Item('a', 'org/repo', '', 1, 'issue', 'unverified lead', status='clean', clone='/preserve', notes=['source URL']))
    q.save()
    assert cli.main(['--config', str(cfgfile), 'hold', 'a', '--reason', 'source unavailable; not a false bug']) == 0
    q = Queue(q.path)
    assert q.items['a'].status == 'held'
    assert q.items['a'].notes == ['source URL']
    assert q.items['a'].hold_reason == 'source unavailable; not a false bug'
    assert driver.task(q)['status'] == 'no_work'
    out = agent_next.next_step(cfg, q, str(cfgfile))
    assert out['status'] == 'held_work'
    assert out['held'][0]['id'] == 'a'
    assert out['next_argv'][-3:] == ['loop', '--once', '--prepare-only']
    assert cli.main(['--config', str(cfgfile), 'unhold', 'a']) == 0
    assert Queue(q.path).items['a'].status == 'clean'


def test_held_status_roundtrip_and_other_work_selected(tmp_path, monkeypatch):
    q = Queue(tmp_path/'q.json')
    q.add(Item('held', 'org/repo', '', 1, 'issue', 'lead', status='held', clone='/held', hold_reason='unavailable'))
    q.add(Item('ready', 'org/other', '', 2, 'issue', 'other lead', status='clean', clone='/ready'))
    q.save()
    q = Queue(q.path)
    monkeypatch.setattr(driver.submit, '_base', lambda *a: 'base')
    monkeypatch.setattr(driver.evidence, 'capture', lambda *a: {})
    assert driver.task(q)['id'] == 'ready'


def test_hold_cannot_hide_verified_or_partial_work(tmp_path):
    cfg = tmp_path/'cfg.yaml'
    cfg.write_text(f'state_dir: {tmp_path}/state\n')
    q = Queue(tmp_path/'state/queue.json')
    q.add(Item('verified', 'org/repo', '', 1, 'issue', 'lead', status='verified'))
    q.add(Item('partial', 'org/repo', '', 2, 'issue', 'lead', status='clean', submitted={'issue_url': 'saved'}))
    q.save()
    for item in ('verified', 'partial'):
        assert cli.main(['--config', str(cfg), 'hold', item, '--reason', 'pause']) == 1
    assert Queue(q.path).items['verified'].status == 'verified'
    assert Queue(q.path).items['partial'].submitted == {'issue_url': 'saved'}
