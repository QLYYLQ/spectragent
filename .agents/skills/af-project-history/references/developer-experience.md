# Human–code-agent developer experience

This reference preserves product intent and deferred tooling ideas. It is not a claim
that the described command already exists.

## Why AF uses Conscribe deeply

User rationale, reaffirmed 2026-09-23:

- Human researchers usually choose experiment variants by editing YAML. They should get
  generated schemas, completion, documentation, and startup validation instead of
  memorizing a stringly typed configuration language.
- Substantial code changes are increasingly delegated to coding agents. Each extension
  axis therefore needs a local owner and an explicit entry point so an agent can add an
  Agent, Loop, Capability, Action, or strategy without editing unrelated central
  switches.
- `__wiring__` and registrar metaclasses concentrate complexity in the framework
  toolchain in exchange for small, bounded modification surfaces. Repository skills add
  the procedural map: where to edit, which invariants matter, and how to verify the
  result.

The intended collaboration loop is:

1. A human states research intent.
2. A coding agent changes one bounded extension surface.
3. Conscribe derives and validates the legal configuration space.
4. A human selects experiments through schema-backed YAML.
5. AF rejects incompatible composition before spending run time or model calls.

## Deferred direction: `spectragent explain config.yaml`

The user approved preserving this direction on 2026-09-23 but has not authorized an
implementation design. The purpose is to make AF's compiled composition understandable
without requiring ordinary users or coding agents to understand Conscribe internals.

An explainer should eventually make these facts observable:

- the resolved component/instance graph, including registrar key, concrete class, and
  source declaration;
- each wiring edge and whether it came from auto-discovery, an explicit subset, or an
  inherited/MRO declaration;
- relative action/fragment keys, their candidates, and the binding or ambiguity result;
- active and rejected Observation, Action, and Memory negotiation demands, with the
  party that required/provided each item;
- the descriptors actually advertised to the model and the guarded action set;
- AOP roles and intercepts that will fire for this run;
- generated-config/fingerprint freshness and field provenance where available.

Non-negotiable behavior:

- read-only by default and safe to run before an expensive environment or LLM call;
- secrets redacted under the same deny-by-default policy as trace configuration;
- actionable explanations for invalid composition, not only a validation stack trace;
- machine-readable output should remain possible for coding-agent verification.

Exact CLI flags, static-versus-post-setup modes, and how much generic introspection
belongs upstream in Conscribe remain design questions. Because the user currently has no
capacity to update Conscribe, the first implementation may live entirely in AF and use
the metadata already exposed by its Composer, registrars, generated config, negotiation
results, and Weaver. Generic pieces can be proposed upstream later; AF must not depend on
that happening first.
