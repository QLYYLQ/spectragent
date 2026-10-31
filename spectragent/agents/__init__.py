"""Agent layer — pluggable agent implementations via conscribe.

Every subpackage is one agent family (``browser_use``, ``skyvern``,
``ui_tars``), except the framework's own ``memory`` and ``rendering``. A run
imports only the families its configuration names
(``docs/architecture_design.md`` §4.2.1), and importing any module of this
package runs this file first, so it imports no family. Import a family's
classes from the family's own package.
"""

from spectragent.agents._registrar import AgentBase, AgentRegistrar
from spectragent.agents.base import BaseAgent

__all__ = ["AgentBase", "AgentRegistrar", "BaseAgent"]
