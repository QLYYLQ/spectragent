# Enhance and fix work after Jev integration

User ruling, 2026-10-19: the Jev feature phase is accepted for merging into `main`.
Subsequent work focuses on enhancements and fixes. This ledger records that queue;
`docs/architecture_design.md` remains design authority and `docs/execution_dag.md`
continues to govern dependencies. These items are not implemented by recording them.

## Enhance — common browser observation

**Accepted direction.** Every Agent using an environment with BrowserCapability
receives the same structured browser observation contract through `env.observe()`.
The common information is browser-owned environment behavior, not an opt-in repair
implemented separately inside UI-TARS, BrowserUse or Skyvern. The baseline is available
when the browser capability is present; Agents retain ownership of prompt presentation.

The common information covers:

- Current browser state: screenshot, URL, active tab, viewport/coordinate geometry,
  pointer position, focused control and capture/settle status.
- Observable action effects: actual target/dispatch point, scroll displacement and
  boundaries, focus/value changes, navigation/tab changes, and loading/error facts.
- Explicit evidence quality: measured zero, unknown, unavailable, truncated and stale
  facts remain distinct. Sensitive values are not exposed.

ActionResult continues to report each action's execution outcome. Browser observation
reports the resulting environment facts; task achievement remains an Agent/evaluator
judgment. One screenshot after a multi-action batch does not attribute a change to an
individual action. Additional per-action measurement must establish that attribution.

**Design work before implementation.** Keep registered fragments, resolver ownership,
same-cycle capture, persistence/reconstruction and negotiation. Define how the common
browser baseline enters the negotiated result automatically, its typed fields and
required/optional failure behavior, and budgets for expensive target/focus/media facts.
Do not implement the direction by bypassing negotiation, putting browser fields on every
environment's observation, or introducing a single untyped state bag. Browser-specific
facts remain under the browser capability; other capabilities retain their contracts.

**Acceptance.** UI-TARS, BrowserUse and Skyvern receive the same measured facts from the
same browser state without duplicating browser acquisition logic. Validate normal and
ineffective scrolling, boundary conditions, wrong target/focus, invalid input, loading
failure, tab navigation and unavailable evidence. Record/replay/history reconstruction
retain the same facts. Compare repeated ineffective actions and task outcomes on frozen
tasks; changing the information available to an Agent is an explicit experiment change.

Evidence: [headless follow-up report](../../../../tmp/jev_headless_calibration_20261019/report.md)
and `action-feedback-findings.json` beside it. Task130's pointer at (45,34) did not stop
scrolling: measured offsets increased from 0 to 2100. The earlier contrary inference was
corrected; use actual displacement as evidence, not pointer location alone.

## Enhance — Jev questions and calibration

The native Decision providers, typed results, independent review jobs, persistence and
viewer are implemented. Operational reliability of semantic replay judging is separate.
The 28-pair follow-up has 20 acceptable and eight material-difference labels, assigned by
one Codex adjudicator before requests. Baseline Choice agrees on 24/28; compact evidence
on 21/28, with 44.5% fewer reported tokens. Both route zero pairs to candidate acceptance,
27 to further review and one to difference investigation. The samples are clustered and
warning-enriched; these figures are not general accuracy or a calibration guarantee.

Prioritize independent questions for actionable targets, task-relevant information,
focus/occlusion, loading/media state and evidence sufficiency. A visible difference is
material only in relation to the stated task/action. Preserve the separate native Choice,
option probabilities, confidence and Noul outputs. Confidence is not domain accuracy.
Do not average unlike outputs or tune the global thresholds merely to increase acceptance.

Use fixed controls and counterexamples: a suggestion panel away from a direct search
submission; the same panel when selecting a suggestion; a missing image containing the
answer; equal video controls at different task-relevant frames; authentication loss.
Check whether including a source `done` answer biases the review toward equivalence;
this is a hypothesis, not an established cause of the measured errors. Keep questions,
evidence policies and task-level holdouts versioned. Runtime participation and automatic
rule activation remain later design work, not consequences of merging the feature.

## Fix — keyboard argument validation

Bilibili source `deepseek-hl-l2-220-baseline`, trace step20, submits a whole URL as
`send_keys.keys` and receives success. The current unknown-multichar-key path sends one
key with `char_text=None`; it does not type that text. Define and enforce valid key/chord
inputs, preserve legitimate named keys, and return a useful invalid-input error naming
the text-entry alternative. Do not turn arbitrary absent page changes into input errors.

## Fix — image response availability and sequence

Task238-s11 lacks the image containing four eggs in replay. The same exact URL has two
available 200 response bodies with identical digest, then an incomplete record; replay
serves the latter as a strict-miss 404. The earlier bytes are readable and visually
verified. Investigate why the third source request was incomplete and define response
sequence/availability semantics before changing the matcher. Broader URL matching cannot
repair this exact-key failure. Evidence: `image-sequence-audit.json` in the headless batch.

## Fix — Bilibili account request matching

The new Bilibili replay first diverges at step3. Its
`GET api.bilibili.com/x/space/wbi/acc/info` request differs from eight available source
requests only in `dm_img_inter` and `w_rid`; target `mid` and other fields match. Determine
those fields' semantics, preserve content/account selectors, and validate positive cases,
wrong-account counterexamples and a candidate replay before accepting any scoped rule.
No rule has been validated or applied for these fields. Subsequent login failures must
not inherit the older Origin/CORS diagnosis without fresh evidence.

## Fix — media playback restoration

Task30 source/replay frames include 00:41 versus 00:10 and different travel recommendations.
Playback time/content is task-relevant when the task asks for a media summary; matching
buttons is insufficient. Separate byte coverage, playback position and capture timing,
and define the supported restoration contract. Preserve warnings and actual frames;
do not hide the difference with a broader screenshot tolerance.

## Execution preference

Run browser experiments headless and quietly. The latest batch uses 300-second DeepSeek
calls, 1,800-second task limits and 90-second Jev calls. Fresh recordings and replays use
separate identities; historical artifacts remain immutable. See
[operations-and-exclusions.md](operations-and-exclusions.md) for the durable preference.
