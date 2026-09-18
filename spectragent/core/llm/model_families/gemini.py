from spectragent.core.llm.model_families._base import ModelFamily

GeminiFamily = ModelFamily(
    name="gemini",
    supports_json_schema=True,
    supports_tool_calling=True,
    supports_thinking=True,
    schema_requires_gemini_fix=True,
    schema_rejects_closed_union_branches=True,
    name_patterns=(
        "gemini-",
        "gemma-",
        "google/gemini-",
        "google/gemma-",
    ),
)
