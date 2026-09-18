"""
Utilities for creating optimized Pydantic schemas for LLM usage.

Three policies live here and they are deliberately separate:

* **Flattening** — inline every ``$ref``/``$defs`` and drop metadata the models
  do not need. Every provider wants this.
* **Strict-mode compatibility** — force ``required`` to list *all* properties.
  Only OpenAI-style ``"strict": true`` structured outputs want this, so it is
  opt-in via ``strict=True``. Applying it unconditionally encoded one agent's
  output shape as global policy for every caller and silently made genuinely
  optional fields mandatory.
* **Dialect transforms** — what one endpoint's schema *parser* will accept,
  which is a different question from what the schema means. See
  :ref:`the transforms below <dialect-transforms>`; they are named, in-place
  and individually testable rather than a side effect of some SDK's own
  conversion.
"""

from typing import Any

from pydantic import BaseModel

#: Keywords OpenAI's ``"strict": true`` structured-output dialect accepts.
#:
#: An *allowlist*, not a list of banned shapes, because the failure mode it
#: guards is "a keyword nobody thought about ships and the API answers 400
#: on the schema, before the model has read a word of the prompt". A
#: denylist has to be extended every time Pydantic learns to emit something
#: new; an allowlist fails closed — the request quietly drops to
#: ``strict: false``, which every endpoint accepts, instead of being
#: rejected outright.
#:
#: Deliberately absent, and each one measured or documented as a rejection:
#: ``oneOf`` (no structured-output dialect accepts it — a closed union has
#: to compile to ``anyOf``), ``discriminator`` (what produces ``oneOf``),
#: ``minItems``/``maxItems``/``uniqueItems`` (the array keywords the strict
#: dialect lists as unsupported — see ``remove_min_items``), and the
#: combinators ``allOf``/``not``/``if``/``then``/``else``/
#: ``patternProperties``/``dependentSchemas``.
STRICT_DIALECT_KEYWORDS: frozenset[str] = frozenset(
    {
        # structure
        "type", "properties", "required", "additionalProperties", "items",
        "anyOf", "$ref", "$defs",
        # value domains
        "enum", "const",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength", "pattern", "format",
        # annotations
        "description", "title", "default",
    }
)

#: Keywords whose value is *data*, not a nested schema. Walking into them
#: would read property names, enum members or a ``required`` list as if they
#: were schema keywords and answer ``False`` for a perfectly legal schema.
_STRICT_LEAF_KEYWORDS: frozenset[str] = frozenset(
    {
        "type", "required", "enum", "const", "description", "title", "default",
        "format", "pattern", "$ref",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength",
    }
)

# ``_strict_ok`` checks the allowlist *before* it checks the leaf set, so a
# keyword that is a leaf but not allowed answers ``False`` for every schema
# carrying it — a silent drop to ``strict: false``, which is the failure
# mode the allowlist exists to make loud. Held here rather than in a
# review note, because the two lists are edited together and forgotten
# separately.
assert _STRICT_LEAF_KEYWORDS <= STRICT_DIALECT_KEYWORDS, sorted(
    _STRICT_LEAF_KEYWORDS - STRICT_DIALECT_KEYWORDS
)


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

        ``minItems`` is not a preference under strict, it is a rejection —
        see :meth:`remove_min_items`, which is the transform this implies.
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

        # Final pass, through the same named transforms a provider would call
        # on its own — one implementation of "drop this keyword everywhere",
        # not a second private copy of the walk.
        if remove_min_items:
            SchemaOptimizer.remove_min_items(optimized_schema)
        if remove_defaults:
            SchemaOptimizer._pop_everywhere(optimized_schema, ("default",))

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

        Decided by :data:`STRICT_DIALECT_KEYWORDS` — an allowlist of the
        keywords the dialect accepts — plus the two structural rules the
        allowlist cannot express:

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

        This used to be a denylist of two names (``oneOf``,
        ``discriminator``), which answered ``True`` for every keyword nobody
        had hit yet. The allowlist fails the other way: an unrecognised
        keyword costs constrained decoding, never a 400 on the schema.

        What that actually buys, precisely — because it is narrower than it
        looks. Both production callers
        (:meth:`ChatModelBase._use_strict_schema`,
        :meth:`ToolDefinition.from_model`) hand this a schema compiled with
        ``strict=True``, and that compile already drops ``minItems``. So
        ``minItems`` can only reach this predicate from a direct caller.
        The keywords that *do* survive a strict compile and are caught here
        are ``maxItems``, ``uniqueItems``, and the combinators ``allOf`` /
        ``not`` / ``if`` / ``then`` / ``else`` / ``patternProperties`` /
        ``dependentSchemas`` — none of which the compiler removes and all of
        which the dialect refuses. A model carrying one now ships
        ``strict: false`` instead of a rejected request.
        """
        return SchemaOptimizer._strict_ok(schema)

    @staticmethod
    def _strict_ok(node: Any) -> bool:
        """The recursive half of :meth:`is_strict_compatible`.

        Split out because the walk has to know *where* it is: the keys of a
        ``properties`` (or ``$defs``) map are property names, not schema
        keywords, and checking them against the allowlist would refuse
        ``{"properties": {"browser.click": ...}}`` for the crime of naming a
        property after an action.
        """
        if isinstance(node, list):
            return all(SchemaOptimizer._strict_ok(item) for item in node)
        if not isinstance(node, dict):
            return True

        for key, value in node.items():
            if key not in STRICT_DIALECT_KEYWORDS:
                return False
            if key in _STRICT_LEAF_KEYWORDS:
                continue
            if key in ("properties", "$defs"):
                if not isinstance(value, dict):
                    return False
                if not all(SchemaOptimizer._strict_ok(child) for child in value.values()):
                    return False
                continue
            if key == "additionalProperties":
                # A bare ``false`` is what the dialect wants; a *schema* here
                # is a typed map, which it refuses.
                if isinstance(value, dict):
                    return False
                continue
            if not SchemaOptimizer._strict_ok(value):
                return False

        # An object that declares no ``properties`` is an open map — the one
        # structural rule the keyword allowlist cannot express.
        return not (node.get("type") == "object" and "properties" not in node)

    # ------------------------------------------------------------------
    # .. _dialect-transforms:
    #
    # Dialect transforms — what one endpoint's schema parser accepts.
    #
    # These are not opinions about the schema, they are facts about a wire
    # format, and each one is here rather than inside a provider (or, worse,
    # inside an SDK's own type conversion) so that it can be named in a
    # request, read by the next person, and tested without a network.
    #
    # All three mutate *in place* and return ``None``: they run last, on the
    # dict a provider is about to put on the wire, and a returned copy is one
    # more thing a caller can forget to assign.
    # ------------------------------------------------------------------

    @staticmethod
    def remove_min_items(schema: Any) -> None:
        """Drop ``minItems``/``min_items`` everywhere (defect F12).

        Not a preference — a rejection. The strict dialect lists
        ``minItems``/``maxItems``/``uniqueItems`` among the array keywords it
        does not accept, and a request carrying one comes back 400 on the
        schema before the model has read a word of the prompt. A Pydantic
        ``Field(min_length=1)`` on a list is an ordinary thing to write
        (``action`` carries one, so an empty action list is caught locally),
        so the removal belongs here, once, rather than in each caller's knob.
        """
        SchemaOptimizer._pop_everywhere(schema, ("minItems", "min_items"))

    @staticmethod
    def strip_additional_properties(schema: Any) -> None:
        """Drop ``additionalProperties`` everywhere — the Gemini dialect.

        Gemini's ``generationConfig.response_schema`` is a protobuf message,
        not a JSON Schema validator: an unknown field name is a parse error,
        not an ignored hint. Measured against ``generateContent`` directly,
        the root node alone is enough to fail the request::

            Invalid JSON payload received. Unknown name "additionalProperties"
            at 'generation_config.response_schema': Cannot find field.

        ``google-genai``'s typed ``Schema`` conversion happens to drop the key
        on the way out, which is why the provider worked without this call.
        That is an SDK side effect, invisible from the codebase and untested;
        this makes it the provider's own statement.

        Nothing is lost: Pydantic still refuses unknown keys at parse time
        (``extra="forbid"``), so a model that invents a property is caught —
        one round trip later instead of by the decoder.
        """
        SchemaOptimizer._pop_everywhere(schema, ("additionalProperties",))

    @staticmethod
    def open_union_branches(schema: Any) -> None:
        """Drop ``additionalProperties`` inside every ``anyOf``/``oneOf`` branch.

        The root node keeps its closure. That asymmetry is measured, not
        chosen: an OpenAI-shaped request carrying a closed union reaches
        Gemini through a gateway's translator as
        ``generation_config.response_schema`` and fails at
        ``...items.any_of[0]``, while the same payload with open branches and
        a closed root is accepted (probe case B). Root-level
        ``additionalProperties`` survives the translation; the branches do not.

        The *whole branch subtree* is opened, not just the branch's top node:
        af's action union nests one object per verb inside each branch
        (``{"browser.click": {...}}``), and the translator reports each nested
        node under its own ``any_of[N]`` path.

        Only for dialects that ask for it — OpenAI's own strict mode requires
        ``additionalProperties: false`` on *every* object, branches included,
        so applying this unconditionally would trade one 400 for another. The
        model family declares which side it is on
        (``ModelFamily.schema_rejects_closed_union_branches``).
        """
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key in ("anyOf", "oneOf") and isinstance(value, list):
                    for branch in value:
                        SchemaOptimizer._pop_everywhere(branch, ("additionalProperties",))
                elif key != "required" and isinstance(value, (dict, list)):
                    SchemaOptimizer.open_union_branches(value)
        elif isinstance(schema, list):
            for item in schema:
                SchemaOptimizer.open_union_branches(item)

    @staticmethod
    def _pop_everywhere(node: Any, keys: tuple[str, ...]) -> None:
        """Remove *keys* from every mapping in *node*, recursively, in place."""
        if isinstance(node, dict):
            for key in keys:
                node.pop(key, None)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    SchemaOptimizer._pop_everywhere(value, keys)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    SchemaOptimizer._pop_everywhere(item, keys)

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
