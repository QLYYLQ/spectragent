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

## Runs, kinds and keys

`--root DIR` is searched for run directories (a directory holding a readable
`manifest.json`; exported datasets are skipped, and the search does not
descend into a run). A run that cannot be read is skipped and named on
standard error with the reason.

What a run is comes from what it recorded, never from its path:

- `branch`: its configuration records branch provenance;
- `replay`: its composed configuration names the `archive` network source (or,
  for a run composed without one, its task setup names the archive it replayed);
- `record`: everything else.

A branch run is paired with its source by the source's recorded run id (and,
when several runs share it, by the recorded source path).

Every run named in an output is a **run key**: the run directory relative to
`--viewer-root` (default: `--root`), the root `spectragent view` will be
started with. A root outside the viewer root is refused.

The commands that write a review queue write it to
`<annotations>/queues/<queue_id>.json` (`--annotations`, default `--out`;
`--queue-id`, default `<command>-<today>`), so
`spectragent view --root <viewer root> --annotations <annotations>` shows it.
The queues follow the `review-queue-1` format: images are an observation
(`{"kind": "observation", "run", "step"}`) or a `browser.frames` file
(`{"kind": "archive", "run", "step", "archiver", "name"}`, `step` being the
step whose commit took the frame), and boxes are `[x0, y0, x1, y1]` in the
image's own pixels.

## `extract --root DIR --out FILE.jsonl`

One JSON object per line (`"format": "lookahead-step-1"`) for every policy or
restored step that recorded an observation. A terminal step appears only as
the next observation of the step before it.

| field | meaning |
|---|---|
| `run`, `run_id`, `kind`, `site`, `task` | the run key, the recorded run id, record / replay / branch, the (source) task's start host, the task text |
| `task_variant` | a task-variant branch's variant provenance, else `null` |
| `step`, `origin`, `timestamp`, `error_phase`, `error`, `llm_output` | the step record's facts |
| `observation` | O_t: `step`, `screenshot` (an image was committed), `url`, `captured_at`, image and viewport sizes |
| `observation_cycle` | the browser's observation cycle of O_t (below) |
| `settle` | the capture's settle wait: `outcome` (`settled` / `capped` / `unknown`), `waited_ms`, `waiting_for`, `detail` |
| `capture_state` | `pointer`, `failed_visible_resources`, `foreground_change` |
| `actions` | each action: `name`, `params`, `success`, `error`, `effect` (the recorded effect as JSON), `effect_text`, and `facts`: the pointer's `point` and `hit` element (`{"role", "name"}`), `focus_after`, a navigation's `url_before` / `url_after`, `scroll_before` / `scroll_after`, the typed `text` |
| `action_spans` | per-action start and end times as recorded; `null` when not measured, `[]` when the step made no environment call |
| `frames` | the `browser.frames` records that follow O_t (same cycle), each with `batch_step`, the step whose commit took it (`null`: a trailing commit); `[]` when the run recorded none |
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

## `stability --root DIR --out DIR2`

Did the screen the agent saw stay the same while it thought? Each observation
with a screenshot is compared with its delayed frame (`after_observation`) and
with the frame taken right before the step's first action (`before_action`):
the 64-bit dHash distance (the restorer's own function) and the changed
regions (the 32-px grid of `spectragent flywheel diagnose`: a pixel changed
when its largest RGB difference exceeds 16, a cell is active when 1 % of its
pixels changed, four-neighbour cells merge into one box).

Verdicts, a frame within `--max-distance` (default 5) counting as the same
screen: `stable_through_think` (the before-action frame is the same, and the
delayed one too when there is one), `stable_short` (the delayed frame is the
same, the before-action frame is not or is missing), `changed_short` (the
delayed frame differs), `changed_before_action` (no delayed frame, the
before-action frame differs), `no_frames`. A changed box overlapping a media
element that was playing around the screenshot is flagged separately
(`media_change`).

Outputs:

- `stability.json` (`"format": "lookahead-stability-1"`): the method, the
  counts, and per observation the settle facts, the previous step's last
  action, both comparisons (frame, outcome, seconds after the observation,
  distance, boxes, changed pixels), the verdict and the media flags;
- `report.md`: verdict counts, settle outcome × verdict, the previous step's
  last action × verdict, changes over playing media;
- a review queue of up to `--queue-size` (60) observations with frames, taken
  in turn from each (settle outcome × verdict) group (`--seed`). Images:
  `observation`, `delayed`, `before_action`. Choices: stable / still loading /
  animation or video / other change / cannot tell; region kind `change`. The
  suggestion is the machine verdict (a change over playing media suggests
  animation or video, one after a capped settle still loading, any other
  change other change) with the changed boxes as regions.

## `variants --root DIR --steps-per-run N --per-step K --out FILE.json`

Alternative tasks that start from exactly a recorded screen, for task-variant
branches. In each `record` run, N checkpoints are taken from the middle of N
equal stretches of the steps a branch can start from (policy steps of a closed
trace, contiguous from step 0, within the complete prefix, observation
recorded) that have a screenshot. For each, the judge model sees the
screenshot, the URL and the source task and writes K tasks: a different
information need on the same site, answerable without logging in, concrete and
checkable, in the source task's language. A reply must hold exactly K distinct
tasks, none repeating the source task, each in the source task's script.

`FILE.json` is the `task-variants-1` file the `trace_branches` loader reads:
`source_run_id` (the recorded run id), `step`, `variant_id` (`"1"` … `"K"`
per checkpoint), `task_text`, `note` (the model, the step, and where the
answer is found). Every judge call is in `FILE.calls.jsonl`.

## `judge-forks --root DIR --out DIR2`

For every branch run: is its decision at the checkpoint the same as the
source's, and is it sound? The branch's first policy step is paired with the
source run's step at the checkpoint through the recorded provenance; same-task
and task-variant branches alike.

- **Rules**: action types (`click` = `click_coordinate`, `type` =
  `type_coordinate`; waits are ignored), the element the pointer hit (role and
  name), the typed text, a scroll's direction, a navigation's URL, a dropdown's
  option, and the next observation's URL (a different page decides; a
  difference only after the path does not). `same`, `different` or `unknown`,
  with the reasons.
- **Judge model**: the checkpoint screen with the pointer actions ringed (the
  branch's red B1…, the source's blue S1…), the branch's next screen and, for a
  same-task fork, the source's actions and next screen. It answers
  `same_intent` (same-task forks only), `reasonable` and
  `executed_as_intended`, each `yes` / `no` / `uncertain` with a one-sentence
  reason.

Outputs: `judgments.jsonl` (one object per branch run: both runs, the
checkpoint, fork kind, tasks, both sides' actions and next observations, the
rule verdict, the model's answers, its call ids; `status` `judged`,
`rules_only` or `unpaired` with the reason), `report.md` (how many same-task
forks the rules decide, their agreement with the model, rule verdict × model
`same_intent`, reasonable × executed per fork kind), `calls.jsonl`, and a
review queue whose items carry the model's answers as the suggested choices
(several may be checked: same / different intent, reasonable / not, executed /
not). `--rules-only` asks no model and writes no queue.

## `gold-queue --root DIR --n 100 --out DIR2`

A review queue for human gold labels. Items are policy steps whose screen and
next screen were both recorded, taken in turn from each (site, action type)
group, the least-used run first (`--seed`). Each shows `current` (O_t), `next`
(O_{t+1}) and `before_action` (the frame before the first action, when one was
taken), with the task, the actions and their measured effects, both URLs and
the agent's reply. Choices for the kind of information needed next: result
list, confirmation or feedback, detail fields, navigation target, nothing new
needed, other. Region kind `evidence`, drawn on the next screen. The
suggestion carries no choice; its regions are the changed boxes between the
two screens, as hints (source `pixel-diff`). `DIR2/sampling.json` counts the
items by site, action type and run.

## The judge model

`variants` and `judge-forks` call `--model` (default
`deepseek-v4.1-flash-1017`) with `reasoning_effort` `--reasoning-effort`
(default `high`) through the OpenAI-protocol gateway named by `OPENAI_BASE_URL`
and `OPENAI_API_KEY`, with the `openai` SDK, `--concurrency` requests at once
(default 4). Images are sent as base64 data URLs. Every request asks for strict
JSON (`response_format` `json_schema`, strict) and every reply is validated
with pydantic: an HTTP 200 is no proof the schema was honoured. A request is
sent at most three times; each retry tells the model why its previous reply
was refused. Every attempt is recorded (`calls.jsonl`): model, parameters,
latency, usage (reasoning tokens included), the raw reply, whether a code fence
had to be removed, and why it was refused. The totals are printed at the end.
