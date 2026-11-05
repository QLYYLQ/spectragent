"""A refusal written for the person running the command.

A component that cannot work with what it was given (a benchmark's data
missing or of another version, an installation missing a part) raises a
:class:`Refusal`. Its message names the problem and the fix (the command to
run, the setting to change) in a few lines, and never quotes a secret, so
every command shows it as it is: the Composer passes a constructor's
refusal through with its own message (``CompositionIssue`` code
``component_refused``), where it withholds what any other exception a
constructor raises says (``constructor_failed``).
"""

from __future__ import annotations


class Refusal(Exception):
    """A component refused what it was given; the message says what and how to fix it, and holds no secret."""


__all__ = ["Refusal"]
