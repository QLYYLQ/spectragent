"""AgentLoop registrar — conscribe-driven auto-registration.

Each concrete Loop subclass sets ``__requires_protocol__`` to declare
which compatibility Protocol it requires from Agents. This replaces
hardcoded mapping dicts — the validation logic reads from the Loop's
own class metadata.

See docs/execution_dag.md M03 for the registrar design.
"""

from __future__ import annotations

from typing import ClassVar

from conscribe import create_registrar

from spectragent.core.loops.base import AgentLoop

AgentLoopRegistrar = create_registrar(
    "agent_loop",
    AgentLoop,
    discriminator_field="loop_id",
)


class AgentLoopBase(metaclass=AgentLoopRegistrar.Meta):
    """Concrete base for all Loop implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"react"``, ``"codeact"``)
    - ``__requires_protocol__``: the compatibility Protocol Agents must satisfy

    Example::

        class ReactLoop(AgentLoopBase):
            __registry_key__ = "react"
            __requires_protocol__ = ReactCompatible

            async def run(self, agent, env, task, **kwargs):
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __requires_protocol__: ClassVar[type | None] = None

    @property
    def loop_id(self) -> str:
        return self.__registry_key__
