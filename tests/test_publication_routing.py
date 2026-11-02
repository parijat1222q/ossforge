import pytest
from ossforge import policy_review, submit
from ossforge.config import Config
from ossforge.queue import Item


def snap(sha='a'):
    return {'docs': [{'path': 'docs/contributing.rst', 'url': 'https://example.invalid/guide', 'sha': sha,
                      'text': 'Do not create both an issue and a pull request right away.'}], 'scanned_locations': []}


def test_reviewed_route_follows_pinned_contribution_sources(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path))
    monkeypatch.setattr(policy_review, 'snapshot', lambda *a, **kw: snap())
    policy_review.record(cfg, 'org/repo', snap(), 'Read guide: direct PR for new fixes', publication_route='pr_only')
    item = Item('i', 'org/repo', 'a.py', 1, 'perf', 'candidate')
    assert submit._publication_mode(item, cfg) == 'pr_only'
    monkeypatch.setattr(policy_review, 'snapshot', lambda *a, **kw: snap('changed'))
    with pytest.raises(submit.Refused, match='changed'):
        submit._publication_mode(item, cfg)


def test_issue_first_review_stops_immediate_pr_but_existing_source_stays_linked(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path))
    monkeypatch.setattr(policy_review, 'snapshot', lambda *a, **kw: snap())
    policy_review.record(cfg, 'org/repo', snap(), 'Open discussion and obtain maintainer feedback first', publication_route='issue_first')
    item = Item('i', 'org/repo', 'a.py', 1, 'bug', 'candidate')
    with pytest.raises(submit.Refused, match='issue.first'):
        submit._publication_mode(item, cfg)
    item.source_issue = 7
    # Existing issue alone is not evidence of required feedback/approval.
    with pytest.raises(submit.Refused, match='issue.first'):
        submit._publication_mode(item, cfg)


def test_unresolved_route_never_falls_back_to_default(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path))
    monkeypatch.setattr(policy_review, 'snapshot', lambda *a, **kw: snap())
    policy_review.record(cfg, 'org/repo', snap(), 'Conflicting instructions require review', publication_route='unresolved')
    with pytest.raises(submit.Refused, match='unresolved'):
        submit._publication_mode(Item('i', 'org/repo', '', 0, 'bug', ''), cfg)


def test_reviewed_no_specific_rule_keeps_default_issue_plus_pr(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path))
    monkeypatch.setattr(policy_review, 'snapshot', lambda *a, **kw: snap())
    policy_review.record(cfg, 'org/repo', snap(), 'No specific publication rule after reading complete guide', publication_route='issue_pr')
    assert submit._publication_mode(Item('i', 'org/repo', '', 0, 'bug', ''), cfg) == 'issue_pr'


def test_configured_override_cannot_contradict_reviewed_default(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path), pr_only_repos=['org/repo'])
    monkeypatch.setattr(policy_review, 'snapshot', lambda *a, **kw: snap())
    policy_review.record(cfg, 'org/repo', snap(), 'No specific rule found', publication_route='issue_pr')
    with pytest.raises(submit.Refused, match='conflicts'):
        submit._publication_mode(Item('i', 'org/repo', '', 0, 'bug', ''), cfg)


def test_missing_extra_source_is_unavailable_not_default(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path))
    source = {**snap(), 'extra_paths': ['docs/contributing.rst']}
    policy_review.record(cfg, 'org/repo', source, 'Direct PR required', publication_route='pr_only')
    monkeypatch.setattr(policy_review.gh, 'existing_files', lambda *a, **kw: [])
    monkeypatch.setattr(policy_review.gh, 'api', lambda *a, **kw: (_ for _ in ()).throw(policy_review.gh.GhError('missing', 404)))
    with pytest.raises(submit.Refused, match='missing'):
        submit._publication_mode(Item('i', 'org/repo', '', 0, 'bug', ''), cfg)


def test_extra_policy_paths_pinned_fresh_and_escape_refused(monkeypatch):
    import base64
    seen = []
    monkeypatch.setattr(policy_review.gh, 'existing_files', lambda *a, **kw: [])
    def api(path, **kw):
        seen.append((path, kw))
        return {'encoding': 'base64', 'content': base64.b64encode(b'Direct PR for new work').decode(),
                'sha': 'guide-sha', 'html_url': 'https://example.invalid/guide'}
    monkeypatch.setattr(policy_review.gh, 'api', api)
    source = policy_review.snapshot('org/repo', extra_paths=['docs/contributing.rst'])
    assert source['docs'][0]['sha'] == 'guide-sha' and seen[0][1]['fresh']
    for path in ['../private', '/absolute', 'docs/*', 'docs/../private']:
        with pytest.raises(ValueError):
            policy_review.snapshot('org/repo', extra_paths=[path])


def test_cli_records_route_and_additional_guide(tmp_path, monkeypatch):
    from ossforge import cli
    config = tmp_path / 'cfg.yaml'
    config.write_text(f'state_dir: {tmp_path}/state\n')
    seen = []
    def snapshot(repo, **kw):
        seen.append(kw)
        return {**snap(), 'extra_paths': kw['extra_paths']}
    monkeypatch.setattr(policy_review, 'snapshot', snapshot)
    assert cli.main(['--config', str(config), 'policy-review', 'org/repo', '--accept',
                     '--extra-path', 'docs/contributing.rst', '--publication-route', 'pr_only',
                     '--note', 'Read linked contribution guide: direct PR']) == 0
    rec = policy_review._load(Config(state_dir=str(tmp_path/'state')))['org/repo']
    assert rec['publication_route'] == 'pr_only' and seen == [{'extra_paths': ['docs/contributing.rst']}]
