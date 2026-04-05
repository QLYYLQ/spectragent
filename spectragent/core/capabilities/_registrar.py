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

from spectragent.core.capabilities.base import BaseCapability

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
        """Derive provided fragments from ``__wiring__["observation"]``.

        Supports conscribe wiring modes:
        - Mode 2: ``("observation", ["dom", "screenshot"])``
        - Mode 3: ``("observation", ["dom"], ["screenshot"])``

        Both required and optional keys are included in the provides set.
        """
        obs_wiring = self.__wiring__.get("observation")
        if obs_wiring is None:
            return frozenset()
        if isinstance(obs_wiring, str):
            return frozenset()
        # Mode 2: (registry, [keys]) or Mode 3: (registry, [required], [optional])
        if isinstance(obs_wiring, tuple):
            if len(obs_wiring) == 2:
                _, keys = obs_wiring
                return frozenset(keys)
            elif len(obs_wiring) == 3:
                _, required, optional = obs_wiring
                return frozenset(required) | frozenset(optional)
        return frozenset()

    # ── Default no-op lifecycle ───────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list:
        return []

    async def execute_action(self, action: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__} must implement execute_action()"
        )

    async def resolve_fragment(self, fragment_key: str, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__} cannot resolve fragment '{fragment_key}'"
        )

    async def setup(self, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        pass

    async def teardown(self) -> None:
        pass

    @property
    def is_alive(self) -> bool:
        return True
