"""Configuration inspection through the same Composer and prepare boundary as a run."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from spectragent import GENERATED_DIR
from spectragent.cli.configuration_errors import inspection_stage
from spectragent.cli.configuration_provenance import explain_provenance
from spectragent.cli.fingerprints import source_fingerprints
from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import collect_registrars
from spectragent.core.run_cli import task_context
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import TraceNegotiation
from spectragent.core.trace.redaction import redact_config


def explain_config(
    path: Path,
    *,
    agent_id: str | None = None,
    env_id: str | None = None,
    start_url: str | None = None,
    replay_archive: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    with inspection_stage("yaml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("The configuration must be a YAML mapping")
    with inspection_stage("schema"):
        registrars = collect_registrars()
        composer = ConfigComposer(registrars=registrars)
    with inspection_stage("validation"):
        config = composer.top_level_type.model_validate(raw)
    with inspection_stage("composition"):
        run = composer.compose(config)
    agents = run.ids("agent")
    negotiation = None
    descriptors: list[dict[str, Any]] = []
    checked = "configuration"
    selected_agent = agent_id
    with inspection_stage("selection"):
        context = task_context(start_url=start_url, replay_archive=replay_archive)
        loop_inputs = None
        if agents and selected_agent is None:
            if len(agents) != 1:
                raise ValueError("Several agents are configured; select one with --agent-id")
            selected_agent = agents[0]
        if not agents and any((agent_id, env_id, start_url, replay_archive, task_id)):
            raise ValueError("Agent or task options require an agent in the configuration")
        if agents and task_id is not None:
            loader = run.single("task_loader")
            task = loader.get_task_context(task_id)
            context, loop_inputs = task.env, task.loop
    if agents:
        assert selected_agent is not None
        with inspection_stage("prepare"):
            runtime = AgentRuntime.for_agent(run, selected_agent, env_id=env_id)
            runtime.prepare(env_context=context, loop_inputs=loop_inputs)
        bound = TraceNegotiation.from_results(
            runtime.negotiated_observation,
            runtime.negotiated_actions,
            runtime.negotiated_archive,
        )
        negotiation = bound.model_dump(mode="json") if bound is not None else None
        descriptors = [
            descriptor.model_dump(mode="json") for descriptor in runtime.active_descriptors
        ]
        checked = "configuration_and_prepare"
    full = redact_config(run.top_level_cfg, record_secrets=True)
    digest = hashlib.sha256(
        json.dumps(full, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    fingerprints = source_fingerprints()
    missing_files = [
        name
        for name in ("_composed.py", "_composed.schema.json", ".fingerprint.json")
        if not (GENERATED_DIR / name).is_file()
    ]
    stored = None
    with suppress(OSError, ValueError):
        stored = json.loads((GENERATED_DIR / ".fingerprint.json").read_text())
    stored = stored if isinstance(stored, dict) else {}
    stale_layers = sorted(
        name
        for name in set(fingerprints) | set(stored)
        if stored.get(name) != fingerprints.get(name)
    )
    generated_status = "missing" if missing_files else "stale" if stale_layers else "current"
    with inspection_stage("provenance"):
        provenance = explain_provenance(run, registrars)
    return {
        "checked": checked,
        "launched": False,
        "agent_instance": selected_agent,
        "resolved_config_sha256": digest,
        "registry_fingerprints": fingerprints,
        "generated": {
            "status": generated_status,
            "missing_files": missing_files,
            "stale_layers": stale_layers,
            "remedy": "spectragent config generate" if generated_status != "current" else None,
        },
        "components": {
            layer: [
                {"instance": key, "implementation": type(value).__registry_key__}
                for key, value in values.items()
            ]
            for layer, values in run.by_layer.items()
            if values
        },
        "negotiation": negotiation,
        "action_descriptors": descriptors,
        "hooks": [
            {
                "hook_id": hook.hook_id,
                "blocking": hook.blocking,
                "priority": hook.priority,
                "intercepts": [
                    {
                        "role": intercept.role,
                        "method": intercept.method,
                        "advice": intercept.advice.value,
                    }
                    for intercept in hook.__intercepts__
                ],
            }
            for hook in run.hooks
        ],
        "resolved": redact_config(run.top_level_cfg),
        "provenance": provenance,
    }


def list_components(layer: str | None = None) -> dict[str, Any]:
    registrars = collect_registrars()
    composer = ConfigComposer(registrars=registrars)
    if layer is not None and layer not in registrars:
        raise ValueError(f"Unknown component layer {layer!r}; choose {', '.join(registrars)}")
    schema = composer.top_level_type.model_json_schema()
    # Registrars expose implementations, not instantiated runtime objects.
    return {
        "components": {
            name: sorted(registrar.keys())
            for name, registrar in registrars.items()
            if layer is None or name == layer
        },
        "configuration_layers": sorted(schema.get("properties", {})),
    }
