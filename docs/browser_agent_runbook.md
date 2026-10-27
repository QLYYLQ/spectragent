# Browser agent 运行手册

从仓库根目录执行下列命令。当前执行链是 `YAML → ConfigComposer → AgentRuntime.execute()`；单任务入口是 `spectragent run`，数据集入口是 `spectragent bench`。`agent_id` 选模型理解页面和输出动作的方式，`capability_id: browser` 提供浏览器动作与观察，`backend.provider_id` 选 Chrome 会话的来源。这三项可以分别配置。

## 同一实验共享 Chrome

`configs/browser_use_shared.yaml` 使用 `backend: {provider_id: local_chrome, lifetime: experiment}`。`spectragent bench --config configs/browser_use_shared.yaml ...` 的整个 sweep 共用进程，每个 task 新建独立 BrowserContext，分别记录网络与存储；`spectragent run` 也拥有显式实验作用域，结束后关闭 Chrome。配置检查与 dry-run 不启动浏览器。离线回放将 backend 设回 `lifetime: run`。

并发 Python 调用方显式持有作用域，每 run 仍重新 compose。资源使用 Composer 的 provider instance identity（内联实例按稳定构造顺序编号）；相同 identity 的配置必须一致，不同 identity 拥有不同进程。不要复用一个 capability 执行两个 run。

```python
import asyncio
from spectragent.core.experiment import ExperimentScope
from spectragent.core.runtime import AgentRuntime

async with ExperimentScope() as experiment:
    runs = [experiment.bind(composer.compose(config)) for _ in tasks]
    await asyncio.gather(*(
        AgentRuntime.for_agent(run, "main").execute(task=task)
        for run, task in zip(runs, tasks, strict=True)
    ))
```

每个 run 结束只释放自己的 context；实验作用域结束关闭共享进程。磁盘登录 profile 不能自动继承到新 context，改用显式会话 state 导入；持久 profile 继续使用独占进程。共享的 browser-wide recorder 会短暂暂停未参与 context 的新 target 并立即放行、detach，不归档或关闭其页面。它不是给任意外部 CDP 端点增加录制所有权；进程故障仍会影响全部共享 run。

## 统一命令与结果界面

安装或更新工作区后执行 `uv sync --extra dev`。统一入口为 `uv run spectragent --help`；模型、浏览器与实验参数继续写在 YAML，不需要在界面里重新配置一遍。

需要新的配置文件时，可以直接生成完整的注释模板：

```bash
uv run spectragent config template \
  --agent browser_use.dom --loop react --llm openai.direct --model YOUR_MODEL \
  --environment browser --backend local_chrome \
  --record metrics --replayability none --output experiment.yaml
uv run spectragent config check experiment.yaml
# 将已有选型和参数补全为带注释的配置；不会修改原文件
uv run spectragent config template --from configs/browser_use_local.yaml --output expanded.yaml
```

新模板的默认选型是 BrowserUse（`browser_use.dom`）＋ReAct、`openai.direct`、simple memory、local Chrome、instant 输入、BrowserUse cleaner、text serializer、live 网络、无回放固定；型号必须用 `--model` 提供。所选组件和受体的配置字段、默认动作及各自参数全部展开，说明与约束来自当前 schema；未选的可选组件不展开。注释标明来自模板选型、用户参数、骨架或 schema 的默认值。新模板只启用 metrics，含模型报告的 token、调用次数和计时；可以重复 `--record metrics --record errors`，也可以用 `--record all` / `--record none` 全开或全关四个内容通道。`none` 不删除 trace writer，也不关闭骨架中独立选择的 archive。

`--replayability none|pinned` 只控制回放固定，`network_source: live|archive` 与 `trace_writer.archive` 独立；新模板为 live 和 `archive: []`，离线回放用 `--from configs/flywheel/replay_ui_tars_archive.yaml` 保留其网络与归档选择。生成只校验配置和装配，不启动 Chrome、不调用模型、不打开回放任务证据；任务相关输入仍在 `config check` 或 `run --dry-run` 中验证。必填项缺失会列出字段位置，框架不编造型号或路径。没有 `--output` 时 stdout 是纯 YAML；输出路径存在会拒绝覆盖。

`--from` 保留已有值，包括明确填写的凭据，它是生成可运行配置的工具；查看脱敏报告使用 `config explain`。选择同一实现保留其参数；用选择 flag 更换实现会重置该实现的标量参数，保留同名受体与实例 id。多实例骨架可直接展开；若选择 flag 无法唯一定位要修改的组件，编辑骨架后再生成。

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

普通交互终端显示 Rich 实时进度；加 `--tui` 使用全屏界面。TUI 的阶段来自当前进程，显示的动作步骤标为已观察，是否持久化由 Trace 查看器确认。按 `q` 或在活动表格按 Ctrl-C 关闭正在执行的界面会请求取消并等待 trace 和环境收尾。详情与日志支持鼠标或 Shift+方向键选择，F7 全选，Ctrl-C 复制选区（无选区时复制当前文本）；F8 直接复制详情或当前日志。日志有选区时暂缓刷新。本机 macOS 使用系统剪贴板，SSH 使用终端 OSC 52 剪贴板支持。不存在暂停或原地恢复的暗含操作。

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

分叉页面直接显示 `source/restored` 历史策略与 `strict/warn` 恢复策略，时间线标出 checkpoint 和可测差异。Observation 中并排查看 Source record 与 Actual replay，包括截图、URL/DOM 校验、动作重定位、原结果与实际结果；通过恢复校验仍可能存在容差内的截图差异。Agent observation 默认按录制的 cursor 配置重建模型图片；Browser state 切回干净截图。cursor 开启但位置未知时保持原图并明确标注，精确发送的 prompt 图片在 Model calls 查看。

恢复步骤的源动作、源结果和源模型回复与本次实际恢复执行分开展示。实时终端把 `max_steps` 标为配置上限，并单列 `continue_steps`，不把配置上限当成分叉的实际步数预算。回放日志页按原有读取器的折叠权重统计 hit/miss/exhausted/synthetic，包含 trailing 收尾决定；未记录日志、空日志和日志损坏分别显示。

查看器只读本地目录，展示截图、步骤、模型请求/回复、每次失败与重试、观测、评测、配置和分叉来源。运行对比与实验组对比分开：实验组按配置摘要选择，按相同任务配对，必须明确选择 evaluator；重复尝试记为 ambiguous，不自动挑最好的一次。判分配置不同不合并统计。报告页读取同一根目录下的独立评测、飞轮、诊断、数据集和任务清单；可以把 `--root` 指向包含这些产物的共同父目录。

服务默认只监听 `127.0.0.1`。SSH 使用时在服务器运行 view，在本机建立端口转发后打开 `http://127.0.0.1:8765`：

```bash
ssh -L 8765:127.0.0.1:8765 your-server
```

网页前端已经随 Python 包分发，使用者无需 Node。修改前端源码时，在 `frontend/viewer` 执行 `npm ci`、`npm run build`，产物写入 `spectragent/viewer/static/`。界面不会重新执行网络回放或调用 judge；这两项仍由各自运行命令明确启动。

`view` 在前台运行。在启动它的终端按 Ctrl-C 会关闭监听端口，未完成请求有 2 秒退出宽限；关闭浏览器标签不会停止服务。若另一个已脱离终端的 viewer 仍占着端口，先用 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 查 PID，再用 `ps -p PID -o command=` 确认是预期的 `spectragent view`，然后 `kill -INT PID`；不要按端口盲目结束其它程序。

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

每步动作上限由 `agent_loop.max_actions_per_step` 唯一决定。Runtime 将有类型的 `ExecutionLimits` 交给 Agent 原有 `prepare`，三个发货 Agent 用它构造自己的提示；消费声明或异步提示入口不匹配时，`--dry-run` 就拒绝。`metrics.execution_ms` 从环境 setup 前计到 Loop 结束（含分叉恢复、终止观测），不含结束 hook 与资源释放；任务表的 `elapsed_ms` 则包含完整收尾和结果回读。超时预算仍只覆盖 `agent.run`。

### 本地 Chrome 与外部端点的所有权

本地 provider 连接前验证本次启动进程、CDP 端点与 profile；端口占用、启动退出或 profile 转交旧实例时明确拒绝，不接管旧 Chrome。正常结束、失败和取消均只清理本次进程与临时 profile。手工设置的持久 profile 不会删除。显式 `cdp_url` 仍可连接现有浏览器，结束时只清理 AF 的连接、订阅、bridge 和 scope，已有 tab、外部进程与 profile 保留；归档/pinned/replay 的协作限制照常检查。

浏览器事件接口依赖发布版 `agent-cdp==0.5.0`。归档队列容量为 65536，观测、JS 和归档 scope 不保留事件历史；收集用原生前缀 flush 并检查公开完成/失败/取消/拒收统计。动作要求实际执行处理者及合法业务结果，排队动作在发起者退出等待时撤销；已执行的浏览器副作用没有自动回滚承诺。DOM 自抓或归档失败按记录缺口报告，不改写实际动作结果。WARC revisit 只引用当前收集批次，前批记录失败不会破坏后批的引用。

`action_filter=None` 表示未额外过滤，`[]` 表示不允许动作；trace 的 `action_space` 同时保存原输入、最终 prompt 描述、guard 全限定集合及绑定，参与实验配置摘要及 source 历史分叉兼容检查。

## 选择 agent

| 想运行的 agent | 现成 YAML | 页面输入与动作 | 单任务命令 |
|---|---|---|---|
| BrowserUse（`browser_use.dom`、`browser_use.dom_vision`） | `configs/browser_use_local.yaml` | DOM 文本，按元素索引操作；`browser_use.dom_vision` 另给模型看截图 | `uv run spectragent run --config configs/browser_use_local.yaml --start-url https://example.com --task "报告当前页面标题"` |
| Skyvern（`skyvern.dom`、`skyvern.dom_vision`） | `configs/skyvern_agent_local.yaml` | Skyvern DOM 清洗与 HTML 序列化，按元素序号操作，动作历史带每个动作的返回内容，闭合的结构化输出；`skyvern.dom_vision` 另给模型看截图，可另用 `click_coordinate` | `uv run spectragent run --config configs/skyvern_agent_local.yaml --start-url https://example.com --task "报告当前页面标题"` |
| UITarsAgent | `configs/ui_tars_local.yaml` | 截图驱动，解析 `Action:` 坐标动作，并接收逐项执行反馈 | `uv run spectragent run --config configs/ui_tars_local.yaml --start-url https://example.com --task "报告当前页面标题"` |

三个文件中的 `llm` 都只是可编辑的 OpenAI 示例。运行 UI-TARS 专用模型时，把 `llm.model_type`、`model`、`provider`（必要时 `base_url`）改为该模型实际提供的 API；`coordinate_space` 要与它输出的坐标一致，值为 `screenshot_pixels` 或 `normalized_1000`。`prompt_template` 可选 `computer_use`、`grounding`、`mobile_use`。`ui_tars` 不请求 DOM，浏览器不会为这个 agent 提取 DOM。

UI-TARS 的 Task 只在初始 system 中声明；浏览器实测平台在首次观测时加入一次 system 上下文。每轮 user 消息包含当前截图、步数和上一批动作的逐项执行结果，区分返回成功、返回失败、没有返回结果的动作及被截断的数量。它通过 `loop.history` 获取执行事实，由 Agent 渲染后交给已配置的 memory strategy 保存和裁剪；memory 不自行从 trace 补结果或去重 Task。`success` 只说明执行器结果，是否达成页面目标仍需检查截图。新执行或独立导出段重新初始化平台；老轨迹保留原件，不能用新提示伪造旧 prompt 的逐字历史重建。

模型客户端在一次任务或独立评测执行内惰性创建并复用，结束时由相应执行入口关闭。Runtime 统一清理主模型与显式绑定的提示辅助模型；独立 judge 由评测入口收尾。调用方注入的 HTTP transport 默认借用，框架不关闭它。成功、失败、超时和取消都走有界清理；清理异常单独报告，不改变已取得的模型回复、用量或原始错误。后一次执行重新创建自建 transport，prepare/dry-run 不打开连接。

BrowserUse 是一个 agent 家族，用 `agent[0].agent_id` 选成员：

| `agent_id` | 模型看到 | 点击方式 |
|---|---|---|
| `browser_use.dom` | DOM 文本 | 元素索引 `browser.click` |
| `browser_use.dom_vision` | DOM 文本和截图 | 元素索引，另有坐标 `browser.click_coordinate` |

两个成员都要求 DOM，配 `profile: vision_only` 会在协商时失败。只看截图的运行用 UI-TARS。截图在两个成员下都写入 trace。若页面动画导致坐标点击落在过时画面上，可给 `actions:` 中的 `browser.click_coordinate` 配 `post_delay_ms`；显式列出 `actions:` 后，也要列出该运行所需的其他动作，包括 `browser.navigate`（数据集起始 URL 要用它打开）。不写 `actions:` 是全部动作，`actions: []` 是一个动作都没有。详细例子见 [configs/README.md](../configs/README.md#vision-agents)。

动作引起的主 frame 跨文档导航不需要这个延迟，比如提交表单、在文本框里回车、点链接、设置 `location.href`。观测在每次采集尝试之前先等在途的导航提交或停止加载，每次最多 10 s，到时仍未结束就照常采集并记一条警告；没有在途导航时不等。所以提交表单后，下一次观测看到的是结果页。观测截图另有 10 s 上限：Chrome 不回应时，这次尝试算缺截图，再采一次（每次观测最多尝试两次）。页面定时器在观测开始采集之后才发起的导航不在这个等待之内，只受截图上限约束。

按键发给 Chrome 时只带 `key`、`code`、Windows 按键码和文字，不带系统原生按键码：`browser.send_keys`（UI-TARS 的 `hotkey` 也是它）、`human` 节奏的逐字键入，以及 `browser.type` / `browser.type_coordinate` 写字之前的清空（全选加 Backspace）都是这样。以前带着原生按键码，在 macOS 上页面自己不处理的按键（最常见的是在搜索框里回车、表单原生提交，焦点被弹窗抢走时的清空也会）让 Chrome 打开它自己的"关于 Chrome"页（`chrome://settings/help`）并提到前台，agent 的页面从此是后台页面：页面脚本读到 `document.hidden`，动画帧停了，截图照常，靠动画帧绘制的页面却画不出来。页面的按键处理函数读到的 `key`、`code`、`keyCode` 不变，React 式的回车处理照常收到回车。带修饰键的回车照真实浏览器的规矩：表单里 `meta+enter` 把结果开在新标签页，`shift+enter` 开在新窗口。见 [architecture_design.md](architecture_design.md) §4.13 "按键事件不带原生按键码"。

`browser.send_keys` 只接受按键：命名键（`Enter`、`Escape`、`ArrowDown`、`F5`……）、单个字符，或用 `+` 把修饰键连到一个键上的组合键（`Control+A`、`Shift+Tab`）。加号键本身写成单独的 `+`，在组合里写成末尾的 `++`（`Control++`）；`ctrl+`、`+a`、`++` 这类空分段照旧拒绝。其他字符串（整段 URL、一个词、一句话）在发出任何按键之前就被拒绝，动作失败，错误写明 "To enter text, use type"：输入文字要用 `browser.type` / `browser.type_coordinate`。页面没有变化不算输入错误。见 [architecture_design.md](architecture_design.md) §4.13。

按编号（元素 index）点击或滚动一个元素时，先把它滚进视口：`DOM.scrollIntoViewIfNeeded` 之后元素中心仍在视口外（只露出一部分、比视口还高），就用脚本把它即时居中再量一次（即时滚动，页面的 `scroll-behavior: smooth` 不会让量到的还是旧位置）；仍在视口外（例如固定在屏幕外的已收起面板）就拒绝这个动作并说明原因，不在视口外派发一次 Chrome 会丢掉、却报告成功的按下。见 [architecture_design.md](architecture_design.md) §4.5 "动作效果"。

`browser.navigate`（setup 打开起始页用的也是它）分两段等：先等页面提交，即 Chrome 回应这次导航，最多 `timeout_ms`（默认 30 s）；再等 `wait_until`（默认 `load`），最多 `lifecycle_timeout_ms`（不写时同 `timeout_ms`）。页面提交了而 `load` 没按时到（常见于有资源一直加载不完的页面，页面其实已经画出来了），记一条 WARNING，导航照常算成功。服务器一直不回应、Chrome 拒绝这个 URL、Chrome 报导航失败（主机连不上、204、下载），导航都失败；setup 打开起始页失败时报 `could not open start_url '<URL>' — <原因>`，run 以环境失败结束，不调用模型。这样的页面之后第一次观测还会在落定上限处等一次 `load`（见下一段），之后的观测不再等它。

导航等完之后、采集之前，观测还会等页面落定：主 frame 的文档加载完，再连续 `settle_quiet_ms`（默认 500 ms）没有 DOM 变化、也没有资源加载完成，而且视口里看得见东西（文字、图片、视频、canvas、SVG 图形、iframe、输入框或按钮；只有 CSS 涂的加载条或底色的白页不算），最多 `settle_max_wait_s`（默认 5 s），到了就按当时的样子采集（`0` 关掉）。两个旋钮写在 capability 块上，DOM、截图与元数据共用这一次等待，只看页面状态，不比截图；每个观测 fragment 的 `settle` 记录写着这次等了多久、是不是被上限截断。代价是每次观测至少多等一个静默窗口，一直在变的页面（轮播、视频）和视口一直空着的页面每一步等满上限。点击之后页面的变化要等一个比窗口长、期间又什么都不发生的请求时，仍然看不到它，这时再给 `browser.click_coordinate` 配 `post_delay_ms`。设计与实测见 [architecture_design.md](architecture_design.md) §4.14 "观测之前等页面落定"。

点了一个在新标签页打开的链接（`target=_blank` 的链接或表单、调用 `window.open` 的按钮），下一次观测看到的就是新标签页，和真实浏览器一样，对每个 agent 都如此，UI-TARS 这种没有 `browser.switch_tab` 的也一样。跟过去的是动作期间当前页打开的 tab，有好几个时取最后打开的那个；页面在没有用户点击的情况下自己开的 tab（加载时弹的、计时器弹的、一次点击开的第二个窗口，真实浏览器会拦下的那些）不跟，只列在 tab 列表里。跟过去的 tab 自己关掉时，焦点回到打开它的 tab。导航与落定两段等待都在跟过去的 tab 上做；这次观测的每个 fragment 记下 `focus_moved`（为什么移、从哪个 tab 到哪个 tab），agent 看不到这条记录，提示词只因为页面换了而不同。capability 块上写 `new_tabs: stay` 可以不跟（这之前的行为，新 tab 只能用 `browser.switch_tab` 过去）；默认是 `follow`，录制、回放和分叉要用同一个值。见 [configs/README.md](../configs/README.md#new-tabs) 与 [architecture_design.md](architecture_design.md) §4.14 "页面新开的标签页"。

`skyvern_agent_local.yaml` 用的 `skyvern` cleaner 不看绘制顺序，被吸顶栏盖住的链接照样有编号。把 `dom_cleaner` 换成 `{ cleaner_type: skyvern_hit_test }` 后，页面会对每个编号元素做点击命中测试，默认（`covered_elements: drop`）所有探测点都点不到的元素不编号；视口外的元素不测，保留编号。见 [configs/README.md](../configs/README.md#dom-cleaners)。

### 动作效果、采集状态与媒体状态

每个动作在它自己的派发前后测量它实际做了什么，记在 `ActionResult.effect`，随步记录写进 trace（`steps/NNN.json` 的 `action_results`）。点击类（`click`、`click_coordinate`、`drag`）记按下的点、按下前那一点上的元素（角色与可读名）、按编号点击时是否命中目标、执行后的焦点；输入类（`type`、`type_coordinate`、`send_keys`）记接收输入的控件、是否敏感字段、值是否变化（页面里只比较，不记内容也不记长度）、实际发出的键、执行后的焦点；`scroll` 记实际滚动的是页面还是哪个元素、前后偏移、是否到了这个方向的边缘；`navigate`、`go_back`、`go_forward` 记前后 URL 与文档是否加载完；`switch_tab` 记前后标签页；`select_dropdown` 记当前选中项；`wait` 记实际等待的时长。`done`、`screenshot`、`extract_content` 没有效果。探测失败或用完上限的事实记 `unknown`，这里测不到的（例如另一个站点的 frame 里）记 `unavailable`，两者都不是测得的 0；还有余量的滚动在上限内没看到移动，之后的位置记 `unknown`，不写成 0 位移。测量从不改变动作的成败。一批动作之后的页面状态属于下一次观测，不算在某个动作头上。

模型只看到效果自己写的一行英文（`describe()`），从不看到原始字段：UI-TARS 在每个执行结果的 JSON 里多一个 `"effect"`，Skyvern 写在 `<action_history>` 每一行 JSON 的 `result.effect` 里，BrowserUse 写在每步的 `<effects>` 里。敏感字段既不回显内容也不报长度。

`capture_state` 是一个 optional 观测，说明这次采集时环境的样子：采集前的落定等待（`settled` / `capped` / `unknown`、等了多久、哪些还没落定）、环境自己引起的前台变化（页面开了新标签页、当前页自己关掉）、指针最后一次确认的位置（viewport 像素）、视口内加载失败的可见图片（个数与前几个文件名）。三个 Agent 都在每步的 `<capture_state>` 块里看到它；没测到的写明没测到，测得为零才写 none。

截图另带 `media` 元数据：每个 `<video>` / `<audio>` 的 frame、文档顺序、位置、来源种类、总时长，以及截图前后各一次读到的精确播放位置与暂停状态；读不到的子 frame 记作读不到。录制时只读，不暂停、不跳转。它用于回放恢复的媒体校验与对齐（见 [数据飞轮运行手册](data_flywheel_runbook.md)），默认不进提示词。

| 写在哪里 | 字段 | 默认 | 作用 |
|---|---|---|---|
| browser capability 块 | `action_effect_timeout_s` | `1` | 一个动作测量自身效果的上限，秒；只计探测（含等滚轮生效），不计动作本身。超时的事实记 `unknown` |
| browser capability 块 | `capture_state_timeout_s` | `1` | `capture_state` 页内探测（可见图片加载失败）的上限，秒，最多 3；超时记 `unknown`，不影响这次观测的其他部分 |
| browser capability 块 | `media_state_timeout_s` | `1` | 截图前后两次媒体读数各自的上限，秒，最多 1.5；超时记 `unknown`，截图照常。分叉恢复时也是媒体对齐起始读数的上限 |
| 三个 Agent | `show_action_effects` | `true` | 把效果那一行写进提示词；关掉时 trace 照样记录效果，消融的两组测量开销相同 |
| 三个 Agent | `show_capture_state` | `true` | 协商并渲染 `capture_state`；关掉时这个 fragment 不采集、不记录、不渲染 |
| Skyvern、BrowserUse | `show_media_state` | `false` | 在页面状态之后写 `<media_state>` 块；关掉时媒体状态照样随截图记录、用于恢复校验 |
| BrowserUse | `step_history_window` | `10` | 每步消息的 `<agent_history>` 重放最近多少步（每步含回复、动作、结果与效果），至少 1；Skyvern 的对应字段是按动作计数的 `action_history_window` |

`capture_state_timeout_s` 加上两倍的 `media_state_timeout_s` 必须小于 4 s，否则构造 capability 时就拒绝并点名两个字段：一次采集在 30 s 的事件里依次采各个 fragment，DOM 采集、截图命令和三次小读取的固定上限已经占去 26 s，探测超时只能让它自己的事实记为 unknown，不能拖垮整次采集（[architecture_design.md](architecture_design.md) §4.14 "命令上限"）。

`show_capture_state` 改变观测协商，所以它属于分叉与导出的实验身份：取值不同的录制之间不能互相分叉，也不能混在一起导出；录制时没有 `capture_state` 的 run 也不能被协商它的 Agent 分叉。`show_action_effects` 与 `show_media_state` 只改提示词，是显式的实验变量；`history_source: source` 的分叉要求前缀提示与源录制逐字节一致，所以它们也要取与源相同的值。纯视觉实验若一点页面文字都不能读，关掉 `show_action_effects`：效果行会写出命中元素与焦点的角色和可读名。见 [architecture_design.md](architecture_design.md) §4.4 "观测增强按消费方分层"、§4.5 "动作效果"。

### 浏览器实际平台与预期断言

每次 setup 都在首个业务页面和模型调用前，从 Chrome 内部版本页测量实际桌面平台（`macos` / `windows` / `linux`），不从 UA、网页 navigator 字段或 Agent 宿主猜测。`browser.platform` 是独立观测，三个浏览器 Agent 都读取同一事实；纯视觉 Agent 不增加 DOM，也不会因此看到截图的来源 URL。平台观测在各内置 profile 下可用，按 run 缓存，不每步重新测量。

可在 browser capability 块中写 `expected_platform: macos`（或 `windows` / `linux`），默认 `null` 表示不限定某一平台，但仍必须测量成功。不符、检测失败或不支持的平台都在 setup 拒绝；这个字段不会模拟操作系统，也不改变 `send_keys` 的字面键含义。实际浏览器尚未启动时，`--dry-run` 只能检查配置与源证据，不能声称实测已通过。云 CDP 若不允许读取内部版本页也会明确拒绝；本轮没有真实云服务平台验收，不将 UA 伪装当作跨平台测试。

归档指纹的 `input_platform` 记录实际值。回放先检查源字段存在且合法，启动后再测当前浏览器并比较；源平台与配置预期已冲突时可在 prepare 提前拒绝。缺少新平台证据的旧 run 保留供离线分析，但不能猜一个平台继续执行，也不回写旧归档。

### 只通过页面交互完成任务

需要禁止构造搜索页、详情页或接口 URL 的实验，在原 YAML 的 `hook` 列表中追加：

```yaml
hook:
  - hook_id: ui_navigation_guard
  # 保留原有 trace_writer（及其 recording/archive 配置）等 hook
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

这里启动的是 BrowserUseAgent + Skyvern 云浏览器。要运行 Skyvern agent + 本地 Chrome，用上表的 `skyvern_agent_local.yaml`。要组合 Skyvern agent 与 Skyvern 云浏览器，复制该文件并把 backend 改成：

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

每次运行的 trace 写在 `traces/<run_id>/`，配置里的 `hook: [{hook_id: trace_writer}]` 决定是否落盘。`manifest.json` 是已提交内容的唯一索引（`format_version: 7`），`config.json` 是配置快照，`steps/NNN.json` 是每步的记录。以下目录取决于 `trace_writer.recording` 的选择；只选 metrics/errors 时不采集完整模型内容或观测。`observations/` 存观测和截图，文件名带全限定的 fragment key，如 `000_browser.dom.json`、`000_browser.screenshot.png`。`messages/` 每次 LLM 调用一个 `NNN_call_KK.json`（parse 重试、校验失败、截断、被取消的调用各占一个）；请求了结构化输出时，schema 每个 run 只在 `messages/output_formats/` 存一份。prompt 里内联的图片（vision agent 发给模型的截图）每张每个 run 只在 `messages/images/` 存一份（`<sha256>.png` 之类，就是那张图片本身），调用记录里在图片的位置写一个引用；所以带着全部历史截图的 agent，`messages/` 也只随不同截图的张数增长。直接打开 `NNN_call_KK.json` 看到的是引用，经 `RunTrace` 读回的是完整的 prompt。绑定了归档器的录制 run 还有 `env/`；现成的评测 YAML 不录环境证据，没有这个目录。

run 开始之后，不论完成、超时、出错、被否决还是被外部中断，记录器都会尝试保存 `result.json`，它的 `agent_done` 说 agent 怎么结束（`done`、`max_steps`、`error`、`timeout`、`vetoed`）。manifest 的 `status` 只说 trace 记全没有：写着时是 `writing`，收尾之后是 `complete`，或 `incomplete` 加上 `incomplete_reason`（某一步或收尾时有东西没记下来）和 `complete_through`（完整到第几步）。超时的 run 照样可以是 `complete`。`result.json` 的 `metrics.steps` 只数策略步，即 `steps/` 里 `origin` 为 `policy` 的步。`agent_loop.terminal_observation: true` 在 run 结束后多记的一次观测（`origin: terminal`），以及分叉 run 恢复出的前缀步（`origin: restored`），都在 `steps/` 里，但不计数。模型调用次数另记在 `metrics.llm_calls`；未启用统计时调用数与用量为 `None`，provider 未报告用量时展示已知小计与缺失覆盖。记录失败不停止 Agent，后续提交可越过缺口；`missing_steps` 与 `complete_through` 说明实际缺失和连续完整范围。`spectragent bench` 会在终端先列出三本账的计数（agent 怎么结束、环境、trace 完整与否），再列每个任务的终止状态与 trace 目录名；环境坏了、模型在重试之后仍不可用、trace 不完整都标成 infrastructure failure，退出码 1；guard 否决了某个任务时 sweep 停在那里，退出码 3。

用 `RunTrace` 读 trace，参数是 `Path`：

```python
from pathlib import Path
from spectragent.core.trace import RunTrace

trace = RunTrace(Path("traces/<run_id>"))
trace.result.metrics.steps   # 策略步数
trace.llm_messages(0)        # 第 0 步的每一次 LLM 调用
trace.screenshot(0)          # 第 0 步截图的字节
```

它只按 manifest 找文件，manifest 没列出的文件不算数；列出了却读不回来的文件（缺失、为空、损坏、类型不符）抛 `TraceSchemaError`，点名那个文件。`format_version` 不是 7 的旧 trace 整个读不出来（构造 `RunTrace` 时就抛 `TraceSchemaError`，点名版本）：不做兼容。保留旧轨迹原件，历史复核使用冻结的已准备证据或原版本工具；要获得当前格式轨迹则重跑任务。当前格式明确记录范围、稀疏提交和轻量调用事实，并要求模型调用带 `kind: chat | decision`；旧版本测试数据需要重建。调用记录引用的图片缺失或与它的摘要不符，读那一步的调用时同样抛 `TraceSchemaError`，点名那张图片。

格式升级不意味着所有现存轨迹都要重采。2026 年 10 月 25 日十站轮次的 22 条源 run 已经是 v7，当前读者能打开；历史 observation、动作与回复可直接审阅。`browser.frames` 的动作前截图和调用中的独立 thinking 只有实际录到或服务商返回时才有，缺失时保留缺失；不能用之后更新的配置补造。

需要继续改入口时，按执行顺序看 [单任务入口](../spectragent/core/run_cli.py)、[基准入口](../spectragent/core/benchmark_runner.py)、[装配器](../spectragent/core/composer.py)、[运行时](../spectragent/core/runtime.py)。agent 的配置与输出协议分别在 `spectragent/agents/browser_use/`（家族根 `family.py`，成员 `dom.py`、`dom_vision.py`）、`skyvern/`（家族根 `family.py`，成员 `dom.py`、`dom_vision.py`，闭合回复 `reply.py`）、`ui_tars/agent.py`（UI-TARS 的回复读写在无状态编解码 `ui_tars/codec.py`）；browser capability 与 backend 在 `spectragent/core/capabilities/browser/`。配置字段细节见 [configs/README.md](../configs/README.md)。

## 录制、回放与训练导出

数据飞轮把 browser run 连同它的网络、存储和原始 DOM 一起录下，再离线回放、从录制的检查点分叉、导出训练数据，最后出一份度量报告。用法见 [数据飞轮运行手册](data_flywheel_runbook.md)，配置字段见 [configs/README.md](../configs/README.md)。录制用哪个 teacher、哪些浏览器设置、各类网站上表现如何，按实测结果写在运行手册的 [配置指南 101（实测推荐）](data_flywheel_runbook.md#配置指南-101实测推荐)。

当前前瞻采集的唯一采用表在上述指南（2026-10-28 逐项核对）。四份 agent 模板采用 UI-TARS、30 步、1 次解析重试、200 条 memory；teacher 是用户选定的 m17 私有路由，通过 `openai/deepseek` 发送 `max_tokens`，judge 独立使用官方 DeepSeek。先按 [lookahead/README.md](../lookahead/README.md#a-collection-round) 把地址、规则和任务上下文固定进轮次副本，不能直接运行 teacher 地址仍为 `null` 的公共模板。

录制和分叉统一 learned（同一导出、seed 0）、显示指针、`Asia/Shanghai`、`zh-CN`、1280×720 窗口及视口，恢复采用 `warn` 与 `recorded_timeline`。learned 需要 `learned-mouse` extra 和指定的 `generator.npz` / `meta.json`。每项为何采用实跑值、原 runbook 值或用户新选择，以及输出预算、时间成本、旧源兼容和未验证范围，都在采用表中说明。新组合尚未整轮实跑；健康报告先复核为 `reviewed-exclusions.json` 再供所有下游使用。

视频采集保留原始播放画面。分叉用 `media: align` 尝试对齐，`site_rules` 中的 `media_areas` 只调整声明页面的恢复比较，不修改原图；超过 75% 的像素遮罩不应用。`freeze`、共享时钟、落定等待与“主文档没加载完”的筛选各有作用，不能靠放宽截图阈值互相替代，详见数据飞轮手册“视频和动态页面”。

本轮采集与通用示例的入口分开选择：

| 用途 | 命令 |
|---|---|
| 本轮录制（四个归档器） | 先准备轮次副本，再 `uv run spectragent bench --config <轮次>/configs/record_ui_tars_deepseek.yaml --no-eval --timeout 1800`，任务及种子按轮次清单选择 |
| 通用离线重跑示例（只配套同组 `configs/flywheel/` 录制） | `uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml --start-url URL --task "录制时的任务" --replay-archive traces/<录制的 run_id>` |
| 本轮检查点分叉 | 先复核排除文件并生成变体，再按轮次 `branch_same_task.yaml`、`branch_task_variants.yaml`、`branch_task_variants_without_source_thoughts.yaml` 执行 `spectragent bench --no-eval --timeout 1500` |
| 导出训练数据 | `uv run spectragent export --config configs/flywheel/export_sft.yaml` |
| 跑整条链并出度量报告 | `uv run spectragent flywheel run --spec chain.yaml`；只重算报告用 `uv run spectragent flywheel report --spec chain.yaml` |

`--replay-archive` 只能配 `network_source: archive`，YAML 写的是 `live` 时 setup 会拒绝。`spectragent export` 只读 trace，不启动浏览器；默认 `wait_actions: keep` 不调用模型，显式选择 `drop_when_capped` 时由配置的 helper 改写受影响的 thought。`chain.yaml` 要自己写，格式见 [configs/README.md](../configs/README.md#measuring-the-flywheel)。`spectragent flywheel` 和 `spectragent bench` 一样不读 `.env`，跑之前先在 shell 里导出密钥（`set -a; . ./.env; set +a`）。

宿主机的浅色 / 深色外观既不固定也不记录。运行固定页面配色，但 headless Chrome 的原生滚动条看起来跟随操作系统外观：宿主机设成自动切换时，同一页面在不同时段的截图可能只在滚动条那一条上不同，改变恢复校验的截图距离。正式运行请固定宿主机外观，恢复校验的结果只在同一外观下的运行之间比较。


## 使用登录态

预置登录态在任务的 `setup` 中设置 `storage_state`：可用 `BrowserStorageState` 文档或其 JSON 路径。Python 调用者传 `BrowserSetup(storage_state="/path/to/state.json", start_url="https://example.com").to_context()` 给 `AgentRuntime.execute(env_context=...)`。路径在 Runtime 入口读取为文档，setup 冻结 canonical bytes；首请求与首个业务脚本前写入 cookie、按真实 storageKey 的 localStorage，以及按 logical tab + storageKey 的 sessionStorage。seed 只接受 owned fresh context，持久 profile / 外部 CDP 不能与 seed 混用。保留的 tabs 初始停在 about:blank，只有 `start_url` 自动打开业务页面；快照的 tab URL 只是来源信息。

要让 Agent 自己在网页输入账号密码，把登录步骤与凭据放进现有 `task_text`，使用既有 prompt 和 type / click 动作。`login_required` / `login_type` 仅保留数据集信息；`credentials_ref` 不受支持并明确报错。框架没有自动登录、credential store 或额外加密机制。

要保存可恢复初态，在 `trace_writer.archive` 选择 `browser.storage`，与网络 / DOM 证据一同录制。初态写成 immutable `initial_state.json`，动作后状态另写 `storage.json`；回放只认前者。task setup 记录不复制 seed / 原路径，源文件后来删改不影响回放与分叉。未录 storage 的非空 seed 可以正常执行，但之后不能假作空初态回放。seed 覆盖 cookies、local / session storage（含 CHIPS、第三方分区及 OOPIF），不包含 IndexedDB、SW、cache 或完整 profile；持久 profile 的未知 origin 覆盖不完整，只用于 `none`，不能据此离线恢复。
