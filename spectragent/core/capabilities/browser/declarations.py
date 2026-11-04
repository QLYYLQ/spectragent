"""What the browser capability declares to the framework.

``core/registries.py`` reads the ``declarations`` module of every capability
package (``docs/architecture_design.md`` §4.2.1). This one imports only
registrar definitions and the setup schema, never the implementation, so
reading it loads no agent-cdp, websockets or numpy
(``tests/test_core/test_capabilities/test_capability_declarations.py``).
"""

from collections.abc import Mapping

from pydantic import JsonValue

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderRegistrar
from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerRegistrar
from spectragent.core.capabilities.browser.dom.serializers import DOMSerializerRegistrar
from spectragent.core.capabilities.browser.input.rhythm import TypingRhythmRegistrar
from spectragent.core.capabilities.browser.input.trajectory import MouseTrajectoryRegistrar
from spectragent.core.capabilities.browser.replay.miss_policy import MissPolicyRegistrar
from spectragent.core.capabilities.browser.replay.network_source import NetworkSourceRegistrar
from spectragent.core.capabilities.browser.replay.replayability import ReplayabilityRegistrar
from spectragent.core.capabilities.browser.task_setup import BrowserSetup
from spectragent.core.registries import TemplateFlag
from spectragent.schemas.task import REPLAY_ARCHIVE_KEY, StartFlag

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

#: Where a single task starts, as ``spectragent run`` flags (§4.9 "起始参数"):
#: each fills the browser setup's context key of the same meaning.
START_FLAGS = (
    StartFlag(
        "--start-url",
        "start_url",
        "Page to open before the agent starts: one complete http:// or https:// URL.",
    ),
    StartFlag(
        "--storage-state",
        "storage_state",
        "Cookies and Web Storage to start the browser with: a JSON file of a browser storage "
        "state, such as one the browser.storage archiver recorded.",
    ),
    StartFlag(
        "--replay-archive",
        REPLAY_ARCHIVE_KEY,
        "Recorded run directory to replay the browser's network from (needs network_source: "
        "archive).",
    ),
)

#: Where ``spectragent config template`` starts the browser block of a new
#: template (§4.17 "capability 的模板起点"): a local Chrome, instant mouse and
#: typing, BrowserUse's DOM cleaning printed as text, no pinning, the live
#: network. These are the template's starting choices, written into the YAML
#: it prints, not defaults: a run still names its ``backend``, ``dom_cleaner``
#: and ``dom_serializer`` itself (§4.2.1 "单实例策略受体省略即默认").
TEMPLATE_BLOCK: Mapping[str, JsonValue] = {
    "backend": {"provider_id": "local_chrome"},
    "mouse_trajectory": {"trajectory_type": "instant"},
    "typing_rhythm": {"rhythm_type": "instant"},
    "dom_cleaner": {"cleaner_type": "browser_use"},
    "dom_serializer": {"serializer_type": "text"},
    "replayability": {"replayability_id": "none"},
    "network_source": {"source_id": "live"},
}

#: The browser's ``config template`` flags (§4.17): each replaces the strategy
#: in one receptor of the template's browser block.
TEMPLATE_FLAGS = (
    TemplateFlag(
        "--backend",
        "backend",
        "Browser provider registry key; new templates use local_chrome.",
    ),
    TemplateFlag(
        "--replayability",
        "replayability",
        "Replayability registry key (none/pinned). Network source and archive remain independent.",
    ),
    TemplateFlag(
        "--dom-cleaner",
        "dom_cleaner",
        "DOM cleaner registry key; new templates use the agent's start, else browser_use.",
    ),
    TemplateFlag(
        "--dom-serializer",
        "dom_serializer",
        "DOM serializer registry key; new templates use the agent's start, else text.",
    ),
)

#: Modules whose top-level imports need an optional extra, which discovery
#: never imports. The numpy sampler (the ``learned-mouse`` extra) registers
#: nothing; the ``learned`` trajectory imports it when it is constructed.
OPTIONAL_EXTRA_MODULES = ("spectragent.core.capabilities.browser.input._mouse_gan_v2",)
