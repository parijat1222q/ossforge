"""Acceptance fixtures for explicit same-source continuation, not fresh search.

These tests first capture the current blocker. They are not implementation or
new contribution proof. Preserve existing submitted source and all search state.
"""
import json
import subprocess

import pytest

from ossforge import repo_research, research_session
from ossforge.config import Config
from ossforge.queue import Queue, Item


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture
def prior_submission(tmp_path, monkeypatch):
    root = tmp_path / 'old-source'
    root.mkdir()
    git(root, 'init', '-qb', 'main')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    (root/'source.py').write_text('value=1\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    before = repo_research.evidence.capture(root, base, [])
    (root/'source.py').write_text('value=2\n')
    git(root, 'commit', '-qam', 'previous submitted fix')
    cfg = Config(repo_first=True,state_dir=str(tmp_path/'state'),work_dir=str(tmp_path/'work'),kill_file=str(tmp_path/'STOP'))
    monkeypatch.setattr(research_session.time,'time',lambda:1000.)
    research_session.start(cfg,60)
    monkeypatch.setattr(research_session.time,'time',lambda:1061.)
    lead = {'repo':'org/repo','status':'investigating','clone':str(root),
            'base':base,'checkout':before,'revision':base,'revision_url':'https://example.invalid/commit',
            'default_branch':'main','research_id':'previous'}
    repo_research._save(cfg,{'schema':1,'cursor':3,'lead':lead,'deferred':[{'repo':'org/other','reason':'preserved'}]})
    q = Queue(cfg.path('queue.json'))
    q.add(Item('old','org/repo','source.py',1,'bug','prior fix',status='submitted',clone=str(root),
               submitted={'issue':'https://example.invalid/issues/1','pr':'https://example.invalid/pull/2'}))
    q.save()
    cfg.path('research_source_review.json').write_text(json.dumps({'context':{'repo':'org/repo'},'reviews':{'keep':'exact bytes'}}))
    files = ['queue.json','research_session.json','repo_research.json','research_source_review.json']
    snapshot = {f:cfg.path(f).read_bytes() for f in files}
    return cfg,root,lead,snapshot


def test_current_proposal_refuses_old_submitted_patch_without_erasing_it(prior_submission):
    cfg,root,lead,before = prior_submission
    head = git(root,'rev-parse','HEAD')
    with pytest.raises(repo_research.Invalid,match='checkout changed'):
        repo_research.propose(cfg,'source.py',1,'a distinct future hypothesis')
    assert git(root,'rev-parse','HEAD') == head != lead['revision']
    assert all(cfg.path(f).read_bytes() == raw for f,raw in before.items())


def test_current_refresh_exhausted_search_refusal_preserves_selected_source(prior_submission,monkeypatch):
    cfg,root,lead,before = prior_submission
    monkeypatch.setattr(repo_research.repo_access,'pr_restriction',lambda *a:pytest.fail('remote read after expired selection allowance'))
    with pytest.raises(research_session.Invalid,match='search_budget_exhausted'):
        repo_research.refresh(cfg)
    assert all(cfg.path(f).read_bytes() == raw for f,raw in before.items())


@pytest.mark.parametrize('args', [['hold','old','--reason','wait'],['unhold','old'],['reject','old'],
                                 ['verify','old','--branch','fix','--issue-title','fixture','--issue-body-file','unused','--pr-title','fixture','--pr-body-file','unused'],['submit','old','--yes'],['agent-verify','unused.json']])
def test_public_direct_writers_refuse_before_context_under_state_lock(prior_submission,tmp_path,monkeypatch,capsys,args):
    from ossforge import cli
    cfg,root,lead,before = prior_submission
    config = tmp_path/'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    monkeypatch.setattr(cli,'_ctx',lambda *a:pytest.fail('writer loaded stale context without exclusion'))
    with repo_research.locked(cfg):
        assert cli.main(['--config',str(config),*args]) == 1
    assert 'another run holds' in capsys.readouterr().err
    assert all(cfg.path(f).read_bytes() == raw for f,raw in before.items())


def test_lock_acquisition_config_drift_never_opens_unlocked_new_state(prior_submission,tmp_path,monkeypatch,capsys):
    from contextlib import contextmanager
    from ossforge import cli
    cfg,root,lead,before = prior_submission
    config = tmp_path/'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    real = repo_research.locked
    seen = []
    @contextmanager
    def drift(c):
        with real(c):
            config.write_text(f'state_dir: {tmp_path}/UNLOCKED\n')
            yield
    monkeypatch.setattr(repo_research,'locked',drift)
    context = cli._ctx
    def inspect(c):
        seen.append(c.state_dir)
        assert c.state_dir == cfg.state_dir
        return context(c)
    monkeypatch.setattr(cli,'_ctx',inspect)
    assert cli.main(['--config',str(config),'hold','old','--reason','wait']) == 1
    assert seen == [cfg.state_dir]
    assert not (tmp_path/'UNLOCKED').exists()


@pytest.fixture
def transition(prior_submission,tmp_path,monkeypatch):
    from ossforge import research_continuation as rc
    cfg,old,lead,before = prior_submission
    cfg.git_name,cfg.git_email,cfg.github_user = 'Test','test@example.invalid','test'
    # This remains an expired original session with only its fixture identity bound.
    session = json.loads(cfg.path('research_session.json').read_text())
    session['config_identity'] = research_session._identity(cfg)
    cfg.path('research_session.json').write_text(json.dumps(session))
    target = {**lead,'repository_identity':{'id':123,'full_name':'org/repo','url':'https://github.com/org/repo'},'policy':{'docs':[]},'holds':[], 'code_suitability':{'status':'native_candidate'}}
    target['revision'] = git(old,'rev-parse','HEAD')
    target['revision_url'] = 'https://example.invalid/new-tip'
    monkeypatch.setattr(rc,'_target',lambda c,lead:dict(target))
    def clone(c,t,d):
        if d.exists():
            return
        d.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','clone',str(old),str(d)],check=True,capture_output=True)
        git(d,'remote','set-url','origin','https://github.com/org/repo')
        git(d,'config','user.name','Test')
        git(d,'config','user.email','test@example.invalid')
    monkeypatch.setattr(rc,'_clone',clone)
    config = tmp_path/'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\nrepo_first: true\ngit_name: Test\ngit_email: test@example.invalid\ngithub_user: test\n')
    return rc,cfg,old,lead,target,config


def test_public_expired_same_source_continuation_preserves_old_state_and_retry(transition,monkeypatch,capsys):
    from ossforge import cli
    rc,cfg,old,lead,target,config = transition
    old_head = git(old,'rev-parse','HEAD')
    files = ['queue.json','research_session.json','research_source_review.json']
    before = {f:cfg.path(f).read_bytes() for f in files}
    state = repo_research.load(cfg)
    assert cli.main(['--config',str(config),'research-repo','--another-investigation','one']) == 0
    outcome = json.loads(capsys.readouterr().out)
    new = repo_research.load(cfg)
    assert new['cursor'] == state['cursor'] and new['deferred'] == state['deferred']
    assert new['lead']['research_id'] != lead['research_id']
    assert new['lead']['clone'] != str(old)
    assert git(old,'rev-parse','HEAD') == old_head
    assert git(new['lead']['clone'],'rev-parse','HEAD') == target['revision']
    assert all(cfg.path(f).read_bytes() == raw for f,raw in before.items())
    import base64
    archive = new['continuation_outcomes']['one']
    assert archive['old_lead'] == lead
    assert base64.b64decode(archive['old_review']) == before['research_source_review.json']
    assert 'continuation_pending' not in new
    monkeypatch.setattr(rc,'_target',lambda *a:pytest.fail('completed retry read live source'))
    monkeypatch.setattr(rc.uuid,'uuid4',lambda:pytest.fail('completed retry allocated identity'))
    assert cli.main(['--config',str(config),'research-repo','--another-investigation','one']) == 0
    assert json.loads(capsys.readouterr().out) == outcome


@pytest.mark.parametrize('phase',['planned','clone_started','clone_validated','commit'])
def test_crash_boundary_retry_has_one_archive_one_destination(transition,monkeypatch,phase):
    rc,cfg,old,lead,target,config = transition
    real_save = repo_research._save
    tripped = []
    def crash(c,data):
        real_save(c,data)
        p = data.get('continuation_pending')
        at = p['phase'] if p else 'commit'
        if at == phase and not tripped:
            tripped.append(True)
            raise OSError('simulated crash after durable replace')
    monkeypatch.setattr(repo_research,'_save',crash)
    with pytest.raises(OSError,match='simulated crash'):
        rc.another(cfg,'one')
    state = repo_research.load(cfg)
    if phase != 'commit':
        assert state['lead'] == lead
        dest = state['continuation_pending']['destination']
    else:
        dest = state['lead']['clone']
    monkeypatch.setattr(repo_research,'_save',real_save)
    out = rc.another(cfg,'one')
    assert out['clone'] == dest
    final = repo_research.load(cfg)
    assert list(final['continuation_outcomes']) == ['one']
    assert final['continuation_outcomes']['one']['old_lead'] == lead


def test_partial_timeout_pending_visible_blocks_writers_and_conflicting_token(transition,monkeypatch,capsys):
    from ossforge import cli, agent_next
    rc,cfg,old,lead,target,config = transition
    def partial(c,t,d):
        d.mkdir(parents=True)
        (d/'partial').write_text('keep')
        raise rc.Invalid('clone timeout')
    monkeypatch.setattr(rc,'_clone',partial)
    assert cli.main(['--config',str(config),'research-repo','--another-investigation','one']) == 1
    p = rc.pending(cfg)
    assert repo_research.load(cfg)['lead'] == lead
    dest = p['destination']
    out = agent_next.next_step(cfg,Queue(cfg.path('queue.json')),str(config))
    assert out['status'] == 'research_continuation_pending' and out['old_lead_authoritative']
    assert cli.main(['--config',str(config),'research-repo','--refresh']) == 1
    with pytest.raises(rc.Invalid,match='another or changed'):
        rc.another(cfg,'two')
    with pytest.raises(rc.Invalid,match='another or changed'):
        cfg.github_user = 'different'
        rc.another(cfg,'one')
    assert (rc.Path(dest)/'partial').read_text() == 'keep'
    assert repo_research.load(cfg)['lead'] == lead


@pytest.mark.parametrize('status',['submitted','rejected','duplicate','anti_ai'])
def test_partial_publication_terminal_records_refuse_before_source_reads(transition,monkeypatch,status):
    rc,cfg,old,lead,target,config = transition
    q = Queue(cfg.path('queue.json'))
    q.set('old',status=status,submitted={'issue_url':'https://example.invalid/issues/1'})
    q.save()
    monkeypatch.setattr(rc,'_target',lambda *a:pytest.fail('read before partial-publication refusal'))
    with pytest.raises(rc.Invalid,match='partial publication'):
        rc.another(cfg,'one')
    assert not cfg.path('repo_research.json').read_text().find('continuation_pending') >= 0


@pytest.mark.parametrize('status',['found','clean','verified','held'])
def test_same_source_active_work_refuses(transition,status):
    rc,cfg,old,lead,target,config = transition
    q = Queue(cfg.path('queue.json'))
    q.add(Item('active','org/repo','source.py',1,'bug','active',status=status))
    q.save()
    with pytest.raises(rc.Invalid,match='active work'):
        rc.another(cfg,'one')


def test_stop_during_clone_preserves_old_and_partial_then_exact_retry(transition,monkeypatch):
    rc,cfg,old,lead,target,config = transition
    clone = rc._clone
    def stop(c,t,d):
        clone(c,t,d)
        rc.Path(cfg.kill_file).write_text('stop')
    monkeypatch.setattr(rc,'_clone',stop)
    with pytest.raises(rc.safety.Blocked):
        rc.another(cfg,'one')
    p = rc.pending(cfg)
    assert repo_research.load(cfg)['lead'] == lead
    rc.Path(cfg.kill_file).unlink()
    monkeypatch.setattr(rc,'_clone',clone)
    assert rc.another(cfg,'one')['clone'] == p['destination']


@pytest.mark.parametrize('mutation',['queue','tip','policy','branch','review','session','research','symlink','dirty','remote','head','identity','root'])
def test_changes_during_clone_never_promote_or_destroy(transition,monkeypatch,mutation):
    rc,cfg,old,lead,target,config = transition
    clone = rc._clone
    destinations = []
    def change(c,t,d):
        clone(c,t,d)
        destinations.append(d)
        if mutation == 'queue':
            q = Queue(cfg.path('queue.json'))
            q.add(Item('drift','other/repo','x',1,'bug','keep'))
            q.save()
        elif mutation in {'tip','policy','branch'}:
            key = {'tip':'revision','policy':'policy','branch':'default_branch'}[mutation]
            target[key] = {'docs':[{'changed':True}]} if mutation == 'policy' else 'changed'
        elif mutation == 'review':
            cfg.path('research_source_review.json').write_text('{"changed":true}')
        elif mutation == 'session':
            session = json.loads(cfg.path('research_session.json').read_text())
            session['budget_seconds']=1
            cfg.path('research_session.json').write_text(json.dumps(session))
        elif mutation == 'research':
            state=repo_research.load(cfg)
            state['cursor']+=1
            repo_research._save(cfg,state)
        elif mutation == 'symlink':
            (d/'.git').rename(d/'original-git')
            (d/'.git').symlink_to(d/'original-git',target_is_directory=True)
        elif mutation == 'dirty':
            (d/'source.py').write_text('uncommitted work')
        elif mutation == 'remote':
            git(d,'remote','set-url','origin','https://github.com/wrong/repo')
        elif mutation == 'head':
            git(d,'checkout','--detach')
        elif mutation == 'identity':
            git(d,'config','user.name','Wrong')
        elif mutation == 'root':
            import shutil
            shutil.rmtree(d/'.git')
    monkeypatch.setattr(rc,'_clone',change)
    with pytest.raises((rc.Invalid,rc.evidence.StaleEvidence,research_session.Invalid)):
        rc.another(cfg,'one')
    state = repo_research.load(cfg)
    assert state['lead'] == lead and 'continuation_pending' in state
    assert len(destinations) == 1 and destinations[0].exists()
    assert git(old,'rev-parse','HEAD') == target['revision'] if mutation != 'tip' else git(old,'rev-parse','HEAD') != 'changed'


def test_environment_status_reloads_queue_after_lock_acquisition(prior_submission,tmp_path,monkeypatch,capsys):
    from contextlib import contextmanager
    from ossforge import cli
    cfg,root,lead,before = prior_submission
    config = tmp_path/'cfg.yaml'
    config.write_text(f'state_dir: {cfg.state_dir}\nwork_dir: {cfg.work_dir}\nkill_file: {cfg.kill_file}\n')
    real=repo_research.locked
    @contextmanager
    def intervene(c):
        with real(c):
            queue=Queue(c.path('queue.json'))
            queue.set('old',detail='new current queue')
            queue.save()
            yield
    monkeypatch.setattr(repo_research,'locked',intervene)
    def status(c,q,*args):
        assert q.items['old'].detail == 'new current queue'
        return {'status':'environment_prepared'}
    monkeypatch.setattr(cli.environment,'status',status)
    assert cli.main(['--config',str(config),'environment-status','old','unused']) == 0


def test_same_sha_review_context_is_stale_after_new_research_identity(transition,monkeypatch):
    from ossforge import issue_reviews
    rc,cfg,old,lead,target,config=transition
    # Same SHA is intentional, but separate independent source and new identity.
    lead['revision']=target['revision']
    lead['base']=target['revision']
    lead['checkout']=repo_research.evidence.capture(old,lead['base'],[])
    state=repo_research.load(cfg)
    state['lead']=lead
    repo_research._save(cfg,state)
    context={'repo':lead['repo'],'research_id':lead['research_id'],'base':lead['base'],'checkout':lead['checkout']}
    import hashlib
    review={'schema':1,'context':context,'fingerprint':hashlib.sha256(json.dumps(context,sort_keys=True).encode()).hexdigest(),'reviews':{},'hits':{}}
    issue_reviews._save(cfg.path('research_source_review.json'),review)
    rc.another(cfg,'same-sha')
    out=repo_research.checkpoint(cfg,['ossforge'])
    assert out['lead']['source_review']['status'] == 'stale'
    with pytest.raises(rc.Invalid,match='matching active'):
        repo_research.review_source(cfg,'123','old context cannot clear new work')


@pytest.mark.parametrize('damage',['digest','destination','phase','request','token'])
def test_corrupt_pending_refuses_and_preserves(transition,monkeypatch,damage):
    rc,cfg,old,lead,target,config=transition
    monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
    with pytest.raises(rc.Invalid,match='pause'):
        rc.another(cfg,'one')
    state=repo_research.load(cfg)
    p=state['continuation_pending']
    p[{'digest':'fingerprint'}.get(damage,damage)]='bad'
    repo_research._save(cfg,state)
    raw=cfg.path('repo_research.json').read_bytes()
    with pytest.raises(rc.Invalid):
        rc.another(cfg,'one')
    assert cfg.path('repo_research.json').read_bytes() == raw


def test_publication_exclusion_refuses_continuation_before_live_reads(transition,monkeypatch,capsys):
    from ossforge import cli, native_replay
    rc,cfg,old,lead,target,config=transition
    monkeypatch.setattr(rc,'_target',lambda *a:pytest.fail('must refuse publication contention first'))
    with native_replay.publication_locked(cfg):
        assert cli.main(['--config',str(config),'research-repo','--another-investigation','one']) == 1
    assert 'publication' in capsys.readouterr().err
    assert not repo_research.load(cfg).get('continuation_pending')


def test_real_submit_excluded_by_transition_publication_lock(transition,monkeypatch):
    from ossforge import native_replay, submit, safety
    from ossforge.limiter import DailyLimiter
    rc,cfg,old,lead,target,config=transition
    item=Queue(cfg.path('queue.json')).items['old']
    item.status='verified'
    monkeypatch.setattr(submit,'_submit',lambda *a:pytest.fail('publication must be excluded'))
    with repo_research.locked(cfg), native_replay.publication_locked(cfg):
        with pytest.raises(submit.Refused,match='another publication'):
            submit.submit(item,DailyLimiter(cfg.path('daily.json'),cfg.cap),cfg,safety.Audit(cfg.path('audit.jsonl')),execute=True)


def test_failure_before_atomic_commit_retains_pending_old_pointer_then_retry(transition,monkeypatch):
    rc,cfg,old,lead,target,config=transition
    real=repo_research._save
    def fail_before(c,state):
        if 'continuation_pending' not in state:
            raise OSError('before commit')
        real(c,state)
    monkeypatch.setattr(repo_research,'_save',fail_before)
    with pytest.raises(OSError,match='before commit'):
        rc.another(cfg,'one')
    assert repo_research.load(cfg)['lead'] == lead
    assert rc.pending(cfg)['phase'] == 'clone_validated'
    monkeypatch.setattr(repo_research,'_save',real)
    assert rc.another(cfg,'one')['status'] == 'repo_investigation'


def test_default_config_missing_is_not_reclassified_as_explicit(tmp_path,monkeypatch,capsys):
    from ossforge import cli
    monkeypatch.chdir(tmp_path)
    # Run the genuine dispatch and writer wrapper but avoid digest integration.
    monkeypatch.setattr(cli.digest,'render',lambda *a,**k:'controlled digest')
    monkeypatch.setattr(cli.patrol,'patrol',lambda *a:[])
    monkeypatch.setattr(cli.gh,'api',lambda *a,**k:pytest.fail('default-config dispatch fixture must not use live GitHub credentials'))
    result=cli.main(['digest'])
    assert result == 0
    captured=capsys.readouterr()
    assert captured.out.strip() == 'controlled digest'
    assert 'explicit config file unavailable' not in captured.err


def test_actual_narrow_collector_never_calls_health_or_acceptance(prior_submission,monkeypatch):
    from ossforge import research_continuation as rc
    cfg,old,lead,before=prior_submission
    git(old,'remote','add','origin','https://github.com/org/repo')
    monkeypatch.setattr(repo_research.radar,'analyze',lambda *a:pytest.fail('no rescoring'))
    monkeypatch.setattr(repo_research.acceptance,'evidence',lambda *a:pytest.fail('no outside acceptance resampling'))
    monkeypatch.setattr(rc.repo_access,'pr_restriction',lambda *a:None)
    def api(path,**kwargs):
        if path == 'repos/org/repo':
            return {'id':123,'full_name':'org/repo','html_url':'https://github.com/org/repo','default_branch':'main'}
        if path == 'repos/org/repo/commits/main':
            return {'sha':lead['revision'],'html_url':f"https://github.com/org/repo/commit/{lead['revision']}"}
        pytest.fail(path)
    monkeypatch.setattr(rc.gh,'api',api)
    monkeypatch.setattr(rc.policy_review,'_load',lambda *a:{'org/repo':{'publication_route':'issue_pr'}})
    snap={'repo':'org/repo','docs':[],'anti_ai':False,'agent_review':False}
    monkeypatch.setattr(rc.policy_review,'snapshot',lambda *a,**k:snap)
    monkeypatch.setattr(rc.policy_review,'check',lambda *a:'')
    monkeypatch.setattr(rc.repo_suitability,'assess',lambda *a:{'status':'native_candidate'})
    out=rc._target(cfg,lead)
    assert out['repository_identity']['id'] == 123
    assert 'not a historical certification' in out['repository_identity']['provenance']
    bad={**lead,'repository_identity':{'id':999}}
    with pytest.raises(rc.Invalid,match='stable repository identity changed'):
        rc._target(cfg,bad)


@pytest.mark.parametrize('section,key,value',[
    ('pending','old_lead',[]),('pending','target',[]),('pending','inputs',[]),
    ('pending','request',[]),('pending','old_review',123),('pending','old_review_sha256','bad'),
    ('history','outcome',[]),('history','old_lead',None),('history','target',[]),
    ('history','request',None),('history','old_review','not base64'),('history','fingerprint','bad')])
def test_nested_malformed_records_controlled_refusal_before_remote_read(transition,monkeypatch,section,key,value):
    rc,cfg,old,lead,target,config=transition
    if section == 'pending':
        monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
        with pytest.raises(rc.Invalid,match='pause'):
            rc.another(cfg,'one')
    else:
        rc.another(cfg,'one')
    state=repo_research.load(cfg)
    obj=state['continuation_pending'] if section == 'pending' else state['continuation_outcomes']['one']
    obj[key]=value
    repo_research._save(cfg,state)
    raw=cfg.path('repo_research.json').read_bytes()
    monkeypatch.setattr(rc,'_target',lambda *a:pytest.fail('corrupt state caused remote read'))
    with pytest.raises(rc.Invalid):
        rc.another(cfg,'one')
    assert cfg.path('repo_research.json').read_bytes() == raw


def test_save_refuses_symlink_state_ancestors_without_following(tmp_path):
    target=tmp_path/'real'
    target.mkdir()
    link=tmp_path/'linked'
    link.symlink_to(target,target_is_directory=True)
    cfg=Config(state_dir=str(link))
    with pytest.raises(repo_research.Invalid,match='symlink'):
        repo_research._save(cfg,{'schema':1,'cursor':0,'lead':None})
    assert list(target.iterdir()) == []


def test_previous_predictable_tmp_symlink_cannot_overwrite_victim(tmp_path):
    cfg=Config(state_dir=str(tmp_path/'state'))
    cfg.path('repo_research.tmp').parent.mkdir()
    victim=tmp_path/'victim'
    victim.write_text('keep')
    cfg.path('repo_research.tmp').symlink_to(victim)
    repo_research._save(cfg,{'schema':1,'cursor':0,'lead':None})
    assert victim.read_text() == 'keep'


def test_unexpected_planned_destination_not_adopted(transition,monkeypatch):
    rc,cfg,old,lead,target,config=transition
    real=repo_research._save
    def create_unexpected(c,state):
        real(c,state)
        p=state.get('continuation_pending')
        if p and p['phase'] == 'planned':
            rc.Path(p['destination']).mkdir(parents=True,exist_ok=True)
            (rc.Path(p['destination'])/'unrelated').write_text('keep')
    monkeypatch.setattr(repo_research,'_save',create_unexpected)
    with pytest.raises(rc.Invalid,match='unexpected destination'):
        rc.another(cfg,'one')
    p=rc.pending(cfg)
    assert (rc.Path(p['destination'])/'unrelated').read_text() == 'keep'


@pytest.mark.parametrize('drift',['tip','queue','review','lead'])
def test_abandon_archives_original_and_observed_without_restoring_drift(transition,monkeypatch,capsys,drift):
    from ossforge import cli
    rc,cfg,old,lead,target,config=transition
    clone=rc._clone
    def pause(c,t,d):
        clone(c,t,d)
        raise rc.Invalid('pause after clone')
    monkeypatch.setattr(rc,'_clone',pause)
    with pytest.raises(rc.Invalid,match='pause'):
        rc.another(cfg,'one')
    original=repo_research.load(cfg)['continuation_pending']
    if drift == 'tip':
        target['revision']='f'*40
    elif drift == 'queue':
        q=Queue(cfg.path('queue.json'))
        q.add(Item('newactive','org/repo','source.py',1,'bug','preserve',status='held'))
        q.save()
    elif drift == 'review':
        cfg.path('research_source_review.json').write_text('{"current":"preserve"}')
    elif drift == 'lead':
        data=repo_research.load(cfg)
        data['lead']['current_note']='preserve'
        repo_research._save(cfg,data)
    before={f:cfg.path(f).read_bytes() for f in ['queue.json','research_session.json','research_source_review.json']}
    currentlead=repo_research.load(cfg)['lead']
    assert cli.main(['--config',str(config),'research-repo','--abandon-investigation','one','--reason','inspected changed source, preserve partial clone']) == 0
    out=json.loads(capsys.readouterr().out)
    assert out['status'] == 'research_continuation_abandoned'
    data=repo_research.load(cfg)
    assert data['lead'] == currentlead
    assert 'continuation_pending' not in data
    archive=data['continuation_abandoned']['one']
    assert archive['pending'] == original
    assert archive['observed']['lead'] == currentlead
    assert all(cfg.path(f).read_bytes() == raw for f,raw in before.items())
    assert rc.Path(original['destination']).is_dir()
    monkeypatch.setattr(rc,'_target',lambda *a:pytest.fail('abandoned retry made remote read'))
    assert rc.another(cfg,'one') == out
    assert rc.abandon(cfg,'one','inspected changed source, preserve partial clone') == out
    if drift == 'queue':
        with pytest.raises(rc.Invalid,match='active work'):
            rc.another(cfg,'two')


def test_abandon_stopped_and_lock_contention_preserves_pending(transition,monkeypatch,capsys):
    from ossforge import cli
    rc,cfg,old,lead,target,config=transition
    monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
    with pytest.raises(rc.Invalid):
        rc.another(cfg,'one')
    raw=cfg.path('repo_research.json').read_bytes()
    with repo_research.locked(cfg):
        assert cli.main(['--config',str(config),'research-repo','--abandon-investigation','one','--reason','preserve']) == 1
    rc.Path(cfg.kill_file).write_text('stop')
    assert cli.main(['--config',str(config),'research-repo','--abandon-investigation','one','--reason','preserve']) == 1
    assert cfg.path('repo_research.json').read_bytes() == raw


def test_reconcile_current_exact_run_does_not_acknowledge_changed_journal(transition,monkeypatch):
    rc,cfg,old,lead,target,config=transition
    # Use unchanged clean source for this local inspection assertion fixture.
    lead['base']=target['revision']
    lead['checkout']=repo_research.evidence.capture(old,lead['base'],[])
    data=repo_research.load(cfg)
    data['lead']=lead
    repo_research._save(cfg,data)
    journal={'schema':1,'id':'prior','status':'failed','events':[{'at':1,'message':'run started'}]}
    cfg.path('run.json').write_text(json.dumps(journal))
    with pytest.raises(rc.Invalid,match='unresolved current'):
        rc._preconditions(cfg,lead)
    raw=cfg.path('run.json').read_bytes()
    out=rc.reconcile_run(cfg,'inspected failed read-only stage, no child launched')
    assert out['status'] == 'current_run_inspection_recorded'
    assert cfg.path('run.json').read_bytes() == raw
    rc._preconditions(cfg,lead)
    journal['events'].append({'at':2,'message':'changed later'})
    cfg.path('run.json').write_text(json.dumps(journal))
    with pytest.raises(rc.Invalid,match='unresolved current'):
        rc._preconditions(cfg,lead)


@pytest.mark.parametrize('status,events',[('running',[]),('driver_timeout',[{'message':'driver: starting old'}]),('failed',[{'message':'driver: starting old'}])])
def test_reconcile_cannot_certify_unresolved_driver_lifetime(transition,status,events):
    rc,cfg,old,lead,target,config=transition
    cfg.path('run.json').write_text(json.dumps({'schema':1,'id':'prior','status':status,'events':events}))
    with pytest.raises(rc.Invalid,match='lifetime'):
        rc.reconcile_run(cfg,'not enough to certify termination')
    assert not repo_research.load(cfg).get('continuation_run_review')


def test_production_clone_and_validation_with_local_transport_rewrite(prior_submission,tmp_path,monkeypatch):
    from ossforge import research_continuation as rc
    cfg,old,lead,before=prior_submission
    cfg.github_user,cfg.git_name,cfg.git_email='test','Test','test@example.invalid'
    # The actual clone command runs unchanged. A test-only Git URL rewrite uses
    # this controlled local repository, never a network clone.
    gitconfig=tmp_path/'test-gitconfig'
    gitconfig.write_text(f'[url "file://{old}"]\n insteadOf = https://github.com/org/repo\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(gitconfig))
    monkeypatch.setattr(rc.identity,'check_login',lambda c:[])
    target={**lead,'revision':git(old,'rev-parse','HEAD')}
    dest=tmp_path/'independent'
    rc._clone(cfg,target,dest)
    receipt=rc._validate_clone(cfg,target,dest,lead)
    assert receipt['base'] == target['revision']
    assert git(dest,'config','--get','remote.origin.url') == 'https://github.com/org/repo'
    assert (dest/'.git').is_dir()
    (dest/'.git/objects/info/alternates').write_text(str(old/'.git/objects'))
    with pytest.raises(rc.Invalid,match='shared object'):
        rc._validate_clone(cfg,target,dest,lead)


@pytest.mark.parametrize('boundary',['fsync','replace'])
def test_atomic_save_internal_failure_preserves_previous_durable_pointer(tmp_path,monkeypatch,boundary):
    cfg=Config(state_dir=str(tmp_path/'state'))
    initial={'schema':1,'cursor':0,'lead':None}
    repo_research._save(cfg,initial)
    raw=cfg.path('repo_research.json').read_bytes()
    if boundary == 'fsync':
        monkeypatch.setattr(repo_research.os,'fsync',lambda *a:(_ for _ in ()).throw(OSError('fsync failure')))
    else:
        monkeypatch.setattr(repo_research.Path,'replace',lambda *a:(_ for _ in ()).throw(OSError('rename failure')))
    with pytest.raises(OSError):
        repo_research._save(cfg,{'schema':1,'cursor':1,'lead':None})
    assert cfg.path('repo_research.json').read_bytes() == raw


@pytest.mark.parametrize('runner',['agent','loop'])
def test_direct_library_runner_pending_refuses_before_any_journal_write(transition,monkeypatch,runner):
    from ossforge import agent_run, loop
    rc,cfg,old,lead,target,config=transition
    monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
    with pytest.raises(rc.Invalid):
        rc.another(cfg,'one')
    cfg.path('run.json').write_text('{"schema":1,"id":"prior","status":"completed","events":[]}')
    before={str(p):p.read_bytes() for p in cfg.path('').rglob('*') if p.is_file() and not p.name.endswith('.lock')}
    with pytest.raises(rc.Invalid,match='pending'):
        if runner == 'agent':
            agent_run.run(cfg,['must-not-launch'],discover=False)
        else:
            loop.run_once(cfg)
    after={str(p):p.read_bytes() for p in cfg.path('').rglob('*') if p.is_file() and not p.name.endswith('.lock')}
    assert after == before


@pytest.mark.parametrize('damage',['missing_lead','lead_status','pending_phase','queue_repo','queue_pr','repository_identity'])
def test_actual_cli_malformed_state_refuses_preserves_bytes(transition,monkeypatch,capsys,damage):
    from ossforge import cli
    rc,cfg,old,lead,target,config=transition
    if damage == 'pending_phase':
        monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
        with pytest.raises(rc.Invalid):
            rc.another(cfg,'one')
    data=repo_research.load(cfg)
    if damage == 'missing_lead':
        del data['lead']
    elif damage == 'lead_status':
        data['lead']['status']=[]
    elif damage == 'pending_phase':
        data['continuation_pending']['phase']=[]
    elif damage == 'repository_identity':
        data['lead']['repository_identity']=[]
        # Restore actual collector for this type-check path, stub no network.
        monkeypatch.undo()
        monkeypatch.setattr(rc.repo_access,'pr_restriction',lambda *a:None)
        monkeypatch.setattr(rc.gh,'api',lambda path,**k:{'id':123,'full_name':'org/repo','html_url':'https://github.com/org/repo','default_branch':'main'} if path == 'repos/org/repo' else {'sha':lead['revision'],'html_url':f"https://github.com/org/repo/commit/{lead['revision']}"})
    elif damage.startswith('queue'):
        q=Queue(cfg.path('queue.json'))
        if damage == 'queue_repo':
            q.items['old'].repo=[]
        else:
            q.items['old'].submitted={'pr':[]}
        q.save()
    repo_research._save(cfg,data)
    before={f:cfg.path(f).read_bytes() for f in ['queue.json','repo_research.json','research_session.json','research_source_review.json']}
    assert cli.main(['--config',str(config),'research-repo','--another-investigation','one']) == 1
    assert 'REFUSED:' in capsys.readouterr().err
    assert all(cfg.path(f).read_bytes() == raw for f,raw in before.items())


def test_overlapping_transition_token_states_refuse(transition):
    rc,cfg,old,lead,target,config=transition
    rc.another(cfg,'one')
    data=repo_research.load(cfg)
    data['continuation_pending']={'token':'one'}
    repo_research._save(cfg,data)
    with pytest.raises(rc.Invalid,match='overlaps'):
        rc.another(cfg,'one')


@pytest.mark.parametrize('runner',['agent-run','loop'])
def test_cli_pending_created_between_initial_check_and_runner_lock(transition,monkeypatch,capsys,runner):
    from ossforge import cli, runs
    rc,cfg,old,lead,target,config=transition
    real_enter=runs.Run.__enter__
    entered=[]
    def race(self):
        if not entered:
            entered.append(True)
            clone=rc._clone
            monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
            with pytest.raises(rc.Invalid):
                rc.another(cfg,'race')
            monkeypatch.setattr(rc,'_clone',clone)
        return real_enter(self)
    monkeypatch.setattr(runs.Run,'__enter__',race)
    args=['agent-run','--command','must-not-launch','--skip-discovery'] if runner == 'agent-run' else ['loop','--once']
    assert cli.main(['--config',str(config),*args]) == 1
    assert not cfg.path('run.json').exists()
    assert rc.pending(cfg)['token'] == 'race'


@pytest.mark.parametrize('archive',['abandoned_pending','abandoned_observed','ack','ack_history'])
def test_archived_nested_malformed_data_refuses_even_with_recomputed_outer_hash(transition,monkeypatch,archive):
    rc,cfg,old,lead,target,config=transition
    if archive.startswith('abandoned'):
        monkeypatch.setattr(rc,'_clone',lambda *a:(_ for _ in ()).throw(rc.Invalid('pause')))
        with pytest.raises(rc.Invalid):
            rc.another(cfg,'one')
        rc.abandon(cfg,'one','preserve')
        data=repo_research.load(cfg)
        record=data['continuation_abandoned']['one']
        if archive == 'abandoned_pending':
            record['pending']['old_lead']=[]
        else:
            record['observed']['inputs']={'bad':'not base64'}
        record['fingerprint']=rc._digest({k:v for k,v in record.items() if k != 'fingerprint'})
    else:
        data=repo_research.load(cfg)
        record={'schema':1,'raw':'not base64','digest':'x','config_identity':'x','reason':'keep'}
        record['fingerprint']=rc._digest(record)
        data['continuation_run_review']=record if archive == 'ack' else None
        data['continuation_run_review_history']=[record] if archive == 'ack_history' else []
    repo_research._save(cfg,data)
    raw=cfg.path('repo_research.json').read_bytes()
    with pytest.raises(rc.Invalid):
        rc.another(cfg,'fresh')
    assert cfg.path('repo_research.json').read_bytes() == raw


def test_deadline_expiring_in_final_local_clone_validation_never_promotes(transition,monkeypatch):
    rc,cfg,old,lead,target,config=transition
    clock=[10.]
    monkeypatch.setattr(rc.gh.time,'monotonic',lambda:clock[0])
    real=rc._validate_clone
    calls=[]
    def expire(c,t,d,lead):
        result=real(c,t,d,lead)
        calls.append(True)
        if len(calls) == 2:
            clock[0]+=cfg.discovery_read_budget_seconds+1
        return result
    monkeypatch.setattr(rc,'_validate_clone',expire)
    with pytest.raises(rc.gh.ReadBudgetExceeded):
        rc.another(cfg,'one')
    data=repo_research.load(cfg)
    assert data['lead'] == lead
    assert data['continuation_pending']['phase'] == 'clone_validated'
    assert not data.get('continuation_outcomes')


def test_post_rename_directory_fsync_failure_visible_committed_retry_authoritative(transition,monkeypatch):
    import os
    import stat
    rc,cfg,old,lead,target,config=transition
    real=repo_research.os.fsync
    failed=[]
    def fsync(fd):
        is_dir=stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_dir and not failed:
            visible=repo_research.load(cfg)
            if visible.get('continuation_outcomes', {}).get('one'):
                failed.append(True)
                raise OSError('directory fsync failed after commit replacement')
        return real(fd)
    monkeypatch.setattr(repo_research.os,'fsync',fsync)
    with pytest.raises(OSError,match='directory fsync'):
        rc.another(cfg,'one')
    visible=repo_research.load(cfg)
    assert failed and 'continuation_pending' not in visible
    outcome=visible['continuation_outcomes']['one']['outcome']
    monkeypatch.setattr(rc,'_target',lambda *a:pytest.fail('committed retry read remote'))
    monkeypatch.setattr(rc.uuid,'uuid4',lambda:pytest.fail('committed retry allocated clone'))
    assert rc.another(cfg,'one') == outcome
