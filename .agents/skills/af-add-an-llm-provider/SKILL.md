---
name: af-add-an-llm-provider
description: Add or debug a Spectragent LLM provider, gateway route, schema dialect, or structured-output failure path. Use for `ChatModelBase` providers, `model_type` and `base_url` routing, schema-related 400s, ignored schemas, fenced output, truncation, and provider-specific validation errors.
---

# Add or Debug an LLM Route

Use the model's wire protocol—not its vendor reseller—as `model_type`; gateways normally belong in `base_url`. Do not add per-config schema booleans to compensate for a bad route.

Read [references/guide.md](references/guide.md) by symptom:

- Routing or gateway setup: §1–§3.
- New provider mechanics and coverage: §2, §5, and §8.
- Schema 400s or dialect work: §4.
- Structured validation, traces, or metrics: §5–§6.
- Removed workaround knobs: §7.
- The model's reasoning (`thinking`): §9.
- DeepSeek's official API (`openai.deepseek`): §11.

Validate the actual response against the real closed action union; an HTTP 200 does not prove that a gateway preserved the schema.

A Chat provider fills `ChatInvokeCompletion.thinking` (and `redacted_thinking`) from whatever reasoning its response already carries, on every return path, and never changes the request to obtain it; the reasoning is recorded, never fed back.


Decision providers use the separate `DecisionModelRegistrar` / `decision_model` axis in
`core/llm`, with `BaseDecisionModel.ainvoke(DecisionRequest) -> DecisionResult`. Preserve
native questions, probabilities, legends and assets. Use Decision's own contract tests;
never route a Decision validation failure through Chat's parse-retry exception. See
architecture §4.7/§4.18 and `tests/test_core/test_llm/test_decision_provider.py`.
