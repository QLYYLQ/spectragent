# Regenerate, then verify it landed

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

## 1. One entry point

```bash
uv run spectragent-gen
.venv/bin/python -m spectragent.scripts.generate_all   # same thing
```

One pass writes all four artifact kinds: `spectragent/generated/_composed.py`,
`_composed.schema.json`, `.fingerprint.json` (`generate_all.py:131,134,141`) and the
`.pyi` stubs, dropped *next to each source file* (`:149`). Regeneration is global by
construction — there is one `ComposedConfig`, so "regenerate just my layer" is not
expressible.

> Older project notes named nine per-registrar `generate_config.py` files. That
> instruction was wrong and has been retired. `find spectragent -name
> generate_config.py` returns zero; `generate_all.py`'s own docstring line 3 says it
> replaced them. Current `AGENTS.md` §3.4 documents this single entry point.

Order inside `main()` matters and is worth knowing when output looks wrong: stale
`_composed*` is unlinked *before* `discover("spectragent")` (`:105-113`), because
`discover` imports the whole package and would otherwise re-import a half-written
module; layers with zero registered classes are dropped (`:118`, `Union[]` is not
constructible); `build_composed_config(..., inline_wiring=True)` (`:126`) is what
inlines wired children as nested discriminated unions.

## 2. A new registrar must be *collected*, not just created

`_collect_registrars()` (`generate_all.py:40-97`) hand-pins an ordered dict of 16
registrars — ordered by hand for deterministic union output, not scanned. A registrar
missing from it degrades to `Literal[<keys>]` instead of the discriminated union, so
**every knob the strategy declares is unreachable from YAML**. Measured: regenerating
with and without `dom_serializer` collected gives
`Union[HtmlDom_SerializerConfig, TextDom_SerializerConfig]` versus
`Literal['text', 'html']`. The `Literal` still rejects a typo, so the loss is silent
only for a *brand-new* axis no YAML names yet; once a config block exists the degraded
field rejects it loudly. The comment at `generate_all.py:74-78` says "bare `str`" and a
silently-downgraded typo — that was `mouse_trajectory`'s pre-`__wiring__` shape (a
hand-written `Annotated[Literal[...]]` scalar plus an untyped overrides dict), not what
an uncollected registrar does today.

`ObservationRegistrar` is deliberately absent because fragments are not a top-level
configuration layer. A fragment-only class does not move `_composed.py`, and stubs are
written only for collected registrars. Regenerate when the same change updates a
collected owner's `__wiring__` or config fields — as fragment additions usually do.

Adding a registrar also changes the documented extension surface. In the same change,
add an exact row to `docs/architecture_design.md` §6 and `AGENTS.md` §2.2; the registrar
coverage test compares both maps with the code. Add the registrar to
`_collect_registrars()` only when YAML must select that axis.

## 3. The staleness trap — the reason this skill exists

A stale `_composed.py` is not *broken*, it is **stale**. It still imports and remains
internally consistent, so tests that merely consume the generated model can pass. The
dedicated fingerprint test must fail. The stale model still describes an older registry,
so a knob that exists in source is unreachable from YAML and a config naming it fails
with "extra inputs are not permitted".

Two reasons it stayed invisible:

- `tests/conftest.py::_ensure_generated_config` (`:41`) regenerates only when
  `_composed.py` is **missing** (`if _COMPOSED.exists(): return`, `:43`). A stale file
  exists, so it is left alone — correctly, since regenerating every session would hide
  this.
- `spectragent/generated/` is gitignored except `__init__.py` and `.fingerprint.json`
  (`.gitignore:44,54`), so regenerating produces almost no diff and code review sees
  no signal.

`.fingerprint.json` was written by codegen and read by nothing. The drift is not
hypothetical: the `hook` layer was caught out of sync (`7050f18d0b365460` on disk vs
`15cb44d3518a26fb` live) in a tree where all 2908 tests were green.
`tests/test_generated/test_in_sync_with_registries.py` now closes it.

## 4. Why that test runs in a subprocess — generalise this

A fingerprint hashes a registry's **contents**. By the time a test module runs inside
the full suite, other test modules have imported stub capabilities, agents, actions and
environments into those very registries. Computing the hash in the pytest interpreter
measures the suite's fixtures, not the shipped code: the first version passed run alone
and reported four layers drifting run in the suite. So the check shells out to a clean
interpreter (`test_in_sync_with_registries.py:58` `_PROBE`, `:71-85`).

`tests/conftest.py` had already learned this — its docstring says "**In a subprocess,
deliberately**", for a different reason (in-process codegen pins `ComposedConfig` to
`None` in `sys.modules`).

**The rule: anything that measures global registry state must measure it in a clean
interpreter.** Write the next such check as a subprocess probe from the start.

## 5. Did my knob land? Four checks

1. **It is on the right config class.** Search `_composed.py` with `rg` for the field name and read
   which shape came back — line numbers move, the shapes do not:

   ```python
   # a wired receptor, inside BrowserCapabilityConfig — this is what you want
   mouse_trajectory: Union[BezierMouse_TrajectoryConfig, ...] = ...  # wired from: mouse_trajectory

   # a bare registry-list entry, inside ComposedConfig — present either way
   mouse_trajectory: list[MouseTrajectoryConfig] = []
   ```

   The second appears for any collected registrar. Only the first proves the owner can
   be configured through it. A `Literal[...]` where the nested union should be means the
   registrar was not collected; a bare `str` usually indicates an older hand-written
   config shape. In either case, return to step 2.
2. **`uv run pytest tests/test_generated/ -q`** — the fingerprint test and
   `test_no_dropped_annotated_fields.py` (the PEP 563 trap: a stringized `Annotated`
   that conscribe silently drops, which is the other way a knob "does nothing").
3. **A YAML naming it composes.** Add the block to `configs/*.yaml` (pattern:
   `configs/browser_use_local.yaml:125`) and let
   `tests/test_core/test_composer/test_shipped_config_integration.py` prove it.
4. **A deliberate typo is rejected, not defaulted.** Feed a bogus discriminator value
   and require `ValidationError` — see
   `test_composer.py::test_invalid_yaml_rejected_by_pydantic` (`:414-421`). Silence
   here is the `mouse_trajectory` failure mode, and it is the only check that catches it.

Reading a regeneration diff: `_composed.py`/`.schema.json` move only when a **config
field** moves (an `Annotated[...]` field or a `__wiring__` entry); a `.pyi` moves when
**any** attribute or method signature moves, private ones included.
