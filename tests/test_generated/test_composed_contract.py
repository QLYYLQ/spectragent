"""Generated source, JSON Schema and the runtime validator describe the same input."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_generated_and_runtime_configs_agree_in_a_clean_process() -> None:
    # Test modules register synthetic implementations. Measure only shipped classes.
    probe = r"""
import copy
import json
from pathlib import Path

import yaml
from conscribe import build_composed_config, discover, validate_wiring, wired_fields
from conscribe.introspect import wiring_slots
from conscribe.stubs import collect_class_stub_info
from pydantic import ValidationError

from spectragent.scripts.generate_all import _collect_registrars

discover("spectragent", exclude=["spectragent.generated", "spectragent.scripts"])
registrars = _collect_registrars()
validate_wiring(registrars)
result = build_composed_config(registrars, inline_wiring=True, top_level_extra="forbid")
from spectragent.generated import ComposedConfig

runtime = result.top_level_type
schema = json.loads(Path("spectragent/generated/_composed.schema.json").read_text())
assert runtime.model_json_schema() == schema
assert ComposedConfig.model_json_schema() == schema
assert "archiver" not in schema["properties"]
assert schema["additionalProperties"] is False
assert "archiver" not in result.top_level_layers

for path in sorted(Path("configs").glob("*.yaml")) + sorted(Path("configs/flywheel").glob("*.yaml")):
    raw = yaml.safe_load(path.read_text())
    assert runtime.model_validate(raw).model_dump(mode="json") == ComposedConfig.model_validate(raw).model_dump(mode="json"), path

raw = yaml.safe_load(Path("configs/ui_tars_local.yaml").read_text())
bad_inputs = [{"hooks": []}, {"archiver": []}, {"archiver": None}, {"archiver": [{"archiver_id": "browser.network"}]}, {"step_selector": [{"selector_id": "explicit", "steps": []}]}]
for field in ("dom_max_elements", "dom_max_serialized_chars"):
    bad = copy.deepcopy(raw)
    bad["environment"][0]["capabilities"][0][field] = 0
    bad_inputs.append(bad)
bad = copy.deepcopy(raw)
bad["environment"][0]["capabilities"][0]["mouse_trajectory"] = {"trajectory_type": "typo"}
bad_inputs.append(bad)
for bad in bad_inputs:
    errors = []
    for model in (runtime, ComposedConfig):
        try:
            model.model_validate(bad)
        except ValidationError as exc:
            errors.append([(e["loc"], e["type"]) for e in exc.errors()])
        else:
            raise AssertionError(f"{model} accepted {bad}")
    assert errors[0] == errors[1], errors

for model in (runtime, ComposedConfig):
    branch = model.model_validate({"task_loader": [{"benchmark_name": "trace_branches", "step_selector": {"selector_id": "all"}}]}).task_loader[0]
    assert branch.sources == ("*",)
    assert branch.require_archive == ("browser.network",)
    export = model.model_validate({"exporter": [{"exporter_id": "openai_messages", "student_run_config": "student.yaml", "output_dir": "dataset", "translator": {"translator_id": "identity"}}]}).exporter[0]
    assert export.sources == ("*",)

for registrar in registrars.values():
    for cls in registrar.get_all().values():
        # Every runtime receptor is annotated; negotiation declarations must
        # neither inject phantom attributes nor require a module-shadowing stub.
        assert collect_class_stub_info(cls) is None, cls
        for name, entry in wiring_slots(cls).items():
            if entry.declare_only:
                assert name not in wired_fields(cls), (cls, name)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
