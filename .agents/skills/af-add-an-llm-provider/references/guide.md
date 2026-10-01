# Adding or debugging an LLM route

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

## 1. The routing rule (read this before anything else)

`model_type` is the **wire protocol**, not the vendor's brand and not the gateway you
bought the model through. `base_url` exists on all three main providers
(`openai_base.py:108`, `google.py:89`, `anthropic_base.py:83`), so **a gateway is a
`base_url`** — it is never a reason to send everything through the OpenAI-compatible
route.

| model | `model_type` | `base_url` |
|---|---|---|
| `gemini-*` | `google` | gateway **root** (no `/v1`) |
| `claude-*` | `anthropic` | gateway **root** (no `/v1`) |
| `gpt-*`, and models whose own vendor speaks OpenAI protocol (`deepseek-*`, `qwen-*`, `glm-*`) | `openai` | `<root>/v1` |

Measured live against dmxapi.cn this round, with af's real closed action union
(`agents/_action_union.py` — one single-key model per verb, every branch
`extra="forbid"`), same key, same schema:

| route | model | result |
|---|---|---|
| compat `/v1` | `gpt-4o-mini` | PASS |
| compat `/v1` | `gemini-2.5-flash` | PASS |
| compat `/v1` | `gemini-3.5-flash` | **400** `Unknown name "additionalProperties" at 'generation_config.response_schema...any_of[0]'` |
| compat `/v1` | `claude-haiku-4-5-20251001` | **0/3** — fenced reply whose keys were not the schema's |
| native `google`, root | `gemini-2.5-flash`, `gemini-3.5-flash` | PASS |
| native `anthropic`, root | `claude-haiku-4-5-20251001` | **3/3** (both `output_config` and the forced-tool-use fallback) |

The Claude row is the one to remember: the gateway's translator dropped
`response_format` **entirely**, so the model was never constrained. Fence-stripping
cannot save that — you get valid JSON of the wrong shape.

**A gateway's translation layer is a route you can usually avoid.** Full measurements
and the superseded 2026-03 conclusions: `docs/llm/dmxapi.cn.md`. A worked YAML block
with the rule inline: `configs/browser_use_local.yaml:46-68`.

Do **not** configure gateway **aliases**. `claude-fable-5-1` (a DMX-side alias) failed
on every route — `tool_choice: {"type":"tool"}` 400'd, `output_config` returned `None`
— while the real Anthropic model id passed 3/3.

## 2. Mechanics

`LLMRegistrar` is the repo's only **nested** registrar
(`core/llm/_registrar.py:21-26`): `discriminator_fields=["model_type", "provider"]`,
`key_separator="."`. Keys are `model_type.provider`; there are 15 registered:

```
anthropic.aws  anthropic.direct  bedrock.default  browser_use.default  google.default
mistral.default  oci.default  ollama.default  openai.azure  openai.cerebras
openai.deepseek  openai.direct  openai.groq  openai.openrouter  openai.vercel
```

`openai_base.py:51` and `anthropic_base.py:45` register the bare `openai` / `anthropic`
prefixes as abstract bases — a new OpenAI-compatible gateway subclasses
`OpenAICompatBase` and sets `__registry_key__ = "openai.<name>"`. In YAML that is
`model_type: openai` plus `provider: {name: <name>}`.

Model-specific behaviour goes on the **capability table**, never in an
`if "gemini" in self.model` (`core/llm/model_families/_base.py`, reached by every
provider via `ChatModelBase.model_family` → `detect_model_family`). Currently consulted:
`supports_strict_mode`, `supports_json_schema`, `schema_requires_gemini_fix`,
`schema_rejects_closed_union_branches`.

Adding a provider owes the test suite three things:

1. A `_Case` row in `tests/test_core/test_llm/test_provider_validation.py:451-470` —
   19 rows for 15 keys, because a provider with two structured paths owes one per path.
   `test_every_registered_provider_is_covered` (`:474`) fails if you skip it, and the
   same row is re-run against the closed action union at `:537`.
2. No private fence stripper. `test_no_provider_reimplements_fence_stripping`
   (`test_fence_stripping.py:195`) `rglob`s `providers/` and fails on any ``"```"``
   literal, docstrings included.
3. The structured path routed through `ChatModelBase._validate_structured` — enforced
   by an AST scan (`test_provider_validation.py:85`) that hunts bare
   `output_format.model_validate*`. One documented exemption: `_groq_parser.py`.

## 3. Three traps

**(a) HTTP 200 does not mean the schema was honoured.** Of seven models probed on the
compat route, **three returned 200 while ignoring the schema** — free text, or fenced
JSON whose keys were not the schema's. Nothing errors: status 200, valid JSON body,
normal `usage`. This is *worse* than a 400. A 400 tells you the route is unusable; a
200 looks like "the model got it wrong this time", so the framework **retries** — on a
route that can never succeed, because `response_format` was dropped before the model
saw anything. Never evaluate a gateway route by status code: send the real schema and
validate the body.

The framework's tell for this is `_validate_structured(..., wire_text=...)`
(`_registrar.py:180-185`): the provider passes the **upstream text** while `payload` is
the unwrapped/trimmed one, and when they differ the exception message says so. "Arrived
fenced *and* the wrong shape" is the signature of a dropped `response_format`.
`TraceWriter._failed_call` stores that message as `detail`, so it survives into the
trace. (`StructuredOutputValidationError` itself takes only `raw_output`/`model`/`usage`
— there is no `wire_text` kwarg; don't copy one.)

**(b) DMX round-robins upstream channels.** The same request with the same model id
lands on different upstreams across attempts, so it can answer 200 once and 400 the
next — most visible on the Claude route. Consequence: **that route is unusable for
reproducible evaluation.** A benchmark failure may come from the route rather than the
agent, a rerun will not reproduce it, and the `RunTrace` records model behaviour, not
which channel you drew. Cheap dev LLM: fine. Experimental variable: no.

**(c) Thinking tokens eat the output budget.** `gemini-3.5-flash` burns ~550 thinking
tokens on a trivial request even at the lowest setting (`reasoning_effort: low` on the
compat route, `thinking_level: low` natively, `google.py:77`). Any output budget below
~600 — `max_completion_tokens` (`openai_base.py:111`, default 4096) or
`max_output_tokens` (`google.py:78`, default 8096) — is spent before the model speaks,
and the reply is silently truncated into prose **that looks exactly like a model
ignoring the schema**. Both defaults are safely above it, so this only bites someone
who turned the knob down. Before blaming the route on a gemini-3.x schema failure, raise
the budget above 1000 and reproduce once. (OpenAI's own path does catch the clean case:
`finish_reason == "length"` raises a named truncation error at `openai_base.py:368`.)

## 4. Schema dialects

One compiler: `SchemaOptimizer.create_optimized_json_schema` (`schema/optimizer.py:80`)
inlines every `$ref`, drops metadata `title`, and `_apply_object_policy` (`:263`) closes
every object that declares `properties` — deliberately leaving `properties`-less nodes
(open maps) open, since closing `dict[str, Any]` yields a schema whose only legal value
is `{}`.

Dialect rules live in one named, tested section (`optimizer.py:371-469`), all in-place,
all run last by the provider about to hit the wire:

- `remove_min_items` (`:387`) — defect F12. `minItems`/`maxItems`/`uniqueItems` are not
  in OpenAI's strict dialect; a `Field(min_length=1)` on a list (the `action` field has
  one) otherwise 400s on the schema before the prompt is read. Implied by `strict=True`
  (`:112`).
- `strip_additional_properties` (`:401`) — native Gemini's `response_schema` is a
  protobuf message, so an unknown field is a *parse error*: the root node alone fails.
  Called unconditionally by `ChatGoogle` (`google.py:324`) and by `fix_gemini_schema`
  (`schema/gemini.py:92`).
- `open_union_branches` (`:424`) — opens the **whole subtree** of each `anyOf`/`oneOf`
  branch while the root stays closed. Gated on
  `ModelFamily.schema_rejects_closed_union_branches` (`model_families/_base.py:37`,
  True on `gemini.py:9`), read at `openai_base.py:344`.

**This is the transferable lesson.** `ChatGoogle` used to be correct *by accident*:
`google-genai` silently drops unknown keywords when it builds its typed `Schema`, so the
provider never had to state the rule and no test could fail. Pin or swap the SDK and it
breaks invisibly. A dialect rule is a fact about a wire format — it belongs in one
shared, testable transform, not in an SDK side effect and not in a provider's `if`.

Whose rule is it?
- **Endpoint** rule (true for every request this provider class makes) → call it
  unconditionally in the provider (`google.py:310-324`).
- **Model-family** rule (true for a family anywhere, and especially when the *opposite*
  is true for another family) → a flag on `ModelFamily` whose docstring states the
  measurement, read via `self.model_family.<flag>`.

Write the probe into the docstring; every rule there cites its exact error string. And
do not write down a defect you did not measure.

Test both halves: the transform in isolation **and** what reaches the wire. Stub the
client, `ainvoke`, assert on the captured request dict —
`TestWhatReachesAGeminiRoute` / `TestWhatReachesATranslatedGeminiRoute`
(`test_schema_dialects.py:261`, `:325`). A transform test alone cannot catch a provider
that forgot to call it. If the rule's opposite is true somewhere, write that test too
(`test_an_openai_model_keeps_them_closed`, `:367`) — it is what stops the gate being
"simplified" away.

`is_strict_compatible` (`optimizer.py:292`) answers whether a *compiled* schema can ship
under `"strict": true`. `ChatModelBase._use_strict_schema` (`_registrar.py:100`) pairs it
with `model_family.supports_strict_mode`: strict goes out only when the model supports it
**and** this payload can express it. It is an allowlist (`STRICT_DIALECT_KEYWORDS`,
`:41`), so it fails closed — an unknown keyword costs constrained decoding, never a 400.
Two things to know:

- Its reach is narrower than it looks. Both production callers compile with
  `strict=True` first, which already strips `minItems` — so cite `maxItems`/
  `uniqueItems` and the combinators as what it actually catches, not `minItems`.
- The walk **cannot** apply the allowlist inside `properties`/`$defs`: those keys are
  property *names*, and af's real schema has a property called `browser.click`
  (`_strict_ok`, `:333`). Any new keyword whose value is a name→schema map needs the
  same special case. A leaf keyword must also stay in the allowlist — there is an
  `assert` under the two frozensets (`:73`) and a test (`:447`).

## 5. Error discipline

A reply that does not fit the schema is the **one provider error the model can fix**.
It must raise `StructuredOutputValidationError` (`exceptions.py:38`, status 422,
carrying `raw_output` + `usage`), never a generic `ModelProviderError`. The Loop catches
it at `core/loops/react.py:357-376`, rebuilds a `ChatInvokeCompletion` from
`exc.raw_output`, and feeds it back through the agent's own parser — which knows this
run's negotiated verbs and answers with one located line. Before this was fixed (defect
F10) only 3 of 12 providers did it, so whether a mistyped parameter cost a step or the
whole run depended on which provider key the YAML happened to name.

Two guards keep it: the AST scan above, and the 19-row behaviour table at
`test_provider_validation.py:451`, which drives a real bad reply through every
registered key and asserts `status_code == 422` and a usable `raw_output`.

**A failing call must still be visible to hooks.** An exception fires no `after` advice,
so both hooks also intercept `around` (defect F17):

- `TraceWriter.around_ainvoke` (`hooks/trace_writer.py:292`, `Intercept` at `:82`)
  buffers **every** exception, so the step's messages file exists for exactly the steps
  a researcher most wants to read, and accumulates LLM wall time in a `finally`.
- `MetricsCollector.around_ainvoke` (`hooks/metrics_collector.py:130`) counts **only**
  `StructuredOutputValidationError` — a 429 or a dead socket carries no usage and no
  reply, and staying invisible is the honest answer for a call that never happened.

The failure is recorded as `{error, detail, raw_output?}` — a plain dict, never a
fabricated `ChatInvokeCompletion` (`trace_writer.py:501`). A completion-shaped object
with an error inside reads as something the model wrote. `raw_output` appears only when
the provider had text to hand over.

## 6. Fences

One helper: `strip_code_fence` (`core/llm/fences.py:44`). It used to be five or six
near-identical copies with four behaviours between them — and `openai_base`, the base
every OpenAI-compatible gateway goes through and the path most likely to be talking to a
translated model, had none. It returns a reply that already starts with `{`/`[`
untouched, because `{"memory": "I clicked ```Run```"}` is a real thing a browser agent
writes; the old Groq `content.split("```")[1]` would have returned `Run`. Call it
**before** `_validate_structured`, and pass the original as `wire_text=`.

## 7. The smell this round removed

`dont_force_structured_output` is **deleted** — along with `add_schema_to_system_prompt`,
`remove_min_items_from_schema` and `remove_defaults_from_schema`. The LLM layer now has
**zero** per-config schema booleans, and two tests keep it that way
(`test_providers_capabilities.py:118` and `:186`; `:134` drives a real request to prove
the *branch* went too, not just the field).

None of the four was a property of a model. The first two were workarounds for a gateway
that loses the schema in translation, and native routing makes the translation not exist.
The last two let a YAML hand-edit a compiled schema.

> If you are reaching for a per-config boolean to paper over a gateway: fix the route
> (§1), or add a tested dialect transform (§4). Never a fifth knob.

## 8. Debug checklist

1. Reply ignores the schema, or 400s on `additionalProperties` → check the route first
   (§1). Gemini/Claude on a `/v1` compat path is the bug.
2. Reply looks like prose → check the output budget before the route (§3c), especially
   on gemini-3.x.
3. Failure is intermittent across attempts → suspect upstream round-robin (§3b); stop
   using that route as an experiment variable.
4. Run dies on one bad action parameter → the provider is flattening
   `StructuredOutputValidationError`; add its `_Case` row and route through
   `_validate_structured` (§5).
5. New config key rejected as "extra inputs are not permitted" → regenerate; see the
   `af-regenerate-and-verify` skill.

Run `uv run pytest tests/test_core/test_llm -q`. Do **not** run `ruff format` here — the
tree is not format-clean and it produces a large diff across files you did not touch.
`ruff check` is the enforced gate.
