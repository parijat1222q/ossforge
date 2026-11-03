"""Durable repo-first research leads, independent of the issue-search cursor.

Selection only collects sources; begin may clone a policy-reviewed repo and
propose records an unverified hypothesis. No patch execution or publication.
The IDE owns semantic diagnosis. Health and merge samples are not permission.
"""
from __future__ import annotations

import json
import uuid
import hashlib
import fcntl
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone

from pathlib import Path
from urllib.parse import quote

from . import duplicate_reviews, issue_reviews, preparation, acceptance, audit, dedupe, discovery, evidence, gh, hunt, policy_review, radar, repo_access, safety, submit, vet
from .queue import Item, Queue, item_id
from .config import Config
from . import research_session, repo_suitability


class Invalid(ValueError):
    pass


@contextmanager
def locked(cfg: Config):
    """Share the existing state-run exclusion without rewriting its journal."""
    if any(p.is_symlink() for p in (cfg.path('run.lock'), *cfg.path('run.lock').parents)):
        raise Invalid('symlink state lock path; preserve it')
    cfg.path('run.lock').parent.mkdir(parents=True, exist_ok=True)
    with cfg.path('run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Invalid('another run holds this state directory; preserve pending work') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load(cfg: Config) -> dict:
    if any(p.is_symlink() for p in (cfg.path('repo_research.json'), *cfg.path('repo_research.json').parents)):
        raise Invalid('symlink research state path; preserve it')
    try:
        data = json.loads(cfg.path('repo_research.json').read_text())
    except FileNotFoundError:
        return {'schema': 1, 'cursor': 0, 'lead': None}
    except (OSError, ValueError) as exc:
        raise Invalid('repo research state unreadable; preserve it before retry') from exc
    if (not isinstance(data, dict) or data.get('schema') != 1
            or type(data.get('cursor')) is not int or data['cursor'] < 0
            or 'lead' not in data
            or (data.get('lead') is not None and not isinstance(data['lead'], dict))):
        raise Invalid('repo research state invalid; preserve it before retry')
    lead = data['lead']
    if lead is not None and (not isinstance(lead.get('repo'), str)
                             or not discovery._REPO.fullmatch(lead.get('repo', ''))
                             or lead.get('status') not in ('collecting', 'repo_review_needed', 'investigating')):
        raise Invalid('repo research lead invalid; preserve it before retry')
    if lead and lead['status'] == 'investigating' and any(not lead.get(k) for k in
            ('clone', 'base', 'checkout', 'revision', 'revision_url', 'default_branch')):
        raise Invalid('investigation state incomplete; preserve it before retry')
    return data


def _save(cfg: Config, data: dict) -> None:
    path = cfg.path('repo_research.json')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Invalid('symlink research state path; preserve it')
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    tmp = Path(name)
    with os.fdopen(fd, 'w') as out:
        out.write(json.dumps(data, indent=2))
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _selection_scope(cfg: Config) -> str:
    return hashlib.sha256(json.dumps({'languages': cfg.languages, 'min_stars': cfg.min_stars,
        'min_repo_score': cfg.min_repo_score, 'preferred_repos': cfg.preferred_repos}, sort_keys=True).encode()).hexdigest()


def prepare(cfg: Config) -> dict:
    """One bounded repository selection and source collection, no issue query."""
    safety.kill_switch(cfg)
    data = load(cfg)
    if not data['lead'] and data.get('exhausted', {}).get('scope') == _selection_scope(cfg):
        return data['exhausted']
    if data['lead'] is not None:
        return data['lead']  # explicit refresh/defer, never silently replace a lead
    if len(cfg.languages) != 1:
        raise Invalid('repo-first research requires exactly one configured language')
    with gh.read_budget(research_session.allowance(cfg) if cfg.repo_first else cfg.discovery_read_budget_seconds):
        pool = discovery.discover_broad_repos(cfg, cfg.languages[0], data.get('window', data['cursor'] // 5))
        if not pool:
            raise Invalid('repository pool unavailable; cursor unchanged, not proof of no work')
        scope = _selection_scope(cfg)
        rejected = {d['repo'].lower() for d in data.get('deferred', [])
                    if d.get('scope', scope) == scope}
        ordered = pool[data['cursor'] % len(pool):] + pool[:data['cursor'] % len(pool)]
        repo = next((r for r in ordered if r.lower() not in rejected), None)
        if repo is None:
            data['exhausted'] = {'status': 'repo_pool_exhausted', 'scope': scope, 'pool': pool,
                                 'reason': 'all repositories in the current bounded search window were deferred; not global exhaustion. Inspect reasons before explicitly searching another window'}
            _save(cfg, data)
            return data['exhausted']
        data.pop('exhausted', None)
        # Persist identity before potentially slow evidence collection. A partial
        # read is a recoverable research lead, never a completed suitability pass.
        lead = {'repo': repo, 'research_id': uuid.uuid4().hex, 'status': 'collecting', 'pool': pool,
                'reason': 'source collection incomplete; refresh before assessment'}
        data['lead'] = lead
        _save(cfg, data)
    return refresh(cfg)


def next_window(cfg: Config) -> dict:
    """Explicit continuation within the same budget, never reset rejections."""
    safety.kill_switch(cfg)
    research_session.allowance(cfg)
    data = load(cfg)
    if data['lead'] or not data.get('exhausted'):
        raise Invalid('finish or defer the current lead and exhaust its window before advancing')
    data['window'] = data.get('window', data['cursor'] // 5) + 1
    data.pop('exhausted', None)
    _save(cfg, data)
    return prepare(cfg)


def refresh(cfg: Config) -> dict:
    safety.kill_switch(cfg)
    data = load(cfg)
    lead = data['lead']
    if not lead:
        raise Invalid('no selected repository; run research-repo first')
    result = collect_selected(cfg, lead, research_session.allowance(cfg) if cfg.repo_first else cfg.discovery_read_budget_seconds)
    data['lead'] = result
    _save(cfg, data)
    return result


def collect_selected(cfg: Config, lead: dict, seconds: float) -> dict:
    """Collect only this already selected source, without changing search state."""
    repo = lead['repo']
    with gh.read_budget(seconds):
        restriction = repo_access.pr_restriction(repo)
        health = radar.analyze(repo)
        outside = acceptance.evidence(repo)
        recorded = policy_review._load(cfg).get(repo.lower(), {})
        extras = recorded.get('extra_paths', [])
        snapshot = policy_review.snapshot(repo, extra_paths=extras) if extras else policy_review.snapshot(repo)
        info = gh.api(f'repos/{repo}', fresh=True)
        branch = info.get('default_branch')
        if not branch or not info.get('html_url'):
            raise Invalid('default branch identity unavailable; preserve lead')
        tip = gh.api(f'repos/{repo}/commits/{branch}', fresh=True)
        if not tip.get('sha') or not tip.get('html_url'):
            raise Invalid('default branch revision unavailable; preserve lead')
        suitability = repo_suitability.assess(repo, tip['sha'], snapshot)
    reasons = []
    if restriction:
        reasons.append(restriction)
    if health.score < cfg.min_repo_score:
        reasons.append('repository health below configured threshold')
    if snapshot['anti_ai']:
        reasons.append('literal contribution prohibition; do not target')
    if snapshot['agent_review']:
        reasons.append('agent-specific contribution restriction needs review')
    result = {**lead, 'status': 'repo_review_needed', 'url': info['html_url'],
              'default_branch': branch, 'revision': tip['sha'], 'revision_url': tip['html_url'],
              'health': asdict(health), 'outside_contributions': asdict(outside),
              'policy': snapshot, 'code_suitability': suitability, 'holds': reasons,
              'requirements': ['read full policy documents and linked contribution guides',
                               'merge samples and stars are not permission or bug proof',
                               'check feasibility of native reproduction before selecting work',
                               'IDE AI must inspect code, diagnose and reproduce a useful bug',
                               'search issues, all-state PRs and recent commits before a patch',
                               'issue age alone never clears claims, assignment or policy',
                               'no code handoff or publication approval is created here']}
    return result


def defer(cfg: Config, reason: str) -> dict:
    if not reason.strip():
        raise Invalid('a source-backed reason is required')
    data = load(cfg)
    if not data['lead']:
        raise Invalid('no selected repository to defer')
    history = data.setdefault('deferred', [])
    history.append({'repo': data['lead']['repo'], 'reason': reason,
                    'revision': data['lead'].get('revision'), 'scope': _selection_scope(cfg),
                    'freshness': 'until explicit selection-scope change; not a permanent claim about upstream'})
    data.update(cursor=data['cursor'] + 1, lead=None)
    _save(cfg, data)
    return {'status': 'deferred', 'reason': reason, 'cursor': data['cursor']}


def review_native(cfg: Config, source_path: str, guide_path: str, command: list[str], note: str = "") -> dict:
    """Source-backed feasibility review for nonstandard layouts, not a test pass."""
    safety.kill_switch(cfg)
    paths = (source_path, guide_path)
    for path in paths:
        if (not path or path != path.strip() or path.startswith('/') or
                any(p in ('', '.', '..') for p in path.split('/')) or
                any(c in path for c in '*?#[\\%') or any(ord(c) < 32 for c in path)):
            raise Invalid('native review paths must be exact files inside the selected repository')
    if source_path == guide_path:
        raise Invalid('cite separate implementation source and native test instructions')
    if Path(source_path).suffix.lower() not in {'.py', '.rs', '.go', '.js', '.ts', '.c', '.h', '.cpp', '.java', '.rb', '.cs', '.sh'}:
        raise Invalid('implementation citation needs a code-file path, not another documentation file; this is not semantic proof')
    if not note.strip() or len(note) > 2000:
        raise Invalid('describe the cited implementation, native test route and remaining feasibility uncertainty')
    if not command or any(not v or '\x00' in v for v in command):
        raise Invalid('a concrete native test command argv is required')
    lead = refresh(cfg)
    sources = []
    with gh.read_budget(research_session.allowance(cfg) if cfg.repo_first else cfg.discovery_read_budget_seconds):
        for path in paths:
            content = gh.api(f"repos/{lead['repo']}/contents/{quote(path, safe='/')}", ref=lead['revision'], fresh=True)
            if (not isinstance(content, dict) or content.get('type') != 'file' or
                    content.get('encoding') != 'base64' or
                    not content.get('sha') or not content.get('html_url')):
                raise Invalid('native review must cite readable source and test-guide files')
            import base64
            try:
                text = base64.b64decode(content.get('content', ''), validate=False).decode('utf-8')
            except (ValueError, UnicodeError) as exc:
                raise Invalid('native citation is not readable text') from exc
            if not text or len(text) > 200000:
                raise Invalid('native review file unavailable or too large for bounded review')
            sources.append({'path': path, 'sha': content['sha'], 'url': content['html_url']})
    data = load(cfg)
    data['lead']['native_review'] = {'revision': lead['revision'], 'sources': sources,
                                    'test_command': command, 'note': note.strip(),
                                    'suitability_status': lead['code_suitability']['status'],
                                    'warning': 'source-qualified review assertion; semantic feasibility uncertain, not proof tests ran or permission'}
    _save(cfg, data)
    return data['lead']['native_review']


def begin(cfg: Config) -> dict:
    """Policy-reviewed, source-pinned clean checkout for IDE investigation.

    No finding, queue item, patch or permission is manufactured by this step.
    """
    lead = refresh(cfg)
    prep_review = policy_review.preparation_review(cfg, lead['repo'], lead['policy'])
    prep = bool(prep_review and (lead['policy']['anti_ai'] or lead['policy'].get('agent_review') or prep_review.get('prohibited')))
    blocking = [h for h in lead['holds'] if not (prep and h in {'literal contribution prohibition; do not target', 'agent-specific contribution restriction needs review'})]
    if blocking:
        raise Invalid('; '.join(blocking))
    review = lead.get('native_review', {})
    reviewed = (review.get('revision') == lead['revision'] and bool(review.get('note'))
                and bool(review.get('test_command')) and len(review.get('sources', [])) == 2
                and all(s.get('sha') and s.get('url') for s in review.get('sources', []))
                and review.get('suitability_status') == lead['code_suitability']['status'])
    if lead['code_suitability']['status'] != 'native_candidate' and not reviewed:
        raise Invalid('native code suitability unresolved: ' + lead['code_suitability']['reason'])
    if prep:
        return preparation.begin(cfg, lead, prep_review)
    why = policy_review.check(cfg, lead['repo'], lead['policy'])
    if why:
        raise Invalid(why)
    try:
        route = policy_review.publication_route(cfg, lead['repo'])
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    if route not in {'issue_pr', 'pr_only'}:
        raise Invalid('contribution discussion/permission prerequisite unresolved; no coding investigation')
    history = safety.Audit(cfg.path('audit.jsonl')).read()
    blocked = safety.repo_allowed(cfg, lead['repo'], safety.effective_history(history))
    if blocked:
        raise Invalid(blocked)
    try:
        clone = hunt.clone(cfg, lead['repo'])
    except (RuntimeError, OSError) as exc:
        raise Invalid(f'checkout unavailable; preserve it and inspect: {exc}') from exc
    try:
        tip = submit._run(['git', 'rev-parse', 'HEAD'], str(clone))
    except submit.Refused as exc:
        raise Invalid(f'checkout has no readable HEAD; preserve it and inspect: {exc}') from exc
    if tip != lead['revision']:
        raise Invalid('checkout differs from current selected revision; preserve it and inspect, never reset')
    try:
        snapshot = evidence.capture(clone, tip, [])
    except evidence.StaleEvidence as exc:
        raise Invalid(f'checkout changed; preserve it and inspect: {exc}') from exc
    data = load(cfg)
    # Deterministic local hints come first. They save broad source reads, but
    # empty output is not proof that the repo has no defects. No AI is invoked.
    findings = audit.scan_tree(clone)
    hints = [asdict(f) | {'snippet': hunt.snippet(clone, f.path, f.line, 2)} for f in findings[:8]]
    data['lead'] = {**lead, 'research_id': lead.get('research_id') or uuid.uuid4().hex, 'status': 'investigating', 'clone': str(clone.resolve()),
                    'base': tip, 'checkout': snapshot, 'hints': hints,
                    'scan': {'method': 'builtin Python AST candidate scan', 'found': len(findings),
                             'shown': len(hints), 'coverage': 'finite detectors, not semantic bug completeness'},
                    'next': 'review compact deterministic hints first; use targeted IDE reasoning for unresolved diagnosis, not a whole-repo dump'}
    _save(cfg, data)
    return data['lead']



def _coding_route(cfg: Config, repo: str) -> None:
    """Fresh source-reviewed prerequisite; not owner publication authority."""
    try:
        route = policy_review.publication_route(cfg, repo)
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    if route not in {'issue_pr', 'pr_only'}:
        raise Invalid('contribution discussion/permission prerequisite unresolved; no coding investigation')

def propose(cfg: Config, path: str, line: int, detail: str, source_issue: int = 0) -> dict:
    """An IDE's source-reviewed hypothesis, not a verified bug or publication.

    Optional issue adoption uses fresh vetting and a minimum 48-hour age.
    Age or textual similarity is never an exemption from other work/policy gates.
    """
    safety.kill_switch(cfg)
    research_session.validate_existing(cfg)
    if type(source_issue) is not int or source_issue < 0:
        raise Invalid('source issue must be a nonnegative integer')
    data = load(cfg)
    lead = data['lead']
    if not lead or lead['status'] != 'investigating':
        raise Invalid('begin a policy-reviewed repo investigation before proposing code work')
    if not detail.strip() or len(detail) > 4000 or '\x00' in detail or line < 1:
        raise Invalid('a specific bounded candidate description and positive line are required')
    rel = Path(path)
    root = Path(lead['clone'])
    preparation.public_guard(root)
    file = root / rel
    if (rel.is_absolute() or '..' in rel.parts or not file.is_file()
            or file.is_symlink() or not file.resolve().is_relative_to(root.resolve())):
        raise Invalid('candidate path must be a real file inside the selected checkout')
    if line > len(file.read_text(errors='replace').splitlines()):
        raise Invalid('candidate line is outside the selected file')
    try:
        current = evidence.capture(root, lead['base'], [])
    except evidence.StaleEvidence as exc:
        raise Invalid(f'investigation checkout changed; preserve it and inspect: {exc}') from exc
    if any(current[k] != lead['checkout'][k] for k in ('tree', 'branch', 'base')):
        raise Invalid('investigation checkout changed; preserve it and refresh before proposing')
    why = policy_review.check(cfg, lead['repo'])
    if why:
        raise Invalid(why)
    _coding_route(cfg, lead['repo'])
    blocked = safety.repo_allowed(cfg, lead['repo'], safety.effective_history(safety.Audit(cfg.path('audit.jsonl')).read()))
    if blocked:
        raise Invalid(blocked)
    with gh.read_budget(cfg.discovery_read_budget_seconds):
        restriction = repo_access.pr_restriction(lead['repo'])
        if restriction:
            raise Invalid(restriction)
        tip = gh.api(f"repos/{lead['repo']}/commits/{lead['default_branch']}", fresh=True)
        if tip.get('sha') != lead['revision']:
            raise Invalid('upstream changed during investigation; preserve checkout and refresh before proposing')
        source = None
        if source_issue:
            source = gh.api(f"repos/{lead['repo']}/issues/{source_issue}", fresh=True)
            if not isinstance(source, dict) or source.get('state') != 'open' or 'pull_request' in source:
                raise Invalid('source issue is not a confirmed open issue')
            try:
                if not isinstance(source.get('created_at'), str):
                    raise ValueError('missing creation timestamp')
                created = datetime.fromisoformat(source['created_at'].replace('Z', '+00:00'))
                if created.tzinfo is None:
                    raise ValueError('creation timestamp has no timezone')
                age = (datetime.now(timezone.utc) - created).total_seconds()
            except (KeyError, ValueError, TypeError) as exc:
                raise Invalid('source issue creation time unavailable') from exc
            if age < 48 * 3600:
                raise Invalid('source issue is younger than 48 hours; preserve investigation without claiming it')
            verdict = vet.vet_issue(lead['repo'], source_issue, repo_ok=True)
            if verdict.decision != 'approve':
                raise Invalid('source issue not eligible: ' + '; '.join(verdict.reasons))
            try:
                submit._source_work_gate(Item('adoption-review', lead['repo'], path, line,
                                             'issue', detail, source_issue=source_issue))
            except submit.Refused as exc:
                raise Invalid(f'source issue activity needs review: {exc}') from exc
        matches = dedupe.find_matches(lead['repo'], detail, detail, path, [], strict=True)
        if source_issue:
            if not any(m.number == source_issue and not m.is_pr for m in matches):
                raise Invalid('source issue does not match candidate search evidence; inspect semantic relevance')
            if source.get('html_url') != f"https://github.com/{lead['repo']}/issues/{source_issue}":
                raise Invalid('source issue URL identity mismatch')
            matches = [m for m in matches if m.number != source_issue or m.is_pr]
        commits = dedupe.recent_commit_hits(lead['repo'], path, dedupe.evidence_terms(detail, detail), strict=True)
        hits = {}
        for match in matches:
            hits[str(match.number)] = {'url': match.url, 'title': match.title,
                                      'snapshot': _source_snapshot(lead['repo'], match.number)}
        for url in commits:
            prefix = f"https://github.com/{lead['repo']}/commit/"
            if not url.startswith(prefix):
                raise Invalid('commit search returned a mismatched repository URL')
            sha = url[len(prefix):]
            hits[sha] = {'url': url, 'snapshot': duplicate_reviews.commit_snapshot(lead['repo'], sha)}
    # Pre-coding review is separate from verified publication duplicate reviews.
    # Every retry repeats live search, policy, tip and clean-checkout checks above.
    context = {'repo': lead['repo'], 'research_id': lead.get('research_id', ''), 'base': lead['base'], 'checkout': current,
               'path': path, 'line': line, 'detail': detail, 'source_issue': source_issue}
    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    prior = issue_reviews.load(cfg.path('research_source_review.json'))
    reviews = prior.get('reviews', {}) if prior.get('fingerprint') == fingerprint else {}
    pending = {'schema': 1, 'fingerprint': fingerprint, 'context': context,
               'hits': hits, 'reviews': reviews}
    issue_reviews._save(cfg.path('research_source_review.json'), pending)
    unresolved = [key for key, hit in hits.items()
                  if reviews.get(key, {}).get('snapshot') != hit['snapshot']
                  or not reviews.get(key, {}).get('reason')]
    if unresolved:
        raise Invalid('possible existing work needs source review before coding: ' +
                      ', '.join(hits[key]['url'] for key in unresolved) +
                      '; inspect each source, then research-review SOURCE --reason REASON; '
                      'retry the exact research-propose; no publication clearance')
    queue = Queue(cfg.path('queue.json'))
    id_ = f"{lead['repo']}#{source_issue}:issue" if source_issue else item_id(lead['repo'], path, 'ide-hypothesis', line)
    if id_ in queue.items:
        raise Invalid('candidate already exists; inspect its current state instead of replacing it')
    item = Item(id_, lead['repo'], path, line, 'issue' if source_issue else 'ide-hypothesis', detail.strip(),
                status='clean', clone=str(root), notes=['IDE source-reviewed hypothesis, not bug proof',
                                                     lead['revision_url']] + ([source['html_url']] if source else []),
                source_issue=source_issue, research_id=lead.get('research_id', ''), snippet=hunt.snippet(root, path, line))
    queue.add(item)
    queue.save()
    return {'status': 'candidate', 'id': id_, 'warning': 'must check current issues/PRs/commits and reproduce before editing; submit independently rechecks duplicates'}


def _source_snapshot(repo: str, number: int) -> dict:
    """Include editable discussion, not merely an issue's comment count."""
    snapshot = duplicate_reviews.snapshot(repo, number)
    comments = gh.api(f'repos/{repo}/issues/{number}/comments', per_page=100, fresh=True)
    if (not isinstance(comments, list) or len(comments) >= 100
            or type(snapshot.get('comments')) is not int or snapshot['comments'] != len(comments)):
        raise Invalid('source comment coverage incomplete; inspect before source review')
    fields = ('id', 'node_id', 'html_url', 'body', 'created_at', 'updated_at', 'user',
              'author_association', 'performed_via_github_app')
    if any(not isinstance(c, dict) or not c.get('id') or not c.get('html_url')
           or not isinstance(c.get('body'), str) or not c.get('created_at')
           or not c.get('updated_at') or not isinstance(c.get('user'), dict)
           or not c['user'].get('id') or not c['user'].get('login') for c in comments):
        raise Invalid('source comment identity or content incomplete')
    snapshot['discussion'] = [{k: c.get(k) for k in fields} for c in comments]
    # Bracket discussion reads so changing count/body/PR head cannot be missed.
    if duplicate_reviews.snapshot(repo, number) != {k: v for k, v in snapshot.items() if k != 'discussion'}:
        raise Invalid('source changed during discussion read; repeat proposal')
    return snapshot


def review_source(cfg: Config, source: str, reason: str) -> dict:
    """Record an inspected unrelated source for one exact clean hypothesis only."""
    safety.kill_switch(cfg)
    research_session.validate_existing(cfg)
    data = load(cfg)
    lead = data['lead']
    pending = issue_reviews.load(cfg.path('research_source_review.json'))
    context = pending.get('context', {})
    if hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest() != pending.get('fingerprint'):
        raise Invalid('saved hypothesis fingerprint changed; repeat research-propose')
    if not lead or lead['status'] != 'investigating' or (context.get('repo') != lead['repo'] or context.get('research_id', '') != lead.get('research_id', '')):
        raise Invalid('no matching active investigation source review')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise Invalid('provide a bounded source-specific unrelated-work reason')
    hit = pending.get('hits', {}).get(source)
    if not hit:
        raise Invalid('source is not a pending exact issue number or full commit SHA')
    preparation.public_guard(Path(lead['clone']))
    try:
        current = evidence.capture(Path(lead['clone']), lead['base'], [])
    except evidence.StaleEvidence as exc:
        raise Invalid(f'checkout changed; preserve work and repeat proposal after recovery: {exc}') from exc
    if current != context.get('checkout') or lead['base'] != context.get('base'):
        raise Invalid('checkout changed; preserve work and propose again before source review')
    with gh.read_budget(cfg.discovery_read_budget_seconds):
        why = policy_review.check(cfg, lead['repo'])
        if why:
            raise Invalid(why)
        _coding_route(cfg, lead['repo'])
        if gh.api(f"repos/{lead['repo']}/commits/{lead['default_branch']}", fresh=True).get('sha') != lead['revision']:
            raise Invalid('upstream changed; refresh investigation before source review')
        snapshot = (_source_snapshot(lead['repo'], int(source)) if source.isdecimal()
                    else duplicate_reviews.commit_snapshot(lead['repo'], source))
    if snapshot != hit['snapshot']:
        raise Invalid('source changed; repeat research-propose and inspect its current evidence')
    pending.setdefault('reviews', {})[source] = {'snapshot': snapshot, 'reason': reason.strip()}
    issue_reviews._save(cfg.path('research_source_review.json'), pending)
    return {'status': 'research_source_review_recorded', 'source': hit['url'],
            'warning': 'exact pre-coding hypothesis only; retry research-propose for fresh gates; '
                       'not bug proof, owner permission or publication duplicate clearance'}


def checkpoint(cfg: Config, prefix: list[str]) -> dict:
    data = load(cfg)
    from . import research_continuation
    pending = research_continuation.pending(cfg)
    if pending:
        return {'schema': 1, 'status': 'research_continuation_pending', 'local_only': True,
                'repo': pending['old_lead']['repo'], 'phase': pending['phase'],
                'old_lead_authoritative': True, 'destination': pending['destination'],
                'next_argv': prefix + ['research-repo', '--another-investigation', pending['token']],
                'warning': 'preserve old source and partial destination; exact token retry only, no search renewal'}
    if not data['lead'] and data.get('exhausted', {}).get('scope') == _selection_scope(cfg):
        return {'schema': 1, 'local_only': True, **data['exhausted'],
                'next_argv': prefix + ['research-repo', '--next-window']}
    if data['lead']:
        # Checkpoint is routing context, not a repeated dump of all documents.
        lead = dict(data['lead'])
        if lead.get('policy'):
            lead['policy'] = {**lead['policy'], 'docs': [
                {k: d[k] for k in ('path', 'url', 'sha')} for d in lead['policy']['docs']]}
        data = {**data, 'lead': lead}
    review = issue_reviews.load(cfg.path('research_source_review.json'))
    if data['lead'] and data['lead']['status'] == 'investigating':
        context = review.get('context', {})
        if context.get('repo') == data['lead']['repo']:
            valid = (context.get('research_id', '') == data['lead'].get('research_id', '') and
                     hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest() == review.get('fingerprint'))
            try:
                current = evidence.capture(Path(data['lead']['clone']), data['lead']['base'], [])
            except evidence.StaleEvidence:
                current = None
            valid = valid and current == context.get('checkout') and data['lead']['base'] == context.get('base')
            data['lead']['source_review'] = ({'status': 'saved_unvalidated',
                'warning': 'Saved hypothesis only, not fresh clearance. Retry exact research-propose to validate current source evidence.',
                **review} if valid else {'status': 'stale',
                'warning': 'Saved source review does not match this clean checkout/context. Repeat research-propose; no clearance.'})
        return {'schema': 1, 'status': 'repo_investigation', 'local_only': True, 'lead': data['lead'],
                'next': 'IDE: inspect selected code for a useful defect; research-propose PATH LINE --detail HYPOTHESIS [--source-issue NUMBER], or research-repo --defer REASON',
                'warning': 'no bug is confirmed; do not edit before current duplicate/claim/policy review and reproduction'}
    return {'schema': 1, 'status': 'repo_research', 'local_only': True,
            'lead': data['lead'], 'next_argv': prefix + ['research-repo'] if not data['lead'] else
            prefix + ['research-repo', '--refresh'],
            'after_source_review_argv': prefix + ['research-repo', '--begin'] if data['lead'] else None,
            'warning': 'repository research, not a code task or permission; inspect sources and policy before proceeding'}
