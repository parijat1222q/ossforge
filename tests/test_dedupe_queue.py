import pytest

from ossforge import dedupe, queue, safety, submit
from ossforge.config import Config
from ossforge.limiter import DailyLimiter


def test_similarity():
    a = "swallowed exception in telemetry flush"
    assert dedupe.similarity(a, "Telemetry flush swallowed exception hides errors") > 0.4
    assert dedupe.similarity(a, "add dark mode to dashboard") < 0.1


def M(state, score):
    return dedupe.Match(1, "t", "u", state, False, score, "r")


def test_is_duplicate():
    assert dedupe.is_duplicate([M("open", 0.26)])
    assert dedupe.is_duplicate([M("closed", 0.6)])
    assert not dedupe.is_duplicate([M("closed", 0.3)])
    assert not dedupe.is_duplicate([])


def test_queue_roundtrip(tmp_path):
    q = queue.Queue(tmp_path / "q.json")
    it = queue.Item("a", "o/r", "x.py", 3, "k", "d")
    assert q.add(it) and not q.add(it)
    q.set("a", status="clean")
    q.save()
    assert queue.Queue(tmp_path / "q.json").items["a"].status == "clean"


def _verified(**kw):
    base = dict(status="verified", issue_title="t", issue_body="b", pr_title="t", pr_body="b", branch="fix/x", clone="/c")
    return queue.Item("a", "o/r", "x.py", 3, "k", "d", **{**base, **kw})


def test_submit_refuses_unverified(tmp_path):
    it = queue.Item("a", "o/r", "x.py", 3, "k", "d", status="clean")
    cfg = Config(kill_file=str(tmp_path / "STOP"))
    with pytest.raises(submit.Refused, match="verified"):
        submit.preflight(it, DailyLimiter(tmp_path / "d.json"), cfg, [])


def test_submit_refuses_over_cap(tmp_path):
    lim = DailyLimiter(tmp_path / "d.json", cap=1)
    lim.record()
    cfg = Config(kill_file=str(tmp_path / "STOP"))
    with pytest.raises(submit.Refused, match="cap"):
        submit.preflight(_verified(), lim, cfg, [])


def test_submit_refuses_when_kill_switch_on(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("x")
    with pytest.raises(submit.Refused, match="kill switch"):
        submit.preflight(_verified(), DailyLimiter(tmp_path / "d.json"), Config(kill_file=str(stop)), [])


def test_safety_blocks_protected_and_secrets():
    bad = safety.check_diff([".github/workflows/ci.yml", "a.py"], "+key = 'AKIAABCDEFGHIJKLMNOP'\n")
    assert any("protected" in b for b in bad) and any("secret" in b for b in bad)
    assert safety.check_diff(["src/a.py"], "+x = 1\n") == []


def test_repo_limits():
    cfg = Config()
    open_pr = [{"event": "pr_opened", "repo": "o/r", "state": "open"}]
    assert "already open" in safety.repo_allowed(cfg, "o/r", open_pr)
    closed = [{"event": "pr_opened", "repo": "o/r", "state": "closed_unmerged", "ts": 1000}]
    assert safety.repo_allowed(cfg, "o/r", closed, now=1000 + 86400) is not None
    assert safety.repo_allowed(cfg, "o/r", closed, now=1000 + 86400 * 30) is None
    assert safety.repo_allowed(cfg, "other/x", open_pr) is None


def test_effective_history_folds_state():
    recs = [{"event": "pr_opened", "repo": "o/r", "pr": 5, "state": "open"},
            {"event": "pr_state", "repo": "o/r", "pr": 5, "state": "merged"}]
    assert safety.effective_history(recs)[0]["state"] == "merged"


def test_snippet_marks_the_finding_line(tmp_path):
    from ossforge import hunt
    (tmp_path / "m.py").write_text("\n".join(f"line{i}" for i in range(1, 21)))
    out = hunt.snippet(tmp_path, "m.py", 10, radius=2)
    assert out.splitlines()[2].startswith("   10>") and "line8" in out and "line12" in out and "line13" not in out
    assert hunt.snippet(tmp_path, "missing.py", 3) == ""


def test_publication_duplicate_search_cannot_treat_unavailable_as_empty(monkeypatch):
    from ossforge import gh
    monkeypatch.setattr(gh, 'search_issues', lambda *a, **k: (_ for _ in ()).throw(gh.GhError('unavailable')))
    with pytest.raises(dedupe.Incomplete):
        dedupe.find_matches('o/r', 'parser regression', 'reproduction', 'x.py', [], strict=True)


def test_publication_commits_cannot_treat_unavailable_as_empty(monkeypatch):
    from ossforge import gh
    monkeypatch.setattr(gh, 'api', lambda *a, **k: (_ for _ in ()).throw(gh.GhError('unavailable')))
    with pytest.raises(dedupe.Incomplete):
        dedupe.recent_commit_hits('o/r', 'x.py', ['parser'], strict=True)


def test_strict_search_requires_fresh_complete_results(monkeypatch):
    from ossforge import gh
    calls = []
    monkeypatch.setattr(gh, 'api', lambda *a, **k: (calls.append(k) or {'items': [], 'total_count': 1, 'incomplete_results': False}))
    with pytest.raises(dedupe.Incomplete):
        dedupe.find_matches('o/r', 'parser crash', '', 'x.py', [], strict=True)
    assert calls and calls[0]['fresh'] is True


def test_strict_search_rejects_incomplete_even_when_empty(monkeypatch):
    from ossforge import gh
    monkeypatch.setattr(gh, 'api', lambda *a, **k: {'items': [], 'total_count': 0, 'incomplete_results': True})
    with pytest.raises(dedupe.Incomplete):
        dedupe.find_matches('o/r', 'parser crash', '', 'x.py', [], strict=True)


@pytest.mark.parametrize('text', ['[{"id":"prior-publication",', '{}', '[{}]', '[null]'])
def test_corrupt_existing_queue_refuses_without_overwriting(tmp_path, text):
    p = tmp_path / 'queue.json'
    p.write_text(text)
    with pytest.raises(queue.InvalidState):
        queue.Queue(p)
    assert p.read_text() == text


def test_failed_queue_save_preserves_previous_bytes(tmp_path, monkeypatch):
    from pathlib import Path
    p = tmp_path / 'queue.json'
    q = queue.Queue(p)
    q.add(queue.Item('first', 'o/r', 'm.py', 1, 'controlled', 'local'))
    q.save()
    original = p.read_bytes()
    q.add(queue.Item('second', 'o/r', 'n.py', 1, 'controlled', 'local'))
    replace = Path.replace
    def fail(source, target):
        if target == p:
            raise OSError('controlled replace failure')
        return replace(source, target)
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        q.save()
    assert p.read_bytes() == original


def test_duplicate_queue_ids_refuse_instead_of_losing_first_progress(tmp_path):
    import json
    from dataclasses import asdict
    p = tmp_path / 'queue.json'
    first = asdict(queue.Item('same', 'o/r', 'm.py', 1, 'controlled', 'first', submitted={'issue_url': 'retained'}))
    second = {**first, 'submitted': {}}
    p.write_text(json.dumps([first, second]))
    with pytest.raises(queue.InvalidState):
        queue.Queue(p)
    assert 'retained' in p.read_text()


@pytest.mark.parametrize('text', ['{"day":', '{}', '[]', '{"day":"2026-11-02","count":true}', '{"day":"2026-11-02","count":-1}'])
def test_existing_invalid_daily_ledger_cannot_restore_allowance(tmp_path, text):
    from ossforge import limiter
    text = text.replace("2026-11-02", limiter.date.today().isoformat())
    p = tmp_path / 'daily.json'
    p.write_text(text)
    with pytest.raises(limiter.InvalidState):
        limiter.DailyLimiter(p).remaining()
    assert p.read_text() == text


def test_daily_ledger_failed_replace_preserves_used_allowance(tmp_path, monkeypatch):
    from pathlib import Path
    p = tmp_path / 'daily.json'
    limit = DailyLimiter(p)
    limit.record()
    before = p.read_bytes()
    replace = Path.replace
    def fail(source, target):
        if target == p:
            raise OSError('controlled failed replace')
        return replace(source, target)
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        limit.record()
    assert p.read_bytes() == before and limit.remaining() == 1


def test_daily_ledger_rollover_and_future_date(tmp_path):
    import json
    from datetime import timedelta
    from ossforge import limiter
    p = tmp_path / 'daily.json'
    today = limiter.date.today()
    p.write_text(json.dumps({'day': (today - timedelta(days=1)).isoformat(), 'count': 2}))
    assert DailyLimiter(p).remaining() == 2
    p.write_text(json.dumps({'day': (today + timedelta(days=1)).isoformat(), 'count': 2}))
    with pytest.raises(limiter.InvalidState, match='future'):
        DailyLimiter(p).remaining()


def test_valid_audit_lifecycle_and_missing_file(tmp_path):
    audit = safety.Audit(tmp_path / 'audit.jsonl')
    assert audit.read() == []
    audit.log('pr_opened', repo='o/r', pr=1, state='open')
    audit.update_state('o/r', 1, 'merged')
    audit.log('issue_opened', repo='o/r', url='local-test')
    history = safety.effective_history(audit.read())
    assert len(history) == 3 and history[0]['state'] == 'merged'
    assert safety.repo_allowed(Config(), 'o/r', history) is None


def test_unreadable_audit_is_not_empty_history(tmp_path, monkeypatch):
    from pathlib import Path
    audit = safety.Audit(tmp_path / 'audit.jsonl')
    read = Path.read_text
    def fail(path, *a, **k):
        if path == audit.path:
            raise PermissionError('controlled unreadable file')
        return read(path, *a, **k)
    monkeypatch.setattr(Path, 'read_text', fail)
    with pytest.raises(safety.Blocked, match='audit'):
        audit.read()


@pytest.mark.parametrize('style', ['compact', 'week'])
def test_same_parsed_day_never_restores_allowance(tmp_path, style):
    import json
    from ossforge import limiter
    today = limiter.date.today()
    iso = today.isocalendar()
    day = today.strftime('%Y%m%d') if style == 'compact' else f'{iso.year}-W{iso.week:02d}-{iso.weekday}'
    p = tmp_path / 'daily.json'
    p.write_text(json.dumps({'day': day, 'count': 2}))
    limit = DailyLimiter(p)
    try:
        limiter.date.fromisoformat(day)
    except ValueError:  # Python 3.10 accepts only canonical dates.
        with pytest.raises(limiter.InvalidState):
            limit.remaining()
        return
    assert limit.remaining() == 0
    limit.record()
    assert json.loads(p.read_text()) == {'day': today.isoformat(), 'count': 3}


def test_same_gate_reuses_identical_queries_but_next_gate_is_fresh(monkeypatch):
    from ossforge import dedupe, gh
    calls = []
    def search(q, **kw):
        calls.append((q, kw))
        return []
    monkeypatch.setattr(gh, 'search_issues', search)
    import inspect
    for _ in range(2):
        gate = {}
        for path in ['rapidjson.cpp', 'docs/rawjson.rst', 'CHANGES.rst']:
            extra = {'query_results': gate} if 'query_results' in inspect.signature(dedupe.find_matches).parameters else {}
            dedupe.find_matches('o/r', 'RawJSON Unicode corrupts ASCII', '', path, [], strict=True, **extra)
    assert len(calls) == 8  # three distinct paths plus one shared title, per fresh gate
    assert all(kw['fresh'] and kw['require_complete'] for _, kw in calls)


def test_empty_title_uses_body_evidence_without_repo_wide_query(monkeypatch):
    from ossforge import gh
    calls = []
    body = 'Replacement vector update deletes existing retained memory record on invalid dimensions'
    def search(query, **kw):
        calls.append(query)
        if query.strip() == 'repo:o/r':
            raise gh.GhError('search coverage incomplete')
        if 'replacement' in query.lower():
            return [{'number': 7, 'title': 'Replacement vector update', 'body': body,
                     'state': 'open', 'html_url': 'https://example.invalid/7'}]
        return []
    monkeypatch.setattr(gh, 'search_issues', search)
    hits = dedupe.find_matches('o/r', 'Fix bug', body, 'new/path.py', [], strict=True)
    assert hits and hits[0].number == 7
    assert not any(q.strip() == 'repo:o/r' for q in calls)


def test_no_semantic_evidence_requires_review_not_repo_wide_search(monkeypatch):
    from ossforge import gh
    calls = []
    monkeypatch.setattr(gh, 'search_issues', lambda q, **kw: calls.append(q) or [])
    with pytest.raises(dedupe.Incomplete, match='evidence'):
        dedupe.find_matches('o/r', 'Fix bug', '', 'a.py', [], strict=True)
    assert not any(q.strip() == 'repo:o/r' for q in calls)


def test_empty_commit_keywords_still_check_api_availability(monkeypatch):
    from ossforge import gh
    monkeypatch.setattr(gh, 'api', lambda *a, **kw: (_ for _ in ()).throw(gh.GhError('unavailable')))
    with pytest.raises(dedupe.Incomplete, match='unavailable'):
        dedupe.recent_commit_hits('o/r', 'a.py', [], strict=True)
