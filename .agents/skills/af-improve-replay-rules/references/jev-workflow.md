# Jev in the replay improvement workflow

Formal integration is implemented in `spectragent/replay_review/` (architecture §4.18).
Use the ordinary composed `replay_reviewer` layer, its `decision_model` receptor and
`spectragent replay-review`; the earlier standalone HTTP helper is retired. Review jobs
do not modify source runs, runtime restoration verdicts, matcher rules or task scores.

## Prepare aligned pairs

Use current trace format 6 and a replay with explicit branch/source correspondence.
Select pairs from its `StepRecord.restoration`, retaining the original machine verdict.
Write a private JSON input outside the source/replay run directories:

```json
{
  "format": "af-replay-review-input-1",
  "pairs": [
    {
      "source": "/absolute/path/to/source-run",
      "replay": "/absolute/path/to/replay-run",
      "source_step": 4,
      "replay_step": 4,
      "source_config": "/absolute/path/to/record.yaml",
      "visual_required": true
    }
  ]
}
```

Relative paths resolve against the input file. `source_config` is the actual recording
YAML: evidence preparation composes its browser cleaner, budget and serializer, verifies
the recorded recipe, and reconstructs prior selector maps. It does not instantiate the
source Agent or require its model credentials. The browser-owned reader aligns capture
cycle, tab, document identity and screenshot before cleaning raw DOM. Missing evidence,
corruption and alignment failures have distinct statuses; two empty views do not prove
equivalence. Keep expected labels and development/holdout assignments in another file.

## Run and inspect

```bash
uv run spectragent replay-review \
  --config configs/replay_review/jev_dmx.yaml \
  --pairs path/to/pairs.json --out path/to/new-review-directory --dry-run

uv run spectragent replay-review \
  --config configs/replay_review/jev_dmx.yaml \
  --pairs path/to/pairs.json --out path/to/new-review-directory --json
```

`--dry-run` validates composition and input shape without requests or writes; it does not
establish that trace evidence aligns or the model works. The execution directory must be
new. The CLI loads `.env`; the DMX configuration reads `OPENAI_API_KEY`. Official direct
and OpenRouter use their shipped configurations and corresponding credential variables.
Keep credentials, storage seeds and raw authorization headers out of review inputs.

The job preserves its config, questions/policy versions, evidence references and digests,
native Decision answers and calls under its own indexed `review-manifest.json`. Calls
carry `kind=decision`, `owner=replay_review`, purpose `replay_review` and no Agent step.
Read results with `spectragent.replay_review.storage.open_review(...).report()` or the
viewer's Reports & datasets view; do not walk uncommitted output files as if valid.

Review configuration selects `include_screenshots`, `max_text_chars`, `max_links` and a
versioned policy. Images are raw evidence, without the Agent's cursor decoration.
For context overflow, retain the failed job and use a new explicitly bounded config and
output directory. A prior small trial succeeded with 6,000 text characters and 20 links
per view; that is a measured starting point, not a universal budget or silent truncation
rule. Check recorded clipping before interpreting the result.

## Interpret triage

Inspect the native Choice label, confidence, option probabilities and independent Noul
answer separately. The default policy requires confidence ≥0.9, equivalence ≥0.8 for an
acceptance candidate, and equivalence ≤0.2 for a confident difference. These are
provisional routing thresholds, not calibrated accuracy or runtime acceptance criteria.

| Route | Follow-up |
|---|---|
| `candidate_acceptable` | Check the supporting task/action facts; retain the original machine warnings. |
| `investigate_difference` | Locate causal misses, wrong action targets or missing resources before proposing rules. |
| `further_review` | Inspect screenshots, DOM, geometry and outcomes; retain low confidence, disagreement or missing evidence. |

Execution status and route are separate. HTTP/schema/context errors are unavailable
review evidence, never task failures. A missing required image causes abstention without
a model call. The workflow's agent performs further evidence review; the job does not
automatically invoke a stronger model or require a new user permission question.

## Close the improvement loop

Validate scoped YAML with positive and negative matcher cases and a new source-prefix
replay. Re-review affected pairs with the same task, model, question version and policy;
preserve before/after evidence and disagreements. Do not tune only the label/confidence,
mutate source traces, broaden unrelated rules or relax restoration thresholds to pass.

Historical v5 traces remain immutable and are rejected by the v6 reader. Use frozen
prepared evidence or their original-version tools for dated experiments; never infer a
missing call kind or add a compatibility shim. Saved-response routing is not a fresh
model evaluation. Historical measurements are in [clean-dom-jev.md](clean-dom-jev.md);
current integration evidence is indexed by the
[handoff](../../af-project-history/references/jev-integration-handoff.md).
