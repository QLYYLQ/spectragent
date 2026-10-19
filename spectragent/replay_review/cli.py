"""Run independent replay reviews from the ordinary composed configuration."""

import argparse
import asyncio
import json
from pathlib import Path

import yaml
from dotenv import load_dotenv

from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.base import require_inference_protocol
from spectragent.core.registries import collect_registrars
from spectragent.replay_review.models import ReviewInputs
from spectragent.replay_review.runner import ReplayReviewRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--pairs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate composition and input shape without requests or writes",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    composed = ConfigComposer(registrars=collect_registrars()).compose(
        yaml.safe_load(args.config.read_text())
    )
    reviewer = composed.single("replay_reviewer")
    require_inference_protocol(
        reviewer.decision_model, "decision", consumer="replay_reviewer.decision_model"
    )
    inputs = ReviewInputs.model_validate_json(args.pairs.read_bytes())
    for pair in inputs.pairs:
        for key in ("source", "replay", "source_config"):
            value = Path(getattr(pair, key))
            if not value.is_absolute():
                setattr(pair, key, str((args.pairs.parent / value).resolve()))
    if args.dry_run:
        print(
            json.dumps(
                {"reviewer": reviewer.reviewer_id, "inputs": len(inputs.pairs), "dry_run": True}
            )
        )
        return 0
    result = asyncio.run(
        ReplayReviewRunner(reviewer).run(inputs.pairs, out=args.out, config=composed.top_level_cfg)
    )
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if not result["recording_problems"] and result["summary"]["assessed"] == len(inputs.pairs)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
