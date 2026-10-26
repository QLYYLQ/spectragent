from spectragent.core.llm.model_families._base import ModelFamily

DeepSeekFamily = ModelFamily(
    name="deepseek",
    # DeepSeek's own API has no json_schema response format. Measured
    # 2026-10-26 against https://api.deepseek.com (deepseek-flash,
    # deepseek-v4-pro; strict or not, /v1 or /beta, thinking on or off):
    # HTTP 400 "This response_format type is unavailable now". Only "text"
    # and "json_object" exist, so the schema goes into the prompt
    # (``OpenAICompatBase.takes_schema_in_prompt``).
    supports_json_schema=False,
    supports_tool_calling=True,
    name_patterns=(
        "deepseek-",
        "deepseek/",
    ),
)
