"""Environment registrar — conscribe-driven auto-registration.

EnvironmentBase implements prefix routing for action dispatch and
observation delegation. Concrete subclasses declare capabilities
via ``__wiring__`` and set ``__registry_key__``.

Registration is import-driven. Use ``discover("spectragent.core.environments")``
to scan and register all concrete Environment implementations.

See docs/execution_dag.md M07 for the registrar design.
"""

from __future__ import annotations

from typing import Any, ClassVar

from conscribe import create_registrar

from spectragent.core.capabilities.base import BaseCapability
from spectragent.core.environments.base import Environment
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.dynamic_model import build_observation_model

EnvironmentRegistrar = create_registrar(
    "environment",
    Environment,
    discriminator_field="env_type",
)


class EnvironmentBase(metaclass=EnvironmentRegistrar.Meta):
    """Concrete base for all environment implementations.

    Provides prefix routing for multi-capability environments:
    - Single capability: action names pass through unchanged
    - Multiple capabilities: action names are auto-prefixed
      (``browser.click``), and ``act()`` strips the prefix before
      delegating to the matching Capability.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"browser"``)
    - ``__wiring__``: capability requirements

    Example::

        class BrowserEnvironment(EnvironmentBase):
            __registry_key__ = "browser"
            __wiring__ = {
                "capability": ("capability", ["browser"]),
            }
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __wiring__: ClassVar[dict[str, Any]] = {}

    def __init__(self, capabilities: dict[str, BaseCapability]) -> None:
        self._capabilities = capabilities
        self._is_composite = len(capabilities) > 1
        self._fragment_router: dict[str, BaseCapability] | None = None

    # ── Registry property ─────────────────────────────────────

    @property
    def env_type(self) -> str:
        return self.__registry_key__

    # ── Capability discovery ──────────────────────────────────

    def capabilities(self) -> frozenset[str]:
        return frozenset(self._capabilities.keys())

    def has_capability(self, cap_id: str) -> bool:
        return cap_id in self._capabilities

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Union of all capabilities' provided_fragments."""
        fragments: set[str] = set()
        for cap in self._capabilities.values():
            fragments |= cap.provided_fragments
        return frozenset(fragments)

    @property
    def provided_actions(self) -> frozenset[str]:
        """Union of all capabilities' provided_actions.

        Symmetric to :attr:`provided_fragments`. Composite environments
        expose this as the unprefixed action names; prefix routing is
        applied separately in :meth:`get_action_space` / :meth:`act`.
        """
        actions: set[str] = set()
        for cap in self._capabilities.values():
            actions |= cap.provided_actions
        return frozenset(actions)

    # ── Action space aggregation ──────────────────────────────

    def get_action_space(self, **context: Any) -> list[ActionDescriptor]:
        """Aggregate descriptors from all capabilities.

        Multi-capability environments auto-prefix each descriptor
        with the capability ID (e.g. ``click`` → ``browser.click``).
        Single-capability environments pass names through unchanged.
        """
        descriptors: list[ActionDescriptor] = []
        for cap_id, cap in self._capabilities.items():
            for desc in cap.get_action_descriptors(**context):
                if self._is_composite:
                    descriptors.append(desc.with_prefix(cap_id))
                else:
                    descriptors.append(desc)
        return descriptors

    # ── Action execution (prefix routing) ─────────────────────

    async def act(
        self, action: Action, session_id: str | None = None,
    ) -> ActionResult:
        """Route action to the correct Capability.

        For composite environments, strips the prefix from the action
        name (``browser.click`` → ``click``) and delegates to the
        matching Capability. For single-capability environments,
        delegates directly.
        """
        if self._is_composite:
            prefix, sep, local_name = action.name.partition(".")
            if not sep:
                return ActionResult(
                    success=False,
                    error=(
                        f"Composite environment requires prefixed action names "
                        f"(e.g. 'browser.click'), got '{action.name}'"
                    ),
                )
            cap = self._capabilities.get(prefix)
            if cap is None:
                return ActionResult(
                    success=False,
                    error=(
                        f"No capability handles prefix '{prefix}'. "
                        f"Available: {sorted(self._capabilities.keys())}"
                    ),
                )
            return await cap.execute_action(
                Action(name=local_name, params=action.params),
                session_id=session_id,
            )
        else:
            cap = next(iter(self._capabilities.values()))
            return await cap.execute_action(action, session_id=session_id)

    # ── Observation delegation ────────────────────────────────

    async def observe(
        self, negotiated: Any, session_id: str | None = None,
    ) -> Any:
        """Resolve observation by delegating to Capability resolvers.

        For each env-sourced fragment in the negotiation result,
        routes to the Capability that provides it. Loop-sourced
        fragments are not filled here (the Loop handles them).

        Args:
            negotiated: NegotiationResult from three-party negotiation.
            session_id: Optional session/tab routing.

        Returns:
            A NegotiatedObservation Pydantic model instance.
        """
        if self._fragment_router is None:
            self._build_fragment_router()

        obs_model = build_observation_model(negotiated.active_fragments)
        field_values: dict[str, Any] = {}

        for frag_key in negotiated.env_sourced:
            cap = self._fragment_router.get(frag_key)
            if cap is not None:
                fragment = await cap.resolve_fragment(
                    frag_key, session_id=session_id,
                )
                field_values[frag_key] = fragment

        return obs_model(**field_values)

    def _build_fragment_router(self) -> None:
        """Build reverse index: fragment_key → Capability."""
        router: dict[str, BaseCapability] = {}
        for cap in self._capabilities.values():
            for frag_key in cap.provided_fragments:
                router[frag_key] = cap
        self._fragment_router = router

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup(self, **kwargs: Any) -> None:
        for cap in self._capabilities.values():
            await cap.setup(**kwargs)

    async def teardown(self) -> None:
        for cap in self._capabilities.values():
            await cap.teardown()

    @property
    def is_alive(self) -> bool:
        return all(cap.is_alive for cap in self._capabilities.values())
