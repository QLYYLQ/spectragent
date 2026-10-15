"""Generate the composed config + per-layer stubs in one pass.

Replaces the nine per-layer ``generate_config.py`` scripts. Run it whenever
a registrar gains or loses a registered class::

    uv run spectragent config generate          # via pyproject [project.scripts]
    uv run python -m spectragent.scripts.generate_all

Outputs (under ``spectragent/generated/``):

- ``_composed.py`` — a single ``ComposedConfig`` Pydantic model with a
  ``list[layer_union]`` field per registrar and wired fields inlined as
  nested discriminated unions.
- ``_composed.schema.json`` — JSON Schema for the same.
- ``.fingerprint.json`` — content fingerprint used by CI to assert the
  generated file is in sync with the current registry state.

Only injected attributes absent from source declarations need ``.pyi`` stubs.
Obsolete conscribe-generated stubs are pruned; all current receptors are typed
in the source, so the current build needs none.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from conscribe import (
    build_composed_config,
    compute_registry_fingerprint,
    discover,
    generate_composed_config_source,
    generate_composed_json_schema,
)
from conscribe.stubs import write_layer_stubs

from spectragent import GENERATED_DIR, PACKAGE_ROOT


def _collect_registrars() -> dict[str, type]:
    """Gather every concrete registrar under ``spectragent/``.

    We import-and-pin them explicitly rather than scan ``get_registry``
    so ``build_composed_config`` receives a stable dict (order matters
    for deterministic output).
    """
    from spectragent.agents._registrar import AgentRegistrar
    from spectragent.agents.memory._registrar import MemoryRegistrar
    from spectragent.core.actions._registrar import ActionRegistrar
    from spectragent.core.archive._registrar import ArchiverRegistrar
    from spectragent.core.capabilities._registrar import CapabilityRegistrar
    from spectragent.core.capabilities.browser.backends._registrar import (
        BrowserProviderRegistrar,
    )
    from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerRegistrar
    from spectragent.core.capabilities.browser.dom.serializers import (
        DOMSerializerRegistrar,
    )
    from spectragent.core.capabilities.browser.input import (
        MouseTrajectoryRegistrar,
        TypingRhythmRegistrar,
    )
    from spectragent.core.capabilities.browser.replay import (
        MissPolicyRegistrar,
        NetworkSourceRegistrar,
        ReplayabilityRegistrar,
    )
    from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
    from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
    from spectragent.core.data_loader.task.step_selector import StepSelectorRegistrar
    from spectragent.core.environments._registrar import EnvironmentRegistrar
    from spectragent.core.hooks._registrar import HookRegistrar
    from spectragent.core.llm._registrar import LLMRegistrar
    from spectragent.core.loops._registrar import AgentLoopRegistrar
    from spectragent.core.loops.restore import RestorerRegistrar
    from spectragent.evaluator._registrar import EvaluatorRegistrar
    from spectragent.export._registrar import ExporterRegistrar
    from spectragent.export.translate._registrar import ActionTranslatorRegistrar

    return {
        # Leaves of the wiring DAG first — not load-bearing but keeps
        # the generated union ordering stable under rename.
        "browser_provider": BrowserProviderRegistrar,
        # Browser-internal strategy axes. Excluding them left their
        # config surface untyped — most visibly ``mouse_trajectory``,
        # which was emitted as a bare ``str`` so a typo was silently
        # downgraded to the instant (non-humanized) strategy.
        "mouse_trajectory": MouseTrajectoryRegistrar,
        "typing_rhythm": TypingRhythmRegistrar,
        "dom_cleaner": DOMCleanerRegistrar,
        "dom_serializer": DOMSerializerRegistrar,
        # How far a run is pinned for replay (``none`` / ``pinned``, §4.14):
        # collected so ``pinned``'s seed / time zone / locale / DPR knobs are
        # typed in the generated union rather than lost behind a Literal.
        "replayability": ReplayabilityRegistrar,
        # Offline replay (§4.14 "回放"): the network source and, nested in its
        # ``archive`` block, the miss policy — leaves before their owners, so
        # the generated union nests capability → network_source → miss_policy
        # with every knob (matcher, isolation, shift_dates, …) typed.
        "miss_policy": MissPolicyRegistrar,
        "network_source": NetworkSourceRegistrar,
        # Environment evidence (§4.5, §4.14): the trace hook's ``archive``
        # receptor draws from it, so each archiver's knobs are typed in YAML
        # and a mistyped archiver_id is refused.
        "archiver": ArchiverRegistrar,
        "action": ActionRegistrar,
        "capability": CapabilityRegistrar,
        # How a branch run restores its checkpoint (§4.1 ``BranchLoop``): a
        # receptor of the ``branch`` loop, collected before ``agent_loop``
        # so the loop's block nests the restorer union with every knob
        # (verify, screenshot_max_distance, pace) typed.
        "restorer": RestorerRegistrar,
        "agent_loop": AgentLoopRegistrar,
        "llm": LLMRegistrar,
        "memory": MemoryRegistrar,
        "environment": EnvironmentRegistrar,
        "hook": HookRegistrar,
        # Where a branch sweep forks (§4.10 ``trace_branches``): a receptor of
        # that task loader, collected before ``task_loader`` so the loader's
        # block nests the selector union (``explicit`` steps, ``every_k``'s k
        # and offset) typed.
        "step_selector": StepSelectorRegistrar,
        "task_loader": TaskLoaderRegistrar,
        "eval_loader": EvalLoaderRegistrar,
        # Scoring. An optional layer — a config that only runs an agent
        # omits it — but a *layer*, so the judge model, its prompts and
        # its thresholds are chosen in YAML like everything else instead
        # of living in a per-benchmark script outside the package.
        "evaluator": EvaluatorRegistrar,
        # Training export (§4.15): how a teacher's actions become a student's.
        # An optional top-level layer, and the exporter's ``translator``
        # receptor — collected before the exporter so its block nests the
        # translator union with every knob (occlusion, untranslatable) typed.
        "action_translator": ActionTranslatorRegistrar,
        # The export job itself (§4.15): sources, filters, split, the student's
        # run config and the record format. An optional top-level layer — an
        # export YAML has only this block — whose ``translator`` receptor nests
        # the union collected just above.
        "exporter": ExporterRegistrar,
        "agent": AgentRegistrar,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent config generate",
        description="Regenerate composed configuration, schema and registry fingerprints.",
    )
    parser.parse_args(argv)
    discover("spectragent", exclude=["spectragent.generated", "spectragent.scripts"])
    registrars = _collect_registrars()
    # Snapshot declarations before rendering: pydantic can normalize numeric
    # constraint metadata (0 to 0.0) while building the composed schema.
    fingerprints: dict[str, str] = {
        layer: compute_registry_fingerprint(reg) for layer, reg in registrars.items()
    }
    fingerprint_path = GENERATED_DIR / ".fingerprint.json"
    fingerprint = json.dumps(fingerprints, indent=2, sort_keys=True) + "\n"

    result = build_composed_config(registrars, inline_wiring=True, top_level_extra="forbid")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    source_path = GENERATED_DIR / "_composed.py"
    source = generate_composed_config_source(result)

    schema_path = GENERATED_DIR / "_composed.schema.json"
    schema = json.dumps(generate_composed_json_schema(result), indent=2) + "\n"

    # Only undeclared injected attributes need stubs in conscribe 2.0.
    # Prune obsolete generated stubs so they cannot shadow typed source.
    # ``write_layer_stubs`` takes one registrar at a time; omit
    # ``output_dir`` so stubs land next to the source files.
    stub_paths: list[Path] = []
    for reg in registrars.values():
        stub_paths.extend(write_layer_stubs(reg, prune=True))

    for path, content in (
        (source_path, source),
        (schema_path, schema),
        (fingerprint_path, fingerprint),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(path)

    print(f"Wrote {source_path}")
    print(f"Wrote {schema_path}")
    print(f"Wrote {fingerprint_path}")
    print(f"Stubs ({len(stub_paths)}) written under {PACKAGE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
