from spectragent.core.llm.model_families._base import ModelFamily

MistralFamily = ModelFamily(
    name="mistral",
    supports_json_schema=True,
    name_patterns=(
        "mistral-",
        "codestral",
        "pixtral",
        "ministral",
        "magistral",
        "mixtral",
        "mistral/",
    ),
)
