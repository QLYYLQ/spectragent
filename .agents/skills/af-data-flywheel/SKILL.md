---
name: af-data-flywheel
description: Configure or extend the Spectragent data flywheel, which records browser environment evidence, replays it offline, branches recorded runs, exports SFT samples and measures the data line. Use for record, replay, branch, export or metrics YAML, and when adding or changing an archiver, network source, miss policy, replayability tier, restorer, step selector, action translator, exporter, or a student agent's `format_react_output`.
---

# Configure and Extend the Data Flywheel

A recording run keeps its environment evidence under `env/`; a replay serves that evidence instead of the network; a branch sweep restores a recorded run to step *t* and lets the teacher decide again; the export renders samples through the student agent itself; the metrics chain reports on all of it from committed traces. Treat `docs/architecture_design.md` §4.14–§4.16, §4.1 (`BranchLoop`), §4.5 (the archive axis), §4.10 (`trace_branches`), §5 and §6 as authoritative. If they conflict with this skill or the code, stop and surface the conflict before coding.

Read [references/guide.md](references/guide.md) by task:

- Any change: §1, for the pipeline, which layer owns what, and the layering rule.
- Writing or debugging a stage's YAML: §2 (record, replay, branch, export, measure).
- Adding or changing a strategy: §3.0, then the recipe for its axis (§3.1–§3.9).
- Before calling it done: §4, the invariants and traps.
- An error, a refusal or a surprising number: §5.

The mechanics this guide points to live in sibling skills: `af-add-a-receptor` (receptor shapes and the conscribe traps), `af-regenerate-and-verify` (proving a knob reached YAML), `af-add-a-capability` (the archive contract, `RestorationSupport`, `RestorationAlignment`), `af-add-a-loop` (`BranchLoop`, optional slots) and `af-trace-and-hooks` (the WAL, `env/`, privacy).

For recording/replay diagnosis, scoped URL/API rule proposals and baseline/candidate
validation, use [af-improve-replay-rules](../af-improve-replay-rules/SKILL.md).

To run an actual collection round (record teacher runs, branch them into same-task and
variant-task forks, build the review queues, label in the viewer), start from
`lookahead/README.md` "A collection round" and the shipped `configs/lookahead/` files; what the
earlier rounds measured is in `af-project-history/references/lookahead-rounds.md`.
