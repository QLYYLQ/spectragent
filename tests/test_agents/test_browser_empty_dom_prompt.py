"""An empty serialized DOM must never expand into an unbudgeted dataclass repr."""

from __future__ import annotations

from types import SimpleNamespace

from spectragent.agents.browser_use import BrowserUseDomAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState
from tests._helpers.assembly import make_agent


def test_empty_dom_string_does_not_render_tree_repr() -> None:
    state = DOMState(
        dom_tree=DOMElementNode(tag_name="input", attributes={"value": "private-value"}),
        serialized_dom="",
    )
    observation = SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        dom=SimpleNamespace(element_tree=state),
    )

    browser_use = make_agent(BrowserUseDomAgent, memory=SimpleMemory(max_messages=10))
    rendered_browser_use = browser_use._render_browser_state(observation, None)
    skyvern = SkyvernAgent.__new__(SkyvernAgent)
    rendered_skyvern = skyvern._render_browser_state(observation)

    assert "DOMState(" not in rendered_browser_use + rendered_skyvern
    assert "private-value" not in rendered_browser_use + rendered_skyvern
