# Enhance and fix work after Jev integration

User ruling, 2026-10-19: the Jev feature phase is accepted for merging into `main`.
Subsequent work focuses on enhancements and fixes. This ledger records that queue;
`docs/architecture_design.md` remains design authority and `docs/execution_dag.md`
continues to govern dependencies. These items are not implemented by recording them.

## Enhance — action effects and observation layered by consumer

**Design settled 2026-10-20; implementation on `enhance/action-effects-and-capture-state`.**
The contracts are in `docs/architecture_design.md` §4.4 (layering, `capture_state`) and
§4.5 ("动作效果"). This entry records why, not a second copy of the design.

**Correction.** The 2026-10-19 wording said every Agent using BrowserCapability gets
one browser observation, UI-TARS included. That was a misrecord: UI-TARS is a
cross-platform computer-use Agent and consumes only platform-neutral contracts;
Skyvern and BrowserUse are browser-specific. The user confirmed the correction.

User decisions in the 2026-10-20 design discussion:

- Information is split by consumer. Action effects go to every Agent; cross-platform
  capture state (settle, foreground change, pointer, failed visible resources) goes to
  every Agent as an optional `capture_state` fragment; browser page state (URL, title,
  tabs via `browser.browser_meta`) goes only to browser-specific Agents, as required.
- Negotiation rules stay unchanged. Each Agent declares what it consumes.
- Per-action effects live on `ActionResult.effect`, defined by a nested `Effect` model
  on the action class, symmetric with `Params`. Read-back uses the existing
  `ActionRegistrar` by action key; no new registrar (the user weighed registrar growth
  against the rejected Protocol explosion and chose this).
- Each effect provides a one-line `describe()`. Agents render that line, so a
  cross-platform Agent never needs browser types. Shared agent-side renderers
  (`render_capture_state`, `render_browser_state`) follow `render_browser_platform`;
  an abstract intermediate class was rejected because subclass wiring entries replace
  inherited ones and it would tie UI-TARS to the browser.
- `capture_state` is a separate fragment, not fields on the screenshot, so a failing
  measurement cannot degrade the required screenshot.
- Scope this round: pointer, text-input, scroll, navigation, select and wait effects
  (17 browser actions minus done, screenshot, extract_content). Media playback time is
  deferred to the media restoration fix. The `send_keys` validation fix lands on the
  same branch as its own commit.
- Acceptance includes real model runs: UI-TARS + DeepSeek v4.1 flash headless on the
  five frozen L2 tasks against the 2026-10-19 batch as baseline, same-path replays, and
  one short live task each for Skyvern and BrowserUse. Raw trajectories judged to have
  no value after acceptance may be deleted; the acceptance report stays.
- After implementation, a separate clean-context review/refine pass runs, then the
  related skills and docs are updated.

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

Scheduled on `enhance/action-effects-and-capture-state` as its own commit; the rule is in
`docs/architecture_design.md` §4.13.


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
