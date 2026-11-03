"""Explain the existing pre-begin expiry boundary without changing its gates."""
import pytest
from ossforge import agent_next, repo_research, research_session
from ossforge.config import Config
from ossforge.queue import Queue


def test_selected_unstarted_expiry_is_preserved_and_explained(tmp_path, monkeypatch):
    cfg = Config(repo_first=True, state_dir=str(tmp_path), kill_file=str(tmp_path/'STOP'))
    monkeypatch.setattr(research_session.time, 'time', lambda: 1000.)
    research_session.start(cfg, 60)
    repo_research._save(cfg, {'schema': 1, 'cursor': 0, 'lead': {
        'repo': 'org/selected', 'status': 'repo_review_needed', 'revision': 'a' * 40}})
    state = cfg.path('repo_research.json').read_bytes()
    budget = cfg.path('research_session.json').read_bytes()
    monkeypatch.setattr(research_session.time, 'time', lambda: 1060.)
    monkeypatch.setattr(repo_research, 'collect_selected', lambda *a: pytest.fail('no fresh read permitted'))
    step = agent_next.next_step(cfg, Queue(cfg.path('queue.json')), 'cfg')
    assert step['status'] == 'search_budget_exhausted'
    assert step['selected_repository'] == 'org/selected'
    assert step['investigation_started'] is False
    assert 'initial begin' in step['warning']
    assert 'already-started' in step['warning']
    with pytest.raises(research_session.Invalid, match='search_budget_exhausted'):
        repo_research.begin(cfg)
    assert cfg.path('repo_research.json').read_bytes() == state
    assert cfg.path('research_session.json').read_bytes() == budget


def test_session_scope_states_initial_begin_boundary(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path))
    monkeypatch.setattr(research_session.time, 'time', lambda: 1000.)
    status = research_session.start(cfg, 60)
    assert 'initial begin' in status['scope']
    assert 'already-started' in status['scope']
