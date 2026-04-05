"""Gemini-specific schema fixes."""

from typing import Any


def fix_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic schema to a Gemini-compatible schema.

    Removes unsupported properties like ``additionalProperties`` and resolves
    ``$ref`` references that Gemini doesn't support.
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
                if key not in ["additionalProperties", "default"] and not is_metadata_title:
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

    return clean_schema(schema)
