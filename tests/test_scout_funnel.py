from ossforge import gh, radar, scout, vet
from ossforge.config import Config


def _issue(n, body="Steps: run x, expected y, got z. Reproduce with `foo`.", title="crash on empty input"):
    return {"title": title, "body": body, "repository_url": "https://api.github.com/repos/acme/lib", "number": n,
            "html_url": f"https://github.com/acme/lib/issues/{n}", "updated_at": "2099-01-01T00:00:00Z", "labels": [],
            "comments": 1}


def _cfg(tmp_path):
    return Config(kill_file=str(tmp_path / "STOP"), state_dir=str(tmp_path / "state"))


def test_no_matches_is_distinct_from_filtered(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [])
    assert scout.scout(_cfg(tmp_path)) == []
    assert scout.explain_funnel(scout.LAST_FUNNEL).startswith("NO MATCHES")
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [_issue(1, title="Bounty $50 for fix")])
    assert scout.scout(_cfg(tmp_path)) == []
    msg = scout.explain_funnel(scout.LAST_FUNNEL)
    assert msg.startswith("ALL FILTERED") and "'spam': 1" in msg


def test_rate_limit_reports_partial_not_silence(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "api", lambda *a, **k: {"stargazers_count": 500})
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [_issue(1), _issue(2)])
    monkeypatch.setattr(vet, "clear_requirements", lambda b: True)

    def boom(repo):
        raise gh.RateLimited("secondary limit (retry in ~60s)", 429, 60)

    monkeypatch.setattr(radar, "analyze", boom)
    assert scout.scout(_cfg(tmp_path)) == []
    msg = scout.explain_funnel(scout.LAST_FUNNEL)
    assert msg.startswith("STOPPED EARLY") and "rate limit" in msg
    # Test a fresh search failure separately from the pending-batch resume.
    (_cfg(tmp_path).path("discovery-resume.json")).unlink()
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: (_ for _ in ()).throw(gh.RateLimited("cooldown", 429, 30)))
    scout.scout(_cfg(tmp_path))
    assert scout.explain_funnel(scout.LAST_FUNNEL).startswith("STOPPED EARLY")


def test_kept_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "api", lambda *a, **k: {"stargazers_count": 500})
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [_issue(1)])
    monkeypatch.setattr(vet, "clear_requirements", lambda b: True)
    monkeypatch.setattr(radar, "analyze", lambda r: radar.RepoScore(r, 80, "Good"))
    monkeypatch.setattr(vet, "vet_issue", lambda *a, **k: vet.Verdict("acme/lib#1", "approve"))
    from ossforge import acceptance
    monkeypatch.setattr(acceptance, "evidence", lambda r: acceptance.Acceptance(r, "outside-merges-seen", ["x"]))
    out = scout.scout(_cfg(tmp_path))
    assert len(out) == 1 and scout.LAST_FUNNEL["approved"] == 1
    assert scout.explain_funnel(scout.LAST_FUNNEL).startswith("1 of 1")


def test_unknown_queue_id_is_a_clean_error(tmp_path, capsys):
    from ossforge import cli
    cfgf = tmp_path / "c.yaml"
    cfgf.write_text(f"kill_file: {tmp_path}/STOP\nstate_dir: {tmp_path}/.state\n")
    rc = cli.main(["--config", str(cfgf), "submit", "nope"])
    assert rc == 2 and "unknown queue id" in capsys.readouterr().err


def test_min_stars_checked_against_repo_not_issue_search(monkeypatch, tmp_path):
    monkeypatch.setattr(vet, "clear_requirements", lambda b: True)
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [_issue(1), _issue(2)])
    calls = []
    monkeypatch.setattr(gh, "api", lambda path, **k: calls.append(path) or {"stargazers_count": 18})
    monkeypatch.setattr(radar, "analyze", lambda r: (_ for _ in ()).throw(AssertionError("below minimum")))
    assert scout.scout(_cfg(tmp_path)) == []
    assert calls == ["repos/acme/lib"]
    assert scout.LAST_FUNNEL["below_min_stars"] == 2
    assert "below_min_stars" in scout.explain_funnel(scout.LAST_FUNNEL)


def test_star_metadata_failure_is_not_a_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [_issue(1)])
    monkeypatch.setattr(vet, "clear_requirements", lambda b: True)
    monkeypatch.setattr(gh, "api", lambda *a, **k: {})
    assert scout.scout(_cfg(tmp_path)) == []
    assert scout.LAST_FUNNEL["repo_metadata_failed"] == 1
    assert "repo_metadata_failed" in scout.explain_funnel(scout.LAST_FUNNEL)


def test_star_metadata_rate_limit_stops_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [_issue(1)])
    monkeypatch.setattr(vet, "clear_requirements", lambda b: True)
    monkeypatch.setattr(gh, "api", lambda *a, **k: (_ for _ in ()).throw(gh.RateLimited("cooldown", 429, 60)))
    assert scout.scout(_cfg(tmp_path)) == []
    assert scout.explain_funnel(scout.LAST_FUNNEL).startswith("STOPPED EARLY")


def test_features_aggregates_funnel_and_stops_on_rate_limit(monkeypatch, tmp_path):
    calls = []

    def fake_scout(*args, **kwargs):
        calls.append(args[3])
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL.update(fetched=1, kept=0, approved=0, spam=1,
                                stopped="cooldown" if len(calls) == 2 else "")
        return []

    monkeypatch.setattr(scout, "scout", fake_scout)
    assert scout.scout_features(_cfg(tmp_path)) == []
    assert calls == scout.FEATURE_LABELS[:2]
    assert scout.LAST_FUNNEL["fetched"] == 2
    assert scout.LAST_FUNNEL["spam"] == 2
    assert scout.LAST_FUNNEL["stopped"] == "cooldown"
    assert scout.explain_funnel(scout.LAST_FUNNEL).startswith("STOPPED EARLY")


def test_features_empty_searches_are_no_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "search_issues", lambda *a, **k: [])
    assert scout.scout_features(_cfg(tmp_path)) == []
    assert scout.LAST_FUNNEL["fetched"] == 0
    assert scout.LAST_FUNNEL["stopped"] == ""
    assert scout.explain_funnel(scout.LAST_FUNNEL).startswith("NO MATCHES")


def test_features_does_not_query_another_label_after_stop(monkeypatch, tmp_path):
    calls = []

    def fake_scout(*args, **kwargs):
        calls.append(args[3])
        scout.LAST_FUNNEL.clear()
        scout.LAST_FUNNEL.update(fetched=0, kept=0, approved=0, stopped="rate limit")
        return []

    monkeypatch.setattr(scout, "scout", fake_scout)
    assert scout.scout_features(_cfg(tmp_path)) == []
    assert calls == scout.FEATURE_LABELS[:1]
    assert scout.LAST_FUNNEL["stopped"] == "rate limit"
