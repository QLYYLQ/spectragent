---
name: af-trace-and-hooks
description: Design or change All4One AOP hooks, RunTrace persistence, redaction, timing, or trace-based evaluation. Use when recording a new fact, intercepting success or failure, changing WAL semantics, deciding whether data belongs in a trace, or implementing an evaluator.
---

# Hooks and RunTrace

Hooks observe methods and lifecycle calls without emitters; evaluators consume persisted traces, never live run state. A trace records agent behavior and experimental variables, not the framework's low-level execution mechanics.

Read [references/guide.md](references/guide.md) by task:

- Hook creation and interception: §1–§2.
- WAL or reader behavior: §3.
- Whether a fact belongs in the trace: §4.
- Run configuration, negotiation, or digests: §5.
- Secrets or redaction: §6.
- Evaluators: §7.

Preserve the WAL commit point and deny-by-default secret policy. If the desired semantic event has no method, introduce an appropriate Protocol method rather than an emitter side channel.
