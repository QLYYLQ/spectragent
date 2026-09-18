"""One answer to "the model wrapped its JSON in a Markdown fence".

A model told to answer with JSON writes a fence about as often as it does
not, and the reply is then *almost* right: valid JSON, three backticks and
a language tag away from parsing. Six providers had each noticed this
independently and written their own unwrapper —
``anthropic_base``, ``google`` (twice), ``oci``, ``openai_vercel`` and
``_groq_parser`` — with four different behaviours between them, while
``openai_base``, the base every OpenAI-compatible gateway goes through,
had none at all.

That split is not cosmetic. Which of a fenced reply and a bare one parses
would otherwise depend on which provider key the YAML happens to name,
and the gateway route — the one most likely to be talking to a model
whose own API would have constrained the output — was the one with no
coverage.

**What this does not fix.** A gateway that drops ``response_format`` on
the way to a vendor whose native API it is translating returns a reply
that is fenced *and* does not match the schema — measured on
``claude-haiku-4-5`` through an OpenAI-compatible route, 0/3, where the
keys inside the fence were not the schema's keys. Unwrapping gets you to
"valid JSON of the wrong shape". The fix for that is to stop translating:
route Claude at the Anthropic endpoint and Gemini at the Google one.
"""

import re

__all__ = ["strip_code_fence"]

#: The first fenced block: an opening ``` with an optional language tag,
#: then everything up to the closing fence or the end of the text.
#:
#: An unterminated fence still unwraps. A reply cut off at the token
#: ceiling is the usual way to get one, and its content is unparseable
#: either way — but ``{"a": 1`` is a JSON error the caller can report,
#: while "```json\n{\"a\": 1" is a mystery about backticks.
_FENCE = re.compile(
    r"```[ \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n?(?P<body>.*?)(?:```|\Z)",
    re.DOTALL,
)


def strip_code_fence(text: str) -> str:
    """Return *text* with a surrounding Markdown code fence removed.

    Handles the three shapes providers actually see:

    * ``"```json\\n{...}\\n```"`` — fence with a language tag;
    * ``"```\\n{...}\\n```"`` — fence without one;
    * ``"Here you go:\\n```json\\n{...}\\n```"`` — a preamble before the
      fence, which is the shape the Groq salvage path was written for.

    A reply that already starts with ``{`` or ``[`` is returned unchanged
    even when it contains backticks, because those backticks are inside a
    string value. Unwrapping there would take a correct reply apart —
    ``{"memory": "I clicked ```Run```"}`` is a real thing a browser agent
    writes about a page it just read.

    Whitespace is stripped either way, so every caller can hand the result
    straight to a JSON parser.
    """
    stripped = text.strip()
    if not stripped.startswith("```") and (
        stripped.startswith(("{", "[")) or "```" not in stripped
    ):
        return stripped
    match = _FENCE.search(stripped)
    if match is None:  # pragma: no cover — the pattern matches any "```"
        return stripped
    return match.group("body").strip()
