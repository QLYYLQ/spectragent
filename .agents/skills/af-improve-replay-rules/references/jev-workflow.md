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

A replay recorded before site rules declared `unavailable` cannot be prepared: its recorded
rule snapshot no longer validates, and the pair reports `corrupt_evidence`. Replay it again
with the current rules; do not adapt the old snapshot.

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

The job writes its own indexed `review-manifest.json`: the configuration snapshot (the three
review blocks with their digests), evidence references, native Decision answers and calls.
Calls carry `kind=decision`, `owner=replay_review`, purpose `replay_review` and no Agent step.
The summary counts `statuses`, `routes`, `reasons` and `threshold_scopes`. Read results with
`spectragent.replay_review.storage.open_review(...).report()` or the viewer's Reports &
datasets view; do not walk uncommitted output files as if valid.

## Review configuration

The `jev` reviewer has three required blocks and no code defaults. Each has a content
digest (SHA-256 of canonical JSON); a version label is for people, the digest decides.

- `evidence`: `done_answer` (`withhold` removes the source `done` answer's text, keeps the
  action type), `max_text_chars` and `max_links` per observation side, and
  `include_screenshots`. The shipped budget, 6,000 characters and 20 links, is the one the
  thresholds were fitted on; larger budgets exceeded the DMX input limit on most fitted
  pairs. Images are raw evidence, without the Agent's cursor decoration. Action effects
  and serving facts never enter the request.
- `questions`: `version` and `items`, question ID → native Decision question. The shipped
  `replay-relevance-2` asks three independent Noul questions: `targets_equivalent`,
  `information_equivalent`, `evidence_sufficient`. Change the version when you reword.
- `policy`: `version: replay-triage-2`, `information_question` (must be a Noul in the set),
  default `thresholds` (`accept_information_min`, `investigate_information_max`, `fit`) and
  `overrides` (required, may be `[]`). An override is `{scope: site, domain: <registrable
  domain>, ...}` (that host and its subdomains; longest domain wins) or `{scope: task,
  benchmark, subset, task_id, ...}`, with the same threshold fields. Task beats site beats
  default.

Every threshold set's `fit` records the dataset and its digest, the date, the policy
version, the question-set digest, and the evidence options with their digest. Composition
refuses a set whose question or evidence digest differs from the configured blocks, naming
both; re-fit, or write a new fit entry. For context overflow, retain the failed job and use
a new explicitly bounded config and output directory — a new budget means a new evidence
digest and therefore a new fit. Check recorded clipping before interpreting a result.

## Interpret triage

Only the information question's native Noul routes; the other two answers are recorded.
There is no overall Choice, confidence or verdict. Routing (`route_result(result,
settings, facts)`, pure and re-runnable offline) checks in this order, with the shipped
default thresholds:

| Route | Condition (shipped default) | Follow-up |
|---|---|---|
| `investigate_difference` | An in-viewport image the replay did not serve, or information ≤ 0.25 | Locate causal misses, wrong action targets or missing resources before proposing rules. The image gate holds even when the model call failed. |
| `candidate_acceptable` | Information ≥ 0.45, restoration check `ok`, and no unserved in-viewport image measured | Review last, never skip: check the supporting task/action facts; retain the original machine warnings. |
| `further_review` | Anything else: between the thresholds, a restoration warning (`warn` or `diverged`), the image fact unmeasurable, missing evidence, service failure, answer-contract error | Inspect screenshots, DOM, geometry and outcomes. |

`reasons` names the deciding conditions; `thresholds` records the scope (`default`,
`site`, `task`) and fit actually used. The missing-image fact
(`facts.viewport_image_not_served`) combines two readings kept in
`facts.viewport_image_sources`: the replay log + raw DOM (an `Image` miss referenced by a
main-document element in the viewport) and the replay step's `capture_state` failed
visible images that the source step did not already have (`capture_state_baseline:
source_step`, or `none` when the source had no usable reading and every replay failure
counts). Either true makes it true; both unmeasurable is `null`. CSS background images and
images in subframes are not covered.

Execution status and route are separate. HTTP/schema/context errors are unavailable
review evidence, never task failures. A missing required image causes abstention without
a model call. The thresholds came from 12 labelled pairs on two development tasks; they
are provisional, not calibrated accuracy or runtime acceptance criteria. The workflow's
agent performs further evidence review; the job does not automatically invoke a stronger
model or require a new user permission question.

## Close the improvement loop

Validate scoped YAML with positive and negative matcher cases and a new source-prefix
replay. Re-review affected pairs with the same task and model and a configuration whose
question-set, policy and evidence-option digests equal the baseline job's (compare them in
the two manifests); otherwise the routes are not comparable. Preserve before/after
evidence and disagreements. Do not tune thresholds to move a label, mutate source traces,
broaden unrelated rules or relax restoration thresholds to pass. New labelled pairs are
for re-fitting the thresholds, recorded as a new `fit`, checked on runs other than the
ones they were fitted on.

Historical v5 traces remain immutable and are rejected by the v6 reader. Use frozen
prepared evidence or their original-version tools for dated experiments; never infer a
missing call kind or add a compatibility shim. Saved-response routing is not a fresh
model evaluation. Historical measurements are in [clean-dom-jev.md](clean-dom-jev.md);
current integration evidence is indexed by the
[handoff](../../af-project-history/references/jev-integration-handoff.md).
