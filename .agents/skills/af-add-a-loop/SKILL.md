---
name: af-add-a-loop
description: Add or change an All4One AgentLoop execution strategy, including phases, retries, failures, abort propagation, observation cycles, and loop configuration. Use for code under `spectragent/core/loops/` or when deciding whether behavior belongs to a Loop or Agent.
---

# Add an AgentLoop

Start with the current Loop design in `docs/architecture_design.md`; the repository is mid-refactor, so do not preserve an older code shape when the design says otherwise. Surface any unresolved document conflict before coding.

Read [references/guide.md](references/guide.md) selectively:

- Always read §1, §5, and §6 for Loop/Agent separation, run state, typed execution limits, config, and required test seams.
- Read §2 for retry, parsing, public ReAct round operations, or the async prompt contract.
- Read §3 before touching execution outcomes, recording isolation, cancellation, or timing.
- Read §4 before re-observing or re-asking within a step.
- Read §7–§8 for branching, trace reconstruction, terminal observation, or frozen export consumers.

Preserve the central split: Agents provide domain prompts and typed failures; Loops own execution policy and remain stateless across woven agents. Runtime owns identity and execution timing; the recording component owns model-call accounting and persistence failures. Use declared public round operations across module boundaries, with no old-name forwarding aliases.
