"""Tests for the ``WiredField`` contract on agent classes.

After Stage 4, agent ``llm`` and ``memory`` are wired from ``__wiring__``
rather than declared as dataclass fields with ``default=None``. This
pins down the user-visible behaviour: pre-injection reads raise with a
clear message; post-injection reads return the injected value.
"""

from __future__ import annotations

import pytest
from conscribe.wiring import WiredField

from spectragent.agents.browser_use import BrowserUseDomAgent, BrowserUseDomVisionAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.agents.ui_tars import UITarsAgent

AGENT_CLASSES = [
    BrowserUseDomAgent,
    BrowserUseDomVisionAgent,
    SkyvernDomAgent,
    SkyvernDomVisionAgent,
    UITarsAgent,
]


class TestWiredFieldContract:
    @pytest.mark.parametrize("cls", AGENT_CLASSES)
    def test_llm_is_wired_field_descriptor(self, cls: type) -> None:
        assert isinstance(cls.__dict__.get("llm", None), WiredField) or isinstance(
            getattr(cls, "llm", None), WiredField
        )

    @pytest.mark.parametrize("cls", AGENT_CLASSES)
    def test_memory_is_wired_field_descriptor(self, cls: type) -> None:
        assert isinstance(cls.__dict__.get("memory", None), WiredField) or isinstance(
            getattr(cls, "memory", None), WiredField
        )

    @pytest.mark.parametrize("cls", AGENT_CLASSES)
    def test_llm_unset_raises(self, cls: type) -> None:
        agent = cls()
        with pytest.raises(AttributeError, match="llm"):
            _ = agent.llm

    @pytest.mark.parametrize("cls", AGENT_CLASSES)
    def test_memory_unset_raises(self, cls: type) -> None:
        agent = cls()
        with pytest.raises(AttributeError, match="memory"):
            _ = agent.memory

    @pytest.mark.parametrize("cls", AGENT_CLASSES)
    def test_setattr_shadows_descriptor(self, cls: type) -> None:
        agent = cls()
        mem = SimpleMemory(max_messages=5)
        agent.memory = mem
        assert agent.memory is mem

    @pytest.mark.parametrize("cls", AGENT_CLASSES)
    def test_no_default_none_field(self, cls: type) -> None:
        """Ensure the old ``llm: ... = field(default=None)`` is gone.

        If the field were still there, ``cls()`` would produce an
        instance with ``llm is None`` rather than an unset WiredField.
        """
        agent = cls()
        # The instance dict must NOT contain ``llm`` / ``memory`` keys —
        # they live on the class as WiredField descriptors.
        assert "llm" not in vars(agent)
        assert "memory" not in vars(agent)
