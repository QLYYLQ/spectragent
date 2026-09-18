"""Schema *dialects* — what an endpoint's parser accepts, tested without one.

A dialect rule is a fact about a wire format, and af learned every one of
these the expensive way: a live probe against a gateway, reading a 400 that
names a protobuf field path. That knowledge used to live in three places
where nothing could test it —

* inside ``google-genai``'s conversion from ``dict`` to its typed
  ``Schema``, which silently drops unknown keywords (so af's Gemini
  provider was correct by accident);
* inside a per-config boolean, ``dont_force_structured_output``, which
  "fixed" a lost schema by sending no schema at all;
* nowhere, for the OpenAI-compatible route, which is the one most likely
  to be translating to a vendor whose native API af could have spoken.

— and now lives in named transforms on :class:`SchemaOptimizer`. These
tests are the regression guards for the measurements, so the next person
does not have to re-run the probes to know why the code looks like this.

Probe scripts and their raw results: ``dmx_probe3.py`` (which shape breaks),
``dmx_probe4.py`` (gateway or family?), ``dmx_native_probe.py`` (native
endpoints), ``af_native_route2.py`` / ``af_haiku.py`` (af's real payload).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers them
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.model_families import GeminiFamily, GPTFamily
from spectragent.core.llm.schema.optimizer import (
    STRICT_DIALECT_KEYWORDS,
    SchemaOptimizer,
)

# ── shared fixtures ────────────────────────────────────────────────


def _agent_output_model() -> type[BaseModel]:
    """The per-run output model a real browser run ships.

    Built from *real* descriptors and the real agent, because the shape
    under test is the one the closed action union compiles to, not a
    hand-written stand-in for it.
    """
    keys = sorted(ActionRegistrar.get_all())
    descriptors = [ActionRegistrar.get(key)().descriptor() for key in keys]
    agent = BrowserUseAgent()
    agent.prepare(task="t", action_descriptors=descriptors)
    model = agent.get_react_output_model()
    assert model is not None
    return model


def _walk(node: Any):
    """Yield every dict node in a schema."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _closed_union_schema() -> dict[str, Any]:
    """The shape probe 3 isolated: ``anyOf`` of closed branches, closed root.

    Written out rather than compiled so the test states the input it is
    asserting on. It matches what ``agents/_action_union.py`` produces:
    each branch names one verb as a *property*, and the params object
    inside it is closed too — which is why the transform has to open the
    whole branch subtree, not just the branch's top node.
    """
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                verb: {
                                    "type": "object",
                                    "properties": {"index": {"type": "integer"}},
                                    "required": ["index"],
                                    "additionalProperties": False,
                                }
                            },
                            "required": [verb],
                            "additionalProperties": False,
                        }
                        for verb in ("browser.click", "browser.type")
                    ]
                },
            }
        },
        "required": ["action"],
        "additionalProperties": False,
    }


# ── 1. open_union_branches ─────────────────────────────────────────


class TestOpenUnionBranches:
    """dmx_probe3: A pass, B pass, C 400, D 400.

    A = flat closed (no union). B = union with *open* branches, closed
    root. C/D = union with closed branches. Only C and D fail, and they
    fail naming ``...response_schema.properties[0].value.items.any_of[0]``
    — so the branch is the problem and the root is not.
    """

    def test_branches_lose_it_and_the_root_keeps_it(self) -> None:
        schema = _closed_union_schema()
        SchemaOptimizer.open_union_branches(schema)

        assert schema["additionalProperties"] is False, "root closure was measured to pass"
        for branch in schema["properties"]["action"]["items"]["anyOf"]:
            assert "additionalProperties" not in branch

    def test_the_whole_branch_subtree_is_opened(self) -> None:
        """Not just the branch's top node.

        af's union nests one params object per verb *inside* each branch.
        The translator reports those under the same ``any_of[N]`` path, so
        an implementation that popped only the top node would still 400 —
        and every existing test would still pass.
        """
        schema = _closed_union_schema()
        SchemaOptimizer.open_union_branches(schema)

        branches = schema["properties"]["action"]["items"]["anyOf"]
        params = branches[0]["properties"]["browser.click"]
        assert "additionalProperties" not in params

    def test_a_closed_object_outside_any_union_is_untouched(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "memory": {"type": "string"},
                "meta": {
                    "type": "object",
                    "properties": {"step": {"type": "integer"}},
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        SchemaOptimizer.open_union_branches(schema)

        assert schema["additionalProperties"] is False
        assert schema["properties"]["meta"]["additionalProperties"] is False

    def test_one_of_branches_too(self) -> None:
        """af never emits ``oneOf``, but a caller's own model might."""
        schema = {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "additionalProperties": False,
                }
            ]
        }
        SchemaOptimizer.open_union_branches(schema)
        assert "additionalProperties" not in schema["oneOf"][0]

    def test_required_is_not_walked_as_a_schema(self) -> None:
        """``required`` holds property *names*; one of them may be a keyword."""
        schema = {
            "type": "object",
            "properties": {"additionalProperties": {"type": "string"}},
            "required": ["additionalProperties"],
            "anyOf": [{"type": "object", "properties": {}, "additionalProperties": False}],
        }
        SchemaOptimizer.open_union_branches(schema)
        assert schema["required"] == ["additionalProperties"]


# ── 2. strip_additional_properties ─────────────────────────────────


class TestStripAdditionalProperties:
    """dmx_native_probe: native Gemini rejects it *anywhere*, root included.

    ``gemini_native[root]_closed`` → 400,
    ``Unknown name "additionalProperties" at
    'generation_config.response_schema'``. Same payload with the key
    removed → 200. So the native route is a strictly stronger rule than
    the translated one above.
    """

    def test_every_node_loses_it(self) -> None:
        schema = _closed_union_schema()
        SchemaOptimizer.strip_additional_properties(schema)
        assert "additionalProperties" not in json.dumps(schema)

    def test_a_typed_map_loses_it_too(self) -> None:
        """``dict[str, int]`` compiles to a *schema*-valued one; Gemini
        rejects the field name regardless of what the value is."""
        schema = {"type": "object", "additionalProperties": {"type": "integer"}}
        SchemaOptimizer.strip_additional_properties(schema)
        assert schema == {"type": "object"}


# ── 3. remove_min_items (defect F12) ───────────────────────────────


class TestRemoveMinItems:
    def test_it_is_removed_everywhere(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "tags": {"type": "array", "min_items": 2},
            },
        }
        SchemaOptimizer.remove_min_items(schema)
        assert "minItems" not in json.dumps(schema)
        assert "min_items" not in json.dumps(schema)

    def test_the_compile_flag_routes_through_the_same_transform(self) -> None:
        """``strict=True`` implies it — one implementation, not two."""

        class _Model(BaseModel):
            values: list[int] = Field(default_factory=list, min_length=1)

        schema = SchemaOptimizer.create_optimized_json_schema(_Model, strict=True)
        assert "minItems" not in json.dumps(schema)


# ── 4. What af actually sends to a Gemini route ────────────────────


class _FakeGoogleModels:
    """Records the ``config`` every generate_content call was given."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._payload = payload

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed=None,
            text=json.dumps(self._payload),
            usage_metadata=None,
            candidates=[],
        )


class TestWhatReachesAGeminiRoute:
    """The assertion the SDK side effect made impossible to write.

    ``ChatGoogle`` handed a plain ``dict`` to ``google-genai``, which built
    a typed ``Schema`` from it and dropped the keys Gemini has no field
    for. The request was therefore correct, and nothing in af said so —
    pin the SDK version, or swap the SDK, and the provider quietly starts
    sending a schema that 400s.
    """

    def _chat(self, model_name: str):
        from spectragent.core.llm.providers.google import ChatGoogle

        return ChatGoogle(model=model_name, api_key="stub")

    async def _response_schema(self, model_name: str) -> dict[str, Any]:
        payload = {"memory": "m", "action": [{"browser.click": {"index": 1}}]}
        chat = self._chat(model_name)
        models = _FakeGoogleModels(payload)
        chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
            aio=SimpleNamespace(models=models)
        )
        await chat.ainvoke([UserMessage(content="hi")], _agent_output_model())
        return models.calls[0]["config"]["response_schema"]

    async def test_no_additional_properties_reaches_the_wire(self) -> None:
        schema = await self._response_schema("gemini-2.5-flash")
        assert "additionalProperties" not in json.dumps(schema)

    async def test_it_holds_for_a_model_outside_the_gemini_family(self) -> None:
        """The endpoint decides this, not the family table.

        ``fix_gemini_schema`` runs only when the family declares
        ``schema_requires_gemini_fix``. A model name the table does not
        recognise therefore skipped it — and still went to a
        ``generateContent`` endpoint, which has no such field.
        """
        from spectragent.core.llm.model_families import detect_model_family

        family = detect_model_family("some-unlisted-model")
        assert family.schema_requires_gemini_fix is False, "pick a name the table misses"

        schema = await self._response_schema("some-unlisted-model")
        assert "additionalProperties" not in json.dumps(schema)

    async def test_the_action_union_survives_the_strip(self) -> None:
        """Opening the objects must not cost the schema its meaning."""
        schema = await self._response_schema("gemini-2.5-flash")
        branches = schema["properties"]["action"]["items"]["anyOf"]
        verbs = {next(iter(b["properties"])) for b in branches}
        assert "browser.click" in verbs and "browser.done" in verbs

    def test_gemini_tool_declarations_are_stripped_too(self) -> None:
        """The other thing this provider puts a schema in."""
        from spectragent.core.llm.serializers.google_format import (
            GoogleMessageSerializer,
        )
        from spectragent.core.llm.tools import ToolDefinition

        tool = ToolDefinition.from_model(_agent_output_model(), name="emit")
        serialized = GoogleMessageSerializer.serialize_tools([tool])
        assert "additionalProperties" not in json.dumps(serialized, default=str)


class TestWhatReachesATranslatedGeminiRoute:
    """The OpenAI-compatible route, where only the branches are the problem."""

    def _chat(self, model_name: str):
        from spectragent.core.llm.providers.openai import ChatOpenAI

        return ChatOpenAI(model=model_name, api_key="stub", base_url="https://gw.invalid/v1")

    async def _schema(self, model_name: str) -> dict[str, Any]:
        payload = {"memory": "m", "action": [{"browser.click": {"index": 1}}]}
        captured: dict[str, Any] = {}

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(payload), tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        chat = self._chat(model_name)
        chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=_Completions())
        )
        await chat.ainvoke([UserMessage(content="hi")], _agent_output_model())
        return captured["response_format"]["json_schema"]["schema"]

    async def test_a_gemini_model_gets_open_branches(self) -> None:
        assert GeminiFamily.schema_rejects_closed_union_branches is True
        schema = await self._schema("gemini-3.5-flash")

        assert schema["additionalProperties"] is False
        for branch in schema["properties"]["action"]["items"]["anyOf"]:
            assert "additionalProperties" not in json.dumps(branch)

    async def test_an_openai_model_keeps_them_closed(self) -> None:
        """Not unconditional: OpenAI's strict dialect requires the closure
        on *every* object, branches included. Applying the Gemini rule to
        a GPT request would trade one 400 for another."""
        assert GPTFamily.schema_rejects_closed_union_branches is False
        schema = await self._schema("gpt-4o")

        branches = schema["properties"]["action"]["items"]["anyOf"]
        assert all(b["additionalProperties"] is False for b in branches)


# ── 5. The plain-union decision, pinned ────────────────────────────


class TestTheUnionStaysPlain:
    """A Pydantic *discriminated* union is unusable on all three routes.

    Measured, each for a different reason, which is why no single provider
    fix would recover it:

    * **Gemini** rejects ``discriminator`` and ``const`` outright.
    * **google-genai** rejects it in its own local pydantic validation,
      before a request is ever made — so it cannot even be probed.
    * **Anthropic** rejects ``oneOf``.

    So ``agents/_action_union.py`` builds a *plain* ``Union`` of single-key
    models: the verb is the property **name**, which needs nothing beyond
    ``object``/``properties``/``required``/``anyOf`` — the intersection of
    the three dialects. This test is what stops someone from "tidying" it
    into a discriminated union, which reads better and does not work.
    """

    @pytest.mark.parametrize("strict", [False, True])
    def test_no_discriminator_no_const_no_one_of(self, strict: bool) -> None:
        schema = SchemaOptimizer.create_optimized_json_schema(_agent_output_model(), strict=strict)
        text = json.dumps(schema)
        assert "discriminator" not in text
        assert "const" not in text
        assert "oneOf" not in text

    def test_the_union_is_an_any_of_of_single_key_objects(self) -> None:
        """The positive half: what it *is*, not only what it is not."""
        schema = SchemaOptimizer.create_optimized_json_schema(_agent_output_model(), strict=True)
        branches = schema["properties"]["action"]["items"]["anyOf"]
        assert branches
        for branch in branches:
            assert len(branch["properties"]) == 1
            assert len(branch["required"]) == 1

    def test_every_keyword_it_emits_is_in_the_strict_allowlist(self) -> None:
        """The compiled payload and the allowlist must not drift apart.

        If a future action param makes Pydantic emit something new, this
        fails here — loudly, in a test — instead of as a silent drop to
        ``strict: false`` on the next real run.
        """
        schema = SchemaOptimizer.create_optimized_json_schema(_agent_output_model(), strict=True)
        seen: set[str] = set()

        def collect(node: Any, *, in_properties: bool = False) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if not in_properties:
                        seen.add(key)
                    if key == "required":
                        continue
                    collect(value, in_properties=(key == "properties"))
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        collect(schema)
        assert seen <= STRICT_DIALECT_KEYWORDS, sorted(seen - STRICT_DIALECT_KEYWORDS)

    def test_the_leaf_set_is_a_subset_of_the_allowlist(self) -> None:
        """The other direction of drift, which the payload scan cannot see.

        ``_strict_ok`` checks the allowlist *first*: a keyword added to
        ``_STRICT_LEAF_KEYWORDS`` but not to ``STRICT_DIALECT_KEYWORDS``
        makes every schema carrying it answer ``False`` — a silent drop to
        ``strict: false``, which is exactly the failure the allowlist was
        introduced to make loud. An instruction in a docstring is not a
        guard; this is.
        """
        from spectragent.core.llm.schema.optimizer import _STRICT_LEAF_KEYWORDS

        assert _STRICT_LEAF_KEYWORDS <= STRICT_DIALECT_KEYWORDS, sorted(
            _STRICT_LEAF_KEYWORDS - STRICT_DIALECT_KEYWORDS
        )


# ── 6. One compiler, three readers ─────────────────────────────────


class TestOneSchemaCompiler:
    """``SchemaOptimizer.create_optimized_json_schema`` is the only answer.

    Three answers used to exist: ``params_view``'s raw
    ``model_json_schema()``, ``ToolDefinition`` (which wraps the optimizer
    for the provider wire format), and an SDK's implicit conversion. For
    every action shipped today they happen to agree — the browser's params
    are flat scalars and the optimizer's only effect on those is dropping
    ``title``. So this is not a bug fix; it is the removal of a divergence
    that the tree has not yet written but the architecture permits.

    ``ToolDefinition`` itself stays out of ``core/actions``: merging it
    with ``ActionDescriptor`` is deferred with an open blocker
    (``docs/action_descriptor_vs_tool_definition.md``), and sharing the
    compiler is not that merge. The last test here is what keeps the two
    honest without the import.
    """

    def test_a_string_enum_param_survives_the_descriptor(self) -> None:
        """The one divergence that is reachable under today's rules.

        ``architecture_design.md`` §4.5 admits a string enum as a param
        type. Written as a Python ``str``-``Enum`` subclass (rather than
        ``Literal``, which every shipped action happens to use) Pydantic
        compiles it to ``$ref`` + ``$defs`` — and ``params_view`` reads
        ``properties`` and drops the rest, so the raw path published a
        reference with nothing to resolve against. It does not fail at the
        provider: it fails much later, when an agent compiles the closed
        union and the node has no ``type`` and no ``enum``.
        """
        from enum import StrEnum
        from typing import Annotated

        from pydantic import BaseModel as PydanticBaseModel

        from spectragent.agents._action_union import (
            UnsupportedParamsSchema,
            params_model_from_schema,
        )
        from spectragent.core.actions.params import params_schema
        from spectragent.schemas.action import ActionDescriptor

        class _Direction(StrEnum):
            up = "up"
            down = "down"

        class _Params(PydanticBaseModel):
            direction: Annotated[_Direction, Field(description="which way")]

        schema = params_schema(_Params)
        assert "$ref" not in json.dumps(schema)
        assert schema["properties"]["direction"]["enum"] == ["up", "down"]

        def _compile(node: dict[str, Any]) -> Any:
            return params_model_from_schema(
                ActionDescriptor(
                    name="probe.enum",
                    description="d",
                    params={"direction": ""},
                    params_schema=node,
                )
            )

        # The compiled schema goes through; the raw one would not have.
        assert _compile(schema).model_fields["direction"].annotation is not None
        raw_node = _Params.model_json_schema()["properties"]["direction"]
        with pytest.raises(UnsupportedParamsSchema):
            _compile(
                {
                    "type": "object",
                    "properties": {"direction": raw_node},
                    "required": ["direction"],
                    "additionalProperties": False,
                }
            )

    def test_every_real_action_compiles_to_a_ref_free_schema(self) -> None:
        """Whatever is registered when this runs — an action from another
        capability is exactly the case the raw compiler would break on
        first, since browser params happen to be flat scalars."""
        for key in sorted(ActionRegistrar.get_all()):
            schema = ActionRegistrar.get(key)().descriptor().params_schema or {}
            assert "$ref" not in json.dumps(schema), key

    def test_the_tool_path_and_the_descriptor_path_agree(self) -> None:
        """One compiler means the two readers see the same node.

        Native function calling reaches a params model through
        ``ToolDefinition.from_model``; the prompt reaches it through
        ``params_schema``. Neither imports the other — this is the test
        that would fail if one of them grew a second compiler.

        With no fragment filtering and no hints, the only differences left
        are the two the descriptor removes on purpose: ``default`` (the
        capability's knob applies it, not the model) and the rewritten
        ``description``.
        """
        from pydantic import BaseModel as PydanticBaseModel

        from spectragent.core.actions.params import params_schema
        from spectragent.core.llm.tools import ToolDefinition

        class _Params(PydanticBaseModel):
            index: int = Field(description="element index")
            amount: int | None = Field(default=None, description="how far")

        from_tool = ToolDefinition.from_model(_Params).parameters
        from_descriptor = params_schema(_Params)

        assert set(from_tool["properties"]) == set(from_descriptor["properties"])
        assert from_tool["required"] == from_descriptor["required"]
        assert from_tool["additionalProperties"] == from_descriptor["additionalProperties"]
        assert from_tool["properties"]["index"]["type"] == "integer"
        assert "default" not in from_descriptor["properties"]["amount"]

    def test_the_action_layer_does_not_import_the_provider_facing_type(self) -> None:
        """``ActionDescriptor``/``ToolDefinition`` merge is deferred.

        ``architecture_design.md`` §4.5 and
        ``docs/action_descriptor_vs_tool_definition.md`` both say so, and
        the blocker (dotted names are not legal tool names) is unresolved.
        Sharing ``SchemaOptimizer`` is allowed; importing the wire type is
        the merge starting by accident. Checked on the import graph rather
        than on the text, so the docstring may keep saying why.
        """
        import ast
        from pathlib import Path

        import spectragent.core.actions.params as params_module

        tree = ast.parse(Path(params_module.__file__ or "").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)

        assert not any("tools" in name for name in imported), sorted(imported)
        assert "spectragent.core.llm.schema.optimizer.SchemaOptimizer" in imported
