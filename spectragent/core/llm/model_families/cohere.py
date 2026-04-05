from spectragent.core.llm.model_families._base import ModelFamily

CohereFamily = ModelFamily(
    name="cohere",
    supports_json_schema=True,
    supports_tool_calling=True,
    name_patterns=(
        "command-",
        "cohere/",
    ),
)
