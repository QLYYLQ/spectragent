"""Agent registry using conscribe.

M12: AgentBase declares ``__wiring__`` for cross-registry dependencies.
Default wiring uses Mode 1 (auto-discovery) for agent_loop and llm,
allowing all registered values. Subclasses narrow via MRO merge.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M12.
"""

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

from conscribe import create_registrar
from pydantic import Field

from spectragent.agents.base import BaseAgent

AgentRegistrar = create_registrar(
    "agent",
    BaseAgent,
    discriminator_field="agent_id",
)


class AgentReceptors:
    """Annotation-only declaration of the Agent's wired receptor slots.

    :class:`~spectragent.core.composer.ConfigComposer` injects a wired
    dependency **only** when the owning class declares a slot for it —
    either an ``__init__`` parameter or a class-level annotation. That is
    what separates a real receptor (``llm``, ``memory``, ``agent_loop``)
    from a pure negotiation declaration (``observation``, ``action``,
    which live in ``__wiring__`` but are read straight off the class by
    the negotiation engine and have no runtime attribute).

    These stay on a plain (non-dataclass) mixin on purpose: turning them
    into dataclass fields would materialise conscribe's ``WiredField``
    descriptor into every instance's ``__dict__`` and destroy the
    "not injected yet" AttributeError.
    """

    agent_loop: Any
    llm: Any
    memory: Any


@dataclass
class AgentBase(AgentReceptors, metaclass=AgentRegistrar.Meta):
    """Concrete base for all agent implementations.

    Subclasses auto-register via conscribe's AutoRegistrar metaclass.
    Set ``__registry_key__ = "key"`` to specify the registration key.

    ``__wiring__`` declares cross-registry dependencies:
    - ``agent_loop``: compatible Loop strategies (default: all)
    - ``llm``: compatible LLM providers (default: all)

    Subclasses can narrow wiring; conscribe MRO merge handles inheritance.

    ``id`` is the **instance** identity (as opposed to ``agent_id``,
    which is the *class* identity). It is a dataclass field on the base
    so every dataclass subclass inherits it as an ``__init__`` parameter
    and conscribe therefore emits it into every per-key config model.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": "agent_loop",  # Mode 1: auto-discovery, allows all loops
        "llm": "llm",  # Mode 1: auto-discovery, allows all LLMs
        "memory": "memory",  # Mode 1: auto-discovery, allows all memories
    }

    id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Instance identity. Unique across the whole config; "
                "defaults to '<agent_id>#<ordinal>' when omitted."
            ),
        ),
    ] = None

    @property
    def agent_id(self) -> str:
        """Return the conscribe registry key as agent_id."""
        return self.__registry_key__

    @property
    def domain(self) -> str:
        """Domain identifier. Override in subclasses for specific domains."""
        return "general"
