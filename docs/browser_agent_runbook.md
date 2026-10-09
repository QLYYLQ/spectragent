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

BrowserUseAgent 的 `agent[0].observation_mode` 可设为：

| 值 | 模型看到 | 点击方式 |
|---|---|---|
| `dom` | DOM 文本 | 元素索引 `browser.click` |
| `dom_vision` | DOM 文本和截图 | 元素索引或坐标 |
| `vision` | 截图 | 坐标 `browser.click_coordinate` |

视觉模式建议保留 `profile: standard`：协商会跳过不需要的 DOM 提取，同时保留该 profile 的 SecurityWatchdog。截图会在三个 BrowserUse 模式下写入 trace。若页面动画导致坐标点击落在过时画面上，可给 `actions:` 中的 `browser.click_coordinate` 配 `post_delay_ms`；显式列出 `actions:` 后，也要列出该运行所需的其他动作，包括 `browser.navigate`（数据集起始 URL 要用它打开）。详细例子见 [configs/README.md](../configs/README.md#vision-agents)。

## 选择浏览器后端

切换的是 `environment[0].capabilities[0].backend`，与 `agent_id` 无关。例如直接使用现成的云浏览器配置：

```bash
export SKYVERN_API_KEY="你的密钥"
uv run spectragent-run --config configs/browser_use_skyvern.yaml \
  --start-url https://example.com --task "报告当前页面标题"
```

这里启动的是 **BrowserUseAgent + Skyvern 云浏览器**。要运行 **SkyvernAgent + 本地 Chrome**，用上表的 `skyvern_agent_local.yaml`。要组合 SkyvernAgent 与 Skyvern 云浏览器，复制该文件并把 backend 改成：

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

## 看结果与定位入口

每次运行的 trace 写在 `traces/<run_id>/`；`manifest.json` 是已提交内容的唯一索引，`config.json` 是配置快照，`steps/` 存每步的记录，`observations/` 存观测和截图，`messages/` 每次 LLM 调用一个 `NNN_call_KK.json`（parse 重试、校验失败、截断、被取消的调用各占一个），完成后还有 `result.json`。manifest 没列出的文件不算数：用 `RunTrace` 读 trace，它只按 manifest 找文件；manifest 里没有 `format_version: 2` 的旧 trace，只能读出配置、结果和步骤记录。`spectragent-bench` 会在终端列出每个任务的终止状态与 trace 目录名。配置里的 `hook: [{hook_id: trace_writer}]` 决定是否落盘。

需要继续改入口时，按执行顺序看 [单任务入口](../spectragent/core/run_cli.py)、[基准入口](../spectragent/core/benchmark_runner.py)、[装配器](../spectragent/core/composer.py)、[运行时](../spectragent/core/runtime.py)。agent 的配置与输出协议分别在 `spectragent/agents/browser_use.py`、`skyvern.py`、`ui_tars.py`；browser capability 与 backend 在 `spectragent/core/capabilities/browser/`。配置字段细节见 [configs/README.md](../configs/README.md)。
