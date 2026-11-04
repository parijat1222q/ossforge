import pytest

from ossforge import discovery, gh, scout
from ossforge.config import Config
from ossforge.outcomes import Stats


@pytest.fixture(autouse=True)
def isolated_funnel(monkeypatch):
    # The mocked scout mutates this shared object. Restore it after each test,
    # the real scout clears and updates its funnel on every call.
    monkeypatch.setattr(scout, 'LAST_FUNNEL', {})


@pytest.mark.parametrize("preferred,history,stride", [(False, False, 1), (False, True, 2), (True, False, 2), (True, True, 3)])
def test_rotating_broad_turns_cover_all_first_page_windows(monkeypatch, tmp_path, preferred, history, stride):
    cfg = Config(state_dir=str(tmp_path), preferred_repos=["preferred/lib"] if preferred else [])
    stats = Stats(tmp_path/'stats.json')
    if history:
        stats.set_history('history/lib', 2, 0)
    calls = []
    monkeypatch.setattr(gh, 'search_repositories', lambda *a, **k: [{'full_name': f'org/r{i}'} for i in range(30)])
    def empty(*a, **kw):
        calls.append(kw['query'])
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL['stopped'] = ''
        return []
    monkeypatch.setattr(scout, 'scout', empty)
    for _ in range(6 * stride):
        discovery.discover(cfg, stats)
    broad = [q for q in calls if 'repo:org/' in q]
    assert len(broad) == 6
    assert {part for q in broad for part in q.split() if part.startswith('repo:org/')} == {f'repo:org/r{i}' for i in range(30)}


def test_broad_search_advances_pages_after_six_windows(monkeypatch, tmp_path):
    calls = []
    def repositories(*a, **kw):
        calls.append(kw)
        page = kw.get('page', 1)
        return [{'full_name': f'org/p{page}r{i}'} for i in range(30)]
    monkeypatch.setattr(gh, 'search_repositories', repositories)
    cfg = Config(state_dir=str(tmp_path))
    assert discovery.discover_broad_repos(cfg, cursor=5)[0] == 'org/p1r25'
    assert discovery.discover_broad_repos(cfg, cursor=6)[0] == 'org/p2r0'
    assert discovery.discover_broad_repos(cfg, cursor=197)[0].startswith('org/p33r')
    assert discovery.discover_broad_repos(cfg, cursor=198)[0] == 'org/p1r0'
    assert calls[2]['per_page'] == 30  #33 full pages stay within the first1000 results


def test_repository_page_is_forwarded_to_api(monkeypatch):
    calls = []
    monkeypatch.setattr(gh, 'api', lambda path, **kw: calls.append(kw) or {'items': []})
    gh.search_repositories('stars:>=500', page=2)
    assert calls[0]['page'] == 2


def test_stopped_broad_discovery_retries_same_page_window(monkeypatch, tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    stats = Stats(tmp_path/'stats.json')
    discovery.issue_reviews._save(cfg.path('discovery.json'), {'cursor': 6, 'recent': {}})
    calls = []
    def repositories(*a, **kw):
        calls.append(kw['page'])
        return [{'full_name': f'org/r{i}'} for i in range(30)]
    def stopped(*a, **kw):
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL['stopped'] = 'read budget exhausted'
        return []
    monkeypatch.setattr(gh, 'search_repositories', repositories)
    monkeypatch.setattr(scout, 'scout', stopped)
    discovery.discover(cfg, stats)
    discovery.discover(cfg, stats)
    assert calls == [2, 2]
    assert discovery.issue_reviews.load(cfg.path('discovery.json'))['cursor'] == 6


def test_empty_later_page_falls_back_to_qualifying_first_page(monkeypatch, tmp_path):
    calls = []
    def repositories(*a, **kw):
        calls.append(kw['page'])
        return [{'full_name': 'org/small'}] if kw['page'] == 1 else []
    monkeypatch.setattr(gh, 'search_repositories', repositories)
    assert discovery.discover_broad_repos(Config(state_dir=str(tmp_path)), cursor=6) == ['org/small']
    assert calls == [2, 1]
