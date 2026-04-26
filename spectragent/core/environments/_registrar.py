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

    Action and observation keys are fully qualified at the registry level
    (``source.action_id`` / ``source.fragment_id``) so the environment
    doesn't apply any runtime prefixing. ``act()`` still routes by
    splitting the leading segment of the action name to pick the owning
    capability, but the action itself is passed through unchanged.

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

    # dict-form annotation tells the Composer this wired receptor is multi-
    # instance — each capability in ``__wiring__["capabilities"]`` becomes
    # an entry keyed by its ``capability_id``.
    capabilities: dict[str, BaseCapability]

    def __init__(self) -> None:
        self._fragment_router: dict[str, BaseCapability] | None = None

    # ── Registry property ─────────────────────────────────────

    @property
    def env_type(self) -> str:
        return self.__registry_key__

    # ── Capability discovery ──────────────────────────────────

    def capability_ids(self) -> frozenset[str]:
        return frozenset(self.capabilities.keys())

    def has_capability(self, cap_id: str) -> bool:
        return cap_id in self.capabilities

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Union of all capabilities' provided_fragments."""
        fragments: set[str] = set()
        for cap in self.capabilities.values():
            fragments |= cap.provided_fragments
        return frozenset(fragments)

    @property
    def provided_actions(self) -> frozenset[str]:
        """Union of all capabilities' provided_actions.

        Keys are fully qualified (``source.action_id``) and symmetric
        to :attr:`provided_fragments`.
        """
        actions: set[str] = set()
        for cap in self.capabilities.values():
            actions |= cap.provided_actions
        return frozenset(actions)

    # ── Action space aggregation ──────────────────────────────

    def get_action_space(
        self,
        *,
        active_actions: frozenset[str] | None = None,
        **context: Any,
    ) -> list[ActionDescriptor]:
        """Aggregate descriptors from all capabilities, narrowed by the
        negotiation result.

        Descriptors carry the fully-qualified action name from the
        registry (``source.action_id``); no runtime re-prefixing.

        Args:
            active_actions: Optional set of fully-qualified action names
                produced by :func:`negotiate_action`. When provided,
                descriptors whose name is not in the set are filtered
                out. ``None`` disables narrowing.
            **context: Forwarded to each capability's
                ``get_action_descriptors`` call.
        """
        descriptors: list[ActionDescriptor] = []
        for cap in self.capabilities.values():
            for desc in cap.get_action_descriptors(**context):
                if active_actions is not None and desc.name not in active_actions:
                    continue
                descriptors.append(desc)
        return descriptors

    # ── Action execution (prefix routing) ─────────────────────

    async def act(
        self,
        action: Action,
        session_id: str | None = None,
    ) -> ActionResult:
        """Route a fully-qualified action to the owning Capability.

        The capability is identified by the leading segment of
        ``action.name`` (``browser.click`` → ``browser``). The action
        itself is passed through unchanged — capabilities look it up by
        its full registry key.
        """
        prefix, sep, _rest = action.name.partition(".")
        if not sep:
            return ActionResult(
                success=False,
                error=(
                    f"Action name must be fully qualified "
                    f"('source.action_id'), got '{action.name}'"
                ),
            )
        cap = self.capabilities.get(prefix)
        if cap is None:
            return ActionResult(
                success=False,
                error=(
                    f"No capability handles prefix '{prefix}'. "
                    f"Available: {sorted(self.capabilities.keys())}"
                ),
            )
        return await cap.execute_action(action, session_id=session_id)

    # ── Observation delegation ────────────────────────────────

    async def observe(
        self,
        negotiated: Any,
        session_id: str | None = None,
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
                    frag_key,
                    session_id=session_id,
                )
                # Dynamic-model attribute names use the local segment of
                # the fully-qualified key (``browser.dom`` → ``dom``), so
                # agents keep their ``obs.dom`` access pattern.
                field_values[frag_key.rsplit(".", 1)[-1]] = fragment

        return obs_model(**field_values)

    def _build_fragment_router(self) -> None:
        """Build reverse index: fragment_key → Capability."""
        router: dict[str, BaseCapability] = {}
        for cap in self.capabilities.values():
            for frag_key in cap.provided_fragments:
                router[frag_key] = cap
        self._fragment_router = router

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup(self, **kwargs: Any) -> None:
        for cap in self.capabilities.values():
            await cap.setup(**kwargs)

    async def teardown(self) -> None:
        for cap in self.capabilities.values():
            await cap.teardown()

    @property
    def is_alive(self) -> bool:
        return all(cap.is_alive for cap in self.capabilities.values())
