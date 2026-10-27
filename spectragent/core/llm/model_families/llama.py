from spectragent.core.llm.model_families._base import ModelFamily

LlamaFamily = ModelFamily(
    name="llama",
    supports_json_schema=True,
    name_patterns=(
        "llama",
        "meta-llama/",
        "meta/llama",
    ),
)
