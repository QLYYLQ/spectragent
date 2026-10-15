import json
import logging
import re
from typing import TypeVar

from groq import APIStatusError
from pydantic import BaseModel

from spectragent.core.llm.fences import strip_code_fence

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ParseFailedGenerationError(Exception):
    pass


def try_parse_groq_failed_generation(
    error: APIStatusError,
    output_format: type[T],
) -> T:
    """Extract JSON from model output, handling both plain JSON and code-block-wrapped JSON.

    The one structured path in ``providers/`` that deliberately does **not**
    go through ``ChatModelBase.validate_structured``: it is a salvage
    attempt inside ``except GroqStatusError``, where the request has
    already failed. Its caller discards any exception it raises and lets
    the original status error stand, so a recoverable-error type raised
    here could never reach the Loop — the failure is the provider's, not
    the model's. ``tests/test_core/test_llm/test_provider_validation.py``
    names this exemption so the scan does not have to guess at it.
    """
    try:
        content = error.body["error"]["failed_generation"]  # type: ignore

        # If content is wrapped in code blocks, extract just the JSON part.
        # The unwrapping itself is the shared one — this path is exempt from
        # ``validate_structured``, not from agreeing with every other
        # provider about what a fence is.
        content = strip_code_fence(content)

        # remove html-like tags before the first { and after the last }
        # This handles cases like <|header_start|>assistant<|header_end|> and <function=AgentOutput>
        # Only remove content before { if content doesn't already start with {
        if not content.strip().startswith("{"):
            content = re.sub(r"^.*?(?=\{)", "", content, flags=re.DOTALL)

        # Remove common HTML-like tags and patterns at the end, but be more conservative
        # Look for patterns like </function>, <|header_start|>, etc. after the JSON
        content = re.sub(r"\}(\s*<[^>]*>.*?$)", "}", content, flags=re.DOTALL)
        content = re.sub(r"\}(\s*<\|[^|]*\|>.*?$)", "}", content, flags=re.DOTALL)

        # Handle extra characters after the JSON, including stray braces
        # Find the position of the last } that would close the main JSON object
        content = content.strip()

        if content.endswith("}"):
            # Try to parse and see if we get valid JSON
            try:
                json.loads(content)
            except json.JSONDecodeError:
                # If parsing fails, try to find the correct end of the JSON
                # by counting braces and removing anything after the balanced JSON
                brace_count = 0
                last_valid_pos = -1
                for i, char in enumerate(content):
                    if char == "{":
                        brace_count += 1
                    elif char == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            last_valid_pos = i + 1
                            break

                if last_valid_pos > 0:
                    content = content[:last_valid_pos]

        # Fix control characters in JSON strings before parsing
        # This handles cases where literal control characters appear in JSON values
        content = _fix_control_characters_in_json(content)

        # Parse the cleaned content
        result_dict = json.loads(content)

        # some models occasionally respond with a list containing one dict: https://github.com/browser-use/browser-use/issues/1458
        if (
            isinstance(result_dict, list)
            and len(result_dict) == 1
            and isinstance(result_dict[0], dict)
        ):
            result_dict = result_dict[0]

        logger.debug(f"Successfully parsed model output: {result_dict}")
        return output_format.model_validate(result_dict)

    except KeyError as e:
        raise ParseFailedGenerationError(e) from e

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse model output: {content} {str(e)}")
        raise ValueError(f"Could not parse response. {str(e)}") from e

    except Exception as e:
        raise ParseFailedGenerationError(error.response.text) from e


def _fix_control_characters_in_json(content: str) -> str:
    """Fix control characters in JSON string values to make them valid JSON."""
    try:
        # First try to parse as-is to see if it's already valid
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass

    # More sophisticated approach: only escape control characters inside string values
    # while preserving JSON structure formatting

    result = []
    i = 0
    in_string = False
    escaped = False

    while i < len(content):
        char = content[i]

        if not in_string:
            # Outside of string - check if we're entering a string
            if char == '"':
                in_string = True
            result.append(char)
        else:
            # Inside string - handle escaping and control characters
            if escaped:
                # Previous character was backslash, so this character is escaped
                result.append(char)
                escaped = False
            elif char == "\\":
                # This is an escape character
                result.append(char)
                escaped = True
            elif char == '"':
                # End of string
                result.append(char)
                in_string = False
            elif char == "\n":
                # Literal newline inside string - escape it
                result.append("\\n")
            elif char == "\r":
                # Literal carriage return inside string - escape it
                result.append("\\r")
            elif char == "\t":
                # Literal tab inside string - escape it
                result.append("\\t")
            elif char == "\b":
                # Literal backspace inside string - escape it
                result.append("\\b")
            elif char == "\f":
                # Literal form feed inside string - escape it
                result.append("\\f")
            elif ord(char) < 32:
                # Other control characters inside string - convert to unicode escape
                result.append(f"\\u{ord(char):04x}")
            else:
                # Normal character inside string
                result.append(char)

        i += 1

    return "".join(result)
