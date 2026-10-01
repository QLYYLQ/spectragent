---
name: af-add-an-observation-fragment
description: Add or change an All4One observation fragment and the resolver that produces it. Use for new environment state, fragment negotiation and qualification, optional fragment failure, batched capture cycles, capture timestamps, or observation cost budgets.
---

# Add an Observation Fragment

Read `docs/architecture_design.md` §4.4 first for any non-trivial change. A fragment is independently registered, negotiated, traced, and assembled into the run's dynamic observation model.

Use [references/guide.md](references/guide.md) by concern:

- Always read §0–§4 and §9 for the class, resolver, model assembly, key qualification, and checklist.
- Read §5–§7 for batching, multi-capability behavior, optional failures, and the shared contract harness.
- Read §8 when the fragment has measurable prompt or capture cost.

Resolvers raise on failure; the environment alone decides whether a required fragment aborts or an optional fragment becomes `FragmentFailure`. Never fabricate an empty fragment.
