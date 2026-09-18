"""Agent registry using conscribe.

M12: AgentBase declares ``__wiring__`` for cross-registry dependencies.
Default wiring uses Mode 1 (auto-discovery) for agent_loop and llm,
allowing all registered values. Subclasses narrow via MRO merge.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M12.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, ClassVar

from conscribe import create_registrar
from pydantic import Field

from spectragent.agents.base import BaseAgent
from spectragent.agents.prompts import (
    PromptEngine,
    agent_prompt_dir,
    load_prompt,
    render_template,
)
from spectragent.core.negotiation._base import extract_wiring
from spectragent.prompts import template_snapshot
from spectragent.schemas.prompt import PromptRef, TemplatePrompt

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

    __prompt_engine__: ClassVar[PromptEngine] = "format"
    """How this agent's templates substitute — declared, never sniffed.

    Sniffing the engine from a template's contents would make the meaning
    of a file depend on whether it happens to contain a ``{%``: a format
    template that grows a literal Jinja-looking token would silently
    switch engines and start dropping every ``{placeholder}`` it has.
    """

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

    _prompt_cache: dict[str, tuple[str, PromptRef]] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def agent_id(self) -> str:
        """Return the conscribe registry key as agent_id."""
        return self.__registry_key__

    @property
    def domain(self) -> str:
        """Domain identifier. Override in subclasses for specific domains."""
        return "general"

    # ── Negotiation ─────────────────────────────────────────────────

    def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
        """``(required, optional)`` this *instance* takes into negotiation.

        The class ``__wiring__`` is the declared space; a configured
        instance may narrow inside it — promote an optional key to
        required, or exclude one it will not use — which is how one agent
        class covers several modes without a second registry key or a
        second prompt tree (``docs/design_discussion.md`` §2.5 Q3).

        The default narrows nothing. An override must route through
        :func:`~spectragent.core.negotiation._base.bounded_demand`, which
        is what refuses a demand that leaves the declared space: conscribe
        compiled that space into the generated config's ``Literal``, so a
        key outside it names something no YAML could have configured.
        """
        return extract_wiring(type(self), registry)

    # ── Prompt access ───────────────────────────────────────────────
    # Keyed off ``__registry_key__``, exactly as ``EvaluatorBase`` does.
    # No agent holds a path constant, so no agent can name a directory
    # the registry does not know about.

    @property
    def prompt_dir(self) -> Path:
        """This agent's prompt directory — derived, never mapped."""
        return agent_prompt_dir(self.__registry_key__)

    def load_prompt(self, name: str) -> tuple[str, PromptRef]:
        return load_prompt(self.__registry_key__, name)

    def prompt(self, name: str) -> tuple[str, PromptRef]:
        """Load-and-memoise one prompt for this agent instance."""
        cached = self._prompt_cache.get(name)
        if cached is None:
            cached = self.load_prompt(name)
            self._prompt_cache[name] = cached
        return cached

    def render(self, name: str, params: dict[str, Any]) -> tuple[str, TemplatePrompt]:
        """Render a template and snapshot exactly what was substituted.

        The snapshot carries the template *and* the params rather than
        just the rendered string, so "which action set did this run
        advertise?" is a field read on the trace instead of a regex over
        prose.
        """
        template, ref = self.prompt(name)
        text = render_template(self.__prompt_engine__, template, params)
        return text, template_snapshot(template, ref, params)
