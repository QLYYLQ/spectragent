from spectragent.core.llm.model_families._base import ModelFamily

DeepSeekFamily = ModelFamily(
    name="deepseek",
    supports_json_schema=True,
    supports_tool_calling=True,
    name_patterns=(
        "deepseek-",
        "deepseek/",
    ),
)
