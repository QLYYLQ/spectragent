# Browser agent 运行手册

从仓库根目录执行下列命令。当前执行链是 `YAML → ConfigComposer → AgentRuntime.execute()`；单任务入口是 `spectragent run`，数据集入口是 `spectragent bench`。`agent_id` 选模型理解页面和输出动作的方式，`capability_id: browser` 提供浏览器动作与观察，`backend.provider_id` 选 Chrome 会话的来源。这三项可以分别配置。

## 统一命令与结果界面

安装或更新工作区后执行 `uv sync --extra dev`。统一入口为 `uv run spectragent --help`；模型、浏览器与实验参数继续写在 YAML，不需要在界面里重新配置一遍。

```bash
# 只检查配置、协商和可静态判断的 setup 条件
uv run spectragent config check configs/browser_use_local.yaml
# 查看实际组件、动作/观测绑定、拦截器与脱敏配置
uv run spectragent config explain configs/browser_use_local.yaml
# 查询已注册组件
uv run spectragent components list --layer agent
# 注册声明改变后重新生成配置
uv run spectragent config generate
```

普通交互终端显示 Rich 实时进度；加 `--tui` 使用全屏界面。TUI 的阶段来自当前进程，显示的动作步骤标为已观察，是否持久化由 Trace 查看器确认。按 `q` 或 Ctrl-C 关闭正在执行的界面会请求取消并等待 trace 和环境收尾。不存在暂停或原地恢复的暗含操作。

```bash
uv run spectragent run --config configs/browser_use_local.yaml \
  --start-url https://example.com --task "报告当前页面标题" --tui
uv run spectragent bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --limit 5 --no-eval --tui
```

批量命令在 trace 根目录的 `.sessions/` 中记录所选任务及停止原因，保留未启动任务。文件里的 running 只表示未记录收尾，不是进程存活证明。`--json` 将结构化结果写到 stdout，日志仍到 stderr；不能与 `--tui` 同用。

```bash
uv run spectragent --json runs list --root traces
uv run spectragent runs show RUN_KEY --root traces --step 0
uv run spectragent runs compare RUN_A RUN_B --root traces
uv run spectragent tui --root traces
uv run spectragent view --root traces --port 8765 --open
```

`view`、历史 `tui` 和 `runs list` 也支持按配置文件来源筛选。`--root` 始终是搜索目录，默认 `traces`；`--config` 不会从当前 YAML 推断或扩大这个范围，也不要求该文件仍然存在。同一文件改过参数的历史版本都包含在内，不同目录下的同名文件分开。新的录制与批量运行记录来源路径和源文件版本摘要；此前未记录来源的结果显示 Unknown，不凭目录或脱敏配置猜归属。

```bash
# 同一份配置启动的全部历史版本（在所选根目录内）
uv run spectragent view --root traces --config configs/experiment.yaml --open
uv run spectragent tui --root traces --config configs/experiment.yaml
uv run spectragent --json runs list --root traces --config configs/experiment.yaml
# 启动时直接聚焦一个 run；根目录仍保留其他结果和源运行导航
uv run spectragent view --root traces --run branch/RUN_KEY --open
uv run spectragent tui --root traces --run branch/RUN_KEY
```

`--config-hash` 是另一种筛选：精确选择 Trace 已记录的有效配置摘要，可与来源路径取交集；它不是源 YAML 字节摘要。`--run` 与显式筛选冲突会在打开界面前拒绝。若源录制与分叉分别在 `record/`、`branch/`，请选它们的共同父目录作为 root；TUI 的 `s` 跳到所选运行的源检查点，网页提供同样的链接，不越界打开根外的来源。

恢复步骤的源动作、源结果和源模型回复与本次实际恢复执行分开展示。实时终端把 `max_steps` 标为配置上限，并单列 `continue_steps`，不把配置上限当成分叉的实际步数预算。回放日志页按原有读取器的折叠权重统计 hit/miss/exhausted/synthetic，包含 trailing 收尾决定；未记录日志、空日志和日志损坏分别显示。

查看器只读本地目录，展示截图、步骤、模型请求/回复、每次失败与重试、观测、评测、配置和分叉来源。运行对比与实验组对比分开：实验组按配置摘要选择，按相同任务配对，必须明确选择 evaluator；重复尝试记为 ambiguous，不自动挑最好的一次。判分配置不同不合并统计。报告页读取同一根目录下的独立评测、飞轮、诊断、数据集和任务清单；可以把 `--root` 指向包含这些产物的共同父目录。

服务默认只监听 `127.0.0.1`。SSH 使用时在服务器运行 view，在本机建立端口转发后打开 `http://127.0.0.1:8765`：

```bash
ssh -L 8765:127.0.0.1:8765 your-server
```

网页前端已经随 Python 包分发，使用者无需 Node。修改前端源码时，在 `frontend/viewer` 执行 `npm ci`、`npm run build`，产物写入 `spectragent/viewer/static/`。界面不会重新执行网络回放或调用 judge；这两项仍由各自运行命令明确启动。

## 独立评测已录下的任务

`spectragent eval` 只读已保存的 RunTrace，调用独立 judge 判断任务正确性，不启动浏览器或重跑 agent；它不改变回放保真、恢复校验或分叉放行条件。先完成 judge 路由和图像输入验证，再把验证过的 `llm` 配置放进 YAML。例如保存为本地 `tmp/evaluation.yaml`：

```yaml
evaluator:
  - evaluator_id: lexbench_browser
    eval_strategy: stepwise
    image_scale_factor: 1.0
    max_screenshots: 30
    llm:
      model_type: openai
      model: 替换为已验证支持图像的型号
      provider: {name: direct}
      # base_url: 已验证的兼容接口地址
eval_loader:
  - benchmark_name: lexbench_browser
    subset: All
    # data_dir: 本地含 data_info.json 的参考数据目录
```

这里的 `openai/direct` 是配置形状示例，型号或网关采用别的协议时，整体替换为验证过的 `llm` 块。密钥从相应环境变量读取；入口也加载 `.env`。参考数据默认由 `SPECTRAGENT_BENCHMARK_DATA` 指定的根目录定位。每次只组合一个 evaluator 和一个 eval_loader；不接受 agent、environment 或 hook 块。

```bash
uv run spectragent eval --config tmp/evaluation.yaml \
  --traces traces/chains/example/traces/record \
  --out evals/example-first --dry-run

uv run spectragent eval --config tmp/evaluation.yaml \
  --traces traces/chains/example/traces/record traces/chains/example/traces/branch \
  --out evals/example-first
```

`--dry-run` 验证配置与输入选择，不调用模型、不写文件。`--traces` 可接单个 run 或直接包含多个 run 的目录；不递归搜索，重复路径只算一次。分叉通过自身 provenance 查找源 benchmark/subset/task 的参考，结果仍保留分叉 task/run 身份。`--out` 必须是新目录，防止新批次盖掉之前的证据。默认还在每份 trace 的 `evals/` 写当前配置的最新判定；加 `--read-only-traces` 只写独立批次，不修改源 trace。

主要结果是 `report.json` 与中文 `report.md`：输入轨迹数、有效判定数、判对、判错、无法判定分别列出，正确率是“判对 / 有效判定”，覆盖率是“有效判定 / 提供的轨迹”。没有有效判定时正确率为 `null`。没有提供的其他 benchmark 任务不进入分母；同一任务的不同轨迹分别计数，所以这是本批轨迹的任务正确性，不应称为整个 benchmark 的任务成功率。旧 runner 同时产生的 `*_summary.json` 保留覆盖策略下的旧统计口径；本阶段使用新的 `report.*` 作为主要报告，不混用两种分母。

缺少参考、任务文字、可用截图、损坏的 trace、judge 调用失败或无有效总分，都记无法判定，不算任务判错。已知的提示快照、录制时间、模型与路由身份、响应及 `eval_usage` 随判定或错误保存；endpoint 只保留摘要，脱敏配置不存密钥。没有报告用量的请求单列，不能当成免费。输出目录保存本批副本；同配置重复评测时，源 trace 的同 slug 文件仍采用既有的最新判定语义。

“今日”“明天”和时效数值按录制时点、录下的任务日期及页面证据解释。评测器不访问当前网站，不以模型的当前知识替代历史事实。参考资料自身过时或轨迹无法支持事实时，应无法判定。`stepwise` 会按配置筛选与缩放截图；`final` 只看最后一帧。需要判断最后动作后的结果时，新录制应开启 `terminal_observation: true`；旧 trace 没有该帧不能补造。

退出码：0 表示每份输入都有有效判定（任务判错也返回 0）；1 表示至少一份无法判定或本次评测运行失败；2 表示配置、输入路径或输出目录被拒绝。本入口没有额外 token 上限；正式运行前先用明确的小批次核算 judge 用量，再决定扩展规模。

## 第一次运行

```bash
uv sync --extra dev
export OPENAI_API_KEY="你的密钥"
uv run spectragent run --config configs/browser_use_local.yaml --dry-run
uv run spectragent run --config configs/browser_use_local.yaml \
  --start-url https://example.com --task "报告当前页面标题"
```

本地配置默认打开可见的 Chrome 窗口。Chrome/Chromium 必须已安装；自动发现失败时设置 `CHROME_EXECUTABLE_PATH` 为可执行文件路径。`--start-url` 让 browser capability 在 agent 开始前打开页面，三个 agent 都能使用。它要写成一个完整的 `http://` 或 `https://` URL：只写主机名（`example.com`）、写别的协议头、写几个站点，`--dry-run` 就报错，消息点名这个起始页。`spectragent run` 会读取仓库根目录的 `.env`，也接受已导出的环境变量。`--dry-run` 会验证 YAML、实例化配置并执行 `AgentRuntime.prepare()` 的协商检查；不会启动 Chrome 或调用模型。

## 选择 agent

| 想运行的 agent | 现成 YAML | 页面输入与动作 | 单任务命令 |
|---|---|---|---|
| BrowserUseAgent | `configs/browser_use_local.yaml` | 默认 DOM 文本，按元素索引操作；也支持 DOM + 截图或纯截图 | `uv run spectragent run --config configs/browser_use_local.yaml --start-url https://example.com --task "报告当前页面标题"` |
| SkyvernAgent | `configs/skyvern_agent_local.yaml` | Skyvern DOM 清洗与 HTML 序列化、动作历史和结构化输出 | `uv run spectragent run --config configs/skyvern_agent_local.yaml --start-url https://example.com --task "报告当前页面标题"` |
| UITarsAgent | `configs/ui_tars_local.yaml` | 只向模型发送截图，解析 `Action:` 文本中的坐标动作 | `uv run spectragent run --config configs/ui_tars_local.yaml --start-url https://example.com --task "报告当前页面标题"` |

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

按键发给 Chrome 时只带 `key`、`code`、Windows 按键码和文字，不带系统原生按键码：`browser.send_keys`（UI-TARS 的 `hotkey` 也是它）、`human` 节奏的逐字键入，以及 `browser.type` / `browser.type_coordinate` 写字之前的清空（全选加 Backspace）都是这样。以前带着原生按键码，在 macOS 上页面自己不处理的按键（最常见的是在搜索框里回车、表单原生提交，焦点被弹窗抢走时的清空也会）让 Chrome 打开它自己的"关于 Chrome"页（`chrome://settings/help`）并提到前台，agent 的页面从此是后台页面：页面脚本读到 `document.hidden`，动画帧停了，截图照常，靠动画帧绘制的页面却画不出来。页面的按键处理函数读到的 `key`、`code`、`keyCode` 不变，React 式的回车处理照常收到回车。带修饰键的回车照真实浏览器的规矩：表单里 `meta+enter` 把结果开在新标签页，`shift+enter` 开在新窗口。见 [architecture_design.md](architecture_design.md) §4.13 "按键事件不带原生按键码"。

`browser.navigate`（setup 打开起始页用的也是它）分两段等：先等页面提交，即 Chrome 回应这次导航，最多 `timeout_ms`（默认 30 s）；再等 `wait_until`（默认 `load`），最多 `lifecycle_timeout_ms`（不写时同 `timeout_ms`）。页面提交了而 `load` 没按时到（常见于有资源一直加载不完的页面，页面其实已经画出来了），记一条 WARNING，导航照常算成功。服务器一直不回应、Chrome 拒绝这个 URL、Chrome 报导航失败（主机连不上、204、下载），导航都失败；setup 打开起始页失败时报 `could not open start_url '<URL>' — <原因>`，run 以环境失败结束，不调用模型。这样的页面之后第一次观测还会在落定上限处等一次 `load`（见下一段），之后的观测不再等它。

导航等完之后、采集之前，观测还会等页面落定：主 frame 的文档加载完，再连续 `settle_quiet_ms`（默认 500 ms）没有 DOM 变化、也没有资源加载完成，最多 `settle_max_wait_s`（默认 5 s），到了就按当时的样子采集（`0` 关掉）。两个旋钮写在 capability 块上，DOM、截图与元数据共用这一次等待，只看页面状态，不比截图；每个观测 fragment 的 `settle` 记录写着这次等了多久、是不是被上限截断。代价是每次观测至少多等一个静默窗口，一直在变的页面（轮播、视频）每一步等满上限。点击之后页面的变化要等一个比窗口长、期间又什么都不发生的请求时，仍然看不到它，这时再给 `browser.click_coordinate` 配 `post_delay_ms`。设计与实测见 [architecture_design.md](architecture_design.md) §4.14 "观测之前等页面落定"。

点了一个在新标签页打开的链接（`target=_blank` 的链接或表单、调用 `window.open` 的按钮），下一次观测看到的就是新标签页，和真实浏览器一样，对每个 agent 都如此，UI-TARS 这种没有 `browser.switch_tab` 的也一样。跟过去的是动作期间当前页打开的 tab，有好几个时取最后打开的那个；页面在没有用户点击的情况下自己开的 tab（加载时弹的、计时器弹的、一次点击开的第二个窗口，真实浏览器会拦下的那些）不跟，只列在 tab 列表里。跟过去的 tab 自己关掉时，焦点回到打开它的 tab。导航与落定两段等待都在跟过去的 tab 上做；这次观测的每个 fragment 记下 `focus_moved`（为什么移、从哪个 tab 到哪个 tab），agent 看不到这条记录，提示词只因为页面换了而不同。capability 块上写 `new_tabs: stay` 可以不跟（这之前的行为，新 tab 只能用 `browser.switch_tab` 过去）；默认是 `follow`，录制、回放和分叉要用同一个值。见 [configs/README.md](../configs/README.md#new-tabs) 与 [architecture_design.md](architecture_design.md) §4.14 "页面新开的标签页"。

`skyvern_agent_local.yaml` 用的 `skyvern` cleaner 不看绘制顺序，被吸顶栏盖住的链接照样有编号。把 `dom_cleaner` 换成 `{ cleaner_type: skyvern_hit_test }` 后，页面会对每个编号元素做点击命中测试，默认（`covered_elements: drop`）所有探测点都点不到的元素不编号；视口外的元素不测，保留编号。见 [configs/README.md](../configs/README.md#dom-cleaners)。

### 浏览器实际平台与预期断言

每次 setup 都在首个业务页面和模型调用前，从 Chrome 内部版本页测量实际桌面平台（`macos` / `windows` / `linux`），不从 UA、网页 navigator 字段或 Agent 宿主猜测。`browser.platform` 是独立观测，三个浏览器 Agent 都读取同一事实；纯视觉 Agent 不增加 DOM，也不会因此看到截图的来源 URL。平台观测在各内置 profile 下可用，按 run 缓存，不每步重新测量。

可在 browser capability 块中写 `expected_platform: macos`（或 `windows` / `linux`），默认 `null` 表示不限定某一平台，但仍必须测量成功。不符、检测失败或不支持的平台都在 setup 拒绝；这个字段不会模拟操作系统，也不改变 `send_keys` 的字面键含义。实际浏览器尚未启动时，`--dry-run` 只能检查配置与源证据，不能声称实测已通过。云 CDP 若不允许读取内部版本页也会明确拒绝；本轮没有真实云服务平台验收，不将 UA 伪装当作跨平台测试。

归档指纹的 `input_platform` 记录实际值。回放先检查源字段存在且合法，启动后再测当前浏览器并比较；源平台与配置预期已冲突时可在 prepare 提前拒绝。缺少新平台证据的旧 run 保留供离线分析，但不能猜一个平台继续执行，也不回写旧归档。

### 只通过页面交互完成任务

需要禁止构造搜索页、详情页或接口 URL 的实验，在原 YAML 的 `hook` 列表中追加：

```yaml
hook:
  - hook_id: ui_navigation_guard
  # 保留原有 trace_writer（及其 archive 配置）、metrics 等 hook
```

守卫在 `env.act` 执行前检查动作，拒绝策略直接 `browser.navigate` / `browser.create_tab`、地址栏/新空白 tab/新窗口快捷键（包括 Ctrl/Cmd/Meta+L、Alt+D、F6、Ctrl/Cmd+T/N 及常见别名，搜索地址栏的 Ctrl/Cmd+K/E 也拒绝），以及 `browser.type` / `browser.type_coordinate` 的整段 URL 或主机路径输入。给定起始 URL 由 setup 打开，不经过这个 hook，所以仍可启动；页面链接点击、后退/前进、切换已有 tab、普通编辑键（Ctrl+A/C/V 等）、选择、滚动、拖动和页面内普通输入继续允许。没有该 hook 的配置保持原行为。

拒绝通过 `RunAbort` 作为协议否决终止，`result.agent_done=vetoed`，trace 保留原回复、被拒动作和原因；不是动作尝试后的失败，底层 `env.act` 不会执行。所有模型的提示仍应写明同一协议，旧 trace 仅审计，不回写。这个守卫不读 DOM，也不推断任意文本中的拼 URL 意图：包含链接的整句话不会仅因有点号被拒；完整的域名形状 token 会按地址处理，因此域名形状的文件名可能有歧义。应结合原回复与轨迹审计协议遵守情况。

## 选择浏览器后端

切换的是 `environment[0].capabilities[0].backend`，与 `agent_id` 无关。例如直接使用现成的云浏览器配置：

```bash
export SKYVERN_API_KEY="你的密钥"
uv run spectragent run --config configs/browser_use_skyvern.yaml \
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

`pinned` 启动 Chrome 时设环境变量 `TZ=<pinned 的时区>`：一个 iframe 或弹窗在同进程里设时区的那个页面走了之后，读到的仍是这个时区。语言做不到这一点（macOS 上 Chrome 取系统语言），所以录制把浏览器的默认时区与语言记进指纹，回放只在默认值相同的机器上进行：不同就在 `--dry-run` 报错，系统设置在启动前说不出的那一项在浏览器启动之后报，消息点名两边的值。页面自己打开的新 tab（`target=_blank` 链接、`window.open`）从第一个请求起就被录下、pin 住，开 tab 不再多等。

颜色主题由 pinned 在录制启动时实测，不默认设成浅色：测到的 `light` / `dark` 固定在本 run 的所有 tab 和进程外 iframe，并写进 `replayability.color_scheme`。回放沿用这个源值，宿主机主题不同也可以回放；源值缺失或非法则在 `--dry-run` 拒绝，必须重录。测量失败会拒绝录制。现有 `reduced_motion` 仍同时生效，`none` 不改变主题，网站自己保存的主题偏好仍由网站决定。

`pinned` 的 `user_agent` 可以写普通 Chrome 标识，即 Chrome 自己的写法，后面不跟 `Edg/` 之类，例如 `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36`。这时页面读到的 `navigator.userAgentData`（brands、平台、是否移动端）和请求带的 `Sec-CH-UA` 取自浏览器自己：浏览器启动之后先读出来，再随标识一起发给每个页面，页面新开的 tab 的第一个请求也一样。所以版本要写本机 Chrome 的大版本（Chrome 可执行文件加 `--version` 就能看到），平台要与本机相同：不符时浏览器启动之后、打开任何页面之前报 `BrowserCooperationError`，点名两边的版本或平台。这一项 `--dry-run` 查不出来，要启动浏览器才知道；Chrome 自动更新之后，改标识里的版本号就行。不写 `user_agent` 时页面读到的也是浏览器自己的。写别的形式的字符串时不带这些：brands 为空，不发 `Sec-CH-UA`。回放要求与录制带的相同，不同就拒绝。

连到本机 CDP 端点（`localhost`、`127.0.0.0/8`、`::1`，`local_chrome` 启动的浏览器就在这里）的连接直连、不压缩，不读 `http_proxy` 等代理环境变量，本机开着代理（如 Clash）时 CDP 流量也不经过它。远端端点（云后端）仍按 websockets 的默认读环境代理、协商压缩。

## 跑数据集

单任务命令只接收自由文本。需要数据集身份、任务起始 URL、逐任务 trace 时，用 `spectragent bench`。基准数据不包含在安装包中；数据根目录约定为 `<root>/browser/<benchmark>/data_info.json`。

```bash
export OPENAI_API_KEY="你的密钥"
export SPECTRAGENT_BENCHMARK_DATA="$HOME/agent-data"
uv run spectragent bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --list
uv run spectragent bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --limit 1 --no-eval
```

把 `--config` 换成上面的其他 agent YAML，就使用同一个数据集入口。也可以用 `--task-id <ID>` 指定任务，或用 `--limit N` 控制数量。`--list` 会先加载数据并只打印选中的任务 ID，不启动浏览器。数据集 loader 把每个任务的 `start_url` 传给 browser capability，trace 记下的也是这一个。

起始页必须是完整的 http(s) URL。`lexbench_browser` 的数据（每个 split）把起始页写成不带协议头的主机名，例如 `www.bilibili.com`、`s.weibo.com/top/summary`，加载器把它补成 `https://www.bilibili.com` 再交出去，飞轮报告和导出按这个 URL 的主机认站点。写成文字或几个站点的起始页（L1 有 7 个、L2 有 3 个，例如 `www.xiachufang.com或其他美食网站`、`jd.com + tmall.com + suning.com`）原样留下，在 `prepare()` 被拒，不打开 trace，消息点名这个起始页：这样的任务若是 sweep 的第一个任务，开跑前的预检查停下，退出码 2；否则只记在那个任务上，汇总表里那一行写着原因（算作 infrastructure failure，退出码 1）。其他数据集的起始页本来就是完整的 URL，加载器不改。`spectragent bench` 不调用 `load_dotenv()`，所以运行前要在 shell 导出模型及云后端需要的密钥。需要自动评分时，在 YAML 中配置 `evaluator:`，然后去掉 `--no-eval`；只有任务数据与 trace 不会自动产生评分。

按 Ctrl-C 中断时，正在跑的任务照常收尾：trace 写出 `result.json`（`agent_done: error`，`error: "Execution cancelled"`），进行中的那次 LLM 调用以 `cancelled` 状态连同 prompt 落盘，浏览器也会关掉。随后中断继续往外传，sweep 结束，不打印汇总表，也不评分。

要在认出风控页之类的情况时停掉整个 sweep，可以写一个 `blocking` 的 guard hook，在它的 handler 里抛 `RunAbort`（`spectragent/core/loops/errors.py`）。被否决的任务同样先收尾，sweep 在它这里停下：日志里留一条 `sweep vetoed at <task_id> after N/M task(s)`，汇总表和评分只包括之前跑完的任务。否决与 run 自己的超时或收尾失败同时发生时，sweep 照样停下；只有外部中断比否决优先。

## 看结果与定位入口

每次运行的 trace 写在 `traces/<run_id>/`，配置里的 `hook: [{hook_id: trace_writer}]` 决定是否落盘。`manifest.json` 是已提交内容的唯一索引（`format_version: 4`），`config.json` 是配置快照，`steps/NNN.json` 是每步的记录。`observations/` 存观测和截图，文件名带全限定的 fragment key，如 `000_browser.dom.json`、`000_browser.screenshot.png`。`messages/` 每次 LLM 调用一个 `NNN_call_KK.json`（parse 重试、校验失败、截断、被取消的调用各占一个）；请求了结构化输出时，schema 每个 run 只在 `messages/output_formats/` 存一份。prompt 里内联的图片（vision agent 发给模型的截图）每张每个 run 只在 `messages/images/` 存一份（`<sha256>.png` 之类，就是那张图片本身），调用记录里在图片的位置写一个引用；所以带着全部历史截图的 agent，`messages/` 也只随不同截图的张数增长。直接打开 `NNN_call_KK.json` 看到的是引用，经 `RunTrace` 读回的是完整的 prompt。绑定了归档器的录制 run 还有 `env/`；现成的评测 YAML 不录环境证据，没有这个目录。

run 开始之后，不论完成、超时、出错、被否决还是被外部中断，都会写 `result.json`，它的 `agent_done` 说 agent 怎么结束（`done`、`max_steps`、`error`、`timeout`、`vetoed`）。manifest 的 `status` 只说 trace 记全没有：写着时是 `writing`，收尾之后是 `complete`，或 `incomplete` 加上 `incomplete_reason`（某一步或收尾时有东西没记下来）和 `complete_through`（完整到第几步）。超时的 run 照样可以是 `complete`。`result.json` 的 `metrics.steps` 只数策略步，即 `steps/` 里 `origin` 为 `policy` 的步。`agent_loop.terminal_observation: true` 在 run 结束后多记的一次观测（`origin: terminal`），以及分叉 run 恢复出的前缀步（`origin: restored`），都在 `steps/` 里，但不计数。模型调用次数另记在 `metrics.llm_calls`。`spectragent bench` 会在终端先列出三本账的计数（agent 怎么结束、环境、trace 完整与否），再列每个任务的终止状态与 trace 目录名；环境坏了、模型在重试之后仍不可用、trace 不完整都标成 infrastructure failure，退出码 1；guard 否决了某个任务时 sweep 停在那里，退出码 3。

用 `RunTrace` 读 trace，参数是 `Path`：

```python
from pathlib import Path
from spectragent.core.trace import RunTrace

trace = RunTrace(Path("traces/<run_id>"))
trace.result.metrics.steps   # 策略步数
trace.llm_messages(0)        # 第 0 步的每一次 LLM 调用
trace.screenshot(0)          # 第 0 步截图的字节
```

它只按 manifest 找文件，manifest 没列出的文件不算数；列出了却读不回来的文件（缺失、为空、损坏、类型不符）抛 `TraceSchemaError`，点名那个文件。`format_version` 不是 4 的旧 trace 整个读不出来（构造 `RunTrace` 时就抛 `TraceSchemaError`，点名版本）：不做兼容，只能重跑任务。格式 2 的 `status` 把 agent 的结局和 trace 是否完整混在一起，格式 3 把每张历史截图在每条调用记录里再存一遍，都已经换掉。调用记录引用的图片缺失或与它的摘要不符，读那一步的调用时同样抛 `TraceSchemaError`，点名那张图片。

需要继续改入口时，按执行顺序看 [单任务入口](../spectragent/core/run_cli.py)、[基准入口](../spectragent/core/benchmark_runner.py)、[装配器](../spectragent/core/composer.py)、[运行时](../spectragent/core/runtime.py)。agent 的配置与输出协议分别在 `spectragent/agents/browser_use.py`、`skyvern.py`、`ui_tars.py`；browser capability 与 backend 在 `spectragent/core/capabilities/browser/`。配置字段细节见 [configs/README.md](../configs/README.md)。

## 录制、回放与训练导出

数据飞轮把 browser run 连同它的网络、存储和原始 DOM 一起录下，再离线回放、从录制的检查点分叉、导出训练数据，最后出一份度量报告。用法见 [数据飞轮运行手册](data_flywheel_runbook.md)，配置字段见 [configs/README.md](../configs/README.md)。各步的入口：

| 用途 | 命令 |
|---|---|
| 录制（`pinned` 加三个归档器） | `uv run spectragent run --config configs/flywheel/record_ui_tars_pinned.yaml --start-url URL --task "任务"`；数据集换用 `spectragent bench` |
| 用录制的网络离线重跑 | `uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml --start-url URL --task "录制时的任务" --replay-archive traces/<录制的 run_id>` |
| 从录制的检查点分叉 | 把 `configs/flywheel/branch_ui_tars_archive.yaml` 的 `task_loader.data_dir` 指向录制所在的目录，再 `uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval` |
| 导出训练数据 | `uv run spectragent export --config configs/flywheel/export_sft.yaml` |
| 跑整条链并出度量报告 | `uv run spectragent flywheel run --spec chain.yaml`；只重算报告用 `uv run spectragent flywheel report --spec chain.yaml` |

`--replay-archive` 只能配 `network_source: archive`，YAML 写的是 `live` 时 setup 会拒绝。`spectragent export` 只读 trace，不启动浏览器，也不调用模型。`chain.yaml` 要自己写，格式见 [configs/README.md](../configs/README.md#measuring-the-flywheel)。`spectragent flywheel` 和 `spectragent bench` 一样不读 `.env`，跑之前先在 shell 里导出密钥（`set -a; . ./.env; set +a`）。
