"""Capability registrar — conscribe-driven auto-registration.

Each concrete Capability subclass sets ``__registry_key__`` and
``__wiring__`` to declare its identity and observation provides.

Registration is import-driven: use ``discover("spectragent.core.capabilities")``
to scan and register all concrete Capability implementations.

See docs/execution_dag.md M06 for the registrar design.

.. note::
   This module deliberately does **not** use ``from __future__ import
   annotations``. ``CapabilityBase.id`` must stay a real
   ``Annotated[...]`` object so the dataclass-generated ``__init__`` of
   subclasses (whose ``__globals__`` belong to *their* module) carries a
   resolvable annotation for conscribe's config extractor.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from conscribe import create_registrar
from conscribe.wiring import parse_wiring
from pydantic import Field

from spectragent.core.archive._registrar import ArchiverBase
from spectragent.core.archive.types import ArchiveBatch
from spectragent.core.capabilities.base import BaseCapability


def _wiring_keys_for(cls: type, registry_name: str) -> frozenset[str]:
    """Collect ``required ∪ optional`` keys from ``cls.__wiring__`` for a registry.

    Uses ``conscribe.parse_wiring`` so MRO-merged wiring, all three grammar
    modes (Mode 1 bare string, Mode 2 2-tuple, Mode 3 3-tuple), and
    validation errors are handled centrally.
    """
    for spec in parse_wiring(cls):
        if spec.registry_name != registry_name:
            continue
        required = frozenset(spec.allowed_keys) if spec.allowed_keys else frozenset()
        optional = frozenset(spec.optional_keys) if spec.optional_keys else frozenset()
        return required | optional
    return frozenset()


CapabilityRegistrar = create_registrar(
    "capability",
    BaseCapability,
    discriminator_field="capability_id",
)


@dataclass
class CapabilityBase(metaclass=CapabilityRegistrar.Meta):
    """Concrete base for all capability implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"browser"``, ``"docker"``)
    - ``__wiring__``: observation provides declaration (and, for a
      capability that hands over environment evidence, the
      ``"archiver"`` provides declaration — see :attr:`provided_archivers`)

    The base satisfies
    :class:`~spectragent.core.archive.protocols.ArchivingCapability` with
    inert defaults, so the environment can treat every capability alike;
    one that declares archiver provides overrides :meth:`bind_archivers`
    and :meth:`collect_archive`.

    Example::

        class BrowserCapability(CapabilityBase):
            __registry_key__ = "browser"
            __wiring__ = {
                "observation": ("observation", ["dom", "screenshot", "browser_meta"]),
            }

            def get_action_descriptors(self, **context):
                return [...]

            async def execute_action(self, action, **kwargs):
                ...

            async def resolve_fragment(self, fragment_key, **kwargs):
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __wiring__: ClassVar[dict[str, Any]] = {}

    #: Whether an action run by *another* capability invalidates this
    #: one's current capture. See
    #: :attr:`BaseCapability.__invalidated_by_foreign_actions__` for the
    #: policy; ``False`` says "we are independent worlds", which is what
    #: keeps ``browser.click`` from costing an Android capability an ADB
    #: round trip. Declaring it here — even as the default — is what turns
    #: an assumption nobody wrote down into something a test can read.
    __invalidated_by_foreign_actions__: ClassVar[bool] = False

    #: Instance identity — distinct from ``capability_id`` (class identity).
    #: A dataclass field on the base so every dataclass subclass inherits
    #: it as an ``__init__`` parameter and conscribe emits it into the
    #: per-key config model.
    id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Instance identity. Unique across the whole config; "
                "defaults to '<capability_id>#<ordinal>' when omitted."
            ),
        ),
    ] = None

    #: Fragments the three-party negotiation actually activated for this
    #: run, or ``None`` before ``setup()``. Set through
    #: :meth:`apply_negotiation`; narrows :attr:`provided_fragments` so a
    #: capability only pays for what the agent and loop asked for.
    _negotiated_fragments: frozenset[str] | None = field(default=None, init=False, repr=False)

    @property
    def capability_id(self) -> str:
        return self.__registry_key__

    @property
    def declared_fragments(self) -> frozenset[str]:
        """Fragments this capability class can resolve, ignoring negotiation.

        Default derivation is ``__wiring__["observation"]``. Subclasses
        whose resolver set is dynamic (the Browser Capability derives it
        from its registered watchdogs) override this.
        """
        return _wiring_keys_for(type(self), "observation")

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Fragments this capability will resolve for *this* run.

        Before negotiation this is :attr:`declared_fragments` — that is
        what the negotiation engine consumes. After
        :meth:`apply_negotiation` it narrows to the negotiated active
        set, which is what drives per-step resolver cost.
        """
        declared = self.declared_fragments
        if self._negotiated_fragments is None:
            return declared
        return declared & self._negotiated_fragments

    def apply_negotiation(self, negotiation: Any) -> None:
        """Record the observation negotiation result for this run.

        Accepts an ``ObservationNegotiationResult`` (duck-typed on
        ``env_sourced`` / ``active_fragments``) or a bare iterable of
        fragment keys. ``None`` clears the narrowing.

        Kept structural rather than importing the negotiation package so
        the capability layer stays free of a dependency on it.
        """
        if negotiation is None:
            self._negotiated_fragments = None
            return
        keys = getattr(negotiation, "env_sourced", None)
        if keys is None:
            keys = getattr(negotiation, "active_fragments", negotiation)
        self._negotiated_fragments = frozenset(keys)

    @property
    def provided_actions(self) -> frozenset[str]:
        """Derive provided action names from the materialised dispatch map.

        If the subclass has an ``_actions_by_name`` dict (built at
        construction from the config's ``actions:`` list, or from the
        wiring when that list is omitted), that is the single source of
        truth — it is what an ablated config actually narrows. Falls
        back to the ``"action"`` registry entry in ``__wiring__`` for
        capability subclasses that have no action dispatch (e.g.
        lightweight test stubs).
        """
        dispatch: dict | None = getattr(self, "_actions_by_name", None)
        if dispatch is not None:
            return frozenset(dispatch.keys())
        return _wiring_keys_for(type(self), "action")

    # ── Archive axis (the optional ArchivingCapability surface) ─

    @property
    def provided_archivers(self) -> frozenset[str]:
        """Archiver keys this capability class can provide.

        Derived from ``__wiring__["archiver"]`` exactly as
        :attr:`declared_fragments` is derived from
        ``__wiring__["observation"]`` — the same declaration, on the third
        axis. Empty for a capability that declares none, which is every
        capability that records no environment evidence.
        """
        return _wiring_keys_for(type(self), "archiver")

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        """Inert default: an empty binding is fine, a real one is refused.

        Unbinding (``[]``) is what ``Environment.bind_archivers`` sends
        every capability that gets no archiver, so it must cost nothing
        here. A non-empty binding reaching this default means a capability
        declared ``__wiring__["archiver"]`` provides without implementing
        the archiving surface — refused loudly, naming the class, rather
        than accepted and then collected as nothing.
        """
        if not archivers:
            return
        keys = sorted(str(getattr(a, "archiver_id", a)) for a in archivers)
        # Through the conscribe metaclass ``__name__`` is invisible to the
        # type checker; the value is always there.
        owner = getattr(type(self), "__name__", "capability")
        raise NotImplementedError(
            f"{owner} cannot bind archivers {keys}: it does not implement "
            f"the archiving surface. A capability that declares __wiring__['archiver'] "
            f"provides must override both bind_archivers() and collect_archive() "
            f"(docs/architecture_design.md §4.9)."
        )

    async def collect_archive(self) -> ArchiveBatch:
        """Inert default: nothing bound, nothing captured — an empty batch."""
        return ArchiveBatch()

    # ── Default no-op lifecycle ───────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list:
        return []

    async def execute_action(self, action: Any) -> Any:
        raise NotImplementedError(f"{type(self).__name__} must implement execute_action()")

    async def begin_observation(self, cycle: int) -> None:
        """Open an observation cycle. No-op unless the capability caches.

        The default is doing nothing, because a capability that resolves
        every fragment with its own round trip holds no cycle state.
        Override it to drop the previous capture — and key the new one on
        ``cycle``, rather than inferring the boundary from which fragments
        you have already served. See
        :meth:`~spectragent.core.capabilities.base.BaseCapability.begin_observation`.

        Overriding it means "my fragments share a capture", and that
        carries a second obligation on the failure path: record that
        **this cycle's capture failed** and refuse the rest of the cycle
        with the same error. ``Environment.observe`` isolates failures per
        *fragment*, so after your first fragment raises it will ask you
        for the next one; a capability that re-enters its capture there
        pays for the whole thing — retries included — once per negotiated
        fragment, on exactly the step that was already going badly.
        ``BrowserCapability`` keeps it on ``_ObserveCycleCache.error``;
        ``assert_a_failed_cycle_costs_one_capture`` in
        ``tests/_helpers/capability_contract.py`` is the clause that
        checks it.
        """
        return None

    async def resolve_fragment(self, fragment_key: str) -> Any:
        raise NotImplementedError(f"{type(self).__name__} cannot resolve fragment '{fragment_key}'")

    async def setup(self, *, negotiation: Any = None) -> None:
        """Record the negotiation result. Subclasses extend the signature.

        Every extra keyword-only parameter a subclass declares becomes a
        task-context key :meth:`EnvironmentBase.setup` is allowed to route
        here. Do not add ``**kwargs``: that turns the signature back into
        a sink that accepts — and drops — anything.
        """
        self.apply_negotiation(negotiation)

    async def teardown(self) -> None:
        pass

    @property
    def is_alive(self) -> bool:
        return True
