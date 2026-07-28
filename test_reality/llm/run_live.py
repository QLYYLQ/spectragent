"""Live runner — points each selected chat template at the real proxy and runs
plain text chat + structured output (output_format=<PydanticModel>).

Credentials come from the environment (see env.py / README.md). Nothing is
hard-coded. Each phase is wrapped in a timeout and full try/except so one broken
provider never aborts the run.

Examples:
    uv run python test_reality/llm/run_live.py                 # core: openai, anthropic, google (+openrouter if kind)
    uv run python test_reality/llm/run_live.py --extended      # + deepseek/cerebras/vercel/mistral/google-http
    uv run python test_reality/llm/run_live.py --only openai,google
    uv run python test_reality/llm/run_live.py --no-structured # text chat only
    uv run python test_reality/llm/run_live.py --list          # show the matrix and exit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build import build_llm  # noqa: E402
from cases import ProviderSpec, StructuredMode, select_specs  # noqa: E402
from env import HarnessEnv, load_env  # noqa: E402
from report import Result, dump_json, print_summary, print_table  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from spectragent.core.llm.messages import SystemMessage, UserMessage  # noqa: E402

TEXT_PROMPT = "Say hello in one short sentence."


def _usage_tuple(result) -> tuple | None:
    u = getattr(result, "usage", None)
    if u is None:
        return None
    return (u.prompt_tokens, u.completion_tokens, u.total_tokens)


async def run_text(spec: ProviderSpec, model: str, env: HarnessEnv) -> Result:
    r = Result(provider=spec.id, registry_key=spec.registry_key, model=model, phase="text", status="FAIL")
    try:
        llm = build_llm(spec, model, {}, env)
        t0 = time.perf_counter()
        result = await asyncio.wait_for(
            llm.ainvoke([UserMessage(content=TEXT_PROMPT)]), timeout=env.timeout_s
        )
        r.latency_ms = (time.perf_counter() - t0) * 1000
        completion = result.completion
        if isinstance(completion, str) and completion.strip():
            r.status = "PASS"
            r.preview = completion
        else:
            r.status = "FAIL"
            r.error = f"empty/non-str completion: {type(completion).__name__}"
        r.usage = _usage_tuple(result)
        r.stop_reason = result.stop_reason
    except asyncio.TimeoutError:
        r.error = f"timeout after {env.timeout_s}s"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


async def run_structured(spec: ProviderSpec, model: str, mode: StructuredMode, env: HarnessEnv) -> Result:
    r = Result(
        provider=spec.id,
        registry_key=spec.registry_key,
        model=model,
        phase=f"structured:{mode.label}",
        status="FAIL",
    )
    try:
        llm = build_llm(spec, model, mode.extra_kwargs, env)
        messages = []
        if mode.inject_system:
            messages.append(
                SystemMessage(content="You are a precise data-extraction assistant.")
            )
        messages.append(UserMessage(content=mode.prompt))

        t0 = time.perf_counter()
        result = await asyncio.wait_for(
            llm.ainvoke(messages, output_format=mode.output_model), timeout=env.timeout_s
        )
        r.latency_ms = (time.perf_counter() - t0) * 1000
        completion = result.completion
        if isinstance(completion, mode.output_model):
            r.status = "PASS"
            r.preview = completion.model_dump_json()
        elif isinstance(completion, BaseModel):
            r.status = "FAIL"
            r.error = f"wrong model type: got {type(completion).__name__}"
            r.preview = completion.model_dump_json()
        else:
            r.status = "FAIL"
            r.error = f"not a model: {type(completion).__name__}"
            r.preview = str(completion)
        r.usage = _usage_tuple(result)
        r.stop_reason = result.stop_reason
    except asyncio.TimeoutError:
        r.error = f"timeout after {env.timeout_s}s"
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    return r


def print_matrix(specs: list[ProviderSpec], env: HarnessEnv) -> None:
    print("  Matrix (kind = %s):" % env.kind)
    for spec in specs:
        enabled = spec.enabled_for(env.kind)
        tag = "" if enabled else "  [SKIP: not on %s]" % env.kind
        models = ", ".join(env.models[spec.model_key])
        modes = ", ".join(m.label for m in spec.structured_modes) or "-"
        style = spec.base_url_style
        print(f"    • {spec.id:12s} {spec.registry_key:20s} style={style:20s} models=[{models}]{tag}")
        print(f"        structured: {modes}")
        if spec.note:
            print(f"        note: {spec.note}")


async def run_all(specs: list[ProviderSpec], env: HarnessEnv, do_text: bool, do_structured: bool) -> list[Result]:
    results: list[Result] = []
    for spec in specs:
        if not spec.enabled_for(env.kind):
            results.append(
                Result(
                    provider=spec.id,
                    registry_key=spec.registry_key,
                    model="-",
                    phase="all",
                    status="SKIP",
                    error=f"not available on kind={env.kind}",
                )
            )
            continue
        for model in env.models[spec.model_key]:
            print(f"\n  ── {spec.id} / {model} ──")
            if do_text:
                res = await run_text(spec, model, env)
                _emit(res)
                results.append(res)
            if do_structured:
                for mode in spec.structured_modes:
                    res = await run_structured(spec, model, mode, env)
                    _emit(res)
                    results.append(res)
    return results


def _emit(r: Result) -> None:
    from report import _c

    ms = f"{r.latency_ms:.0f}ms" if r.latency_ms is not None else "-"
    detail = r.error if r.status == "FAIL" else (r.preview or "")
    print(f"    {_c(r.status, r.status):>4}  {r.phase:24s} {ms:>8}  {detail[:70]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Live LLM chat-template reality check")
    ap.add_argument("--only", type=str, default="", help="comma-separated spec ids (openai,anthropic,google,...)")
    ap.add_argument("--extended", action="store_true", help="include extended-tier providers")
    ap.add_argument("--no-text", action="store_true", help="skip plain text chat phase")
    ap.add_argument("--no-structured", action="store_true", help="skip structured-output phase")
    ap.add_argument("--list", action="store_true", help="print the matrix and exit (no calls)")
    ap.add_argument("--timeout", type=float, default=None, help="override per-call timeout seconds")
    args = ap.parse_args()

    env = load_env()
    if args.timeout is not None:
        env.timeout_s = args.timeout

    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    specs = select_specs(only, args.extended)

    print("=" * 72)
    print("LIVE chat-template reality check — spectragent.core.llm")
    print("=" * 72)
    print(f"  proxy host : {env.host or '<unset>'}")
    print(f"  kind       : {env.kind}")
    print(f"  api key    : {env.masked_key}")
    print(f"  openai base: {env.openai_base_url}")
    print(f"  native base: {env.native_base_url}")
    print(f"  timeout    : {env.timeout_s}s")
    print("-" * 72)
    print_matrix(specs, env)
    print("-" * 72)

    if args.list:
        return 0

    if not env.has_creds:
        print(
            "\033[31mMissing credentials.\033[0m Set PROXY_BASE_URL + PROXY_API_KEY "
            "(or rely on OPENAI_BASE_URL + OPENAI_API_KEY in .env)."
        )
        return 2

    results = asyncio.run(
        run_all(specs, env, do_text=not args.no_text, do_structured=not args.no_structured)
    )

    print("\n" + "=" * 72)
    print("RESULTS")
    print("=" * 72)
    print_table(results)
    print_summary(results)

    meta = {
        "host": env.host,
        "kind": env.kind,
        "openai_base_url": env.openai_base_url,
        "native_base_url": env.native_base_url,
        "timeout_s": env.timeout_s,
        "models": {s.model_key: env.models[s.model_key] for s in specs},
        "only": only,
        "extended": args.extended,
    }
    path = dump_json(results, meta)
    print(f"\n  JSON written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
