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

## `stability --root DIR --out DIR2`

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

Outputs:

- `stability.json` (`"format": "lookahead-stability-2"`): the method, the
  minimum area, the origins, the counts, and per observation the settle facts,
  the previous step's last action, the think time, both comparisons (frame,
  outcome, seconds after the observation, changed fraction, regions, dHash
  distance), the verdict and the media flags;
- `report.md`: verdict counts, settle outcome × verdict, the previous step's
  last action × verdict, origin × verdict (when restored steps are included),
  changes over playing media, and how often the grid and the dHash disagree;
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
  action.

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

`FILE.json` is the `task-variants-1` file the `trace_branches` loader reads,
built with the loader's own model (`TaskVariantsFile`): `source_run_id` (the
recorded run id), `step`, `variant_id` (`"1"` … `"K"` per checkpoint, so task
ids read `…/s<step>/v1/b<i>`), `task_text`, `note` (the model, the step, and
where the answer is found). Every judge call is in `FILE.calls.jsonl`.

## `judge-forks --root DIR --out DIR2`

For every branch run: is its decision at the checkpoint the same as the
source's, and is it sound? The branch's first policy step is paired with the
source run's step at the checkpoint through the recorded provenance; same-task
and task-variant branches alike. A fork is judged only when it is paired, its
checkpoint restored (the restorer's check of the checkpoint observation is
`ok`; the check is copied into the judgment) and the branch made a decision.
The others get the status `checkpoint_diverged`, `no_decision` or `unpaired`
with the reason, and stay out of every statistic, table and queue item; the
report lists them by count and reason.

- **Rules**, deciding only on strong evidence: action types (`click` =
  `click_coordinate`, `type` = `type_coordinate`; waits are ignored); a pointer
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
- **Judge model**, in two separate requests so that neither answer sees the
  other side. `reasonable` and `executed_as_intended` are asked from the
  branch alone, identically for both kinds of fork: its task, the checkpoint
  screen with its pointer and coordinate-typing actions ringed, its actions
  with their measured effects, its reply and its next screen. `same_intent`
  (same-task forks whose source decided) shows both runs' actions on the same
  screen (run A blue, run B red) and both next screens. Each answer is `yes` /
  `no` / `uncertain` with a one-sentence reason.

Outputs: `judgments.jsonl` (`"format": "lookahead-fork-judgment-2"`, one object
per branch run: both runs, the checkpoint and its check, fork kind, tasks, both
sides' actions, errors and next observations, the rule verdict, the model's
answers, its call ids; `status` `judged`, `rules_only` (no model answer),
`checkpoint_diverged`, `no_decision` or `unpaired`), `report.md` (how many
same-task forks the rules decide, their agreement with the model, rule verdict
× model `same_intent`, reasonable × executed per fork kind, the forks not
judged), `calls.jsonl`, and a review queue that asks the same questions
separately (`label.questions`, each 是 / 否 / 看不出). The queue explains in
Chinese what a branch is (a recorded run restored to a checkpoint, where the
model decides again on the same screen) and its two kinds, 同任务 (same task)
and 换任务 (same screen, a new task), and asks, each with its help:
意图相同 `same_intent` (do the source and the branch pursue the same target and
purpose on this screen; execution details may differ), 合理 `reasonable`
(judging the branch alone: is this batch a reasonable step toward its task; it
need not be optimal) and 执行如意 `executed_as_intended` (does the branch's
next screen show the intended effect, without an error, a block or missing
content). Each item lists the keys it asks and suggests the model's `answers`
with its (English) `reasons`, titled 模型的判断（仅供参考） and hidden until the
person asks (`suggestion_display.shown: on_request`), so it cannot anchor
them. The checkpoint image draws the branch's actions and, for a same-task
item, the source run's actions at the checkpoint in a second colour
(`marks_from`); the next screens draw none. A task-variant item shows nothing
of the source run; a same-task item shows the source's actions and next
screen only in fields and images marked （只用于“意图相同”）, the rule verdict
among the technical details. `--rules-only` asks no model and writes no
queue. `--answers-from <judgments.jsonl>` asks no model either: it takes the
model answers an earlier `judge-forks` run wrote (matched by branch run id
and checkpoint) and writes the queue with them, so a queue can be rebuilt
without paying for the same answers twice; each record names that file in
`answers_from` (`null` when the model was asked now).

## `gold-queue --root DIR --n 100 --out DIR2`

A review queue for human gold labels of the information the agent **actually
used** for its next step (user ruling). Items are policy steps whose screen and
next screen were both recorded and whose next step is a policy step that
acted; a step that finished the task (`done`) is left out, since nothing
follows it. They are taken in turn from each (site, action type) group, the
least-used run first (`--seed`). Each shows `current` (O_t, drawing this
step's actions), `next` (O_{t+1}, drawing the agent's **next** step's actions)
and `before_action` (the frame before the first action, when one was taken,
drawing that action), with the task, this step's actions, and the next step's
reply and actions in Chinese; URLs, the run, the step numbers and the raw
parameters are technical details. The instructions say: see what the agent
did next, then box where on the next screen the information it needed for
that is; if the next screen is blank or not loaded, press 跳过 and add a note.

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

`variants` and `judge-forks` call `--model` (default
`deepseek-v4.1-flash-1017`) with `reasoning_effort` `--reasoning-effort`
(default `high`) and `max_completion_tokens` `--max-completion-tokens`
(default 8192) through the OpenAI-protocol gateway named by `OPENAI_BASE_URL`
and `OPENAI_API_KEY`, with the `openai` SDK, `--concurrency` requests at once
(default 4). Images are sent as base64 data URLs.

Every request asks for strict JSON (`response_format` `json_schema`, strict).
The schema sent leaves out what strict mode refuses (`minLength`, `minItems`,
`pattern`, titles, defaults …); the pydantic models keep those constraints and
validate every reply, since an HTTP 200 is no proof the schema was honoured. A
refused reply is asked again at most twice, each time saying why.

A call that returns no reply is retried after an exponential, jittered pause
(2 s doubling to at most 60 s, five times) when it is a rate limit (429), a
server error (5xx) or a lost connection; any other 4xx is not retried. The
first request is sent alone as a probe: when the route refuses it that way,
the command stops with the status and the gateway's message, before sending
anything else and before writing any output.

Every call is recorded (`calls.jsonl`): model, parameters, latency, usage
(reasoning tokens included), the raw reply, the HTTP status of a failure,
whether a code fence had to be removed, and why a reply was refused. The
totals are printed at the end.
