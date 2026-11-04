# Developer regression evaluation

This is a small replay layer over existing public tests, not another model runtime
or an autonomous contribution benchmark. Run it from a developer checkout after
installing `.[dev]`:

```bash
python scripts/evaluate.py
python scripts/evaluate.py --stage verification
```

It reports counts separately for selection, budget/checkpoints, policy/duplicates,
verification and onboarding. The selected cases use temporary fixtures and mocked
remote responses; the runner does not invoke live discovery or publication. They
measure the behavior encoded in those cases, not general bug-finding quality.
Skipped cases are counted separately; a stage with no passing tests is not success.
A failed stage returns nonzero and prints its pytest output. No project state is
opened by the runner and temporary XML reports are removed after summarizing.

Keep the same cases when comparing a change with its base. Add a case when a real
failure is reproduced, not merely to raise the test count. The full CI suite still
matters; this selected replay does not replace it. The runner and tests are source
checkout tools, not part of the normal-user wheel.

## Real AI-IDE acceptance is separate

For an approved clean-install trial, record the exact ossforge revision/artifact,
OS/Python, IDE/model, starting config, approved search duration, and one initial
prompt. Record which stages finish: setup, selection, source diagnosis, failing
reproduction, fix, native checks and valid submission. A safe hold/refusal is not a
full contribution success. Record human interventions and elapsed time; record
model usage only when the provider exposes it, otherwise mark it unknown. Never
infer usage from wall time. A PR link proves submission, not merge or acceptance.
Do not renew discovery silently or create public work just to pass the evaluation.
No current fixture result establishes one-prompt IDE success.
