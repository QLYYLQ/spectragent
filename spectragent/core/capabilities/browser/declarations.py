"""What the browser capability declares to the framework.

``core/registries.py`` reads the ``declarations`` module of every capability
package (``docs/architecture_design.md`` §4.2.1). This one imports only
registrar definitions and the setup schema, never the implementation, so
reading it loads no agent-cdp, websockets or numpy
(``tests/test_core/test_capabilities/test_capability_declarations.py``).
"""

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderRegistrar
from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerRegistrar
from spectragent.core.capabilities.browser.dom.serializers import DOMSerializerRegistrar
from spectragent.core.capabilities.browser.input.rhythm import TypingRhythmRegistrar
from spectragent.core.capabilities.browser.input.trajectory import MouseTrajectoryRegistrar
from spectragent.core.capabilities.browser.replay.miss_policy import MissPolicyRegistrar
from spectragent.core.capabilities.browser.replay.network_source import NetworkSourceRegistrar
from spectragent.core.capabilities.browser.replay.replayability import ReplayabilityRegistrar
from spectragent.core.capabilities.browser.task_setup import BrowserSetup

#: The browser's internal strategy axes, layer name → registrar. An axis left
#: out is not collected: its YAML field degrades to a ``Literal`` of its keys
#: and every knob of its strategies becomes unreachable (``mouse_trajectory``
#: shipped that way for a round, §4.13).
STRATEGY_AXES = {
    "browser_provider": BrowserProviderRegistrar,
    "mouse_trajectory": MouseTrajectoryRegistrar,
    "typing_rhythm": TypingRhythmRegistrar,
    "dom_cleaner": DOMCleanerRegistrar,
    "dom_serializer": DOMSerializerRegistrar,
    "replayability": ReplayabilityRegistrar,
    "miss_policy": MissPolicyRegistrar,
    "network_source": NetworkSourceRegistrar,
}

#: The browser task's setup (``start_url``, ``storage_state``, ...); importing
#: it here registers it, so a loader finds it without importing the browser.
TASK_SETUP = BrowserSetup
