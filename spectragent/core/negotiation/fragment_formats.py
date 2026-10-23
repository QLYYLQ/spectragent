"""Which fragment format descriptions an agent's prompt quotes, resolved for one run.

Whoever writes a fragment's text describes its format (the browser's DOM
serializer for ``browser.dom``; ``core/capabilities/base.py::FragmentFormats``).
An agent that quotes such a description in its prompt declares which
fragments it quotes, by the names its ``__wiring__`` uses::

    __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"dom"})

and receives them through its existing ``prepare(..., fragment_formats=...)``
seed slot — the same explicit-consumption shape as
``__execution_limit_fields__`` / ``execution_limits`` (``core/loops/limits.py``):
an agent that declares nothing keeps its signature and receives nothing.

A description is a per-run fact: it depends on configuration (which
serializer, its knobs), never on a step, so it travels once, at ``prepare()``,
and is not repeated on every observation. See ``docs/architecture_design.md``
§4.4 "观测格式由提供方描述".
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from spectragent.core.negotiation._base import extract_wiring

#: The ``prepare`` keyword an agent that declares formats receives them by.
FRAGMENT_FORMATS_PARAMETER = "fragment_formats"


class FragmentFormatError(Exception):
    """An agent's declared fragment formats cannot be supplied as declared.

    Raised by ``AgentRuntime.prepare()``, before setup and before any model
    call: a prompt that quotes a format nobody describes would otherwise go
    out without it, which is the silent drift this contract exists to end.
    """

    def __init__(self, *, agent_name: str, detail: str) -> None:
        self.agent_name = agent_name
        self.detail = detail
        super().__init__(f"Agent {agent_name!r} fragment formats: {detail}")


def quoted_fragment_formats(
    agent: Any, negotiated: Any, env: Any | None
) -> Mapping[str, str] | None:
    """The descriptions *agent*'s prompt quotes this run, or ``None`` when it declares none.

    Keyed by the declared names. A declared fragment negotiation did not
    activate this run (an optional one the environment lacks, or one the
    instance excluded) has no entry: it never reaches the agent, so neither
    does its format. An active one must be described by whoever provides it.

    Args:
        agent: The configured agent; its class carries ``__fragment_formats__``.
        negotiated: The run's ``ObservationNegotiationResult``.
        env: The run's environment, or ``None`` for an env-less run.

    Raises:
        FragmentFormatError: The declaration is not a set of names its
            observation ``__wiring__`` declares; ``prepare`` does not accept
            ``fragment_formats`` by keyword; or an active declared fragment's
            provider describes no format for it.
    """
    agent_name = type(agent).__name__
    declared = getattr(type(agent), "__fragment_formats__", None)
    if declared is None:
        return None

    def refuse(detail: str) -> FragmentFormatError:
        return FragmentFormatError(agent_name=agent_name, detail=detail)

    if not isinstance(declared, (set, frozenset)) or any(
        not isinstance(name, str) for name in declared
    ):
        raise refuse("__fragment_formats__ must be a set of observation fragment names")
    required, optional = extract_wiring(type(agent), "observation")
    undeclared = declared - required - optional
    if undeclared:
        raise refuse(
            f"__fragment_formats__ names {sorted(undeclared)}, which its observation "
            f"__wiring__ does not declare"
        )
    prepare = getattr(agent, "prepare", None)
    parameter = (
        inspect.signature(prepare).parameters.get(FRAGMENT_FORMATS_PARAMETER)
        if callable(prepare)
        else None
    )
    if parameter is None or parameter.kind not in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ):
        raise refuse(
            f"declaring __fragment_formats__ requires a prepare method that explicitly "
            f"accepts {FRAGMENT_FORMATS_PARAMETER} by keyword"
        )

    bindings: Mapping[str, str] = negotiated.fragment_bindings
    active: frozenset[str] = frozenset(negotiated.active_fragments)
    quoted = {
        name: key
        for name in sorted(declared)
        if (key := bindings.get(name) or (name if name in active else None)) is not None
    }
    # Asked only when something is quoted: a run whose quoted fragments are
    # all inactive makes no call on the environment.
    described: Mapping[str, str] = env.fragment_formats() if quoted and env is not None else {}
    formats: dict[str, str] = {}
    for name, key in quoted.items():
        text = described.get(key)
        if not text:
            provider = key.partition(".")[0]
            raise refuse(
                f"its prompt quotes the format of {name!r}, but {key!r} comes from "
                f"{provider!r}, which describes no format for it"
            )
        formats[name] = text
    return MappingProxyType(formats)


__all__ = ["FRAGMENT_FORMATS_PARAMETER", "FragmentFormatError", "quoted_fragment_formats"]
