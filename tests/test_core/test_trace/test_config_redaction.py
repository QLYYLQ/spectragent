"""Unit tests for field-level config redaction.

The rules under test, restated:

* deny by default — an unmarked, open-typed field records as
  ``"<omitted>"``, never as itself and never as a missing key;
* ``null`` still means "not configured", so it survives verbatim;
* a closed-set annotation (``Literal``, ``Enum``, ``bool``) records
  verbatim without a mark, because its value set is written in the source;
* ``trace_safe`` opts an open-typed field in — *and survives conscribe*,
  which is the part that is easy to get wrong and impossible to notice.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace.redaction import (
    OMITTED,
    TraceSafe,
    is_closed_set,
    is_trace_safe,
    redact_config,
    trace_safe,
)


class _Mode(enum.StrEnum):
    FAST = "fast"
    SLOW = "slow"


class _Backend(BaseModel):
    provider_id: Literal["local", "cloud"] = "local"
    api_key: str | None = None
    cdp_url: str | None = None


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="allow")

    env_type: Literal["browser"] = "browser"
    mode: _Mode = _Mode.FAST
    headless: bool = True
    # Open types: a number and two strings.
    max_steps: int = 30
    model: str = "gpt-4o"
    marked_model: Annotated[str, trace_safe("model identity is public", default="gpt-4o")] = (
        "gpt-4o"
    )
    annotated_mark: Annotated[str, Field(default="x"), TraceSafe("hand-written model")] = "x"
    schema_extra_mark: Annotated[
        str, Field(default="y", json_schema_extra={"trace_safe": True})
    ] = "y"
    backend: _Backend = Field(default_factory=_Backend)
    tags: list[str] = Field(default_factory=list)
    children: list[_Backend] = Field(default_factory=list)


class TestClosedSetRule:
    @pytest.mark.parametrize(
        "annotation",
        [Literal["a", "b"], bool, _Mode, type(None), _Mode | None, Literal["a"] | None],
    )
    def test_closed(self, annotation):
        assert is_closed_set(annotation) is True

    @pytest.mark.parametrize(
        "annotation", [str, int, float, list[str], dict[str, str], str | None, None]
    )
    def test_open(self, annotation):
        assert is_closed_set(annotation) is False


class TestDenyByDefault:
    @pytest.fixture()
    def dumped(self) -> dict[str, Any]:
        return redact_config(
            _Cfg(
                model="o3-pro",
                backend=_Backend(
                    api_key="sk-live-AAAABBBBCCCC",
                    cdp_url="ws://box:9222/devtools/browser/TOK",
                ),
                tags=["a", "b"],
            )
        )

    def test_open_string_is_withheld(self, dumped):
        assert dumped["model"] == OMITTED

    def test_open_number_is_withheld(self, dumped):
        """A number is not a closed set. Marking it is one line; guessing
        it is safe is the door a numeric credential walks through."""
        assert dumped["max_steps"] == OMITTED

    def test_withheld_field_is_present_not_dropped(self, dumped):
        """'This field was set' is itself reproducibility information."""
        assert "model" in dumped
        assert "api_key" in dumped["backend"]

    def test_unset_stays_null_and_is_distinguishable(self):
        """``null`` = never configured; ``"<omitted>"`` = set, withheld.

        An api_key resolved from the environment never enters the config
        model, so it must read as the first, not the second.
        """
        dumped = redact_config(_Cfg())
        assert dumped["backend"]["api_key"] is None
        assert dumped["model"] == OMITTED

    def test_closed_sets_survive(self, dumped):
        assert dumped["env_type"] == "browser"
        assert dumped["mode"] == "fast"
        assert dumped["headless"] is True
        assert dumped["backend"]["provider_id"] == "local"

    def test_marked_fields_survive(self, dumped):
        assert dumped["marked_model"] == "gpt-4o"
        assert dumped["annotated_mark"] == "x"
        assert dumped["schema_extra_mark"] == "y"

    def test_nested_structure_is_kept_and_recursed(self, dumped):
        assert isinstance(dumped["backend"], dict)
        assert dumped["backend"]["cdp_url"] == OMITTED

    def test_list_of_values_is_one_withheld_value(self, dumped):
        assert dumped["tags"] == OMITTED

    def test_empty_container_records_as_itself(self, dumped):
        """ "Configured, and empty" — an ablation list naming no actions —
        is a fact worth having, and carries nothing to leak."""
        assert dumped["children"] == []

    def test_list_of_models_is_structure(self):
        dumped = redact_config(_Cfg(children=[_Backend(api_key="sk-x")]))
        assert dumped["children"] == [{"provider_id": "local", "api_key": OMITTED, "cdp_url": None}]

    def test_undeclared_extra_is_withheld(self):
        """``extra="allow"`` fields have no declaration, so no mark. The
        fail-closed direction."""
        dumped = redact_config(_Cfg.model_validate({"smuggled": "sk-live-XYZ"}))
        assert dumped["smuggled"] == OMITTED


class TestRecordSecretsEscapeHatch:
    def test_everything_verbatim(self):
        dumped = redact_config(
            _Cfg(model="o3-pro", backend=_Backend(api_key="sk-live-AAAA")),
            record_secrets=True,
        )
        assert dumped["model"] == "o3-pro"
        assert dumped["max_steps"] == 30
        assert dumped["backend"]["api_key"] == "sk-live-AAAA"


class TestNonModelConfigs:
    def test_none_is_empty(self):
        assert redact_config(None) == {}

    def test_plain_dict_has_no_declarations_to_consult(self):
        """A dict carries no field metadata, so nothing in it can be
        marked — and guessing from key names is the blocklist this
        design exists to avoid."""
        assert redact_config({"api_key": "sk-x", "mode": "fast"}) == {
            "api_key": OMITTED,
            "mode": OMITTED,
        }


class TestMarkSurvivesConscribe:
    """The mark has to reach the model the *Composer* validates against.

    conscribe rebuilds every config model from the source class, keeping
    only the attributes it recognises. A mark that lives anywhere but
    ``FieldInfo.metadata`` is dropped on the way, and the field then
    records as ``"<omitted>"`` with nothing anywhere saying why. This
    test is the tripwire for that.
    """

    def test_source_declaration_carries_the_mark(self):
        """Sanity: the mark is on the class before conscribe sees it.

        ``TraceWriter`` hand-writes ``__init__`` (it is not a dataclass), so
        the declaration conscribe reads is that signature's annotation.
        """
        from typing import get_args, get_type_hints

        annotation = get_type_hints(TraceWriter.__init__, include_extras=True)["trace_dir"]
        marks = [
            meta
            for arg in get_args(annotation)[1:]
            for meta in getattr(arg, "metadata", [])
            if isinstance(meta, TraceSafe)
        ]
        assert marks, f"TraceWriter.trace_dir lost its mark: {annotation}"

    def test_mark_reaches_the_composed_model(self, composed_top_level_type):
        model = _hook_model(composed_top_level_type, "trace_writer")
        assert is_trace_safe(model.model_fields["trace_dir"]) is True

    def test_unmarked_neighbour_is_not_safe(self, composed_top_level_type):
        model = _hook_model(composed_top_level_type, "trace_writer")
        # ``record_secrets`` is a bool, so it records anyway — via the
        # closed-set rule, not via a mark. Keeping the two apart is what
        # stops "it showed up" from being read as "it was marked".
        assert is_trace_safe(model.model_fields["record_secrets"]) is False
        assert is_closed_set(model.model_fields["record_secrets"].annotation) is True


def _hook_model(top_level_type: type[BaseModel], hook_id: str) -> type[BaseModel]:
    """The per-key config model conscribe built for one hook."""
    from typing import get_args

    annotation = top_level_type.model_fields["hook"].annotation
    for candidate in _iter_models(annotation):
        field = candidate.model_fields.get("hook_id")
        if field is not None and get_args(field.annotation) == (hook_id,):
            return candidate
    raise AssertionError(f"no per-key hook model for {hook_id!r}")


def _iter_models(annotation: Any):
    from typing import get_args

    for arg in get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            yield arg
        else:
            yield from _iter_models(arg)
