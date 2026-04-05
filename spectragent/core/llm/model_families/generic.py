from spectragent.core.llm.model_families._base import ModelFamily

GenericFamily = ModelFamily(
    name="generic",
    supports_json_schema=True,
    supports_tool_calling=True,
)
