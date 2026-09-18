"""Gemini-specific schema fixes.

What is *genuinely* Gemini-specific lives here: ``$ref`` resolution against
a local ``$defs`` block, dropping ``default`` (the protobuf ``Schema`` has
no slot for it), and the ``_placeholder`` invention that keeps an empty
``properties`` map from compiling to an object with no fields.

The ``additionalProperties`` removal is **not** specific to this function
even though it is specific to Gemini: it is one of the named dialect
transforms (:meth:`SchemaOptimizer.strip_additional_properties`), because
``ChatGoogle`` has to apply it on requests that never come through here —
a model the family table does not recognise skips ``fix_gemini_schema``
entirely. So this delegates rather than filtering the key inline, and
there is one implementation of "what Gemini does with
``additionalProperties``" reachable from both call sites.
"""

from typing import Any

from spectragent.core.llm.schema.optimizer import SchemaOptimizer


def fix_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic schema to a Gemini-compatible schema.

    Resolves the ``$ref`` references Gemini does not support, drops the
    keywords its ``Schema`` message has no field for, and fills an empty
    ``properties`` map with a placeholder.
    """
    # Handle $defs and $ref resolution
    if "$defs" in schema:
        defs = schema.pop("$defs")

        def resolve_refs(obj: Any) -> Any:
            if isinstance(obj, dict):
                if "$ref" in obj:
                    ref = obj.pop("$ref")
                    ref_name = ref.split("/")[-1]
                    if ref_name in defs:
                        resolved = defs[ref_name].copy()
                        for key, value in obj.items():
                            if key != "$ref":
                                resolved[key] = value
                        return resolve_refs(resolved)
                    return obj
                else:
                    return {k: resolve_refs(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [resolve_refs(item) for item in obj]
            return obj

        schema = resolve_refs(schema)

    # Remove unsupported properties
    def clean_schema(obj: Any, parent_key: str | None = None) -> Any:
        if isinstance(obj, dict):
            cleaned: dict[str, Any] = {}
            for key, value in obj.items():
                is_metadata_title = key == "title" and parent_key != "properties"
                if key != "default" and not is_metadata_title:
                    cleaned_value = clean_schema(value, parent_key=key)
                    if (
                        key == "properties"
                        and isinstance(cleaned_value, dict)
                        and len(cleaned_value) == 0
                        and isinstance(obj.get("type", ""), str)
                        and obj.get("type", "").upper() == "OBJECT"
                    ):
                        cleaned["properties"] = {"_placeholder": {"type": "string"}}
                    else:
                        cleaned[key] = cleaned_value

            if (
                isinstance(cleaned.get("type", ""), str)
                and cleaned.get("type", "").upper() == "OBJECT"
                and "properties" in cleaned
                and isinstance(cleaned["properties"], dict)
                and len(cleaned["properties"]) == 0
            ):
                cleaned["properties"] = {"_placeholder": {"type": "string"}}

            return cleaned
        elif isinstance(obj, list):
            return [clean_schema(item, parent_key=parent_key) for item in obj]
        return obj

    cleaned = clean_schema(schema)
    # One implementation, called from both places that need it — see the
    # module docstring. Idempotent, so ``ChatGoogle`` calling it again on
    # the way to the wire costs nothing and covers the models that skip
    # this function.
    SchemaOptimizer.strip_additional_properties(cleaned)
    return cleaned
