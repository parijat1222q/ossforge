from ossforge import policy


def test_explicit_unsupervised_agent_ban_with_markdown_emphasis():
    text = 'We do not police how you author changes. Absolutely **no** unsupervised agentic tools like ExampleBot.'
    result = policy.scan_files({'.github/AI_POLICY.md': text})
    assert result.anti_ai and result.source == '.github/AI_POLICY.md'


def test_ordinary_agent_tool_discussion_is_not_a_ban():
    assert not policy.scan_text('We allow supervised agentic tools and discuss unsupervised agentic tools.').anti_ai


def test_readme_and_contributing_rst_are_policy_sources(tmp_path):
    from ossforge import hunt
    for name in ('README.rst', 'CONTRIBUTING.rst', '.github/CONTRIBUTING.rst', 'docs/CONTRIBUTING.rst'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('No AI-assisted contributions.')
        assert hunt.policy_of(tmp_path).anti_ai
        path.unlink()


def test_rst_restrictions_are_in_review_snapshot(monkeypatch):
    from ossforge import policy_review
    monkeypatch.setattr(policy_review.gh, 'existing_files', lambda repo, paths, **kw: [p for p in paths if p == 'README.rst'])
    import base64
    text = 'Automated issue reports need production examples, not contrived examples.'
    monkeypatch.setattr(policy_review.gh, 'api', lambda *a, **k: {
        'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode(),
        'html_url': 'https://github.com/local/fixture/blob/main/README.rst', 'sha': 'fixture',
    })
    snap = policy_review.snapshot('local/fixture')
    assert snap['docs'][0]['text'] == text and snap['docs'][0]['path'] == 'README.rst'


def test_pr_template_and_contrib_guide_are_review_sources(monkeypatch):
    from ossforge import policy_review
    import base64
    contents = {
        '.github/PULL_REQUEST_TEMPLATE.md': 'AI-assisted changes must include an LLM commit tag and human input.',
        'docs/contrib.rst': 'All tests must pass before merge.',
    }
    monkeypatch.setattr(policy_review.gh, 'existing_files', lambda repo, paths, **kw: [p for p in paths if p in contents])
    monkeypatch.setattr(policy_review.gh, 'api', lambda path, **kw: {
        'encoding': 'base64', 'content': base64.b64encode(contents[path.split('/contents/')[1]].encode()).decode(),
        'html_url': 'https://github.com/local/fixture', 'sha': path,
    })
    snap = policy_review.snapshot('local/fixture')
    assert {d['path']: d['text'] for d in snap['docs']} == contents


def test_pr_template_changes_invalidate_review(tmp_path, monkeypatch):
    from ossforge import policy_review
    from ossforge.config import Config
    cfg = Config(state_dir=str(tmp_path))
    path = '.github/PULL_REQUEST_TEMPLATE.md'
    import base64
    sha = ['old']
    monkeypatch.setattr(policy_review.gh, 'existing_files', lambda repo, paths, **kw: [p for p in paths if p == path])
    monkeypatch.setattr(policy_review.gh, 'api', lambda *a, **kw: {
        'encoding': 'base64', 'content': base64.b64encode(b'Please read these restrictions.').decode(),
        'html_url': 'https://github.com/local/fixture', 'sha': sha[0],
    })
    policy_review.record(cfg, 'local/fixture', policy_review.snapshot('local/fixture'), 'Reviewed template')
    assert policy_review.check(cfg, 'local/fixture') == ''
    sha[0] = 'new'
    assert path in policy_review.check(cfg, 'local/fixture')


def test_review_check_sees_new_policy_file_despite_cached_listing(tmp_path, monkeypatch):
    import base64
    from ossforge import policy_review
    from ossforge.config import Config
    cfg = Config(state_dir=str(tmp_path))
    policy_review.record(cfg, 'local/fixture', {'docs': [], 'scanned_locations': []}, 'Reviewed no files')
    def listing(repo, paths, *, fresh=False, strict=False):
        return ['.github/PULL_REQUEST_TEMPLATE.md'] if fresh and strict else []
    monkeypatch.setattr(policy_review.gh, 'existing_files', listing)
    monkeypatch.setattr(policy_review.gh, 'api', lambda *a, **kw: {
        'encoding': 'base64', 'content': base64.b64encode(b'Human review is required.').decode(),
        'html_url': 'https://github.com/local/fixture', 'sha': 'new-template',
    })
    assert '.github/PULL_REQUEST_TEMPLATE.md' in policy_review.check(cfg, 'local/fixture')


def test_review_snapshot_refuses_unreadable_listed_document(monkeypatch):
    import pytest
    from ossforge import policy_review
    monkeypatch.setattr(policy_review.gh, 'existing_files', lambda *a, **kw: ['README.md'])
    monkeypatch.setattr(policy_review.gh, 'api', lambda *a, **kw: {'encoding': 'none'})
    with pytest.raises(policy_review.gh.GhError, match='could not be read'):
        policy_review.snapshot('local/fixture')


def test_singular_doc_and_named_pr_templates_gate_local_hunt(tmp_path):
    from ossforge import hunt
    for name in ('doc/CONTRIBUTING.md', '.github/PULL_REQUEST_TEMPLATE/bug_fix.md'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('No AI-assisted contributions.')
        result = hunt.policy_of(tmp_path)
        assert result.anti_ai and result.source == name
        path.unlink()


def test_named_templates_apply_disclosure_and_snapshot(monkeypatch):
    import base64
    from ossforge import gh, policy_review, vet
    name = '.github/PULL_REQUEST_TEMPLATE/bug_fix.md'
    seen = []
    def api(endpoint, **kw):
        seen.append((endpoint, kw))
        if endpoint.endswith('/contents/' + name):
            return {'encoding': 'base64', 'content': base64.b64encode(b'You must disclose AI assistance.').decode(),
                    'sha': 'named-sha', 'html_url': 'https://github.com/local/fixture'}
        if endpoint.endswith('/contents/.github/PULL_REQUEST_TEMPLATE'):
            return [{'path': name, 'type': 'file'}, {'path': '.github/PULL_REQUEST_TEMPLATE/subdir', 'type': 'dir'}]
        return []
    monkeypatch.setattr(gh, 'api', api)
    texts = vet.policy_texts('local/fixture', fresh=True)
    assert policy.classify_disclosure(texts).status == 'mandatory'
    snap = policy_review.snapshot('local/fixture')
    assert snap['docs'][0]['path'] == name and snap['docs'][0]['sha'] == 'named-sha'
    assert all(kw.get('fresh') for _, kw in seen)


def test_named_template_listing_failure_is_not_no_policy(monkeypatch):
    import pytest
    from ossforge import gh, vet
    def api(endpoint, **kw):
        if endpoint.endswith('/contents/.github/PULL_REQUEST_TEMPLATE'):
            raise gh.GhError('service unavailable', 503)
        return []
    monkeypatch.setattr(gh, 'api', api)
    with pytest.raises(gh.GhError):
        vet.policy_texts('local/fixture', fresh=True)
