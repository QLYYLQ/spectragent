"""What the Android capability declares to the framework.

``core/registries.py`` reads the ``declarations`` module of every capability
package (``docs/architecture_design.md`` §4.2.1). This one imports only
registrar definitions and the setup schema, never the implementation
(``tests/test_core/test_capabilities/test_capability_declarations.py``).
"""

from spectragent.core.capabilities.android.backends._registrar import AndroidProviderRegistrar
from spectragent.core.capabilities.android.task_setup import AndroidSetup

#: The Android capability's internal strategy axes, layer name → registrar. The
#: provider is the only one so far: the typing backend, the tap injector and the
#: UI tree source become axes when each has a second implementation (§11.2).
STRATEGY_AXES = {"android_provider": AndroidProviderRegistrar}

#: The Android task's setup; importing it here registers it, so a loader finds
#: it without importing the Android implementation.
TASK_SETUP = AndroidSetup

#: No module of this package needs an optional extra.
OPTIONAL_EXTRA_MODULES: tuple[str, ...] = ()
