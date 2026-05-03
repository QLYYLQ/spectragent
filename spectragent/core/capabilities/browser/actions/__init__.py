"""Browser action classes — each action is an independently registered unit.

Importing this package triggers conscribe registration of every action
class via the metaclass machinery. We never call ``discover()`` here —
that is reserved for ``generate_config.py`` / bootstrap scripts.

Adding a new browser action = new module + new import line here.
"""

from spectragent.core.capabilities.browser.actions.click import ClickAction
from spectragent.core.capabilities.browser.actions.click_coordinate import (
    ClickCoordinateAction,
)
from spectragent.core.capabilities.browser.actions.done import DoneAction
from spectragent.core.capabilities.browser.actions.extract_content import (
    ExtractContentAction,
)
from spectragent.core.capabilities.browser.actions.go_back import GoBackAction
from spectragent.core.capabilities.browser.actions.go_forward import GoForwardAction
from spectragent.core.capabilities.browser.actions.navigate import NavigateAction
from spectragent.core.capabilities.browser.actions.screenshot import ScreenshotAction
from spectragent.core.capabilities.browser.actions.scroll import ScrollAction
from spectragent.core.capabilities.browser.actions.select_dropdown import (
    SelectDropdownAction,
)
from spectragent.core.capabilities.browser.actions.send_keys import SendKeysAction
from spectragent.core.capabilities.browser.actions.switch_tab import SwitchTabAction
from spectragent.core.capabilities.browser.actions.type_action import TypeAction
from spectragent.core.capabilities.browser.actions.wait import WaitAction

__all__ = [
    "ClickAction",
    "ClickCoordinateAction",
    "DoneAction",
    "ExtractContentAction",
    "GoBackAction",
    "GoForwardAction",
    "NavigateAction",
    "ScreenshotAction",
    "ScrollAction",
    "SelectDropdownAction",
    "SendKeysAction",
    "SwitchTabAction",
    "TypeAction",
    "WaitAction",
]
