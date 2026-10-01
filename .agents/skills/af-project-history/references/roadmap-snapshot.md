# Roadmap snapshot from 2026-09-17

This is a dated user-priority snapshot, not the current scheduler. Before using it,
compare it with `docs/execution_dag.md`, current tests, and the user's latest request.

The requested order at that time was:

1. Verify `ui_tars`, `skyvern`, and `browser_use` behavior against their official
   cases and the M18 benchmark gate.
2. Survey recent computer-use/browser-use work, then add worthwhile memory and
   orchestration strategies on the existing registries.
3. Audit the six lite cloud-browser SDK wrappers specifically for proxy, profile,
   fingerprint, and stealth options; avoid version chasing that does not affect the
   narrow session-lifecycle surface AF consumes.
4. Only then open the Android capability line, using it to validate the
   multi-capability abstraction rather than treating AndroidWorld as an isolated port.

The user explicitly rejected building a cross-run resource pool first. A run-local
resource may retain a session with an explicit reset contract, but a pool that outlives
the run conflicts with zero global state and weakens task isolation.

This later ruling superseded the older resource-pool proposal in
`docs/external_references_guide.md`. Its premise was that the runner remained sequential
and composed a fresh stack per task; revisit the ruling if that premise changes.

## Assumptions that have since changed

The original memory claimed the simple memory strategy had no bound and that async
compression strategies were unreachable. Current code has `max_messages`,
`sliding_window`, and `token_budget` behavior, so inspect `spectragent/agents/memory/`
and its tests instead of repeating that diagnosis.

The snapshot also predates later observation, action, input-humanization, and trace
changes. Treat its ordering as historical intent; never reuse its implementation-state
claims without verification.
