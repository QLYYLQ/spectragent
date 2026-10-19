# Jev / System One feasibility snapshot

Historical measured snapshot from 2026-09-18 against repository baseline `b02f770`;
not current design. The primary record is `docs/jev_system_one_model.md`, which is
explicitly unfinished and must be preserved. Read it completely before resuming the
research. Re-verify the provider API and model behavior before relying on any limit,
latency, price, model identifier, or modality claim.

The study asked whether a calibrated, non-generative decision model belongs in
`LLMRegistrar` or needs a separate `DecisionModelRegistrar`, and how calls made as an
experimental subject differ from calls made as an evaluation instrument.

Measured at the time:

- the API accepted a text state plus parallel `noul`, `choice`, and `score` questions
  and returned probabilities/confidence;
- 255 choices worked, 256 parallel questions were tested, and context failed around
  32k tokens;
- three attempted image encodings were silently ignored while HTTP 200 was returned;
- four synthetic browser-use steps showed promising confidence separation, but this
  was only an n=4 mechanism smoke test, not benchmark evidence;
- questions were answered in parallel, so one question could not depend on another's
  selected answer.

The document proposed but did not decide or implement:

- placing Jev in `LLMRegistrar` behind explicit compatibility gates versus a separate
  registrar;
- adding a confidence/probability result contract;
- a new attribution boundary for probe/action/judge model calls;
- a trace-only `SystemOneProbe` as the first experiment;
- a cascade Loop only after real-trace accuracy × coverage is measured;
- text-only evaluator triage.

At that date, no Jev implementation, config, tests, or reproducible probe existed in the repository.
The old memo's `SummaryMemory`, money-accounting, and some runtime statements were later
superseded; the current issue is model-call ownership and per-model token attribution,
not aggregating guessed monetary cost.

Current decision inputs are `docs/architecture_design.md` §§4.1, 4.6, and 4.7;
`docs/execution_dag.md`; and the approved environment-vs-subject ownership rule
in `docs/actions_that_call_an_llm.md`. The memo's proposed order was probe → real-trace
calibration → config-time compatibility gate → cascade. That order is evidence for a
future decision, not authorization to implement it.

## Distinguish the completed generic plumbing

The 2026-10-15 implementation supplies generic caller/purpose/model/step attribution
and prompt-helper model lifecycle. That closes the generic plumbing gap; it does not
resolve Jev's subject-versus-instrument ownership, registrar, result contract, probe,
calibration or cascade proposals. Read the preserved memo before resuming any of them.

## Later offline comparison trial (2026-10-19)

The user authorized a clean-DOM comparison experiment with `jev-1.14.0` through DMX.
Current measurements and reproducible local requests are in
`tmp/jev_dom_review_20261019/delivery.json`; the maintained recipe is
`af-improve-replay-rules/references/clean-dom-jev.md`. The DMX native decision endpoint
was verified. Eight initial valid choices matched prelabelled cases; one oversized
Bilibili input succeeded after its irrelevant link inventory was omitted. This does
not establish broad accuracy/calibration; a direct-typing case had conflicting choice
and yes/no outputs. It remains offline instrument research, with no new registrar,
runtime judge, or cascade implementation. The earlier memo's modality results remain
historical; this new trial sent no images.

The user's subsequent 2026-10-19 ruling promotes formal framework integration to the
highest-priority continuation. Use [jev-integration-handoff.md](jev-integration-handoff.md)
for that active work; the replay skill now includes a reusable native Jev review helper.
The historical proposals above remain choices to resolve, not an already selected design.
