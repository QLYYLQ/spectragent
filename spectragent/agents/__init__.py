"""Agent layer — pluggable agent implementations via conscribe."""

from spectragent.agents._registrar import AgentBase, AgentRegistrar
from spectragent.agents.base import BaseAgent
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.env_interfaces import ActionHandler, Environment
from spectragent.agents.react import BaseReactAgent
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent

__all__ = [
    # Protocols
    "BaseAgent",
    "Environment",
    "ActionHandler",
    # Registry
    "AgentRegistrar",
    "AgentBase",
    # Abstract
    "BaseReactAgent",
    # Concrete agents
    "BrowserUseAgent",
    "UITarsAgent",
    "SkyvernAgent",
]
