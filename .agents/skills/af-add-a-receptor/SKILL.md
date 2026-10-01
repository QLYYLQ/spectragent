---
name: af-add-a-receptor
description: Add or repair a configurable wired strategy receptor in an All4One capability, agent, loop, or action. Use when a swappable implementation has its own knobs, YAML cannot reach those knobs, or code resolves a strategy name and default-constructs a second instance.
---

# Add a Wired Receptor

A receptor injects a constructed, configured instance through `__wiring__`; consumers must not receive a name and look it up again.

Read [references/guide.md](references/guide.md) before changing the receptor path. At minimum, use §1 to choose the correct config shape, follow the end-to-end checklist in §2, apply the single-switch rule in §3, and run the structural guard described in §5. Read §4 for Browser profile placement, §6 for dataclass owners, and §7 for edge cases.

Regenerate configs after changing `__wiring__` or `Annotated` config fields, and verify the nested discriminated union—not merely the registry list—reaches generated YAML configuration.
