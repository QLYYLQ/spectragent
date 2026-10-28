# lookahead — offline data for the lookahead-observation model

The lookahead-observation model reads the task, the history, the current screen
O_t and the actions A_t just issued, and predicts where on the next screen
O_{t+1} the information needed for the next decision will appear. This package
builds its data offline from recorded browser runs. It lives beside
`spectragent`, is not part of its wheel, and reads runs only through
spectragent's public trace reader (`RunTrace`), the archive reader and the
browser package's readers. It never writes inside a run.

```bash
uv run python -m lookahead <command> --help
```

## A collection round

Use `configs/lookahead/` for the next collection round. The adopted settings are reconciled
one by one in [配置指南 101](../docs/data_flywheel_runbook.md#配置指南-101实测推荐), including the
candidate values, evidence, costs and limits. `configs/flywheel/` remains a separate set of
generic examples. The templates are not frozen copies of the 2026-10-25 experiment.

| File | Adopted role |
|---|---|
| `record_ui_tars_deepseek.yaml` | UI-TARS computer-use teacher, screenshot-pixel coordinates; m17 on the requested private route through the DeepSeek adapter; 30 steps, 4 actions, one parse retry, 200-message memory, 32768 output ceiling |
| `branch_same_task.yaml` | every fourth policy step, offset 1; 2 forks per checkpoint; source history; one new decision per fork |
| `branch_task_variants.yaml` | checkpoints and tasks from `variants`; 1 fork per variant; restored history |
| `branch_task_variants_without_source_thoughts.yaml` | the same variants and budget, with `omit_source_replies` as a separate control |
| `judge_deepseek.yaml` | independent official DeepSeek judge, `deepseek-flash`, high reasoning, 16384 output ceiling; use its own API key |

The teacher's `base_url: null` is an unfilled template. Resolve it from the already supplied
private route into all four **round copies** before checking or running them. The DeepSeek
adapter sends the configured ceiling as `max_tokens`, the parameter used by the m17 probes;
this adapter/route/budget combination has not completed a live collection round. The judge's
explicit official URL is independent of the teacher's endpoint. Credentials stay in each
command's environment. Plain `config check` does not prove that a private endpoint was
selected or that its model behaves correctly.

All four agent configs use the same learned export and seed 0, cursor on, `Asia/Shanghai`,
`zh-CN`, 1280×720 window and viewport, PNG, shared pinned clock, frozen background tabs and
followed action-opened tabs. The export needs `generator.npz` and `meta.json` at
`~/code/mouse-gan-torch/runs/v2_mdn_h128_d3/export`; change all four paths together on another
machine. Freeze their contents for the round. Important browser/archive defaults are explicit
in the YAML; the runbook also identifies unchanged and inactive defaults.

Forks use strict isolated archive serving, exact/normalized matching, the frozen site rules,
`verify: warn`, `media: align`, `pace: recorded_timeline`, and one terminal observation.
Timeline recovered 266/466 checkpoints versus 243/466 with none in the paired ten-site round,
at about 3.34 times the summed execution duration. Those are checkpoint checks, not task
success or wall-clock cost; there was no paired latency arm. The default keeps timeline and
does not repeat the none arm in every collection. A dedicated comparison uses a new output
and the same source/task list.

Video remains visible and recorded. Media-area rules only change declared pages' restoration
comparison; they neither block live video requests nor black out saved/model-visible images.
Above 75% mask coverage, pixel comparison uses the full image. Explicitly unloaded checkpoints
are skipped; DOM-only settle caps are not equivalent to unloaded pages. See the runbook's
video policy for alignment, masking and incomplete evidence.

Prepare a new round from the repository root. Set `LOOKAHEAD_JUDGE_API_KEY` to the official
judge credential in your shell; the sourced `OPENAI_API_KEY` and `OPENAI_BASE_URL` belong to
the teacher. The preparation below writes the private route only under the ignored round
folder, copies the current rules, and refuses an existing config folder. Prepare a private
LexBench `All` task directory first, including `data_info.json` and the intended per-task
`setup.storage_state`; set `LOOKAHEAD_TASK_DATA` to that frozen directory. Use local seed
paths whose contents stay fixed for the round. The script explicitly binds that dataset
to the recording config, rather than silently falling back to the installed anonymous data.

```bash
uv sync --extra dev --extra learned-mouse
set -a; . ./.env; set +a
: "${OPENAI_BASE_URL:?Set the teacher endpoint before preparing the round}"
: "${OPENAI_API_KEY:?Set the teacher credential}"
: "${LOOKAHEAD_JUDGE_API_KEY:?Set the independent official judge credential}"
ROUND_DIR=tmp/lookahead/round-20261028  # choose a new directory for each round
: "${LOOKAHEAD_TASK_DATA:?Set the prepared private LexBench task directory}"
export OPENAI_API_KEY OPENAI_BASE_URL LOOKAHEAD_TASK_DATA
mkdir -p "$ROUND_DIR/logs" "$ROUND_DIR/sessions"
uv run python - "$ROUND_DIR" <<'PY'
import os
import shutil
import sys
from pathlib import Path
import yaml

round_dir = Path(sys.argv[1])
task_data = Path(os.environ["LOOKAHEAD_TASK_DATA"]).expanduser().resolve()
if not (task_data / "data_info.json").is_file():
    raise SystemExit("LOOKAHEAD_TASK_DATA must contain data_info.json")
config_dir = round_dir / "configs"
config_dir.mkdir(exist_ok=False)
shutil.copytree("configs/replay_rules", config_dir / "replay_rules")

def paths(value):
    if isinstance(value, dict):
        return {key: paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [paths(item) for item in value]
    if isinstance(value, str):
        return value.replace("tmp/lookahead/round", str(round_dir))
    return value

for source in Path("configs/lookahead").glob("*.yaml"):
    data = paths(yaml.safe_load(source.read_text()))
    if source.name == "record_ui_tars_deepseek.yaml":
        data["task_loader"] = [{"benchmark_name": "lexbench_browser", "subset": "All",
                                "data_dir": str(task_data)}]
    for agent in data.get("agent", []):
        agent["llm"]["base_url"] = os.environ["OPENAI_BASE_URL"]
    for environment in data.get("environment", []):
        for capability in environment["capabilities"]:
            network = capability["network_source"]
            if network["source_id"] == "archive":
                network["matcher"]["site_rules"] = str(config_dir / "replay_rules")
    (config_dir / source.name).write_text(yaml.safe_dump(data, sort_keys=False))
PY
uv run spectragent config check "$ROUND_DIR/configs/record_ui_tars_deepseek.yaml"
```

Before starting, keep the machine's appearance, system language and Chrome version fixed;
record their actual values and the source commit, rules and learned-file digests. The 10/25
source browser was Chrome 143, pinned UTC/en-US with host-default zh-CN, cursor off and a
1280×720 window. These facts differ from the next-round choices. Do not apply the new
browser identity to an old archive; derive an old-run branch configuration from that source's
own recorded settings. Existing v7 history, frames and thinking remain reviewable without
re-recording.

For the same ten-site task set, preserve the explicit eight seeded sites and two fresh sites
(Tonghuashun and Steam). `All` describes the public task dataset, not an anonymous browser.
The preparation script binds `LOOKAHEAD_TASK_DATA` as the record copy's explicit
`lexbench_browser / All` task loader. Keep this private dataset and seed contents fixed;
its directory contains `data_info.json`. Check the visible
account in a pilot. For an anonymous experiment use a separate round and record that change.
If using the installed public task data without seeds, pass `--benchmark lexbench_browser
--subset All` explicitly; that is a different task setup from the authenticated ten-site run.

For the pilot, record one task, run health and create its reviewed exclusion file, then
use `branch_same_task --list` and run one selected branch task ID. Keep an unsuccessful
pilot in its own output. If the pilot uses the frozen formal configuration and is retained
in the formal source set, do not record that task again unnoticed; after expanding the
source set, rerun health and freeze a new reviewed list before variants or bulk forks.

After this small live/offline pilot checks the chosen route, coordinates, archive completeness
and restoration, run the collection. Below each bench command is one worker: split its saved
`--list` into disjoint `--task-id` subsets for up to eight offline workers per arm, with a
separate session/log per worker and arms run in sequence. Live recording remains at most
three site sweeps, sequential within each site. Preserve failed/incomplete attempts and
rerun those tasks separately; inspect recording health before consuming them.

```bash
# 1. Record from the round's explicit task-loader configuration; example Bilibili tasks.
uv run spectragent bench --config "$ROUND_DIR/configs/record_ui_tars_deepseek.yaml" \
  --task-id 6 --task-id 118 --task-id 158 --no-eval --timeout 1800 \
  --session-dir "$ROUND_DIR/sessions/bilibili" > "$ROUND_DIR/logs/record-bilibili.log" 2>&1
uv run python -m lookahead health --root "$ROUND_DIR/record" --viewer-root "$ROUND_DIR" \
  --out "$ROUND_DIR/out/health"

# Review the report, failed endpoints and screenshots. Save a separate run-exclusions-1
# reviewed-exclusions.json plus review notes; preserve the generated exclusions.json.
# An empty reviewed result is {"format": "run-exclusions-1", "runs": []}.
EXCLUDE="$ROUND_DIR/out/health/reviewed-exclusions.json"
test -f "$EXCLUDE" || { echo "Review the raw health exclusions first"; exit 2; }

# 2. Generate one shared variant file with the official judge's credential.
OPENAI_API_KEY="$LOOKAHEAD_JUDGE_API_KEY" uv run python -m lookahead variants \
  --root "$ROUND_DIR/record" --viewer-root "$ROUND_DIR" --exclude-runs "$EXCLUDE" \
  --steps-per-run 2 --per-step 2 --judge-config "$ROUND_DIR/configs/judge_deepseek.yaml" \
  --out "$ROUND_DIR/variants/variants.json"
for arm in branch_same_task branch_task_variants branch_task_variants_without_source_thoughts; do
  uv run spectragent bench --config "$ROUND_DIR/configs/$arm.yaml" --no-eval --list \
    > "$ROUND_DIR/logs/$arm-list.txt"
done
cmp "$ROUND_DIR/logs/branch_task_variants-list.txt" \
    "$ROUND_DIR/logs/branch_task_variants_without_source_thoughts-list.txt" || exit 2
# Only the two variant arms share a task list; same-task forks have their own list.
for arm in branch_same_task branch_task_variants branch_task_variants_without_source_thoughts; do
  uv run spectragent bench --config "$ROUND_DIR/configs/$arm.yaml" --no-eval --timeout 1500 \
    --session-dir "$ROUND_DIR/sessions/$arm" > "$ROUND_DIR/logs/$arm.log" 2>&1
done

# 3. Build all downstream outputs using the same reviewed exclusions and frozen rules.
uv run python -m lookahead extract --root "$ROUND_DIR" --viewer-root "$ROUND_DIR" \
  --exclude-runs "$EXCLUDE" --out "$ROUND_DIR/out/steps.jsonl"
uv run python -m lookahead stability --root "$ROUND_DIR/record" --viewer-root "$ROUND_DIR" \
  --exclude-runs "$EXCLUDE" --annotations "$ROUND_DIR/annotations" \
  --out "$ROUND_DIR/out/stability" --media-areas "$ROUND_DIR/configs/replay_rules"
OPENAI_API_KEY="$LOOKAHEAD_JUDGE_API_KEY" uv run python -m lookahead judge-forks \
  --root "$ROUND_DIR" --viewer-root "$ROUND_DIR" --exclude-runs "$EXCLUDE" \
  --annotations "$ROUND_DIR/annotations" --out "$ROUND_DIR/out/forks" \
  --judge-config "$ROUND_DIR/configs/judge_deepseek.yaml"
uv run python -m lookahead gold-queue --root "$ROUND_DIR/record" --viewer-root "$ROUND_DIR" \
  --exclude-runs "$EXCLUDE" --annotations "$ROUND_DIR/annotations" \
  --n 100 --out "$ROUND_DIR/out/gold"
uv run spectragent view --root "$ROUND_DIR" --annotations "$ROUND_DIR/annotations" \
  --annotator QLYYLQ --open
```

Health is a review signal, not task correctness. In the old ten-site run it excluded four
sources; three Tencent Video exclusions came only from failed telemetry while documents and
screens rendered. A reviewed next pass should remove those three exclusions and retain the
five-step blank iQIYI task 18 exclusion. Preserve the old list and its 932-fork statistics;
new eligibility produces a new denominator. `agent_done`, a readable checkpoint and a
complete run also mean different things, especially for strict training export.

The earlier five-site L1 slice is not installed in the current public dataset; the ten-site
round selected 22 `All` tasks. Check IDs before starting. Historical sources and the newer
comparison are indexed in `.agents/skills/af-project-history/references/lookahead-rounds.md`
and the runbook. The live evidence is under `tmp/lookahead_10site_ui_tars_20261025/`; its
post-edited YAML does not change the trace identity. Pre-edit copies are under
`out/stability_improvement_20261026/input/configs/`.

## Runs, kinds and keys

`--root DIR` is searched for run directories (a directory holding a readable
`manifest.json`; exported datasets are skipped, and the search does not
descend into a run). A run that cannot be read — at the start, or when a
command reaches evidence of it that does not read back (a missing screenshot,
a torn frames index) — is skipped whole, named on standard error with the
reason, and listed in the command's output; the command goes on with the
other runs. Every output file is written whole or not at all (a temporary
file, then a rename); the judge's `calls.jsonl` is a log appended call by call.

What a run is comes from what it recorded, never from its path:

- `branch`: its configuration records branch provenance;
- `replay`: its composed configuration names the `archive` network source (or,
  for a run composed without one, its task setup names the archive it replayed);
- `record`: everything else.

A branch run is paired with its source by the source's recorded run id (and,
when several runs share it, by the recorded source path).

`--exclude-runs FILE` (every command but `health`) leaves out the runs a
run-exclusions file lists: every run whose run id it names, and every branch
run whose recorded source run id it names. The file is the `trace_branches`
loader's own `run-exclusions-1` format, the one `health` writes (below). Runs
are matched by recorded run id, never by path, so one file serves any
`--root`. An excluded run is left out when the runs are opened, like an
unreadable one: no command works on it, each names it on standard error
(`excluded <run key>: <reason>`) and lists it in its output (`excluded_runs`
in `stability.json`, `sampling.json` and the `variants` summary; a section of
`stability` and `judge-forks`' `report.md`).

Every run named in an output is a **run key**: the run directory relative to
`--viewer-root` (default: `--root`), the root `spectragent view` will be
started with. A root outside the viewer root is refused.

The commands that write a review queue write it to
`<annotations>/queues/<queue_id>.json` (`--annotations`, default `--out`;
`--queue-id`, default `<command>-<today>`), so
`spectragent view --root <viewer root> --annotations <annotations>` shows it.
Queues are built only from the viewer's own models (`spectragent.annotation`:
`ReviewQueue`, `QueueItem`, `LabelSpec`, `Question`, `Suggestion`, `Region`)
and written with its `write_queue`, so the viewer's strict loader reads every
one. Images are an observation (`{"kind": "observation", "run", "step"}`) or a
`browser.frames` file (`{"kind": "archive", "run", "step", "archiver",
"name"}`, `step` being the step whose commit took the frame, `null` for a
trailing commit; names `frame-<cycle>-before-<i>.png` /
`frame-<cycle>-after.png`), and boxes are `[x0, y0, x1, y1]` in the image's
own pixels.

### What a queue shows a person

The people who label are Chinese annotators, not researchers, so everything a
queue shows is Simplified Chinese and explains itself; stored values (choice
values, region kinds, question keys, run keys) stay English identifiers.

- **Titles** read `<site> · 任务 <task id> · 第 <n> 步`. The site is the one of
  the page actually shown (from the observation's URL, named from a small
  table of known hosts, else the host); when the task started on another
  site, `（从<start site>开始）` follows.
- **Options explain themselves**: every `LabelChoice` and `Question` carries
  `help`, shown beside it, and each choice a box rule `regions:
  optional | required | forbidden` that the viewer enforces when a label is
  saved (a choice that needs boxes cannot be saved without one, one that
  forbids them cannot be saved with one). `ReviewQueue.guide` holds the
  labeling guide (definitions, examples, what the images are), opened from the
  review page.
- **Machine details are tucked away**: an `ItemField` with `technical: true`
  (raw verdict names, dHash distances, URLs, action parameters, rule-verdict
  strings) goes into the collapsed 技术细节 section. Actions are written as
  numbered Chinese lines (`1. 点击 (223, 234) → 按钮“最多播放”`, `输入“贵州茅台”`,
  `向下滚动，页面移动了 300 像素`, `等待 1 秒`, `完成并回答：…`) by
  `spectragent.inspection.marks.action_lines`, the viewer's own describer.
- **Suggestions** are labelled by `ReviewQueue.suggestion_display.title`;
  `shown: on_request` keeps a model's answers hidden until the person asks.

### Which actions an image draws: `QueueImage.marks_from`

The viewer draws recorded actions on queue images as numbered marks (a click's
point, a drag's start → end arrow, a coordinate typing point with its text, a
scroll's point and direction), numbered as in the step's action list; actions
without a point (keys, waits, done) are listed beside the image. The marks
come from `spectragent/inspection/marks.py` and are converted from CSS
viewport pixels into the image's own pixels with the image's recorded
geometry. Which actions an image draws:

- `marks_from` left out (`null`): what the image is. An observation draws the
  actions its step decided on that screen; a before-action frame draws the one
  action it was taken before; any other image draws nothing.
- `"marks_from": []`: nothing.
- `"marks_from": [{"run": "<run key>", "step": <n>, "label": "<legend>"}, …]`:
  every action of each named step, one colour per entry in order, under its
  label. The steps may belong to another run than the image (a source run's
  actions on a branch's checkpoint screen); their points are drawn on this
  image's geometry.

`observation_image(...)` and `frame_image(...)` in `lookahead/queues.py` take
`marks_from=` and leave it `None` by default.

## `health --root DIR --out DIR2`

Is each recording usable as data? A recording whose page never rendered, or
whose content requests failed while it was recorded, still yields steps,
forks and queue items, and its forks even restore well because nothing on
the screen changes. If review confirms the evidence is unusable, exclude it and
record again; failed telemetry alone does not establish that. Every `record` run
gets a verdict, `usable` or `unusable`, and every signal that fired is a named
reason with its evidence. A run is `unusable` when a reason measured on the
recording fired; an answer-only reason is a flag. Branch and replay runs are not judged (listed in
`not_assessed`): a branch follows its source recording's verdict.

| reason | basis | fires when |
|---|---|---|
| `never_rendered` | recording | at least 5 consecutive policy screens are blank and each unchanged from the one before, and the agent waited or reloaded in at least half of them |
| `degraded_at_load` | recording | in step 0 or 1, at least 5 % and at least 5 of the content requests failed live; or the start page's document failed |
| `answer_states_failure` | answer | the final answer says the task was not done; reported only: on its own the run stays `usable` (the task failed, the recording did not) |

- **Blank**: the capture's settle wait did not see content in the viewport
  (`capped` with `content` among `waiting_for`, or no settle record) and the
  image agrees: under 0.1 % of its pixels differ from its most common colour
  by more than 48 levels (skeleton placeholders are painted a few levels off
  the background; text, icons and pictures are not). **Unchanged**: no changed
  region of 0.5 % of the image on `stability`'s grid. **Waited or
  reloaded**: a `wait`, a reload key (`F5`, `Ctrl/Meta+R`, with or without
  `Shift`), or a navigation to the URL shown. A page that is still loading is
  blank in one observation; five are at least 25 s of capped settle waits.
- **Content requests** are `Document`, `Fetch` and `XHR` exchanges of
  `browser.network`'s index, counted under the step whose commit took the
  line. One **failed live** when the replay loader's own reading of how it
  ended (`source_outcome`) is `network_failed`: Chrome reported a network
  failure. The page cancelling a request (`net::ERR_ABORTED`) is counted
  apart and decides nothing: navigating away cancels requests in healthy
  recordings too. The **start document** failed when the first early
  `Document` request for the task's start URL failed live or answered 400 or
  more, or when an early observation shows Chrome's error page
  (`chrome-error://`).
- **The answer** is `result.json`'s `answer`, matched against named rules for
  task-level failure statements in Chinese and English (`无法完成` / `未能完成`,
  `任务失败`, `unable to complete`, `the task cannot be completed`, `not
  achievable`); "未能读取播放量" (one field missing) is a partial answer and
  matches none. It is the agent's own statement, not a measurement of the
  recording, and labelled so (`basis: answer`). The agent's
  finish/`agent_done` state is not a correctness verdict: UI-TARS's `finished`
  verb has no task-success parameter. A finished run may answer that the task
  could not be completed.

Every threshold is a named constant in `lookahead/health.py`, with the
2026-10-23 measurements behind it; `health.json` repeats them.

Outputs:

- `health.json` (`"format": "lookahead-run-health-1"`): the thresholds, the
  counts, and per recording its verdict, reasons (`code`, `basis`,
  `summary`), policy and blank screen counts, every blank stretch (first and
  last step, length, steps that waited or reloaded, steps the settle wait
  capped waiting for content, URLs), the content requests of steps 0 and 1
  (counted, failed live, cancelled by the page, share, error texts and hosts
  of the failures), the start document, and the answer with the rule it
  matched; the runs not judged and the runs not read;
- `report.md`: the signals, a verdict table, why each unusable run is
  unusable (with its final answer), and the usable runs whose answer says the
  task was not done;
- `exclusions.json`: the unusable recordings as a `run-exclusions-1` file,
  built with the loader's own model (`RunExclusionsFile`): `run_id`, `reason`
  (the reasons' summaries) and `note` (the run key).

**One reviewed exclusions file, read everywhere.** Run `health` after recording and
preserve its generated `exclusions.json` as the raw machine result. Review start
Documents, failed endpoints, screenshots and blank stretches before adopting it:
Fetch/XHR telemetry failures can trigger a whole-run exclusion even when pages
rendered. Save decisions separately as `reviewed-exclusions.json` in the same
`run-exclusions-1` format, with reasons and notes. Delete an entry there to keep a
run; add an entry only with its evidence. An empty reviewed set is valid.

The `trace_branches` loader's `exclude_runs` and `--exclude-runs` of `variants`,
`extract`, `stability`, `judge-forks` and `gold-queue` all use that one reviewed file.
Decide before the expensive stages; changing the list later produces a new batch
and denominator, while the original list and reports remain evidence.

```bash
uv run python -m lookahead health --root "$E/record" --viewer-root "$E" --out "$E/out/health"
# Review raw exclusions and save the separate reviewed-exclusions.json first.
uv run python -m lookahead variants --root "$E/record" --viewer-root "$E" \
  --exclude-runs "$E/out/health/reviewed-exclusions.json" \
  --judge-config configs/lookahead/judge_deepseek.yaml ...
# branch loader exclude_runs: <round>/out/health/reviewed-exclusions.json
uv run python -m lookahead judge-forks --root "$E" --viewer-root "$E" \
  --exclude-runs "$E/out/health/reviewed-exclusions.json" \
  --judge-config configs/lookahead/judge_deepseek.yaml ...
```

What the framework does not state, and is derived here: how many of a step's
requests failed (counted from the index lines through `iter_index_lines` and
`source_outcome`; a per-step tally in the browser's offline statistics,
`archive_stats.py`, would give the count one meaning for the flywheel metrics
too), and which `Document` request was the main frame's (the index line
records no frame, so the start document is found by the task's start URL,
and an error page by the observation's URL).

## `extract --root DIR --out FILE.jsonl`

One JSON object per line (`"format": "lookahead-step-1"`) for every policy or
restored step that recorded an observation. A terminal step appears only as
the next observation of the step before it.

| field | meaning |
|---|---|
| `run`, `run_id`, `kind`, `site`, `task` | the run key, the recorded run id, record / replay / branch, the (source) task's start host, the task text |
| `task_variant` | a task-variant branch's variant provenance (`variant_id`, `task_text`, `source_task_text`, `note`), else `null` |
| `run_outcome` | how the run ended (`result.json`'s `agent_done`: `done`, `error`, …); `null` when it wrote no result |
| `step`, `origin`, `timestamp`, `error_phase`, `error`, `llm_output` | the step record's facts |
| `observation` | O_t: `step`, `screenshot` (an image was committed), `url`, `captured_at`, image and viewport sizes |
| `observation_cycle` | the browser's observation cycle of O_t (below) |
| `settle` | the capture's settle wait: `outcome` (`settled` / `capped` / `unknown`), `waited_ms`, `waiting_for`, `detail` |
| `capture_state` | `pointer`, `failed_visible_resources`, `foreground_change` |
| `actions` | each action: `name`, `params`, `success`, `error`, `effect` (the recorded effect as JSON), `effect_text`, and `facts`: the pointer's `point` and `hit` element (`{"role", "name"}`), `focus_after`, a navigation's `url_before` / `url_after`, `scroll_before` / `scroll_after`, the typed `text` |
| `action_spans` | per-action start and end times as recorded; `null` when not measured, `[]` when the step made no environment call |
| `frames` | the `browser.frames` records that follow O_t (same cycle), every field the frames reader gives (`released_at` when it gives one), plus `batch_step`, the step whose commit took it (`null`: a trailing commit), and for a before-action frame `span`: the step's action span at the frame's `action_index` (the ordinal of the `env.act` call; actions the guard refused are in neither, so frames are never joined through `actions`); `[]` when the run recorded none |
| `next` | O_{t+1}: `step`, `exists` (false when the run ended at this step, as an error does), `origin`, `screenshot`, `url` |
| `restoration` | this step's restoration check in brief (branch runs) |
| `branch` | a branch's provenance, its `source_run` key (when the source is under the root), `source_task`, and `restoration`: the checkpoint's check, restored steps, verdict counts, the first step that did not pass |
| `raw_dom` | `current` / `next`: whether `browser.dom_raw` holds the raw DOM of O_t / O_{t+1} |

The observation cycle is the browser's own counter, not the step number. It is
read from the `browser.dom_raw` files the step's commit took; failing that,
from the frames taken before the step's actions (each names the cycle it
follows); failing that, from a delayed frame requested between this
observation's capture and the next one's. Frames are matched to O_t by that
cycle wherever they were committed (a delayed frame still in flight lands in
a later step's batch).

## `stability --root DIR --out DIR2 [--media-areas DIR3]`

Did the screen the agent saw stay the same while it thought? Each observation
with a screenshot — of a policy step by default, since a restored step has no
think time (`--origins policy,restored` adds restored steps; the report then
splits by origin) — is compared with its delayed frame (`after_observation`)
and with the frame taken right before the step's first `env.act` call
(`before_action`).

A comparison is decided by the changed-region grid of `spectragent flywheel
diagnose` (a pixel changed when its largest RGB difference exceeds 16; a 32-px
cell is active when 1 % of its pixels changed; four-neighbour cells form one
region). It counts as **changed** when one region's own cell area is at least
`--min-change-area` of the image (default `0.005`, 0.5 %: a blinking caret or a
ticking clock stays under it; a playing preview does not). Each comparison
reports the changed-pixel fraction and every region (box clipped to the image,
cells, share of the image, whether it is over the minimum). The 64-bit dHash
distance (the restorer's own) is reported beside it and decides nothing: it is
blind to local changes, such as a video preview that changed 6-8 % of the
pixels at distance 0.

Verdicts: `stable_through_think` (the before-action frame is unchanged, and the
delayed one too when there is one), `stable_short` (the delayed frame is
unchanged; the before-action frame changed or is missing), `changed_short` (the
delayed frame changed), `changed_before_action` (no delayed frame; the
before-action frame changed), `no_frames` (no frame that could be compared). A
changed region overlapping a media element that was playing around the
screenshot is flagged separately (`media_change`), whatever the verdict. The
think time is the first action span's start minus the observation's capture.

**Media areas** (`--media-areas DIR3`, a replay rule set directory, normally
`configs/replay_rules`). In the 2026-10-24 round 99 of the 100 observations
with playing media changed on the media, so a video page almost never counts
as stable. The rule set is loaded as a replay loads it (`SiteRules.load`: the
same validation and digest; a set without `media_areas` rules is refused),
and its media-area rules — the declarations restoration verification applies
(`configs/replay_rules/README.md`, "Media areas in restoration") — are matched
against the observation's page URL, its screenshot's `source_url`. Frames
record no page URL, so a frame is taken to show the observation's page; a
frame taken on another tab than the observation's delayed frame is listed.
On a declared page the boxes measured on the observation's screenshot (every
`<video>` / `<audio>` element and every embedded frame the media read could
not reach) are painted black on the observation and on each frame before the
grid and the dHash compare them, by the browser's own masking
(`spectragent/core/capabilities/browser/media_mask.py`). Past 75 % of the
image masked nothing is masked and the whole images are compared. The
unmasked comparisons are kept as they are; when the mask was applied the
masked ones decide the verdict, the suggestion and the queue item. A
screenshot without `source_url` or without measured media facts cannot be
matched: nothing is masked, and it is counted as such. Without the option
nothing is masked.

Outputs:

- `stability.json` (`"format": "lookahead-stability-4"`): the method, the
  minimum area, the origins, the counts, the runs not read and the runs
  excluded, the media-area rule set (`media_areas`: its directory, digest and
  rules; `null` without the option), and per observation the settle facts,
  the previous step's last action, the think time, both comparisons of the
  whole images (frame, outcome, seconds after the observation, changed
  fraction, regions, dHash distance), the verdict and the verdict of the whole
  images (`unmasked_verdict`; the same unless a mask was applied), the media
  flags, and with the option `media_areas`: `status` (`masked`,
  `mask_too_large`, `no_page_url`, `no_media_facts`, `undeclared`,
  `no_image`), the matching `rule_ids`, `mask_applied`, `masked_share`, the
  painted `boxes` (kind, element, image pixels), `detail`,
  `frames_on_other_tab`, and when the mask was applied both masked
  comparisons, their verdict and their own `media_change`;
- `report.md`: verdict counts, settle outcome × verdict, the previous step's
  last action × verdict, origin × verdict (when restored steps are included),
  changes over playing media, how often the grid and the dHash disagree, and
  with the option a media-area section: the rule set and its digest, the
  observations per status, how many masked observations changed only in the
  masked media areas and how many still change (also per rule), and the
  observations whose playing media changed on pages no rule declares, by host
  and path;
- a review queue of up to `--queue-size` (60) observations with a comparable
  frame, taken in turn from each (settle outcome × verdict) group (`--seed`).
  Images: `observation`, `delayed`, `before_action` (trailing frames
  included). Choices: stable / still loading / animation or video / other
  change / cannot tell; region kind `change`. The suggestion is the machine
  verdict (a change over playing media suggests animation or video, one after
  a capped settle still loading, any other change other change) and, as
  boxes, the grid regions over the minimum area only (the small cells of a
  caret or a clock are noise to a person). The items are in Chinese: the
  images are 智能体看到的 / 约 1 秒后 / 第一个动作之前, the options with their
  help and box rules are 稳定 `stable` (forbidden: substantively the same, a
  blinking caret or ticking seconds do not count), 还在加载 `still_loading`,
  页面自动变化 `animation_or_video` (a playing video, a carousel, an animated
  preview, live stock quotes refreshing), 其他变化 `other_change` (a popup or
  overlay, a suggestions dropdown, an injected ad) and 看不出 `cannot_tell`,
  all optional but the first; the region kind is 变化区域 `change`. The
  observation draws its step's actions, the before-action frame the first
  action. When a media-area mask was applied, the suggestion, its boxes and
  the comparison texts are the masked ones, and a 媒体区域 field says that the
  media areas were masked, under which rule, how much of the image they
  cover and what the whole images gave; on a declared page left unmasked it
  says why.

## `variants --root DIR --steps-per-run N --per-step K --judge-config FILE.yaml --out FILE.json`

Alternative tasks that start from exactly a recorded screen, for task-variant
branches. In each `record` run, N checkpoints are taken from the middle of N
equal stretches of the steps a branch can start from (policy steps of a closed
trace, contiguous from step 0, within the complete prefix, observation
recorded) that have a screenshot. For each, the judge model sees the
screenshot, the URL and the source task and writes K tasks: a different
information need on the same site, answerable without logging in, concrete and
checkable, in the source task's language. A reply must hold exactly K distinct
tasks, none repeating the source task, each in the source task's script.

`FILE.json` is the `task-variants-1` file the `trace_branches` loader reads,
built with the loader's own model (`TaskVariantsFile`): `source_run_id` (the
recorded run id), `step`, `variant_id` (`"1"` … `"K"` per checkpoint, so task
ids read `…/s<step>/v1/b<i>`), `task_text`, `note` (the model, the step, and
where the answer is found). Every judge call is in `FILE.calls.jsonl`.

## `judge-forks --root DIR --judge-config FILE.yaml --out DIR2`

For every branch run: is its checkpoint decision reasonable for its task,
and did its browser actions produce the intended effect? Same-task forks
also ask whether the source and branch intended the same thing. These are
independent judgments of one decision, not a task-success score. The branch's
first policy step is paired with the source run's step at the checkpoint
through the recorded provenance; same-task
and task-variant branches alike. A fork is judged only when it is paired, its
checkpoint restored (the restorer's check of the checkpoint observation is
`ok`; the check is copied into the judgment) and the branch made a decision.
The others get the status `checkpoint_diverged`, `no_decision` or `unpaired`
with the reason, and stay out of every statistic, table and queue item; the
report lists them by count and reason.

- **Rules**, deciding only on strong evidence: action types (`click` =
  `click_coordinate`, `type` = `type_coordinate`, the coordinate counterparts
  the training export translates between; waits are ignored); a pointer
  hit is `same` for the same role and readable name at nearby points (48 CSS
  px), `different` only when both hits are named interactive targets (a click
  on a card's image and one on its title may be the same choice: `unknown`);
  typing is `same` for the same text into the same field (its role and name, or
  nearby points) and `different` for other text, or the same text into a
  differently named field; a scroll's direction, a navigation's URL, a
  dropdown's option; the next observation's URL (a different page decides, a
  difference only in volatile query parameters such as `vt`, `spm_id_from`,
  `utm_*` or long digit and hex tokens is neutral, any other query difference
  leaves it `unknown`). `no_decision` when either side ended in an error
  without an action (a reply that did not parse).
- **Judge model**, in separate quality and intent requests. `reasonable`
  and `executed_as_intended` are asked from the branch alone, identically for
  both kinds of fork: its task, the checkpoint
  screen with its pointer and coordinate-typing actions ringed, its actions
  with their measured effects, its reply and its next screen. `same_intent`
  (same-task forks whose source decided) alone uses source-run evidence: it
  shows both runs' actions on the same screen (run A blue, run B red) and
  both next screens. Each answer is `yes` /
  `no` / `uncertain` with a one-sentence reason. A decision containing only
  `done` asks only `reasonable` in the quality request: judge the answer,
  with no browser effect to assess. A mixed batch still asks about the effect
  of its browser operations.

Outputs: `judgments.jsonl` (`"format": "lookahead-fork-judgment-2"`, one object
per branch run: both runs, the checkpoint and its check, fork kind, tasks, both
sides' actions, errors and next observations, the rule verdict, the applicable
`questions`, the model's answers and its call ids; `status` `judged`,
`rules_only` (no model answer),
`checkpoint_diverged`, `no_decision` or `unpaired`), `report.md` (how many
same-task forks the rules decide, their agreement with the model, rule verdict
× model `same_intent`, reasonable × executed per fork kind, the forks not
judged), `calls.jsonl`, and a review queue that asks the same applicable questions
separately (`label.questions`, each 是 / 否 / 看不出). The report counts a
done-only decision's execution effect as `not applicable`, separately from
yes/no/uncertain and missing replies; it is not a failed or unanswered effect.

The review page explains 同任务 (same task) and 换任务 (a new task at the
checkpoint), and asks in this order:

1. **这次决定合理吗？** `reasonable`: read the branch's task, checkpoint,
   complete reply and actions. A reasonable step need not be optimal; a final
   answer is judged for its content.
2. **动作执行后，出现预期的结果了吗？** `executed_as_intended`: compare the
   branch's post-action screen and action results with what its actions
   intended. A successful action return alone does not establish the effect.
   Done-only items omit this question.
3. **与原决定比较：两次决定的意图相同吗？** `same_intent`: for same-task items
   whose source decided, separately compare the intended target and purpose.
   Different intent can still be reasonable; task-variant items omit this question.

Each item lists the keys it asks and suggests the model's `answers`
with its (English) `reasons`, titled 模型的判断（仅供参考） and hidden until the
person asks (`suggestion_display.shown: on_request`), so it cannot anchor
them. The main checkpoint image draws only the branch's actions. Same-task
items have a separate, initially collapsed **与原决定比较** section: the
branch and source checkpoint images each carry their own actions, alongside
their action lists and complete replies. A further **两次操作后的画面** section
holds their post-action images without action marks. These are result
references; a terminal observation is not another policy decision. A passed
restoration check means the configured tolerances were met, not that the two
checkpoint images are identical. Task-variant items show no source comparison.
The rule verdict stays in the comparison's technical details. The image
difference mode can select any two images across these sections; default
side-by-side presentation keeps the evidence groups separate.

The command takes exactly one of
`--judge-config` (ask the model), `--rules-only` and `--answers-from`; the
last two need no configuration file. `--rules-only` asks no model and writes no
queue. `--answers-from <judgments.jsonl>` asks no model either: it takes the
model answers an earlier `judge-forks` run wrote (matched by branch run id
and checkpoint) and writes the queue with them, so a queue can be rebuilt
without paying for the same answers twice. Both fresh and reused answers are
limited to the applicable `questions`: an older done-only execution answer
does not enter the new record, suggestion or report counts. Reuse does not
rejudge earlier answers under the revised wording. Each record names that
file in `answers_from` (`null` when the model was asked now).

## `gold-queue --root DIR --n 100 --out DIR2`

A review queue for human gold labels of the information the agent **actually
used** for its next step (user ruling). Items are policy steps whose screen and
next screen were both recorded and whose next step is a policy step that
acted; a step that finished the task (`done`) is left out, since nothing
follows it. They are taken in turn from each (site, action type) group, the
least-used run first (`--seed`). Images are ordered by time:

1. **当前屏幕** `current`: O_t, with this step's actions A_t marked.
2. **第一个动作之前** `before_action`: the frame immediately before the first
   action, after the agent thought, with that action marked. It is auxiliary
   archive evidence the model did not see. If no captured frame is available,
   the page explains the missing evidence instead of substituting an observation.
3. **下一屏** `next`: O_{t+1}, with the **next** decision's actions A_{t+1}
   marked. This is the observation those actions were based on, not their result.

Read the complete next-step reply and actions, then box the information on
O_{t+1} that the decision actually used. Only `next` accepts boxes, enforced
for both human labels and machine suggestions; the current screen and action
frame are context. The task, current actions and full recorded replies remain
available; URLs, run and step numbers and raw parameters are technical details.
If the next screen is blank or not loaded, press 跳过 and add a note.

Both gold and fork review items expose **档案** below the current
observation. Cards show all of this run's committed steps, grouped before,
at and after the item's anchor step. Later evidence helps the reviewer judge
what happened; it was not available to the model at the anchor step. Hover or focus a card to
preview it; click to pin and load its full observation fragments, recorded
reply, available native thinking, actions and results. The side image shows
the same step before an action, selectable when several action frames exist.
Both images align at the top. Independent checkboxes display action locations
and the pointer position recorded at capture time, using each image's geometry.
Missing pointer evidence stays unknown; an action's resulting pointer position
is never substituted for a before-action reading.
Restored steps label `source_history` as source material rather than a new
decision or new thinking. Missing thinking and missing or timed-out frames
remain explicit gaps. The actual model request, including the history window
it received, loads only when expanded: the full recorded trajectory is not a
claim that every step was in that request. Archive reads check the queue version
and never write to the trace.

Existing queue files keep the layout they were generated with. Rebuild them
from the saved traces to obtain these fields and history controls; for fork
queues, `--answers-from` reuses existing judge answers. Updating a queue does
not fill gaps in old recordings or require recapturing an otherwise readable trace.

Options (help in the queue; box rule in brackets): 结果列表 `result_list` (the
next step picks or reads among several similar items, even if it then clicks
one; required), 确认/反馈 `confirmation` (the agent checks the previous action
took effect: typed text visible, a filter or sort selected, a dropdown or
suggestions open, a message; required), 详情字段 `detail_fields` (specific
values to read or record, including ones put straight into the answer;
required), 操作入口 `navigation_target` (one specific control to operate next;
required), 不需要新信息 `nothing_new` (the next step does not depend on new
content: it continues a plan, waits, retries; forbidden), 其他 `other` (explain
in the note; optional). Region kind 信息所在位置 `evidence`, drawn on the next
screen. Items carry no suggestion (the viewer seeds and grades a label against
a suggestion, and a pixel difference is no answer). `DIR2/sampling.json`
counts the items by site, action type and run.

## The judge model

`variants` and `judge-forks` ask the chat model that `--judge-config
FILE.yaml` composes, `--concurrency` requests at once (default 4). The file
holds one `llm` layer and nothing else (any other layer is refused); it is
composed by spectragent's `ConfigComposer`, the one way every model in the
framework is built (`docs/architecture_design.md` §4.7), so every provider
and knob of `core/llm` is available, and the model's client is released when
the command ends. `configs/lookahead/judge_deepseek.yaml` is DeepSeek's
official API (`provider: {name: deepseek}`, `model: deepseek-flash`,
`reasoning_effort: high`, `max_completion_tokens: 16384`, `max_retries: 5`,
`timeout: 300`); its comments give the token counts the budget was chosen
from. Its `api_key` is left out, so the OpenAI client reads `OPENAI_API_KEY`
from the environment. Give the command the official judge credential, as in the
collection recipe. `--rules-only` and `--answers-from` need no configuration.

Each request goes through the chat Protocol's `ainvoke` with the reply's
pydantic model as `output_format`. The provider sends the schema the way its
route accepts it (DeepSeek has no `json_schema` response format, so the
schema goes into the system message with `json_object`), removes a code
fence and validates the reply against that model, the variants' own checks
(count, distinct, not the source task, same script) included; an HTTP 200 is
no proof the schema was honoured. A refused reply is asked again at most
twice: the reply goes back as the assistant's turn, followed by "That reply
was refused: <reason>. Reply again with only the JSON object the schema
describes, nothing else.", the reason being pydantic's errors with their
locations.

A call that returns no reply is retried by the provider's SDK
(`max_retries`; the OpenAI client pauses 0.5 s doubling to at most 8 s, or
as long as the server's `Retry-After` says) when it is a rate limit (429), a
server error (5xx) or a lost connection; what still fails then fails only its
request. So does a reply cut at the token ceiling or stopped by the server
(DeepSeek's `insufficient_system_resource` / `aborted`): it is recorded and
not asked again, since the budget and the server would be the same. The first
request is sent alone as a probe: when it gets no reply at all — the route
refuses it (a wrong model id, a bad key), a rate limit or server error
outlasts the SDK's retries, or the client cannot be built (no API key in the
environment) — the command stops with the status, the provider's message and
the configuration file, before sending anything else and before writing any
output. A reply that was refused by validation, cut at the token ceiling or
stopped by the server is an answer from the route, so the batch goes on.

Every call is recorded in `calls.jsonl` (`FILE.calls.jsonl` for `variants`),
one JSON object per `ainvoke`, in the order the calls finished:

| field | meaning |
|---|---|
| `call_id`, `purpose`, `item`, `attempt` | the call (`<purpose>-<n>`), what it asks about, the run and step it concerns, 0 for the first ask and 1, 2 for the re-asks |
| `model`, `config` | the model id, and the composed `llm` entry redacted as a trace records it: numbers and closed choices as set, `null` for what was not set, `<omitted>` for a free-form string that was (the model id in it, a key, a base URL) |
| `prompt_sha256`, `prompt_text`, `images` | a digest of the judge's messages (images included, the provider's schema instruction not), their text on the first attempt only, and which run and step each image is |
| `started_at`, `latency_ms` | when the call started (epoch seconds) and how long it took, SDK retries included |
| `usage` | the provider's `ModelInvokeUsage`: `prompt_tokens`, `prompt_cached_tokens`, `completion_tokens`, `completion_reasoning_tokens` (`null`: not reported), `total_tokens` |
| `stop_reason`, `thinking` | the reply's finish reason, and the reasoning the response carried beside it |
| `reply`, `raw_output` | the validated reply; or the text the validator refused, or what a cut reply had written |
| `error_kind`, `error_type`, `status`, `error` | why there is no valid reply: `validation` (with the reason sent back to the model), `truncated`, `interrupted` or `call` (a failed call, with its status code: the HTTP status the route answered, 429 for a rate limit, 502 when no answer came); the exception's class; the detail |
| `valid` | whether the reply validated |

The totals are printed at the end (and written into `judge-forks`'
`report.md`): requests and those without a valid reply; calls, valid ones,
refused ones, failed ones (cut at the ceiling, stopped by the server);
prompt, completion and reasoning tokens, with the calls whose usage stated
no reasoning count and those without usage; summed latency and wall time.
