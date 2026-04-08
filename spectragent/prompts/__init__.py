"""Prompt templates and references.

``PROMPT_PATH`` points at this package's directory on disk so that
agents can locate ``.md`` / ``.txt`` template files at runtime.
"""

from pathlib import Path

PROMPT_PATH: Path = Path(__file__).resolve().parent
"""Root directory for all prompt templates."""
