---
name: af-improve-replay-rules
description: Diagnose All4One browser recording and replay differences, assess whether URL/API matching or restoration criteria may change, and validate scoped YAML rule proposals against recorded evidence. Use for replay misses, restoration warnings, and evidence-driven rule improvement.
---

# Improve replay rules from evidence

Turn a recorded difference into a supported diagnosis and, when justified, a reproducible
rule change. “Repair recording”, “fix execution”, and “insufficient evidence” are valid
outcomes. A higher hit rate alone does not establish a better replay.

This is an offline operator workflow: Codex reasons over saved evidence and uses existing
AF commands. It does not put an LLM in request serving or rewrite rules during a run.
The default review path is evidence check → configured DOM cleaning and comparison →
Jev triage where task relevance remains uncertain → deeper visual/evidence review when
needed → causal request diagnosis → scoped rules → counterexamples and replay validation.
Honor the requested scope: diagnose, propose, or apply and verify. Existing authorization
persists; do not ask again for each authorized local edit. A proposal task produces a
candidate directory and diff; an apply task may update the selected rules after validation.

## Contracts and inputs

Locate and read the relevant current sections of `docs/architecture_design.md`: §4.1
restoration, §4.14 request matching/rules/logs, §4.18 independent replay review, and §5
recording health/committed readers.
Read `configs/replay_rules/README.md` for the YAML schema and use `af-data-flywheel` for
pipeline configuration/execution. Current architecture outranks historical runbooks and
reports. An undefined framework contract requires a design decision before coding it.

Identify the source recording, replay/branch run, checkpoint, actual YAML and loaded rule
version from supplied paths and provenance. Ask only for inputs that cannot be recovered.
Keep historical traces immutable. Rule-content edits need no config regeneration; changes
to receptors or annotated configuration fields do.

Read committed data through `RunTrace` and browser `ArchiveReader` consumers, not `env/`
directory walks. Useful entry points under `spectragent/`:

- `core/trace/reader.py`: manifest/config/result, step records, observations, screenshots,
  and calls. Check recording scope, gaps and `complete_through` before comparing.
- `core/capabilities/browser/replay/rule_snapshot.py::recorded_site_rules`: the rules
  actually loaded by a replay, including text/hashes; today's files are not its baseline.
- Browser `replay/replay_log.py::iter_decisions`, `iter_replay_log`, `diverged_steps`:
  serving decisions, rule and record identity, nearest candidates and document misses.
- Browser `replay/archive_index.py::iter_index_lines`, `load_archive`, and
  `archive_stats.py::body_completeness`, `site_rule_candidates`: source requests,
  responses, availability and hints. Hints are not equivalence proofs.

Separate task correctness, environment execution and recording health. `done` does not
establish login success or faithful replay; recording failure need not mean Agent failure.
Sparse steps are not contiguous history. Missing evidence and unmeasured fields are not
zero. Old trace formats must not be adapted merely to make this workflow run.

## Diagnose the first meaningful difference

Align source and replay through restoration correspondence. Inspect all restoration
reasons, raw measurements and retargeted actions. `verify: warn` saves failed checks and
allows continuation; it does not repair them, prove equivalence or suppress runtime errors.
Prioritize the first divergence while retaining later warnings and distinguishing
downstream consequences from independent failures.

Correlate events by time, cycle, target/session/frame, request and record identity.
Collection-step ownership is not action causation. `diverged_steps` detects main-frame
document misses only, not every restoration failure. Summary logs count represented
occurrences; disclose missing individual timestamps. A hashed body key cannot reveal
the original fields. Compare clean screenshots, not cursor-decorated model images;
low dHash distance does not prove equal click targets or focus.

The automatic near-miss report considers query differences, not every POST-body
difference. Compare the actual recorded request body with available replay evidence;
preserve empty values when decoding canonical parameters. Packed telemetry may be
misread as enormous query parameter names. Do not copy those strings into rules or
public reports, or infer that every suggested name is a real parameter.

| Evidence | Next decision |
|---|---|
| `no_match` with a near candidate | Compare actual requests/responses and endpoint semantics before proposing normalization. |
| Unrecorded URL, unavailable/corrupt body, uncovered Range | Repair capture/coverage or report missing evidence; rules cannot supply a missing response. |
| `too_large` | Distinguish a missing recorded body from a serving-size limit. |
| `sequence_exhausted` | Inspect polling/state progression and whether last-response reuse is justified. |
| `match_level_disallowed` | Inspect both permissions and semantic correctness before enabling a weaker match. |
| URL/signature/action/retarget/screenshot mismatch without a causal miss | Inspect active tab, focus, geometry, DOM, action outcome, settle evidence and timing. |
| Challenge/login, lost session, unsupported worker/WebSocket behavior | Diagnose that boundary; synthetic success does not fix it. |

## Use Jev to triage task relevance

Use the formal `spectragent replay-review` job with the `replay_reviewer` configuration
axis and its separate `decision_model` receptor. The shipped DMX configuration selects
`jev-1.14.0`; direct and OpenRouter configurations are also available. It reads recorded
evidence independently of the browser Agent. Read [the operational recipe](references/jev-workflow.md)
for aligned trace pairs, source configuration, native calls and conservative routing.
Complete deterministic matches can proceed with their evidence; use Jev for differences
whose relevance to the task needs interpretation, rather than paying for every snapshot.

The job derives the task, next actions, configured clean DOM views, link/geometry facts,
raw screenshots and explicit evidence gaps through committed readers. Keep prior
judgments and expected labels out of the request. Mark visual comparisons with
`visual_required: true`; missing pixels must not become a text-only acceptance.
For missing evidence, low confidence or disagreeing answers, continue with the
screenshot/DOM review below. If the route is unavailable,
report the failure and continue that evidence review; do not block independent diagnosis
or silently substitute a model.

`candidate_acceptable` means the difference may be non-blocking for the stated task;
`investigate_difference` prioritizes causal diagnosis; `further_review` calls for deeper
inspection by the agent conducting this skill. None authorizes dropping a meaningful
request field. The provisional confidence cutoff is a review-routing knob, not a
calibrated accuracy claim or a new runtime acceptance threshold. Further review does
not inherently require another user permission question.

## Complete the evidence review

Use machine checks to locate differences, then have the AI conducting this skill inspect
the task, corresponding actions, source/replay screenshots and DOM. Its task-level
assessment is not determined by the image-hash threshold alone. Keep the raw distances,
machine verdicts and warnings unchanged alongside that assessment.

AF already compares interactive-element signatures when `browser.dom` is available.
Vision-only Agents may have no negotiated DOM fragment while `browser.dom_raw` still
archives the same observation cycle. Read it through the committed archive interface;
use `capture_from_document` and the existing cleaners when a normalized view helps.
Align capture cycle/time, tab, document URL/loader evidence and action correspondence
before comparing. Across browsers, backend node IDs are not semantic identities.

For fast comparison, reconstruct `TargetCapture`, run the configured cleaner, then the
existing `DOMBudgetPass` and serializer. Keep the resulting `DOMState` for geometry and
link checks, and use its text view for a compact comparison/model input. Normalize only
the serializer's transient element handles/new-node markers, including shadow/scroll
prefixes; preserve page text and meaningful attributes. Save derived views outside the
immutable source trace and report applied budgets. Equal text does not establish equal
link destinations, geometry or purely visual properties. See
[clean DOM and Jev trial](references/clean-dom-jev.md) for the measured offline workflow.

Match task-relevant text, roles, input values where available, result links/order,
target geometry, visibility and occlusion. Screenshots establish what was visible;
DOM presence alone does not prove visibility. Check that the corresponding action
reached the intended control and produced the expected state. Keep unavailable frames,
redacted values, missing captures and contradictory evidence explicit. A comparison of
two empty extractions is not evidence of equivalence.

Report **acceptable for this task**, **material difference**, or **insufficient evidence**,
with the step/artifact references, the changed region or elements, and why they affect
the task. For example, a closed search-history panel can be non-blocking when the next
action directly types the query and the same results return; it is material if the task
requires selecting an item from that panel. An empty result list in place of recorded
results is material even with zero document misses: a failed JSON subresource can cause it.

Do not attribute differences to recommendation algorithms without checking the evidence.
Independent live runs may receive different recommendations. Offline replay of one
archive serves saved responses; rendering/animation/focus/timing, request selection and
missing resources must be distinguished from fresh server-side recommendations. Compare
the recorded result identities and content before accepting a result difference.

This AI review belongs to the offline skill. Replacing the runtime restoration judge
with an LLM or introducing semantic verification as a framework strategy is separate
implementation work; do not silently rewrite saved restoration verdicts or relax global
thresholds merely to make a report green.

When testing a decision model, freeze expected labels separately before submitting cases;
send actual task/evidence without prior conclusions. Preserve raw responses, input
selection, model identity, latency and usage. A choice, its confidence, a probability
distribution and a separate yes/no answer are distinct outputs; conflicting answers or
low confidence require further review. Oversized input is an evaluation failure, not a
negative verdict. Reduce irrelevant evidence explicitly and retain the failed attempt.

## Review what may change

For each candidate, state the differing field's meaning, supporting request/response
evidence, proposed scope, expected benefit, and a counterexample that must remain distinct.
An LLM proposes and reviews this reasoning; confidence or parameter names alone are not
evidence. Inspect response consumers too: an ignored nonce may require a supported echo.

Use the narrowest supported host, method, path/URL and port scope. IDs, timestamps,
pagination, dimensions, search terms, session/authentication values and GraphQL variables
can select content. Evidence at one telemetry endpoint does not cover its domain's APIs.
Use `keep_*` for meaningful fields where useful. If the schema cannot express the needed
distinction, report the limitation rather than broadening the rule to fit it.

Each rule has a unique identity, explicit priority and exhaustion policy. One complete
winning rule applies; narrower rules do not inherit the broad rule's content settings.
Recorded/live requests must select the same rule identity. Run-level match permissions
bound rule permissions. Inspect `repeat_last`, fuzzy, synthetic 204, response echo and
JSONP rewriting as separate assumptions. Never add live fallback to the archive source.

Keep two kinds of change separate:

- **Request equivalence:** change scoped matching so the correct recorded response is
  served. Validate positive matches and false-match counterexamples.
- **Restoration acceptance:** change what difference is acceptable, such as a screenshot
  threshold. This may be justified by task-irrelevant animation, but requires visual and
  task evidence. Preserve old/new verdicts and raw distances; reclassification is not
  increased fidelity. Per-URL verification, dynamic masking or semantic verification
  require a design decision if the current framework does not implement them.

## Validate and feed back

Use representative samples: the failure, an affected passing case, and a semantic
counterexample. Prefer a second source run as a holdout when available; otherwise label
the conclusion as local. Do not require a full Agent × benchmark matrix.

1. Parse the complete candidate set with `SiteRules.load`. Validate run levels and the
   observed source/replay requests with `validate_levels` and `validate_requests`;
   parsing alone does not establish conflict-free scope or permission compatibility.
2. Exercise the real matcher on positive and negative cases. Inspect newly merged keys,
   selected records, response order and exhaustion. A new hit can be the wrong payload.
   Add a focused behavioral regression when changing shipped rules.
3. Replay the same source prefix with baseline and candidate rules. Hold checkpoint,
   actions, seed, viewport, initial state, pacing and verification thresholds fixed for
   a rule comparison. Use dedicated offline replay (`lifetime: run`) and full logs.
   Discover the current same-path YAML/CLI from the runbook: `spectragent run` does not
   supply BranchLoop checkpoints. Count any new policy calls separately from restoration.
4. Compare first divergence, reached/planned checks, complete prefix, all reasons and raw
   measurements, document/subresource misses, synthetic replies and exhausted reuse.
   Include task-specific semantic evidence and recording health. Unreached steps stay
   visible in the denominator. Test criterion changes separately from rule changes.
5. For cases triaged by Jev, review the candidate with the same task, questions, model
   and evidence-selection policy used for the baseline. Preserve both reviews and any
   visual adjudication. A changed model label alone does not establish a repaired replay;
   the matched responses, task evidence and negative cases still decide the rule change.

Use the task's time/token/iteration limits. Stop on a verified repair, exhausted budget,
or missing evidence required for the next hypothesis. Without an iteration budget, perform
one supported candidate round and report its result; do not start unbounded optimization.
An unexecuted candidate is not a validated repair.

Deliver source/replay IDs, first affected step and evidence references; candidate diff
and rationale; baseline/candidate hashes; positive/negative/holdout results; replay
comparison and uncertainty. Mark changes proposed, validated, applied or rejected.
Preserve original rules and both run artifacts for reproduction/rollback. Keep private
trace/session data out of reusable rule comments and published reports.
