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

No Jev implementation, config, tests, or reproducible probe exists in the repository.
The old memo's `SummaryMemory`, money-accounting, and some runtime statements were later
superseded; the current issue is model-call ownership and per-model token attribution,
not aggregating guessed monetary cost.

Current decision inputs are `docs/architecture_design.md` §§4.1, 4.6, and 4.7; the M18
gate in `docs/execution_dag.md`; and the approved environment-vs-subject ownership rule
in `docs/actions_that_call_an_llm.md`. The memo's proposed order was probe → real-trace
calibration → config-time compatibility gate → cascade. That order is evidence for a
future decision, not authorization to implement it.
