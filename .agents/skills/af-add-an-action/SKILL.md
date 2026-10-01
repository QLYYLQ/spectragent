---
name: af-add-an-action
description: Add or change an All4One action and its model-facing parameter contract. Use for new verbs, capability action wiring, action negotiation or qualification errors, prompt descriptor changes, and parameters that are advertised but not consumed.
---

# Add an Action

Actions are registered, fully qualified units owned and executed by a Capability. Agents declare relative requirements; negotiation and the guarded action boundary qualify them for the environment.

Read [references/guide.md](references/guide.md) as follows:

- Always read §1, §2, §3, and §6 for class shape, registration, negotiation, end-to-end parameter seams, and verification.
- Read §4 when changing parameter schemas or the structured-output compiler.
- Read §5 before proposing an action that calls an LLM; the general trace/AOP support is intentionally unresolved.

Trace every parameter in both directions: descriptor → agent output, and `Params` → `execute` → transport/event → handler use. Delete obsolete parameters or paths instead of leaving compatibility shims.
