---
name: af-regenerate-and-verify
description: Regenerate and verify All4One's conscribe-composed configuration after changing `__wiring__`, `Annotated` config fields, or registrars. Use when YAML rejects a valid-looking key, a configured knob has no effect, or generated artifacts may be stale.
---

# Regenerate and Verify Configuration

Read [references/guide.md](references/guide.md) completely before diagnosing code-generation drift. It documents the single generation entry point, registrar collection, fingerprint isolation, and four checks that prove a knob reached YAML.

Do not hand-edit `spectragent/generated/`. Regenerate from source declarations, inspect the generated owner field rather than only the top-level registry list, run the generated-config tests, compose shipped YAML, and prove an invalid discriminator is rejected.
