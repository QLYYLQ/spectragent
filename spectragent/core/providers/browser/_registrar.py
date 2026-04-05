"""Browser provider registry using conscribe."""

from typing import ClassVar

from conscribe import create_registrar

from spectragent.core.providers.browser.base import BaseBrowserProvider

BrowserProviderRegistrar = create_registrar(
    "browser_provider",
    BaseBrowserProvider,
    discriminator_field="provider_id",
)


class BrowserProviderBase(metaclass=BrowserProviderRegistrar.Meta):
    """Browser provider base. __capabilities__ for capability declaration.

    __init__ params → conscribe config. Complex capability config in L2.
    Subclasses inherit ``provider_id`` from ``__registry_key__`` — no need to
    define a ``provider_id`` property in each concrete provider.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __capabilities__: ClassVar[set[str]] = set()
    __registry_key__: ClassVar[str]

    @property
    def provider_id(self) -> str:
        """Return the conscribe registry key as provider_id."""
        return self.__registry_key__
