"""Agent layer — pluggable agent implementations via conscribe."""

from spectragent.agents._registrar import AgentBase, AgentRegistrar
from spectragent.agents.base import BaseAgent
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.skyvern import SkyvernAgent, SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.agents.ui_tars import UITarsAgent

__all__ = [
    # Protocols
    "BaseAgent",
    # Registry
    "AgentRegistrar",
    "AgentBase",
    # Concrete agents
    "BrowserUseAgent",
    "UITarsAgent",
    "SkyvernAgent",
    "SkyvernDomAgent",
    "SkyvernDomVisionAgent",
]
