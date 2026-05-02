from typing import Literal

MessageProtocol = Literal["user_assistant", "tool_call", "planning", "code_execution"]

ALL_PROTOCOLS: frozenset[MessageProtocol] = frozenset(
    ("user_assistant", "tool_call", "planning", "code_execution")
)
