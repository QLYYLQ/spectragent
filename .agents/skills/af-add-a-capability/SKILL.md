---
name: af-add-a-capability
description: Add a new All4One environment capability or backend, especially when it must compose with existing capabilities. Use for new Capability classes, backend registrars, lifecycle, observation-cycle contracts, or multi-capability routing; not for adding only one action or fragment to an existing capability.
---

# Add an All4One Capability

Treat `docs/architecture_design.md` §4.9 and `docs/execution_dag.md` M06 as authoritative. If they conflict with the implementation or this skill, stop and surface the conflict before coding.

Use [references/guide.md](references/guide.md) as the detailed implementation guide. Always read its contract-test section, Protocol/base distinction, checklist, and gotchas. Then read the sections relevant to the change:

- New capability structure: Steps 2, 5, and 6.
- Batched observation capture: Step 3.
- Lifecycle or backend work: Step 4.
- Config generation or a new registrar: Step 7.

Follow sibling skills when the work adds a discrete action, observation fragment, or wired receptor. Do not preserve a superseded compatibility path.
