"""test_reality/llm — standalone reality-check harness for spectragent.core.llm.

NOT part of the pytest suite (lives outside tests/). Exercises each registered
chat template (provider) against a real third-party proxy (dmxapi / openrouter):
plain text chat + structured output (output_format=<PydanticModel>).

Entry points:
    uv run python test_reality/llm/run_offline.py      # no network — wiring gate
    uv run python test_reality/llm/run_live.py          # live proxy calls
"""
