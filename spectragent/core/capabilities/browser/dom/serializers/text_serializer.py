"""Indented text serializer — matches browser-use output format.

Output example::

    [12345]<button id="submit" aria-label="Submit Form" />
        Submit
    *[12346]<input type="text" placeholder="Search" format=YYYY-MM-DD />
    |scroll element|<div role="listbox" /> (1.2 pages above, 0.5 pages below)
    |SHADOW(open)|[12347]<custom-element />
        Open Shadow
            ...
        Shadow End

Markers:
- ``[index]`` prefix: interactive element; the number is its selector_map key
  (``element_index`` — the backend node id, namespaced for an element inside
  an out-of-process frame)
- ``*`` prefix: newly appeared element (not in previous snapshot)
- ``|scroll element|`` prefix: scrollable container
- ``|SHADOW(open/closed)|`` prefix: shadow host
- ``|IFRAME|`` / ``|FRAME|`` prefix: frame owner; its document is indented below
- ``<!-- SVG content collapsed -->`` suffix: SVG children skipped
- ``(scroll info)`` suffix: scroll progress hint
- ``compound_components=(...)`` in attributes: virtual sub-components
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState
from spectragent.core.capabilities.browser.dom.serializers import DOMSerializerBase

# Attributes to include in serialized output — matches browser-use
# DEFAULT_INCLUDE_ATTRIBUTES (views.py:18-82).
_INCLUDE_ATTRS: list[str] = [
    "title", "type", "checked", "id", "name", "role", "value", "placeholder",
    "data-date-format", "alt", "aria-label", "aria-expanded", "data-state",
    "aria-checked", "aria-valuemin", "aria-valuemax", "aria-valuenow",
    "aria-placeholder",
    # Validation
    "pattern", "min", "max", "minlength", "maxlength", "step",
    "accept", "multiple", "inputmode", "autocomplete", "aria-autocomplete",
    "list", "data-mask", "data-inputmask", "data-datepicker",
    "format", "expected_format", "contenteditable",
    # Webkit shadow DOM
    "pseudo",
    # AX properties (also scanned from ax_properties)
    "selected", "expanded", "pressed", "disabled", "invalid",
    "valuemin", "valuemax", "valuenow", "keyshortcuts", "haspopup",
    "multiselectable", "required", "valuetext", "level", "busy", "live",
    "ax_name",
]

_INCLUDE_ATTRS_SET: frozenset[str] = frozenset(_INCLUDE_ATTRS)

# Non-content tags to skip entirely during serialization.
_DISABLED_ELEMENTS: frozenset[str] = frozenset({
    "style", "script", "head", "meta", "link", "title", "noscript", "template",
})

# SVG child elements to skip (decorative only).
_SVG_ELEMENTS: frozenset[str] = frozenset({
    "path", "rect", "g", "circle", "ellipse", "line", "polyline", "polygon",
    "use", "defs", "clippath", "mask", "pattern", "image", "text", "tspan",
})

# Date/time format map for HTML5 date inputs.
_DATE_FORMAT_MAP: dict[str, str] = {
    "date": "YYYY-MM-DD",
    "time": "HH:MM",
    "datetime-local": "YYYY-MM-DDTHH:MM",
    "month": "YYYY-MM",
    "week": "YYYY-W##",
}


class TextSerializer(DOMSerializerBase):
    """Indented text serializer for LLM consumption — browser-use compatible."""

    __registry_key__ = "text"

    def __init__(self, *, max_text_length: int = 200) -> None:
        self.max_text_length = max_text_length

    def serialize(self, state: DOMState) -> str:
        if state.dom_tree is None:
            return ""
        lines: list[str] = []
        self._walk(state.dom_tree, 0, lines)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tree walker — compact output (matches original browser-use)
    #
    # Rendered elements (interactive / scrollable / iframe) get a tag
    # line; their children are indented.  Non-rendered elements are
    # transparent (children inherit parent depth).  Text nodes are
    # emitted whenever visible, regardless of nesting context.
    # ------------------------------------------------------------------

    def _walk(
        self, node: DOMElementNode, depth: int, lines: list[str],
    ) -> None:
        tag = node.tag_name.lower()
        pad = "\t" * depth

        # DOCUMENT_NODE (9): transparent.
        if node.node_type == 9:
            self._recurse(node, depth, lines)
            return

        # DOCUMENT_FRAGMENT_NODE (11, shadow root).
        if node.node_type == 11:
            self._recurse(node, depth, lines)
            return

        # TEXT_NODE (3): output if visible + has layout (matches original browser-use).
        if node.node_type == 3:
            if node.is_visible:
                text = (node.node_value or "").strip()
                if text and len(text) > 1:
                    if len(text) > self.max_text_length:
                        text = text[: self.max_text_length] + "..."
                    lines.append(f"{pad}{text}")
            return

        if tag in _DISABLED_ELEMENTS:
            return

        # excluded_by_parent: skip tag, recurse with inherited context.
        # Must come BEFORE SVG handling so excluded SVGs are transparent.
        if not node.should_display:
            self._recurse(node, depth, lines)
            return

        if tag == "svg":
            attrs_str = self._build_attributes_string(node, "")
            prefix = ""
            if node.is_interactive and node.element_index is not None:
                prefix = f"[{node.element_index}]"
            lines.append(f"{pad}{prefix}<svg{' ' + attrs_str if attrs_str else ''} /> <!-- SVG content collapsed -->")
            return

        if tag in _SVG_ELEMENTS:
            return

        # Should this node produce a tag line?
        is_interactive = node.is_interactive and node.element_index is not None
        is_scrollable = node.is_scrollable
        is_iframe = tag in ("iframe", "frame")
        should_render = is_interactive or is_scrollable or is_iframe

        if should_render:
            lines.append(self._build_element_line(node, depth))
            if node._compound_children:
                return
            self._recurse(node, depth + 1, lines)
        else:
            self._recurse(node, depth, lines)

    def _recurse(
        self, node: DOMElementNode, depth: int, lines: list[str],
    ) -> None:
        for child in node.children:
            self._walk(child, depth, lines)
        if node.shadow_roots:
            for sr in node.shadow_roots:
                self._walk(sr, depth, lines)
        if node.content_document:
            self._walk(node.content_document, depth, lines)

    # ------------------------------------------------------------------
    # Element line builder
    # ------------------------------------------------------------------

    def _build_element_line(self, node: DOMElementNode, depth: int) -> str:
        """Build a single line like: ``\\t\\t*|scroll element[12345]<button attr=val />``."""
        pad = "\t" * depth
        tag = node.tag_name.lower()

        # --- Shadow host prefix ---
        shadow_prefix = ""
        if node.shadow_roots:
            has_closed = any(
                sr.shadow_root_type and sr.shadow_root_type.lower() == "closed"
                for sr in node.shadow_roots
            )
            shadow_prefix = "|SHADOW(closed)|" if has_closed else "|SHADOW(open)|"

        # --- New element marker ---
        new_prefix = "*" if node.is_new else ""

        # --- Scrollable / Interactive / Frame prefix ---
        is_scrollable = node.is_scrollable
        should_show_scroll = is_scrollable
        is_indexed = node.is_interactive and node.element_index is not None

        if is_indexed:
            scroll_prefix = "|scroll element[" if should_show_scroll else "["
            element_prefix = f"{new_prefix}{scroll_prefix}{node.element_index}]"
        elif should_show_scroll:
            element_prefix = "|scroll element|"
        elif tag == "iframe":
            element_prefix = "|IFRAME|"
        elif tag == "frame":
            element_prefix = "|FRAME|"
        else:
            element_prefix = ""

        # --- Attributes ---
        text_content = node.text_content.strip() if node.text_content else ""
        attrs_str = self._build_attributes_string(node, text_content)

        # Build line.
        line = f"{pad}{shadow_prefix}{element_prefix}<{tag}"
        if attrs_str:
            line += f" {attrs_str}"
        line += " />"

        # --- Scroll info suffix ---
        if should_show_scroll:
            scroll_text = _get_scroll_info_text(node)
            if scroll_text:
                line += f" ({scroll_text})"

        return line

    # ------------------------------------------------------------------
    # Attributes builder
    # ------------------------------------------------------------------

    def _build_attributes_string(self, node: DOMElementNode, text_content: str) -> str:
        """Build space-separated ``key=value`` pairs for an element.

        Includes HTML attributes, AX properties, date format hints,
        compound components, and deduplication logic.
        """
        attrs_out: dict[str, str] = {}

        # 1. HTML attributes.
        for key, value in node.attributes.items():
            if key in _INCLUDE_ATTRS_SET:
                val = str(value).strip()
                if val:
                    attrs_out[key] = val

        # 2. Date/time format hints.
        if node.tag_name.lower() == "input":
            input_type = node.attributes.get("type", "").lower()
            if input_type in _DATE_FORMAT_MAP:
                attrs_out["format"] = _DATE_FORMAT_MAP[input_type]
                if "placeholder" not in attrs_out:
                    attrs_out["placeholder"] = _DATE_FORMAT_MAP[input_type]

        # 3. AX properties as synthetic attributes.
        if node.ax_properties:
            for prop in node.ax_properties:
                if prop.name in _INCLUDE_ATTRS_SET and prop.value is not None:
                    if isinstance(prop.value, bool):
                        attrs_out[prop.name] = str(prop.value).lower()
                    else:
                        val = str(prop.value).strip()
                        if val:
                            attrs_out[prop.name] = val

            # AX tree value for form elements (reflects actual typed value).
            if node.tag_name.lower() in ("input", "textarea", "select"):
                for prop in node.ax_properties:
                    if prop.name == "valuetext" and prop.value or prop.name == "value" and prop.value:
                        val = str(prop.value).strip()
                        if val:
                            attrs_out["value"] = val
                            break

        # 4. Compound components.
        if node._compound_children:
            compound_parts: list[str] = []
            for child_info in node._compound_children:
                parts: list[str] = []
                if child_info.get("name"):
                    parts.append(f'name={child_info["name"]}')
                if child_info.get("role"):
                    parts.append(f'role={child_info["role"]}')
                if child_info.get("valuemin") is not None:
                    parts.append(f'min={child_info["valuemin"]}')
                if child_info.get("valuemax") is not None:
                    parts.append(f'max={child_info["valuemax"]}')
                if child_info.get("valuenow") is not None:
                    parts.append(f'current={child_info["valuenow"]}')
                if child_info.get("options_count") is not None:
                    parts.append(f'count={child_info["options_count"]}')
                if child_info.get("first_options"):
                    opts = "|".join(child_info["first_options"][:4])
                    parts.append(f"options={opts}")
                if child_info.get("format_hint"):
                    parts.append(f'format={child_info["format_hint"]}')
                if parts:
                    compound_parts.append(f'({",".join(parts)})')
            if compound_parts:
                attrs_out["compound_components"] = ",".join(compound_parts)

        if not attrs_out:
            return ""

        # 5. Dedup: remove attrs with duplicate long values (>5 chars).
        protected = {"format", "expected_format", "placeholder", "value", "aria-label", "title"}
        seen_values: dict[str, str] = {}
        to_remove: set[str] = set()
        for key in _INCLUDE_ATTRS:
            if key not in attrs_out:
                continue
            val = attrs_out[key]
            if len(val) > 5:
                if val in seen_values and key not in protected:
                    to_remove.add(key)
                else:
                    seen_values[val] = key
        for key in to_remove:
            del attrs_out[key]

        # 6. Remove type if it matches tag name.
        if "type" in attrs_out and attrs_out["type"].lower() == node.tag_name.lower():
            del attrs_out["type"]

        # 7. Remove boolean false values.
        for attr in ("invalid", "required"):
            if attr in attrs_out and attrs_out[attr].lower() in ("false", "0", "no"):
                del attrs_out[attr]

        # 8. Remove AX expanded if aria-expanded also present (prefer AX).
        if "expanded" in attrs_out and "aria-expanded" in attrs_out:
            del attrs_out["aria-expanded"]

        # 9. Remove attrs that duplicate text content.
        for attr in ("aria-label", "placeholder", "title"):
            if (
                attr in attrs_out
                and text_content
                and attrs_out[attr].strip().lower() == text_content.strip().lower()
            ):
                del attrs_out[attr]

        # 10. Format with value capping.
        formatted: list[str] = []
        for key, value in attrs_out.items():
            capped = _cap_text(value, 100)
            if not capped:
                formatted.append(f"{key}=''")
            else:
                formatted.append(f"{key}={capped}")

        return " ".join(formatted)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_scroll_info_text(node: DOMElementNode) -> str:
    """Generate scroll position text like ``1.2 pages above, 0.5 pages below``."""
    if not node.scroll_rects or not node.client_rect:
        if node.is_scrollable:
            return "scroll"
        return ""

    scroll_h = node.scroll_rects.height
    client_h = node.client_rect.height
    if client_h <= 0 or scroll_h <= client_h:
        return ""

    # Approximate pages (we don't have scroll position per-element,
    # so just indicate total scrollable area).
    total_pages = scroll_h / client_h
    if total_pages > 1.1:
        return f"{total_pages - 1:.1f} pages below"
    return ""


def _cap_text(text: str, max_len: int) -> str:
    """Cap text length, adding '...' if truncated."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
