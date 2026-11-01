"""The Android provider registry: one per capability (``docs/architecture_design.md`` §4.9.x)."""

from typing import ClassVar

from conscribe import create_registrar

from spectragent.core.capabilities.android.backends.base import BaseAndroidProvider

AndroidProviderRegistrar = create_registrar(
    "android_provider",
    BaseAndroidProvider,
    discriminator_field="provider_id",
    inline_only=True,
)


class AndroidProviderBase(metaclass=AndroidProviderRegistrar.Meta):
    """Base of every Android provider; ``provider_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def provider_id(self) -> str:
        return self.__registry_key__
