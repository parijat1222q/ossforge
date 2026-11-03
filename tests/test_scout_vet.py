from ossforge import policy, radar, vet


def test_claim_detection():
    assert vet.comment_claims_issue("I'll take this one")
    assert not vet.comment_claims_issue("Thanks for the report")


def test_decide_anti_ai_wins():
    pol = policy.scan_text("human-authored only", "README.md")
    code, _ = vet.decide(assigned=False, claimed=False, open_pr_count=0, clear=True,
                         has_contributing=True, anti_ai=pol, repo_ok=True)
    assert code == "anti_ai"


def test_decide_approve_and_skip():
    ok = policy.PolicyResult(False)
    base = dict(assigned=False, claimed=False, clear=True, has_contributing=True, anti_ai=ok, repo_ok=True)
    assert vet.decide(open_pr_count=0, **base)[0] == "approve"
    assert vet.decide(open_pr_count=1, **base)[0] == "skip"


def test_radar_scoring():
    good = radar.RepoSignals(40, 10, 0.1, 3, 0.6, 5000, 2)
    dead = radar.RepoSignals(0, 120, 0.6, None, None, 100, 200)
    assert radar.score_repo(good)[0] >= 75
    assert radar.score_repo(dead)[0] < 45


def test_publication_policy_listing_error_is_unknown_not_missing(monkeypatch):
    import pytest
    from ossforge import gh, submit
    monkeypatch.setattr(gh, 'api', lambda *a, **k: (_ for _ in ()).throw(gh.GhError('service unavailable', 503)))
    with pytest.raises(submit.Refused, match='policy'):
        submit._policy_files('owner/repo')


def test_fresh_file_read_bypasses_negative_and_content_cache(monkeypatch):
    import base64
    from ossforge import gh
    gh._MISSING.add(('owner/repo', 'AI_POLICY.md', None))
    calls = []
    monkeypatch.setattr(gh, 'api', lambda *a, **k: (calls.append(k) or {'encoding': 'base64', 'content': base64.b64encode(b'no AI-generated contributions').decode()}))
    assert gh.file_text('owner/repo', 'AI_POLICY.md', fresh=True) == 'no AI-generated contributions'
    assert calls[0]['fresh'] is True


def test_strict_policy_listing_allows_missing_optional_directory_not_missing_root(monkeypatch):
    import pytest
    from ossforge import gh
    def api(path, **kw):
        if path.endswith('/.github'):
            raise gh.GhError('not found', 404)
        return []
    monkeypatch.setattr(gh, 'api', api)
    assert gh.existing_files('owner/repo', ('README.md', '.github/AI_POLICY.md'), fresh=True, strict=True) == []
    monkeypatch.setattr(gh, 'api', lambda *a, **k: (_ for _ in ()).throw(gh.GhError('not found', 404)))
    with pytest.raises(gh.GhError):
        gh.existing_files('owner/repo', ('README.md',), fresh=True, strict=True)


def test_strict_policy_listing_rejects_malformed_or_saturated_result(monkeypatch):
    import pytest
    from ossforge import gh
    for result in ({}, None, [None], [{'path': 'x', 'type': 'file'}] * 1000):
        monkeypatch.setattr(gh, 'api', lambda *a, result=result, **k: result)
        with pytest.raises(gh.GhError):
            gh.existing_files('owner/repo', ('README.md',), fresh=True, strict=True)


def test_existing_files_handles_file_payload_for_template_parent(monkeypatch):
    from ossforge import gh, policy
    def api(path, **kw):
        if path.endswith('/contents/.github/PULL_REQUEST_TEMPLATE'):
            return {'path': '.github/PULL_REQUEST_TEMPLATE', 'type': 'file'}
        return []
    monkeypatch.setattr(gh, 'api', api)
    res = gh.existing_files('owner/repo', policy.POLICY_FILES, fresh=True, strict=True)
    assert '.github/PULL_REQUEST_TEMPLATE' in res


def test_existing_fork_fix_and_work_claims_are_not_unclaimed():
    for body in ["I have a fix ready for this on my fork", "I'd like to work on improving this",
                 "I have a working fix on my fork", "I’d like to work on this", "I've a patch ready"]:
        assert vet.comment_claims_issue(body), body
    assert not vet.comment_claims_issue('Does anyone have a fix ready?')


def test_collaborator_only_target_stops_vet_before_issue_work(monkeypatch):
    from ossforge import gh
    seen = []
    def api(endpoint, **kw):
        seen.append(endpoint)
        assert endpoint == 'repos/owner/repo'
        return {'pull_request_creation_policy': 'collaborators_only', 'permissions': {'push': False}}
    monkeypatch.setattr(gh, 'api', api)
    out = vet.vet_issue('owner/repo', 993)
    assert out.decision == 'skip' and 'collaborators only' in out.reasons[0]
    assert seen == ['repos/owner/repo']


def test_repository_pr_access_compatibility_and_unknowns(monkeypatch):
    from ossforge import gh, repo_access
    for info, blocked in [({}, False), ({'pull_request_creation_policy': 'all'}, False),
                           ({'pull_request_creation_policy': 'collaborators_only', 'permissions': {'push': True}}, False),
                           ({'pull_request_creation_policy': 'collaborators_only'}, True),
                           ({'pull_request_creation_policy': 'new_mode'}, True),
                           ({'pull_request_creation_policy': 'collaborators_only', 'permissions': []}, True)]:
        monkeypatch.setattr(gh, 'api', lambda *a, info=info, **k: info)
        assert bool(repo_access.pr_restriction('owner/repo')) is blocked
