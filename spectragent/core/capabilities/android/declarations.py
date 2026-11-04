"""What the Android capability declares to the framework.

``core/registries.py`` reads the ``declarations`` module of every capability
package (``docs/architecture_design.md`` §4.2.1). This one imports only
registrar definitions and the setup schema, never the implementation
(``tests/test_core/test_capabilities/test_capability_declarations.py``).
"""

from collections.abc import Mapping

from pydantic import JsonValue

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
from spectragent.core.registries import TemplateFlag
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

#: The single-task start flag: the app the task starts in, by its name on the
#: home screen (or its package name), opened before the agent's first step;
#: that app is the task's start point (``docs/architecture_design.md`` §4.9
#: "起始参数", §4.10 "任务的起始点归 capability").
START_FLAGS: tuple[StartFlag, ...] = (
    StartFlag(
        flag="--start-app",
        key="start_app",
        help=(
            "The app the task starts in, by its name on the home screen (or its package name), "
            "opened before the agent's first step"
        ),
    ),
)

#: Where ``spectragent config template`` starts the Android block of a new
#: template (§4.17 "capability 的模板起点"): the choices the repository runs M3A
#: on AndroidWorld with, the official emulator, touches injected by the shell
#: helper, text typed as key events, AndroidWorld's elements printed as M3A
#: reads them. These are the template's starting choices, written into the YAML
#: it prints, not defaults: a run still names each of these receptors itself
#: (§4.2.1). The AVD and the service APK are the machine's own, and the schema
#: requires both, so they start as placeholders the YAML shows: ``config check``
#: refuses the APK placeholder, and a run refuses the AVD placeholder when it
#: boots the emulator.
TEMPLATE_BLOCK: Mapping[str, JsonValue] = {
    "service_apk": "<the path the device APK build prints>",
    "backend": {"provider_id": "local_emulator", "avd": "<an AVD name emulator -list-avds prints>"},
    "tap_injector": {"injector_id": "shell_injection"},
    "typing_backend": {"typing_id": "key_events"},
    "ui_tree_cleaner": {"cleaner_id": "androidworld"},
    "ui_tree_serializer": {"serializer_id": "m3a"},
}

#: No ``config template`` flags: the Android block's choices are edited in the YAML.
TEMPLATE_FLAGS: tuple[TemplateFlag, ...] = ()

#: No module of this package needs an optional extra.
OPTIONAL_EXTRA_MODULES: tuple[str, ...] = ()

#: A branch of an Android recording needs no archive: the restorer replays the
#: recorded actions on a device the benchmark's setup prepared again
#: (``docs/architecture_design.md`` §11.2 "分支回放"). ``trace_branches`` reads
#: this when a branches YAML leaves ``require_archive`` unwritten (§4.10).
BRANCH_ARCHIVES: tuple[str, ...] = ()
