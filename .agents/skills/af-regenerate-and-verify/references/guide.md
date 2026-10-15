# Regenerate, then verify it landed

## One entry point

Run `uv run spectragent config generate` (or `python -m spectragent.scripts.generate_all`).
It writes `spectragent/generated/_composed.py`, `_composed.schema.json` and
`.fingerprint.json`, then reports the stubs still needed by the source.
Never edit generated files by hand.

Discovery is strict and excludes `spectragent.generated` and `spectragent.scripts`.
It does not delete the old config before discovery. Every collected registrar must
have an implementation; an empty one fails generation. Source and schema are rendered
before replacing files, each through a temporary file and atomic rename; the fingerprint
is written last. This is not a multi-file transaction.

Stubs use `write_layer_stubs(registrar, prune=True)`. conscribe 2.0 writes them only for
injected attributes the source has not already declared. Negotiation declarations use
`declare_only=True`, so they need no stub. Currently all shipped runtime receptors are
annotated and no stub is needed. Pruning removes only files conscribe recognizes as its
own stale generated stubs; it never cleans arbitrary source files or artwork.

## Collect the registrar

`collect_registrars()` explicitly pins the registrars, in stable order. A strategy
registrar missing from this mapping leaves a key-only field instead of the nested union,
so its knobs are unreachable. Add it when its first implementation becomes configurable.
`ObservationRegistrar` and `NegotiationResultRegistrar` are deliberately not config layers.

Also document a new registrar in `docs/architecture_design.md` §6 and `AGENTS.md` §2.2.
The registrar coverage test checks names and discriminators against the source.

Only `ArchiverRegistrar` is currently inline-only. Its models participate in the build,
but its instances may only be written under `hook[trace_writer].archive`. Use
`result.top_level_layers`, not `model_fields`, to enumerate writable root sections:
conscribe keeps a hidden rejection field for an inline-only layer.

## Fingerprints and clean interpreters

A stale generated model still imports and can pass tests that merely consume it.
`test_in_sync_with_registries.py` compares the tracked fingerprints with the current
registrars in a clean subprocess, so test fakes cannot contaminate the measurement.
The test bootstrap generates only missing output; it must not silently refresh stale output.

conscribe 2.0 fingerprints its version, registrar settings, generated layer source and
schema. Capture these before rendering the composed schema: Pydantic may normalize
numeric constraint metadata from `0` to `0.0` during rendering. The generator and drift
probe use the same discovery exclusions. Fingerprints do not cover stubs; regenerate and
prune stubs separately. A failed layer can still have an error fingerprint, so a fingerprint
alone is never proof that generation succeeded.

## Verification

1. Inspect the owner's field in `_composed.py`: it must be a nested discriminated union,
   not merely a top-level registry list or a `Literal` of keys.
2. Run `uv run pytest tests/test_generated -q`. Besides fingerprints and knob coverage,
   the contract test compares runtime schema, generated schema and generated Python in
   a clean process, validates every shipped run YAML through both models, and checks
   invalid nested limits, discriminators, unknown sections and inline-only sections.
3. Run `uv run pytest tests/test_core/test_composer -q`. Configured knobs must reach the
   constructed instances and `prepare()` must accept the shipped combinations.
4. Add a rejection case for a new discriminator or constraint; never accept silent
   fallback or a degraded `Any` as evidence that wiring works.

Tests escalate `conscribe.ConscribeWarning`. Use `isolated_registries()` or the
`conscribe_isolated` fixture for test implementations; capture module-level fakes and
activate them only for the tests that need them. Do not weaken strict discovery or
warning filters to make a malformed fake pass a production-schema build.
