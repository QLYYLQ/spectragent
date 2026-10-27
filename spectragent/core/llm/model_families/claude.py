from spectragent.core.llm.model_families._base import ModelFamily

ClaudeFamily = ModelFamily(
    name="claude",
    supports_json_schema=False,
    supports_strict_mode=False,
    name_patterns=(
        "claude-",
        "anthropic.claude-",
        "anthropic/claude-",
    ),
)
