# 数据飞轮运行手册

从仓库根目录执行下列命令。数据飞轮把一次真实的浏览器 run 连同它当时的网络环境录下来，之后不访问线上站点也能复用它：离线回放、从中间某一步分叉、导出成学生模型的 SFT 数据，最后用一份报告度量这条数据线的质量。每一段都是现有入口加 `configs/flywheel/` 下的一份 YAML，产物都是 trace 目录或数据集目录。各层为什么这样设计，见 [architecture_design.md](architecture_design.md) §4.14（录制与离线回放）、§4.15（训练导出）、§4.16（度量）；YAML 字段的逐项说明见 [configs/README.md](../configs/README.md#recording-environment-evidence)。

| 段 | 入口 | 发布的配置 | 产物 |
|---|---|---|---|
| 录制 | `spectragent-bench` 或 `spectragent-run` | `record_ui_tars_pinned.yaml` | run 目录下的 `env/` |
| 离线回放 | `spectragent-run --replay-archive` | `replay_ui_tars_archive.yaml` | 一个新 run，`env/` 里是回放日志 |
| 分叉 | `spectragent-bench`（任务来自 `trace_branches`） | `branch_ui_tars_archive.yaml`、`same_path_ui_tars_archive.yaml` | 每个分叉一个 run |
| 导出 | `spectragent-export` | `export_sft.yaml`、`ui_tars_student.yaml` | 数据集目录 |
| 度量 | `spectragent-flywheel run`、`spectragent-flywheel report` | 自己写的链规格 | `report.json`、`report.md` |

## 先在 fixture 上演练

```bash
uv sync --extra dev
uv run python -m tests._helpers.flywheel_rehearsal --out /tmp/af-rehearsal
```

这条命令在本机起 fixture 站点和脚本化的模型，用 `configs/flywheel/` 的配置跑完整条链：4 个任务的录制和对照组、原路径回放、分叉、两次导出、报告。它不调用真实模型，也不访问外网，需要本机的 Chrome，约 40 s。产物在 `/tmp/af-rehearsal/chain/`，布局与真实的链相同（见"度量报告"）；`--out` 下已有 `chain/` 时拒绝运行。fixture 只能校准度量本身，所以报告里每条建议都是 `partial`。

## 前提与限制

- 只能用 `local_chrome`。录制、`replayability: pinned` 和 `network_source: archive` 都要在 capability 自己启动的浏览器上开第二条 CDP 连接（recorder 连接）。六个云 provider、capability 块或任务上下文里的 `cdp_url`、注入的控制客户端都会被拒，报错同时点名双方。本机要装有 Chrome 或 Chromium，自动发现失败时设 `CHROME_EXECUTABLE_PATH`。
- 这一层依赖的 Chrome 行为是在 Chrome 153 和 155 上实测的。换了 Chrome 版本，先跑一遍真实 Chrome 的回归测试，它们用脚本化的模型，不需要密钥：

  ```bash
  uv run pytest tests/test_core/test_capabilities/test_browser_recorder_live.py \
    tests/test_core/test_capabilities/test_browser_replay_live.py
  ```

- `pinned` 档位要求每个 run 从新建的 profile 启动：`user_data_dir`、环境变量 `CHROME_USER_DATA_DIR`、不是 `Default` 的 `profile_directory`、`extra_args` 里的 `--user-data-dir` 都会被拒。provider 上与档位矛盾的旋钮也会被拒：`deterministic_rendering: true`，与档位不同的 `force_device_scale_factor` 或 `user_agent`，`extra_args` 里由档位负责的开关。`random_seed` 至少是 1。评测 run 和 anti-bot 实验保持 `"off"`：pinned 会改变站点的行为，本身也是 anti-bot 能识别的特征。
- 每个手写的 capability 块都要写下面两个块。它们是生成 schema 里必填的单实例受体，不录制的配置也要写：

  ```yaml
  replayability: { replayability_id: "off" }   # 录制用 pinned
  network_source: { source_id: live }          # 回放用 archive
  ```

  `"off"` 必须加引号：PyYAML 按 YAML 1.1 把裸的 `off` 读成 `false`。
- 归档器只写在 `trace_writer` 的 `archive` 列表里，键要全限定（`browser.network`）。写进 capability 块的 `archiver:` 在组装时被拒。
- 这些组合问题全部在 `prepare()` 报：归档器与档位、provider 与它的旋钮、profile、网络来源，以及回放时与源 run 的一致性（`prepare()` 只读源 run 的环境指纹）。错误在打开 trace、启动 Chrome 之前出现，每条点名两边，`spectragent-run --dry-run` 也查得出来（回放配置要加 `--replay-archive DIR`）。`spectragent-bench` 开跑前先用第一个任务检查一次（pre-flight）：被拒时一个任务也不跑、不建 trace，在 stderr 印出原因，退出码 2。setup 在启动 Chrome 之前再查一遍。

## 录制

`configs/flywheel/record_ui_tars_pinned.yaml` 是发布的录制配置：UITarsAgent（`coordinate_space: screenshot_pixels`，`max_actions_per_step: 1`），`replayability: { replayability_id: pinned, random_seed: 7 }`，`network_source: { source_id: live }`，trace hook 上绑三个归档器。录制前复制一份（下面记作 `my/record.yaml`），把 `trace_writer.trace_dir` 从发布值 `traces` 改成 `traces/recorded`：发布的分叉与原路径回放配置从这个目录找源 run，导出配置也读 `traces/recorded/*`。回放 run 和分叉 run 不能当分叉的源，和录制混在一个目录里时，分叉 sweep 在开跑前就报错（`on_unbranchable: raise`）。

```bash
export OPENAI_API_KEY="你的密钥"
uv run spectragent-bench --config my/record.yaml \
  --benchmark online_mind2web --subset All --task-id <ID> --no-eval
```

基准数据不随仓库发布，数据目录怎么设见 [Browser agent 运行手册](browser_agent_runbook.md) 的"跑数据集"。单个自由文本任务用 `spectragent-run --config my/record.yaml --start-url URL --task "..."`。数据集任务的 trace 目录是 `<trace_dir>/<task_id>_<8 位随机串>`，`spectragent-run` 的是 `<trace_dir>/<run_id>`。飞轮的每一段都不需要评测结果；只有想按评测结果筛选源 run（分叉和导出的可选过滤 `require_eval`，默认 `null`）时，才在录制配置里加 `evaluator:` 块、去掉 `--no-eval`，判定会写进每个 trace 的 `evals/`。

```yaml
hook:
  - hook_id: trace_writer
    trace_dir: traces/recorded
    archive:
      - archiver_id: browser.network   # 每个请求和响应，含 body
      - archiver_id: browser.storage   # cookie 与 local / session storage
      - archiver_id: browser.dom_raw   # 每个提交的观测 cycle 的原始 DOM
```

`archive: []` 表示不录。不写 `archive` 时，文件里有顶层 `archiver:` 段就采纳那一段的全部归档器，没有就不录。`browser.network` 默认 `require_replayable: true`，与 `"off"` 档位一起绑定会被拒；要把一次评测 run 照原样存档，写 `require_replayable: false`，这样的归档不保证能回放。

证据都在 run 目录的 `env/` 下，只有绑定了归档器的 run 才有这个目录：

| 路径 | 内容 |
|---|---|
| `env/steps/NNN/browser.network/exchanges.warc.gz` | 第 NNN 步的提交收集到的交换，WARC/1.1，body 是解码后的字节；同一 run 里重复的 payload 写成 revisit 记录 |
| `env/steps/NNN/browser.network/index.jsonl` | 同一步的索引：`kind: exchange`（每个交换恰好一行，带 `body_status`），`kind: body`（之后补取到的大 body），`kind: pending`（收集时还在途） |
| `env/steps/NNN/browser.network/websocket.jsonl` | WebSocket 帧（方向、opcode、时间、内容），有帧时才有 |
| `env/steps/NNN/browser.storage/storage.json` | cookie 和每个 origin 的 local / session storage，值原样保存；状态变了才写，某一步的状态是那一步及之前最近的一份 |
| `env/blobs/sha256/<ab>/<rest>` | 内容寻址的文件，run 内只存一份：`browser.dom_raw` 的 `dom-<cycle>-<tab>.json.gz`，补取的大 body `body-<sha>.bin` |
| `env/run.browser.json`，之后的 `env/run.browser.1.json`…… | 环境指纹：Chrome 版本、UA、viewport、DPR、时区、语言、档位与参数（含 T0 和种子）、shim 摘要、启动开关摘要、网络来源、脱敏策略与隐私标记；内容变了才写新的一版 |
| `env/trailing/KK/<archiver_id>/<name>` | run 结束时那次收集的证据，不归任何一步 |

`body_status` 的取值：`ok`、`redirect`、`too_large`（声明的长度超过 `max_inline_body_bytes`，默认 5 MB，又不是能事后补取的静态 GET）、`streamed_later`（大的静态资源，事后补取）、`websocket`、`incomplete`（超时或 run 结束时没读完）。UI-TARS 是纯视觉 agent，不协商 DOM，`browser.dom_raw` 会在每个观测 cycle 自己多采一次 DOM。各归档器的其余旋钮（`body_read_timeout_s`、`drain_timeout_s`、`strip_request_headers`、`self_capture_timeout_s` 等）见生成的 schema，拼错的键在校验时被拒。

pinned 的旋钮：`base_time`（录制用默认的 `run_start`，回放和分叉用 `from_archive`）、`random_seed`、`timezone`（默认 `UTC`）、`locale`（默认 `en-US`）、`device_scale_factor`（默认 1.0）、`user_agent`（默认不固定，只固定 `Accept-Language`）、`reduced_motion`（默认 `true`）。回放会把这些参数、viewport 和 Chrome 启动命令行的摘要（调试端口、profile 目录和网络路由开关除外）与录制逐项比较，所以录制、回放、分叉三份配置的档位参数、`viewport_width` / `viewport_height` 和 backend 块（`headless`、窗口大小、`extra_args`……）都要相同。

## 读回 env/ 里的证据

`env/` 下的每个文件都登记在每步和收尾的 extra 索引里，manifest 是提交点（[architecture_design.md](architecture_design.md) §5.3、§5.5）。按路径直接打开或 glob 可能读到崩溃时没提交成的文件。用 `RunTrace` 的访问器读：它们只认已提交的文件，并按索引核对大小和 sha256。

```python
from pathlib import Path

from spectragent.core.capabilities.browser.archive.dom_raw import read_dom_document
from spectragent.core.trace import RunTrace

trace = RunTrace(Path("traces/recorded/<run>"))
for step, record in trace.iter_archive("browser.network", include_trailing=True):
    print(step, record.name, record.path)          # 收尾批次的 step 是 None
fingerprint = trace.archive_run_file("browser")    # 已提交的最新一版指纹
print(trace.read_extra(fingerprint.path))
for record in trace.archive_files(0, "browser.dom_raw"):
    document = read_dom_document(trace.blob(record.sha256))   # 内容寻址的文件按 sha256 取
```

- `archive_files(step, archiver_id)`：第 `step` 步提交的、属于这个归档器的文件（含内容寻址的），没有就是 `[]`。
- `iter_archive(archiver_id, include_trailing=False)`：按步给出 `(step, record)`；`include_trailing=True` 时再给出收尾批次的 `(None, record)`。
- `blob(sha256)`：内容寻址文件的字节。
- `archive_run_file(capability_id)`：这个 capability 已提交的 run 记录里版本最高的一份。
- `archive_lines(path, model)`：把逐行 JSON 的归档文件按给定的 pydantic 模型读回，坏行报 `TraceSchemaError` 并点名行号。

`trace.config` 里还记着：`negotiation.active_archivers` 和 `negotiation.archive_bindings`（绑定了哪些归档器），`env_evidence_privacy`（所绑定归档器声明的隐私标记），以及按值脱敏后的任务 setup context `task_setup`（`start_url`；回放 run 另有 `replay_archive`，已是绝对路径）。按任务、按功能的字段有值才写进 `config.json`：没绑定归档器的 run 没有 `env_evidence_privacy`，没有 setup context 的没有 `task_setup`，不是分叉的没有 `branch`。组装后的配置（`resolved`）里闭集值与数字原样记录（`max_steps`、`temperature` 都看得到），没标 trace-safe 的自由字符串（模型名、`base_url`）记成 `"<omitted>"`。`env/steps/NNN/` 是第 NNN 步的提交收集到的证据，页面加载完之后才发出的请求（`fetch`、XHR、迟到的 beacon）会落在下一步。

## 离线回放一个 run

`configs/flywheel/replay_ui_tars_archive.yaml` 回放用录制配置录下的 run（设计见 [architecture_design.md](architecture_design.md) §4.14 "回放"）。浏览器经 Fetch 发出的每个请求都在 Request 阶段按源 run 的 `browser.network` 归档应答，归档里没有的由 miss 策略应答（失败、204 或 404），没有请求会被放到网络上。agent 照常运行，每一步仍然调用模型，只有网络来源换成了归档。

```bash
uv run spectragent-run --config configs/flywheel/replay_ui_tars_archive.yaml \
  --task "录制时的任务文本" --start-url <录制时的 start_url> \
  --replay-archive traces/recorded/<run>
```

任务文本和 `start_url` 在源 run 的 `config.json` 里（`task`、`task_setup.context.start_url`）。`replay_archive` 可以写相对路径，按启动目录解析：`AgentRuntime.execute` 在记进 trace、交给浏览器之前把它变成绝对路径，`--replay-archive` 和 Python 里自己组的 `env_context` 都一样，回放 trace 里记的是绝对路径。与录制配置相比，回放配置的 capability 块改了两处：

```yaml
replayability: { replayability_id: pinned, random_seed: 7, base_time: from_archive }
network_source:
  source_id: archive
  shift_dates: none                    # all：日期整体平移，配 base_time: run_start
  matcher: { site_rules: configs/replay_rules }
  miss_policy: { miss_policy_id: strict }
```

`trace_writer.archive` 里用 `browser.replay_log` 代替 `browser.network`，其余归档器不变。`browser.replay_log` 记回放日志，回放没有它就被拒；`browser.network` 只录线上流量，回放没有自己的线上流量（送出的每个字节都已经在源归档里），所以它在 `archive` 来源下被拒，报错点名 `browser.replay_log`。反过来，`live` 来源绑定 `browser.replay_log` 也被拒，因为没有回放可记。这几条都在 `prepare()` 里报，`--dry-run` 也一样。

- `prepare()` 打开源 run（只读已提交的部分），拿它的环境指纹比较本次配置（setup 启动 Chrome 之前再比一次）：档位和参数（`base_time` 除外）、shim、viewport、启动开关摘要。有一项不符就拒绝运行，每条点名两边的值。启动开关摘要只覆盖 provider（`local_chrome`）拼出的启动参数，调试端口和 profile 目录换成占位符，网络路由开关两边都去掉，可执行文件路径和 Chrome 版本都不在里面。`headless: true` 会加上 `--headless=new`，所以只改 `headless` 也会不符；要看着浏览器回放，录制和回放都得写 `headless: false`，也就是重新录制。Chrome 版本不同、档位没固定 UA 而浏览器 UA 不同、脱敏策略不同只警告，写进回放 run 的 `env/run.browser.json` 的 `network_source.warnings`。
- `isolation: required`（默认）给 `local_chrome` 加 `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost` 和 `--no-proxy-server`，并拒绝会绕过它们的旋钮：`proxy_server`、`proxy_bypass_list`、`extra_args` 里的路由开关、不是 `about:blank` 的 `startup_url`。写 `isolation: none_acknowledged` 时只靠 Fetch 拦截，preconnect 和 WebSocket 会连到真实网络。
- `base_time: from_archive` 让页面时钟从录制时的 T0 开始，只能配 `archive` 来源。`shift_dates: none`（默认）原样送出录下的 `Date`、`Expires`、`Last-Modified` 和 cookie 过期时间。
- 匹配默认用 `levels: [exact, normalized]`，模糊一级（`fuzzy`）要显式打开；规范化会去掉 `utm_*`、`fbclid` 这类易变参数。每条录下的记录按录制顺序只送一次，用完之后重复最后一条，并在日志里记 `exhausted`。站点规则放在 `configs/replay_rules/<domain>.yaml`，写法见[它的 README](../configs/replay_rules/README.md)。
- `strict` 处理归档里没有的请求：主 frame 的文档 `Fetch.failRequest` 并标 `diverged`；子 frame 的文档同样失败；beacon、ping、CSP 报告、内置的分析类主机、`extra_benign_hosts` 和站点规则 `synthesize_204` 命中的回 204；其余子资源回 404。回放编出来的应答都带 `x-replay-miss: 1` 头。归档里没有的 CORS 预检合成 204。

回放 run 的 `env/` 里没有 WARC，每步一个 `env/steps/NNN/browser.replay_log/replay.jsonl`；`env/run.browser.json` 多一个 `network_source` 块（`source_run_id`、`source_fingerprint_sha256`、`site_rules_sha256`、matcher、`miss_policy_id`、`shift_dates`、`isolation`、`warnings`）。回放 run 不能再当回放或分叉的源，要回放就用原来的录制。读回放日志：

```python
from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.replay.replay_log import diverged_steps, iter_replay_log

reader = open_archive("traces/<replay-run>")
print(diverged_steps(reader))                   # 有主 frame 文档 miss 的步
for step, entry in iter_replay_log(reader):     # 含收尾批次（step 为 None）
    if entry.kind == "miss":
        print(step, entry.reason, entry.request_class, entry.frame, entry.url)
```

每行是一个 `ReplayLogEntry`。`kind` 是 `hit`、`exhausted`、`synthetic`（合成的预检）、`miss` 或 `summary`（同一次收集里完全相同的条目超过 `log_repeat_limit` 之后折成一条，`count` 是折掉的条数）。命中带 `record_id` 和 `level`。miss 带 `reason`（`no_match`、`too_large`、`body_unavailable`、`range_uncovered`、`websocket`、`unservable`）、`request_class`（`document`、`benign`、`subresource`）、`frame`（`main`、`sub`）、`answer` 和 `status`、`match_key`，以及最近的 3 个候选 `candidates`。

## 分叉

`configs/flywheel/branch_ui_tars_archive.yaml` 把录下的 run 放回第 t 步之前的状态，让 teacher（这份配置的 `llm`）重新决定第 t 步。任务来自 `task_loader: trace_branches`：`data_dir` 下每个通过筛选的源 run、每个选中的步展开 `branches_per_step` 个任务，任务 id 是 `<source_run_id>/s<t>/b<i>`。设计见 [architecture_design.md](architecture_design.md) §4.1 `BranchLoop`、§4.10 `trace_branches` 和 §4.14 "恢复：重定位与校验"。`trace_branches` 没有评测加载器，所以传 `--no-eval`；不传而配了 `evaluator:` 时，开跑前就会被拒。

`branch` loop 需要 loop 输入 `checkpoint`，即源 run 目录和要分叉的步。只有 `trace_branches` 会把它放进每个任务的上下文，同时放进去的还有源 run 的 `start_url` 和 `replay_archive`（源 run 目录）。`spectragent-run` 没有能提供 `checkpoint` 的参数，用它跑分叉配置（包括原路径回放配置）时，`prepare()` 做完槽位校验就报 `IncompatibleLoopInputError`，加 `--dry-run` 也一样。

```bash
uv run spectragent-bench --config configs/flywheel/branch_ui_tars_archive.yaml --list     # 只列任务 id
uv run spectragent-bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
```

```yaml
task_loader:
  - benchmark_name: trace_branches
    data_dir: traces/recorded          # 源 run 所在的目录
    step_selector: { selector_id: all }
    branches_per_step: 1
    agent_outcome: any                 # done | max_steps | error | timeout | vetoed：源 run 的 agent 怎么结束
    on_unbranchable: raise             # skip：跳过并记下原因
```

源 run 由 `sources` 选出。它是相对 `data_dir` 的 glob 列表，默认 `["*"]`，即 `data_dir` 的直接子目录；匹配到的目录里没有 `manifest.json` 就不算 run，直接忽略。一个都匹配不到时（`data_dir` 不存在也一样），加载器只记一条 warning（`trace_branches: no recorded run under ... matches ...; no branch tasks`），展开出 0 个任务，不报错，所以开跑前先用 `--list` 确认任务 id。`data_dir` 不要指向 `traces/`：发布的回放配置也写到那里，不能分叉的回放 run 会混进源里；`traces/recorded` 这样的子目录本身没有 `manifest.json`，默认的 `*` 匹配到它也只会忽略，它下面的录制一个都找不到。只分叉一个 run 时，把 `sources` 写成 `["<那个 run 的目录名>"]`，或者给 `spectragent-bench` 传 `--task-id <source_run_id>/s<t>/b<i>`。

步号从 0 开始。加载器从第 0 步往后扫描源 run 已提交的步，遇到第一个缺失的步号、或超出 trace 完整到的那一步（manifest 的 `complete_through`）就停；扫到的步里 `origin: policy`、`error_phase` 不是 `observe` 的是候选步。trace 从没关闭（manifest 还是 `writing`：录制还在跑，或进程死在收尾之前）的源一个候选步都没有，跳过并记原因。这一条是硬条件，关不掉：一个在第 t 步收证据失败而停下的录制（`incomplete`），仍然贡献第 t 步之前的每个检查点；只是收尾失败的录制一个检查点也不少。`step_selector` 决定在哪些步分叉：

| `step_selector` | 选哪些步 |
|---|---|
| `all` | 每个候选步 |
| `every_k`（`k`、`offset`） | 步号 t 满足 `t >= offset` 且 `(t - offset) % k == 0` 的候选步。按步号数，不按在候选里的位置：不是候选的步号直接跳过，不记原因。例如 k=2、offset=1 时，在 1、3、5、7……里选是候选的那些 |
| `explicit`（`steps: [2, 5]`） | 写了的步；某个源里不是候选的步跳过并记原因 |
| `last` | 每个源最后一个候选步 |

`agent_outcome`（读源 run 的 `result.json`，默认 `any`）和 `require_eval`（`{evaluator_id, label: 1, slug: null}`）筛选源。有些源不能分叉：读不回来的（比当前 trace 格式旧的也读不回来，只能重录）、本身是分叉或回放 run 的、没有 `browser.network` 归档或环境指纹的、拿不到 setup context 的、与别的源共用 `run_id` 的。`on_unbranchable: raise` 时在开跑前一次列出全部，`skip` 时跳过并记原因。`on_unbranchable` 只管整个源：一个步不是候选，不算不可分叉；一个候选步都没有的源也只是跳过并记原因。

Loop 一侧：

```yaml
agent_loop:
  loop_id: branch
  max_steps: 30
  max_actions_per_step: 1          # 必须与源 run 相同
  continue_steps: 0                # 0：只决定第 t 步；N：之后再走 N 个 ReAct 步
  restorer:
    restorer_id: prefix_replay
    verify: strict                 # warn：记下不通过的检查，继续
    screenshot_max_distance: 5     # 两张截图 64 位 dHash 的汉明距离上限
    pace: none                     # recorded_llm_latency：执行动作前等源 run 当时等模型的时长
```

`branch` loop 默认 `terminal_observation: true`。capability 的 `retarget_tolerance_px`（默认 2.0 CSS px）决定录下的元素在恢复出的页面里偏移多少还能被认出。

一个分叉 run 按这个顺序执行：

1. 开跑前对照源 trace：`agent_id`、闭集的 agent 旋钮（`prompt_template`、`coordinate_space`……）、memory、观测与动作的协商结果都要相同，源 loop 必须是 `react`，源 run 本身不能是分叉 run。不符就抛 `IncompatibleCheckpointError`，点名两边的值。teacher 的模型和采样参数可以与源 run 不同。
2. 从源 trace 重建第 t 步之前的上下文，teacher 看到的与源 agent 当时看到的相同。录下的每一次模型回复都过一遍 agent 的解析器，并与记录核对。
3. 浏览器回放源 run 的归档，`prefix_replay` 逐步重新执行录下的动作（元素 index 按位置和签名重新找到，tab id 按顺序和 URL 映射），并校验每一步。恢复出的步记成 `origin: restored`，带这一步的 `restoration` 检查。第 t 步（检查点）的观测也要校验，结果记在第 t 步的策略步上；strict 下它不通过时 teacher 不会被调用。
4. 第 t 步和之后的 `continue_steps` 步由 teacher 决定（`origin: policy`），最后再观测一次（`origin: terminal`）。

`verify: strict` 下第一个不通过的检查就结束 run：记一条 `error_phase: restore` 的恢复步，结果是 `agent_done=error`、`env_status=failed`，`error` 点名 `RestorationDivergedError`。`warn` 只把检查结果写进那一步，继续往下走。每个分叉是独立的任务，重新组装、启动新的浏览器和新的 profile，分叉之间不共享状态。一个分叉的策略步数是 `1 + continue_steps`（不超过 `max_steps`），每个策略步至少调用一次模型；`AgentMetrics.steps` 只数策略步。

分叉 run 写到 `traces/branches/<source_run_id>-s<t>-b<i>_<8 位随机串>/`，`config.json` 的 `branch` 记着源 run、检查点和分支编号。分叉与原路径回放配置的 `trace_writer.archive` 和回放配置一样用 `browser.replay_log`，每个分叉 run 的回放日志在它自己的 `env/steps/NNN/browser.replay_log/replay.jsonl`。读校验结果：

```python
for record in RunTrace(Path("traces/branches/<run>")).step_records:
    check = record.restoration
    print(record.step, record.origin, check and check.verdict, check and check.reasons)
```

### 原路径回放

`configs/flywheel/same_path_ui_tars_archive.yaml` 是同一个分叉 sweep 的一种设置：`step_selector: last`、`continue_steps: 0`、`verify: warn`、`branches_per_step: 2`、`agent_outcome: any`、`on_unbranchable: skip`。每个任务把一个录制恢复到它最后一个候选步，之前每一步都重放并校验；teacher 只在最后一步决定一次，每个任务一次模型调用；同一个源跑两遍，用来量回放确定性。度量报告的"原路径回放保真"就来自它。发布配置的 `data_dir` 已经是 `traces/recorded`，run 写到 `traces/same_path`：

```bash
uv run spectragent-bench --config configs/flywheel/same_path_ui_tars_archive.yaml --no-eval
```

## 导出 SFT 数据

这一期交付的是 teacher 轨迹（录制、回放、分叉）。学生一侧是暂定的，挪到下一期再定；导出代码冻结，不加功能，下面写的是它冻结时的用法（[architecture_design.md](architecture_design.md) §4.15）。

`spectragent-export` 只读已提交的 trace，不起浏览器，也不调模型。学生是一个普通的 af agent：`configs/flywheel/ui_tars_student.yaml`（UITarsAgent，`coordinate_space: normalized_1000`）只被组装，从不运行；每个样本由它自己的 prompt 构建与 memory 渲染，目标轮由它的 `format_react_output` 写出，所以训练格式就是它服务时的格式（设计见 [architecture_design.md](architecture_design.md) §4.15）。

```bash
uv run spectragent-export --config configs/flywheel/export_sft.yaml
```

导出 YAML 有一个 `exporter:` 块，可以再加 `eval_loader:` / `task_loader:` 块（见下面的去污染），没有 agent 和 environment。v1 唯一的导出器是 `exporter_id: openai_messages`，`sample: per_step` 与 `image_store: files` 也只有这一个取值。发布的 `export_sft.yaml` 不按评测结果筛（`filters.require_eval: null`），也不去污染（`decontamination.against: []`），所以直接用在 online_mind2web 的录制上就能导出；teacher 失败的轨迹也会进数据集，每个样本带着 `labels.step_status` 和判定标签，训练时可以再筛。开跑前按自己的数据改这几处：

- `sources_root` 和 `sources`：发布值是 `sources_root: traces`、`sources: ["recorded/*"]`。`sources` 是相对 `sources_root` 的 glob，按路径排序；`sources_root` 写相对路径时按工作目录解析。要导出分叉 run 就加上它们的目录，例如 `["recorded/*", "branches/*"]`，原路径回放的 run 在 `same_path/*`。分叉 run（含原路径回放的 run）的源 run 也必须在本次读到的源里，否则被筛掉，原因记为 `branch source '<id>' not among the sources`。列进来的录制 run 自己也会产出样本，没有只导出分叉的开关。
- `filters`：`agent_outcome`（默认 `any`；读源 run 自己的 `result.json`）；`require_eval`（默认和发布值都是 `null`，不读判定；要只留判分通过的 run 才写 `{evaluator_id, label: 1, slug: null}`，这要求录制时评测过）；`exclude_restoration_diverged: true`，有一步的恢复检查判为 `warn` 或 `diverged` 的 run 整个排除；`max_document_misses: 0`，回放日志里主 frame 文档 miss 超过这个数的 run 排除（`null` 不限，没有回放日志的 run 算 0）；`exclude_sites`，主机名后缀匹配，度量报告会给出这份名单。对分叉 run，`agent_outcome` 看它自己的结局，`require_eval` 看它的源 run 的判定。不管筛选怎么写，trace 从没关闭的源都跳过，一个源只导出它的 trace 完整到的那些步。用 `verify: warn` 跑的原路径回放 run 只要有一步的检查没通过，就整个被 `exclude_restoration_diverged` 排除。
- `decontamination`：`against` 列出学生将来评测用的 benchmark（`[{benchmark: <名字>, subset: <子集>}]`），只查列出来的这些，从不从评测器或判定推断。默认 `[]`：不去污染，manifest 记 `decontaminated: false`；`null` 不是列表，组装时报错。把录制所用的 benchmark 自己列进去会把这些录制全部排除（`ExportEmptyError`）。`match` 取 `task_id`、`site_instruction` 或 `either`。去污染和参考答案泄漏扫描经导出 YAML 自己的 `eval_loader:` / `task_loader:` 块读基准数据，没配的 benchmark 用注册加载器的默认值。
- `split`：`by: site`（源任务 `start_url` 的主机）或 `by: task`，从不按步切，同一组分叉总在同一个切分里。
- `translator`：`identity` 原样使用 teacher 执行过的动作，UI-TARS 录下的本来就是坐标动作。`index_to_coordinate` 把带元素 index 的 `browser.click`、`browser.type` 换成元素可见部分上的坐标动作，其余带 index 的动作不可译（`no_counterpart`），不带 index 的原样保留。它的 `occlusion: check` 要确认换算出的点没有被别的元素盖住，证据按优先级是 cleaner `skyvern_hit_test` 录下的命中测试、`browser.dom_raw` 的原始 DOM、BrowserUse cleaner 录下的 paint order（只认得出部分遮挡）。三样都没有时抛 `OcclusionEvidenceMissingError`；写 `occlusion_unknown: untranslatable` 则把这一步当作不可译。`occlusion: "off"` 要加引号。`untranslatable` 决定不可译的步怎么处理：`drop_step`（结束当前段）、`truncate`（结束这条轨迹）、`keep_as_text`、`fail`。
- `thought_source: teacher | none` 与 `thought_index_refs: drop | keep` 决定 thought 从哪来、提到元素 index 的 thought 是否丢掉。
- `length: { max_tokens: 65536 }`：学生每轮都保留截图，30 步的整段在 1280×720 上光图像约 36k token。有记录超出预算时，导出失败并点名这条记录，从不截断；训练器的上下文更短时，换一个 `max_steps` 更小的学生。
- `output_dir`：发布值 `datasets/sft_v1`，相对工作目录；已存在就拒绝。

学生对录制有要求。学生协商出的观测 fragment 必须是源 run 录下的子集，例如学生要 DOM 而录制是纯视觉时报 `IncompatibleFragmentError`；截图管线（viewport、格式、DPR）也要与录制一致，否则报 `StudentCaptureMismatchError`。用纯视觉 agent、`max_actions_per_step: 1` 录制不会丢样本；一步的动作数超过学生的 `max_actions_per_step` 时，这一步的不可译原因是 `exceeds_action_budget`。

样本是至少执行了一个动作、没有出错的策略步。动作失败的步照样导出，标 `labels.step_status: action_failed`。分叉 run 导出它的策略步，上下文是源 run 的前缀；同一组里检查点步动作相同的样本标 `duplicate_of`，不删。产物先写进 `<output_dir>.partial/`，图片数、坐标范围、参考答案泄漏、凭证形状和长度的检查全部通过才改名：

| 文件 | 内容 |
|---|---|
| `train.jsonl`、`validation.jsonl` | 每行 `{"id", "messages"}`，assistant 轮带 `weight`（目标轮 1，较早的轮 0）；只写有样本的切分 |
| `train.samples.jsonl` 等 | 同序的 canonical 样本和渲染元数据（来源、翻译记录、量化误差） |
| `images/<sha256><后缀>` | 截图，每张只存一份 |
| `manifest.json` | 格式 `af-sft-2`，学生配置摘要，每个源与被跳过、被筛掉的原因，按原因的丢弃数，切分、去污染（`decontaminated`、`against`）与泄漏扫描的结果（`compared` 只数真的比过的样本；没比的按原因记在 `no_eval_loader` 和 `no_reference_answer`，online_mind2web 没有参考答案，它的样本都在后者）；没有墙钟字段和绝对路径。度量报告经同一个模型（`schemas/dataset.py::DatasetManifest`）读它，键对不上时点名文件报错 |

同样的输入导出两次，输出逐字节相同。CLI 打印每个切分的样本数和按原因丢弃的步数；失败时退出码非 0，什么也不写。要评测训练出来的学生，复制一份学生配置，把 `llm` 指向训练好的模型，再加上 `trace_writer` hook；导出拒绝带 hook 的学生配置，所以评测用的那份单独放。

## 度量报告

`spectragent-flywheel` 把前面几段串成一条链，依次是录制、对照组（可选）、原路径回放、分叉、两次导出，最后算一份报告（设计见 [architecture_design.md](architecture_design.md) §4.16）。链在进程内依次调用 `spectragent-bench` 和 `spectragent-export` 的 `main`，每段的运行方式与手动执行相同。它只改各段 YAML 的路径字段（`trace_writer.trace_dir`，`trace_branches` 的 `data_dir`，导出器的 `sources_root`、`sources`、`output_dir`），把改好的配置写到 `<root>/configs/` 再运行。

链规格是一份普通 YAML（pydantic 模型 `ChainSpec`，多余的键被拒），ConfigComposer 不读它。不要把它放进 `configs/flywheel/`，那里每个文件都要能组装并通过 `prepare()`。

```yaml
name: first-live
root: traces/chains/first-live      # 规格里的相对路径都相对工作目录
evidence: live                      # live | rehearsal
tasks: {benchmark: online_mind2web, subset: All, task_ids: [<ID>, <ID>], limit: null}
record:    {config: my/record.yaml, timeout: 600, evaluate: false}
control:   {config: my/control.yaml, timeout: 600}        # 可选：录制配置加 archive: []
same_path: {config: my/same_path.yaml, timeout: 600}
branch:    {config: my/branch.yaml, timeout: 600}
export:    {config: my/export.yaml, repeat: 2}            # 2：顺带量导出确定性
report:    {planted: [{label: teacher api key, env: OPENAI_API_KEY}]}
```

`my/` 下是从 `configs/flywheel/` 复制来改过的文件。

```bash
uv run spectragent-flywheel run --spec chain.yaml                     # 规格里配了的每一段
uv run spectragent-flywheel run --spec chain.yaml --stages record     # 只跑列出的段，按链的顺序
uv run spectragent-flywheel report --spec chain.yaml                  # 只读，随时重算
uv run spectragent-flywheel report --root traces/chains/first-live --evidence live --out /tmp/report
```

`--stages` 可写的段名是 `record`、`control`、`same_path`、`branch`、`export`、`report`，用逗号分隔。

- `root` 放在 git 忽略的目录下，例如 `traces/` 或 `tmp/`；`runs/` 不在 `.gitignore` 里。
- 布局：`<root>/traces/{record,control,same_path,branch}/`，`<root>/datasets/{first,second}/`，`<root>/report/{report.json,report.md}`，`<root>/configs/`，`<root>/evals/<段>/`（写了 `evaluate: true` 的段），`<root>/chain.json`（每段的 argv、退出码、产生的 run 和起止时刻）。
- `same_path` 和 `branch` 的源是 `<root>/traces/record`，所以 `record` 先跑。重跑一个运行段会在已有的 run 旁边再加 run，并记一条 warning（`already holds runs; this stage adds to them`）。重跑 `record` 之后，`same_path`、`branch`、导出和报告都会把新旧录制一起读进来；想干净地重做，换一个 `root`。
- 导出从不覆盖：选了 `export` 时，只要 `repeat` 要写的 `datasets/first`（`repeat: 2` 时还有 `datasets/second`）有一个已经存在，链就在任何一段开跑之前报 `ChainConfigError`。要在链里重导，先删掉这些目录，再跑 `--stages export,report`。也可以复制 `<root>/configs/export_first.yaml`（链实际用的导出配置，路径都已写好），改掉 `output_dir` 和要改的筛选，用 `spectragent-export` 导到别处。只想重算报告就用 `spectragent-flywheel report`。
- 链用的导出 YAML 用 `filters.require_eval: null`（链默认不评测）和 `decontamination: {against: []}`（源本来就是被测 benchmark 的任务）；这两个就是默认值，发布的 `export_sft.yaml` 也这么写。
- 运行段退出码是 0 或 1、并且写出了新的 run，就算跑完；个别任务的基础设施失败是要度量的数据。其余情况链停在这一段，报 `ChainStageError`，之前的产物保留；退出码 2 表示这一段的 pre-flight 拒绝了它的配置，一个任务也没跑（原因在 stderr）；退出码 3 表示一个 guard 否决了这一段（比如录制撞上风控页），sweep 停在被否决的任务，这一段的 run 不会喂给下一段。
- `report --root` 没有规格，也就没有植入值，隐私一项的结论是 `insufficient`。

`report.json` 的 `metrics`：

| 键 | 回答的问题 | 怎么算 |
|---|---|---|
| `archive_overhead` | 开着归档每步多花多少时间和字节 | `record` 与 `control` 每步环境侧时间（`duration_ms` 减 `llm_latency_ms`，策略步，从第 1 步起）的 p50、p95 之差，没有对照组或两组档位不同时为 `null`；`env/` 去重前后的每步字节 |
| `body_completeness` | 录下的交换有没有 body | `index.jsonl` 每条记录的最终状态：有 body，不需要 body（`redirect`、`websocket`），明确没有（`too_large`、`incomplete`） |
| `same_path_fidelity` | 原样重放时，恢复出的每一步与录制是否一致（按站点） | 恢复器判 `ok` 的步的占比；严格版另要求 URL 不变（量得到时）且截图 sha256 与源 run 同一步相同；每个 run 第一个不通过的步与原因 |
| `restoration` | 分叉的前缀恢复得对不对 | `branch` 段校验通过的占比；不通过的按原因计数，重定位失败按 `not_found`、`ambiguous` 计 |
| `branch_coverage` | 分叉之后是否还留在归档里 | 检查点及之后没有主 frame 文档 miss 的分叉占比；其余 miss 按动作、resourceType、host、原因分布 |
| `replay_determinism` | 同样的输入回放两遍，结局是否一样 | 同一源 run、同一检查点、执行动作相同的分支两两比较终止截图的 sha256，另比较检查点步恢复出的截图 |
| `export_determinism` | 同样的 run 导出两遍是否逐字节相同 | `datasets/first` 与 `datasets/second` 的目录树摘要；`repeat: 1` 时为 `null` |
| `privacy` | 凭证有没有落盘 | `<root>/traces` 和 `<root>/datasets` 下每个文件（gzip 先解开）里每个植入值出现的次数，以及凭证形状的命中数；`env/` 也算，`storage.json` 里原样保存的 cookie 与 Web Storage 值会被数到 |

分母为 0 的值是 `null` 加原因，从不写成 0。`sections` 另有：按站点的统计和剔除名单（被 anti-bot 拦截、原路径保真低于 0.8、录制在第 0 步环境就失败的站点；模型调用失败从不让站点进名单），anti-bot 拦截（文档应答 403、429 或 503，且带 `cf-mitigated: challenge` 或 `server: cloudflare` 加 `cf-ray`），重复分叉率，POST 分叉的 miss，按角色和类别的存储字节与 gzip 压缩比，每个任务的字节和 10、20、300 个任务的投影，导出产出。`decisions` 是四个待决问题的建议：`q1_miss_policy`（零文档 miss 的分叉占比不低于 0.9 就保留 `strict`），`q4_coverage`（分叉的文档 miss 里 GET 未录 URL 的占比达到 0.5 就扩大录制时的覆盖），`site_rules`（一个 host 有 2 次以上近邻 miss 就建议生成 `ignore_query` 规则供人审），`q7_retention`（容量）。

每条建议是 `{status, verdict, rule, evidence, basis}`。`status` 在 live 证据上是 `supported`，演练上最多 `partial`，没有数据时是 `insufficient`、`verdict` 为空。`rule` 写出所用的阈值常量，常量都在 `spectragent/flywheel/report.py`；`evidence` 是指向 `report.json` 里具体值的 JSON pointer 和分母。`report.md` 由同一个模型渲染，列出每个角色被跳过的 run，但各项度量自己的 `skipped`（某项读不到证据而略过的 run）只在 `report.json` 里。报告只读链根下已提交的 trace 和数据集，同样的输入算两次逐字节相同。

链在报告之前导出，不会套用报告给出的剔除名单。`sections.sites.yaml` 是 `filters.exclude_sites` 形状的片段：把它放进导出 YAML，换一个 `output_dir`，手动再导一次。

## 隐私

框架与 browser 各自负责哪一部分，见 [architecture_design.md](architecture_design.md) §5.6 与 §4.14 "隐私"。威胁模型是"只有我"：trace 与 `env/` 只在你自己的机器上；框架守两道门，一是发给 LLM 服务商的提示词里不带秘密，二是导出数据集时把住。

| 内容 | 默认 | 开关 |
|---|---|---|
| 请求头 `Cookie`、`Authorization`、`Proxy-Authorization` | 在每个来源剥离，从不落盘；台账只记名字 | `browser.network` 的 `strip_request_headers` 追加别的头（大小写不敏感） |
| POST body（urlencoded、multipart、JSON） | 字段名含 `password`、`passwd`、`pwd`、`otp`、`token`、`secret`、`card`、`cvv` 的值换成 `<redacted>` 并记台账；回放时线上请求先过同一个函数再算匹配键，照样能命中 | 无 |
| 响应头 `Set-Cookie` | 保留，回放的 cookie jar 要用；`browser.network` 因此声明 `contains_session_state` | 无 |
| cookie 与 local / session storage 的值 | `storage.json` 原样保存（名字、值、域、路径、过期时间和标志）；会话 token 就在里面，所以 `browser.storage` 声明 `contains_session_state` | 无 |
| 原始 DOM 里的输入值 | 敏感控件（密码框、一次性验证码、卡号等）的 `value`、它下面 shadow root 里的文本、DOMSnapshot 的输入值、无障碍树的 value，在写盘之前替换 | 无 |
| DOM 观测（提示词与 `browser.dom` fragment） | BrowserUse 清洗时按同一条规则把敏感控件的 `value` 和它下面的文本（包括键入的值）换成 `<redacted>`；Skyvern 本来就不带 | 无 |
| 键入的文字 | 原样记在动作参数和 LLM 记录里；动作结果的 `typed_into_sensitive_field` 记下文字是否进了敏感控件（`true` / `false`，看不见时 `null`），提示词里不显示 | 无 |
| 从非新建 profile 启动的浏览器（登录态） | 绑定了归档器就拒绝；pinned 一律拒绝 | 每个归档器都写 `allow_session_state: true`，只能配 `"off"` 档位 |
| 配置和任务 setup context | `config.json` 只记标为 trace-safe 的字段，其余写 `"<omitted>"`；setup context 按值脱敏（凭证形状的键、URL 里的 userinfo 和凭证形状的查询参数） | `trace_writer` 的 `record_secrets: true` 原样记录；这样的 run 永远不会被导出 |
| `env/` 归档 | 永不导出，数据集里只有截图、动作、thought 和引用 | 无 |
| 导出写出的每个文本字节 | 过凭证形状的正则，命中就整个导出失败，不留输出 | 无 |

每个 run 的隐私标记在 `config.json` 的 `env_evidence_privacy` 和指纹的 `privacy` 块里（`contains_session_state`、`fresh_profile`、`level`）。凭证形状的正则 `SECRET_PATTERNS` 在 `spectragent/core/trace/secret_patterns.py`，有三处使用：secret guard 测试、导出的凭证扫描、度量报告的隐私一项。secret guard 用真实的组装配置植入凭证形状的值，跑真实的 TraceWriter（带一个归档用的假 capability），再扫描 run 留下的每个文件（gzip 先解开）：默认设置下一个都不能出现，`record_secrets: true` 时必须全部找到。改了脱敏相关的代码就跑它：

```bash
uv run pytest tests/test_core/test_trace/test_secret_guard.py
```

已知的残余：正则认的是凭证的形状，teacher 键入的、不像凭证的密码会原样成为训练目标（键入动作的结果已标出它进了敏感控件，导出还不用这个标记）；WebSocket 帧的内容和 URL（含查询参数）原样记录；`browser.extract_content` 读一个元素时返回它的原始 HTML，页面写进标记的密码 `value` 会跟着进提示词。数据来源请选无需登录的公开站点。

## 在真实站点上跑

真实站点上的每一段都会花 LLM token。开跑之前先定下：模型路由（model id 和 `coordinate_space`）、任务列表、每段的 token 上限、要不要对照组、要不要评测、磁盘预算。

1. 路由与密钥。`spectragent-bench` 和 `spectragent-flywheel` 都不读 `.env`（`spectragent-run` 会读），先在 shell 里导出：

   ```bash
   set -a; . ./.env; set +a
   ```

   密钥只放在环境变量里，不要写进 YAML 的 `api_key`：链把每段的 YAML 原样复制到 `<root>/configs/`，隐私一项不扫描那个目录。路由写在 `llm` 块（`model_type`、`model`，需要时 `base_url`）；`OPENAI_BASE_URL` 只在 YAML 没写 `base_url` 时生效。`coordinate_space` 要与模型实际输出的坐标网格一致，Qwen-VL 这类在 0 到 1000 网格上回答的模型用 `normalized_1000`。录制、原路径回放、分叉三份 YAML 的 agent 块必须相同，分叉在开跑前核对；teacher 的模型可以换，原路径回放通常用录制时的模型。
2. 选任务。先取 10 个，只跑录制、原路径回放和报告，看保真度和花费；再扩到 10 到 20 个任务跑整条链。任务要无需登录、公开、不被 Cloudflare 拦截。online_mind2web 的每个任务有 `task_id`、`task_text`、`start_url`、`difficulty` 和 `metadata.reference_length`，可以离线挑：先挑 `easy`，每个可注册域名只取一个任务，跳过任务文本要求账号的，跳过已知重度反爬的站点。会不会被拦截离线判断不了，被拦截的站点会出现在报告的 anti-bot 一节和剔除名单里，代价是一次录制。提交表单（POST）的任务在回放和分叉里必然 miss。
3. token 上限。链本身没有上限，所以按段跑，段与段之间查已经花掉的量。每个任务的模型调用数：

   | 段 | 每个任务的调用数 |
   |---|---|
   | `record`、`control` | 策略步数 S，不超过 `max_steps` |
   | `same_path` | `branches_per_step`（发布值 2），都在最后一步 |
   | `branch` | 选中的步数 × `branches_per_step` × (1 + `continue_steps`) |

   UI-TARS 的 memory 保留每一张截图，第 k 步的调用带 k+1 张截图。单张 1280×720 截图算多少 token 因路由而异：先录一个任务，读它记下的用量，再定上限。按这种算法粗估（没有实测）：gpt-4o 上 10 个 easy 任务的录制加原路径回放约 0.2 到 0.6 M token；20 个任务再加分叉（`every_k` 取 k=2，每步 3 叉）约 0.9 到 2.7 M token；对照组相当于每个任务再录一次。查一段已经花掉的 token（每个 run 的 `result.json` 里有 `metrics.usage`）：

   ```python
   from pathlib import Path

   from spectragent.core.trace import RunTrace

   total = 0
   for run in sorted(Path("traces/chains/first-live/traces/record").iterdir()):
       if (run / "manifest.json").is_file():
           result = RunTrace(run).result
           usage = result.metrics.usage if result else None
           total += usage.total_tokens if usage else 0
   print(total)
   ```

4. 按段跑，每段之后重算报告（只读，不花钱），先看 `inputs.roles.<段>.model_failures` 再继续：

   ```bash
   uv run spectragent-flywheel run --spec chain.yaml --stages record
   uv run spectragent-flywheel report --spec chain.yaml
   uv run spectragent-flywheel run --spec chain.yaml --stages control      # 可选
   uv run spectragent-flywheel run --spec chain.yaml --stages same_path
   uv run spectragent-flywheel run --spec chain.yaml --stages branch
   uv run spectragent-flywheel run --spec chain.yaml --stages export,report
   ```

   以模型调用失败结束的 run（429、超时、被拒的 schema，`error_phase: llm`）列在 `model_failures` 里。它们不会让站点进剔除名单，但照样计入各项度量：teacher 调用失败的分叉算作没有文档 miss 的分叉，会抬高 `q1_miss_policy` 依据的占比；原路径回放在最后一步失败的 run 没有终止截图。预算里给重跑这些任务留出余量。
5. 磁盘。每次模型调用的消息文件都内联了之前的每一张截图，`messages/` 随步数平方增长，此外还有 WARC 里的 body 和原始 DOM。真实页面上每个任务（各段合计）粗估 50 到 150 MB；报告的存储和保留期两节给出实测的字节数。
6. 评测默认关闭（`evaluate: false`，即 `--no-eval`）。给 `record` 段写 `evaluate: true` 并在录制 YAML 里加 `evaluator:` 块，每个录制的任务会多一次评测器调用，判定写到 `<root>/evals/record/` 和每个 trace 的 `evals/`。
7. 报告之后，把 `sections.sites.yaml` 放进导出配置的 `filters`，手动再导一次。

## 排错

- `Input tag 'False' found using 'replayability_id' does not match any of the expected tags: 'off', 'pinned'`：YAML 里写了裸的 `off`，改成 `replayability_id: "off"`。翻译器的 `occlusion: "off"` 同理。
- 校验错误 `environment.0.capabilities.0.replayability`（或 `network_source`）下面写着 `Field required`：capability 块缺必填的受体块，补上 `replayability` 和 `network_source`。`network_source: archive` 块里还要写 `miss_policy: { miss_policy_id: strict }`。
- `UnresolvedWiringError: environment[0].capabilities[0].archiver: ...`：归档器写进了 capability 块，挪到 `trace_writer.archive`。
- `IncompatibleArchiveError`：`trace_writer.archive` 里同一个键写了两次、两个 hook 都带了归档列表，或者环境里没有提供这个归档器的 capability。每个归档器只写一次，只写在一个 trace_writer 上；消息点名请求方和环境实际提供的归档器。
- `BrowserCooperationError: BrowserCapability cannot run this configuration (N problem(s); ...)`：每一行是一个协作问题，点名两边。常见的有：云 provider 没有 `NETWORK_CAPTURE`，换 `local_chrome`；`browser.network require_replayable=true ↔ replayability: off`，录制改用 pinned，或给 `browser.network` 写 `require_replayable: false`；profile 不是新建的，去掉 `user_data_dir` 或 `unset CHROME_USER_DATA_DIR`；pinned 与 provider 的旋钮冲突；`base_time: from_archive` 配了 `live` 来源；`network_source: archive ↔ task context: no replay_archive`，给 `--replay-archive`（只适用于 react 回放配置），或者经 `trace_branches` 任务运行；`live` 来源却给了 `replay_archive`；回放没有绑定 `browser.replay_log`，或者绑了 `browser.network`（都是把 `browser.network` 换成 `browser.replay_log`）；`live` 来源绑了 `browser.replay_log`；`isolation: required` 与代理或路由开关冲突。
- 回放一致性不符：同一个错误里以 `source run '<id>'` 开头的行，例如 `replayability random_seed=7 ↔ this run's random_seed=1`、`viewport 1280x720 ↔ this run's viewport ...`、`launch_flags_sha256=... ↔ this run's planned launch flags ...`、`the source run is itself a replay`、`recorded no browser.network evidence`、`has no environment fingerprint`。让回放配置的档位参数、viewport 和 backend 块与录制一致，回放原始录制而不是回放 run。只警告的差异看回放 run 的 `env/run.browser.json` 的 `network_source.warnings`。
- `ArchiveOpenError: replay archive ... is not a directory`：`--replay-archive` 要指向源 run 目录，也就是含 `manifest.json` 的那个。`ReplayArchiveError` 说明源 run 的索引行读不回来，或者指向的 WARC 记录对不上，消息点名文件和记录 id；源归档已损坏，重新录制。
- 回放里有 miss：用 `diverged_steps` 找出有主 frame 文档 miss 的步，再看这些步的 `replay.jsonl`。`no_match` 而 `candidates` 里有同 host、同 path、只差查询参数的记录，说明参数是每次访问都会变的值（时间戳、会话 id），给这个站点写 `ignore_query` 规则；文档 URL 本身带每次访问不同的值时，回放必然在那一步发散。`too_large`：body 超过 `max_fulfill_body_bytes`，或录制时就是 `too_large`。`body_unavailable`：录制时 body 没读完。`range_uncovered`：录制没覆盖请求的字节区间。`websocket` 和 `unservable` 见"已知限制"。报告的 `decision_inputs.site_rule_candidates` 汇总了所有回放 run 的近邻 miss。
- 恢复发散：`strict` 下 run 以 `RestorationDivergedError` 结束，`warn` 下那一步的 `restoration.verdict` 是 `warn`。`reasons` 列出这一步所有不通过的检查，按下面的顺序排列，第一个同时记在 `reason`：
  - `document_miss`：自上一次校验以来回放日志里有主 frame 文档 miss，处理同上一条。
  - `url_mismatch`：恢复出的 URL 与录下的不逐字相同，只在录了 `browser.browser_meta` 的 run 上比较。
  - `retarget_not_found`：在 `retarget_tolerance_px` 之内找不到标签、标识属性和文本签名都相同的元素；源 run 当时用的 index 不在录下的 selector map 里（模型编出来的 index）也归这一类。
  - `retarget_ambiguous`：候选不止一个，常见于嵌套的相同元素。
  - `action_failed`：动作的成败与录制不同，或者执行时抛错。
  - `signature_mismatch`：两边可交互元素的签名多重集不同。
  - `screenshot_distance`：恢复出的截图与源 run 同一步录下的截图，dHash 距离超过 `screenshot_max_distance`（实测值在 `restoration.evidence[*].screenshot_distance`）。常见于动画和依赖时钟的内容；异步加载造成的差异可以试 `pace: recorded_llm_latency`。

  每一步的 `restoration.evidence` 记着各项的测量值，`restoration.actions` 记着每个录下动作的重定位方式和结果。值为 `null` 的项不参与判定，所以 UI-TARS 这类纯视觉 run 主要靠截图距离、文档 miss 和动作成败来判。
- `IncompatibleCheckpointError`：分叉配置与源 run 不匹配，在任何 setup 之前报，点名两边：源 trace 从没关闭，或检查点超出它完整到的那一步（`past the step its trace is complete through`），另一个 agent，不同的 `prompt_template`、`coordinate_space` 等旋钮或数字旋钮（如 memory 的 `max_messages`），不同的 memory 或协商结果，不同的 `max_actions_per_step`（`agent_loop.max_actions_per_step`），源 loop 不是 `react`，源 run 本身是分叉 run；开跑之后、任何动作之前还会查种子 prompt 与源 run 是否相同、录下的回复在这个 agent 上能否复现。分叉配置的 agent 块照抄录制配置（llm 一块可以不同）。
- `IncompatibleLoopInputError: Loop 'branch' loop inputs: requires checkpoint`：用 `spectragent-run` 跑了分叉或原路径回放的配置。它们只能经 `spectragent-bench` 的 `trace_branches` 任务运行，检查配置用 `--list`。
- 录制中途失败：录制按 fail closed 处理，两本账分开记。recorder 连接意外断开时，capability 立刻标记自己不存活，观测、动作和收集都抛 `RecorderLostError`；一次收集失败（例如 archive scope 丢过事件）同样让 run 在这一步停下。录制从不在缺少记录的情况下继续：这一步不提交，它的模型调用作为收尾调用落盘，manifest 记 `"status": "incomplete"`，`incomplete_reason` 以 `recording failed at step t:` 开头，`complete_through` 是 t-1；`result.json` 是 `agent_done: error`、`error: "recording failed at step t: ..."`。agent 恰好在那一步说了 done 时，`result.json` 保留 done 和答案，trace 照样是 `incomplete`。之前提交的步完好，可以拿来分叉、导出；要补全就重跑这个任务。
- run 成功、trace 却是 `incomplete`：尾部没记下来（收尾那次收集、收尾补写的欠账、终止观测那一步，或 `result.json` 写不进去），原因在 manifest 的 `incomplete_reason`。不抛错，run 的结局不变；收尾写不进去的东西丢掉，不重试。磁盘一直满着时连终态都写不进去，manifest 停在 `writing`（日志里有一条 ERROR）。
- sweep 的汇总：表头下面三行是三本账的计数（`agent outcome`、`environment`、`trace`），之后每个任务一行；`infrastructure failure: ...` 是环境坏了、模型在 provider 重试之后仍不可用（`the model call failed after the provider's retries`）或 trace 不完整，计入 `infrastructure_failures`，退出码 1。被否决的任务那一行写 `vetoed: ...`，最后一行 `vetoed: a guard stopped the sweep at ...`，退出码 3。
- 导出失败时什么都不写。`ExportEmptyError` 列出每个筛选和丢弃原因的计数，最常见的是写了 `require_eval` 却没评测过、`against` 列了录制所用的 benchmark，或者 `sources` 只列了分叉 run 而没列它们的源 run。`OcclusionEvidenceMissingError` 见"导出 SFT 数据"。`StudentCaptureMismatchError`：学生的截图管线与录制不同。`SampleTooLongError` 点名超长的样本。`CredentialLeakError`、`ReferenceLeakError`：凭证形状或参考答案进了输出。`output_dir ... exists`：换一个目录，或者删掉旧的。
- `spectragent-bench: the pre-flight check refused the configuration for task ...`（退出码 2）：开跑之前，第一个任务的 `prepare()` 拒绝了配置，一个任务也没跑、没建 trace。冒号后面是原来的错误，点名两边：组合问题（`BrowserCooperationError`）、协商（`IncompatibleFragmentError`、`IncompatibleActionError`、`IncompatibleArchiveError`）、loop 输入、任务上下文里没有谁接受的 setup 键。改配置后重跑；只想看哪里不对可以先 `--dry-run`（`spectragent-run`）。
- 链：`ChainConfigError` 在任何一段开跑之前报，原因可能是段 YAML 缺 `trace_writer`、`trace_branches` 或 `exporter` 块（或者有两个），`--stages` 写了不存在或规格里没配的段，或者数据集目录已经存在（怎么重导见"度量报告"）。`ChainStageError` 点名失败的段，`chain.json` 记着每段的 argv 和退出码。

## 已知限制

- 嵌套的 dedicated worker（worker 里再起的 worker）的脚本在 Request 和 Response 两个阶段都不经过 Fetch：录不到，也回放不了。录制时记成 `incomplete`，回放时看得见的记成 `unservable`。
- WebSocket 不回放。录制时帧写进 `websocket.jsonl`；回放时握手记为 miss（`reason: websocket`），隔离下握手直接失败。
- 隔离留着 `EXCLUDE localhost` 这个口子：绕过 Fetch 的流量（preconnect、嵌套 worker 的脚本、WebSocket）如果指向精确的 `localhost`，回放时仍能连到本机的服务。
- 页面定时器发起的导航：观测开始前会等主 frame 在途的跨文档导航提交（最多 10 s），但检查之后才由定时器发起的导航跟踪不到。截图命令有 10 s 的上限，那一次采集失败后重发，这一步因此多花约 10 s。
- 归档开着时，Response 阶段读 body 会把响应扣到下载完成，页面拿不到渐进加载的 HTML 或流式媒体。SSE 和长轮询被扣到 `body_read_timeout_s`，之后记为 `incomplete`。
- pinned 不固定 `performance.now` 和定时器。没有 `Last-Modified` 头时 `document.lastModified` 读真实时钟，不带参数的 `Intl.DateTimeFormat().format()` 也读真实时钟。`base_time: from_archive` 下，页面 JS 设的绝对过期时间的 cookie 可能被 Chrome 丢掉。
- 回放 `"off"` 档位的录制时，时区、语言、DPR 不比较，HTTPS 升级的试探可能显示为主 frame 文档 miss。
- 模糊匹配可能送错资源，日志里记着 `level: fuzzy` 和得分；用完的记录重复最后一条（`exhausted`）是有意的。
- 回放停在 Chrome 的错误页上时，错误页上写着没送到的 URL；URL 每次都变的站点，按截图比较的回放确定性必然不同。
- 进程外 iframe 的 storage 不读，记在 `storage.json` 的 `unread_frames` 里。
- anti-bot 只按应答头识别，不带这些头的拦截页面认不出来。度量只认 browser 的证据格式。

## 定位入口

需要改这一层时，录制与回放看 `spectragent/core/capabilities/browser/` 下的 `recorder.py`、`archive/`、`replay/`、`cooperation.py` 和 `restoration.py`；trace 的读法在 `spectragent/core/trace/reader.py` 与 `spectragent/core/archive/source.py`；分叉在 `spectragent/core/loops/branch.py`、`spectragent/core/loops/restore/` 和 `spectragent/core/data_loader/task/trace_branches.py`；导出在 `spectragent/export/`；度量与链在 `spectragent/flywheel/`。`tests/_helpers/flywheel_rehearsal.py` 是演练怎么改写发布配置、怎么写链规格的完整例子。字段细节见 [configs/README.md](../configs/README.md)，单个浏览器 agent 怎么运行见 [Browser agent 运行手册](browser_agent_runbook.md)。
