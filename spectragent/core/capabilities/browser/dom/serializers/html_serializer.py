"""Compact HTML serializer — skyvern default format.

Produces minimal HTML that preserves interactive element structure while
aggressively trimming attributes to a reserved set.  Optimized for LLM
token efficiency.

Output example::

    <div id="app">
      <button id="aB3k" aria-label="Submit">Submit</button>
      <input id="cD5m" type="text" name="email" placeholder="Email" />
      <a id="eF7n" href="/about">About</a>
    </div>
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState
from spectragent.core.capabilities.browser.dom.serializers import DOMSerializerBase

# Skyvern-style attribute allowlist — only these survive trimming.
_ALLOWED_ATTRS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "type",
        "value",
        "placeholder",
        "href",
        "src",
        "alt",
        "title",
        "role",
        "for",
        "action",
        "method",
        "checked",
        "selected",
        "disabled",
        "readonly",
        "required",
        "maxlength",
        "pattern",
        "min",
        "max",
        "step",
        "aria-label",
        "aria-checked",
        "aria-current",
        "aria-disabled",
        "aria-expanded",
        "aria-readonly",
        "aria-required",
        "aria-role",
        "aria-selected",
        "accept",
        "shape-description",
        "data-ui",
    }
)

_VOID_TAGS: frozenset[str] = frozenset(
    {
        "img",
        "input",
        "br",
        "hr",
        "meta",
        "link",
        "area",
        "base",
        "col",
        "embed",
        "source",
        "track",
        "wbr",
    }
)


class HTMLSerializer(DOMSerializerBase):
    """Compact HTML serializer with aggressive attribute trimming."""

    __registry_key__ = "html"

    def __init__(self, *, max_href_length: int = 150) -> None:
        self.max_href_length = max_href_length

    def serialize(self, state: DOMState) -> str:
        if state.dom_tree is None:
            return ""
        return self._render(state.dom_tree, depth=0)

    def _render(self, node: DOMElementNode, depth: int) -> str:
        tag = node.tag_name.lower()

        # Skip non-content wrappers (a shadow root included).
        if tag in ("html", "body", "#document-fragment"):
            return self._render_children(node, depth)

        attrs = self._filter_attrs(node)
        # The selector-map key of an indexed element, as a data attribute.
        if node.element_index is not None:
            attrs = f' data-idx="{node.element_index}"' + attrs

        text = node.text_content.strip() if node.text_content else ""

        if tag in _VOID_TAGS:
            return f"{'  ' * depth}<{tag}{attrs} />\n"

        children_html = self._render_children(node, depth + 1)
        if not text and not children_html.strip() and node.element_index is None:
            return ""  # Drop empty non-interactive elements (keep interactive ones).

        if text and not children_html.strip():
            return f"{'  ' * depth}<{tag}{attrs}>{text}</{tag}>\n"

        result = f"{'  ' * depth}<{tag}{attrs}>\n"
        if text:
            result += f"{'  ' * (depth + 1)}{text}\n"
        result += children_html
        result += f"{'  ' * depth}</{tag}>\n"
        return result

    def _render_children(self, node: DOMElementNode, depth: int) -> str:
        # Shadow-root and frame content too: an element that is indexed but
        # never rendered is a target the model cannot see.
        parts: list[str] = []
        for child in node.composed_children():
            rendered = self._render(child, depth)
            if rendered:
                parts.append(rendered)
        return "".join(parts)

    def _filter_attrs(self, node: DOMElementNode) -> str:
        parts: list[str] = []
        for key, value in node.attributes.items():
            if key not in _ALLOWED_ATTRS:
                continue
            if not value:
                parts.append(f" {key}")
                continue
            # Filter base64 data URLs.
            if key in ("src", "href") and value.startswith("data:"):
                continue
            # Truncate long URLs.
            if key in ("href", "src") and len(value) > self.max_href_length:
                value = value[: self.max_href_length - 3] + "..."
            parts.append(f' {key}="{value}"')
        return "".join(parts)
