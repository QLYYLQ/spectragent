from spectragent.core.llm.model_families._base import ModelFamily

GrokFamily = ModelFamily(
    name="grok",
    supports_json_schema=True,
    supports_tool_calling=True,
    name_patterns=(
        "grok-",
        "xai/grok",
    ),
)
