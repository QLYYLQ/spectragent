"""
Utilities for creating optimized Pydantic schemas for LLM usage.

Two policies live here and they are deliberately separate:

* **Flattening** — inline every ``$ref``/``$defs`` and drop metadata the models
  do not need. Every provider wants this.
* **Strict-mode compatibility** — force ``required`` to list *all* properties.
  Only OpenAI-style ``"strict": true`` structured outputs want this, so it is
  opt-in via ``strict=True``. Applying it unconditionally encoded one agent's
  output shape as global policy for every caller and silently made genuinely
  optional fields mandatory.
"""

from typing import Any

from pydantic import BaseModel


class SchemaOptimizer:
    @staticmethod
    def create_optimized_json_schema(
        model: type[BaseModel],
        *,
        strict: bool = False,
        remove_min_items: bool = False,
        remove_defaults: bool = False,
    ) -> dict[str, Any]:
        """
        Create the most optimized schema by flattening all $ref/$defs while preserving
        FULL descriptions and ALL action definitions.

        Args:
                model: The Pydantic model to optimize
                strict: If True, emit OpenAI's ``"strict": true`` dialect: every
                        object's ``required`` lists all of its properties, and the
                        array keywords that dialect refuses are dropped (see below).
                        Leave False for tool-use / prompt-JSON / Gemini paths, which
                        honour the model's own optionality.
                remove_min_items: If True, remove minItems from the schema. Implied by
                        ``strict``; set it for a *non-strict* endpoint that also chokes
                        on it.
                remove_defaults: If True, remove default values from the schema.

        ``minItems`` is not a preference under strict, it is a rejection: the
        strict dialect lists ``minItems``/``maxItems``/``uniqueItems`` among the
        array keywords it does not accept, and a request carrying one comes back
        400 on the schema — before the model has read a word of the prompt. A
        Pydantic ``Field(min_length=1)`` on a list is an ordinary thing to write
        (``action`` carries one, so an empty action list is caught locally), so
        the removal belongs here, once, rather than in each caller's knob.
        ``default`` is left alone deliberately: it is not on that list, and under
        strict every property is required, so a default can never be applied.

        Returns:
                Optimized schema with all $refs resolved
        """
        # Strict is a dialect, not a flag the caller can half-apply.
        remove_min_items = remove_min_items or strict

        # Generate original schema
        original_schema = model.model_json_schema()

        # Extract $defs for reference resolution, then flatten everything
        defs_lookup = original_schema.get("$defs", {})

        # Create optimized schema with flattening
        # Pass flags to optimize_schema via closure
        def optimize_schema(
            obj: Any, defs_lookup: dict[str, Any] | None = None, *, in_properties: bool = False
        ) -> Any:
            """Apply all optimization techniques including flattening all $ref/$defs"""
            if isinstance(obj, dict):
                optimized: dict[str, Any] = {}
                flattened_ref: dict[str, Any] | None = None

                # Inline $defs rather than carrying them along
                skip_fields = ["$defs"]

                for key, value in obj.items():
                    if key in skip_fields:
                        continue

                    # Skip metadata "title" unless we're iterating inside an actual `properties` map
                    if key == "title" and not in_properties:
                        continue

                    # Preserve FULL descriptions without truncation, skip empty ones
                    elif key == "description":
                        if value:  # Only include non-empty descriptions
                            optimized[key] = value

                    # Handle type field - must recursively process in case value contains $ref
                    elif key == "type":
                        optimized[key] = (
                            value
                            if not isinstance(value, (dict, list))
                            else optimize_schema(value, defs_lookup)
                        )

                    # FLATTEN: Resolve $ref by inlining the actual definition
                    elif key == "$ref" and defs_lookup:
                        ref_path = value.split("/")[
                            -1
                        ]  # Get the definition name from "#/$defs/SomeName"
                        if ref_path in defs_lookup:
                            # Get the referenced definition and flatten it
                            referenced_def = defs_lookup[ref_path]
                            flattened_ref = optimize_schema(referenced_def, defs_lookup)

                    # Preserve typed-map value schemas (``dict[str, X]`` compiles to
                    # ``additionalProperties: {...}``). Dropping it and then forcing
                    # ``False`` turned every typed map into "no keys allowed".
                    # Bare booleans are re-derived by _apply_object_policy below.
                    elif key == "additionalProperties":
                        if isinstance(value, dict):
                            optimized[key] = optimize_schema(value, defs_lookup)

                    # Skip minItems/min_items and default if requested (check BEFORE processing)
                    elif key in ("minItems", "min_items") and remove_min_items:
                        continue  # Skip minItems/min_items
                    elif key == "default" and remove_defaults:
                        continue  # Skip default values

                    # Keep all anyOf structures (action unions) and resolve any $refs within
                    elif key == "anyOf" and isinstance(value, list):
                        optimized[key] = [optimize_schema(item, defs_lookup) for item in value]

                    # Recursively optimize nested structures
                    elif key in ["properties", "items"]:
                        optimized[key] = optimize_schema(
                            value,
                            defs_lookup,
                            in_properties=(key == "properties"),
                        )

                    # Keep essential validation fields
                    elif key in [
                        "type",
                        "required",
                        "minimum",
                        "maximum",
                        "minItems",
                        "min_items",
                        "maxItems",
                        "pattern",
                        "default",
                    ]:
                        optimized[key] = (
                            value
                            if not isinstance(value, (dict, list))
                            else optimize_schema(value, defs_lookup)
                        )

                    # Recursively process all other fields
                    else:
                        optimized[key] = (
                            optimize_schema(value, defs_lookup)
                            if isinstance(value, (dict, list))
                            else value
                        )

                # If we have a flattened reference, merge it with the optimized properties
                if flattened_ref is not None and isinstance(flattened_ref, dict):
                    # Start with the flattened reference as the base
                    result = flattened_ref.copy()

                    # Merge in any sibling properties that were processed
                    for key, value in optimized.items():
                        # Preserve descriptions from the original object if they exist
                        if (
                            key == "description"
                            and "description" not in result
                            or key != "description"
                        ):
                            result[key] = value

                    return result

                # No $ref, just return the optimized object
                return optimized

            elif isinstance(obj, list):
                return [
                    optimize_schema(item, defs_lookup, in_properties=in_properties) for item in obj
                ]
            return obj

        optimized_result = optimize_schema(original_schema, defs_lookup)

        # Ensure we have a dictionary (should always be the case for schema root)
        if not isinstance(optimized_result, dict):
            raise ValueError("Optimized schema result is not a dictionary")

        optimized_schema: dict[str, Any] = optimized_result

        SchemaOptimizer._apply_object_policy(optimized_schema, strict=strict)

        # Final pass to remove minItems/min_items and default values if requested
        if remove_min_items or remove_defaults:

            def remove_forbidden_fields(obj: Any) -> None:
                """Recursively remove minItems/min_items and default values"""
                if isinstance(obj, dict):
                    # Remove forbidden keys
                    if remove_min_items:
                        obj.pop("minItems", None)
                        obj.pop("min_items", None)
                    if remove_defaults:
                        obj.pop("default", None)
                    # Recursively process all values
                    for value in obj.values():
                        if isinstance(value, (dict, list)):
                            remove_forbidden_fields(value)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, (dict, list)):
                            remove_forbidden_fields(item)

            remove_forbidden_fields(optimized_schema)

        return optimized_schema

    @staticmethod
    def _apply_object_policy(schema: dict[str, Any] | list[Any], *, strict: bool) -> None:
        """Normalise every object node in place.

        Objects that declare ``properties`` come from a closed Pydantic model, so
        ``additionalProperties: false`` is always right for them. Objects *without*
        ``properties`` are free-form maps (``dict[str, Any]``) or typed maps
        (``dict[str, int]``) — forcing ``additionalProperties: false`` on those is
        what made ``list[dict[str, Any]]`` compile to a schema whose only legal
        value is ``[{}]``, silently discarding every tool call's arguments.

        ``required`` is only rewritten under *strict*, where the API mandates that
        every property be listed.
        """
        if isinstance(schema, dict):
            # Recurse first so nested objects are handled regardless of this level.
            for key, value in schema.items():
                if isinstance(value, (dict, list)) and key != "required":
                    SchemaOptimizer._apply_object_policy(value, strict=strict)

            if schema.get("type") == "object" and "properties" in schema:
                schema["additionalProperties"] = False
                if strict:
                    schema["required"] = list(schema["properties"].keys())

        elif isinstance(schema, list):
            for item in schema:
                SchemaOptimizer._apply_object_policy(item, strict=strict)

    @staticmethod
    def is_strict_compatible(schema: dict[str, Any] | list[Any]) -> bool:
        """Whether *schema* can legally ship under OpenAI's ``"strict": true``.

        The strict dialect requires every object node to declare ``properties``
        and to close itself with ``additionalProperties: false``. Two shapes
        cannot comply, and both are produced by ordinary Pydantic models:

        * **open maps** — ``dict[str, Any]`` compiles to an object node with no
          ``properties`` at all. An agent that accepted ``list[dict[str, Any]]``
          actions produced exactly that, which is why a constrained decoder
          could answer ``{}`` and nothing rejected it.
        * **typed maps** — ``dict[str, int]`` compiles to
          ``additionalProperties: {...}``, which strict forbids outright.

        Rewriting either to satisfy strict is what silently discarded every
        tool call's arguments, so the schema stays honest and the *request*
        gives up ``strict`` instead. Callers pair this with the model-family
        capability: strict is emitted only when the model supports it **and**
        the compiled schema can express it.

        A third shape is refused wherever it appears: ``oneOf``, and the
        ``discriminator`` that produces it. None of the structured-output
        dialects accepts ``oneOf`` — a closed union has to compile to
        ``anyOf`` — and this function used to answer ``True`` for a schema
        full of it, so the request went out strict and was rejected by the
        API with nothing in the codebase pointing at why.
        """
        if isinstance(schema, dict):
            if "oneOf" in schema or "discriminator" in schema:
                return False
            if schema.get("type") == "object":
                if "properties" not in schema:
                    return False
                if isinstance(schema.get("additionalProperties"), dict):
                    return False
            return all(
                SchemaOptimizer.is_strict_compatible(value)
                for key, value in schema.items()
                if isinstance(value, (dict, list)) and key != "required"
            )

        if isinstance(schema, list):
            return all(
                SchemaOptimizer.is_strict_compatible(item)
                for item in schema
                if isinstance(item, (dict, list))
            )

        return True

    @staticmethod
    def create_gemini_optimized_schema(model: type[BaseModel]) -> dict[str, Any]:
        """
        Create Gemini-optimized schema, preserving explicit `required` arrays so Gemini
        respects mandatory fields defined by the caller.

        Args:
                model: The Pydantic model to optimize

        Returns:
                Optimized schema suitable for Gemini structured output
        """
        # Gemini has no strict mode — keep the model's own `required`, which is
        # exactly what this method's name promises.
        return SchemaOptimizer.create_optimized_json_schema(model, strict=False)
