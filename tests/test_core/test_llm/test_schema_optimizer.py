"""Tests for SchemaOptimizer — flattening vs. opt-in strict-mode rewriting (§6.4.5).

Regression guards for the two behaviours that used to be unconditional:

1. every ``type: object`` node got ``additionalProperties: false``, so a
   free-form ``dict[str, Any]`` could only ever match ``{}``;
2. every object's ``required`` was rewritten to all of its properties,
   encoding one agent's output shape as global policy.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from spectragent.core.llm.schema.optimizer import SchemaOptimizer

# ── fixtures ───────────────────────────────────────────────────────


class _Inner(BaseModel):
    index: int
    label: str = "unnamed"


class _WithOptional(BaseModel):
    """One mandatory field, one optional field."""

    required_field: str
    optional_field: str | None = None


class _WithFreeFormDict(BaseModel):
    """The shape that broke: a list of untyped dicts."""

    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class _WithTypedDict(BaseModel):
    counters: dict[str, int] = Field(default_factory=dict)


class _WithNested(BaseModel):
    memory: str
    inner: _Inner
    items: list[_Inner] = Field(default_factory=list)


def _walk(node: Any):
    """Yield every dict node in a schema."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# ── free-form dict must stay open ──────────────────────────────────


class TestFreeFormObjects:
    def test_list_of_free_form_dicts_is_not_closed(self) -> None:
        """`list[dict[str, Any]]` must not compile to an items schema matching only {}."""
        schema = SchemaOptimizer.create_optimized_json_schema(_WithFreeFormDict, strict=True)
        items = schema["properties"]["tool_calls"]["items"]

        assert items["type"] == "object"
        # The old code emitted additionalProperties: false here with no
        # `properties`, so the model could only ever return `[{}]`.
        assert items.get("additionalProperties") is not False

    def test_no_object_node_is_closed_without_properties(self) -> None:
        """No schema node may forbid all keys while declaring none of its own.

        That combination is unsatisfiable-by-anything-useful: it is what silently
        discarded every tool call's arguments.
        """
        for model in (_WithFreeFormDict, _WithTypedDict, _WithNested):
            schema = SchemaOptimizer.create_optimized_json_schema(model, strict=True)
            for node in _walk(schema):
                if node.get("type") == "object" and node.get("additionalProperties") is False:
                    assert node.get("properties"), f"closed object with no properties in {model}"

    def test_typed_dict_value_schema_survives(self) -> None:
        """`dict[str, int]` keeps its value schema instead of being closed off."""
        schema = SchemaOptimizer.create_optimized_json_schema(_WithTypedDict, strict=True)
        counters = schema["properties"]["counters"]

        assert counters["type"] == "object"
        assert counters["additionalProperties"] == {"type": "integer"}

    def test_closed_models_still_get_additional_properties_false(self) -> None:
        """Objects that declare properties are closed Pydantic models — still closed."""
        schema = SchemaOptimizer.create_optimized_json_schema(_WithNested, strict=True)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["inner"]["additionalProperties"] is False


# ── strict is opt-in ───────────────────────────────────────────────


class TestStrictIsOptIn:
    def test_non_strict_preserves_model_optionality(self) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_WithOptional, strict=False)
        assert schema["required"] == ["required_field"]

    def test_strict_forces_all_properties_required(self) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_WithOptional, strict=True)
        assert sorted(schema["required"]) == ["optional_field", "required_field"]

    def test_default_is_non_strict(self) -> None:
        """Callers that do not ask for strict mode must not get it."""
        default = SchemaOptimizer.create_optimized_json_schema(_WithOptional)
        explicit = SchemaOptimizer.create_optimized_json_schema(_WithOptional, strict=False)
        assert default == explicit
        assert default["required"] == ["required_field"]

    def test_strict_applies_to_nested_objects(self) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_WithNested, strict=True)
        assert sorted(schema["properties"]["inner"]["required"]) == ["index", "label"]

    def test_non_strict_nested_keeps_defaults_optional(self) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_WithNested, strict=False)
        # `label` has a default, so Pydantic does not mark it required.
        assert schema["properties"]["inner"]["required"] == ["index"]

    def test_gemini_helper_is_non_strict(self) -> None:
        """create_gemini_optimized_schema promises to respect the caller's `required`."""
        schema = SchemaOptimizer.create_gemini_optimized_schema(_WithOptional)
        assert schema["required"] == ["required_field"]


# ── flattening still works ─────────────────────────────────────────


class TestFlattening:
    def test_refs_are_inlined(self) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_WithNested)
        assert "$defs" not in schema
        for node in _walk(schema):
            assert "$ref" not in node

    def test_array_items_survive(self) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_WithNested)
        items = schema["properties"]["items"]["items"]
        assert items["type"] == "object"
        assert "index" in items["properties"]

    def test_descriptions_preserved(self) -> None:
        class _Described(BaseModel):
            field_a: str = Field(description="A very important field")

        schema = SchemaOptimizer.create_optimized_json_schema(_Described)
        assert schema["properties"]["field_a"]["description"] == "A very important field"
