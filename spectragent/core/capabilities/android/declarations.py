"""What the Android capability declares to the framework.

``core/registries.py`` reads the ``declarations`` module of every capability
package (``docs/architecture_design.md`` §4.2.1). This one imports only
registrar definitions and the setup schema, never the implementation
(``tests/test_core/test_capabilities/test_capability_declarations.py``).
"""

from spectragent.core.capabilities.android.backends._registrar import AndroidProviderRegistrar
from spectragent.core.capabilities.android.input._registrar import (
    AndroidTapInjectorRegistrar,
    AndroidTypingBackendRegistrar,
)
from spectragent.core.capabilities.android.task_setup import AndroidSetup
from spectragent.core.capabilities.android.ui_tree._registrar import (
    AndroidUITreeCleanerRegistrar,
    AndroidUITreeSerializerRegistrar,
    AndroidUITreeSourceRegistrar,
)
from spectragent.schemas.task import StartFlag

#: The Android capability's internal strategy axes, layer name → registrar: the
#: provider, the tap injector, the typing backend, and the UI tree's source,
#: cleaner and serializer (§11.2).
STRATEGY_AXES = {
    "android_provider": AndroidProviderRegistrar,
    "android_tap_injector": AndroidTapInjectorRegistrar,
    "android_typing_backend": AndroidTypingBackendRegistrar,
    "android_ui_tree_source": AndroidUITreeSourceRegistrar,
    "android_ui_tree_cleaner": AndroidUITreeCleanerRegistrar,
    "android_ui_tree_serializer": AndroidUITreeSerializerRegistrar,
}

#: The Android task's setup; importing it here registers it, so a loader finds
#: it without importing the Android implementation.
TASK_SETUP = AndroidSetup

#: No single-task start flag yet: the Android setup takes no key that says where a
#: task starts. ``--start-app`` comes with the app name table, when AndroidWorld
#: becomes a full benchmark; the task then starts in that app
#: (``docs/architecture_design.md`` §4.10 "任务的起始点归 capability").
START_FLAGS: tuple[StartFlag, ...] = ()

#: No module of this package needs an optional extra.
OPTIONAL_EXTRA_MODULES: tuple[str, ...] = ()
