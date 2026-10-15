"""Presence belongs to the supplied block, independently of default factories."""

from pydantic import BaseModel, Field

from spectragent.core.composition_evidence import configured_inputs


def test_an_omitted_default_block_does_not_turn_its_factory_arguments_into_input():
    class Child(BaseModel):
        count: int = 7
        optional: int | None = None
        items: list[int] = Field(default_factory=list)

    class Parent(BaseModel):
        child: Child = Field(default_factory=lambda: Child(count=7))

    omitted = {item.path: item.source for item in configured_inputs(Parent())}
    explicit = {
        item.path: item.source
        for item in configured_inputs(Parent(child=Child(count=7, optional=None, items=[])))
    }
    assert omitted["child.count"] == "default"
    assert explicit["child.count"] == "explicit"
    assert explicit["child.optional"] == "explicit"
    assert explicit["child.items"] == "explicit"
    assert Parent().child.count == Parent(child=Child(count=7)).child.count
