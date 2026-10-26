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

User rulings of 2026-10-26, after DeepSeek's official API was probed (facts in
the provider guide §11):

- A model family without `json_schema` takes the schema in the prompt through
  `OpenAICompatBase` (`takes_schema_in_prompt`); Vercel's own copy of that path was
  folded into the base. `DeepSeekFamily.supports_json_schema` is `False`, so a
  `deepseek-*` model behind a gateway no longer sends `json_schema` either: a gateway
  may silently drop the field, and the prompt plus validation does not depend on it.
- `core/llm` validates and raises; it never re-asks. Re-asking belongs to the
  consumer: ReAct's parse retries for agents, the lookahead judge's own loop for
  offline judging.
- A structured reply that does not fit its output model is worded by the framework
  (`core/action_guard.py::reply_refusal`), in the voice of the params refusal; the
  agent's parser reads only validated replies. Rejected alternatives: validating in
  the Loop instead of the provider (only the provider knows how the reply crossed the
  wire), and putting it in the base Loop (only ReAct has output models).
- The lookahead judge composes its model from YAML through `core/llm`
  (`configs/lookahead/judge_deepseek.yaml`); it had bypassed the framework with no
  recorded reason.

## Prompt-cache and usage accounting

The 2026 survey found that cache support cannot safely be modeled by provider family
alone. Model ID, serving surface, service tier, context bracket, TTL behavior, and
billing mode can all matter. Numeric prices and allowlists rot; only current official
provider docs can establish them.

Normalize the usage fields a provider actually returns, including distinct cache-read
and cache-creation counters. This supports the user ruling to record tokens faithfully
without estimating money.

## Implemented model lifecycle and attribution (2026-10-15)

At the accepted `a6a36d0` implementation, clients are created lazily and reused within
one execution; owned resources close explicitly, borrowed transports remain borrowed,
and later executions create fresh owned transports. Public lifecycle methods and the
provider extension surface `client_resources` replace the earlier per-request close
mechanism. Cleanup is bounded and diagnostic; it never replaces replies, primary
errors or cancellation, and never retries an already completed model request.

Runtime closes its main and explicitly declared prompt-helper models from its final
cleanup, including when another hook's lifecycle fails. Independent evaluation owns
its own model execution. One recording ledger keeps caller/purpose/model/step identity,
known usage and missing coverage; helper replies do not become Agent decisions/thoughts.
This does not deliver arbitrary action-internal models, Jev or cross-run shared pools.

The tested versions were agent-cdp 0.5.0, conscribe 2.0.0 and Ollama 0.6.3 (minimum
0.6.2 to use a public close API). These are dated acceptance facts, not version advice
for future upgrades. Cloud lite SDK lifecycle work remains deferred. Details and tests:
`tmp/flywheel/followup/implementation/README.md` and architecture §§4.6–4.7.

## `conscribe` and `agent-cdp`

Historical integration lessons that remain worth checking:

- `conscribe` registration happens through its metaclass; `__wiring__` receptors are
  constructed and injected instances, not names for consumers to resolve again.
- Capability-relative dotted keys are expanded during config generation and narrowed
  during AF negotiation; `conscribe` does not choose among providers.
- Any new YAML-selectable registrar must be collected by the single global generator,
  not merely declared.
- `agent-cdp` is browser-internal. Watchdogs are resolvers/action executors inside the
  browser capability, not a framework-wide page-object abstraction. Watchdog-to-tab is
  N:M; scope is scheduling, not a blanket browser permission boundary. Same-tab
  observation/JS/OOPIF collaboration remains valid, while binding/session ownership and
  per-binding cleanup are explicit. Generic Capability APIs stay free of browser tabs/scopes.
- Older conscribe 1.3 / agent-cdp 0.4 API descriptions and dataclass/receptor workarounds
  are historical. Current 2.0 / 0.5 integration and generation tests replace those assumptions.

Inspect the installed versions and current tests before relying on old metaclass or
annotation edge cases.

## Runtime-flow memory was retired

Both older Claude runtime-flow snapshots (including the October re-verification) are
retired from active routing: they describe private slot methods, sync prompt builders,
old guard return values and superseded tracing boundaries. Use the implementation
handoff for status, and `docs/architecture_design.md`, public Protocols and current tests
for the call graph. The old records remain in the historical index, not as current API guidance.
