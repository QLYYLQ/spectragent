"""Receptors filled only inline, and what an omitted receptor means (§4.2.1).

A receptor targeting an ``inline_only`` registrar takes its instances from
its own config block or not at all: omitted or null leaves the owner's
default, and the layer's top-level section is refused, naming the receptor.
``trace_writer.archive`` is the shipped case (archiving has one switch;
``test_archive_yaml_surface.py`` pins it on the real registrars). This
module pins the mechanism on an isolated registrar hierarchy, plus the
rule every omitted list or dict receptor follows: with nothing in the pool
to adopt, the owner's own default stands, because an empty container is
what ``[]`` says.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

import pytest
from conscribe import create_registrar
from pydantic import Field, ValidationError

from spectragent.core.composer import (
    ConfigComposer,
)


@runtime_checkable
class _ItemP(Protocol):
    item_id: str


@runtime_checkable
class _OwnerP(Protocol):
    owner_id: str


ItemReg = create_registrar("inl_item", _ItemP, discriminator_field="item_id", inline_only=True)
OwnerReg = create_registrar("inl_owner", _OwnerP, discriminator_field="owner_id")
PoolItemReg = create_registrar("pool_item", _ItemP, discriminator_field="item_id")
PoolerReg = create_registrar("inl_pooler", _OwnerP, discriminator_field="owner_id")


class _ItemBase(metaclass=ItemReg.Meta):
    __abstract__ = True
    __config_annotated_only__ = True

    @property
    def item_id(self) -> str:
        return self.__registry_key__  # type: ignore[attr-defined]


class _Red(_ItemBase):
    __registry_key__: ClassVar[str] = "red"

    def __init__(self, depth: Annotated[int, Field(description="depth")] = 1) -> None:
        self.depth = depth


class _Blue(_ItemBase):
    __registry_key__: ClassVar[str] = "blue"

    def __init__(self) -> None:
        pass


PoolItemReg.register(key="blue")(_Blue)


class _ItemReceptors:
    """Annotation-only list receptor slot (the ``_TraceWriterReceptors`` shape)."""

    items: list[_ItemBase]


class _Keeper(_ItemReceptors, metaclass=OwnerReg.Meta):
    """Fills ``items`` only inline; its own default is an empty list."""

    __registry_key__: ClassVar[str] = "keeper"
    __config_annotated_only__ = True
    __wiring__: ClassVar[dict[str, Any]] = {"items": "inl_item"}

    def __init__(
        self,
        items: Annotated[list[_ItemBase] | None, Field(description="items")] = None,
    ) -> None:
        self.items = list(items or [])

    @property
    def owner_id(self) -> str:
        return self.__registry_key__


class _Pooler(_ItemReceptors, metaclass=PoolerReg.Meta):
    """Adopts the ordinary ``pool_item`` layer from its instance pool."""

    __registry_key__: ClassVar[str] = "pooler"
    __config_annotated_only__ = True
    __wiring__: ClassVar[dict[str, Any]] = {"items": "pool_item"}

    def __init__(
        self,
        items: Annotated[list[_ItemBase] | None, Field(description="items")] = None,
    ) -> None:
        if items is not None:
            self.items = list(items)

    @property
    def owner_id(self) -> str:
        return self.__registry_key__


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(
        registrars={"inl_item": ItemReg, "inl_owner": OwnerReg}, discover_roots=()
    )


class TestAnInlineOnlyReceptor:
    @pytest.mark.parametrize("block", [{}, {"items": None}], ids=["omitted", "null"])
    def test_left_out_keeps_the_owner_default(
        self, composer: ConfigComposer, block: dict[str, Any]
    ) -> None:
        run = composer.compose({"inl_owner": [{"owner_id": "keeper", **block}]})
        assert run.single("inl_owner").items == []

    def test_left_out_never_adopts_what_another_owner_built_inline(
        self, composer: ConfigComposer
    ) -> None:
        """The layer's pool also holds instances built inside other blocks."""
        run = composer.compose(
            {
                "inl_owner": [
                    {"owner_id": "keeper", "items": [{"item_id": "red"}]},
                    {"owner_id": "keeper"},
                ]
            }
        )
        assert [len(owner.items) for owner in run.instances("inl_owner")] == [1, 0]

    def test_written_inline_builds_in_place(self, composer: ConfigComposer) -> None:
        run = composer.compose(
            {"inl_owner": [{"owner_id": "keeper", "items": [{"item_id": "red", "depth": 3}]}]}
        )
        (item,) = run.single("inl_owner").items
        assert isinstance(item, _Red)
        assert item.depth == 3

    @pytest.mark.parametrize(
        "block",
        [{}, {"items": []}, {"items": [{"item_id": "blue"}]}],
        ids=["omitted", "empty", "inline"],
    )
    def test_its_layer_top_level_section_is_refused(
        self, composer: ConfigComposer, block: dict[str, Any]
    ) -> None:
        raw = {
            "inl_item": [{"item_id": "red"}, {"item_id": "blue"}],
            "inl_owner": [{"owner_id": "keeper", **block}],
        }
        with pytest.raises(ValidationError) as refused:
            composer.compose(raw)
        error = refused.value.errors()[0]
        assert error["type"] == "inline_only_layer"
        assert error["loc"] == ("inl_item",)
        assert "inl_owner[keeper].items" in error["msg"]

    @pytest.mark.parametrize("value", [[], None])
    def test_an_empty_top_level_section_is_refused(self, composer: ConfigComposer, value) -> None:
        with pytest.raises(ValidationError, match="inline-only"):
            composer.compose({"inl_item": value, "inl_owner": [{"owner_id": "keeper"}]})


class TestAnOmittedReceptorWithNothingToAdopt:
    """For ordinary pooled layers: the pool when it has instances, else the owner's own value."""

    @pytest.fixture(scope="class")
    def pooling(self) -> ConfigComposer:
        return ConfigComposer(
            registrars={"pool_item": PoolItemReg, "inl_pooler": PoolerReg}, discover_roots=()
        )

    def test_adopts_the_top_level_section(self, pooling: ConfigComposer) -> None:
        run = pooling.compose(
            {"pool_item": [{"item_id": "blue"}], "inl_pooler": [{"owner_id": "pooler"}]}
        )
        assert [type(i) for i in run.single("inl_pooler").items] == [_Blue]

    def test_an_owner_without_a_default_gets_the_empty_container(
        self, pooling: ConfigComposer
    ) -> None:
        run = pooling.compose({"inl_pooler": [{"owner_id": "pooler"}]})
        assert run.single("inl_pooler").items == []

    def test_an_explicit_empty_list_is_kept(self, pooling: ConfigComposer) -> None:
        run = pooling.compose(
            {
                "pool_item": [{"item_id": "blue"}],
                "inl_pooler": [{"owner_id": "pooler", "items": []}],
            }
        )
        assert run.single("inl_pooler").items == []
