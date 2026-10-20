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
  (13 of the 16 browser actions: all but done, screenshot, extract_content). Media playback time is
  deferred to the media restoration fix. The `send_keys` validation fix lands on the
  same branch as its own commit.
- No hidden configuration. Measurement budgets are BrowserCapability YAML fields
  (`action_effect_timeout_s`, `capture_state_timeout_s`); what the model sees is an
  Agent YAML switch (`show_action_effects`, `show_capture_state`, default true). The
  switch belongs to whoever renders the model input, as `screenshot_cursor` does on the
  capability; turning effects off keeps them in the trace so ablation arms cost the same.
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

**Implemented on `enhance/action-effects-and-capture-state`** (`tmp/action_effects_20261020/report.md`,
head `a2d868b`). User decisions on its open points, 2026-10-20:
- capture_state never fails the batched capture; probe problems become `unknown` inside the
  fragment. Keep this; update the design text and leave a short TODO in the capture code for
  allowing partial captures of optional fragments later.
- `show_capture_state` changes negotiation, so it is part of branch/export identity:
  recordings with different settings (including all 2026-10-19 recordings) cannot be
  branched or exported together. Accepted; formal data collection has not started.
- Effect lines keep element role and readable name for every Agent, UI-TARS included:
  desktop and Android expose similar accessibility facts, so this is not a browser-only
  channel. No extra detail switch for now.
- Fix the Skyvern index click whose element centre was off-screen (scroll into view first),
  and reword UI-TARS's platform block as a computer-use agent operating a browser, keeping
  browser-operation semantics.

Review decision, 2026-10-20: a scroll with no container moving is a measured zero only when
every container the wheel could reach was already at its edge; when one still had room and
no move was seen within `action_effect_timeout_s`, the position after and the edge fact are
`unknown` (the wheel may land after the budget).

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

**Experiment, 2026-10-20** (`tmp/jev_questions_20261020/brief.md`, 147 DMX calls): on the 28
labelled pairs a split information question separated the labels (acceptable never below
0.48, differences never above 0.24); a threshold chosen on the development tasks gave 0
holdout false accepts and 11/11 coverage. This is optimistic: the question names failure
kinds seen in the data, and with general wording Bilibili login/error pairs rose to
0.61-0.68. Withholding the source `done` answer does not repair the current questions;
serving facts in the request changed no route; an in-viewport replay image 404 rule alone
catches the missing-image pair without false alarms.

**User decisions, 2026-10-20 (implementation pending):**

- Question set `replay-relevance-2`: three native questions (actionable targets, task
  information, evidence sufficiency); only the information question routes, the other two
  are recorded. The overall three-way Choice is removed. The information question names
  login state, error pages, unrendered images and video frames, marked unvalidated until
  a new-task, new-site holdout.
- The source `done` answer text is withheld by default (YAML option). Serving facts stay
  out of the request; deterministic rules live outside the model.
- Policy `replay-triage-2`: candidate_acceptable needs information >= 0.45, no restoration
  warning and no in-viewport missing image; investigate_difference when information <= 0.25
  or an in-viewport missing image; otherwise further_review. Thresholds are provisional,
  and candidate_acceptable only orders review, never skips it.
- Implement the in-viewport image 404 rule in browser review evidence (replay log + DOM;
  CSS backgrounds not covered); also read `capture_state` failed images when present.
- Questions, policy and evidence options become required YAML fields with no code
  defaults; every job records their content hash; fix the hardcoded question version.
  Policy is a typed field on the Jev reviewer, not a registrar. Delete `replay-relevance-1`
  and `replay-triage-1` with no parallel path.
- Thresholds are a living parameter (user, 2026-10-20): keep collecting labelled pairs and
  re-fit them; per-site or even per-task thresholds are acceptable. So the policy carries a
  default plus optional YAML overrides scoped by site host or task, each recording the data
  and policy version it was fitted on. Keep fitting and checking on different runs so an
  error rate stays measurable; this replaces the earlier "two-annotator holdout before use".

**Implemented on `enhance/jev-split-review-questions`** (`tmp/jev_split_impl_20261020/report.md`):
required `evidence` / `questions` / `policy` YAML blocks with content digests, fitted
threshold provenance, the in-viewport image 404 rule, viewer update; the 28-pair job
reproduced the experiment's requests byte for byte and its routes 28/28. User decisions on
the follow-ups, 2026-10-20: shipped evidence budget 6000 characters / 20 links (the fit
budget); fit provenance records question-set and evidence-option digests and composition
refuses a mismatch; a failed model call with a true in-viewport missing image routes to
investigate_difference; an unmeasurable missing-image fact blocks candidate acceptance;
site overrides use an explicit registrable domain (`domain: bilibili.com` covers its
subdomains, longest match wins, no public-suffix list).

**Integrated with the observation branch, 2026-10-20:** the replay step's `capture_state`
failed visible images are the second source of the in-viewport missing-image fact, next to
the replay-log + DOM rule. Either source true makes the fact true; both unmeasurable leave it
unmeasurable; the facts keep each source's reading (`viewport_image_sources`) and the
evidence keeps the source step's own reading for comparison. Thresholds and gates unchanged.
User decision, 2026-10-20 (review pass): the capture_state source counts only failures new in
the replay — a replay failed image whose identifier is not among the source step's failed
images; with no usable source reading it counts every replay failure (conservative) and
records the fallback (`capture_state_baseline: none`). Kept as implemented (review
decision): replay failures past the 8 named identifiers count as new, a truncated source
list excuses only the names it lists, and matching is by file-name identifier.
Review decision, 2026-10-20: measured action effects never enter the Jev request; they are
stripped from the restoration correspondence, so requests stay identical to the fitted
evidence (the 28-pair rebuild is byte-identical to the earlier job). Showing effects would
need an explicit evidence option and a re-fit.

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

**Diagnosis, 2026-10-20** (`tmp/image_sequence_20261020/brief.md`): the page cancelled the
third request itself (`net::ERR_ABORTED`, no response). The source page displayed the eggs
from a different preview URL (`nd_prv`); the replay page ended on the larger `nd_dft` URL
and hit the cancelled record. Why the replay page chose `nd_dft` remains unknown. Across 11
recordings, 209 records lack a body (178 page cancellations); the same pattern occurs 14
times in 6 runs. The current behaviour is documented design: unservable matched records
consume the cursor, and `repeat_last` ignores availability.

**User decisions, 2026-10-20 (design and implementation pending):**

- Every site rule declares a required `unavailable: miss | serve_identical`, like
  `exhaustion`; `baseline.yaml` chooses `serve_identical`; all rule files gain the field,
  with no compatibility path.
- Substitution only when every available record in that sequence has the same status and
  an identical body; never for a recorded real network failure (fixed, not configurable).
  Logged as `substituted` with both record ids and the source outcome. Substituting for a
  page-cancelled request is a deliberate, logged deviation from the source.
- An unavailable record keeps its sequence position. Page-cancelled and recorder-missed
  bodies are both covered and counted separately. A later identical record may answer an
  earlier request (check task130 screenshots).
- `repeat_last` repeats the last available response; with none, a body-unavailable miss.
- With nothing to substitute, keep 404 and log the source outcome. Holding a request until
  the page cancels it is a separate later design.
- The recorder adds a start time to still-pending lines and an end time to finished
  exchanges; replay ordering stays as is until holding is decided.
- Acceptance: real offline replays of 238 and 130 (eggs visible, 130 checks not worse) and
  counterexamples that must stay unsubstituted.

**Implemented on `fix/replay-serve-identical-for-unavailable-records`**
(`tmp/replay_unavailable_20261020/report.md`): 238 step 11 distance 21 -> 0 with the eggs
visible, 130 unchanged at 30/30. Follow-ups decided 2026-10-20: records with an `unknown`
source outcome may be substituted; count `repeat_last` reuse per site next to
substitutions; mark repeats of a body-less record in the replay log; no headed-browser
verification of the headed 130 recording. Review decision, 2026-10-20: a consumed record
whose response status was recorded (headers arrived, body lost) is substituted only when
that status equals the available records' status; a record with no status stays eligible.

## Fix — Bilibili account request matching

The new Bilibili replay first diverges at step3. Its
`GET api.bilibili.com/x/space/wbi/acc/info` request differs from eight available source
requests only in `dm_img_inter` and `w_rid`; target `mid` and other fields match. Determine
those fields' semantics, preserve content/account selectors, and validate positive cases,
wrong-account counterexamples and a candidate replay before accepting any scoped rule.
No rule has been validated or applied for these fields. Subsequent login failures must
not inherit the older Origin/CORS diagnosis without fresh evidence.

**Diagnosis, 2026-10-20** (`tmp/bilibili_acc_info_20261020/brief.md`): `w_rid` is the WBI
request signature (md5 over the other fields, `wts` and a key; recomputed for all 8
records); `dm_img_inter` is anti-bot telemetry (window size and scroll mixed with random
numbers). `mid` selects the profile. The login overlay follows a chain: the `acc/info`
miss leaves the profile page without a scrollbar, the header shifts about 15 px, the step-4
avatar click misses, page visits fall out of step, and `/x/web-interface/nav` answers served
in recorded order reach the wrong page. Cookies and storage are not the cause. A scoped
candidate rule raised restoration from 3/30 to 24/30 and 25/30 in two replays.

**User decisions, 2026-10-20:**

- Add the `acc/info` rule (ignore only `dm_img_inter` and `w_rid`; `exhaustion: repeat_last`)
  to `configs/replay_rules/` with a regression test modelled on the Xiaohongshu rules test.
  Validation is local: one account, one recording.
- Validate the same-shape `arc/search` rule with a real replay; add it only if it passes.
- Do not chase the unidentified signing key of four first-visit requests until one blocks a task.
- `/nav` answers differ by requesting page but are served in recorded order. Design that
  after the image-sequence implementation lands (same sequence code), then ask the user.

## Fix — media playback restoration

Task30 source/replay frames include 00:41 versus 00:10 and different travel recommendations.
Playback time/content is task-relevant when the task asks for a media summary; matching
buttons is insufficient. Separate byte coverage, playback position and capture timing,
and define the supported restoration contract. Preserve warnings and actual frames;
do not hide the difference with a broader screenshot tolerance.

**Diagnosis, 2026-10-20** (`tmp/media_playback_20261020/brief.md`): the cause is playback
position. The video autoplays and kept playing while the source model thought (about 33 s
over steps 13 and 15); replay with `pace: none` does not wait. Byte coverage was complete in
the headless pair (41 of 41 ranges served) and screenshot timing matched. The existing check
passed step 19 at screenshot distance 4 against threshold 5 although the content differed.

**User decisions, 2026-10-20 (design and implementation pending):**

- Record, check and align. Media state (per element position, paused, duration) is
  screenshot-fragment metadata measured just before and after capture, with a
  BrowserCapability YAML time budget. Recording behaviour does not change.
- Restoration gains a "media differs" reason evaluated before screenshot distance:
  YAML time tolerance (default 1 s), paused-state and missing-element differences count,
  unknown values do not decide; the screenshot threshold is unchanged.
- Alignment: restorer YAML field `media: none | align`, default `align`, written explicitly
  in shipped same-path configs. It runs inside the capture after the settle wait and before
  the screenshot, through the capability's restoration interface, only in the restored
  prefix, never in the Loop and never after the checkpoint.
- Matching across runs: same frame, document order, viewport position; URLs compared only
  when not `blob:`; ambiguous means no alignment. Live streams, unreachable cross-site
  iframes, canvas players, seeks into unrecorded bytes and ambiguous matches are marked
  non-restorable, never passed.
- Agents: browser-specific Agents may render media state through `render_browser_state`
  behind an Agent YAML switch `show_media_state`, default off (an explicit experiment variable).
- Byte coverage is not extended; `range_uncovered` misses log the requested range.
- A real-Chrome probe of the site's player (seek and pause reliability) comes first; if it
  fails, land recording and checking first and defer alignment.
- Probe result, 2026-10-20 (`tmp/media_playback_20261020/probe/report.md`): alignment is
  feasible live and in offline replay within recorded bytes (exact position, 16-60 ms;
  checkpoint distances 9 -> 0 and the step-19 miss -> 0). Beyond recorded bytes a seek hangs
  silently: `currentTime` reports the target while `seeking` stays true and the frame does
  not change. Implementation consequences: success requires `seeked`, `seeking == false` and
  `readyState >= 2` within a YAML budget (default 2 s); record exact `currentTime`; compare
  paused state separately (screenshot hashes cannot); for a playing source, seek, play, then
  capture immediately (the player's control bar shows; note it in the alignment record);
  `range_uncovered` counts do not decide alignment.
- User decision: after a failed seek, seek back to the pre-alignment position and play state
  (stops the retry storm) and mark the step non-restorable. The unexplained self-pause
  in the headed run is investigated after media facts exist.

## Pending — source captures of half-loaded pages

Found 2026-10-20 (`tmp/jev_new_setup_eval_20261020/brief.md`): when the live source capture
hits the settle cap, the source screenshot shows a half-loaded page (e.g. Bilibili coin
balance 0, blank profile) while the replay, served from the archive, is fully loaded (687).
Waiting cannot make a replay less loaded; reproducing the source state would need network
timing replay, the kind of time control previously judged not worth shipping.

User direction, 2026-10-20 (decide after the observation branch lands, when `capture_state`
records capped settles):
- When generating data, avoid decisions on half-loaded pages: the Agent sees the capped
  settle and can call `wait` explicitly.
- With definite evidence of an incomplete load, filter such steps (e.g. do not choose them
  as branch checkpoints or export samples).
- Post-process trajectories for training: drop explicit `wait` steps and rewrite the
  preceding step's thought so the sample reads as a normal load.
- The labelling convention for such pairs in Jev calibration data is decided then too.
Boundaries to keep when designing it: recorded traces stay immutable; filtering and
rewriting happen in derived data (step selection, export) with provenance of every removed
step and rewritten thought; rewritten thoughts are marked synthetic.

## Fix — native scrollbar follows the host appearance

Found 2026-10-20 (`tmp/bilibili_acc_info_20261020/delivery.json`, `distance_zero_check`):
two replay sessions of the same Bilibili recording differ by exactly the 15-pixel native
scrollbar strip. Runs pin the page colour scheme to dark, but the headless Chrome scrollbar
apparently follows the Mac's automatic light/dark appearance, which no run records. Earlier
session frames differed from the source by 10,800 pixels (screenshot distance about 4); this
session by 0. Restoration counts across sessions are therefore not directly comparable.
User decision, 2026-10-20: no framework change now. The README states the limit (host
appearance is neither pinned nor recorded; keep it fixed for production runs and compare
restoration results only under the same appearance). Cause is inferred, not proven.

## Execution preference

Run browser experiments headless and quietly. The latest batch uses 300-second DeepSeek
calls, 1,800-second task limits and 90-second Jev calls. Fresh recordings and replays use
separate identities; historical artifacts remain immutable. See
[operations-and-exclusions.md](operations-and-exclusions.md) for the durable preference.
