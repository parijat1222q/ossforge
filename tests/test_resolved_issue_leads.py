import pytest
from ossforge import gh, repo_access, vet


@pytest.mark.parametrize('body', [
    'The missing-platform case was fixed in #1504: preserve the tracking ID, if present.',
    'ZCode support was merged later in #504 (d62fe62, September 26). Current main registers zcode.',
    'This is already fixed by https://github.com/org/lib/pull/42 .',
])
def test_maintainer_resolved_work_is_not_a_new_coding_task(monkeypatch, body):
    monkeypatch.setattr(repo_access, 'pr_restriction', lambda *a: None)
    def api(path, **kw):
        if path.endswith('/comments'):
            return [{'body': body, 'author_association': 'MEMBER', 'user': {'login': 'maint'}}]
        return {'state': 'open', 'assignees': [], 'body': 'Steps to reproduce expected actual error ' * 4}
    monkeypatch.setattr(gh, 'api', api)
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **k: [])
    monkeypatch.setattr(vet, 'policy_texts', lambda *a: {})
    out = vet.vet_issue('org/lib', 7)
    assert out.decision == 'skip'
    assert any('resolved' in reason for reason in out.reasons)


@pytest.mark.parametrize('body,association', [
    ('This is already fixed by #42', 'NONE'),
    ('This is not fixed by #42', 'MEMBER'),
    ('This was not already fixed by #42', 'MEMBER'),
    ('This will be fixed by #42', 'MEMBER'),
    ('The fix in #42 is not merged yet', 'MEMBER'),
    ('The missing case was fixed in #42, but it still fails on current main.', 'MEMBER'),
])
def test_ambiguous_or_untrusted_resolution_does_not_hide_work(body, association):
    assert not vet.resolution_evidence([{'body': body, 'author_association': association}])
