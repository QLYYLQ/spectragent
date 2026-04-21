"""Capability registrar — conscribe-driven auto-registration.

Each concrete Capability subclass sets ``__registry_key__`` and
``__wiring__`` to declare its identity and observation provides.

Registration is import-driven: use ``discover("spectragent.core.capabilities")``
to scan and register all concrete Capability implementations.

See docs/execution_dag.md M06 for the registrar design.
"""

from __future__ import annotations

from typing import Any, ClassVar

from conscribe import create_registrar
from conscribe.wiring import parse_wiring

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


class CapabilityBase(metaclass=CapabilityRegistrar.Meta):
    """Concrete base for all capability implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"browser"``, ``"docker"``)
    - ``__wiring__``: observation provides declaration

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

    @property
    def capability_id(self) -> str:
        return self.__registry_key__

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Derive provided fragments from ``__wiring__["observation"]``."""
        return _wiring_keys_for(type(self), "observation")

    @property
    def provided_actions(self) -> frozenset[str]:
        """Derive provided action names from the materialised dispatch map.

        If the subclass has an ``_actions_by_name`` dict (built during
        ``__post_init__``), that is the single source of truth — it
        reflects both wiring and any ``action_overrides``.  Falls back
        to parsing ``__wiring__["action"]`` for capability subclasses
        that have no action dispatch (e.g. lightweight test stubs).
        """
        dispatch: dict | None = getattr(self, "_actions_by_name", None)
        if dispatch is not None:
            return frozenset(dispatch.keys())
        return _wiring_keys_for(type(self), "action")

    # ── Default no-op lifecycle ───────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list:
        return []

    async def execute_action(self, action: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{type(self).__name__} must implement execute_action()")

    async def resolve_fragment(self, fragment_key: str, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{type(self).__name__} cannot resolve fragment '{fragment_key}'")

    async def setup(self, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        pass

    async def teardown(self) -> None:
        pass

    @property
    def is_alive(self) -> bool:
        return True
