from spectragent.core.llm.model_families._base import ModelFamily

ClaudeFamily = ModelFamily(
    name="claude",
    supports_json_schema=False,
    supports_tool_calling=True,
    prefers_tool_for_structured=True,
    supports_strict_mode=False,
    supports_thinking=True,
    supports_cache_control=True,
    name_patterns=(
        "claude-",
        "anthropic.claude-",
        "anthropic/claude-",
    ),
)
