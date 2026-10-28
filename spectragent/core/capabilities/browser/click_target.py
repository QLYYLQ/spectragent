"""Compare a click's actual DOM target before movement and before the press.

This is an execution precondition, separate from diagnostic action effects.
Only a measured target change refuses the click; evidence that cannot be read
never does. References live in one object group per click, never in page globals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import uuid4

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.effect_probes import (
    DEEP_HIT_JS,
    ProbeBudget,
    evaluation_value,
)
from spectragent.core.capabilities.browser.input._schedule import finish_cleanup

logger = logging.getLogger(__name__)


class ClickTargetChanged(RuntimeError):
    """The target under the pointer was replaced while it moved; nothing was pressed."""

    def __init__(self) -> None:
        super().__init__(
            "Click target changed while the pointer was moving; no mouse button was pressed. "
            "Observe the page again before choosing the next action."
        )


# These identify the DOM control, not its label: replacing a span inside the
# same button preserves identity, replacing a button with its twin does not.
_CLICK_TARGET_JS = r"""
function clickTarget(el) {
  const roles = new Set(['button', 'link', 'checkbox', 'radio', 'switch', 'tab',
    'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'combobox', 'listbox',
    'textbox', 'searchbox', 'slider', 'spinbutton', 'treeitem']);
  let fallback = null;
  for (let node = el, hops = 0; node && hops < 4096; hops += 1) {
    if (node.nodeType === 1) {
      const tag = node.localName;
      const role = (node.getAttribute('role') || '').trim().toLowerCase().split(/\s+/)[0];
      if (['button', 'input', 'textarea', 'select', 'summary', 'label'].includes(tag) ||
          (tag === 'a' && node.hasAttribute('href')) || roles.has(role) ||
          (node.isContentEditable && !node.parentElement?.isContentEditable)) return node;
      if (!fallback && (node.hasAttribute('onclick') || node.hasAttribute('tabindex'))) {
        fallback = node;
      }
    }
    node = node.assignedSlot || node.parentNode || node.host || null;
  }
  return fallback || el;
}
"""

_CAPTURE_EXPRESSION = (
    "(() => { /* spectragent click target snapshot */"
    + DEEP_HIT_JS
    + _CLICK_TARGET_JS
    + "return clickTarget(deepHit(%(x)d, %(y)d).el); })()"
)

_CHANGED_FUNCTION = (
    "function(x, y) { /* spectragent click target check */"
    + DEEP_HIT_JS
    + _CLICK_TARGET_JS
    + "return !this.isConnected || clickTarget(deepHit(x, y).el) !== this; }"
)


@dataclass
class ClickTargetGuard:
    """One click's independent, probe-time-only target comparison budget."""

    cdp: CDPCommandProtocol
    session_id: str | None
    budget: ProbeBudget
    _object_id: str | None = field(default=None, init=False)
    _group: str = field(default_factory=lambda: f"click-target-{uuid4().hex}", init=False)

    async def capture(self, x: int, y: int) -> None:
        async def read() -> str | None:
            reply = await self.cdp.send(
                "Runtime.evaluate",
                {
                    "expression": _CAPTURE_EXPRESSION % {"x": x, "y": y},
                    "returnByValue": False,
                    "objectGroup": self._group,
                },
                session_id=self.session_id,
            )
            if not isinstance(reply, dict) or reply.get("exceptionDetails"):
                return None
            result = reply.get("result")
            return result.get("objectId") if isinstance(result, dict) else None

        answer = await self.budget.run(read, "click target before movement")
        self._object_id = answer if isinstance(answer, str) else None

    async def changed(self, x: int, y: int) -> bool:
        """Whether a change was measured; a target that cannot be compared never counts."""
        if self._object_id is None:
            return False

        async def compare() -> bool:
            reply = await self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": _CHANGED_FUNCTION,
                    "objectId": self._object_id,
                    "arguments": [{"value": x}, {"value": y}],
                    "returnByValue": True,
                },
                session_id=self.session_id,
            )
            return evaluation_value(reply) is True

        return await self.budget.run(compare, "click target before press") is True

    async def close(self) -> None:
        # Also frees a reference whose reply was lost or cancelled. The cleanup
        # has its own finite bound, even when either probe budget is exhausted.
        try:
            await finish_cleanup(
                self.cdp.send(
                    "Runtime.releaseObjectGroup",
                    {"objectGroup": self._group},
                    session_id=self.session_id,
                ),
                timeout=1.0,
                logger=logger,
                failure="Releasing click target references failed",
            )
        except Exception as exc:
            logger.debug("Releasing click target references failed: %r", exc)
