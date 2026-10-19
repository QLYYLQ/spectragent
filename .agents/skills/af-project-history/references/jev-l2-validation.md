# Jev follow-up: original LexBench L2 tasks

Measured on 2026-10-19, after implementation commit `01b8dbe` on `feature/jev-add`.
The user rejected the initial simple tasks and requested Xiaohongshu/Bilibili L2 tasks,
then added `deepseek-v4.1-flash-1017` alongside Luna to inspect native thinking.
This is experimental evidence, not a new architecture contract or calibrated acceptance.

Local delivery: [report](../../../../tmp/jev_lexbench_l2_comparison_20261019/report.md),
[structured results](../../../../tmp/jev_lexbench_l2_comparison_20261019/delivery.json),
and [thinking index](../../../../tmp/jev_lexbench_l2_comparison_20261019/thinking-index.json).
If local artifacts are absent, these are dated measurements, not reproducible evidence
available in the current checkout.

## Scope and outcomes

Original L2 tasks: Xiaohongshu 30 and 130 (hard), Bilibili 220 (medium; the only Bilibili
task in that local L2 split). Both cohorts used UI-TARS, four actions per step, up to
32 policy steps and authorized storage seeds in fresh contexts. Six source recordings
and six same-path replays closed with complete recording health; task and replay
success remained separate.

| Task | Luna | DeepSeek 1017 |
|---|---|---|
| 30, Japan travel note | Provider timeout at step 3; rubric 15/100 | Answer returned; rubric 96/100, with some inferential recommendations |
| 130, meal note | Provider timeout at step 2; rubric 10/100 | Repeated ineffective scrolling, 32-step limit; rubric 40/100 |
| 220, coin balance | Answer confused the two currencies; rubric 45/100 | Correct balance from account evidence; rubric 88/100, but did not visit the requested dedicated page |

One fixed DeepSeek judge assessed both cohorts against the original rubric. Its score
is not ground truth or whole-benchmark accuracy. In particular, the Bilibili passing
score does not establish compliance with the requested navigation path.

Native thinking was present on 69 of 73 DeepSeek source calls; none of the 13 main Luna
source calls returned native thinking. API reasoning remains separate from UI-TARS's
reply `Thought:` and is never reconstructed when absent.

## Replay and independent review

- 85 restoration checks: 52 `ok`, 33 `warn`. DeepSeek Bilibili accounted for 28 screenshot
  warnings and 27 URL mismatches, starting at step 3. Its final answer matching the source
  is not evidence of faithful environment restoration.
- DeepSeek task 30 had two video-frame warnings and 294 uncovered video Range requests.
  Task 130 retained the failed source behavior with 31 passing checks and one warning.
  No matcher normalization or screenshot threshold was loosened.
- Jev reviewed 39 selected aligned observations: 35 assessed, four timeouts;
  37 `further_review`, two `investigate_difference`, zero acceptance candidates.
  Long trajectories sampled up to eight evenly spaced pairs including the checkpoint;
  task 30's DeepSeek run reviewed all ten. All deterministic checks remain recorded.
- 2,393 source/replay files were hash-checked after review; none changed. Review calls,
  native answers, input clipping, errors and policy decisions remain in independent jobs.

## Limitations and harness correction

Initial Luna attempts used a 75-second call timeout and are preserved separately. An
exact recorded-prompt probe succeeded after 121.62 seconds with a 180-second timeout;
the main cohort still timed out later. DeepSeek used 120 seconds, an 8,192-token output
budget and default reasoning; Luna used 180 seconds, 4,096 tokens and low reasoning.
This is an operational comparison, not a controlled model ranking.

The local Python harness omitted `subset` when calling `AgentRuntime.execute`.
Strict CLI evaluation correctly refused those immutable traces. Separate public
evaluator calls bound explicit L2 references only after validating the benchmark,
task ID and exact original task text. That omission is disclosed in the batch evidence,
not repaired by rewriting traces or weakening the CLI. The preserved harness was then
corrected for future executions to pass `subset`; branch provenance reads the actual
source subset. The data-flywheel skill records this requirement.

Useful diagnostic leads are the pointer location during ineffective scrolling,
coordinate selection against the screenshot grid, page/menu readiness before coordinate
clicks, and video coverage/timing. They are not proof that request equivalence rules
should broaden. BranchLoop's final fresh policy call, runtime restoration and task
correctness must continue to be reported separately.

The replay-improvement skill now uses the formal `spectragent replay-review` job; its
old standalone HTTP helper and duplicate tests were removed in `ef2915e`. Runbook v6
corrections are in `d849233`. The 27 formal replay-review tests, skill validation,
relative links and CLI dry-run passed. All ten shipped runtime YAML action budgets
were checked as four. No production Python behavior changed in this follow-up.

## Subsequent rule audit

At the user's request, the two Jev `investigate_difference` cases (Bilibili steps 8 and
21) were manually reviewed. The differing pages and replay login overlay support those
judgments. The first screenshot difference is step 2, before the URL divergence.
The local [audit report](../../../../tmp/jev_lexbench_l2_comparison_20261019/rule-audit/report.md)
and `audit.json` preserve the supporting records and a rejected candidate.

The history page's correct `/x/web-interface/nav` response was selected but logged
`answer=failed`; a subsequent request in the same top-level history frame consumed a
response recorded for the message subdomain, with its `Access-Control-Allow-Origin`
and `Vary: Origin`. The saved screenshots show login-information retrieval failure.
CORS failure is a supported causal inference, not a captured console exception.

A narrowly scoped candidate keeping only Origin in the request-header tie-break passed
schema/level/source-scope checks but failed the real Matcher's necessary counterexample:
both baseline and candidate selected the same wrong response after six constructed
Origin requests against actual recorded responses. It was rejected before browser
validation and never applied. Current architecture gives recording order priority within
a key; headers only break ties. Vary/Origin response variants and failed-delivery cursor
consumption need a matching-contract decision, not a broad URL/header ignore rule.

The existing Xiaohongshu notes rules passed seven positive ID-change cases and 21
keyword/page/sort counterexamples from seven real requests. The relevant video had only
4,369,565 of 36,600,669 bytes recorded (11.94%); missing Range coverage cannot be repaired
by normalizing requests. Telemetry misses were not promoted into broad synthetic-204
rules. No new rule was accepted and the shipped rule directory remained unchanged.
