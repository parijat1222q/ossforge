# ossforge

**Find, verify and file quality open-source contributions — with guard rails.**

ossforge scouts healthy projects, reads their code for real bugs, checks that nobody has already reported
or fixed the problem, and only then helps you file an **issue and a pull request together**. It is built for
contributors who want to send maintainers *fewer, better* contributions, not more noise.


**Identity setup per work tree (no global change):** put `github_user`, `git_name`, `git_email` in `ossforge.yaml`, run `ossforge prepare ID` (sets the clone's own git config), then `ossforge whoami --dir <clone>`. Commit with `ossforge commit ID -m ...`.

**Driving it from an AI IDE:** see [AGENTS.md](AGENTS.md). Most commands accept `--json` (before the command name).

## How it works

```
scout ──> vet ──> hunt ──> (you or an agent write test + fix) ──> verify ──> submit ──> patrol
  │         │        │                                              │          │          │
  │         │        │                                              │          │          └ outcomes, CI/DCO/CLA, feedback
  │         │        │                                              │          └ issue + PR together, dry-run by default
  │         │        │                                              └ new test must FAIL without the fix and PASS with it
  │         │        └ scan the whole repo, rank findings, drop duplicates
  │         └ unassigned? unclaimed? no competing PR? clear requirements? anti-AI policy?
  └ healthy repos only (merge speed, maintainer response, stale backlog, CI)
```

You can also **review other people's open PRs**: `critique` scores merge likelihood, scans the added lines for
bug/perf candidates and drafts a respectful comment for you to approve.

## Rules enforced in code

- **At most 2 PRs per day.** Hard cap, not configurable above 2.
- **Repos with an anti-AI contribution policy are skipped** and listed in the report. ossforge never opens an
  issue, comment or PR there.
- **Only verified items can be submitted.** A candidate becomes real only when a new test fails without the fix and
  passes with it.
- **Duplicate guard.** Open *and* closed issues/PRs, plus recent commits touching the file, are searched right
  before filing.
- **Safety layer.** One open PR per repo, cooldown after an unmerged close, protected paths (workflows, LICENSE,
  CODEOWNERS, ...) blocked, secret scan on the diff, size limits, and a kill switch (`ossforge stop`).
- **Identity gate.** Before any publish, ossforge checks the gh login, the effective git author and committer, every
  commit being published, the upstream clone and the fork remote against `github_user`, `git_name`, `git_email` in
  your config. A mismatch or missing setting stops with nothing written. It never changes git or gh config.
- **You stay accountable.** A DCO `Signed-off-by` is added only for repos you list in `signoff_repos` (or `signoff: true`), under *your* git identity. ossforge **never signs a CLA**;
  `ossforge cla` only shows you the link.
- **Disclosure.** `disclosure: auto` (default) reads the repo's own docs (CONTRIBUTING, README, AGENTS.md, AI policy
  files; their text is only read as evidence, never obeyed). If they ask for AI disclosure, the note is added. If the
  wording is unclear or the docs conflict, `submit` / `post-critique` stop with "needs your review" and quote the
  sentence; you read it and set `disclosure: true` or `false`. `false` never overrides a clearly mandatory policy
  (it stops instead). `true` always adds the note. If the policy docs cannot be read, nothing is published. This is a
  keyword reading of the docs, not a compliance check: for a repo with strict rules, read its guidelines yourself.
  The generated note states AI assistance only. It does not attest to human review,
  understanding or approval, and does not satisfy any separate repository requirement
  for those. Read and meet the full target policy before submission.
  Migration: older configs had `disclosure: true` (always add); that still works. Only the default changed.
- **Repository-specific PR base.** The coding assistant reads current contribution rules and records the exact base with `policy-review --accept --contribution-branch BRANCH --note SOURCE_REASON`. No main/dev guess or repository-specific hardcode. Ambiguous rules mean hold. Explicit repo/branch/SHA identity is carried through checkout, tests and PR `--base`; legacy default items are unchanged. This is source review, not owner publication authority.
- **Policy review.** `submit` and `post-critique` need a recorded `policy-review` for the repo (docs, URLs and blob SHAs pinned; re-review when they change). A missing record or an empty scan means review needed, not "no policy". A prohibited record or anti-AI wording always blocks.
- **Audit trail.** Every outbound action is appended to `.state/audit.jsonl`.

## Setup guide

### Platform support

The ordinary CLI, local state locks, repository research and host-native source
verification support Windows and Linux with Python 3.10+. Keep state on a local
filesystem, not a network share. Windows uses exclusive byte-range locks; Linux
uses flock. Do not install a third-party package named `fcntl` to fix startup.

Optional Bubblewrap isolation, offline environment preparation and prepared Python
replay require Linux. Requesting them on Windows stops without a host fallback.
Ordinary host verification is not containment. Native process-tree cleanup uses
POSIX process groups or Windows Job Objects; if a Windows job cannot be created
or joined, the target command is not started.

Research state files are flushed before replacement. Linux also flushes their
parent directory. Windows uses write-through replacement, but does not promise
POSIX directory-fsync or parent/ancestor namespace durability after power loss.
After an interrupted or uncertain write, preserve and reconcile the existing state.

### 1. Prerequisites

| Tool | Why | Check |
|---|---|---|
| Python 3.10+ | runs ossforge | `python --version` |
| Git | cloning, branches, commits | `git --version` |
| [GitHub CLI](https://cli.github.com/) (`gh`) | all GitHub access (no token handling in ossforge) | `gh --version` |

### 2. Log in to GitHub and set your identity

```bash
gh auth login                         # choose GitHub.com, HTTPS, log in via browser
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

Commits use **this** identity; set the same values as `github_user`, `git_name`, `git_email` in `ossforge.yaml` (publishing stops until they match). Use the name you want on your contributions.

### 3. Install: normal use or development

**Normal use** means contributing to other projects with ossforge, not editing
ossforge itself. The current distribution route is this GitHub source checkout;
a public wheel/PyPI release is not available yet. Cloning downloads source, while
installing puts the CLI into your Python environment. Use a regular, non-editable
install with the verification tools, without ossforge's own developer test suite:

```bash
git clone https://github.com/parijat1222q/ossforge.git
cd ossforge
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
python -m pip install ".[tools]"
```

The `tools` extra installs Ruff in the same Python environment as ossforge.
`doctor` and source scanning need that copy, not an unrelated executable on PATH.
It does not install ossforge's developer test suite or an AI model. A target
repository can require its own interpreter and test dependencies; those are
separate from this installation. Your coding assistant uses its own sign-in and
usage limits.

For **ossforge development**, use the same clone and virtual environment but
install editable source with developer tools instead:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests
```

`-e` makes local source edits visible to the installed CLI. `dev` includes pytest
and Ruff; tests live in `tests/`, runtime code in `src/ossforge/`. Normal users do
not need to run ossforge's whole test suite. Both installs retain the same policy,
identity and verification safeguards. There is no second product codebase.

The checkout already contains `AGENTS.md`, `docs/local-ai.md` and the example
config. Open it in your coding assistant and follow those public guides. Finish
configuration and `doctor`/`whoami` checks below before starting work; installation
alone does not sign in, authorize publication or start a search.

For a wheel obtained from a verified future release, `ossforge guide` prints the
packaged workflow. `ossforge guide --output-dir ossforge-workspace` creates a
**new** companion folder containing those three public files. Open that folder in
your coding assistant and copy its example config to `ossforge.yaml`. Existing
paths are refused, never overwritten. Exporting guides does not configure
identity, sign in, install a model, start a search or authorize publication.

### 4. Configure (optional)

```bash
cp ossforge.example.yaml ossforge.yaml     # Windows: copy ossforge.example.yaml ossforge.yaml
```

Every key is optional; defaults are safe.

| Key | Default | Meaning |
|---|---|---|
| `daily_cap` | `2` | PRs per day (hard max 2) |
| `signoff` | `false` | add DCO `Signed-off-by` to every repo; prefer `signoff_repos` |
| `signoff_repos` | `[]` | repos where you approved the DCO sign-off |
| `github_user`, `git_name`, `git_email` | empty | expected identity; required to publish |
| `disclosure` | `auto` | AI-assistance note: `auto` = follow the repo's docs (stops when unclear), `true` = always, `false` = never (stops if the repo requires it) |
| `github_user` | gh login | your login, used by `bootstrap` |
| `min_repo_score` | `60` | minimum repo health score (0-100) |
| `min_stars` | `500` | ignore tiny repos |
| `languages` | `[python]` | languages to scout |
| `labels` | `help wanted`, `good first issue` | issue labels to search (one per pass, rotating) |
| `max_open_prs_per_repo` | `1` | do not pile onto one project |
| `cooldown_days` | `14` | pause new PRs to a repo after one of yours is closed unmerged (pacing only; it does not change repo scores, and `0` turns it off) |
| `auto_submit` | `false` | let the loop submit already-verified items |
| `patch_command` | `""` | optional shell command that drafts a patch; env `OSSFORGE_ITEM` = item id |

### 5. Check it works

```bash
ossforge doctor                 # git identity, gh login, ruff, config
ossforge bootstrap              # optional: learn from your merged/closed PRs so scout prefers repos that merged you (closed PRs are listed but never lower a score, since a closure can mean duplicate, superseded or withdrawn)
# Developer checkout only: python -m pytest -q
ossforge radar pallets/click    # prints a health score for a repo
```

## Where files go (you do not create these)

ossforge creates its working folders **in the directory you run it from**, the first time they are needed.
They are git-ignored, so they never get committed or pushed.

| Path | Created by | What it holds | Safe to delete? |
|---|---|---|---|
| `.work/` | `hunt`, `audit`, `verify` | shallow clones of target repos and a per-repo virtualenv in `.work/venvs/` | yes, it is re-cloned on demand |
| `.state/queue.json` | `hunt`, `loop` | candidate queue and each item's status | resets your queue |
| `.state/audit.jsonl` | `submit`, `loop`, `patrol` | append-only log of every issue/PR/comment opened | **keep**: it drives the one-PR-per-repo and cooldown rules |
| `.state/stats.json` | `verify`, `reject`, `patrol` | per-detector precision and per-repo merge history | resets learning |
| `.state/daily.json` | `submit` | today's PR counter (the daily cap) | resets the cap for today |
| `.state/cache/` | any GitHub call | cached API responses (15 min), repo health (24 h) | yes |
| `.state/digest.md` | `loop`, `digest` | readable report of the last pass | yes |
| `.state/STOP` | `ossforge stop` | kill switch: while it exists, nothing is written to GitHub | remove with `ossforge resume` |
| `.venv/` | you (step 3) | ossforge's own virtualenv | yes, recreate with step 3 |
| `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `*.egg-info/` | pytest, ruff, Python, pip | tool caches and build metadata | yes |

Run ossforge from one folder consistently (for example the repo root) so it always finds the same `.state/`.
To keep state elsewhere, set `state_dir` and `work_dir` in `ossforge.yaml`.

## First run, step by step

```bash
# 1. look around (read-only)
ossforge scout --language python --limit 10
ossforge radar owner/repo

# 2. scan one promising repo; duplicates are dropped automatically
ossforge hunt owner/repo
ossforge queue

# 3. read the top candidate in .work/owner__repo/. If it is a false positive:
ossforge reject "<item id>" --reason "intentional, documented"

# 4. if it is a real bug: on a branch in the clone, add a failing test + the fix, commit, then
ossforge verify "<item id>" --branch fix/short-name --tests path/to/test_file.py::test_name \
    --issue-title "..." --issue-body-file issue.md --pr-title "..." --pr-body-file pr.md

# 5. preview, then submit (issue + PR together)
ossforge submit "<item id>"            # dry run, shows what would happen
ossforge submit "<item id>" --yes      # real submit

# 6. afterwards
ossforge cla owner/repo <pr-number>    # shows the CLA link if the project requires one; you sign it yourself
ossforge patrol                        # what each open PR needs next
```

### Continuous mode

```bash
ossforge loop --once          # one pass: patrol -> scout -> hunt -> digest (read .state/digest.md)
ossforge loop                 # keep going every 6 hours (--interval-hours N)
ossforge stop                 # kill switch
ossforge resume
```

The loop never submits anything unless `auto_submit: true` **and** an item is already `verified`.

## Command reference

```
bootstrap                 import your merged/closed PR history (personalises scout)
doctor                    check git, gh, ruff and config
scout / features          find vetted fix / feature issues (spam filtered, health gated)
radar REPO                health score for a repo
vet REPO NUMBER           vet one issue
hunt REPO                 clone, scan, rank, de-duplicate, enqueue
audit REPO                scan and print candidates without queueing
queue                     list queue items
verify ID ...             run tests without/with the fix, then mark verified
reject ID                 mark a false positive (improves detector ranking)
submit ID [--yes]         issue + PR together; dry run unless --yes; resumes after a partial failure
critique REPO PR          score someone's open PR and draft a review (nothing is posted)
post-critique REPO PR F   post an approved comment from file F (dry run unless --yes)
cla REPO PR               show the CLA bot's comment (read-only)
patrol                    outcomes and what each open PR needs
digest                    print the daily report
loop [--once]             continuous research
stop / resume             kill switch on / off
```

Use `--config path.yaml` *before* the subcommand to load a different config.

## Troubleshooting

- **Rate limits.** ossforge follows GitHub's documented guidance: it reads `Retry-After` and `x-ratelimit-reset`,
  waits (up to 90 s) and retries, otherwise stops the pass and writes a cooldown file in `.state/` that every
  ossforge process shares (a file lock also shares the search budget and write spacing). Searches are capped at 9 per minute and writes are spaced at least 1 s apart.

- **`exceeded a secondary rate limit`**: GitHub throttles bursts of requests even when quota remains. ossforge paces
  itself (searches capped at 9 per minute) and stops the pass cleanly; wait a few minutes and run again.
- **`REFUSED: ...`** from `submit`: a safety gate fired (not verified, daily cap, duplicate, anti-AI policy,
  protected path, kill switch). The message names the gate.
- **DCO check fails on a PR**: commits need `Signed-off-by`. With the repo listed in `signoff_repos` ossforge adds it before the
  first push.
- **CLA bot comment on your PR**: open the link from `ossforge cla` and sign it yourself. It is a personal legal
  agreement, so ossforge never does it for you.

## Honest limits

- Static detectors only produce *candidates*; many are false positives. Only `verify` makes one real.
- Writing the failing test and the patch is up to you or an agent (`patch_command` hook). ossforge does not invent
  patches.
- `verify` runs the project's tests in a virtualenv with a scrubbed environment (no GitHub tokens). That is hygiene,
  not a sandbox; use Docker for repositories you do not trust.
- Detectors are strongest for Python; JavaScript/TypeScript has a few patterns.

## Contributing to ossforge

Issues and PRs are welcome. Run `pytest -q` and `ruff check src tests` before opening a PR.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for credits.

### Avoid reviewing the same unchanged issue again

After reading an issue, record an explicit local decision with a reason:

```sh
ossforge review-issue owner/repo 123 --reason "Broad product request; no source-level reproducer found"
ossforge reconsider-issue owner/repo 123
```

`scout`, `features` and the research loop skip that issue only while GitHub's
`updated_at` matches the reviewed snapshot. New activity returns it for review;
a missing timestamp is never suppressed. `reconsider-issue` clears the decision.
This is not a permanent bug verdict, repository ban or evidence of rejection by
maintainers. Decisions live in the configured `state_dir/issue_reviews.json`:
configs sharing that directory share decisions; separate state directories do not.
The `reviewed_unchanged` funnel count distinguishes these skips from no matches.
The existing `reject ID` command still applies to queued static findings only.

### Bounded discovery and skill hints

`ossforge discover --language python --label bug --strategy rotate --limit 20`
keeps the same star, health, claim, policy and acceptance checks as `scout`, but
rotates one bounded search source per successful pass:

- `broad`: normal ecosystem label/language search.
- `preferred`: repositories in `preferred_repos` (up to five per turn).
- `history`: repositories with observed merged PRs in local `bootstrap`/outcome history (up to five per turn).

Empty sources are omitted from automatic rotation. An explicitly selected empty
source returns no matches, not an unexpected broad search. Repository windows
rotate so a long preference/history list is not permanently truncated. A rate-limit
stop retains the turn and does not hide partial results from the retry. JSON
includes the selected source, exact query and repository window. One pass performs
one search query, with the usual per-issue vetting calls; it does not silently fall
through every source trying to fill a quota.

`discover` temporarily hides previously surfaced **approved** issues while their
upstream `updated_at` is unchanged (`recent_issue_days: 7`). Changed or missing
timestamps return for review. `--include-recent` bypasses this rotation; setting
`recent_issue_days: 0` disables it. This is not a rejection or permanent validity
judgment. State is local to `state_dir` in `discovery.json`; `review-issue` remains
the separate explicit reasoned decision. Legacy `scout` and `features` do not hide
recent results. For the continuous loop, opt in with `discovery_strategy: rotate`;
the default `broad` keeps the original loop behavior.

Optional configuration:

```yaml
preferred_repos: [pallets/click, python/cpython]
skills: [python, asyncio]
recent_issue_days: 7
discovery_strategy: rotate
```

Skill terms add a small, bounded ranking bonus for word-boundary matches in the
issue title/body and show the matched terms. They never waive a gate and are not
proof of difficulty, implementation fit or maintainer acceptance. No model or API
key is required. Affinity means where to look, never permission to publish.

### Verification receipt and stale-change protection

`verify` now records the exact tested Git tree, merge-base SHA, active branch and
command in the queue. Both test runs require a clean working tree; a changed tree
or branch during testing cannot earn a receipt. `submit` checks the receipt in its
dry run and again at the write boundary and before pushing. A source/test commit,
branch switch, dirty work tree or changed merge base requires verification again.
The requested PR branch must be the tested active branch. A message-only rewrite
(such as an explicitly approved DCO signoff) may preserve the identical tree and
base, so it does not invalidate the source-level proof.

Existing queue items that only have `status: verified` but no receipt must be
verified again. The receipt prevents stale test evidence being reused for a
changed patch. It is local state, not a signature, remote attestation, protection
against local-state tampering, or a guarantee that the test proves the right bug.

### Native test commands (non-Python projects)

For a prepared repository, `verify` accepts explicit argv commands instead of
forcing pip/pytest. For example, replace `--tests ...` in the normal verify command
with `--test-command 'node --test tests/regression.test.js'` and optionally
`--check-command 'npm run lint'`. For a Rust project's selected regression test,
use `--test-command 'cargo test regression_name' --failure-code 101`.

Commands are split into argv with `shlex`, never run through a shell. There is no
automatic dependency install in this mode: prepare the project's own locked
requirements first. The same test argv runs without and with the source fix; the
baseline must exit with the expected assertion-failure code (1, or explicitly101
for Cargo), and the fixed version must exit0. Missing commands/timeouts do not
count. Pytest now requires exit1; collection/configuration errors no longer count
as a reproduced failing test. Additional check commands must also pass and are
included in the tested-content receipt. Python's existing `--tests` path retains
its venv/install/ruff behavior.

This is runner support, not automatic language setup or a universal failure
classifier. Cargo101, for example, can also mean a build failure; read the stored
logs and prove the intended assertion failed. Commands run with a scrubbed
environment but are **not sandboxed**. Review repository scripts first; use
separate isolation for untrusted code. No built-in paid model calls are added.

### Optional maintainer admission (draft proposals only)

For projects that explicitly invite automated proposals, set
`maintainer_admission: true` in your local config. This adds a stricter admission
gate; it does not replace your own approval, repository policy review, tested
content receipt, identity checks, protected paths or duplicate checks. The normal
default remains unchanged. This mode is independently implemented, inspired by
ContribAI's separation of generation from maintainer admission, not copied code.

A target maintainer may add `.github/ossforge.yml` on their default branch:

```yaml
version: 1
enabled: true
max_files: 5
max_lines: 250
allowed_dirs: [src, tests]
```

Only literal relative directory prefixes are supported, recursively beneath each
prefix. No globs, unknown/duplicate keys or disabled/malformed manifests are
accepted. File and added-plus-removed-line budgets are checked against the actual
diff. Binary/ambiguous diffs fail closed. A missing manifest may instead use an
open issue carrying the exact `ossforge-approved` label, selected by
`verify --consent-issue NUMBER`; that route has a five-file/250-line budget and
still applies the normal protected-path checks. An existing disabled/malformed
manifest cannot be bypassed by an issue label. GitHub normally restricts label
management to repository collaborators; the label is intent to receive a
proposal, never acceptance of its code or authority to waive another policy.

`ossforge consent-check owner/repo --issue NUMBER` is read-only. Admission checks
fetch current upstream state without the local cache, in preflight, before the
first write, before pushing and before creating the PR. Removing/closing the
approval or disabling the manifest blocks later steps. Already-created issues or
fork pushes remain if consent is revoked mid-workflow; the existing resumable
progress records those effects. Every PR created in strict admission mode is a
**draft**. Nothing signs a CLA or merges a PR.

This is a narrow live scope gate, not ContribAI's full run lifecycle, expiring
permit, risk engine or signed evidence service. It requires no model key and does
not grant us or any other assistant permission to act for the operator.

### Operational timing limits

`loop --interval-hours N` sleeps after each completed pass, not on a fixed wall-clock schedule.
The interval must be finite and at least one second (the default is six hours).
GitHub API read subprocesses time out after 120 seconds; a new clone times out after
300 seconds and its partial checkout is removed before retry. These are individual
operation bounds, not a deadline for an entire discovery pass, verification, external
patch command or contribution. A pass may perform multiple reads and rate-limit waits.
CLI results and the local digest are not push notifications, and a loop needs a running host.

### Run progress and recovery records

`loop` and `loop --once` now print stage starts and discovery filter reasons while
work is in progress. `.state/run.json` is an atomically replaced latest-run record;
`.state/runs/<id>.json` holds completed/failed/interrupted run summaries. Each record
contains timestamps and progress, not a proof of contribution correctness.
A nonblocking state-directory lock prevents two loop passes using the same state
at once. A process that dies leaves a running snapshot; the next pass archives it
as interrupted before starting a new pass. It does not blindly replay remote writes.
Existing queue and submission progress still govern later work. Separate state
directories do not share this lock, and this is not a distributed execution service.

### External local AI drivers

For an assistant already open in an IDE, start with `agent-next` and
[one starter prompt](docs/local-ai.md). It reports local STOP/reconciliation/verified/
candidate/no-work checkpoints without invoking a model or publishing. An IDE can
drive the CLI directly; it does not need to spawn a second AI process.

`agent-task [--item ID]` emits a versioned model-neutral task for a clean cloned
candidate. `agent-verify RESULT.json` validates the result contract and reruns
verification itself; it never trusts model-supplied test evidence or publishes.
Use [the local AI workflow](docs/local-ai.md) for a Claude Code or other terminal
assistant starting prompt and the limits. This is a local driver interface, not a
claim that an unattended external-model contribution has been validated.

`agent-run --command '/path/to/local-driver'` optionally coordinates one bounded
external-command attempt and actual verification. The child writes patch/result
only, not nested mutating CLI calls. Optional `commit_message` plus exact
`commit_paths` request a parent-owned identity-checked commit before verification;
legacy already-committed results still work. Publication is off unless
`--publish` is explicitly authorized. See the local workflow for the JSON-file
contract, timeout/daily-attempt semantics, recovery and security limitations.
It is not a token budget, sandbox, cloud service or validated Claude adapter.

Publication also takes a nonblocking `.state/publication.lock`, including direct
`submit --yes` calls. A competing same-state call refuses before writes. After
acquiring the lock, submission reloads saved queue progress and persists each
completed remote step before continuing, so a stale caller resumes that progress.
This does not coordinate separate state directories or make remote GitHub writes
and local persistence one atomic transaction. A crash after a successful remote
write but before its URL is saved still requires operator reconciliation; do not
blindly retry an unknown remote outcome.

`discovery_read_budget_seconds` (default120) adds a cooperative deadline for the
GitHub-read discovery phase, including cache reads, per-request timeout reduction
and refusing pacing/retry sleeps that cannot fit. An exhausted phase preserves
partial findings and reports stopped/partial, not a complete no-find result.
This is not a hard whole-loop deadline: lock contention, filesystem work, patrol,
clone/hunt, external commands, tests and publication are outside this phase.

### Duplicate and active-work checks at publication

Publication uses uncached path/title/symbol issue and PR searches (all states),
plus recent commits touching the candidate file (45 days). Failed, incomplete,
or saturated checks stop publication; they do not mean no duplicate exists.
An existing saved issue does not bypass these checks on resume. Checks run
before external writes and again immediately before PR creation.

An approved source issue is reused, never re-created. Its current state,
assignees, claims in comments, timeline cross-references, and all-state PR
references are rechecked. A draft/open/closed/merged PR reference requires
review rather than an automatic competing PR. An old issue with no claim or
PR is not rejected merely because it is old. Existing claims are conservative
review stops, not proof somebody is still working on the issue.

These are finite heuristics, not a guarantee: unrelated wording, unlinked work,
unpushed branches and commits outside the file/time sample can be missed.
GitHub search can also lag even with cache bypass. Read the actual competing
change before deciding whether a new PR adds a real correctness or performance
benefit. That decision is not automated. No remote transaction can eliminate
the race between a last read and another person's publication.

File coverage comes from every non-test path in the actual committed patch, not
only the detector's original location (issue leads may have no original path).
Changes over10 source paths stop for separate review rather than silently
checking only a subset.

Publication also compares the live upstream default tip with the clone's saved
`origin/HEAD` and patch merge base. A moved/unavailable tip stops publication
before writes and again before PR creation. It never resets, rebases or discards
an active checkout automatically. Fetch, inspect/rebase the patch, resolve any
conflict and rerun verification; fetching alone does not make old test evidence
current. A completed saved PR can still be recovered without new writes. This
conservative rule can require retesting even after an unrelated upstream change;
it cannot eliminate a change racing immediately after the final read.

### Review a duplicate-search false positive

A monolithic source file can match unrelated issues. Do not lower duplicate thresholds or
skip the publication guard. After reading the returned issue/PR and its current diff:

```sh
ossforge review-duplicate ITEM 123 --reason "Different API/cause; describe the distinction after reading it"
```

This explicit local decision applies only to that verified candidate's exact text and
verification receipt and the reviewed upstream item revision (PR head/base included).
Any candidate or upstream change invalidates it. Every publication boundary re-reads
reviewed matches; unreadable sources refuse publication. New matches and recent-commit
hits still block. It is not a general approval of a repository or a claim of novelty.

For a recent-commit keyword false positive, inspect the complete commit first, then use
`ossforge review-commit-match ITEM FULL_SHA --reason "different API/cause"`.
It binds the same candidate receipt/text to one immutable commit SHA/tree/parents;
new commit hits and candidate changes still block, and each read must succeed.

### Targets that ask for a PR without a new issue

After reviewing a target's current contribution guide, list it in
`pr_only_repos: [owner/repo]` to avoid creating a new issue for new work.
This is a publication-shape choice, not permission or automatic policy detection.
An existing `source_issue` is still checked and linked normally. Other targets
keep the default issue-plus-PR flow.

The dry run names `publication_mode`. PR-only progress records that mode before
fork/push effects, returns `issue: null`, and adds no invented `Closes` reference.
Changing the mode after progress, or hiding a saved issue under PR-only mode,
stops for reconciliation. Identity, current upstream, policy review, duplicates,
exact-tree verification and submission limits remain checked. Reconcile unknown
remote outcomes instead of retrying a possibly completed write.

### Contribution-policy routing

The coding assistant reads the target's current contribution rules, including
linked guides, before choosing a route. Record that source-backed decision with
`policy-review REPO --accept --publication-route pr_only --note '...'`.
Use `--extra-path docs/contributing.rst` (repeatable) to include an exact linked
repository file outside the standard locations. Its blob revision is pinned and
fetched fresh again at submit time. This is policy review, not owner consent.

`issue_pr` is the normal default when a complete review finds no specific
publication rule. It does not mean hold. `pr_only` avoids a new issue for new
work; an existing source issue is still reused. `issue_first` stops immediate
submission until its discussion/feedback prerequisites have been reviewed.
`unresolved` stops for unclear or conflicting rules. Neither a failed read nor an
unread linked guide counts as no rules. The assistant must record unresolved
rather than infer a route when it cannot finish reading the required sources.

Submit applies the reviewed route, not a fixed repository allowlist. Changed or
missing pinned sources require review again. The older explicit `pr_only_repos`
setting cannot contradict a reviewed route. Arbitrary website text is not run as
instructions; the assistant interprets contribution policy within the owner's
existing authority. This does not sign agreements, approve spending, change
safety limits, or implement every repository's custom process. External-only
linked pages without a pinned repository source must be checked separately;
the blob freshness guard does not certify those pages.

#### Opt-in breadth-first coverage experiment

`discovery_strategy: breadth-first` (or `discover --strategy breadth-first`)
tries six broad repository windows before one preferred window and one history
window, omitting either affinity turn when its pool is empty. Each broad window
still contains at most five repositories; six windows cover one 30-repository
search page. Affinity windows advance on later epochs, including pools larger
than five repositories. Existing `broad` and `rotate` defaults are unchanged.

This mode has its own `breadth_cursor` in the discovery state, so switching modes
does not consume the baseline cursor. Partial or rate-limited passes do not
advance it. The recent-approval list remains shared to avoid resurfacing an
unchanged accepted lead when switching modes. Query terms, vetting, duplicate
review, read budget and request pacing are unchanged. Six windows is a bounded
experiment, not a claim of optimal scheduling or better contribution quality.

Motivation: a seven-turn offline replay using one saved 30-repository page and
an actual four-repository history pool covered 34 unique repositories with this
schedule versus 24 with alternating history turns. That measures planned
repository coverage only, not available bugs, current health, successful patches
or accepted contributions. Compare actual query/candidate/repeat counts and
qualified work before adopting this mode for a longer run.

Agent-specific contribution restrictions (including conditional first-time restrictions) can
produce a separate "agent contribution policy needs review" hold. This is not an all-AI ban
and a missing signal is not permission. Read the full, current policy documents, including
conflicting sections and any human-assistance allowance, with `ossforge policy-review OWNER/REPO`.
The hold does not accept a prose override or an authorship attestation; publication still
requires the existing source-pinned policy review and all other gates. Choose other work
while a restriction remains unresolved. Literal matching is conservative and incomplete.

Repo-first research is available as a staged opt-in with `repo_first: true`.
Start a bounded session with `ossforge research-session --seconds 3600` (for
example, one hour). `research-session` reports remaining time; resume does not
extend it. After exhaustion, choose a new session explicitly with
`research-session --new --seconds 3600`. Existing investigation and verification
are not a whole-contribution timeout. Search has no background scheduler.
`agent-next` then selects a repository-research checkpoint independently of open
issue search, using `research-repo`, policy-reviewed `research-repo --begin`, and
`research-propose PATH LINE --detail ...`. If fresh duplicate or recent-commit searches find possible prior work,
proposal stops before coding and saves exact source URLs in the research checkpoint.
Inspect each issue/PR discussion or commit diff, then record an unrelated-work reason
with `research-review ISSUE_NUMBER_OR_FULL_COMMIT_SHA --reason ...` and retry the
identical `research-propose`. Every retry rechecks current sources, policy, tip and
clean checkout. Reviews are pinned to that hypothesis, checkout and source snapshot;
changed/new evidence holds again. These pre-coding reviews do not confirm a bug,
authorize publication, or replace the separate verified-candidate duplicate reviews. The IDE AI investigates code and owns
semantic diagnosis; finite static `hunt` hints are optional, not bug proof.
See [the repo-first flow](docs/local-ai.md#repo-first-investigation-staged-opt-in)
for evidence limits and prerequisites. Existing local work, STOP, reconciliation
and cooldown still take priority. The issue-only compatibility route and saved
partial issue batches remain unchanged.
