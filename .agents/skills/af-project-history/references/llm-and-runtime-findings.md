# LLM, dependency, and runtime findings

These notes explain past decisions. Provider behavior and installed dependency APIs are
time-sensitive; re-run a focused probe or consult current official docs when a task
depends on them.

## Gateway routing

September 2026 DMX probes established the repository rule now documented in
`docs/llm/dmxapi.cn.md`: `model_type` identifies the wire protocol, while a gateway is
only `base_url`.

- Route Gemini through the native Google provider at the gateway root.
- Route Claude through the native Anthropic provider at the gateway root.
- Use the OpenAI-compatible `/v1` route for models actually speaking that protocol.

An HTTP 200 did not prove that a compatibility translator honored the structured-output
schema. DMX also routed identical requests through different upstream channels, so its
Claude route was not suitable for reproducible evaluation. Re-verify before applying
this measured behavior to a later gateway version.

## Structured output

The old memory tracked a sequence of fixes and therefore contradicts itself. The
durable lesson is to test the agent's real closed action schema through the actual
provider path; generic `PersonInfo`-style schemas do not exercise open dictionaries,
unions, provider translation, or retry behavior.

Do not repeat the obsolete claim that `ReactLoop` always passes
`output_format=None`; current code wires the output model. Use the current
`af-add-an-llm-provider` skill and reality harness for a new diagnosis.

## Prompt-cache and usage accounting

The 2026 survey found that cache support cannot safely be modeled by provider family
alone. Model ID, serving surface, service tier, context bracket, TTL behavior, and
billing mode can all matter. Numeric prices and allowlists rot; only current official
provider docs can establish them.

Normalize the usage fields a provider actually returns, including distinct cache-read
and cache-creation counters. This supports the user ruling to record tokens faithfully
without estimating money.

## `conscribe` and `agent-cdp`

Historical integration lessons that remain worth checking:

- `conscribe` registration happens through its metaclass; `__wiring__` receptors are
  constructed and injected instances, not names for consumers to resolve again.
- Capability-relative dotted keys are expanded during config generation and narrowed
  during AF negotiation; `conscribe` does not choose among providers.
- Any new YAML-selectable registrar must be collected by the single global generator,
  not merely declared.
- `agent-cdp` is browser-internal. Watchdogs are resolvers/action executors inside the
  browser capability, not a framework-wide page-object abstraction.

Inspect the installed versions and current tests before relying on old metaclass or
annotation edge cases.

## Runtime-flow memory was retired

The Claude `reference_af_framework_flow` snapshot is intentionally not reproduced. It
mixed a now-disputed memory-negotiation claim with an obsolete structured-output dead
path and reflected an older runtime. Use `docs/architecture_design.md` plus `core/runtime.py`,
`core/step_recorder.py`, and the loop tests for the current call graph.
