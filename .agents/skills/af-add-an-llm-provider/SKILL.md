---
name: af-add-an-llm-provider
description: Add or debug an All4One LLM provider, gateway route, schema dialect, or structured-output failure path. Use for `ChatModelBase` providers, `model_type` and `base_url` routing, schema-related 400s, ignored schemas, fenced output, truncation, and provider-specific validation errors.
---

# Add or Debug an LLM Route

Use the model's wire protocol—not its vendor reseller—as `model_type`; gateways normally belong in `base_url`. Do not add per-config schema booleans to compensate for a bad route.

Read [references/guide.md](references/guide.md) by symptom:

- Routing or gateway setup: §1–§3.
- New provider mechanics and coverage: §2, §5, and §8.
- Schema 400s or dialect work: §4.
- Structured validation, traces, or metrics: §5–§6.
- Removed workaround knobs: §7.

Validate the actual response against the real closed action union; an HTTP 200 does not prove that a gateway preserved the schema.
