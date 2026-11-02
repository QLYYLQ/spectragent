"""The Android UI-tree axes: the tree's source, its cleaner and its serializer (``docs/architecture_design.md`` §11.2 "策略轴").

Each is a registrar in the Android package and a receptor on
``AndroidCapability``; the same shape as the browser's DOM cleaner and
serializer (§4.12), and apart from the source:

- **Source** (``source_id``, the ``ui_tree_source`` receptor): who captures the
  window forest. One value today, ``accessibility_service``: our service reads
  the full tree, the views that are not important for accessibility and the
  view ids included, each node marked with whether it is important
  (``device/forest.py``). Being the only option, it is the default a block may
  leave out (§4.2.1). ``uiautomator dump`` is the other source §11.2 names;
  it is deferred: it runs only while our service is off, which today turns off
  every gesture injector and both typing backends.
- **Cleaner** (``cleaner_id``, ``ui_tree_cleaner``): which nodes become
  elements, and their numbers. It keeps each element's bounds and the
  number-to-node map (:class:`~spectragent.core.capabilities.android.ui_tree.models.UITree`)
  even when the printed text carries no bounds: ``click`` and ``scroll`` find
  an element by its number.
- **Serializer** (``serializer_id``, ``ui_tree_serializer``): how the elements
  are printed for a model, and a description of that text
  (:meth:`AndroidUITreeSerializer.describe_format`), which an agent's prompt
  quotes instead of describing the format itself (§4.4 "观测格式由提供方描述").

The cleaner and the serializer decide what the agent sees, so both are
required, with no default (§4.2.1; the user's browser ruling, "dom配置必须写").
Which source produced each observation is recorded on it
(``android.ui_tree``'s ``producer``, §14.4).

This module stays light: the package's ``declarations.py`` imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest
from spectragent.core.capabilities.android.input._registrar import DevicePrograms
from spectragent.core.capabilities.android.ui_tree.models import UITree

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.device.clients import TreeCapture

#: A screen's width and height, in the pixels a tree's bounds are in.
ScreenSize = tuple[int, int]


@runtime_checkable
class AndroidUITreeSource(Protocol):
    """Captures the window forest."""

    @property
    def source_id(self) -> str:
        """Its registry key: the producer each observation records."""
        ...

    async def capture(self, device: DevicePrograms) -> TreeCapture:
        """The whole forest as the device reports it now, with what the capture cost."""
        ...


AndroidUITreeSourceRegistrar = create_registrar(
    "android_ui_tree_source",
    AndroidUITreeSource,
    discriminator_field="source_id",
    inline_only=True,
)


class AndroidUITreeSourceBase(metaclass=AndroidUITreeSourceRegistrar.Meta):
    """Base of every tree source; ``source_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def source_id(self) -> str:
        return self.__registry_key__


def default_ui_tree_source() -> AndroidUITreeSource:
    """The tree source a capability holds unless configured otherwise: the only one there is.

    ``AndroidCapability.__init__`` installs it, and its ``__wiring__`` names the
    same key as its ``default`` (§4.2.1); ``test_strategy_defaults.py`` holds
    the two together. Imported lazily: ``source.py`` imports this module.
    """
    from spectragent.core.capabilities.android.ui_tree.source import (
        AccessibilityServiceTreeSource,
    )

    return AccessibilityServiceTreeSource()


@runtime_checkable
class AndroidUITreeCleaner(Protocol):
    """Chooses the elements of a captured forest and numbers them."""

    @property
    def cleaner_id(self) -> str: ...

    def clean(self, forest: AndroidAccessibilityForest, screen: ScreenSize) -> UITree:
        """The elements of *forest* on a screen of *screen* pixels, numbered, with the nodes they stand for."""
        ...


AndroidUITreeCleanerRegistrar = create_registrar(
    "android_ui_tree_cleaner",
    AndroidUITreeCleaner,
    discriminator_field="cleaner_id",
    inline_only=True,
)


class AndroidUITreeCleanerBase(metaclass=AndroidUITreeCleanerRegistrar.Meta):
    """Base of every cleaner; ``cleaner_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def cleaner_id(self) -> str:
        return self.__registry_key__


@runtime_checkable
class AndroidUITreeSerializer(Protocol):
    """Prints a cleaned tree's elements for a model, and describes what it prints."""

    @property
    def serializer_id(self) -> str: ...

    def serialize(self, tree: UITree) -> str:
        """The elements of *tree* as the text a model reads."""
        ...

    def describe_format(self) -> str:
        """A short description, for a model, of the text :meth:`serialize` writes.

        What an element looks like, how an action names one by its index, and
        anything else the text can carry; true of everything :meth:`serialize`
        emits, since an agent quotes it in its prompt verbatim.
        """
        ...


AndroidUITreeSerializerRegistrar = create_registrar(
    "android_ui_tree_serializer",
    AndroidUITreeSerializer,
    discriminator_field="serializer_id",
    inline_only=True,
)


class AndroidUITreeSerializerBase(metaclass=AndroidUITreeSerializerRegistrar.Meta):
    """Base of every serializer; ``serializer_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def serializer_id(self) -> str:
        return self.__registry_key__
