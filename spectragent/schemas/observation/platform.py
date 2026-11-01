"""The input platform: the operating system an agent's input reaches, as a capability reports it.

A capability that takes an agent's input reports where that input lands as
its ``<capability>.platform`` fragment (``browser.platform``,
``android.platform``), measured on the environment and never inferred from the
agent's host. :data:`InputPlatform` is every value such a fragment may hold.
Each capability's fragment narrows it to a closed ``Literal`` of what that
capability can actually report: the browser's ``macos``, ``windows`` and
``linux``, Android's ``android``.

An agent's prompt states the platform with the keyboard rules that hold there
(``agents/rendering/platform.py``), and the agent declares which platforms its
prompt renders (``__input_platforms__``). Both are fixed before a run starts,
so ``AgentRuntime.prepare()`` refuses a run whose platform fragment may report
one the agent cannot render (``core/negotiation/platform.py``).
"""

from typing import Literal

InputPlatform = Literal["macos", "windows", "linux", "android"]
"""Every operating system a capability's ``platform`` fragment may report."""

__all__ = ["InputPlatform"]
