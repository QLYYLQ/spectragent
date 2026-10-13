# Browser agent 运行手册

从仓库根目录执行下列命令。当前执行链是 `YAML → ConfigComposer → AgentRuntime.execute()`；单任务入口是 `spectragent-run`，数据集入口是 `spectragent-bench`。`agent_id` 选模型理解页面和输出动作的方式，`capability_id: browser` 提供浏览器动作与观察，`backend.provider_id` 选 Chrome 会话的来源。这三项可以分别配置。

## 第一次运行

```bash
uv sync --extra dev
export OPENAI_API_KEY="你的密钥"
uv run spectragent-run --config configs/browser_use_local.yaml --dry-run
uv run spectragent-run --config configs/browser_use_local.yaml \
  --start-url https://example.com --task "报告当前页面标题"
```

本地配置默认打开可见的 Chrome 窗口。Chrome/Chromium 必须已安装；自动发现失败时设置 `CHROME_EXECUTABLE_PATH` 为可执行文件路径。`--start-url` 让 browser capability 在 agent 开始前打开页面，三个 agent 都能使用。`spectragent-run` 会读取仓库根目录的 `.env`，也接受已导出的环境变量。`--dry-run` 会验证 YAML、实例化配置并执行 `AgentRuntime.prepare()` 的协商检查；不会启动 Chrome 或调用模型。

## 选择 agent

| 想运行的 agent | 现成 YAML | 页面输入与动作 | 单任务命令 |
|---|---|---|---|
| BrowserUseAgent | `configs/browser_use_local.yaml` | 默认 DOM 文本，按元素索引操作；也支持 DOM + 截图或纯截图 | `uv run spectragent-run --config configs/browser_use_local.yaml --start-url https://example.com --task "报告当前页面标题"` |
| SkyvernAgent | `configs/skyvern_agent_local.yaml` | Skyvern DOM 清洗与 HTML 序列化、动作历史和结构化输出 | `uv run spectragent-run --config configs/skyvern_agent_local.yaml --start-url https://example.com --task "报告当前页面标题"` |
| UITarsAgent | `configs/ui_tars_local.yaml` | 只向模型发送截图，解析 `Action:` 文本中的坐标动作 | `uv run spectragent-run --config configs/ui_tars_local.yaml --start-url https://example.com --task "报告当前页面标题"` |

三个文件中的 `llm` 都只是可编辑的 OpenAI 示例。运行 UI-TARS 专用模型时，把 `llm.model_type`、`model`、`provider`（必要时 `base_url`）改为该模型实际提供的 API；`coordinate_space` 要与它输出的坐标一致，值为 `screenshot_pixels` 或 `normalized_1000`。`prompt_template` 可选 `computer_use`、`grounding`、`mobile_use`。`ui_tars` 不请求 DOM，浏览器不会为这个 agent 提取 DOM。

`model_type` 为 `openai`、`anthropic` 或 `google` 时，provider 每次请求新建一个 SDK 客户端，在这次请求返回前关掉它（请求失败或被取消也关），所以请求之间不复用连接。没关的客户端被垃圾回收时，SDK 迟到的关闭可能注销下一次请求的连接。那次模型调用就卡在建立连接上，超时设多长都要等满，换个任务顺序又不出现（在本机 loopback 模型端点上测到过）。按请求关闭避开了这种情况。`ollama` 与 `mistral` 的客户端仍交给回收，只会泄漏到被回收为止，不会这样卡住。

BrowserUseAgent 的 `agent[0].observation_mode` 可设为：

| 值 | 模型看到 | 点击方式 |
|---|---|---|
| `dom` | DOM 文本 | 元素索引 `browser.click` |
| `dom_vision` | DOM 文本和截图 | 元素索引或坐标 |
| `vision` | 截图 | 坐标 `browser.click_coordinate` |

视觉模式建议保留 `profile: standard`：协商会跳过不需要的 DOM 提取，同时保留该 profile 的 SecurityWatchdog。截图会在三个 BrowserUse 模式下写入 trace。若页面动画导致坐标点击落在过时画面上，可给 `actions:` 中的 `browser.click_coordinate` 配 `post_delay_ms`；显式列出 `actions:` 后，也要列出该运行所需的其他动作，包括 `browser.navigate`（数据集起始 URL 要用它打开）。不写 `actions:` 是全部动作，`actions: []` 是一个动作都没有。详细例子见 [configs/README.md](../configs/README.md#vision-agents)。

动作引起的主 frame 跨文档导航不需要这个延迟，比如提交表单、在文本框里回车、点链接、设置 `location.href`。观测在每次采集尝试之前先等在途的导航提交或停止加载，每次最多 10 s，到时仍未结束就照常采集并记一条警告；没有在途导航时不等。所以提交表单后，下一次观测看到的是结果页。观测截图另有 10 s 上限：Chrome 不回应时，这次尝试算缺截图，再采一次（每次观测最多尝试两次）。页面定时器在观测开始采集之后才发起的导航不在这个等待之内，只受截图上限约束。

`skyvern_agent_local.yaml` 用的 `skyvern` cleaner 不看绘制顺序，被吸顶栏盖住的链接照样有编号。把 `dom_cleaner` 换成 `{ cleaner_type: skyvern_hit_test }` 后，页面会对每个编号元素做点击命中测试，默认（`covered_elements: drop`）所有探测点都点不到的元素不编号；视口外的元素不测，保留编号。见 [configs/README.md](../configs/README.md#dom-cleaners)。

## 选择浏览器后端

切换的是 `environment[0].capabilities[0].backend`，与 `agent_id` 无关。例如直接使用现成的云浏览器配置：

```bash
export SKYVERN_API_KEY="你的密钥"
uv run spectragent-run --config configs/browser_use_skyvern.yaml \
  --start-url https://example.com --task "报告当前页面标题"
```

这里启动的是 BrowserUseAgent + Skyvern 云浏览器。要运行 SkyvernAgent + 本地 Chrome，用上表的 `skyvern_agent_local.yaml`。要组合 SkyvernAgent 与 Skyvern 云浏览器，复制该文件并把 backend 改成：

```yaml
backend:
  provider_id: skyvern
  timeout_minutes: 30
```

当前已注册的 browser backend 如下。云后端由对应的 lite SDK 创建会话；密钥由环境变量或 backend 的 `api_key` 字段提供。

| `provider_id` | 所需环境变量 | 说明 |
|---|---|---|
| `local_chrome` | 无；可选 `CHROME_EXECUTABLE_PATH` | 本地 Chrome/Chromium |
| `skyvern` | `SKYVERN_API_KEY` | 现成示例为 `configs/browser_use_skyvern.yaml` |
| `lexmount` | `LEXMOUNT_API_KEY`、`LEXMOUNT_PROJECT_ID` | 可选上下文持久化 |
| `browser_use` | `BROWSER_USE_API_KEY` | Browser-Use 云会话；与同名 agent 独立 |
| `agentbay` | `AGENTBAY_API_KEY`；可选 `AGENTBAY_ENDPOINT` | AgentBay 云会话 |
| `airtop` | `AIRTOP_API_KEY` | Airtop 云会话 |
| `hyperbrowser` | `HYPERBROWSER_API_KEY` | Hyperbrowser 云会话 |

其他后端可从任一现成 YAML 复制为自己的配置，再把 `backend` 块改成 `{ provider_id: <上表的值> }`。额外参数见各实现的 `Annotated` 字段或重新生成的配置 schema；环境变量由各 SDK 读取。先用 `--dry-run` 检查配置形状，再用真实任务检查服务连接。

每个 browser capability 块都必须写 `replayability` 与 `network_source`，现成 YAML 里都有。评测用的写法是：

```yaml
replayability: { replayability_id: none }
network_source: { source_id: live }
```

漏写任一项，`--dry-run` 会报该字段 `Field required`，例如 `environment.0.capabilities.0.replayability`。这一档原名 `off`，现在叫 `none`：PyYAML 按 YAML 1.1 把裸的 `off` 读成 `false`，旧写法会报 `Input tag 'False' found using 'replayability_id' does not match any of the expected tags: 'none', 'pinned'`。`none` 不 pin 任何东西，`live` 直接用线上网络，两者都不会打开第二条 CDP 连接，任何后端都能用。

`replayability: pinned`、`network_source: archive` 和 trace hook 绑定的归档器用于录制与回放，只支持 `local_chrome`。归档器或 `pinned` 配云后端，都在 `--dry-run` 就报错（`prepare()` 的 setup 检查；正式运行时同样在打开 trace、打开浏览器会话之前报）。评测和 anti-bot 实验保持 `none`，因为 `pinned` 会改变线上站点的行为，它本身也是能被检测到的特征。

连到本机 CDP 端点（`localhost`、`127.0.0.0/8`、`::1`，`local_chrome` 启动的浏览器就在这里）的连接直连、不压缩，不读 `http_proxy` 等代理环境变量，本机开着代理（如 Clash）时 CDP 流量也不经过它。远端端点（云后端）仍按 websockets 的默认读环境代理、协商压缩。

## 跑数据集

单任务命令只接收自由文本。需要数据集身份、任务起始 URL、逐任务 trace 时，用 `spectragent-bench`。基准数据不包含在安装包中；数据根目录约定为 `<root>/browser/<benchmark>/data_info.json`。

```bash
export OPENAI_API_KEY="你的密钥"
export SPECTRAGENT_BENCHMARK_DATA="$HOME/agent-data"
uv run spectragent-bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --list
uv run spectragent-bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --limit 1 --no-eval
```

把 `--config` 换成上面的其他 agent YAML，就使用同一个数据集入口。也可以用 `--task-id <ID>` 指定任务，或用 `--limit N` 控制数量。`--list` 会先加载数据并只打印选中的任务 ID，不启动浏览器。数据集 loader 把每个任务的 `start_url` 传给 browser capability。`spectragent-bench` 不调用 `load_dotenv()`，所以运行前要在 shell 导出模型及云后端需要的密钥。需要自动评分时，在 YAML 中配置 `evaluator:`，然后去掉 `--no-eval`；只有任务数据与 trace 不会自动产生评分。

按 Ctrl-C 中断时，正在跑的任务照常收尾：trace 写出 `result.json`（`agent_done: error`，`error: "Execution cancelled"`），进行中的那次 LLM 调用以 `cancelled` 状态连同 prompt 落盘，浏览器也会关掉。随后中断继续往外传，sweep 结束，不打印汇总表，也不评分。

要在认出风控页之类的情况时停掉整个 sweep，可以写一个 `blocking` 的 guard hook，在它的 handler 里抛 `RunAbort`（`spectragent/core/loops/errors.py`）。被否决的任务同样先收尾，sweep 在它这里停下：日志里留一条 `sweep vetoed at <task_id> after N/M task(s)`，汇总表和评分只包括之前跑完的任务。否决与 run 自己的超时或收尾失败同时发生时，sweep 照样停下；只有外部中断比否决优先。

## 看结果与定位入口

每次运行的 trace 写在 `traces/<run_id>/`，配置里的 `hook: [{hook_id: trace_writer}]` 决定是否落盘。`manifest.json` 是已提交内容的唯一索引（`format_version: 3`），`config.json` 是配置快照，`steps/NNN.json` 是每步的记录。`observations/` 存观测和截图，文件名带全限定的 fragment key，如 `000_browser.dom.json`、`000_browser.screenshot.png`。`messages/` 每次 LLM 调用一个 `NNN_call_KK.json`（parse 重试、校验失败、截断、被取消的调用各占一个）；请求了结构化输出时，schema 每个 run 只在 `messages/output_formats/` 存一份。绑定了归档器的录制 run 还有 `env/`；现成的评测 YAML 不录环境证据，没有这个目录。

run 开始之后，不论完成、超时、出错、被否决还是被外部中断，都会写 `result.json`，它的 `agent_done` 说 agent 怎么结束（`done`、`max_steps`、`error`、`timeout`、`vetoed`）。manifest 的 `status` 只说 trace 记全没有：写着时是 `writing`，收尾之后是 `complete`，或 `incomplete` 加上 `incomplete_reason`（某一步或收尾时有东西没记下来）和 `complete_through`（完整到第几步）。超时的 run 照样可以是 `complete`。`result.json` 的 `metrics.steps` 只数策略步，即 `steps/` 里 `origin` 为 `policy` 的步。`agent_loop.terminal_observation: true` 在 run 结束后多记的一次观测（`origin: terminal`），以及分叉 run 恢复出的前缀步（`origin: restored`），都在 `steps/` 里，但不计数。模型调用次数另记在 `metrics.llm_calls`。`spectragent-bench` 会在终端先列出三本账的计数（agent 怎么结束、环境、trace 完整与否），再列每个任务的终止状态与 trace 目录名；环境坏了、模型在重试之后仍不可用、trace 不完整都标成 infrastructure failure，退出码 1；guard 否决了某个任务时 sweep 停在那里，退出码 3。

用 `RunTrace` 读 trace，参数是 `Path`：

```python
from pathlib import Path
from spectragent.core.trace import RunTrace

trace = RunTrace(Path("traces/<run_id>"))
trace.result.metrics.steps   # 策略步数
trace.llm_messages(0)        # 第 0 步的每一次 LLM 调用
trace.screenshot(0)          # 第 0 步截图的字节
```

它只按 manifest 找文件，manifest 没列出的文件不算数；列出了却读不回来的文件（缺失、为空、损坏、类型不符）抛 `TraceSchemaError`，点名那个文件。`format_version` 不是 3 的旧 trace 整个读不出来（构造 `RunTrace` 时就抛 `TraceSchemaError`，点名版本）：旧格式的 `status` 把 agent 的结局和 trace 是否完整混在一起，读不出可靠的含义，只能重跑任务。

需要继续改入口时，按执行顺序看 [单任务入口](../spectragent/core/run_cli.py)、[基准入口](../spectragent/core/benchmark_runner.py)、[装配器](../spectragent/core/composer.py)、[运行时](../spectragent/core/runtime.py)。agent 的配置与输出协议分别在 `spectragent/agents/browser_use.py`、`skyvern.py`、`ui_tars.py`；browser capability 与 backend 在 `spectragent/core/capabilities/browser/`。配置字段细节见 [configs/README.md](../configs/README.md)。

## 录制、回放与训练导出

数据飞轮把 browser run 连同它的网络、存储和原始 DOM 一起录下，再离线回放、从录制的检查点分叉、导出训练数据，最后出一份度量报告。用法见 [数据飞轮运行手册](data_flywheel_runbook.md)，配置字段见 [configs/README.md](../configs/README.md)。各步的入口：

| 用途 | 命令 |
|---|---|
| 录制（`pinned` 加三个归档器） | `uv run spectragent-run --config configs/flywheel/record_ui_tars_pinned.yaml --start-url URL --task "任务"`；数据集换用 `spectragent-bench` |
| 用录制的网络离线重跑 | `uv run spectragent-run --config configs/flywheel/replay_ui_tars_archive.yaml --start-url URL --task "录制时的任务" --replay-archive traces/<录制的 run_id>` |
| 从录制的检查点分叉 | 把 `configs/flywheel/branch_ui_tars_archive.yaml` 的 `task_loader.data_dir` 指向录制所在的目录，再 `uv run spectragent-bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval` |
| 导出训练数据 | `uv run spectragent-export --config configs/flywheel/export_sft.yaml` |
| 跑整条链并出度量报告 | `uv run spectragent-flywheel run --spec chain.yaml`；只重算报告用 `uv run spectragent-flywheel report --spec chain.yaml` |

`--replay-archive` 只能配 `network_source: archive`，YAML 写的是 `live` 时 setup 会拒绝。`spectragent-export` 只读 trace，不启动浏览器，也不调用模型。`chain.yaml` 要自己写，格式见 [configs/README.md](../configs/README.md#measuring-the-flywheel)。`spectragent-flywheel` 和 `spectragent-bench` 一样不读 `.env`，跑之前先在 shell 里导出密钥（`set -a; . ./.env; set +a`）。
