import pytest
from ossforge import gh, vet


@pytest.mark.parametrize('source', [
    {'state': 'closed'},
    {'state': 'open', 'pull_request': {'url': 'https://api.github.com/repos/org/repo/pulls/7'}},
])
def test_closed_issue_or_pull_request_cannot_be_a_coding_issue(monkeypatch, source):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    monkeypatch.setattr(gh, 'api', lambda path, **kw: [] if path.endswith('/comments') else {
        'body': 'Steps to reproduce expected record retained but actual deletion occurs on invalid input.',
        'assignees': [], 'comments': 0, **source})
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **kw: [])
    monkeypatch.setattr(vet, 'policy_texts', lambda r: {})
    assert vet.vet_issue('org/repo', 7).decision == 'skip'


def test_issue_and_comment_checks_refresh_cached_open_unclaimed_state(monkeypatch):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    calls = []
    def api(path, **kw):
        calls.append(kw)
        if path.endswith('/comments'):
            return [{'body': 'working on this'}] if kw.get('fresh') else []
        return {'state': 'open', 'body': 'Steps to reproduce expected record retained but actual deletion occurs on invalid replacement input.',
                'assignees': [], 'comments': 1 if kw.get('fresh') else 0}
    monkeypatch.setattr(gh, 'api', api)
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **kw: [])
    monkeypatch.setattr(vet, 'policy_texts', lambda r: {})
    result = vet.vet_issue('org/repo', 7)
    assert result.decision == 'skip' and 'claimed in comments' in result.reasons
    assert all(c.get('fresh') is True for c in calls)


def test_open_issue_with_complete_unclaimed_discussion_is_eligible(monkeypatch):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    monkeypatch.setattr(gh, 'api', lambda path, **kw: [] if path.endswith('/comments') else {
        'state': 'open', 'body': 'Steps to reproduce expected record retained but actual deletion occurs on invalid replacement input.',
        'assignees': [], 'comments': 0})
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **kw: [])
    monkeypatch.setattr(vet, 'policy_texts', lambda r: {})
    assert vet.vet_issue('org/repo', 7).decision == 'approve'
