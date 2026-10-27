from spectragent.core.llm.model_families._base import ModelFamily

CohereFamily = ModelFamily(
    name="cohere",
    supports_json_schema=True,
    name_patterns=(
        "command-",
        "cohere/",
    ),
)
