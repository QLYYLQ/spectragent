from spectragent.core.llm.model_families._base import ModelFamily

QwenFamily = ModelFamily(
    name="qwen",
    supports_json_schema=True,
    name_patterns=(
        "qwen",
        "alibaba/qwen",
    ),
)
