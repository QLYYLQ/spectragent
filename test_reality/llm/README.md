# test_reality/llm

A **standalone reality-check harness** for `spectragent.core.llm` — the project's
in-house LLM backend. It points each registered *chat template* (provider) at a
real third-party proxy (dmxapi / openrouter) and verifies:

1. **plain text chat** — `llm.ainvoke([UserMessage(...)])`
2. **structured output (`format`)** — `llm.ainvoke(..., output_format=<PydanticModel>)`

This is deliberately **not** part of the pytest suite (it lives outside `tests/`,
makes real network calls, and costs API credits). It uses only the public
`spectragent.core.llm` surface (registry + provider classes + message types).

## Quick start

```bash
# 1. Offline wiring gate — no network, no key. Always run this first.
uv run python test_reality/llm/run_offline.py

# 2. See the live matrix without making any calls.
uv run python test_reality/llm/run_live.py --list

# 3. Live run (core: openai, anthropic, google [+ openrouter when kind=openrouter]).
uv run python test_reality/llm/run_live.py

# 4. Everything, including extended providers.
uv run python test_reality/llm/run_live.py --extended
```

## Configuration (environment only — never hard-code keys)

| Var | Meaning | Default |
|---|---|---|
| `PROXY_BASE_URL` | Proxy host **without** `/v1` (e.g. `https://www.dmxapi.cn`) | falls back to `OPENAI_BASE_URL` (with `/v1` stripped) |
| `PROXY_API_KEY` | Proxy API key | falls back to `OPENAI_API_KEY` |
| `PROXY_KIND` | `dmxapi` or `openrouter` | `dmxapi` |
| `LLM_TIMEOUT_S` | Per-call timeout (seconds) | `60` |
| `OPENAI_MODELS`, `ANTHROPIC_MODELS`, `GOOGLE_MODELS`, `OPENROUTER_MODELS`, `DEEPSEEK_MODELS`, `CEREBRAS_MODELS`, `VERCEL_MODELS`, `MISTRAL_MODELS` | Comma-separated model ids per provider | sensible dmxapi defaults |

The repo's existing `.env` (`OPENAI_BASE_URL=https://www.dmxapi.cn/v1`, `OPENAI_API_KEY=...`)
is picked up automatically, so `run_live.py` works with the current dmxapi config
out of the box. To test a different proxy/key, set `PROXY_BASE_URL` / `PROXY_API_KEY`.

## The `/v1` quirk (the #1 source of 404s)

A single `PROXY_BASE_URL` host is the source of truth; the harness derives both shapes:

| Provider family | base_url the SDK needs | why |
|---|---|---|
| OpenAI-compat (`openai.*`, `mistral.default`) | `host + /v1` | SDK/code appends `/chat/completions` |
| Anthropic (`anthropic.direct`) | `host` (no `/v1`) | SDK appends `/v1/messages` |
| Google (`google.default`) | `host` (no `/v1`) | SDK appends `/v1beta/models` |

`build.py:base_url_kwargs()` applies this per provider — callers never hard-code it.

## What gets tested

| id | registry key | proxyable | structured mechanism |
|---|---|---|---|
| `openai` | `openai.direct` | dmxapi/openrouter | `response_format` json_schema **strict** (+ prompt-fallback probe) |
| `openrouter` | `openai.openrouter` | openrouter only | strict json_schema |
| `anthropic` | `anthropic.direct` | **dmxapi only** (native Messages API) | forced tool-use |
| `google` | `google.default` | **dmxapi only** (native Gemini API) | native `response_schema` (+ prompt variant) |
| `google-http` | `google.default` | dmxapi only | native, via `http_options` base_url |
| `deepseek` | `openai.deepseek` | dmxapi/openrouter | function-calling (proxy-tolerant) |
| `cerebras` | `openai.cerebras` | dmxapi/openrouter | prompt-injection + regex |
| `vercel` | `openai.vercel` | dmxapi/openrouter | dual-path |
| `mistral` | `mistral.default` | dmxapi | json_schema strict |

**Not proxyable (offline-only, excluded from live runs):** `openai.groq` (distinct
SDK), `openai.azure`, `anthropic.aws`, `bedrock.default`, `oci.default`,
`ollama.default`, `browser_use.default` — vendor-locked clients that ignore
`base_url` or need vendor auth.

`openrouter` exposes only an OpenAI-compat surface — it has **no** native Anthropic
or Gemini endpoint, so `anthropic` / `google` are skipped when `PROXY_KIND=openrouter`.

## Interpreting structured-output failures

Structured output through a proxy fails for two very different reasons, and the
harness is designed to tell them apart:

- **Proxy/backend rejects strict json_schema** (`additionalProperties:false` +
  all-required). `openai.direct` / `openai.openrouter` / `mistral.default` send
  exactly this and have **no fallback** → 400 or plain text → parse error. If the
  `strict` mode FAILs but `prompt-fallback` PASSes, it's the proxy, not the model.
- **Model incapacity** — the tolerant paths (`deepseek` tool-call, `google` native
  with `json`-fence fallback, `anthropic` tool-use) succeed where strict json_schema
  fails, isolating a genuine schema-satisfaction problem.

## Output

Each `run_live.py` run prints a table + summary and writes a JSON snapshot to
`test_reality/llm/out/<timestamp>.json` (gitignored) for later diffing. The API
key is never written; completions are stored (they're model output, not secrets).

## Files

- `env.py` — env/config loader + the `/v1` derivation. No network.
- `schemas.py` — `PersonInfo` (flat) + `ProjectPlan` (nested/enum stress case).
- `cases.py` — declarative `ProviderSpec` registry (one row per template).
- `build.py` — construct a provider from a spec (applies the base_url rule).
- `report.py` — `Result` model + table/summary/JSON.
- `run_offline.py` — no-network wiring gate (registry, construct, serializers, schema).
- `run_live.py` — the live runner.
