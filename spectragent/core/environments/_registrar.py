"""Environment registrar — conscribe-driven auto-registration.

EnvironmentBase implements prefix routing for action dispatch and
observation delegation. Concrete subclasses declare capabilities
via ``__wiring__`` and set ``__registry_key__``.

Registration is import-driven. Use ``discover("spectragent.core.environments")``
to scan and register all concrete Environment implementations.

See docs/execution_dag.md M07 for the registrar design.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar

from conscribe import create_registrar
from pydantic import Field

from spectragent.core.capabilities.base import BaseCapability
from spectragent.core.environments.base import Environment
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.observation.dynamic_model import build_observation_model

logger = logging.getLogger(__name__)

EnvironmentRegistrar = create_registrar(
    "environment",
    Environment,
    discriminator_field="env_type",
)

#: Framework-owned ``setup()`` parameters — never task-context keys.
_RESERVED_SETUP_PARAMS = frozenset({"self", "negotiation"})


def _setup_keys(capability: BaseCapability) -> frozenset[str]:
    """Task-context keys a capability's ``setup()`` declares by name.

    A ``**kwargs`` catch-all contributes nothing: it names no option, so
    it claims none. That is the whole point — the pre-F8 base and browser
    capabilities both ended in ``**kwargs``, which is why a typo'd key
    was accepted by every capability and acted on by none.
    """
    try:
        sig = inspect.signature(capability.setup)
    except (TypeError, ValueError):  # pragma: no cover — exotic callables
        return frozenset()
    return frozenset(
        name
        for name, param in sig.parameters.items()
        if name not in _RESERVED_SETUP_PARAMS
        and param.kind
        in (param.KEYWORD_ONLY, param.POSITIONAL_OR_KEYWORD)
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
    # instance. Keys are capability **instance ids** (``browser#0``); the
    # values must have *distinct* ``capability_id``s, because every routing
    # key the environment exposes — action prefixes (``browser.click``) and
    # observation fragment keys (``browser.dom``) — is scoped to the
    # capability *class*, not the instance. See :meth:`_by_capability_id`.
    capabilities: dict[str, BaseCapability]

    def __init__(
        self,
        id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Instance identity. Unique across the whole config; "
                    "defaults to '<env_type>#<ordinal>' when omitted."
                ),
            ),
        ] = None,
        capabilities: Annotated[
            dict[str, BaseCapability] | None,
            Field(
                default=None,
                description=(
                    "Capabilities this environment owns. Omit to adopt every "
                    "compatible capability the rest of the config builds; "
                    "declare inline to bind them explicitly (required once a "
                    "run holds more than one environment)."
                ),
            ),
        ] = None,
    ) -> None:
        self.id = id
        self._fragment_router: dict[str, BaseCapability] | None = None
        self._capability_router: dict[str, BaseCapability] | None = None
        if capabilities is not None:
            self.capabilities = capabilities
            # Reject a colliding capability set at construction rather
            # than at the first ``act()``, which is several minutes and
            # one launched browser too late.
            self._by_capability_id()

    # ── Registry property ─────────────────────────────────────

    @property
    def env_type(self) -> str:
        return self.__registry_key__

    # ── Capability discovery ──────────────────────────────────

    def _by_capability_id(self) -> dict[str, BaseCapability]:
        """Reverse index ``capability_id -> capability`` for prefix routing.

        ``self.capabilities`` is keyed by *instance* id; action names are
        fully qualified with the *class* key (``browser.click``), so
        routing needs this projection. Built lazily and cached.

        Two instances of the same capability class are rejected here.
        The projection used to be a last-wins comprehension, which
        composed and ran without a word while sending every action to
        the last instance, resolving fragments from whichever instance
        happened to win each key, and advertising every action to the
        model twice — with the losing instance's Chrome fully launched,
        held for the whole run and unreachable. Nothing in the routing
        vocabulary can tell two ``browser`` instances apart, so the
        honest answer is to refuse the config.

        Raises:
            ValueError: Two capability instances share a ``capability_id``.
        """
        if self._capability_router is None:
            by_cap_id: dict[str, list[str]] = {}
            for inst_id, cap in self.capabilities.items():
                by_cap_id.setdefault(cap.capability_id, []).append(inst_id)
            collisions = {k: v for k, v in by_cap_id.items() if len(v) > 1}
            if collisions:
                where = getattr(type(self), "__name__", "Environment")
                detail = "; ".join(
                    f"{cap_id!r} claimed by {sorted(inst_ids)}"
                    for cap_id, inst_ids in sorted(collisions.items())
                )
                raise ValueError(
                    f"{where}: capability instances collide on capability_id "
                    f"({detail}). Action names ('browser.click') and fragment "
                    f"keys ('browser.dom') are scoped to the capability class, "
                    f"so two instances of one class cannot be addressed apart "
                    f"— the extra instance would be started and then never "
                    f"reached. Use one instance per capability class, or give "
                    f"each environment its own."
                )
            self._capability_router = {
                cap.capability_id: cap for cap in self.capabilities.values()
            }
        return self._capability_router

    def capability_ids(self) -> frozenset[str]:
        return frozenset(self._by_capability_id())

    def has_capability(self, cap_id: str) -> bool:
        return cap_id in self._by_capability_id()

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

    async def act(self, action: Action) -> ActionResult:
        """Route a fully-qualified action to the owning Capability.

        The capability is identified by the leading segment of
        ``action.name`` (``browser.click`` → ``browser``). The action
        itself is passed through unchanged — capabilities look it up by
        its full registry key.

        Nothing else is passed. This used to forward a session/tab handle
        to whichever capability matched, which meant a browser tab id
        could be handed to a tool sandbox and a run-scoped id could be
        handed to the browser (where it fails every action as an unknown
        session). Sub-context is capability state.
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
        cap = self._by_capability_id().get(prefix)
        if cap is None:
            return ActionResult(
                success=False,
                error=(
                    f"No capability handles prefix '{prefix}'. "
                    f"Available: {sorted(self.capability_ids())}"
                ),
            )
        return await cap.execute_action(action)

    # ── Observation delegation ────────────────────────────────

    async def observe(self, negotiated: Any) -> Any:
        """Resolve observation by delegating to Capability resolvers.

        For each env-sourced fragment in the negotiation result,
        routes to the Capability that provides it. Loop-sourced
        fragments are not filled here (the Loop handles them).

        Args:
            negotiated: NegotiationResult from three-party negotiation.

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
                fragment = await cap.resolve_fragment(frag_key)
                # Dynamic-model attribute names use the local segment of
                # the fully-qualified key (``browser.dom`` → ``dom``), so
                # agents keep their ``obs.dom`` access pattern.
                field_values[fragment_field_name(frag_key)] = fragment

        return obs_model(**field_values)

    def _build_fragment_router(self) -> None:
        """Build reverse index: fragment_key → Capability."""
        router: dict[str, BaseCapability] = {}
        for cap in self.capabilities.values():
            for frag_key in cap.provided_fragments:
                router[frag_key] = cap
        self._fragment_router = router

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        negotiation: Any = None,
    ) -> None:
        """Set up every capability with *its own* slice of the task context.

        ``context`` is normally ``TaskLoader.get_task_context(task_id)``.
        It used to be splatted verbatim into every capability, which meant
        two capabilities could not be configured independently and a
        misspelled key was dropped by all of them without a word. See
        :meth:`route_setup_context` for the routing rules.
        """
        # Before anything is acquired: a capability set that cannot be
        # routed is a config error, not a runtime surprise.
        self._by_capability_id()
        routed = self.route_setup_context(context or {})
        for inst_id, cap in self.capabilities.items():
            await cap.setup(negotiation=negotiation, **routed[inst_id])

    def route_setup_context(
        self, context: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Split a task context into one keyword mapping per capability.

        Two forms, which compose:

        - **Flat** (``{"start_url": ...}``) — what ``TaskSetup.to_context()``
          emits. Each key goes to every capability whose ``setup()``
          *names* it, and to no others. A key no capability names raises.
        - **Namespaced** (``{"browser": {"timeout": 5}, "docker":
          {"timeout": 600}}``) — a mapping under a capability instance id
          or ``capability_id`` is private to that capability. This is how
          two capabilities take the same option name independently.

        Accepted keys are read off each capability's ``setup()``
        signature, so the contract cannot drift from the implementation.
        A ``**kwargs`` catch-all declares nothing and therefore receives
        nothing — being a sink is no longer a way to opt out of the
        check.

        Raises:
            ValueError: A key nobody accepts, or a namespaced block
                naming a key its target capability does not accept.
        """
        # ``type(self).__name__`` through the conscribe metaclass is not
        # visible to the type checker; the value is always there.
        where = getattr(type(self), "__name__", "Environment")
        accepted = {
            inst_id: _setup_keys(cap) for inst_id, cap in self.capabilities.items()
        }
        routed: dict[str, dict[str, Any]] = {
            inst_id: {} for inst_id in self.capabilities
        }
        by_cap_id: dict[str, list[str]] = {}
        for inst_id, cap in self.capabilities.items():
            by_cap_id.setdefault(cap.capability_id, []).append(inst_id)

        unclaimed: list[str] = []
        for key, value in context.items():
            targets = (
                [key]
                if key in self.capabilities
                else by_cap_id.get(key)
            )
            if targets is not None and isinstance(value, Mapping):
                for inst_id in targets:
                    unknown = sorted(set(value) - accepted[inst_id])
                    if unknown:
                        raise ValueError(
                            f"{where}: capability "
                            f"{inst_id!r} does not accept setup key(s) "
                            f"{unknown} — its setup() declares "
                            f"{sorted(accepted[inst_id])}."
                        )
                    routed[inst_id].update(value)
                continue

            claimed = [i for i, keys in accepted.items() if key in keys]
            if not claimed:
                unclaimed.append(key)
                continue
            for inst_id in claimed:
                routed[inst_id][key] = value

        if unclaimed:
            known = sorted({k for keys in accepted.values() for k in keys})
            raise ValueError(
                f"{where}: setup key(s) "
                f"{sorted(unclaimed)} are accepted by none of its "
                f"capabilities {sorted(self.capabilities)}. Keys their "
                f"setup() methods declare: {known}. Nest a mapping under a "
                f"capability id to target one capability explicitly."
            )
        return routed

    async def teardown(self) -> None:
        """Tear every capability down, even if one of them raises.

        Two guarantees, both learned from leaked Chrome processes:

        - Safe after a setup that failed part way, and after no setup at
          all. ``AgentRuntime`` calls this whenever it *attempted* setup,
          because the capabilities that came up before the failure are
          exactly the ones holding OS processes and paid cloud sessions.
        - One capability's teardown blowing up must not strand the
          capabilities after it in the loop. The first failure is
          re-raised once every capability has had its turn; the rest are
          logged.
        """
        errors: list[Exception] = []
        for inst_id, cap in self.capabilities.items():
            try:
                await cap.teardown()
            except Exception as exc:  # noqa: PERF203 — per-capability isolation
                logger.exception(
                    "Environment teardown: capability %r failed to tear down",
                    inst_id,
                )
                errors.append(exc)
        if errors:
            raise errors[0]

    @property
    def is_alive(self) -> bool:
        return all(cap.is_alive for cap in self.capabilities.values())
