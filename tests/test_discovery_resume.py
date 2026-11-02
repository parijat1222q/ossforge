
from ossforge import acceptance, gh, radar, scout, vet
from ossforge.config import Config


def setup(monkeypatch, tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    items = [dict(title=f'Useful defect {n}', body='Steps to reproduce the error with expected output ' * 3,
                  repository_url='https://api.github.com/repos/org/lib', number=n,
                  html_url=f'https://github.com/org/lib/issues/{n}', updated_at='v1', state='open')
             for n in (1, 2, 3)]
    searches, refreshes, visits = [], [], []
    monkeypatch.setattr(gh, 'search_issues', lambda q, **k: searches.append(q) or items)
    def api(path, **kw):
        if '/issues/' in path:
            refreshes.append((path, kw))
            return dict(items[int(path.rsplit('/', 1)[1])-1])
        return {'stargazers_count': 1000}
    monkeypatch.setattr(gh, 'api', api)
    monkeypatch.setattr(radar, 'analyze', lambda r: radar.RepoScore(r, 80, 'Good'))
    monkeypatch.setattr(acceptance, 'evidence', lambda r: acceptance.Acceptance(r, 'unknown', ['limited']))
    def qualify(repo, number, **kw):
        visits.append(number)
        if visits == [1, 2]:
            raise gh.ReadBudgetExceeded('stop during second issue')
        return vet.Verdict(f'{repo}#{number}', 'skip', ['occupied'])
    monkeypatch.setattr(vet, 'vet_issue', qualify)
    return cfg, items, searches, refreshes, visits


def test_resume_reaches_unexamined_issues_without_repeating_completed_vet(monkeypatch, tmp_path):
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    assert [c.number for c in scout.scout(cfg, query='first')] == [1]
    assert scout.LAST_FUNNEL['stopped']
    assert [c.number for c in scout.scout(cfg, query='first')] == [2, 3]
    assert visits == [1, 2, 2, 3]
    assert searches == ['first']
    assert [p for p, k in refreshes] == ['repos/org/lib/issues/2', 'repos/org/lib/issues/3']
    assert all(k.get('fresh') is True for p, k in refreshes)
    # A completed batch is not a permanent rejection cache.
    scout.scout(cfg, query='first')
    assert searches == ['first', 'first']


def test_resume_refresh_drops_closed_issue_and_still_vets_changed_issue(monkeypatch, tmp_path):
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    scout.scout(cfg, query='first')
    items[1]['state'] = 'closed'
    items[2]['body'] += ' New active-work claim must be checked by normal vet.'
    out = scout.scout(cfg, query='first')
    assert [c.number for c in out] == [3]
    assert visits == [1, 2, 3]
    assert scout.LAST_FUNNEL['closed_or_missing'] == 1


def test_query_batches_do_not_mix_or_resume_approval(monkeypatch, tmp_path):
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    scout.scout(cfg, query='first')
    scout.scout(cfg, query='different')
    assert searches == ['first', 'different']
    assert visits == [1, 2, 1, 2, 3]
    scout.scout(cfg, query='first')
    assert visits[-2:] == [2, 3]


def test_resume_refresh_rate_limit_keeps_position(monkeypatch, tmp_path):
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    scout.scout(cfg, query='first')
    real = gh.api
    def limited(path, **kw):
        if '/issues/' in path:
            raise gh.RateLimited('wait', 429, 60)
        return real(path, **kw)
    monkeypatch.setattr(gh, 'api', limited)
    assert scout.scout(cfg, query='first') == []
    assert scout.LAST_FUNNEL['stopped']
    monkeypatch.setattr(gh, 'api', real)
    assert [c.number for c in scout.scout(cfg, query='first')] == [2, 3]


def test_resume_expiry_and_corruption_start_fresh(monkeypatch, tmp_path):
    from ossforge import discovery_resume
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    scout.scout(cfg, query='first')
    monkeypatch.setattr(discovery_resume.time, 'time', lambda: 99999999999)
    scout.scout(cfg, query='first')
    assert searches == ['first', 'first']
    path = cfg.path('discovery-resume.json')
    path.write_text('{broken')
    scout.scout(cfg, query='first')
    assert len(searches) == 3
    discovery_resume.save(path, 'first', 20, [{'repo': '../bad/x', 'number': 1}])
    assert discovery_resume.load(path, 'first', 20) is None


def test_resumed_lead_can_be_approved_only_after_normal_vet(monkeypatch, tmp_path):
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    scout.scout(cfg, query='first')
    approvals = []
    def qualify(repo, number, **kw):
        approvals.append(number)
        return vet.Verdict(f'{repo}#{number}', 'approve')
    monkeypatch.setattr(vet, 'vet_issue', qualify)
    out = scout.scout(cfg, query='first')
    assert approvals == [2, 3]
    assert all(c.verdict.decision == 'approve' for c in out)
    # Resume state contains no stored approval, titles, bodies or verdicts.
    assert cfg.path('discovery-resume.json').read_text().strip() == '{}'


def test_bad_refresh_is_partial_not_a_clean_no_find(monkeypatch, tmp_path):
    cfg, items, searches, refreshes, visits = setup(monkeypatch, tmp_path)
    scout.scout(cfg, query='first')
    monkeypatch.setattr(gh, 'api', lambda *a, **k: {})
    assert scout.scout(cfg, query='first') == []
    assert scout.LAST_FUNNEL['stopped']
    assert visits == [1, 2]
