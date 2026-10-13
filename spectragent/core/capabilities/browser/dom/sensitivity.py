"""Is this form control's value a secret? — the one Python rule (§4.12).

A sensitive control's value never reaches the prompt, the recorded DOM
observation or the archive. The Skyvern page-side script
(``dom/js/skyvern_extraction.py``) applies the rule inside the page while it
builds its tree; this module is the same rule for everything in Python:

- the BrowserUse cleaner (``dom/cleaning/browser_use.py``), which withholds
  the values while it builds its tree;
- the archive's raw-DOM redaction (``archive/redaction.py``);
- the typing actions, which judge the focused element before they type and
  record the verdict (``ActionResult.typed_into_sensitive_field``).

The page script and this module are kept equal by tests, not by generating
one from the other: the hint pattern's literal must equal the one in the
page script character for character, and a table of controls is judged by
both sides in a real Chrome.

Outside the rule's reach: the text a typing action enters stays in the
action's parameters (the step record, the model's own replies), and
``extract_content`` of one element returns its outer HTML as the page has
it, a value attribute the page wrote into its markup included.

The rule: a control is an ``INPUT``, ``TEXTAREA`` or ``SELECT`` and

- it is an ``INPUT`` whose ``type`` is ``password``, ``file`` or ``hidden``, or
- its ``autocomplete`` starts with ``cc-`` (a payment card field) or is
  ``one-time-code``, or
- its ``id``, ``name`` or ``autocomplete`` matches :data:`SENSITIVE_HINT_PATTERN`
  (one-time codes, verification codes, PINs).

It errs wide on purpose (``pin`` matches ``shipping``): a false positive
costs one recorded value, a false negative a leaked one.
"""

import re
from collections.abc import Mapping

#: The page script's ``sensitiveHint`` regex, verbatim (case-insensitive).
SENSITIVE_HINT_PATTERN = re.compile(
    r"(?:otp|one.?time|verification|auth.?code|security.?code|pass.?code|two.?factor|2fa|mfa|pin)",
    re.IGNORECASE,
)

#: Elements that hold a user-entered value.
FORM_CONTROL_TAGS = frozenset({"INPUT", "TEXTAREA", "SELECT"})
#: ``INPUT`` types whose value is always withheld.
SENSITIVE_INPUT_TYPES = frozenset({"password", "file", "hidden"})


def is_sensitive_control(tag: str, attrs: Mapping[str, str]) -> bool:
    """Whether a control's value must be withheld, by the rule above.

    Args:
        tag: The element's tag name, any case (CDP's ``nodeName`` is upper
            case for HTML elements, ``localName`` lower case).
        attrs: Its attributes by (lower-case) name — markup attributes, as
            ``DOM.getDocument`` / ``DOMSnapshot`` / ``DOM.describeNode``
            report them.
    """
    upper = tag.upper()
    if upper not in FORM_CONTROL_TAGS:
        return False
    input_type = str(attrs.get("type") or "").lower()
    autocomplete = str(attrs.get("autocomplete") or "").lower()
    if upper == "INPUT" and input_type in SENSITIVE_INPUT_TYPES:
        return True
    if autocomplete.startswith("cc-") or autocomplete == "one-time-code":
        return True
    # ``[attrs.id, attrs.name, autocomplete].join(' ')`` in the page script:
    # a missing attribute joins as the empty string.
    hint = " ".join((str(attrs.get("id") or ""), str(attrs.get("name") or ""), autocomplete))
    return SENSITIVE_HINT_PATTERN.search(hint) is not None


def attribute_map(flat: list[str] | tuple[str, ...]) -> dict[str, str]:
    """CDP's flat ``[name, value, name, value, …]`` attribute list as a mapping."""
    return {str(flat[i]).lower(): str(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}
