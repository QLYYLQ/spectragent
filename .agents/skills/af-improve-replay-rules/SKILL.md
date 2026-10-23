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
  `archive_stats.py::body_completeness`, `near_misses`, `site_rule_candidates`: source requests,
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
occurrences; disclose missing individual timestamps. A body key the canonicaliser
replaced by a digest cannot reveal the original fields. Compare clean screenshots, not cursor-decorated model images;
low dHash distance does not prove equal click targets or focus.

The automatic near-miss report (`decision_inputs.site_rule_candidates`) groups misses by
endpoint (host, method, path) and lists differing query parameters and body fields apart,
with each combination that differed together (`field_sets`). Both sides are keyed with the
replay's own recorded rules, so a field its rule already ignores is not listed. The replay
log keeps each live body key whole, so a body counts as `opaque_body` only where the
canonical form itself is a digest (over the 4096-character canonical cap, or unparsable):
the matcher had nothing finer either, and the fields are unknown. Compare the
actual recorded request body with available replay evidence then; preserve empty values when
decoding canonical parameters (`canonical.decode_params`). For replays outside a chain, point
a scratch root's `traces/record` and `traces/branch` at the runs and use
`spectragent flywheel report --root`. Packed telemetry may be misread as enormous query
parameter names. Do not copy those strings into rules or public reports, or infer that every
suggested name is a real parameter.

| Evidence | Next decision |
|---|---|
| `no_match` with a near candidate | Compare actual requests/responses and endpoint semantics before proposing normalization. |
| Unrecorded URL, corrupt body | Repair capture/coverage or report missing evidence; rules cannot supply a missing response. |
| `body_unavailable` miss or `substituted` entry (a recorded request kept without a body) | Read `source_outcome`: page-cancelled, in flight at run end, memory-cache and recorder-timeout records may be answered by an identical available record under `unavailable: serve_identical`; `network_failed` never is. A miss despite `serve_identical` means no available record, differing bodies or statuses, or a recorded status that differs. `repeated: true` marks repeats of an exhausted body-less record, not new gaps. A substitution for a page-cancelled request is a deliberate, logged deviation; check the page did not take a path the source did not. |
| `range_uncovered` | Group by `requested_range`: one start requested hundreds of times is one gap and a player's retries (often a media seek past the recorded bytes), not many gaps. Extend recording coverage or accept the gap; a rule cannot invent bytes. |
| `too_large` | Distinguish a missing recorded body from a serving-size limit. |
| `sequence_exhausted` | Inspect polling/state progression and whether last-response reuse is justified. |
| `match_level_disallowed` | Inspect both permissions and semantic correctness before enabling a weaker match. |
| URL/signature/action/retarget/screenshot mismatch without a causal miss | Inspect active tab, focus, geometry, DOM, action outcome, settle evidence and timing. |
| `media_mismatch` | Read `restoration.evidence[*].media` (comparisons, alignment outcomes, unreachable frames). A failed alignment beside `range_uncovered` for the media URL is missing recorded bytes, not a rule problem; compare `media: none` and `media: align` replays before blaming the matcher. |
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
For missing evidence, a failed call or an information value between the thresholds,
continue with the screenshot/DOM review below. If the route is unavailable,
report the failure and continue that evidence review; do not block independent diagnosis
or silently substitute a model.

The route comes from one native Noul, the information question, against fitted
thresholds, plus deterministic gates; there is no overall Choice or confidence.
`investigate_difference` (an unserved in-viewport image, or information at or below the
investigate maximum) prioritizes causal diagnosis; `candidate_acceptable` (information at
or above the accept minimum, a passing restoration check, no such image measured) means
the difference may be non-blocking for the stated task and only orders human review —
it is reviewed last, never skipped; `further_review` calls for deeper inspection by the
agent conducting this skill. None authorizes dropping a meaningful request field. The
thresholds are provisional, re-fitted as labelled pairs accumulate, not a calibrated
accuracy claim or a runtime acceptance threshold. Further review does not inherently
require another user permission question.

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
selection, model identity, latency and usage. Native outputs of different questions or
kinds (a Noul, a choice and its probabilities) are distinct; never average them, and keep
disagreement visible for review. Oversized input is an evaluation failure, not a
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

Each rule has a unique identity, explicit priority, and both required policies:
`exhaustion: repeat_last | miss` and `unavailable: miss | serve_identical` (a rule
missing either is refused; the framework supplies no default). One complete winning rule
applies; narrower rules do not inherit the broad rule's content settings, so a narrow
rule repeats both policies.
Recorded/live requests must select the same rule identity. Run-level match permissions
bound rule permissions. Inspect `repeat_last`, fuzzy, synthetic 204, response echo and
JSONP rewriting and `serve_identical` substitution as separate assumptions. Never add
live fallback to the archive source.

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
   measurements, document/subresource misses, synthetic replies, exhausted reuse and
   substitutions by source outcome.
   Include task-specific semantic evidence and recording health. Unreached steps stay
   visible in the denominator. Test criterion changes separately from rule changes.
5. For cases triaged by Jev, review the candidate with the same task and model and a
   configuration whose question-set, policy and evidence-option digests equal the
   baseline job's (each assessment records them); otherwise the two routes are not
   comparable. Preserve both reviews and any
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
