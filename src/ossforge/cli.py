from __future__ import annotations

import argparse
import shlex
import dataclasses
import json
import math
import sys
from pathlib import Path

from . import agent_next, duplicate_reviews, agent_run, evidence, driver, acceptance, identity, issue_reviews, policy_review
from . import audit as audit_mod
from . import admission, discovery, comment, critique, digest, doctor, gh, hunt, loop, patrol, profile, radar, review, safety, scout, submit, vet, verify
from .config import Config, load
from .limiter import DailyLimiter
from .outcomes import Stats
from .queue import Queue


def _ctx(cfg: Config):
    gh.configure(cfg.path("cache"), cfg.cache_ttl_seconds)
    return (Queue(cfg.path("queue.json")), safety.Audit(cfg.path("audit.jsonl")),
            Stats(cfg.path("stats.json")), DailyLimiter(cfg.path("daily.json"), cfg.cap))


def positive_interval(value: str) -> float:
    try:
        hours = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be finite and at least one second") from exc
    if not math.isfinite(hours) or hours < 1 / 3600:
        raise argparse.ArgumentTypeError("interval must be finite and at least one second")
    return hours


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("ossforge")
    ap.add_argument("--config", default="ossforge.yaml")
    ap.add_argument("--json", action="store_true", help="machine-readable output (radar, acceptance, scout, features, queue, doctor, whoami, submit)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_, *args, **flags):
        p = sub.add_parser(name, help=help_)
        for a in args:
            p.add_argument(a)
        for f, kw in flags.items():
            p.add_argument("--" + f.replace("_", "-"), **kw)
        return p

    add("consent-check", "read explicit maintainer admission, without executing or publishing", "repo", issue={"type": int, "default": 0})
    add("radar", "score a repo's health", "repo")
    prv = add("policy-review", "show a repo's policy docs (URLs, SHAs) and optionally record YOUR review before submitting", "repo")
    prv.add_argument("--accept", action="store_true", help="record that you read the docs and the repo's own pages")
    prv.add_argument("--prohibited", action="store_true", help="record that the repo's rules forbid this contribution")
    prv.add_argument("--publication-route", choices=["issue_pr", "pr_only", "issue_first", "unresolved"], help="source-backed route after reading contribution rules; not owner consent")
    prv.add_argument("--extra-path", action="append", default=[], help="exact linked in-repository contribution file to pin and recheck")
    prv.add_argument("--note", default="", help="restrictions that apply, in your words")
    dr = add("review-duplicate", "record one reviewed false positive for this exact verified candidate", "id")
    dr.add_argument("number", type=int)
    dr.add_argument("--reason", required=True)
    dc = add("review-commit-match", "record one inspected unrelated commit for an exact verified candidate", "id", "sha")
    dc.add_argument("--reason", required=True)
    add("prepare", "set this work tree's own git author/committer to the configured identity (local config only)", "id")
    cm = add("commit", "identity-checked git commit in an item's clone", "id")
    cm.add_argument("-m", "--message", required=True)
    add("acceptance", "evidence on whether outside contributors' PRs get merged lately", "repo")
    wm = add("whoami", "show which GitHub account and git identity ossforge would publish as, and whether they match the config")
    wm.add_argument("--dir", default=".", help="work tree to check (default: current directory)")
    v = add("vet", "vet one issue", "repo")
    v.add_argument("number", type=int)
    add("scout", "find vetted fix issues", language={}, label={"default": "help wanted"}, limit={"type": int, "default": 20})
    add("discover", "rotate bounded broad/history/preferred issue discovery", language={},
        label={"default": "help wanted"}, limit={"type": int, "default": 20},
        strategy={"choices": ["rotate", "broad", "history", "preferred"], "default": "rotate"},
        include_recent={"action": "store_true"})
    add("features", "find vetted feature issues", language={}, limit={"type": int, "default": 15})
    ir = add("review-issue", "skip an explicitly reviewed issue until upstream activity changes", "repo")
    ir.add_argument("number", type=int)
    ir.add_argument("--reason", required=True)
    rr = add("reconsider-issue", "clear a local issue-review decision", "repo")
    rr.add_argument("number", type=int)
    c = add("critique", "score an open PR and draft (not post) a review", "repo")
    c.add_argument("number", type=int)
    pc = add("post-critique", "post an approved critique comment (dry run unless --yes)", "repo")
    pc.add_argument("number", type=int)
    pc.add_argument("body_file")
    pc.add_argument("--yes", action="store_true")
    add("audit", "scan a repo for bug/perf candidates", "repo")
    add("hunt", "scan, rank, de-duplicate and enqueue", "repo")
    add("queue", "list queue items")
    add("agent-next", "read-only IDE checkpoint: stop, reconcile, verify, work or discover")
    at = add("agent-task", "print a model-neutral local task as JSON")
    at.add_argument("--item", default=None)
    add("agent-verify", "validate external-driver result and rerun tests; never publish", "result_file")
    add("agent-run", "one bounded local external-driver contribution attempt", command={"required": True},
             timeout={"type": float, "default": 900}, daily_attempts={"type": int, "default": 2},
             skip_discovery={"action": "store_true"}, publish={"action": "store_true"})
    vf = add("verify", "run the repo's tests with/without the fix, then mark verified", "id")
    vf.add_argument("--consent-issue", type=int, default=0, help="existing maintainer-approved upstream issue for strict admission")
    vf.add_argument("--branch", required=True)
    vf.add_argument("--tests", nargs="+", default=[], help="pytest args selecting the new test")
    vf.add_argument("--test-command", help="explicit test argv parsed with shlex, never a shell; no auto install")
    vf.add_argument("--check-command", action="append", default=[], help="additional argv check; repeat for lint/type checks")
    vf.add_argument("--failure-code", type=int, choices=[1, 101], default=1, help="expected baseline assertion failure (cargo test uses 101)")
    vf.add_argument("--issue-title", required=True)
    vf.add_argument("--issue-body-file", required=True)
    vf.add_argument("--pr-title", required=True)
    vf.add_argument("--pr-body-file", required=True)
    vf.add_argument("--install", nargs="*", default=["."], help="paths to pip install -e in the venv")
    hd = add("hold", "pause an unverified clean candidate without marking it false", "id")
    hd.add_argument("--reason", required=True)
    add("unhold", "explicitly return held work to clean, never verified", "id")
    rj = add("reject", "mark an item as a false positive (feeds detector precision)", "id")
    rj.add_argument("--reason", default="")
    sm = add("submit", "file issue + PR together (dry run unless --yes)", "id")
    sm.add_argument("--yes", action="store_true")
    cl = add("cla", "show the CLA bot comment on a PR (you sign it yourself)", "repo")
    cl.add_argument("pr", type=int)
    add("patrol", "check our open PRs and say what each needs")
    add("digest", "write and print the daily digest")
    lp = add("loop", "run the research loop", once={"action": "store_true"}, interval_hours={"type": positive_interval, "default": 6})
    lp.set_defaults()
    add("doctor", "check git, gh, ruff and config before the first run")
    add("bootstrap", "import your merged/closed PR history to personalise scouting")
    add("stop", "create the kill switch file")
    add("resume", "remove the kill switch file")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    cfg = load(a.config)
    # Emergency controls must not depend on readable workflow state.
    if a.cmd == "stop":
        Path(cfg.kill_file).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.kill_file).write_text("stopped\n")
        print("kill switch on:", cfg.kill_file)
        return 0
    if a.cmd == "resume":
        Path(cfg.kill_file).unlink(missing_ok=True)
        print("kill switch off")
        return 0
    q, audit, stats, limiter = _ctx(cfg)
    if getattr(a, "id", None) is not None and a.id not in q.items:
        print(f"unknown queue id {a.id!r}. Run `queue` to list ids.", file=sys.stderr)
        return 2

    def emit(obj) -> None:
        print(json.dumps(obj, indent=2, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o)))

    if a.cmd == "doctor":
        results = doctor.check(cfg)
        if a.json:
            emit([{"check": n, "ok": ok, "detail": d} for n, ok, d in results])
            return 0 if all(ok for _, ok, _ in results) else 1
        for name, ok, detail in results:
            print(f"[{'ok' if ok else 'FIX'}] {name}: {detail}")
        return 0 if all(ok for _, ok, _ in results) else 1
    elif a.cmd == "consent-check":
        try:
            result = admission.inspect(a.repo, a.issue)
        except admission.Denied as e:
            result = {"allowed": False, "reason": str(e)}
            emit(result) if a.json else print("DENIED:", e)
            return 1
        emit(result) if a.json else print("maintainer intent:", result, "(not operator approval or policy clearance)")
    elif a.cmd == "bootstrap":
        r = profile.bootstrap(stats, cfg.github_user, cfg.own_orgs)
        print(f"{r['user']}: merged PRs in {r['merged_repos']} repos, closed unmerged in {r['closed_repos']} repos")
        for repo, n in sorted(r["merged"].items(), key=lambda kv: -kv[1]):
            print(f"  merged x{n}: {repo}")
        if r["unclassified"]:
            print("  member-org repos not counted (add the org to own_orgs if it is yours, they stay out otherwise):",
                  ", ".join(r["unclassified"]))
    elif a.cmd == "whoami":
        miss = identity.missing_settings(cfg)
        login = identity.check_login(cfg)
        wt = identity.check_worktree(cfg, a.dir)
        info = {"expected": dict(zip(("github_user", "git_name", "git_email"), identity.expected(cfg))),
                "effective_git": {k: f"{n} <{e}>" for k, (n, e) in identity.effective_idents(a.dir).items()},
                "missing_settings": miss, "problems": login + wt, "ready_to_publish": not miss and not login and not wt}
        if a.json:
            emit(info)
        else:
            print(f"expected: {info['expected']}\neffective git: {info['effective_git']}")
            print("READY to publish" if info["ready_to_publish"] else "NOT READY: " + "; ".join(miss and [f"{m} not set" for m in miss] or login + wt))
        return 0 if info["ready_to_publish"] else 1
    elif a.cmd == "policy-review":
        snap = policy_review.snapshot(a.repo, extra_paths=a.extra_path) if a.extra_path else policy_review.snapshot(a.repo)
        if a.accept or a.prohibited:
            if not a.note.strip():
                print("--note is required: state the restrictions that apply (or 'none found after reading X')")
                return 2
            rec = policy_review.record(cfg, a.repo, snap, a.note, prohibited=a.prohibited, publication_route=a.publication_route)
            print("recorded:", json.dumps({k: v for k, v in rec.items() if k != "docs"}), f"{len(rec['docs'])} doc(s) pinned")
        else:
            print(f"{a.repo}: {len(snap['docs'])} policy-bearing doc(s) found in the scanned locations; anti-AI wording: {snap['anti_ai']}")
            for d in snap["docs"]:
                print(f"\n== {d['path']}  {d['url']}  blob {d['sha'][:10]}\n{d['text'][:1500]}")
            print("\nThe scan reads fixed locations only. Also read the project's contribution/AI pages yourself.")
    elif a.cmd == "review-duplicate":
        try:
            duplicate_reviews.record(cfg.path("duplicate_reviews.json"), q.items[a.id], a.number, a.reason)
        except (ValueError, gh.GhError) as exc:
            print("REFUSED:", exc)
            return 1
        print("reviewed only this candidate/item revision; all other duplicate and commit gates remain active")
    elif a.cmd == "review-commit-match":
        try:
            duplicate_reviews.record_commit(cfg.path("duplicate_reviews.json"), q.items[a.id], a.sha, a.reason)
        except (ValueError, gh.GhError) as exc:
            print("REFUSED:", exc)
            return 1
        print("reviewed only this exact candidate and immutable commit; other commit matches still block")
    elif a.cmd == "prepare":
        bad = identity.apply_local(cfg, q.items[a.id].clone)
        print("READY: this clone commits as the configured identity" if not bad else "NOT READY: " + "; ".join(bad))
        return 1 if bad else 0
    elif a.cmd == "commit":
        try:
            print("committed", identity.commit(cfg, q.items[a.id].clone, a.message))
        except identity.IdentityError as e:
            print("REFUSED:", e)
            return 1
    elif a.cmd == "acceptance":
        acc = acceptance.evidence(a.repo)
        if a.json:
            emit(acc)
        else:
            print(f"{acc.repo}: {acc.status} (freshness: {acc.freshness}; sampled {acc.sampled_prs} closed PRs, "
                  f"examined {acc.closed_seen} human PRs over ~{acc.covered_days}d; merger lookups {acc.merger_lookups}, "
                  f"unresolved {acc.merger_unresolved})")
            for r_ in acc.reasons + acc.caveats:
                print("  -", r_)
            for m in acc.external_merged[:8]:
                print(f"  PR #{m['pr']} by {m['author']} merged {m['merged_at'][:10]} by {m['merged_by'] or 'not looked up'}")
    elif a.cmd == "radar":
        x = radar.analyze(a.repo)
        if a.json:
            emit(x)
            return 0
        print(f"{x.repo}: {x.score}/100 {x.recommendation} flags={x.flags}")
        for n in x.notes:
            print("  -", n)
    elif a.cmd == "vet":
        x = vet.vet_issue(a.repo, a.number)
        print(x.issue, x.decision, "; ".join(x.reasons))
    elif a.cmd in ("scout", "features", "discover"):
        fn = (lambda: scout.scout(cfg, stats, a.language, a.label, a.limit)) if a.cmd == "scout" else \
             (lambda: scout.scout_features(cfg, stats, a.language, a.limit))
        with gh.read_budget(cfg.discovery_read_budget_seconds):
            res_ = discovery.discover(cfg, stats, a.language, a.label, a.limit, a.strategy, a.include_recent) if a.cmd == "discover" else fn()
        print("scout:", scout.explain_funnel(scout.LAST_FUNNEL), file=sys.stderr)
        if a.json:
            emit({"funnel": scout.LAST_FUNNEL, "explanation": scout.explain_funnel(scout.LAST_FUNNEL), "candidates": res_})
            return 0
        for c_ in res_:
            print(f"[{c_.verdict.decision:7}] {c_.repo}#{c_.number} score={c_.score:.0f} {c_.title}")
            print(f"          why: {'; '.join(c_.why)}")
            print(f"          {'; '.join(c_.verdict.reasons)}\n          {c_.url}")
    elif a.cmd == "critique":
        x = critique.critique(a.repo, a.number)
        print(f"{x.repo}#{x.number} by {x.author}: merge likelihood {x.prediction.score} ({x.prediction.label})")
        print("  signals:", "; ".join(x.prediction.notes))
        for f in x.findings:
            print(f"  [{f.severity}] {f.path}:{f.line} {f.kind}: {f.detail}")
        print("\n--- DRAFT (not posted) ---\n" + x.draft)
    elif a.cmd == "post-critique":
        try:
            print(comment.post(cfg, audit, a.repo, a.number, Path(a.body_file).read_text(encoding="utf-8"), a.yes))
        except comment.Refused as e:
            print("REFUSED:", e)
            return 1
    elif a.cmd == "review-issue":
        issue = gh.api(f"repos/{a.repo}/issues/{a.number}", fresh=True)
        if "pull_request" in issue:
            print("expected an issue, not a pull request", file=sys.stderr)
            return 2
        try:
            issue_reviews.record(cfg.path("issue_reviews.json"), a.repo, a.number,
                                 issue.get("updated_at", ""), a.reason)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"reviewed {a.repo}#{a.number}: skipped only while updated_at stays {issue['updated_at']}")
    elif a.cmd == "reconsider-issue":
        issue_reviews.reconsider(cfg.path("issue_reviews.json"), a.repo, a.number)
        print(f"reconsidering {a.repo}#{a.number}")
    elif a.cmd == "audit":
        dest = hunt.clone(cfg, a.repo)
        pol = hunt.policy_of(dest)
        if pol.anti_ai:
            print(f"SKIP {a.repo}: anti-AI policy in {pol.source}. No findings produced.")
            return 0
        for f in sorted(audit_mod.scan_tree(dest), key=lambda x: (x.severity, x.path, x.line)):
            print(f"  [{f.severity}] {f.path}:{f.line} {f.kind}: {f.detail}")
    elif a.cmd == "hunt":
        r = hunt.hunt(cfg, a.repo, q, stats)
        print(f"SKIP {a.repo}: anti-AI policy in {r['anti_ai']}" if r["anti_ai"]
              else f"{a.repo}: {r['clean']} clean (need verification), {r['duplicate']} duplicate")
    elif a.cmd == "agent-run":
        try:
            result = agent_run.run(cfg, shlex.split(a.command), timeout=a.timeout, attempts=a.daily_attempts,
                                   execute=a.publish, discover=not a.skip_discovery,
                                   progress=lambda text: print(text, file=sys.stderr, flush=True))
        except (driver.Invalid, OSError, evidence.StaleEvidence, safety.Blocked) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        emit(result)
        return 0 if result["status"] in ("no_work", "verified_not_submitted", "submitted") else 1
    elif a.cmd == "agent-next":
        emit(agent_next.next_step(cfg, q, a.config))
    elif a.cmd == "agent-task":
        try:
            emit(driver.task(q, a.item))
        except (OSError, evidence.StaleEvidence, submit.Refused) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
    elif a.cmd == "agent-verify":
        try:
            result = driver.complete(q, driver.parse(Path(a.result_file)))
        except (driver.Invalid, OSError, evidence.StaleEvidence) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        emit(result)
        return 0 if result["status"] == "verified" else 1
    elif a.cmd == "queue":
        if a.json:
            emit([dataclasses.asdict(i) for i in sorted(q.items.values(), key=lambda i: -i.rank)])
            return 0
        for i in sorted(q.items.values(), key=lambda i: -i.rank):
            print(f"[{i.status:9}] {i.rank:.2f} {i.id}  {i.detail}")
            if i.status == "held":
                print("              held:", i.hold_reason)
            for n in i.notes:
                print("              ", n)
            if i.snippet and i.status == "clean":
                print("\n".join("              | " + ln for ln in i.snippet.splitlines()))
    elif a.cmd == "verify":
        it = q.items[a.id]
        if it.status == "held":
            print("REFUSED: held work must be explicitly unheld after resolving its prerequisite", file=sys.stderr)
            return 1
        clone = Path(it.clone)
        if not a.tests and not a.test_command:
            print("verify requires --tests or --test-command", file=sys.stderr)
            return 2
        try:
            test_command = shlex.split(a.test_command) if a.test_command is not None else None
            checks = [shlex.split(c) for c in a.check_command]
        except ValueError as e:
            print(f"invalid command argv: {e}", file=sys.stderr)
            return 2
        if test_command == [] or any(not c for c in checks):
            print("commands must not be empty", file=sys.stderr)
            return 2
        py = Path(sys.executable)
        if test_command is None:
            py = verify.venv_python(Path(cfg.work_dir), it.repo)
            rc, out = verify.install_project(py, clone, a.install)
            if rc:
                print("install failed:\n", out)
                return 1
        base = submit._run(["git", "merge-base", "HEAD", "origin/HEAD"], str(clone))
        r = verify.verify_fix(clone, base, py, a.tests, test_command=test_command,
                              check_commands=checks, failure_code=a.failure_code)
        print("failed without fix:", r.failed_without_fix, "| passed with fix:", r.passed_with_fix, "| lint ok:", r.lint_ok)
        stats.record_kind(it.kind, r.ok)
        stats.save()
        if not r.ok:
            print("\n".join(r.log))
            return 1
        q.set(a.id, status="verified", branch=a.branch, issue_title=a.issue_title, pr_title=a.pr_title,
              issue_body=Path(a.issue_body_file).read_text(encoding="utf-8"),
              pr_body=Path(a.pr_body_file).read_text(encoding="utf-8"), verification=r.evidence, consent_issue=a.consent_issue)
        q.save()
        print("verified:", a.id)
    elif a.cmd in ("hold", "unhold"):
        it = q.items.get(a.id)
        expected = "clean" if a.cmd == "hold" else "held"
        if not it or it.status != expected or it.submitted or (a.cmd == "hold" and not a.reason.strip()):
            print(f"REFUSED: {a.cmd} requires {expected} work without publication progress and a nonempty hold reason", file=sys.stderr)
            return 1
        q.set(a.id, status="held" if a.cmd == "hold" else "clean",
              hold_reason=a.reason.strip() if a.cmd == "hold" else "")
        q.save()
        print(f"{a.cmd}: {a.id}")
    elif a.cmd == "reject":
        it = q.set(a.id, status="rejected", notes=[a.reason] if a.reason else [])
        stats.record_kind(it.kind, False)
        stats.save()
        q.save()
        print("rejected:", a.id)
    elif a.cmd == "submit":
        try:
            res = submit.submit(q.items[a.id], limiter, cfg, audit, execute=a.yes,
                                save=lambda p: (q.set(a.id, submitted=p), q.save()))
        except submit.Refused as e:
            if a.json:
                emit({"refused": str(e)})
            else:
                print("REFUSED:", e)
            return 1
        if a.json:
            emit(res)
        if a.yes:
            q.set(a.id, status="submitted", submitted=res)
            q.save()
        if not a.json:
            print(res)
    elif a.cmd == "selfreview":
        it = q.items[a.id]
        base = submit._base(it.clone)
        found = review.review_branch(it.repo, it.clone, base, it.pr_body, submit._run, gh)
        for f in found:
            print(f"[{f.level}] {f.msg}")
        print("ok: nothing to flag" if not found else "")
        return 1 if review.blocking(found) else 0
    elif a.cmd == "cla":
        url = submit.cla_status(a.repo, a.pr)
        print(url or "No CLA bot comment yet.", "\nOpen it and sign as yourself; ossforge never signs a CLA.")
    elif a.cmd == "patrol":
        for x in patrol.patrol(audit, stats):
            print(f"{x.repo}#{x.pr} [{x.kind}] {x.detail}")
    elif a.cmd == "digest":
        print(digest.render(q, audit, patrol.patrol(audit, stats)))
    elif a.cmd == "loop":
        if a.once:
            loop.run_once(cfg, progress=lambda text: print(text, flush=True))
        else:
            loop.run_forever(cfg, int(a.interval_hours * 3600))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
