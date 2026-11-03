"""Repository metadata is a ranking hint, never a code or permission gate."""
from ossforge import discovery, gh
from ossforge.config import Config


def page():
    # Public search descriptions/topics observed in the first Python/stars page.
    # Their names deliberately differ: ranking must not blacklist repositories.
    return [
        {'full_name': 'org/api-catalog', 'description': 'A collective list of free APIs', 'topics': ['list', 'resources']},
        {'full_name': 'org/book-catalog', 'description': 'Freely available programming books', 'topics': ['books', 'education', 'list']},
        {'full_name': 'org/design-lessons', 'description': 'Learn how to design large-scale systems. Prep for the system design interview. Includes Anki flashcards.'},
        {'full_name': 'org/tool-catalog', 'description': 'The definitive list that answers which tool should I use?', 'topics': ['awesome-list']},
        {'full_name': 'org/learning-projects', 'description': 'Curated list of project-based tutorials', 'topics': ['tutorial']},
        {'full_name': 'org/runtime', 'description': 'A Python task runtime', 'topics': ['python']},
    ]


def test_code_session_does_not_spend_first_window_on_five_collections(monkeypatch):
    monkeypatch.setattr(gh, 'search_repositories', lambda *a, **kw: page())
    pool = discovery.discover_broad_repos(Config(repo_first=True), 'python')
    assert pool[0] == 'org/runtime'
    assert len(pool) == 5


def test_ranking_does_not_drop_collections_or_change_issue_only_route(monkeypatch):
    monkeypatch.setattr(gh, 'search_repositories', lambda *a, **kw: page())
    ordinary = discovery.discover_broad_repos(Config(repo_first=False), 'python')
    assert ordinary == [r['full_name'] for r in page()[:5]]
    all_names = set()
    for cursor in range(6):
        all_names.update(discovery.discover_broad_repos(Config(repo_first=True), 'python', cursor))
    assert all_names == {r['full_name'] for r in page()}


def test_unknown_or_real_code_metadata_is_not_rejected_for_education_words(monkeypatch):
    repos = [
        {'full_name': 'org/unknown'},
        {'full_name': 'org/education-engine', 'description': 'An education platform with interactive tutorials', 'topics': ['education', 'tutorial']},
        {'full_name': 'org/collection-engine', 'description': 'A library for efficient collections and lists'},
        {'full_name': 'org/list', 'description': 'A curated list of learning resources'},
    ]
    monkeypatch.setattr(gh, 'search_repositories', lambda *a, **kw: repos)
    assert discovery.discover_broad_repos(Config(repo_first=True)) == [r['full_name'] for r in repos]


def test_code_ranking_keeps_page_window_coverage(monkeypatch):
    calls = []
    def search(*args, **kwargs):
        calls.append(kwargs)
        return [dict(r, full_name=f'org/p{kwargs["page"]}r{i}')
                for i, r in enumerate((page() * 5))]
    monkeypatch.setattr(gh, 'search_repositories', search)
    cfg = Config(repo_first=True)
    first_page = [name for cursor in range(6)
                  for name in discovery.discover_broad_repos(cfg, cursor=cursor)]
    assert len(first_page) == len(set(first_page)) == 30
    assert discovery.discover_broad_repos(cfg, cursor=6)[0].startswith('org/p2r')
    assert calls[-1]['page'] == 2
