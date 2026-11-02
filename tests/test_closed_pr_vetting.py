from ossforge import gh, vet


def test_closed_referenced_pr_requires_review_before_approval(monkeypatch):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    monkeypatch.setattr(gh, 'api', lambda path, **kw: [] if path.endswith('/comments') else {
        'state': 'open', 'body': 'Steps to reproduce: invalid replacement vector deletes existing data. Expected record retained; actual lost.', 'assignees': []})
    monkeypatch.setattr(vet, 'policy_texts', lambda r: {'CONTRIBUTING.md': 'Contributions welcome'})
    seen = []
    def search(query, **kw):
        seen.append((query, kw))
        return [] if 'is:open' in query else [{'number': 7793, 'state': 'closed', 'pull_request': {}}]
    monkeypatch.setattr(gh, 'search_issues', search)
    verdict = vet.vet_issue('mem0ai/mem0', 7792)
    assert verdict.decision == 'skip'
    assert 'review' in ' '.join(verdict.reasons)
    assert 'is:open' not in seen[0][0]
    assert seen[0][1]['require_complete'] is True


def test_incomplete_reference_search_is_not_clear(monkeypatch):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    monkeypatch.setattr(gh, 'api', lambda path, **kw: [] if path.endswith('/comments') else {'state': 'open', 'body': 'x'*100})
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **kw: (_ for _ in ()).throw(gh.GhError('search coverage incomplete')))
    out = vet.vet_issue('org/repo', 1)
    assert out.decision == 'skip' and 'coverage unavailable' in out.reasons[0]


def test_no_reference_still_eligible_and_open_reference_blocked(monkeypatch):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    monkeypatch.setattr(gh, 'api', lambda path, **kw: [] if path.endswith('/comments') else {
        'state': 'open', 'body': 'Steps to reproduce: invalid vector should retain the record but actual deletion occurs.', 'assignees': []})
    monkeypatch.setattr(vet, 'policy_texts', lambda r: {'CONTRIBUTING.md': 'Contributions welcome'})
    for refs, expected in [([], 'approve'), ([{'state': 'open'}], 'skip')]:
        monkeypatch.setattr(gh, 'search_issues', lambda *a, refs=refs, **kw: refs)
        assert vet.vet_issue('org/repo', 1).decision == expected
