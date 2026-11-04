import pytest

from ossforge import loop, scout, vet
from ossforge.config import Config
from ossforge.queue import Queue


@pytest.fixture(autouse=True)
def isolated_mock_scout_funnel(monkeypatch):
    # These tests replace scout itself, so provide the fresh funnel state that a real
    # scout call would clear and update rather than inheriting another test's stop state.
    monkeypatch.setattr(scout, 'LAST_FUNNEL', {})


def test_loop_keeps_approved_issue_for_external_driver_when_static_scan_empty(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path/'state'), work_dir=str(tmp_path/'work'), kill_file=str(tmp_path/'STOP'))
    candidate = scout.Candidate('owner/repo', 42, 'Incorrect parser result', 'https://github.com/owner/repo/issues/42',
                                80, vet.Verdict(42, 'approve', ['policy clear']), updated_at='2026-11-01')
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: [candidate])
    monkeypatch.setattr(loop.scout, 'scout_features', lambda *a, **k: [])
    monkeypatch.setattr(loop.hunt, 'hunt', lambda *a: {'anti_ai': None, 'clean': 0, 'duplicate': 0})
    clone = tmp_path/'work'/'owner__repo'
    clone.mkdir(parents=True)
    monkeypatch.setattr(loop.hunt, 'clone', lambda *a: clone)
    loop.run_once(cfg)
    items = Queue(cfg.path('queue.json')).by_status('clean')
    assert len(items) == 1
    assert items[0].line == 42 and items[0].clone == str(clone)
    assert items[0].notes == [candidate.url]
    assert items[0].verification == {}  # approval is not bug proof


def test_local_policy_denial_does_not_enqueue_search_issue(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path), kill_file=str(tmp_path/'STOP'))
    candidate = scout.Candidate('owner/repo', 42, 'Candidate', 'https://github.com/owner/repo/issues/42',
                                80, vet.Verdict(42, 'approve', []))
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: [candidate])
    monkeypatch.setattr(loop.scout, 'scout_features', lambda *a, **k: [])
    monkeypatch.setattr(loop.hunt, 'hunt', lambda *a: {'anti_ai': 'CONTRIBUTING.md', 'clean': 0, 'duplicate': 0})
    monkeypatch.setattr(loop.hunt, 'clone', lambda *a: tmp_path)
    monkeypatch.setattr(loop.hunt, 'policy_of', lambda *a: loop.hunt.policy.PolicyResult(True, 'CONTRIBUTING.md', []))
    loop.run_once(cfg)
    assert Queue(cfg.path('queue.json')).by_status('clean') == []


def test_approved_feature_is_cloned_and_reuses_existing_issue(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path/'state'), work_dir=str(tmp_path/'work'), kill_file=str(tmp_path/'STOP'))
    candidate = scout.Candidate('owner/repo', 84, 'Useful feature', 'https://github.com/owner/repo/issues/84',
                                80, vet.Verdict(84, 'approve', []), kind='feature')
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: [])
    monkeypatch.setattr(loop.scout, 'scout_features', lambda *a, **k: [candidate])
    monkeypatch.setattr(loop.time, 'time', lambda: 86400)
    clone = tmp_path/'work'/'owner__repo'
    clone.mkdir(parents=True)
    monkeypatch.setattr(loop.hunt, 'clone', lambda *a: clone)
    monkeypatch.setattr(loop.hunt, 'policy_of', lambda *a: loop.hunt.policy.PolicyResult(False, '', []))
    loop.run_once(cfg)
    item = Queue(cfg.path('queue.json')).items['owner/repo#84:feature']
    assert item.clone == str(clone) and item.source_issue == 84


def test_feature_local_policy_denial_keeps_it_out_of_driver_queue(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path), kill_file=str(tmp_path/'STOP'))
    candidate = scout.Candidate('owner/repo', 84, 'Feature', 'https://github.com/owner/repo/issues/84',
                                80, vet.Verdict(84, 'approve', []), kind='feature')
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: [])
    monkeypatch.setattr(loop.scout, 'scout_features', lambda *a, **k: [candidate])
    monkeypatch.setattr(loop.time, 'time', lambda: 86400)
    monkeypatch.setattr(loop.hunt, 'clone', lambda *a: tmp_path)
    monkeypatch.setattr(loop.hunt, 'policy_of', lambda *a: loop.hunt.policy.PolicyResult(True, 'CONTRIBUTING.md', []))
    loop.run_once(cfg)
    assert Queue(cfg.path('queue.json')).by_status('clean') == []


def test_issue_handoff_does_not_require_whole_repo_static_hunt(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path/'state'), kill_file=str(tmp_path/'STOP'))
    candidate = scout.Candidate('org/lib', 7, 'real lead', 'https://github.com/org/lib/issues/7', 80,
                                vet.Verdict('org/lib#7', 'approve'))
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: [candidate])
    monkeypatch.setattr(loop.scout, 'scout_features', lambda *a, **k: [])
    monkeypatch.setattr(loop.hunt, 'hunt', lambda *a: (_ for _ in ()).throw(AssertionError('issue work must not wait for static hunt')))
    monkeypatch.setattr(loop.hunt, 'clone', lambda *a: tmp_path)
    monkeypatch.setattr(loop.hunt, 'policy_of', lambda *a: loop.hunt.policy.PolicyResult(False, '', []))
    loop.run_once(cfg)
    assert Queue(cfg.path('queue.json')).items['org/lib#7:issue'].source_issue == 7


def test_issue_clone_failure_retains_other_issue_work(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path/'state'), kill_file=str(tmp_path/'STOP'))
    candidates = [scout.Candidate(f'org/{name}', 7, 'real lead', f'https://github.com/org/{name}/issues/7', 80,
                                 vet.Verdict(f'org/{name}#7', 'approve')) for name in ('broken', 'good')]
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.scout, 'scout', lambda *a, **k: candidates)
    monkeypatch.setattr(loop.scout, 'scout_features', lambda *a, **k: [])
    def clone(cfg, repo):
        if repo.endswith('broken'):
            raise RuntimeError('existing checkout preserved, inspect it')
        return tmp_path
    monkeypatch.setattr(loop.hunt, 'clone', clone)
    monkeypatch.setattr(loop.hunt, 'policy_of', lambda *a: loop.hunt.policy.PolicyResult(False, '', []))
    notes = loop.run_once(cfg)
    assert 'org/good#7:issue' in Queue(cfg.path('queue.json')).items
    assert any('inspect' in note for note in notes)
