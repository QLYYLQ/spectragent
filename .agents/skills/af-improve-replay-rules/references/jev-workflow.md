# Jev in the replay improvement workflow

Use Jev after the source/replay evidence is aligned and cleaned, before deciding which
differences require a rule change. The operating model is `jev-1.14.0`; the verified DMX
endpoint is `https://www.dmxapi.cn/typesafe/v1/systemone`. This is a skill helper, with
no framework registrar, runtime interception or modification of saved verdicts.

## Prepare one review case

Create a local JSON document with the actual task, next action, observations and evidence
limits. An observation can contain inline `text` or a UTF-8 `text_file` (relative to the
case file). Include selected link targets or geometry when the task needs them; do not
attach the browser storage seed, credentials, raw request headers or the whole archive.
The caller supplies already selected/redacted evidence; the helper does not clean DOM.

```json
{
  "task": "Read the first three search results for the recorded query",
  "next_action": "Read titles and authors",
  "reference_observation": {"text_file": "source.cleaned.txt", "budget_cut": null},
  "replay_observation": {"text_file": "replay.cleaned.txt", "budget_cut": null},
  "evidence_notes": "Clean DOM text; screenshots are retained separately, not supplied here.",
  "evidence_refs": {"source_step": 4, "replay_step": 4},
  "visual_review_required": false
}
```

For absent evidence, use `{"available": false, "reason": "..."}`; an empty string alone
does not explain whether the page was empty or capture failed. Keep expected labels
separate. `evidence_refs` and `visual_review_required` guide local routing/provenance and
are not sent as hints to the model. Do not silently truncate the DOM to fit context.
Long advertising URLs caused an input failure in the initial trial; omit such details
only when irrelevant to the particular task, and describe the omission.

## Run and inspect

From the AF repository, with the existing DMX credential in the calling environment:

```bash
uv run python .agents/skills/af-improve-replay-rules/scripts/jev_review.py \
  --case path/to/case.json --out path/to/new-review-directory
```

The default credential variable is `OPENAI_API_KEY`, shared with the existing route.
`--api-key-env`, `--model`, `--endpoint` and `--timeout` are explicit overrides.
`--dry-run` saves the exact request without a model call. The output directory must be
new; it contains private `request.json`, `response.json` and `review.json`, including
request digest, model/endpoint, elapsed time, raw answers and reported usage. Authentication
headers are never saved. The helper makes one request and has no hidden retry loop.

Inspect the native choice, confidence, probability distribution and independent `noul`
answer separately. They are not one interchangeable score. Routing uses a provisional
`--confidence-min 0.9`, recorded in the report:

| Route | Follow-up |
|---|---|
| `candidate_acceptable` | Check the preserved facts supporting non-blocking task equivalence. Keep the machine warning. |
| `investigate_difference` | Locate the causal miss, action/target issue or missing evidence before proposing a rule. |
| `further_review` | Inspect aligned screenshots, clean/raw DOM, geometry and outcomes. Use the workflow's AI reviewer; ask the user only when an actual decision remains unresolved. |

Low choice confidence, disagreement with the yes/no answer, missing observations,
visual evidence requirements or `insufficient_evidence` all require further review.
HTTP errors, malformed answers, unexpected model identity and context errors produce
an unavailable/invalid review, never a task failure. A context error can motivate a new,
explicitly reduced input in a new output directory; retain the failed attempt.

For testing only, `--response path/to/envelope.json` routes a saved
`{"status": 200, "response": {...}}` without a network call. Its report explicitly says
that this is saved-response routing and does not verify the answer's binding to the
newly prepared request. Do not count it as a fresh evaluation or bill its stored usage twice.

## Close the improvement loop

Jev selects review work; the skill's agent diagnoses causes and writes scoped YAML.
Require the existing positive/negative matcher tests and source-prefix replay comparison.
Re-review the affected candidate with the same task, model and question version. Preserve
the before/after evidence and any deeper visual assessment, including disagreements.
Do not optimize only the label or confidence, mutate old traces, broaden unrelated API
rules, or treat an unavailable model as permission to relax verification.

The measured trial and its limits are in [clean-dom-jev.md](clean-dom-jev.md). Formal
framework integration is the user's highest-priority follow-up and has its own
[handoff](../../af-project-history/references/jev-integration-handoff.md).
