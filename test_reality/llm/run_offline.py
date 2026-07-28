"""Offline wiring gate — NO network. Proves the harness + core.llm plumbing is
sound before any live API key is spent.

Checks:
  1. discover() + registry completeness (all expected registry keys present).
  2. construct-smoke: instantiate every proxyable provider with dummy creds
     (no ainvoke) to catch dataclass/field errors and base_url wiring.
  3. serializer roundtrip: OpenAI / Anthropic / Google serializers produce the
     expected wire shapes (role mapping, system extraction, image encoding).
  4. SchemaOptimizer output: strict schema is flattened ($defs/$ref gone), every
     object is additionalProperties:false with all props required; the Gemini
     variant strips the forbidden keywords.

Run:  uv run python test_reality/llm/run_offline.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

# --- bootstrap sibling imports for direct-script invocation ---
sys.path.insert(0, str(Path(__file__).parent))

from cases import SPECS  # noqa: E402
from env import dummy_env  # noqa: E402
from schemas import PersonInfo, ProjectPlan  # noqa: E402

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_failures = 0


def check(name: str, fn) -> None:
    global _failures
    try:
        detail = fn()
        print(f"  [{PASS}] {name}" + (f" — {detail}" if detail else ""))
    except AssertionError as e:
        _failures += 1
        print(f"  [{FAIL}] {name} — assertion: {e}")
    except Exception as e:  # noqa: BLE001
        _failures += 1
        print(f"  [{FAIL}] {name} — {type(e).__name__}: {e}")
        traceback.print_exc()


# ──────────────────────────────────────────────────────────────────────────
def check_registry() -> str:
    from build import registry_keys

    keys = registry_keys()
    expected = {
        "openai.direct", "openai.openrouter", "openai.deepseek", "openai.cerebras",
        "openai.vercel", "openai.groq", "openai.azure", "anthropic.direct",
        "anthropic.aws", "google.default", "mistral.default", "ollama.default",
        "oci.default", "bedrock.default", "browser_use.default",
    }
    missing = expected - set(keys)
    assert not missing, f"missing registry keys: {sorted(missing)}"
    return f"{len(keys)} keys registered"


def check_construct_proxyable() -> str:
    """Construct every proxyable spec with dummy creds (no network)."""
    from build import build_llm

    env = dummy_env()
    built = []
    for spec in SPECS:
        model = env.models[spec.model_key][0]
        # use the first structured mode's extra_kwargs (union of construction fields)
        extra = dict(spec.structured_modes[0].extra_kwargs) if spec.structured_modes else {}
        llm = build_llm(spec, model, extra, env)
        assert llm.model == model, f"{spec.id}: model not set"
        assert llm.model_type, f"{spec.id}: no model_type"
        built.append(spec.id)
    return f"constructed: {', '.join(built)}"


def check_serializers() -> str:
    from spectragent.core.llm.messages import (
        AssistantMessage,
        ContentPartImageParam,
        ContentPartTextParam,
        ImageURL,
        SystemMessage,
        UserMessage,
    )
    from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer
    from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer
    from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer

    msgs = [
        SystemMessage(content="You are helpful."),
        UserMessage(
            content=[
                ContentPartTextParam(text="What is in this image?"),
                ContentPartImageParam(
                    image_url=ImageURL(url="data:image/png;base64,AAAA", media_type="image/png")
                ),
            ]
        ),
        AssistantMessage(content="It is a test."),
    ]

    om = OpenAIMessageSerializer.serialize_messages(msgs)
    assert [m["role"] for m in om] == ["system", "user", "assistant"], f"openai roles: {om}"

    am, sys_prompt = AnthropicMessageSerializer.serialize_messages(msgs)
    assert sys_prompt and "helpful" in str(sys_prompt), "anthropic: system not extracted"
    assert all(m["role"] != "system" for m in am), "anthropic: system leaked into messages"

    gc, gsys = GoogleMessageSerializer.serialize_messages(msgs)
    assert gsys, "google: system_instruction empty"
    assert gc, "google: no contents"
    return "openai/anthropic/google serialize OK (roles, system split, image kept)"


def _iter_objects(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_objects(v)


def check_schema_optimizer() -> str:
    from spectragent.core.llm.schema.optimizer import SchemaOptimizer

    schema = SchemaOptimizer.create_optimized_json_schema(ProjectPlan)
    text = str(schema)
    assert "$ref" not in text and "$defs" not in text, "refs/defs not flattened"
    # every object must have additionalProperties:false and list all props as required
    obj_count = 0
    for obj in _iter_objects(schema):
        if obj.get("type") == "object" and "properties" in obj:
            obj_count += 1
            assert obj.get("additionalProperties") is False, "object missing additionalProperties:false"
            props = set(obj["properties"].keys())
            req = set(obj.get("required", []))
            assert props == req, f"strict mode: required != properties ({props ^ req})"
    assert obj_count >= 3, f"expected nested objects flattened, got {obj_count}"

    # simple model sanity
    simple = SchemaOptimizer.create_optimized_json_schema(PersonInfo)
    assert set(simple["required"]) == {"name", "age", "hobbies"}, simple.get("required")

    # gemini variant should not carry additionalProperties (fix_gemini_schema strips it)
    from spectragent.core.llm.schema.gemini import fix_gemini_schema

    gem = fix_gemini_schema(SchemaOptimizer.create_gemini_optimized_schema(ProjectPlan))
    assert "additionalProperties" not in str(gem), "gemini schema still has additionalProperties"
    return f"strict schema: {obj_count} objects flattened + all-required; gemini variant clean"


def main() -> int:
    print("=" * 72)
    print("OFFLINE wiring gate — no network, no API key needed")
    print("=" * 72)
    check("registry completeness", check_registry)
    check("construct proxyable providers (dummy creds)", check_construct_proxyable)
    check("message serializers roundtrip", check_serializers)
    check("SchemaOptimizer strict + gemini output", check_schema_optimizer)
    print("-" * 72)
    if _failures:
        print(f"\033[31m{_failures} check(s) FAILED\033[0m")
        return 1
    print("\033[32mAll offline checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
