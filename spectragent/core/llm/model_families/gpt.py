from spectragent.core.llm.model_families._base import ModelFamily

GPTFamily = ModelFamily(
    name="gpt",
    supports_json_schema=True,
    supports_tool_calling=True,
    supports_strict_mode=True,
    supports_reasoning_effort=True,
    name_patterns=(
        "gpt-",
        "o1",
        "o3",
        "o4",
        "openai/gpt-",
        "openai/o1",
        "openai/o3",
        "openai/o4",
    ),
)
