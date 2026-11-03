"""Native Git fixture: dev is ahead, origin/HEAD intentionally remains main."""
import subprocess
import pytest
from ossforge import submit, evidence
from ossforge.queue import Item


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path/'checkout'
    subprocess.run(['git', 'init', '-b', 'main', str(root)], check=True, capture_output=True)
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    (root/'source.py').write_text('def value():\n    return 0\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'main')
    main = git(root, 'rev-parse', 'HEAD')
    git(root, 'update-ref', 'refs/remotes/origin/main', main)
    git(root, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    git(root, 'switch', '-c', 'dev')
    (root/'source.py').write_text('def value():\n    return 1\n')
    git(root, 'commit', '-am', 'upstream dev change')
    dev = git(root, 'rev-parse', 'HEAD')
    git(root, 'update-ref', 'refs/remotes/origin/dev', dev)
    git(root, 'switch', '-c', 'fix/local')
    return root, main, dev


def test_explicit_base_keeps_upstream_dev_commit_out_of_patch(checkout):
    root, main, dev = checkout
    item = Item('one', 'org/repo', 'source.py', 1, 'bug', 'fixture', clone=str(root))
    item.contribution_base = {'repo':item.repo,'branch':'dev','revision':dev}
    assert submit._item_base(item) == dev
    assert submit._base(str(root)) == main  # legacy/default semantics stay separate
    assert git(root, 'diff', '--name-only', submit._item_base(item)+'..HEAD') == ''


def test_evidence_rejects_same_tip_different_branch(checkout):
    root, main, dev = checkout
    rec = evidence.capture(root, dev, [['python','-m','pytest']], contribution_base={'repo':'org/repo','branch':'dev','revision':dev})
    assert rec['contribution_base']['branch'] == 'dev'
    evidence.check(root, dev, 'fix/local', rec, contribution_base={'repo':'org/repo','branch':'dev','revision':dev})
    with pytest.raises(evidence.StaleEvidence, match='identity'):
        evidence.check(root, dev, 'fix/local', rec, contribution_base={'repo':'org/repo','branch':'other','revision':dev})


def test_exact_base_ref_must_exist_and_be_ancestor(checkout):
    root, main, dev = checkout
    item = Item('one', 'org/repo', 'source.py', 1, 'bug', 'fixture', clone=str(root))
    item.contribution_base = {'repo':item.repo,'branch':'missing','revision':dev}
    with pytest.raises(submit.Refused):
        submit._item_base(item)
    item.contribution_base['branch'] = '--upload-pack=bad'
    with pytest.raises(submit.Refused):
        submit._item_base(item)


def test_existing_public_driver_task_chooses_dev_tip(checkout, tmp_path):
    from ossforge import driver
    from ossforge.queue import Queue
    root, main, dev = checkout
    item = Item('one', 'org/repo', 'source.py', 1, 'bug', 'fixture', clone=str(root), status='clean')
    item.contribution_base = {'repo':item.repo,'branch':'dev','revision':dev}
    queue = Queue(tmp_path/'queue.json')
    queue.add(item)
    # Exercise existing production task implementation, no proposed helper needed.
    task = driver.task(queue)
    assert task['base'] == dev, 'upstream dev commit must not become part of a contribution patch'


@pytest.mark.parametrize('value', [{},None,'dev',[],{'repo':'other/repo','branch':'dev','revision':'a'*40},
                                  {'repo':'org/repo','branch':'dev','revision':'short'},
                                  {'repo':'org/repo','branch':'dev','revision':'a'*40,'extra':True},
                                  {'repo':'org/repo','branch':False,'revision':'a'*40}])
def test_present_malformed_queue_identity_never_legacy(tmp_path,value):
    import json
    from dataclasses import asdict
    from ossforge.queue import Queue, InvalidState
    item=Item('one','org/repo','x.py',1,'bug','fixture')
    row=asdict(item)
    row['contribution_base']=value
    path=tmp_path/'queue.json'
    path.write_text(json.dumps([row]))
    before=path.read_bytes()
    with pytest.raises(InvalidState):
        Queue(path)
    assert path.read_bytes()==before


def test_legacy_queue_roundtrip_omits_key(tmp_path):
    import json
    from ossforge.queue import Queue
    q=Queue(tmp_path/'queue.json')
    q.add(Item('one','org/repo','x.py',1,'bug','fixture'))
    q.save()
    assert 'contribution_base' not in json.loads(q.path.read_text())[0]
    assert Queue(q.path).items['one'].contribution_base is None


def test_local_base_ref_cannot_move_before_first_verification(checkout):
    root, main, dev=checkout
    item=Item('one','org/repo','x.py',1,'bug','fixture',clone=str(root),
              contribution_base={'repo':'org/repo','branch':'dev','revision':dev})
    git(root,'update-ref','refs/remotes/origin/dev',main)
    with pytest.raises(submit.Refused,match='changed'):
        submit._item_base(item)


def test_receipt_downgrade_both_directions(checkout):
    root, main, dev=checkout
    identity={'repo':'org/repo','branch':'dev','revision':dev}
    legacy=evidence.capture(root,dev,[['true']])
    explicit=evidence.capture(root,dev,[['true']],contribution_base=identity)
    with pytest.raises(evidence.StaleEvidence,match='identity'):
        evidence.check(root,dev,'fix/local',legacy,contribution_base=identity)
    with pytest.raises(evidence.StaleEvidence,match='downgrade'):
        evidence.check(root,dev,'fix/local',explicit)


def test_live_gate_exact_branch_not_default(checkout,monkeypatch):
    root, main, dev=checkout
    item=Item('one','org/repo','x.py',1,'bug','fixture',clone=str(root),
              contribution_base={'repo':'org/repo','branch':'dev','revision':dev})
    monkeypatch.setattr(submit.identity,'check_upstream',lambda *a:[])
    original=submit.subprocess.run
    seen=[]
    def run(argv,**kw):
        if argv[:2]==['git','ls-remote']:
            seen.append(argv)
            return subprocess.CompletedProcess(argv,0,dev+'\trefs/heads/dev\n','')
        return original(argv,**kw)
    monkeypatch.setattr(submit.subprocess,'run',run)
    submit._upstream_gate(item)
    assert seen==[['git','ls-remote','origin','refs/heads/dev']]
    def moved(argv,**kw):
        if argv[:2]==['git','ls-remote']:
            return subprocess.CompletedProcess(argv,0,main+'\trefs/heads/dev\n','')
        return original(argv,**kw)
    monkeypatch.setattr(submit.subprocess,'run',moved)
    with pytest.raises(submit.Refused,match='moved'):
        submit._upstream_gate(item)


def test_signoff_selected_range_never_rewrites_upstream(checkout):
    root, main, dev=checkout
    (root/'fix.py').write_text('value=2\n')
    git(root,'add','fix.py')
    git(root,'commit','-m','local fix')
    submit._ensure_signoff(str(root),dev)
    assert git(root,'rev-parse','HEAD^')==dev
    assert 'Signed-off-by:' not in git(root,'show','-s','--format=%B',dev)
    assert 'Signed-off-by:' in git(root,'show','-s','--format=%B','HEAD')


def test_selected_commit_search_uses_pinned_revision(monkeypatch):
    from ossforge import dedupe
    seen=[]
    monkeypatch.setattr(dedupe.gh,'api',lambda endpoint,**kw: (seen.append((endpoint,kw)) or [
        {'html_url':'https://github.com/org/repo/commit/'+('a'*40),'commit':{'message':'fix regression'}}]))
    assert dedupe.recent_commit_hits('org/repo','source.py',['regression'],strict=True,ref='a'*40)
    assert seen[0][1]['sha']=='a'*40


def test_policy_review_pins_docs_at_reviewed_tip_and_refuses_movement(tmp_path,monkeypatch):
    import base64
    from ossforge import policy_review
    from ossforge.config import Config
    tip=['a'*40]
    seen=[]
    def api(endpoint,**kw):
        seen.append((endpoint,kw))
        if '/commits/' in endpoint:
            return {'sha':tip[0]}
        return {'encoding':'base64','content':base64.b64encode(b'Use dev, tests required.').decode(),
                'sha':'doc-sha','html_url':'https://github.com/org/repo/blob/'+tip[0]+'/CONTRIBUTING.md'}
    monkeypatch.setattr(policy_review.gh,'api',api)
    monkeypatch.setattr(policy_review.gh,'existing_files',lambda repo,paths,**kw:(seen.append(('listing',kw)) or ['CONTRIBUTING.md']))
    cfg=Config(state_dir=str(tmp_path))
    snap=policy_review.snapshot('org/repo',ref='dev')
    assert all(kw.get('ref')=='a'*40 for endpoint,kw in seen if '/commits/' not in endpoint)
    rec=policy_review.record(cfg,'org/repo',snap,'Read CONTRIBUTING.md requiring dev',
                             contribution_branch='dev',publication_route='issue_pr')
    assert rec['contribution_base']=={'repo':'org/repo','branch':'dev','revision':'a'*40}
    assert policy_review.check(cfg,'org/repo')==''
    tip[0]='b'*40
    assert 'branch changed' in policy_review.check(cfg,'org/repo')


def test_public_parser_branch_review_requires_accept():
    from ossforge import cli
    a=cli.build_parser().parse_args(['policy-review','org/repo','--accept','--contribution-branch','dev','--note','Read docs'])
    assert a.contribution_branch=='dev' and a.accept


def test_explicit_driver_actual_verify_and_result_downgrade(checkout,tmp_path):
    import sys
    from ossforge import driver
    from ossforge.queue import Queue
    root, main, dev=checkout
    selected={'repo':'org/repo','branch':'dev','revision':dev}
    item=Item('one','org/repo','source.py',1,'bug','fixture',clone=str(root),status='clean',contribution_base=selected)
    q=Queue(tmp_path/'queue.json')
    q.add(item)
    task=driver.task(q)
    assert task['base']==dev and task['contribution_base']==selected
    r=task['result_schema']
    (root/'source.py').write_text('def value():\n    return 2\n')
    (root/'test_value.py').write_text('from source import value\nassert value()==2\n')
    git(root,'add','.')
    git(root,'commit','-m','fix and regression')
    r.update(branch='fix/local',test_command=[sys.executable,'test_value.py'],check_commands=[])
    missing={k:v for k,v in r.items() if k!='contribution_base'}
    with pytest.raises(ValueError,match='identity'):
        driver.complete(q,missing)
    out=driver.complete(q,r)
    assert out['status']=='verified' and out['evidence']['contribution_base']==selected
    assert q.items['one'].verification['base']==dev


@pytest.mark.parametrize('name',['main','dev','ABCD/topic'])
def test_source_review_record_to_public_collection_is_repository_specific(tmp_path,monkeypatch,name):
    import base64
    from ossforge import repo_research, policy_review, radar, acceptance
    from ossforge.config import Config
    selected='a'*40
    seen=[]
    def api(endpoint,**kw):
        seen.append((endpoint,kw))
        if endpoint=='repos/org/repo':
            return {'default_branch':'main','html_url':'https://github.com/org/repo'}
        if '/commits/' in endpoint:
            return {'sha':selected,'html_url':'https://github.com/org/repo/commit/'+selected}
        return {'encoding':'base64','content':base64.b64encode(('All PRs target '+name).encode()).decode(),
                'sha':'doc','html_url':'https://github.com/org/repo/blob/'+selected+'/CONTRIBUTING.md'}
    monkeypatch.setattr(policy_review.gh,'api',api)
    monkeypatch.setattr(policy_review.gh,'existing_files',lambda *a,**kw:['CONTRIBUTING.md'])
    cfg=Config(state_dir=str(tmp_path),repo_first=False)
    snap=policy_review.snapshot('org/repo',ref=name)
    policy_review.record(cfg,'org/repo',snap,'Read CONTRIBUTING.md: exact repository-specific PR base '+name,
                         contribution_branch=name,publication_route='issue_pr')
    monkeypatch.setattr(repo_research.repo_access,'pr_restriction',lambda *a:'')
    monkeypatch.setattr(repo_research.radar,'analyze',lambda r:radar.RepoScore(r,80,'Go'))
    monkeypatch.setattr(repo_research.acceptance,'evidence',lambda r:acceptance.Acceptance(r,'unknown'))
    monkeypatch.setattr(repo_research.repo_suitability,'assess',lambda *a:{'status':'native_candidate'})
    lead=repo_research.collect_selected(cfg,{'repo':'org/repo'},30)
    assert lead['default_branch']=='main'
    assert lead['contribution_base']=={'repo':'org/repo','branch':name,'revision':selected}
    assert lead['policy']['contribution_base']==lead['contribution_base']
    assert all(kw.get('ref')==selected for endpoint,kw in seen if '/contents/' in endpoint)


def test_same_sha_policy_branch_swap_refuses_without_touching_checkout(checkout,tmp_path,monkeypatch):
    from ossforge import policy_review
    from ossforge.config import Config
    root,main,dev=checkout
    item=Item('one','org/repo','source.py',1,'bug','fixture',clone=str(root),
              contribution_base={'repo':'org/repo','branch':'dev','revision':dev})
    cfg=Config(state_dir=str(tmp_path))
    monkeypatch.setattr(policy_review,'_load',lambda c:{'org/repo':{
        'repo':'org/repo','contribution_base':{'repo':'org/repo','branch':'other','revision':dev}}})
    before=git(root,'rev-parse','HEAD')
    with pytest.raises(submit.Refused,match='differs from policy'):
        submit._base_identity_gate(item,cfg)
    assert git(root,'rev-parse','HEAD')==before


@pytest.mark.parametrize('name',['--upload-pack=x','HEAD','refs/heads/dev','dev..main','a b','dev@{1}','a\nb'])
def test_invalid_review_branch_refuses_before_network(monkeypatch,name):
    from ossforge import policy_review
    monkeypatch.setattr(policy_review.gh,'api',lambda *a,**kw:pytest.fail('invalid branch reached network'))
    with pytest.raises(ValueError,match='branch'):
        policy_review.snapshot('org/repo',ref=name)


def test_public_checkpoint_requests_semantic_resolution_not_owner_branch_prompt(tmp_path):
    from ossforge import repo_research
    from ossforge.config import Config
    cfg=Config(state_dir=str(tmp_path))
    repo_research._save(cfg,{'schema':1,'cursor':0,'lead':{'repo':'org/repo','status':'repo_review_needed'}})
    step=repo_research.checkpoint(cfg,['ossforge'])
    assert step['base_resolution']['mode']=='repo-specific source review by the coding agent'
    assert 'recent merged PR' in step['base_resolution']['unresolved']
    assert step['base_resolution']['review_command'][2]=='org/repo'


def test_branch_previous_checkout_expression_is_not_exact_name(checkout,monkeypatch):
    from ossforge import contribution_base
    root,main,dev=checkout
    monkeypatch.chdir(root)
    # Git accepts and expands this expression in a repo with checkout history.
    assert subprocess.run(['git','check-ref-format','--branch','@{-1}'],capture_output=True).returncode==0
    with pytest.raises(ValueError,match='exact'):
        contribution_base.branch('@{-1}')


def test_invalid_explicit_policy_cli_is_controlled_refusal(tmp_path,capsys,monkeypatch):
    from ossforge import cli
    cfg=tmp_path/'config.yaml'
    cfg.write_text('state_dir: '+str(tmp_path/'state')+'\nkill_file: '+str(tmp_path/'STOP')+'\n')
    monkeypatch.setattr(cli.gh,'api',lambda *a,**kw:pytest.fail('invalid branch reached network'))
    assert cli.main(['--config',str(cfg),'policy-review','org/repo','--accept',
                     '--contribution-branch=--bad','--note','read docs'])==1
    assert 'REFUSED:' in capsys.readouterr().err
    assert not (tmp_path/'state/policy_reviews.json').exists()


def test_actual_new_clone_pins_nondefault_branch_without_rewriting_default(checkout,tmp_path,monkeypatch):
    from ossforge import hunt
    from ossforge.config import Config
    root,main,dev=checkout
    cfg=Config(work_dir=str(tmp_path/'work'),git_name='Fixture',git_email='fixture@example.invalid')
    gcfg=tmp_path/'gitconfig'
    gcfg.write_text('[url "'+str(root)+'"]\n insteadOf = https://github.com/org/repo\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(gcfg))
    selected={'repo':'org/repo','branch':'dev','revision':dev}
    new=hunt.clone(cfg,'org/repo',contribution_base=selected)
    assert git(new,'rev-parse','HEAD')==dev
    assert git(new,'symbolic-ref','--short','HEAD')=='dev'
    assert git(new,'rev-parse','refs/remotes/origin/dev')==dev
    assert subprocess.run(['git','symbolic-ref','--quiet','refs/remotes/origin/HEAD'],cwd=new,capture_output=True).returncode==1
    # Mismatched or dirty old checkout is preserved rather than switched/reset.
    (new/'keep.txt').write_text('do not erase')
    with pytest.raises(RuntimeError,match='dirty'):
        hunt.clone(cfg,'org/repo',contribution_base=selected)
    assert (new/'keep.txt').read_text()=='do not erase'


def test_missing_review_branch_does_not_write_policy_record(tmp_path,monkeypatch):
    from ossforge import policy_review
    from ossforge.config import Config
    cfg=Config(state_dir=str(tmp_path))
    def api(*a,**kw):
        raise policy_review.gh.GhError('Not Found',404)
    monkeypatch.setattr(policy_review.gh,'api',api)
    with pytest.raises(policy_review.gh.GhError):
        policy_review.snapshot('org/repo',ref='nonexistent')
    assert not cfg.path('policy_reviews.json').exists()
