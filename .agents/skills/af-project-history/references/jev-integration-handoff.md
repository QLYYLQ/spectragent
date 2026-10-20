# Formal Jev integration — highest-priority continuation

> **Feature phase accepted, 2026-10-19.** The user accepts the implemented Jev feature for merging into `main`; subsequent work is enhancement and fixes. Start with [enhance.md](enhance.md) for the common BrowserCapability observation decision and evidence-driven follow-ups, and [jev-l2-validation.md](jev-l2-validation.md) for the latest headless results. The pre-implementation discussion and open-design lists retained below are historical where the implementation update supersedes them; they are not an instruction to restart Jev integration.

> **Implementation update, 2026-10-19 — feature/jev-add.** Formal native Decision models and the independent replay reviewer are implemented. Current design is `architecture_design.md` §4.7/§4.18 and trace format 6; AgentResult remains 3.0. The earlier planning text below is retained history, not an unstarted implementation queue. Current local delivery: [jev_add/delivery.json](../../../../tmp/jev_add/delivery.json). Full validation: 8,264 passed, no failures/errors/skips, 22 existing warnings; Pyright 0/0; frontend 13 tests and build passed. Three Luna records and three same-path replays completed with max_actions_per_step=4, 7/7 restoration checks and pixel-identical observations. Three unservable jQuery requests remain explicit (the recording also lacks complete responses). Jev bounded review returned 7/7 valid results, 2 candidate accepts and 5 further reviews, including one low-confidence semantic error caught by disagreement handling. DMX and OpenRouter native routes were live-tested; official direct has HTTP contract coverage but no live credential. Dynamics remain a separate future design, not accepted by these small experiments.

**User ruling, 2026-10-19:** formal Jev integration into AF is the highest-priority next
framework work. Jev is also authorized as the fast offline review stage in
`af-improve-replay-rules`. Begin from this handoff rather than the old deferred-study label.

This handoff is not design authority. Read current `docs/architecture_design.md` before
core changes, especially §4.1 restoration, §4.6 recording/attribution, §4.7 models,
§4.11 independent evaluation, §4.12 DOM and §4.14 replay. Preserve the user-owned
`docs/jev_system_one_model.md` as dated research. Do not implement its historical proposals
or reuse its old runtime/metrics assumptions without current verification.

## Pre-implementation review document and DMX probes (historical, 2026-10-19)

The detailed, user-requested TODO handoff is
[HANDOFF_261019_jev_integration_todo.md](../../../../HANDOFF_261019_jev_integration_todo.md).
It is local-only and may be absent in a fresh clone. It records scope, unresolved
decisions, source files, staged TODOs, acceptance tests and the resume instructions.
No production integration was implemented in this discussion/documentation session.

The user has now accepted the proposed structure: one `core/llm` package with a common
model base, Chat/Decision subcontracts and separate registrars. Keep the existing
`LLMRegistrar` / `llm` axis for Chat and add `DecisionModelRegistrar` / `decision_model`.
Both subcontracts use typed `ainvoke` signatures; the parent owns identity/lifecycle,
not a catch-all invocation. Retain native fields and `RecordedLLMCall`'s endorsed
`chat | decision` semantics. The first consumer is independent replay review; its exact
owner, record container, detailed schemas and policies still need design. Shared-registry
whitelists and a Conscribe Protocol-filter extension are no longer the selected route.

The earlier phrase "not authorized" about additional behavior was too broad: synchronous
restoration gating, an Agent action-decision cascade Loop and automatically activating
candidate replay rules are workflows not yet designed into the first integration TODO.
This is not a requirement for approval on each model call or each rule change. The
Agent cascade is distinct from the reviewer's cheap triage followed by deeper review.

**Confirmed phase boundary, 2026-10-19:** the user explicitly chose offline replay
review first. After experiments pass, Jev may dynamically participate in replay as a
separate development task. Define and preserve the offline experiment criteria/results
now; do not add runtime restoration gates or placeholder switches in this integration.
The current small trial is not itself acceptance for dynamic replay. The exact later
control points and policies remain to be designed; this does not automatically scope
an Agent cascade or rule auto-activation into that later task.

New local evidence is
[jev_integration_assessment_20261019/assessment.json](../../../../tmp/jev_integration_assessment_20261019/assessment.json):

- An isolated Conscribe probe showed that a Chat Protocol annotation alone does not
  narrow Mode 1 wiring. Explicit key subsets reject non-Chat single/list entries.
  Follow-up documentation/parser verification confirmed that 2.1.0 already supports
  two-element key subsets and three-element required/optional key declarations;
  four-element tuples are rejected, and dict entries carry options such as defaults.
  An explicit subset needs no Conscribe extension. Automatic Protocol-based candidate
  selection is the optional enhancement being discussed, not an integration prerequisite.
  A further check confirmed existing `__skip_registries__` / registration filters:
  an implementation may stay in one registrar while being excluded from another.
  Chat and Decision can therefore use separate registrars within `core/llm`, sharing
  infrastructure and using ordinary wiring. The user subsequently selected this
  separate-registrar arrangement. Per-receptor
  `{"registry": "llm", "exclude": [...]}` is not supported by 2.1.0.
- DMX `jev-1.14.0` accepted structured state/instructions/criteria, array state and
  null Choice descriptions; Score legends retained structured objects.
- Three exact repeats of the restored-results input returned `acceptable`, confidence
  0.95–0.97 and equivalence noul 0.85–0.87. Direct-typing input had two valid repeats:
  `acceptable`, confidence 0.56–0.64, noul 0.23–0.30; another timed out at 35 seconds.
  That input contains no target bounds, focus or occlusion evidence.
- Correctly encoded native images **were tested in this later batch**. Swapping red/blue
  halves changed the left-is-red noul from 0.99 to 0.01, and Box moved with the target.
  Initial undersized synthetic images returned 400; corrected 640×320 images were
  recorded separately. This establishes pixel ingestion, not real-page visual accuracy.
- The valid image also succeeded without the experimental header required by official
  documentation. Whether DMX added the header or its upstream accepted it is unknown.
- Fourteen new requests produced ten successful, schema-checked responses, three
  invalid-image-dimension errors and one timeout. Reported usage was 87,907 input and
  654 output tokens; the four unreported usages remain unknown. No hidden retries.
- There were no fresh official-direct or OpenRouter calls. Cross-route equivalence,
  calibrated acceptance thresholds and broad replay accuracy remain unmeasured.

Official current documentation describes confidence as derived from the returned
distribution, not an independent correctness estimate. Native images/Box are experimental;
old blanket statements that Jev cannot see images no longer describe the measured DMX
route. Read the detailed handoff for primary-source links and the limits of these findings.

## Initial clean-DOM trial

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
2. **Protocol and registration.** The user selected the shared-base/Chat/Decision split
   with separate registrars; settle the detailed public contracts and validate receptor
   compatibility using the existing composition mechanisms. Keep native typed
   questions/answers. Do not register a
   non-generative endpoint where unrestricted chat/free-text consumers silently accept it.
3. **Configuration.** Put the receptor on its real consumer. Define model/endpoint,
   question specification, review policy, input selection and budgets through existing
   configuration composition. Revalidate current wiring, not the old memo's line numbers.
4. **Results and identity.** The user endorsed `RecordedLLMCall`'s `chat | decision`
   semantics. Preserve choice, distributions, confidence, score/structured legend,
   noul and image/Box fields as their actual types; include model identity, question/prompt version, evidence
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
