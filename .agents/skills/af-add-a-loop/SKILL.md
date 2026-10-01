---
name: af-add-a-loop
description: Add or change an All4One AgentLoop execution strategy, including phases, retries, failures, abort propagation, observation cycles, and loop configuration. Use for code under `spectragent/core/loops/` or when deciding whether behavior belongs to a Loop or Agent.
---

# Add an AgentLoop

Start with the current Loop design in `docs/architecture_design.md`; the repository is mid-refactor, so do not preserve an older code shape when the design says otherwise. Surface any unresolved document conflict before coding.

Read [references/guide.md](references/guide.md) selectively:

- Always read §1, §5, and §6 for Loop/Agent separation, config, and required test seams.
- Read §2 for retry or parsing policy.
- Read §3 before touching exception handling or `RunAbort`.
- Read §4 before re-observing or re-asking within a step.

Preserve the central split: Agents provide domain knowledge and typed failures; Loops own execution policy and remain stateless across woven agents.
