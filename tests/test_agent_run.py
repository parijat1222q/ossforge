import json
import subprocess
import sys
from pathlib import Path

import pytest

from ossforge import agent_run, driver
from ossforge.config import Config
from ossforge.queue import Item, Queue


def fixture(tmp_path):
    clone = tmp_path / 'clone'
    clone.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=clone, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (clone / 'calc.py').write_text('def f(n): return n+1\n')
    git('add', '.')
    git('commit', '-m', 'baseline')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    git('switch', '-c', 'fix')
    (clone / 'calc.py').write_text('def f(n): return n*2\n')
    (clone / 'test_calc.py').write_text('from calc import f\nassert f(3)==6\n')
    git('add', '.')
    git('commit', '-m', 'fix and test')
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    q = Queue(cfg.path('queue.json'))
    q.add(Item('a', 'owner/repo', 'calc.py', 1, 'controlled', 'fixture', status='clean', clone=str(clone)))
    q.save()
    script = tmp_path / 'driver.py'
    script.write_text('''import json,os,sys
from pathlib import Path
t=json.loads(Path(os.environ['OSSFORGE_TASK_FILE']).read_text())
r=t['result_schema'];r.update(branch='fix',test_command=[sys.executable,'test_calc.py'],check_commands=[])
Path(os.environ['OSSFORGE_RESULT_FILE']).write_text(json.dumps(r))
''')
    return cfg, [sys.executable, str(script)]


def test_controlled_process_to_actual_verifier_retains_patch_no_publication(tmp_path, monkeypatch):
    cfg, command = fixture(tmp_path)
    monkeypatch.setattr(agent_run.submit, 'submit', lambda *a, **k: pytest.fail('publication disabled'))
    out = agent_run.run(cfg, command, discover=False)
    assert out['status'] == 'verified_not_submitted'
    q = Queue(cfg.path('queue.json'))
    assert q.items['a'].verification['tree'] and q.items['a'].status == 'verified'
    assert json.loads(cfg.path('driver-attempts.json').read_text())['count'] == 1
    # Resume verified without another driver invocation, even at attempt limit.
    assert agent_run.run(cfg, ['missing-driver'], attempts=1, discover=False)['status'] == 'verified_not_submitted'


def test_driver_failure_reserves_attempt_and_blocks_retry(tmp_path):
    cfg, _ = fixture(tmp_path)
    assert agent_run.run(cfg, [sys.executable, '-c', 'raise SystemExit(3)'], discover=False, attempts=1)['status'] == 'driver_failed'
    assert agent_run.run(cfg, ['missing'], discover=False, attempts=1)['status'] == 'attempt_limit'


def test_timeout_consumes_attempt_and_records_outcome(tmp_path):
    cfg, _ = fixture(tmp_path)
    assert agent_run.run(cfg, [sys.executable, '-c', 'import time;time.sleep(30)'], discover=False, timeout=1)['status'] == 'driver_timeout'
    assert json.loads(cfg.path('run.json').read_text())['status'] == 'driver_timeout'
    assert Queue(cfg.path('queue.json')).items['a'].status == 'clean'


def test_explicit_publication_still_uses_existing_refusal_gate(tmp_path, monkeypatch):
    cfg, command = fixture(tmp_path)
    def refuse(*a, **kw):
        assert kw['execute'] is True
        raise agent_run.submit.Refused('policy not approved')
    monkeypatch.setattr(agent_run.submit, 'submit', refuse)
    out = agent_run.run(cfg, command, discover=False, execute=True)
    assert out['status'] == 'publication_refused'
    assert Queue(cfg.path('queue.json')).items['a'].status == 'verified'


def test_invalid_limits_do_not_start_work(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    for args in [dict(timeout=0), dict(timeout=float('nan')), dict(attempts=0), dict(attempts=True)]:
        with pytest.raises(driver.Invalid):
            agent_run.run(cfg, ['driver'], **args)


def test_partial_discovery_without_candidate_is_incomplete_not_no_work(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / 'state'), kill_file=str(tmp_path / 'STOP'))
    def partial(cfg, journal):
        journal.data['outcome'] = 'discovery_budget_exhausted'
        return []
    monkeypatch.setattr(agent_run.loop, '_run_once', partial)
    out = agent_run.run(cfg, ['must-not-start'])
    assert out['status'] == 'discovery_incomplete'
    assert not cfg.path('driver-attempts.json').exists()


def test_dirty_candidate_refuses_before_driver_attempt_or_launch(tmp_path, monkeypatch):
    from ossforge import evidence
    cfg, command = fixture(tmp_path)
    q = Queue(cfg.path('queue.json'))
    (Path(q.items['a'].clone) / 'calc.py').write_text('uncommitted user work\n')
    monkeypatch.setattr(agent_run, 'invoke', lambda *a: pytest.fail('dirty checkout must not launch driver'))
    with pytest.raises(evidence.StaleEvidence, match='dirty'):
        agent_run.run(cfg, command, discover=False)
    assert not cfg.path('driver-attempts.json').exists()


def test_verified_resume_rechecks_receipt_before_reporting_retained_patch(tmp_path, monkeypatch):
    from ossforge import evidence
    cfg, command = fixture(tmp_path)
    assert agent_run.run(cfg, command, discover=False)['status'] == 'verified_not_submitted'
    q = Queue(cfg.path('queue.json'))
    (Path(q.items['a'].clone) / 'calc.py').write_text('uncommitted later change\n')
    monkeypatch.setattr(agent_run, 'invoke', lambda *a: pytest.fail('do not rerun driver over changed work'))
    with pytest.raises(evidence.StaleEvidence, match='dirty'):
        agent_run.run(cfg, ['must-not-start'], discover=False)
    assert (Path(q.items['a'].clone) / 'calc.py').read_text() == 'uncommitted later change\n'


def test_verified_resume_rejects_committed_tree_change(tmp_path):
    from ossforge import evidence
    cfg, command = fixture(tmp_path)
    assert agent_run.run(cfg, command, discover=False)['status'] == 'verified_not_submitted'
    q = Queue(cfg.path('queue.json'))
    clone = Path(q.items['a'].clone)
    (clone / 'calc.py').write_text('def f(n): return 0\n')
    for cmd in (['git', 'add', 'calc.py'], ['git', 'commit', '-qm', 'later unchecked change']):
        subprocess.run(cmd, cwd=clone, check=True, capture_output=True)
    with pytest.raises(evidence.StaleEvidence, match='no longer matches'):
        agent_run.run(cfg, ['must-not-start'], discover=False)


@pytest.mark.parametrize('ledger', [
    {'day': '2099-01-01', 'count': 20}, {'count': 20}, {'day': 'typo', 'count': 20},
    {'day': 'today', 'count': -1}, {'day': 'today', 'count': True},
    {'day': 'today', 'count': '1'}, [], None,
])
def test_invalid_attempt_ledger_preserved_before_any_driver_launch(tmp_path, monkeypatch, ledger):
    from datetime import date
    cfg, command = fixture(tmp_path)
    if isinstance(ledger, dict) and ledger.get('day') == 'today':
        ledger['day'] = date.today().isoformat()
    path = cfg.path('driver-attempts.json')
    path.write_text(json.dumps(ledger))
    before = path.read_bytes()
    monkeypatch.setattr(agent_run, 'invoke', lambda *a: pytest.fail('invalid ledger must not launch driver'))
    with pytest.raises(driver.Invalid, match='ledger|count|date'):
        agent_run.run(cfg, command, discover=False, attempts=1)
    assert path.read_bytes() == before


def test_prior_day_attempt_count_rolls_over(tmp_path):
    from datetime import date, timedelta
    cfg, _ = fixture(tmp_path)
    path = cfg.path('driver-attempts.json')
    path.write_text(json.dumps({'day': (date.today() - timedelta(days=1)).isoformat(), 'count': 20}))
    assert agent_run.run(cfg, ['missing-driver'], discover=False, attempts=1)['status'] == 'driver_error'
    assert json.loads(path.read_text()) == {'day': date.today().isoformat(), 'count': 1}


def parent_commit_fixture(tmp_path,monkeypatch):
    cfg,command = fixture(tmp_path)
    cfg.github_user,cfg.git_name,cfg.git_email = 'test','Test','test@example.invalid'
    q = Queue(cfg.path('queue.json'))
    clone = Path(q.items['a'].clone)
    subprocess.run(['git','remote','add','origin','https://github.com/owner/repo'],cwd=clone,check=True)
    monkeypatch.setattr(agent_run.identity,'check_login',lambda c:[])
    monkeypatch.setattr(agent_run.policy_review,'check',lambda *a:'')
    monkeypatch.setattr(agent_run.policy_review,'publication_route',lambda *a:'issue_pr')
    # Real child edits, creates branch and returns a parent-owned commit request.
    script = Path(command[1])
    script.write_text('''import json,os,sys,subprocess
from pathlib import Path
t=json.loads(Path(os.environ['OSSFORGE_TASK_FILE']).read_text())
subprocess.run(['git','switch','-c','next-fix'],check=True,capture_output=True)
Path('calc.py').write_text('def f(n): return n*3\\n')
Path('test_calc.py').write_text('from calc import f\\nassert f(3)==9\\n')
r=t['result_schema'];r.update(branch='next-fix',test_command=[sys.executable,'test_calc.py'],check_commands=[],commit_message='second real fix',commit_paths=['calc.py','test_calc.py'])
Path(os.environ['OSSFORGE_RESULT_FILE']).write_text(json.dumps(r))
''')
    return cfg,command,clone


def test_actual_child_patch_parent_identity_commit_actual_verification(tmp_path,monkeypatch):
    cfg,command,clone = parent_commit_fixture(tmp_path,monkeypatch)
    before = subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
    out = agent_run.run(cfg,command,discover=False)
    assert out['status'] == 'verified_not_submitted'
    head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
    assert head != before
    journal = json.loads(cfg.path('run.json').read_text())
    assert head.startswith(journal['driver_commit']['head'])
    assert Queue(cfg.path('queue.json')).items['a'].status == 'verified'
    assert subprocess.check_output(['git','status','--porcelain'],cwd=clone,text=True) == ''


@pytest.mark.parametrize('case',['wrong_branch','wrong_base','invalid_message','stop','identity','verify_failure','unrelated'])
def test_parent_commit_refusal_or_failed_verify_preserves_patch(tmp_path,monkeypatch,case):
    cfg,command,clone = parent_commit_fixture(tmp_path,monkeypatch)
    before = subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
    script=Path(command[1])
    text=script.read_text()
    if case == 'wrong_branch':
        text=text.replace("branch='next-fix'","branch='not-current'")
    elif case == 'wrong_base':
        text=text.replace("r=t['result_schema'];r.update", "r=t['result_schema'];r['base']='wrong';r.update")
    elif case == 'invalid_message':
        text=text.replace("commit_message='second real fix'","commit_message=''")
    elif case == 'stop':
        text += f'Path({cfg.kill_file!r}).write_text("stop")\n'
    elif case == 'identity':
        monkeypatch.setattr(agent_run.identity,'check_login',lambda c:['wrong account'])
    elif case == 'verify_failure':
        text=text.replace('assert f(3)==9','assert f(3)==123')
    elif case == 'unrelated':
        text+='Path("unrelated.txt").write_text("keep")\n'
    script.write_text(text)
    if case == 'verify_failure':
        out=agent_run.run(cfg,command,discover=False)
        assert out['status'] == 'verification_failed'
        journal=json.loads(cfg.path('run.json').read_text())
        assert journal['driver_commit']['head']
    else:
        with pytest.raises((driver.Invalid,agent_run.identity.IdentityError,agent_run.safety.Blocked)):
            agent_run.run(cfg,command,discover=False)
        assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip() == before
    assert Queue(cfg.path('queue.json')).items['a'].status == 'clean'
    assert 'return n*3' in (clone/'calc.py').read_text()


def test_parent_commit_failure_preserves_staged_index_and_unpromoted_queue(tmp_path,monkeypatch):
    cfg,command,clone=parent_commit_fixture(tmp_path,monkeypatch)
    # Make git commit itself fail after staging without changing global config.
    hook=clone/'.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o755)
    before=subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
    with pytest.raises(agent_run.identity.IdentityError):
        agent_run.run(cfg,command,discover=False)
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip() == before
    assert set(subprocess.check_output(['git','diff','--cached','--name-only'],cwd=clone,text=True).splitlines()) == {'calc.py','test_calc.py'}
    assert Queue(cfg.path('queue.json')).items['a'].status == 'clean'


def test_restart_after_parent_commit_before_verification_preserves_commit(tmp_path,monkeypatch):
    cfg,command,clone=parent_commit_fixture(tmp_path,monkeypatch)
    real=driver.complete
    monkeypatch.setattr(driver,'complete',lambda *a,**k:(_ for _ in ()).throw(OSError('crash after parent commit')))
    with pytest.raises(OSError,match='crash after'):
        agent_run.run(cfg,command,discover=False)
    journal=json.loads(cfg.path('run.json').read_text())
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
    assert commit.startswith(journal['driver_commit']['head'])
    monkeypatch.setattr(driver,'complete',real)
    # A separately inspected already-committed result uses the original schema.
    result_file=next(cfg.path('driver-jobs').glob('*/result.json'))
    result=json.loads(result_file.read_text())
    result.pop('commit_message')
    result.pop('commit_paths')
    checked=driver.complete(Queue(cfg.path('queue.json')),result)
    assert checked['status'] == 'verified'
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip() == commit


def test_standalone_public_commit_success_remains_supported(tmp_path,monkeypatch,capsys):
    from ossforge import cli
    cfg,command,clone=parent_commit_fixture(tmp_path,monkeypatch)
    (clone/'calc.py').write_text('def f(n): return n*4\n')
    config=tmp_path/'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nkill_file: {cfg.kill_file}\ngithub_user: test\ngit_name: Test\ngit_email: test@example.invalid\n')
    assert cli.main(['--config',str(config),'commit','a','-m','standalone identity checked']) == 0
    assert subprocess.check_output(['git','log','-1','--format=%s'],cwd=clone,text=True).strip() == 'standalone identity checked'


def test_crash_after_git_commit_before_journal_does_not_undo_or_duplicate(tmp_path,monkeypatch):
    cfg,command,clone=parent_commit_fixture(tmp_path,monkeypatch)
    real=agent_run.runs.Run.note
    def crash(self,text):
        if text.startswith('parent identity-checked commit'):
            raise OSError('journal persistence interrupted')
        return real(self,text)
    monkeypatch.setattr(agent_run.runs.Run,'note',crash)
    with pytest.raises(OSError,match='journal persistence'):
        agent_run.run(cfg,command,discover=False)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip()
    assert subprocess.check_output(['git','log','-1','--format=%s'],cwd=clone,text=True).strip() == 'second real fix'
    assert Queue(cfg.path('queue.json')).items['a'].status == 'clean'
    # Explicitly inspect and complete the committed legacy result, never rerun child.
    result=json.loads(next(cfg.path('driver-jobs').glob('*/result.json')).read_text())
    result.pop('commit_message')
    result.pop('commit_paths')
    assert driver.complete(Queue(cfg.path('queue.json')),result)['status'] == 'verified'
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip() == head
