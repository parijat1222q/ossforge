"""Write orchestration against local stand-ins only: bare git repos for upstream and fork, a fake `gh` that logs
calls and answers from fixtures. Nothing here reaches GitHub, and no CLA path exists to trigger."""
import json
import os
import stat
import subprocess
import textwrap

import pytest

from ossforge import admission, evidence, gh, policy_review, queue, safety, submit
from ossforge.config import Config
from ossforge.limiter import DailyLimiter

GH_SHIM = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    open(os.environ["FAKE_GH_LOG"], "a").write(json.dumps(a) + "\\n")
    def reply(code, body):
        print(f"HTTP/2.0 {code} X\\n\\n" + json.dumps(body))
        sys.exit(0 if code < 400 else 1)
    if a[:1] == ["api"]:
        path = [x for x in a if "/" in x or x == "user"][-1] if len(a) > 4 else "user"
        path = a[a.index("GET") + 1]
        if path == "user":
            reply(200, {"login": os.environ["FAKE_GH_LOGIN"]})
        if path == "search/issues":
            reply(200, {"total_count": 0, "items": []})
        if path == "repos/acme/lib":
            reply(200, {"pull_request_creation_policy": "all"})
        if path == "repos/acme/lib/contents":
            reply(200, [])
        if path == "repos/acme/lib/issues/11":
            reply(200, {"state": "open", "html_url": "https://github.com/acme/lib/issues/11"})
        if path in ("repos/acme/lib/issues/11/comments", "repos/acme/lib/issues/11/timeline"):
            reply(200, [])
        if path.endswith("/commits"):
            reply(200, [])
        reply(404, {"message": "Not Found"})
    if a[:2] == ["issue", "create"]:
        print("https://github.com/acme/lib/issues/11"); sys.exit(0)
    if a[:2] == ["pr", "create"]:
        print("https://github.com/acme/lib/pull/12"); sys.exit(0)
    if a[:2] == ["repo", "fork"]:
        sys.exit(0)
    sys.exit(0)
''')

ME = ("Parijat Test", "owner@example.invalid")


def sh(*cmd, cwd=None, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "gh"
    shim.write_text(GH_SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "gh.log"
    up, fork = tmp_path / "up.git", tmp_path / "fork.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(up))
    sh("git", "init", "-q", "--bare", "-b", "main", str(fork))
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text(f"[user]\n name = {ME[0]}\n email = {ME[1]}\n"
                    f'[url "{up}"]\n insteadOf = https://github.com/acme/lib.git\n'
                    f'[url "{fork}"]\n insteadOf = https://github.com/me/lib.git\n')
    for k, v in {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_LOG": str(log), "FAKE_GH_LOGIN": "me",
                 "GIT_CONFIG_GLOBAL": str(gcfg), "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(up), str(seed))
    (seed / "a.py").write_text("x = 1\n")
    sh("git", "add", "-A", cwd=seed)
    sh("git", "commit", "-qm", "init", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    clone = tmp_path / "clone"
    sh("git", "clone", "-q", str(up), str(clone))
    sh("git", "remote", "set-url", "origin", "https://github.com/acme/lib.git", cwd=clone)
    sh("git", "checkout", "-qb", "fix/x", cwd=clone)
    (clone / "a.py").write_text("x = 2\n")
    (clone / "test_a.py").write_text("def test_a():\n    assert True\n")
    sh("git", "add", "-A", cwd=clone)
    sh("git", "commit", "-qm", "fix: x", cwd=clone)
    monkeypatch.setattr(gh, "WRITE_GAP_S", 0.0)
    monkeypatch.setattr(gh, "PACING_S", 0.0)
    monkeypatch.setattr(gh, "SEARCH_PER_MIN", 10_000)
    gh._MEM_GATES.clear()
    gh.configure(None)
    cfg = Config(kill_file=str(tmp_path / "STOP"), state_dir=str(tmp_path / ".state"), github_user="me",
                 git_name=ME[0], git_email=ME[1])
    it = queue.Item("a", "acme/lib", "a.py", 1, "k", "d", status="verified", issue_title="t", issue_body="b",
                    pr_title="fix: x", pr_body="b", branch="fix/x", clone=str(clone))
    it.verification = evidence.capture(clone, sh("git", "merge-base", "HEAD", "origin/HEAD", cwd=clone), [["fixture-test"]])
    policy_review.record(cfg, "acme/lib", {"docs": [], "scanned_locations": []}, "test: no restrictions")
    return dict(cfg=cfg, it=it, clone=clone, log=log, up=up, fork=fork, tmp=tmp_path, mp=monkeypatch,
                lim=DailyLimiter(tmp_path / "d.json", 2), audit=safety.Audit(tmp_path / "audit.jsonl"))


def writes(log):
    return [json.loads(x) for x in log.read_text().splitlines() if x and json.loads(x)[0] != "api"] if log.exists() else []


def run(w, execute=False):
    return submit.submit(w["it"], w["lim"], w["cfg"], w["audit"], execute=execute)


def test_dry_run_sends_nothing(world):
    r = run(world)
    assert r["dry_run"] and writes(world["log"]) == []


def test_happy_path_publishes_to_own_fork_with_correct_authorship(world):
    r = run(world, execute=True)
    assert r["pr"].endswith("/pull/12")
    w = writes(world["log"])
    assert [x[:2] for x in w] == [["issue", "create"], ["repo", "fork"], ["pr", "create"]]
    assert "me:fix/x" in w[2] and "acme/lib" in w[2]  # PR head is the operator's fork, target is upstream
    pushed = sh("git", "log", "--format=%an|%ae|%cn|%ce|%B", "-1", "fix/x", cwd=world["fork"])
    assert pushed.startswith(f"{ME[0]}|{ME[1]}|{ME[0]}|{ME[1]}")
    assert "Signed-off-by" not in pushed
    assert "fix/x" not in sh("git", "branch", cwd=world["up"])  # nothing was pushed to upstream


def test_wrong_gh_account_stops_before_any_write(world):
    world["mp"].setenv("FAKE_GH_LOGIN", "someone-else")
    with pytest.raises(submit.Refused, match="logged in as 'someone-else'"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_wrong_author_stops(world):
    sh("git", "-c", "user.email=other@example.invalid", "commit", "--amend", "--reset-author", "--no-edit", cwd=world["clone"])
    with pytest.raises(submit.Refused, match="author is"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_wrong_committer_stops(world):
    env = {**os.environ, "GIT_COMMITTER_EMAIL": "bot@example.invalid", "GIT_COMMITTER_NAME": "Bot"}
    sh("git", "commit", "--amend", "--no-edit", "-q", cwd=world["clone"], env=env)
    with pytest.raises(submit.Refused, match="committer is"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_env_override_of_identity_is_caught_for_new_commits(world):
    world["mp"].setenv("GIT_COMMITTER_EMAIL", "bot@example.invalid")
    with pytest.raises(submit.Refused, match="effective git committer"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_missing_identity_config_stops(world):
    world["cfg"].git_email = ""
    with pytest.raises(submit.Refused, match="git_email is not set"):
        run(world, execute=True)
    world["cfg"].git_email, world["cfg"].github_user = ME[1], ""
    with pytest.raises(submit.Refused, match="github_user is not set"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_wrong_upstream_clone_stops(world):
    sh("git", "remote", "set-url", "origin", "https://github.com/other/lib.git", cwd=world["clone"])
    with pytest.raises(submit.Refused, match="clone origin is other/lib"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_fork_destination_must_be_own_fork(world):
    # Preflight passes; at push time the fork remote the tool sets must be me/lib. Pre-set a hostile remote
    # name is overwritten, so verify the guard directly on a mismatching remote.
    from ossforge import identity
    sh("git", "remote", "add", "fork", "https://github.com/acme/lib.git", cwd=world["clone"])
    assert identity.check_fork(world["cfg"], str(world["clone"]), "acme/lib")
    sh("git", "remote", "set-url", "fork", "https://github.com/stranger/lib.git", cwd=world["clone"])
    assert "expected me/lib" in identity.check_fork(world["cfg"], str(world["clone"]), "acme/lib")[0]


def test_unverified_item_never_publishes(world):
    world["it"].status = "clean"
    with pytest.raises(submit.Refused, match="must be 'verified'"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_signoff_needs_explicit_per_repo_approval(world):
    sh("git", "commit", "--amend", "-q", "-s", "--no-edit", cwd=world["clone"])
    with pytest.raises(submit.Refused, match="Signed-off-by but sign-off is not approved"):
        run(world, execute=True)
    world["cfg"].signoff_repos = ["acme/lib"]
    assert run(world)["dry_run"]


def test_signoff_added_only_where_approved_and_under_verified_identity(world):
    world["cfg"].signoff_repos = ["acme/lib"]
    run(world, execute=True)
    tip = sh("git", "log", "-1", "--format=%an|%ae|%cn|%ce|%B", "fix/x", cwd=world["fork"])
    assert tip.startswith(f"{ME[0]}|{ME[1]}|{ME[0]}|{ME[1]}") and f"Signed-off-by: {ME[0]} <{ME[1]}>" in tip


def test_repo_that_asks_for_dco_is_refused_until_approved(world, monkeypatch):
    from ossforge import vet
    monkeypatch.setattr(vet, "policy_texts", lambda repo, **kw: {"CONTRIBUTING.md": "All commits need a Signed-off-by (DCO)."})
    with pytest.raises(submit.Refused, match="asks for a DCO"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_no_policy_review_record_blocks(world):
    world["cfg"].state_dir = str(world["tmp"] / ".other")
    with pytest.raises(submit.Refused, match="no policy review recorded"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_prohibited_review_blocks_even_though_recorded(world):
    policy_review.record(world["cfg"], "acme/lib", {"docs": [], "scanned_locations": []}, "bans AI-assisted PRs", prohibited=True)
    with pytest.raises(submit.Refused, match="recorded as prohibited"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_changed_policy_doc_invalidates_review(world, monkeypatch):
    snap = {"docs": [{"path": "CONTRIBUTING.md", "url": "u", "sha": "aaa", "text": "x"}], "scanned_locations": []}
    policy_review.record(world["cfg"], "acme/lib", snap, "read it")
    monkeypatch.setattr(policy_review, "snapshot", lambda r: {"docs": [{"path": "CONTRIBUTING.md", "url": "u", "sha": "bbb", "text": "y"}]})
    with pytest.raises(submit.Refused, match="changed since the review"):
        run(world, execute=True)
    assert writes(world["log"]) == []


def test_source_change_after_verification_is_refused(world):
    clone = world['clone']
    (clone / 'a.py').write_text('x = 999\n')
    sh('git', 'add', '-A', cwd=clone)
    sh('git', 'commit', '-qm', 'fix: altered after testing', cwd=clone)
    with pytest.raises(submit.Refused, match='verification evidence'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_missing_receipt_and_dirty_state_refused(world):
    receipt = world['it'].verification
    world['it'].verification = {}
    with pytest.raises(submit.Refused, match='verification evidence missing'):
        run(world, execute=True)
    world['it'].verification = receipt
    (world['clone'] / 'a.py').write_text('x = 22\n')
    with pytest.raises(submit.Refused, match='work tree is dirty'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_changed_branch_refused(world):
    world['it'].branch = 'untested-branch'
    with pytest.raises(submit.Refused, match='verification evidence'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_message_only_rewrite_keeps_evidence(world):
    sh('git', 'commit', '--amend', '-qm', 'fix: same tested content', cwd=world['clone'])
    assert run(world)['dry_run']


def test_changed_base_invalidates_receipt(world):
    world['it'].verification['base'] = '0' * 40
    with pytest.raises(submit.Refused, match='verification evidence'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_strict_admission_requires_live_consent_and_creates_draft(world, monkeypatch):
    world['cfg'].maintainer_admission = True
    monkeypatch.setattr(admission, 'inspect', lambda *a: dict(source='manifest', max_files=5, max_lines=250,
                                                            allowed_dirs=[]))
    result = run(world, execute=True)
    assert result['pr'].endswith('/12')
    assert '--draft' in writes(world['log'])[-1]


def test_strict_admission_revoked_after_preflight_stops_first_write(world, monkeypatch):
    world['cfg'].maintainer_admission = True
    calls = []
    def inspect(*args):
        calls.append(1)
        if len(calls) >= 3:
            raise admission.Denied('revoked')
        return dict(source='manifest', max_files=5, max_lines=250, allowed_dirs=[])
    monkeypatch.setattr(admission, 'inspect', inspect)
    with pytest.raises(submit.Refused, match='revoked'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_resumed_issue_cannot_bypass_exhausted_pr_cap(world):
    cfg, it, lim, aud, log = (world[k] for k in ("cfg", "it", "lim", "audit", "log"))
    it.submitted = {'issue_url': 'https://github.com/acme/lib/issues/11'}
    lim.record()
    lim.record()
    with pytest.raises(submit.Refused, match='daily PR cap'):
        submit.submit(it, lim, cfg, aud, execute=True)
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    assert not any(c[:2] == ['pr', 'create'] for c in calls)


def test_cap_rechecked_after_partial_writes_before_pr_creation(world):
    def exhausted_after_push(cmd, cwd=None):
        value = original(cmd, cwd)
        if cmd[:2] == ['git', 'push']:
            world['lim'].record()
            world['lim'].record()
        return value
    original = submit._run
    world['mp'].setattr(submit, '_run', exhausted_after_push)
    with pytest.raises(submit.Refused, match='daily PR cap'):
        run(world, execute=True)
    assert not any(c[:2] == ['pr', 'create'] for c in writes(world['log']))


def test_completed_pr_resume_does_not_create_another_pr_at_cap(world):
    first = run(world, execute=True)
    world['it'].submitted = {'issue_url': first['issue'], 'fork_pushed': True, 'pr_url': first['pr']}
    world['lim'].record()
    assert run(world, execute=True)['pr'] == first['pr']
    assert sum(c[:2] == ['pr', 'create'] for c in writes(world['log'])) == 1


def test_direct_submit_same_state_lock_refuses_before_writes(world):
    import fcntl
    path = world['cfg'].path('publication.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(submit.Refused, match='another publication'):
            run(world, execute=True)
    assert writes(world['log']) == []
    assert run(world, execute=True)['pr'].endswith('/12')


def test_stale_direct_submit_reloads_partial_progress_after_interruption(world):
    q = queue.Queue(world['cfg'].path('queue.json'))
    q.add(world['it'])
    q.save()
    original = submit._run
    def interrupted(cmd, cwd=None):
        if cmd[:3] == ['gh', 'pr', 'create']:
            raise submit.Refused('simulated interrupted before PR')
        return original(cmd, cwd)
    world['mp'].setattr(submit, '_run', interrupted)
    with pytest.raises(submit.Refused, match='simulated interrupted'):
        run(world, execute=True)
    saved = queue.Queue(world['cfg'].path('queue.json')).items['a'].submitted
    assert saved['issue_url'] and saved['fork_pushed']
    world['it'].submitted = {}  # a second CLI loaded before the first persisted progress
    world['mp'].setattr(submit, '_run', original)
    assert run(world, execute=True)['pr'].endswith('/12')
    calls = writes(world['log'])
    assert sum(c[:2] == ['issue', 'create'] for c in calls) == 1
    assert sum(c[:2] == ['pr', 'create'] for c in calls) == 1


def test_second_stale_invocation_recovers_completed_urls_without_duplicates(world):
    import copy
    q = queue.Queue(world['cfg'].path('queue.json'))
    q.add(world['it'])
    q.save()
    stale = copy.deepcopy(world['it'])
    first = run(world, execute=True)
    second = submit.submit(stale, world['lim'], world['cfg'], world['audit'], execute=True)
    assert second['issue'] == first['issue'] and second['pr'] == first['pr']
    calls = writes(world['log'])
    assert sum(c[:2] == ['issue', 'create'] for c in calls) == 1
    assert sum(c[:2] == ['pr', 'create'] for c in calls) == 1
    assert world['lim'].remaining() == 1


def test_existing_source_issue_is_reused_not_created(world):
    original = gh.api
    def api(path, /, **kwargs):
        if path == 'repos/acme/lib/issues/42':
            return {'state': 'open', 'html_url': 'https://github.com/acme/lib/issues/42'}
        if path in ('repos/acme/lib/issues/42/comments', 'repos/acme/lib/issues/42/timeline'):
            return []
        return original(path, **kwargs)
    world['mp'].setattr(gh, 'api', api)
    world['it'].source_issue = 42
    result = run(world, execute=True)
    assert result['issue'].endswith('/42')
    calls = writes(world['log'])
    assert not any(c[:2] == ['issue', 'create'] for c in calls)
    assert any('Closes #42' in arg for c in calls for arg in c)


def test_closed_source_issue_stops_before_writes(world):
    original = gh.api
    world['it'].source_issue = 42
    world['mp'].setattr(gh, 'api', lambda path, **kw: {'state': 'closed'} if path.endswith('/issues/42') else original(path, **kw))
    with pytest.raises(submit.Refused, match='source issue is closed'):
        run(world, execute=True)
    assert writes(world['log']) == []


@pytest.mark.parametrize('activity', ['claim', 'draft', 'merged', 'saturated', 'unavailable'])
@pytest.mark.parametrize('existing', [False, True])
def test_source_activity_blocks_competing_work_before_writes(world, activity, existing):
    original = gh.api
    if existing:
        world['it'].submitted = {'issue_url': 'https://github.com/acme/lib/issues/42'}
    else:
        world['it'].source_issue = 42
    def api(path, /, **kw):
        if path.endswith('/issues/42'):
            return {'state': 'open', 'html_url': 'https://github.com/acme/lib/issues/42'}
        if path.endswith('/issues/42/comments'):
            if activity == 'unavailable':
                raise gh.GhError('network failed')
            return [{'body': "I'm working on this"}] if activity == 'claim' else []
        if path.endswith('/issues/42/timeline'):
            if activity == 'saturated':
                return [{}] * 100
            if activity in ('draft', 'merged'):
                return [{'event': 'cross-referenced', 'source': {'issue': {'pull_request': {}, 'draft': True, 'state': 'closed' if activity == 'merged' else 'open'}}}]
            return []
        return original(path, **kw)
    world['mp'].setattr(gh, 'api', api)
    with pytest.raises(submit.Refused):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_new_competitor_between_preflight_and_write_refuses(world):
    original = submit._duplicate_gate
    count = []
    def gate(it, cfg=None):
        count.append(True)
        if len(count) == 2:
            raise submit.Refused('new competing PR found')
        return original(it, cfg)
    world['mp'].setattr(submit, '_duplicate_gate', gate)
    with pytest.raises(submit.Refused, match='new competing'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_resumed_issue_does_not_bypass_duplicate_checks(world):
    from ossforge import dedupe
    world['it'].submitted = {'issue_url': 'https://github.com/acme/lib/issues/11'}
    world['mp'].setattr(dedupe, 'find_matches', lambda *a, **k: [
        dedupe.Match(11, 'own', 'https://github.com/acme/lib/issues/11', 'open', False, 1, 'same'),
        dedupe.Match(13, 'competing', 'https://github.com/acme/lib/pull/13', 'open', True, 1, 'same'),
    ])
    with pytest.raises(submit.Refused, match='duplicate found'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_issue_lead_uses_verified_changed_source_paths_for_duplicate_check(world):
    from ossforge import dedupe
    world['it'].path = ''  # retained issue leads have no static-detector file path
    seen = []
    world['mp'].setattr(dedupe, 'find_matches', lambda repo, title, body, path, *a, **k: (seen.append(path) or []))
    run(world)
    assert seen and all(p == 'a.py' for p in seen)


def test_duplicate_check_covers_each_changed_source_not_just_original_candidate(world):
    from ossforge import dedupe
    it = world['it']
    (world['clone'] / 'other.py').write_text('y = 3\n')
    sh('git', 'add', 'other.py', cwd=world['clone'])
    sh('git', 'commit', '-qm', 'additional source', cwd=world['clone'])
    it.verification = evidence.capture(world['clone'], sh('git', 'merge-base', 'HEAD', 'origin/HEAD', cwd=world['clone']), [['fixture-test']])
    searches, commits = [], []
    world['mp'].setattr(dedupe, 'find_matches', lambda repo, title, body, path, *a, **k: (searches.append(path) or []))
    world['mp'].setattr(dedupe, 'recent_commit_hits', lambda repo, path, *a, **k: (commits.append(path) or []))
    run(world)
    assert set(searches) == set(commits) == {'a.py', 'other.py'}


def test_upstream_advance_requires_refresh_and_reverification_before_writes(world):
    seed = world['tmp'] / 'seed'
    (seed / 'a.py').write_text('x = 99\n')
    sh('git', 'add', 'a.py', cwd=seed)
    sh('git', 'commit', '-qm', 'upstream fix', cwd=seed)
    sh('git', 'push', '-q', 'origin', 'HEAD:main', cwd=seed)
    with pytest.raises(submit.Refused, match='upstream'):
        run(world, execute=True)
    assert writes(world['log']) == []
    assert (world['clone'] / 'a.py').read_text() == 'x = 2\n'


def test_fetch_without_rebase_still_requires_updated_base(world):
    seed = world['tmp'] / 'seed'
    (seed / 'new_upstream.py').write_text('new = True\n')
    sh('git', 'add', '.', cwd=seed)
    sh('git', 'commit', '-qm', 'upstream new file', cwd=seed)
    sh('git', 'push', '-q', 'origin', 'HEAD:main', cwd=seed)
    sh('git', 'fetch', 'origin', cwd=world['clone'])
    with pytest.raises(submit.Refused, match='upstream'):
        run(world, execute=True)
    assert writes(world['log']) == []


@pytest.mark.parametrize('contents', ['{partial\n', '[]\n', 'null\n', '{"event":"pr_opened"}\n', '{"event":"pr_state","repo":"acme/lib","pr":1}\n'])
def test_invalid_audit_history_blocks_before_any_publication_checks(world, contents):
    world['audit'].path.write_text(contents)
    before = world['audit'].path.read_bytes()
    with pytest.raises(submit.Refused, match='audit'):
        run(world, execute=True)
    assert writes(world['log']) == []
    assert world['audit'].path.read_bytes() == before


def test_partial_audit_cannot_erase_valid_open_history(world):
    valid = json.dumps({'event': 'pr_opened', 'repo': 'acme/lib', 'pr': 9, 'ts': 1}) + '\n'
    world['audit'].path.write_text(valid + '{partial\n')
    with pytest.raises(submit.Refused, match='audit'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_new_policy_template_not_hidden_by_cached_snapshot_listing(world, monkeypatch):
    import base64
    path = '.github/PULL_REQUEST_TEMPLATE.md'
    monkeypatch.setattr(gh, 'existing_files', lambda repo, paths, *, fresh=False, strict=False: [path] if fresh and strict else [])
    original = gh.api
    monkeypatch.setattr(gh, 'api', lambda endpoint, **kw: {
        'encoding': 'base64', 'content': base64.b64encode(b'Human input is required.').decode(),
        'sha': 'new-template', 'html_url': 'https://github.com/acme/lib/blob/main/' + path,
    } if endpoint.endswith('/contents/' + path) else original(endpoint, **kw))
    with pytest.raises(submit.Refused, match='changed since the review'):
        run(world, execute=True)
    assert writes(world['log']) == []


@pytest.mark.parametrize('change', ['none', 'candidate', 'text', 'head', 'new-match', 'unavailable', 'commit'])
def test_reviewed_false_positive_bound_to_candidate_and_live_upstream(world, change):
    from ossforge import dedupe, duplicate_reviews
    it, cfg, mp = world['it'], world['cfg'], world['mp']
    state = {'title': 'different concurrency defect', 'head': 'abc'}
    original = gh.api
    def api(endpoint, **kw):
        if endpoint == 'repos/acme/lib/issues/13':
            if state.get('unavailable'):
                raise gh.GhError('unavailable')
            return dict(number=13, title=state['title'], body='unrelated', state='open', updated_at='t',
                        html_url='https://github.com/acme/lib/pull/13', pull_request={})
        if endpoint == 'repos/acme/lib/pulls/13':
            return dict(head={'sha': state['head']}, base={'sha': 'base'}, state='open', updated_at='t')
        return original(endpoint, **kw)
    mp.setattr(gh, 'api', api)
    matches = [dedupe.Match(13, 'unrelated', 'https://github.com/acme/lib/pull/13', 'open', True, .3, 'same file')]
    mp.setattr(dedupe, 'find_matches', lambda *a, **k: matches)
    with pytest.raises(submit.Refused, match='duplicate found'):
        run(world)
    duplicate_reviews.record(cfg.path('duplicate_reviews.json'), it, 13, 'Different cause and API; reviewed diff')
    if change == 'candidate':
        it.issue_body += ' changed'
    elif change == 'text':
        state['title'] += ' new'
    elif change == 'head':
        state['head'] = 'def'
    elif change == 'new-match':
        matches.append(dedupe.Match(14, 'actual duplicate', 'https://github.com/acme/lib/pull/14', 'open', True, 1, 'same'))
    elif change == 'unavailable':
        state['unavailable'] = True
    elif change == 'commit':
        mp.setattr(dedupe, 'recent_commit_hits', lambda *a, **k: ['https://github.com/acme/lib/commit/existing'])
    if change == 'none':
        run(world)
    else:
        with pytest.raises(submit.Refused):
            run(world, execute=True)
        assert writes(world['log']) == []


def test_duplicate_review_requires_verified_candidate_and_reason(world):
    from ossforge import duplicate_reviews
    it, path = world['it'], world['cfg'].path('duplicate_reviews.json')
    with pytest.raises(ValueError):
        duplicate_reviews.record(path, it, 13, '')
    it.status = 'clean'
    with pytest.raises(ValueError):
        duplicate_reviews.record(path, it, 13, 'unrelated')
    assert not path.exists()


def test_false_positive_review_rechecked_before_first_write(world):
    from ossforge import dedupe, duplicate_reviews
    it, cfg, mp = world['it'], world['cfg'], world['mp']
    rev = ['old']
    original = gh.api
    def api(endpoint, **kw):
        if endpoint == 'repos/acme/lib/issues/13':
            return dict(number=13, title='different cause', body=rev[0], state='open', updated_at=rev[0],
                        html_url='https://github.com/acme/lib/issues/13')
        return original(endpoint, **kw)
    mp.setattr(gh, 'api', api)
    mp.setattr(dedupe, 'find_matches', lambda *a, **k: [
        dedupe.Match(13, 'unrelated', 'https://github.com/acme/lib/issues/13', 'open', False, .3, 'same file')])
    duplicate_reviews.record(cfg.path('duplicate_reviews.json'), it, 13, 'Different API and cause')
    real = submit._duplicate_gate
    seen = []
    def gate(item, conf=None):
        seen.append(1)
        if len(seen) == 2:
            rev[0] = 'new work'
        return real(item, conf)
    mp.setattr(submit, '_duplicate_gate', gate)
    with pytest.raises(submit.Refused, match='duplicate found'):
        run(world, execute=True)
    assert len(seen) == 2 and writes(world['log']) == []


@pytest.mark.parametrize('change', ['none', 'candidate', 'new-hit', 'unavailable'])
def test_commit_false_positive_review_is_exact_and_rechecked(world, change):
    from ossforge import dedupe, duplicate_reviews
    it, cfg, mp = world['it'], world['cfg'], world['mp']
    sha = 'a' * 40
    url = f'https://github.com/acme/lib/commit/{sha}'
    original = gh.api
    unavailable = []
    def api(endpoint, **kw):
        if endpoint == f'repos/acme/lib/commits/{sha}':
            if unavailable:
                raise gh.GhError('unavailable')
            return dict(sha=sha, html_url=url, commit={'tree': {'sha': 'tree'}}, parents=[])
        return original(endpoint, **kw)
    mp.setattr(gh, 'api', api)
    hits = [url]
    mp.setattr(dedupe, 'recent_commit_hits', lambda *a, **k: hits)
    with pytest.raises(submit.Refused, match='recent commit'):
        run(world)
    duplicate_reviews.record_commit(cfg.path('duplicate_reviews.json'), it, sha, 'Decoder NUL check, not encoder Unicode')
    if change == 'candidate':
        it.pr_body += ' changed'
    elif change == 'new-hit':
        hits.append('https://github.com/acme/lib/commit/' + 'b' * 40)
    elif change == 'unavailable':
        unavailable.append(1)
    if change == 'none':
        run(world)
    else:
        with pytest.raises(submit.Refused):
            run(world, execute=True)
        assert writes(world['log']) == []


@pytest.mark.parametrize('new_competitor', [False, True])
def test_pr_only_resume_checks_exactly_at_preflight_and_pr_effect(world, new_competitor):
    it, mp = world['it'], world['mp']
    # Local fake gh/bare repos only: prepare persisted issue/fork state without a PR.
    it.submitted = {'issue_url': 'https://github.com/acme/lib/issues/11', 'fork_pushed': True}
    real = submit._duplicate_gate
    seen = []
    def gate(item, cfg=None):
        seen.append(1)
        if new_competitor and len(seen) == 2:
            raise submit.Refused('competitor at PR boundary')
        return real(item, cfg)
    mp.setattr(submit, '_duplicate_gate', gate)
    if new_competitor:
        with pytest.raises(submit.Refused, match='competitor'):
            run(world, execute=True)
        assert writes(world['log']) == []
    else:
        out = run(world, execute=True)
        assert out['pr'] == 'https://github.com/acme/lib/pull/12'
        assert [w[:2] for w in writes(world['log'])] == [['pr', 'create']]
    assert len(seen) == 2


@pytest.mark.parametrize('mode', ['restricted', 'changed', 'unavailable'])
def test_repository_pr_access_rechecked_before_effects(world, mode):
    original = gh.api
    reads = []
    def api(endpoint, **kw):
        if endpoint == 'repos/acme/lib':
            reads.append(1)
            if mode == 'unavailable':
                raise gh.GhError('unavailable')
            if mode == 'restricted' or len(reads) > 1:
                return {'pull_request_creation_policy': 'collaborators_only', 'permissions': {'push': False}}
            return {'pull_request_creation_policy': 'all'}
        return original(endpoint, **kw)
    world['mp'].setattr(gh, 'api', api)
    with pytest.raises(submit.Refused, match='PR|collaborator'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_repository_policy_change_before_pr_preserves_progress_without_pr(world):
    original = gh.api
    reads = []
    def api(endpoint, **kw):
        if endpoint == 'repos/acme/lib':
            reads.append(1)
            if len(reads) >= 4:
                return {'pull_request_creation_policy': 'collaborators_only', 'permissions': {'push': False}}
            return {'pull_request_creation_policy': 'all'}
        return original(endpoint, **kw)
    world['mp'].setattr(gh, 'api', api)
    with pytest.raises(submit.Refused, match='collaborators only'):
        run(world, execute=True)
    observed = writes(world['log'])
    assert any(w[:2] == ['issue', 'create'] for w in observed)
    assert not any(w[:2] == ['pr', 'create'] for w in observed)
    assert world['it'].submitted.get('issue_url')
    assert world['it'].submitted.get('fork_pushed')


def test_explicit_pr_only_target_never_creates_issue_or_close_reference(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    out = run(world, execute=True)
    assert out['issue'] is None
    assert [w[:2] for w in writes(world['log'])] == [['repo', 'fork'], ['pr', 'create']]
    command = writes(world['log'])[-1]
    assert 'Closes #' not in command[command.index('-b') + 1]
    assert world['it'].submitted['publication_mode'] == 'pr_only'
    assert world['it'].submitted['pr_url'] == out['pr']


def test_pr_only_dry_run_exposes_mode_without_effect(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    out = run(world)
    assert out['publication_mode'] == 'pr_only' and out['issue'] is None
    assert writes(world['log']) == []


def test_pr_only_resume_keeps_mode_and_does_not_duplicate_fork(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    first = run(world, execute=True)
    world['it'].submitted = {'publication_mode': 'pr_only', 'fork_pushed': True, 'pr_url': first['pr']}
    world['log'].write_text('')
    assert run(world, execute=True)['pr'] == first['pr']
    assert writes(world['log']) == []


def test_pr_only_progress_cannot_silently_switch_to_issue_creation(world):
    world['it'].submitted = {'publication_mode': 'pr_only', 'fork_pushed': True}
    with pytest.raises(submit.Refused, match='publication mode'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_pr_only_setting_does_not_discard_existing_source_issue(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    world['it'].source_issue = 11
    out = run(world, execute=True)
    assert out['issue'].endswith('/issues/11')
    assert [w[:2] for w in writes(world['log'])] == [['repo', 'fork'], ['pr', 'create']]
    assert 'Closes #11' in writes(world['log'])[-1][-1]


def test_pr_only_cannot_hide_existing_saved_issue(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    world['it'].submitted = {'issue_url': 'https://github.com/acme/lib/issues/11'}
    with pytest.raises(submit.Refused, match='saved issue'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_pr_only_still_blocks_new_competitor_before_any_effect(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    world['mp'].setattr(submit, '_duplicate_gate', lambda *a: (_ for _ in ()).throw(submit.Refused('competitor')))
    with pytest.raises(submit.Refused, match='competitor'):
        run(world, execute=True)
    assert writes(world['log']) == []


def test_pr_only_partial_push_resume_rechecks_and_creates_only_pr(world):
    world['cfg'].pr_only_repos = ['acme/lib']
    world['it'].submitted = {'publication_mode': 'pr_only', 'fork_pushed': True}
    seen = []
    original = submit._duplicate_gate
    def gate(*args):
        seen.append(1)
        return original(*args)
    world['mp'].setattr(submit, '_duplicate_gate', gate)
    result = run(world, execute=True)
    assert result['issue'] is None and result['pr']
    assert [w[:2] for w in writes(world['log'])] == [['pr', 'create']]
    assert len(seen) == 2


def test_reviewed_dynamic_pr_only_route_without_config_allowlist(world):
    policy_review.record(world['cfg'], 'acme/lib', {'docs': [], 'scanned_locations': []},
                         'Complete review permits direct PR only', publication_route='pr_only')
    out = run(world, execute=True)
    assert out['issue'] is None
    assert [w[:2] for w in writes(world['log'])] == [['repo', 'fork'], ['pr', 'create']]
