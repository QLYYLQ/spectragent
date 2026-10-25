# Spectragent

面向 agent 研究者的评测与实验框架。

[English](README.md)

Spectragent 把一次 agent 运行拆成可以逐个替换的部分：agent、模型、执行 loop、observation、action、memory 和 environment。一份 YAML 为每个部分选一个实现。trace hook 把运行记录下来，之后的查看、判分、离线回放、从录制的某一步分叉、训练导出都读这份记录，不重跑 agent。

版本 0.3.0 · Python 3.11+ · Apache-2.0 · trace 格式 7 · `AgentResult` schema 5.0

- [工作方式](#工作方式)
- [当前实现](#当前实现)
- [快速开始](#快速开始)
- [配置](#配置)
- [数据飞轮](#数据飞轮)
- [回放审查](#回放审查)
- [RunTrace](#runtrace)
- [查看运行](#查看运行)
- [命令行](#命令行)
- [扩展](#扩展)
- [开发](#开发)
- [文档](#文档)
- [状态与限制](#状态与限制)

## 工作方式

```
YAML ─▶ ConfigComposer ─▶ AgentRuntime.prepare() ─▶ execute ─▶ RunTrace
                               ├─ agent × loop 槽位检查
                               ├─ observation / action / archive 协商
                               └─ setup 检查（环境、任务上下文）
```

- **每个维度一个注册表。** agent、loop、模型、memory、capability、action、observation fragment、归档器、hook、浏览器后端、DOM cleaner 等各自是用 `conscribe` 建的独立注册表。消融就是只改一个注册表键或一个字段的普通运行。
- **Agent 与 loop 分离。** Agent 持有领域知识：构建提示词、解析输出、格式化历史。Loop 驱动 observe、think、act，并声明它需要 agent 提供哪些方法。`prepare()` 检查这些方法，缺哪个就点名哪个。
- **三方协商。** Capability 声明能提供的 observation fragment、action 和归档器；agent 与 loop 声明必需项和可选项。提示词只列出环境实际能执行的动作，做不到的组合在启动浏览器、调用模型之前就被拒绝。
- **生成的类型化配置。** 每个类的 `__wiring__` 与带注解的构造参数生成一份判别联合 schema。YAML 里写的每个策略都以构造好的实例到达，带着它自己的字段。
- **记录靠织入。** Hook 包装 agent、environment、模型和 loop 的公开方法；loop 代码从不调用记录器。`trace_writer` 是唯一把运行写到磁盘的组件。
- **预写式 trace。** 每一步先提交内容文件，再写步记录，最后原子替换 manifest。录制失败只作为录制健康状况报告，从不改变 agent 的结局。

## 当前实现

浏览器是唯一已实现的环境。下表是 2026-10-25 `spectragent components list` 的输出：

| 维度 | 已注册 |
|---|---|
| Agent | `browser_use.dom`、`browser_use.dom_vision`、`skyvern.dom`、`skyvern.dom_vision`、`ui_tars` |
| Loop | `react`；`branch`（在检查点续跑录制的 run），恢复器 `prefix_replay` |
| Environment 与 capability | `browser` |
| 浏览器后端 | `local_chrome`；云端：`lexmount`、`browser_use`、`hyperbrowser`、`agentbay`、`airtop`、`skyvern` |
| 浏览器动作（16 个） | `navigate`、`click`、`type`、`scroll`、`wait`、`send_keys`、`select_dropdown`、`drag`、`screenshot`、`extract_content`、`switch_tab`、`go_back`、`go_forward`、`click_coordinate`、`type_coordinate`、`done` |
| Observation fragment | `browser.dom`、`browser.screenshot`、`browser.browser_meta`、`browser.platform`、`browser.capture_state`；loop 侧：`loop.task`、`loop.step_info`、`loop.history`、`loop.action_result` |
| DOM 管线 | cleaner `browser_use`、`skyvern`、`skyvern_hit_test`；serializer `text`、`html` |
| 输入 | 鼠标 `instant`、`windmouse`、`bezier`、`learned`（需要 `learned-mouse` extra）；打字 `instant`、`human` |
| Chat 模型 | `openai.{direct, azure, deepseek, openrouter, groq, cerebras, vercel}`、`anthropic.{direct, aws}`、`google.default`、`bedrock.default`、`oci.default`、`mistral.default`、`ollama.default`、`browser_use.default` |
| Decision 模型 | `typesafe.direct`、`typesafe.openrouter`（回放审查使用） |
| Memory | `simple`、`sliding_window`、`token_budget` |
| Hook | `trace_writer`、`live_monitor`、`prompt_contribution`、`omit_source_replies`、`ui_navigation_guard` |
| 归档器 | `browser.network`（WARC）、`browser.storage`、`browser.dom_raw`、`browser.frames`、`browser.replay_log` |
| 回放 | replayability `none`、`pinned`；网络来源 `live`、`archive`；miss 策略 `strict` |
| Task loader | `online_mind2web`、`browsecomp`、`lexbench_browser`、`trace_branches`（从录制的 run 生成任务）；选步器 `explicit`、`every_k`、`all`、`last` |
| Evaluator | `exact_match`、`online_mind2web`、`browsecomp`、`lexbench_browser` |
| 导出 | 导出器 `openai_messages`；翻译器 `identity`、`index_to_coordinate` |
| 回放审查器 | `jev` |

“已注册”表示可以选；一次运行只启用 YAML 里写到的实现。注册表键按层区分，所以 `browser_use` 可以同时是 agent 家族、云后端和 DOM cleaner 的名字。

值得知道的浏览器行为：

- **动作效果。** 动作会测量自己做了什么（点击落在哪里、点中了什么，输入是否改变了字段，滚动位移，前后 URL），放进 `ActionResult.effect`。Agent 把它作为一行文字给模型看；`show_action_effects: false` 去掉这一行，trace 照样记录。
- **`capture_state`。** 一个可选 fragment，包含页面落定结果、不是 agent 引起的焦点移动、最后确认的指针位置、视口内加载失败的图片。在 agent 上写 `show_capture_state: false` 就不协商它。
- **浏览器快捷键。** `send_keys` 按实测的平台执行浏览器自己的后退、前进、重新加载、关闭标签页和切换标签页快捷键（例如 macOS 的 `Meta+[`、Linux 的 `Alt+ArrowLeft`），这些键不发给页面。
- **动作失败。** 在 `react` 里，一步中第一个失败的动作结束这一步。其后的动作记为跳过，下一步的提示告诉模型它们没有执行。没有文本框获得焦点时，`type` 和 `type_coordinate` 不输入任何文字，直接失败。

尚未实现：Docker/SWE、tool sandbox、desktop、Android 和 GPU profiling capability。注册表机制已经就位，`docker.terminal` 和 `tool_sandbox.tool_result` 两个 fragment schema 也存在，但没有 capability 提供它们。

## 快速开始

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)，以及供 `local_chrome` 后端使用的 Chrome 或 Chromium（自动发现失败时设置 `CHROME_EXECUTABLE_PATH`）。云后端需要对应厂商的凭证。

```bash
uv sync --extra dev
export OPENAI_API_KEY=sk-...

# 只组装和 prepare：不启动浏览器，不调用模型
uv run spectragent config check configs/browser_use_local.yaml

# 跑一个任务（这份配置会打开可见的 Chrome 窗口）
uv run spectragent run --config configs/browser_use_local.yaml \
  --start-url https://example.com --task "报告页面标题"

# 跑 5 个 benchmark 任务，只记录不判分
uv run spectragent bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --limit 5 --no-eval

# 查看记录
uv run spectragent runs list
uv run spectragent view --open
```

`run`、`eval` 和 `replay-review` 还会读取工作目录下的 `.env`；`bench` 和 `flywheel` 只读 shell 环境（`set -a; . ./.env; set +a`）。Benchmark 数据不随仓库发布：在 task loader 上设 `data_dir:`，或让 `SPECTRAGENT_BENCHMARK_DATA` 指向按 `<env_type>/<benchmark>` 排布的根目录。

发布的入口配置：`configs/browser_use_local.yaml`、`configs/skyvern_agent_local.yaml`、`configs/ui_tars_local.yaml`、`configs/browser_use_skyvern.yaml`（Skyvern 云浏览器）和 `configs/browser_use_shared.yaml`（共享一个 Chrome，每个任务一个新 context）。切换后端只改 `environment[0].capabilities[0].backend`。登录态、共享 Chrome 与排错见 [浏览器 agent 运行手册](docs/browser_agent_runbook.md)。

## 配置

一份能通过 `config check` 的最小配置：

```yaml
agent:
  - agent_id: browser_use.dom      # 或 browser_use.dom_vision、skyvern.dom、skyvern.dom_vision、ui_tars
    id: main
    agent_loop: { loop_id: react, max_steps: 30, max_actions_per_step: 4 }
    llm:
      model_type: openai           # 模型厂商自己的协议
      model: gpt-4o
      provider: { name: direct }   # 走网关时加 base_url:
    memory: { memory_id: simple, max_messages: 200 }

environment:
  - env_type: browser
    capabilities:
      - capability_id: browser
        backend: { provider_id: local_chrome, headless: true }
        mouse_trajectory: { trajectory_type: instant }
        typing_rhythm: { rhythm_type: instant }
        replayability: { replayability_id: none }
        network_source: { source_id: live }
        dom_cleaner: { cleaner_type: browser_use }
        dom_serializer: { serializer_type: text }

hook:
  - hook_id: trace_writer
    trace_dir: traces
```

- YAML 选择组件。任务文本和起始 URL 来自 CLI 或 task loader，所以一份文件可以服务多个任务。
- 单实例受体要写出来。每个 capability 块都必须写 `replayability` 和 `network_source`，不录制时也一样；“关闭”写作 `none`。
- 上面省略了 `actions:`，于是构建全部 16 个动作。写成列表时只构建、广告并允许列出的动作，`[]` 一个也不构建。Agent 需要的动作被去掉时，协商在运行前失败。
- YAML 和 trace 里的键都是全限定的（`browser.click`）。Agent 声明相对名（`click`），协商把它绑定到唯一提供它的 capability。
- 改动 `__wiring__` 条目或 `Annotated[...]` 构造参数后，运行 `uv run spectragent config generate`。`spectragent/generated/` 是构建产物。

`trace_writer` 默认记录 metrics、errors、trajectory 和 observations。`recording` 用来收窄，`archive` 是环境证据的唯一开关（默认为空）：

```yaml
hook:
  - hook_id: trace_writer
    trace_dir: traces
    recording: { metrics: true, errors: true, trajectory: false, observations: false }
    archive:
      - archiver_id: browser.frames     # 每个动作之前、每次观测之后的截图；agent 看不到
      - archiver_id: browser.storage    # cookie 与 Web Storage
```

`browser.network` 另外要求 `replayability: pinned`（或 `require_replayable: false`），所有归档器都要求 `local_chrome`；其他组合由 `prepare()` 拒绝。

```bash
uv run spectragent config explain configs/browser_use_local.yaml   # 组件、来源、注入、协商、脱敏配置
uv run spectragent config template --model gpt-4o                   # 完全展开、带注释的 YAML，输出到 stdout
uv run spectragent components list --layer agent
```

`config check` 和 `run --dry-run` 都会组装并执行 `prepare()`，在环境 setup 之前停下。`bench` 用第一个任务做同样的检查，被拒时以退出码 2 结束。

## 数据飞轮

把一次浏览器运行连同它的环境录下来，离线回放，在某一步分叉，导出训练数据，再度量这条链。只有 `local_chrome` 能录制和回放。

| 阶段 | 入口 | 发布的配置 |
|---|---|---|
| 录制 | `run` 或 `bench` | `configs/flywheel/record_ui_tars_pinned.yaml`：`pinned` 固定种子、时钟、时区、locale、DPR 和配色；归档网络（WARC）、存储和原始 DOM |
| 回放 | `run --replay-archive DIR` | `configs/flywheel/replay_ui_tars_archive.yaml`：每个请求都由归档应答；没命中的按 `strict` 处理（主 frame 文档失败，beacon 回 204，其余子资源回 404） |
| 分叉 | `bench` 配 `trace_branches` | `configs/flywheel/branch_ui_tars_archive.yaml`：恢复到第 *t* 步之前，逐步校验恢复结果，让 teacher 重新决策 |
| 原路径回放 | `bench` 配 `trace_branches` | `configs/flywheel/same_path_ui_tars_archive.yaml`：把每条录制恢复到最后一步，跑两遍，度量保真与确定性 |
| 导出 | `export` | `configs/flywheel/export_sft.yaml`，学生配置 `configs/flywheel/ui_tars_student.yaml`：按学生自己的格式渲染成 `openai_messages` |
| 度量 | `flywheel run`、`flywheel report` | 自己写的链规格；产出 `report.json` 和 `report.md` |
| 诊断 | `flywheel diagnose` | 对齐录制与回放的截图，标出有变化的网格单元，供人工审查 |

```bash
# 在本地 fixture 站点上用脚本化模型演练整条链（需要 Chrome，不需要 API key，约 40 秒）
uv run python -m tests._helpers.flywheel_rehearsal --out /tmp/spectragent-rehearsal

# 录制到 traces/recorded（复制配置并改 trace_dir），再回放其中一个 run
uv run spectragent run --config my/record.yaml --start-url https://example.com --task "..."
uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml \
  --task "<源 config.json 里的任务>" --start-url <它的 start_url> \
  --replay-archive traces/recorded/<run>

# 分叉所有录制的 run、导出，再跑一条带度量的链
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --list
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
uv run spectragent export --config configs/flywheel/export_sft.yaml
uv run spectragent flywheel run --spec chain.yaml
```

回放要在 `archive` 里用 `browser.replay_log` 代替 `browser.network`。`configs/replay_rules/` 下按站点的回放规则只能收窄匹配；每次回放都保存它所用规则的正文和摘要（[规则语法](configs/replay_rules/README.md)）。分叉还可以在检查点换一份任务（`trace_branches` 的 `task_variants`）。导出阶段冻结在第一版，学生一侧是暂定的。链规格、保真度量和隐私见 [数据飞轮运行手册](docs/data_flywheel_runbook.md) 和 [configs/README.md](configs/README.md#recording-environment-evidence)（英文）。

## 回放审查

`spectragent replay-review` 用 `jev` 审查器事后比较录制步与回放步：它向 decision 模型分别提三个问题（目标是否相同、信息是否相同、证据是否足够），把每一对分到 `candidate_acceptable`、`further_review` 或 `investigate_difference`。它只决定人工复核的先后，从不改变恢复校验的结论。

```bash
uv run spectragent replay-review --config configs/replay_review/jev_direct.yaml \
  --pairs pairs.json --out reviews/job-1 --dry-run
uv run spectragent view --root reviews
```

官方 API、OpenRouter 和网关三种配置都在 `configs/replay_review/`。pairs 的格式见 [configs/README.md](configs/README.md#independent-replay-review)（英文）。

## RunTrace

一次运行一个目录，按步提交：

```
traces/<run_id>/
  manifest.json                    # 提交点：状态 writing | complete | incomplete，complete_through
  config.json                      # 有效配置（已脱敏）、源 YAML 路径与摘要
  result.json                      # agent 结局（agent_done）、用量、execution_ms
  steps/000.json                   # StepRecord：origin policy | restored | terminal、动作、结果、坐标系
  observations/000_browser.dom.json
  observations/000_browser.screenshot.png
  messages/000_call_00.json        # 每次模型调用一个文件；images/ 与 output_formats/ 每个 run 只存一份
  env/                             # 环境证据，只在设置了 archive 时出现
  evals/                           # 事后判定，在 manifest 之外
```

trace 在声明的录制范围内是完整的；没有选的通道不算损坏的证据。token 用量按 provider 报告的数保存，没报告的保持未知。框架不计算价格。读取方拒绝格式 7 以外的 trace。

在 Python 里通过 `spectragent.core.trace.RunTrace` 读取，它只提供已提交的文件，并核对大小和哈希。

## 查看运行

```bash
uv run spectragent runs list --search mind2web
uv run spectragent runs show <run> --step 0
uv run spectragent runs compare <run-a> <run-b>
uv run spectragent runs compare-groups --left <hash> --right <hash> --evaluator <slug>
uv run spectragent tui                                   # 浏览已有 run 的终端界面
uv run spectragent run --config ... --task ... --tui     # 实时监控（bench 也有）
uv run spectragent view --open                           # 本地网页查看器，127.0.0.1:8765
uv run spectragent view --annotations labels/ --annotator alice
```

这些命令共用一个读侧，都不会启动 agent 或模型。查看器显示步骤、带动作标记的截图、每个 fragment 和每次模型调用、恢复证据、分叉来源和各类报告。不给 `--annotations DIR` 时它是只读的；给了之后，它把审阅队列（`DIR/queues/*.json`，由你的工具写入）的标签和随手的步骤标签追加到 `DIR/labels/`，从不写 trace。标注要求 `--host` 是回环地址。`--config` 和 `--config-hash` 按源 YAML 或有效配置筛选 run。

## 命令行

| 命令 | 用途 |
|---|---|
| `run` | 跑一个任务；`--dry-run`、`--replay-archive`、`--tui` |
| `bench` | 跑 benchmark 或分叉任务；`--list`、`--task-id`、`--limit`、`--no-eval`、`--token-limit`、`--tui` |
| `eval` | 用一个 evaluator 和一个 eval loader 给已保存的 trace 判分 |
| `export` | 从已保存的 trace 导出训练数据 |
| `flywheel` | `run`、`report`、`diagnose` |
| `replay-review` | 独立审查回放对 |
| `runs` | `list`、`show`、`compare`、`compare-groups` |
| `tui`、`view` | 终端与网页界面 |
| `config` | `check`、`explain`、`template`、`generate` |
| `components` | `list` 列出已注册的实现 |

全局 `--json` 把一份 JSON 结果写到 stdout，日志留在 stderr。每个命令都有 `--help`。

## 扩展

每个维度都有一个注册入口，`.agents/skills/` 下有写好的操作流程，人和 code agent 都可以照着做：

| 任务 | Skill |
|---|---|
| Capability 或后端 | `af-add-a-capability` |
| Loop | `af-add-a-loop` |
| Action | `af-add-an-action` |
| Observation fragment | `af-add-an-observation-fragment` |
| 可配置受体 | `af-add-a-receptor` |
| 模型 provider 或网关路由 | `af-add-an-llm-provider` |
| 重新生成并验证配置 | `af-regenerate-and-verify` |
| 飞轮各阶段与策略 | `af-data-flywheel` |
| 按站点的回放规则 | `af-improve-replay-rules` |
| Hook 与 trace 语义 | `af-trace-and-hooks` |

新增注册表时，还要把它写进 [AGENTS.md](AGENTS.md) §2.2 和 `docs/architecture_design.md` §6；否则 `tests/test_docs/test_registrar_coverage.py` 会失败。

## 开发

```bash
uv sync --extra dev
uv run pytest -m "not requires_chrome"     # 跳过启动真实 Chrome 的测试
uv run pytest                              # 全部测试，在空闲的机器上跑
uv run ruff check . && uv run ruff format .
uv run pyright spectragent tests
```

- `docs/architecture_design.md` 是设计权威。代码与它不一致时，改代码。
- 测试镜像 `spectragent/` 的结构。异步测试不需要装饰器（`asyncio_mode = "auto"`）。
- `tests/test_generated/test_in_sync_with_registries.py` 检查生成的配置与实时注册表是否一致。
- 被取代的代码直接删除，不保留兼容适配器或迁移垫片。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture_design.md](docs/architecture_design.md) | 设计文档：抽象、协商、运行时、trace、注册表 |
| [docs/execution_dag.md](docs/execution_dag.md) | 模块拆分、依赖与实现顺序 |
| [docs/browser_agent_runbook.md](docs/browser_agent_runbook.md) | 运行浏览器 agent |
| [docs/data_flywheel_runbook.md](docs/data_flywheel_runbook.md) | 录制、回放、分叉、导出、度量、回放审查 |
| [configs/README.md](configs/README.md) | 配置布局与字段语义（英文） |
| [configs/replay_rules/README.md](configs/replay_rules/README.md) | 回放规则语法（英文） |
| [AGENTS.md](AGENTS.md) | 本仓库给 code agent 的工作规则（英文） |

## 状态与限制

- 浏览器是唯一的环境。集成按抽样的端到端运行验收，没有跑完整的 agent × benchmark 矩阵。几个真实 agent 各自操作多个标签页的工作流还没有验证。
- Evaluator 作为独立阶段运行（`spectragent eval`），只读已提交的 trace，把任务正确性与回放保真分开。飞轮的任何阶段都不需要判定结果。
- 录制完整不代表任务成功。trace 完整性、恢复校验和截图一致是不同的证据，任何一项都不能证明任务或登录成功。
- 云后端不能录制或回放。不支持 WebSocket 回放。
- `pinned` 固定页面配色，但宿主机自己的浅色或深色外观不被记录，headless Chrome 的原生滚动条看起来跟随它。正式运行请固定外观，恢复校验的结果只在同一外观下的运行之间比较。
- 0.3.x 仍在变化，这份 README 不承诺 API 稳定。

## License

Apache-2.0。第三方代码与依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
