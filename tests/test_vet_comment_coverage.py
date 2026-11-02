from ossforge import gh, vet


def setup_vet(monkeypatch, count, rows):
    monkeypatch.setattr(vet.repo_access, 'pr_restriction', lambda r: '')
    monkeypatch.setattr(gh, 'api', lambda path, **kw: rows if path.endswith('/comments') else {
        'body': 'Steps to reproduce: expected record retained; actual deletion occurs on invalid vector.',
        'assignees': [], 'comments': count})
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **kw: [])
    monkeypatch.setattr(vet, 'policy_texts', lambda r: {})


def test_unread_comments_cannot_approve_unclaimed_work(monkeypatch):
    # Issue says 31 comments, but the default API page contains only 30.
    # A claim or maintainer resolution in the unseen page must not be ignored.
    setup_vet(monkeypatch, 31, [{'body': 'additional reproduction details'}] * 30)
    verdict = vet.vet_issue('org/repo', 7)
    assert verdict.decision == 'skip'
    assert 'comment' in ' '.join(verdict.reasons)


def test_comment_page_at_capacity_requires_review(monkeypatch):
    setup_vet(monkeypatch, 100, [{'body': 'details'}] * 100)
    assert vet.vet_issue('org/repo', 7).decision == 'skip'


def test_complete_small_comment_list_can_still_approve(monkeypatch):
    setup_vet(monkeypatch, 1, [{'body': 'additional reproduction details'}])
    assert vet.vet_issue('org/repo', 7).decision == 'approve'


def test_full_page_request_finds_claim_after_default_page(monkeypatch):
    setup_vet(monkeypatch, 31, [])
    def api(path, **kw):
        if path.endswith('/comments'):
            rows = [{'body': 'details'}] * 30 + [{'body': 'working on this'}]
            return rows[:kw.get('per_page', 30)]
        return {'body': 'Steps to reproduce: expected record retained; actual deletion on invalid vector.',
                'assignees': [], 'comments': 31}
    monkeypatch.setattr(gh, 'api', api)
    verdict = vet.vet_issue('org/repo', 7)
    assert verdict.decision == 'skip'
    assert 'claimed in comments' in verdict.reasons
