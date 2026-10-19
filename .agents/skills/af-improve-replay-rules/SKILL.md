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
Honor the requested scope: diagnose, propose, or apply and verify. Existing authorization
persists; do not ask again for each authorized local edit. A proposal task produces a
candidate directory and diff; an apply task may update the selected rules after validation.

## Contracts and inputs

Locate and read the relevant current sections of `docs/architecture_design.md`: §4.1
restoration, §4.14 request matching/rules/logs, §5 recording health/committed readers.
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

| Evidence | Next decision |
|---|---|
| `no_match` with a near candidate | Compare actual requests/responses and endpoint semantics before proposing normalization. |
| Unrecorded URL, unavailable/corrupt body, uncovered Range | Repair capture/coverage or report missing evidence; rules cannot supply a missing response. |
| `too_large` | Distinguish a missing recorded body from a serving-size limit. |
| `sequence_exhausted` | Inspect polling/state progression and whether last-response reuse is justified. |
| `match_level_disallowed` | Inspect both permissions and semantic correctness before enabling a weaker match. |
| URL/signature/action/retarget/screenshot mismatch without a causal miss | Inspect active tab, focus, geometry, DOM, action outcome, settle evidence and timing. |
| Challenge/login, lost session, unsupported worker/WebSocket behavior | Diagnose that boundary; synthetic success does not fix it. |

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

Use the task's time/token/iteration limits. Stop on a verified repair, exhausted budget,
or missing evidence required for the next hypothesis. Without an iteration budget, perform
one supported candidate round and report its result; do not start unbounded optimization.
An unexecuted candidate is not a validated repair.

Deliver source/replay IDs, first affected step and evidence references; candidate diff
and rationale; baseline/candidate hashes; positive/negative/holdout results; replay
comparison and uncertainty. Mark changes proposed, validated, applied or rejected.
Preserve original rules and both run artifacts for reproduction/rollback. Keep private
trace/session data out of reusable rule comments and published reports.
