"""An empty serialized DOM must never expand into an unbudgeted dataclass repr."""

from __future__ import annotations

from types import SimpleNamespace

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState


def test_empty_dom_string_does_not_render_tree_repr() -> None:
    state = DOMState(
        dom_tree=DOMElementNode(tag_name="input", attributes={"value": "private-value"}),
        serialized_dom="",
    )
    observation = SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        dom=SimpleNamespace(element_tree=state),
    )

    browser_use = BrowserUseAgent.__new__(BrowserUseAgent)
    browser_use.observation_mode = "dom"
    rendered_browser_use = browser_use._render_browser_state(observation)
    rendered_skyvern = SkyvernAgent._render_browser_state(observation)

    assert "DOMState(" not in rendered_browser_use + rendered_skyvern
    assert "private-value" not in rendered_browser_use + rendered_skyvern
