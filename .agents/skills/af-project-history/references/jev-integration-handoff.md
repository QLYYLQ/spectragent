# Formal Jev integration — highest-priority continuation

**User ruling, 2026-10-19:** formal Jev integration into AF is the highest-priority next
framework work. Jev is also authorized as the fast offline review stage in
`af-improve-replay-rules`. Begin from this handoff rather than the old deferred-study label.

This handoff is not design authority. Read current `docs/architecture_design.md` before
core changes, especially §4.1 restoration, §4.6 recording/attribution, §4.7 models,
§4.11 independent evaluation, §4.12 DOM and §4.14 replay. Preserve the user-owned
`docs/jev_system_one_model.md` as dated research. Do not implement its historical proposals
or reuse its old runtime/metrics assumptions without current verification.

## What is established

- `jev-1.14.0` works through the user's DMX gateway at
  `https://www.dmxapi.cn/typesafe/v1/systemone`. Its native contract is
  state + typed questions → typed answers/probabilities/confidence + usage, not Chat
  Completions. Model identity was verified in returned responses.
- Existing raw DOM archives can be reconstructed with the configured cleaner, budget
  pass and serializer. Across 39 saved observations, median cleaning was about 28 ms,
  full read/clean/serialize about 47 ms; compact textual comparisons were sub-millisecond
  on selected pairs. No new browser or site request was needed.
- The small Jev trial froze labels before requests. Eight initial valid choices matched
  their labels; one oversized Bilibili input was retained as an error, then succeeded
  when irrelevant long advertising URLs were omitted while full clean text remained.
  Cases derive from two source tasks and include controls/repeats, not broad calibration.
- The same history-panel difference was acceptable for direct typing but material for
  selecting a history entry. However, direct typing had choice confidence 0.61 and an
  independent equivalence probability 0.26. Preserve this disagreement; do not collapse
  confidence, option probability and yes/no probability into one score.
- Native images were not tested in this trial. Old statements that Jev cannot see images
  are historical evidence; current capability support must be explicitly verified before
  exposing it to image consumers.

Current reusable implementation lives in the **skill**, not `spectragent/`:

- [af-improve-replay-rules](../../af-improve-replay-rules/SKILL.md) describes the workflow.
- `.agents/skills/af-improve-replay-rules/scripts/jev_review.py` prepares an exact request,
  preserves native outputs and routes uncertainty to further review. It never edits
  rules/traces and has no automatic network retry. Its cutoff is provisional routing,
  not calibrated acceptance. The adjacent tests cover disagreement and failure paths.
- `.agents/skills/af-improve-replay-rules/references/jev-workflow.md` is the operator recipe;
  `references/clean-dom-jev.md` preserves the experiment and limits.

Local evidence, when present:

- `tmp/jev_dom_review_20261019/delivery.json`, `clean.py`, `evaluate.py`, frozen evaluation
  plan and raw requests/responses; the Bilibili compact follow-up remains separate.
- `tmp/replay_rules_login_20261019/delivery.json`: authenticated Bilibili/Xiaohongshu
  recordings and baseline/candidate replay. Scoped Xiaohongshu rules restored the same
  19 result links/titles/order; visual warnings remain separate from task relevance.

These artifacts can contain private browser evidence. Do not commit raw inputs, login
seeds or account data while preparing framework tests; use synthetic equivalents.

## Decisions to settle before core implementation

1. **First consumer and scope.** The concrete validated use case is offline replay
   triage. Decide which existing evaluation/review component should own it. Current
   architecture keeps judges out of prefix restoration; a runtime judge or cascade is
   a separate design change, not implied by this priority.
2. **Protocol and registration.** Decide whether a decision-model contract/registrar is
   separate from chat models, or whether explicit capability/compatibility contracts can
   make a shared axis sound. Keep native typed questions/answers. Do not register a
   non-generative endpoint where unrestricted chat/free-text consumers silently accept it.
3. **Configuration.** Put the receptor on its real consumer. Define model/endpoint,
   question specification, review policy, input selection and budgets through existing
   configuration composition. Revalidate current wiring, not the old memo's line numbers.
4. **Results and identity.** Preserve choice, distributions, confidence, score/legend and
   noul as their actual types; include model identity, question/prompt version, evidence
   scope/digest and reported usage. Define what missing fields and unsupported question
   types mean. Do not force probabilities into prose or discard them.
5. **Attribution and persistence.** Reuse the implemented caller/purpose/model lifecycle
   and recording boundaries. Offline instrument calls must not become Agent decisions
   or thoughts. Decide where standalone review records belong and which channels they
   require; no second emitter or ad hoc persistence path inside Loop/Capability.
6. **Failures and escalation.** Define unsupported input, context overflow, HTTP/schema
   failure, timeout, cancellation, incomplete evidence, low confidence and disagreement.
   Unavailable is not a negative task verdict. Client reuse/ownership, bounded cleanup,
   retry budgets and honest unknown usage need the existing lifecycle contracts.
7. **Calibration and acceptance.** Use representative real traces plus semantic negative
   cases; keep labels out of requests and show valid-verdict coverage. Repeats of one
   source are not independent holdouts. The user's sampled-acceptance policy applies;
   do not reintroduce a full Cartesian matrix as a prerequisite.

## Suggested next working sequence

Inspect current protocols, registrars, composition, call context and evaluation readers;
then present a concrete integration design resolving the decisions above with the user.
Record agreed contracts in `architecture_design.md` before implementing unclear parts.
Only then implement the chosen axis/consumer, update registrar tables if needed,
regenerate through the one generator and run relevant compatibility/lifecycle/persistence
checks plus bounded real-route acceptance. Keep the current skill usable as the initial
operator workflow while the framework contract is settled.
