"""Present input, declaration and assembly facts without inspecting runtime state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from spectragent.core.composer import ComposedRun
from spectragent.core.composition_evidence import implementation_name


def declaration_document(value: Any) -> Any:
    """Serialize public immutable evidence; defaults and user values are absent."""
    if isinstance(value, type):
        return implementation_name(value)
    if is_dataclass(value):
        return {
            field.name: declaration_document(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: declaration_document(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [declaration_document(item) for item in value]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise TypeError(f"Unsupported declaration evidence: {type(value).__name__}")


def explain_provenance(run: ComposedRun, registrars: Mapping[str, Any]) -> dict[str, Any]:
    """Join Composer evidence with Conscribe's public declaration extraction."""
    evidence = run.composition
    if evidence is None:
        return {"available": False, "reason": "This run was assembled without Composer evidence."}
    definitions = {(component.layer, component.key) for component in evidence.components}
    declarations = {
        f"{layer}/{key}": declaration_document(registrars[layer].get_config_provenance(key))
        for layer, key in sorted(definitions)
    }
    return {
        "available": True,
        "declaration_scope": "declared_fields_before_schema_composition",
        "inputs": declaration_document(evidence.inputs),
        "components": [
            {
                **declaration_document(component),
                "declaration": f"{component.layer}/{component.key}",
            }
            for component in evidence.components
        ],
        "wiring": declaration_document(evidence.wiring),
        "declarations": declarations,
    }


def render_provenance(console: Console, provenance: dict[str, Any]) -> None:
    """Summarize real injection decisions and inherited declarations for people."""
    if not provenance.get("available"):
        return
    inputs = provenance["inputs"]
    explicit = sum(entry["source"] == "explicit" for entry in inputs)
    console.print(Text(f"Field inputs: {explicit} explicit · {len(inputs) - explicit} default"))
    table = Table("Wiring receptor", "Source", "Injection", "Targets")
    for edge in provenance["wiring"]:
        if edge["source"] == "declaration_only":
            continue
        table.add_row(
            Text(edge["path"]),
            edge["source"],
            edge["via"] or "—",
            Text(
                ", ".join(target["instance_id"] or target["key"] for target in edge["targets"])
                or "none"
            ),
        )
    console.print(table)
    tree = Tree("Declaration origins")
    for component in provenance["components"]:
        declaration = provenance["declarations"][component["declaration"]]
        branch = tree.add(Text(component["path"]))
        branch.add(
            Text("MRO: " + " → ".join(name.rsplit(".", 1)[-1] for name in declaration["mro"]))
        )
        inherited = [
            f"{name} ← {field['declared_on'].rsplit('.', 1)[-1]}"
            for name, field in declaration["fields"].items()
            if field["declared_on"] != declaration["cls"]
        ]
        if inherited:
            branch.add(Text("Inherited fields: " + "; ".join(inherited)))
        chain = declaration["constructor_chain"]
        if chain and chain["stop_reason"]:
            branch.add(
                Text(f"Constructor chain stopped: {chain['stop_reason']} at {chain['stopped_at']}")
            )
    console.print(tree)
