"""Tests for ActionDescriptor evolution (with_prefix, to_tool_schema, params_schema)."""

from spectragent.schemas.action import ActionDescriptor


class TestWithPrefix:
    def test_basic_prefix(self) -> None:
        d = ActionDescriptor(name="click", description="Click", params={"idx": "int"})
        p = d.with_prefix("browser")
        assert p.name == "browser.click"
        assert p.description == "Click"
        assert p.params == {"idx": "int"}

    def test_preserves_params_schema(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        d = ActionDescriptor(name="t", description="t", params_schema=schema)
        p = d.with_prefix("cap")
        assert p.params_schema == schema

    def test_original_unchanged(self) -> None:
        d = ActionDescriptor(name="click", description="Click")
        d.with_prefix("browser")
        assert d.name == "click"


class TestToToolSchema:
    def test_simple_params(self) -> None:
        d = ActionDescriptor(name="click", description="Click it", params={"index": "int"})
        s = d.to_tool_schema()
        assert s["type"] == "function"
        assert s["function"]["name"] == "click"
        assert s["function"]["description"] == "Click it"
        assert s["function"]["parameters"]["properties"]["index"]["type"] == "int"

    def test_rich_params_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {"tool_name": {"type": "string"}},
            "required": ["tool_name"],
        }
        d = ActionDescriptor(name="call", description="Call", params_schema=schema)
        s = d.to_tool_schema()
        assert s["function"]["parameters"] == schema

    def test_params_schema_takes_priority(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        d = ActionDescriptor(
            name="t", description="t", params={"b": "int"}, params_schema=schema,
        )
        s = d.to_tool_schema()
        assert "a" in s["function"]["parameters"]["properties"]
        assert "b" not in s["function"]["parameters"]["properties"]

    def test_empty_params(self) -> None:
        d = ActionDescriptor(name="wait", description="Wait")
        s = d.to_tool_schema()
        assert s["function"]["parameters"]["properties"] == {}


class TestEnvTypesRemoved:
    def test_no_env_types_field(self) -> None:
        assert "env_types" not in ActionDescriptor.model_fields

    def test_params_default_empty(self) -> None:
        d = ActionDescriptor(name="x", description="x")
        assert d.params == {}
