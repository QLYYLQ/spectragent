# 数据飞轮运行手册

从仓库根目录执行下列命令。数据飞轮把一次真实的浏览器 run 连同它当时的网络环境录下来，之后不访问线上站点也能复用它：离线回放、从中间某一步分叉、导出成学生模型的 SFT 数据，最后用一份报告度量这条数据线的质量。每一段都是现有入口加 `configs/flywheel/` 下的一份 YAML，产物都是 trace 目录或数据集目录。各层为什么这样设计，见 [architecture_design.md](architecture_design.md) §4.14（录制与离线回放）、§4.15（训练导出）、§4.16（度量）；YAML 字段的逐项说明见 [configs/README.md](../configs/README.md#recording-environment-evidence)。

下表是通用飞轮机制示例，使用同组配置配套演练；下一轮前瞻采集的实际选择见下节。

| 段 | 入口 | 通用示例配置 | 产物 |
|---|---|---|---|
| 录制 | `spectragent bench` 或 `spectragent run` | `record_ui_tars_pinned.yaml` | run 目录下的 `env/` |
| 离线回放 | `spectragent run --replay-archive` | `replay_ui_tars_archive.yaml` | 一个新 run，`env/` 里是回放日志 |
| 分叉 | `spectragent bench`（任务来自 `trace_branches`） | `branch_ui_tars_archive.yaml`、`same_path_ui_tars_archive.yaml` | 每个分叉一个 run |
| 导出 | `spectragent export` | `export_sft.yaml`、`ui_tars_student.yaml` | 数据集目录 |
| 度量 | `spectragent flywheel run`、`spectragent flywheel report` | 自己写的链规格 | `report.json`、`report.md` |

## 配置指南 101（实测推荐）

**下一轮采集统一使用 `configs/lookahead/` 的五份模板；本节是逐项采用决定（2026-10-28）。** `configs/flywheel/` 保留通用机制示例，不能把其中的 GPT-4o、instant、strict、UTC 默认值混进本轮。模板中的 teacher 私有地址和轮次路径须先按 [lookahead/README.md](../lookahead/README.md#a-collection-round) 写入轮次副本，再做配置检查。teacher 和 judge 是两个独立的模型角色，分别配置路由与密钥。

取舍按下面的证据作出。用户明确的采集选择优先；其余字段逐项比较运行事实、故障和成本，不把“曾跑过”直接等同于“更好”。新增组合尚未完成真实站点 rollout。

| 依据 | 用来决定什么 | 边界 |
|---|---|---|
| 10 月 27 日用户选择 | learned 鼠标、显示指针、`Asia/Shanghai`、`zh-CN` | 下一轮要求；不伪造旧轨迹设置，也没有宣称提高成功率 |
| 10 月 25 日十站 trace、指纹、调用与冻结 YAML | 30 步、200 条 memory、窗口几何、归档与恢复参数的实际值；重试与 pace 的成对比较 | 22 条源 run；分叉统计实际来自健康筛选留下的 18 条源，不把十站录制覆盖当作所有后续统计的覆盖 |
| 10 月 21–24 日轮次总结 | freeze/shared、站点规则、媒体对齐与遮罩、像素坐标等已有经验 | 早期原始轨迹已删除；保留为有日期的文本证据，不能重新验算每条历史结论 |
| 10 月 26 日路由要求、请求探针与后续 provider 实现 | 下一轮 teacher 的 m17 路由、`max_tokens` 参数通道；独立 judge 的官方 DeepSeek 配置 | 新路由完成聊天、图像、JSON object 和 thinking 探针，尚无该组合的整轮任务效果证据；60 条稳定性回归仍用旧 1017 路由 |
| 当前设计、源码及离线装配 | 字段归属、默认值、请求参数与录制/分叉兼容条件 | 配置检查通过证明能装配，不证明真实网站表现 |

原始证据在本地 `tmp/lookahead_10site_ui_tars_20261025/` 的 `RUN.md`、`STRATEGY_DIFF.md`、trace、`out/health/`、`out/judge-forks/`；10 月 26 日探针与旧配置在 `out/stability_improvement_20261026/`（`input/configs/`、`new_route_probe/`）。旧配置副本与 trace 用来辨认当时实际跑了什么，不能用后改的同名 YAML 替代。10 月 28 日逐项重算的中间结果在本地 `tmp/config-reconciliation/`。本节已保存结论与分母，以上本地目录不随 Git 发布。

### 一轮采集的顺序

1. 复制到新的轮次目录，填入 teacher 地址，冻结本轮规则副本和 learned 导出身份；固定 Chrome 版本、宿主外观及系统语言。检查 agent、memory、浏览器参数在录制和三个分叉臂中一致。
2. 先小样本录制和分叉检查模型输出坐标、请求预算、恢复与记录健康，再启动同一份冻结配置；正式录制最多 3 个站点并行，站内串行。
3. 运行 `lookahead health`，保留机器生成的 `exclusions.json`；查看失败端点、主文档、截图和持续空白证据，另存 `reviewed-exclusions.json` 及复核理由。后续任务只读复核版。
4. 用独立 judge 生成任务变体。同任务、换任务、换任务去源回复三组都先 `--list`；以该列表划分互不重复的任务，记录每个进程的会话目录。
5. 执行分叉，检查 recording health。统计恢复必须注明 checkpoint、完整前缀、任务成功或可评判性中的哪一种，不能互换分母。
6. `extract`、`stability --media-areas <本轮规则目录>`、`judge-forks`、`gold-queue` 都使用同一份复核排除清单，再开 viewer 标注。
7. 新规则的效果以同一源、同一任务列表、只改规则的对照判断。保留旧产物与规则快照；新增或撤销排除项后另算结果，不改写原轮次分母。

### 录制前准备与操作取舍

| 项目 | 采用值／做法 | 采用谁，为什么 |
|---|---|---|
| 配置和路径 | 每轮独立目录；四份 agent 文件的 `id: main`、环境 `browser_env` 是装配名称；各 `trace_dir`、`data_dir`、`task_variants` 指向该轮目录 | 沿用现有命名，隔离输出；路径不是模型策略。保留执行时 YAML、源码提交、规则摘要和 learned 文件摘要 |
| teacher 地址与密钥 | 将本地 `OPENAI_BASE_URL` 写入四份轮次 YAML 的 `llm.base_url`，密钥仍由环境读取；不将私有地址或凭据写入仓库 | 采用 10/26 用户的新路由要求，并使实际地址进入该轮配置身份；`base_url: null` 是未填写模板，不能直接开跑 |
| learned 导出 | `~/code/mouse-gan-torch/runs/v2_mdn_h128_d3/export`，`generator.npz` 与 `meta.json`；`uv sync --extra dev --extra learned-mouse` | 用户指定 learned，沿用 10/25 实跑导出与 seed 0；换机器统一改四份文件。模型和时序以 meta 为准，整轮固定同一份文件内容 |
| 数据集与身份 | `lexbench_browser / All`，开跑前核对本机任务 ID | 采用 10/25 实际数据集；旧五站轮次的 L1 在当前本地数据中不存在，不凭旧拆分名运行 |
| 登录态 | 按任务需要选择新 context 的 `storage_state` 种子；任务清单明确“有种子/无种子”，用页面核实种子有效 | 10/25 八站用种子、同花顺和 Steam 从新状态开始；公开任务不等于未登录。沿用需要账号的任务种子，不一律清空或复用长期 profile |
| 页面外观与浏览器 | 同轮固定 Chrome 版本、系统语言与宿主 Light/Dark；记录实际指纹，不照抄文档中的版本号 | 10/25 指纹是 Chrome 143、宿主 Light；较早 Chrome 155 实验另属一批。原生滚动条受宿主外观影响，切换曾破坏截图比较 |
| live 网络与并发 | 快网络；最多 3 个站点并行，每站任务串行 | 采用多轮操作经验：5 站并行曾使下载饥饿；离线即时应答无法复现录下的“还在加载”状态 |
| 每任务超时 | 录制 `1800 s`；分叉 `1500 s`；模型请求 `300 s` 单独限制 | 沿用已用过的 runbook 预算；恢复后段检查点曾超出 `900 s`，不能把模型请求上限当整个 task 上限 |
| 健康排除 | 机器报告原样保存，所有下游统一 `out/health/reviewed-exclusions.json` | 采用这次证据复核：10/25 腾讯 3 条只有遥测请求失败、首末画面已渲染，应撤销其 recording-health 整 run 排除；爱奇艺任务 18 有连续 5 个空白策略步，保留。原报告的 4 条排除和原 932 分叉结果继续作为历史事实 |
| 复核不是成功判分 | 验证主文档/内容和截图；验证码、站外跳转和账号状态人工检查 | 当前 health 把 Document/Fetch/XHR 一并统计，会误伤遥测，也识别不了所有风控页。恢复通过、health 保留、Agent done 均不能替代任务正确性 |
| 旧 trace 的复用 | 审阅 v7 不重采；继续分叉时从该源的实际配置建立匹配副本，保留 cursor、时区、窗口、memory、提示等身份 | 新模板用于下一轮。仅换配置不能补出旧帧/thinking；改提示、memory 或浏览器指纹后，不能把新模板直接套到旧录制上要求 source 精确历史 |

### Teacher、judge 与决策预算：逐项采用结论

本表的“实跑”指 2026-10-25 十站轮次的已提交 trace 与实际调用；该目录在 10 月 26 日改过 YAML，不能用现在的 YAML 反推旧运行身份。新采集采用用户后来要求的 `m17` 路由；judge 则采用后来单独确定的 DeepSeek 官方接口。两者不是同一次迁移。

| 键（四份 teacher 录制/分叉配置） | 候选差异 | 最终值 | 采用谁、为什么 | 证据范围 |
|---|---|---|---|---|
| `agent_id` / `prompt_template` | 已停的 BrowserUse DOM；实跑和手册 UI-TARS | `ui_tars` / `computer_use` | 采用用户 10/25 选定且已采集的纯视觉策略 | 22 条源、932 条分叉；旧 BrowserUse 轨迹保留作检查，不混入本批 〔策略对照〕〔十站调用复算〕 |
| `coordinate_space` | 早期 `normalized_1000`；实跑和手册 `screenshot_pixels` | `screenshot_pixels` | 保持已录的图像与动作坐标合同 | 1017 早期样本改像素提示后未再观察到坐标系混写；m17 仅有图像理解探针，尚未完成坐标行为整轮验证 〔早期轮次〕〔新路由探针〕 |
| `show_capture_state` | 默认开启，实跑解析为 `true` | `true` | 保持录制/分叉观测协商一致，模型能看到主文档未加载的事实 | 954 份配置解析一致；没有开关对照证明成功率提升 〔十站调用复算〕 |
| `show_action_effects` | 默认开启，实跑解析为 `true` | `true` | 保留实际动作效果和失败反馈；录制、分叉提示一致 | 954 份解析配置；不是模型成功率对照 〔十站调用复算〕 |
| `llm.model_type` | 实跑与当前均 OpenAI 兼容协议 | `openai` | 按线协议选型，不把网关名称当模型协议 | 新旧路由均有此协议证据 〔十站调用复算〕〔新路由探针〕 |
| `llm.model` | 历史 `deepseek-v4.1-flash-1017`；10/26 用户指定 `deepseek-v4.1-flash-m17` | `deepseek-v4.1-flash-m17` | 采用后来的明确用户选择；不能用旧轮次规模较大覆盖新选择 | 旧 1,562 次策略调用请求的是 1017；m17 只有聊天、图像、JSON object、thinking 探针，响应自报 `deepseek-flash`，不证明与旧别名权重相同 〔十站调用复算〕〔新路由探针〕 |
| `llm.provider` | 历史与10/26手写配置 `direct`；现有 `deepseek` 可覆盖端点并正确映射预算 | `{name: deepseek}` | 采用现有 DeepSeek provider，向指定网关发已被探针验证的 `max_tokens`；无需新适配层 | 已离线验证 m17+自定义地址可构造，参数为 `max_tokens:32768`、`reasoning_effort:high`；此 provider+网关组合尚未整轮实跑 〔新路由探针〕〔请求实现〕 |
| `llm.base_url` | 手册固定 DMX；新路由是本地私有端点；隐式环境读取不利于固定轮次身份 | 公共模板 `null`；新轮次副本写入已提供的 `OPENAI_BASE_URL` | 运行前复制四份模板到 ignored 轮次目录，缺环境地址即停止准备，再写成显式 URL；不直接执行未解析模板，不把私有地址提交 Git | `direct` 省略地址时 SDK 会读环境、缺省后落到 OpenAI；`deepseek` 缺省则是官方地址，故新 teacher 必须完成轮次解析。已离线检查现有代码，不读密钥、不发请求 〔请求实现〕 |
| `llm.api_key` | teacher 与官方 judge 属于不同服务 | YAML 省略；各命令用对应 `OPENAI_API_KEY` | 密钥只在运行环境，不写模板或轮次 YAML；不能把 teacher 的密钥直接沿用于官方 judge | 现有 SDK/接口约定；密钥不写入产物 〔请求实现〕 |
| `llm.reasoning_effort` | 实跑 `high`；m17 探过 none/low/high/max | `high` | 保留用户采用的推理模式 | m17 的 high 有原生 reasoning 内容；15 个探针不能证明档位强度单调，也未证明 high 对浏览任务最优 〔新路由探针〕 |
| `llm.temperature` | 实跑录制 `0.2`、分叉 `0.8`；手册 `null` | `null` | 采用手册的明确无采样覆盖；DeepSeek 思考模式下不发无效温度，不能把旧温度差解释成已控制的分叉多样性 | 官方接口实测思考模式下无效；m17 网关没有温度因果对照 〔十站调用复算〕〔官方接口探针〕 |
| `llm.frequency_penalty` | 实跑继承默认 `0.3`；手册 `null` | `null` | 采用手册；DeepSeek provider 不发送该无效参数 | 官方接口实测；历史值仍应如实记录为 0.3 〔十站调用复算〕〔官方接口探针〕 |
| `llm.seed` / `top_p` / `service_tier` | 实跑未指定，解析为 `null`；手册也不指定 | 均不指定（`null`） | 保留既有身份，不添无证据的采样或服务层选择 | 解析配置事实；不据此宣称确定性 〔十站调用复算〕 |
| `llm.max_completion_tokens` | 实跑 `8192`；手册 `32768`，另有已无法重验的“16384 曾截断”文字 | `32768`，经 provider 发送为 `max_tokens` | 采用较大保守预算；8192 已实际截断，不能机械退回历史值 | 一条换任务分叉在 8192 截断，其他两臂峰值 7852/7978；32768 不是实测最优值。m17 探针只证明 `max_tokens:2048` 能截断及 4096 下简单题返回，未验证 32768 整轮表现 〔十站调用复算〕〔新路由探针〕 |
| `llm.timeout` | 手册 `300`；实跑解析为 `null`，使用当时 SDK 默认 | `300` 秒 | 采用显式可解释的当前调用上限；不要把旧实跑叙述成配置了 300 秒 | 源调用最大 40.641 秒；300 是有余量的运行策略，新路由长尾未测 〔十站调用复算〕 |
| `llm.max_retries` | 手册 `2`；实跑解析为 `5` | `2` | 采用当前有界传输重试策略；它与模型重写解析错误不同 | 未单独记录 SDK 内部传输重试，无法从策略调用次数推出 5 次的收益或证明未发生重试；2 是预算选择，不是实验最优结论 〔十站调用复算〕〔请求实现〕 |
| `agent_loop.loop_id` | 两边一致 | 录制 `react`；分叉 `branch` | 保留工作流职责 | 954 份运行配置 〔十站调用复算〕 |
| `agent_loop.max_steps` | 手册 `32`；实际 `30` | `30` | 采用真实轮次的步数与提示预算身份；32 不只是额外两步，UI-TARS 每步提示包含 max_steps | 22 源中 20 个发出 done、2 个用尽步数；其中4个在第30步发出 done。没有30/32对照；done不等于任务经验证成功 〔十站调用复算〕〔提示与历史实现〕 |
| `agent_loop.max_actions_per_step` | 两边一致 | `4` | 保持录制/分叉动作合同一致；学生若更少会影响可导出样本 | 954 份配置，没有动作批量最优性实验 〔十站调用复算〕 |
| `agent_loop.parse_retries_per_step` | 手册 `1`；实际 `2` | `1` | 采用手册值，但依据更新为本轮实际边际结果：第二次调用有效，第三次没有新增成功 | 源14步、分叉47步在第二次调用成功；唯一三次调用最终仍解析失败。是本批观测，不保证新模型分布相同 〔十站调用复算〕 |
| `agent_loop.max_consecutive_parse_errors` | 两边默认一致 | `3` | 保持连续失败步的停止上限；不要与同一步重试数混为一谈 | 954 份解析配置，未做阈值对照 〔十站调用复算〕 |
| `agent_loop.terminal_observation` | 两边一致，分叉可继承默认 | `true` | 最后一个动作需要结果画面 | 22 源都有终止观测；一条模型截断的分叉没有终止观测，开启不保证异常路径一定有图 〔十站调用复算〕 |
| `memory.memory_id` / `keep_system` | 两边一致 | `simple` / `true` | 保持完整消息存储策略与 system 保护 | 954 份解析配置 〔十站调用复算〕 |
| `memory.max_messages` | 手册 `80`；实际 `200` | `200` | 采用实跑容量；每次解析重试还会加入 assistant 与反馈消息，30步带重试可能超过80，200留住这部分历史。容量上限本身不会预先增加实际token | 本批所有实际请求最多65条，所以80在本批未造成截断；这不是追溯修复，也不是200提高任务成功率的对照结论 〔十站调用复算〕〔提示与历史实现〕 |

分叉的 `continue_steps`、`history_source`、恢复与选步配置由分叉表另列；它们应与上面的提示预算和 memory 共同核对，不把新配置当成与旧 trace 逐字节相同的 source-history 续跑。

| 键（`judge_deepseek.yaml`） | 候选差异 | 最终值 | 采用谁、为什么 | 证据范围 |
|---|---|---|---|---|
| `model_type` / `provider` / `model` | 历史 judge 为网关1017；后来单独选择官方DeepSeek | `openai` / `{name: deepseek}` / `deepseek-flash` | 采用后来的 judge 明确选择，独立于 teacher 的 m17 网关 | 旧轮次746次judge全有效、当日稳定性回归60次也为旧1017；官方只做过小样本真实schema探测，不是整轮成绩 〔新路由探针〕〔官方接口探针〕〔十站 judge〕 |
| `base_url` | 先前模板依 provider 官方默认；teacher另有环境地址 | 显式 `https://api.deepseek.com/v1` | 固定官方 judge 身份，不让 teacher 环境地址成为歧义 | 现有 provider 默认本就为此官方地址；显式写出便于审阅 〔请求实现〕〔官方接口探针〕 |
| `api_key` | 不同服务密钥不能混用 | YAML省略；judge命令注入官方的 `OPENAI_API_KEY` | 保持凭据与 teacher 分离 | 不写入Git、轮次副本或报告；密钥不进入产物 |
| `reasoning_effort` | 两边 high | `high` | 保留当前模式 | 官方真实闭合动作schema5/5有效；没有推理档位质量比较 〔官方接口探针〕 |
| `temperature` / `frequency_penalty` | 旧helper未显式采样；旧模板继承通用默认再由provider省略 | `null` / `null` | 显式表达不发送，无行为变化 | 官方DeepSeek参数实测；避免与teacher旧0.2/0.8实验配置混淆 〔官方接口探针〕 |
| `max_completion_tokens` | 历史8192；当前16384 | `16384`，发送 `max_tokens` | 采用当前judge独立预算，不因teacher截断而同步放大 | 十站746次最大2816且全有效；更早299次摘要最大4806。官方整轮输出尾部尚未测 〔十站 judge〕〔早期 judge〕 |
| `timeout` | 当前300；旧轮次调用最大28.486秒 | `300` 秒 | 保持明确上限和余量 | 历史路由延迟，不能当官方新路由SLA 〔十站 judge〕 |
| `max_retries` | 当前SDK传输重试5；旧helper尝试记法不同 | `5` | 保留离线批量judge的传输容错策略；与teacher的2分开 | 未证明第3至5次重试带来收益，属于运行预算选择 〔请求实现〕 |
| 结构化重问 | 旧报告称新路由仍强制json_schema而拒绝；现代码已改 | provider按模型家族把schema写进提示并校验；judge收到不合schema回复最多重问2次 | 采用10/26后的架构裁决，删除把旧限制当当前状态的措辞；截断或服务端中止不会反复重问 | `DeepSeekFamily.supports_json_schema=False`已在代码，官方json_object+真实闭合schema5/5。m17当前自动judge没有整轮实跑，本次也未调用 〔请求实现〕〔官方接口探针〕 |

证据索引：

- **策略对照**：`tmp/lookahead_10site_ui_tars_20261025/STRATEGY_DIFF.md`，UI-TARS策略及停止BrowserUse的身份区分。
- **十站调用复算**：`tmp/config-reconciliation/model-observed-stats.json`（本次经 `RunTrace` 对954条运行、1,499个策略步/1,562次调用的只读复算）；`model-source-stats.json`（源调用长度/延迟补充）；原始解析配置例 `tmp/lookahead_10site_ui_tars_20261025/record/128_3deaf2bd/config.json`，关键路径 `resolved.agent[0]`，模型身份另核对顶层 `llm_model` 和调用记录。
- **早期轮次**：`.agents/skills/af-project-history/references/lookahead-rounds.md:24-26`；旧轮次原数据已删除，属于日期化摘要。
- **新路由探针**：`tmp/lookahead_10site_ui_tars_20261025/out/stability_improvement_20261026/report.md:58-73` 及 `new_route_probe/effort.json`、`formats_and_vision.json`、`user_schema_control.json`。冻结旧YAML在 `input/configs/`；后改的 `configs/*.yaml` 不能证明旧运行已用m17。
- **请求实现**：`spectragent/core/llm/providers/openai.py:32-43`、`openai_deepseek.py:77-110`；`lookahead/judge.py:1-21`。SDK读取环境的路径已本地检查，未访问端点。
- **官方接口探针**：`docs/architecture_design.md:2140`；`.agents/skills/af-add-an-llm-provider/references/guide.md:381-403`，官方API的有日期实测与当前实现。
- **提示与历史实现**：`spectragent/agents/ui_tars/agent.py:374`（max_steps进提示）；`spectragent/agents/memory/simple.py:19-57`；`spectragent/core/loops/react.py:855-856`（重试向memory追加两条）。
- **十站 judge**：`tmp/lookahead_10site_ui_tars_20261025/out/judge-forks/calls.jsonl`，本次只抽取参数、响应模型名、有效性、usage和latency统计；746次请求1017、响应自报 `DeepSeek-V4.1-Flash-261017`，全有效，最大2816 completion tokens、28,486 ms。
- **早期 judge**：`configs/lookahead/judge_deepseek.yaml:21-27` 的较早299次调用摘要。其16384预算说明有余量，不是新官方路由整轮验证。

采用新teacher与以上预算后，必须把“配置已解析/装配通过”“小探针通过”“整轮成功率验证”分开陈述。较大预算、历史容量和重试次数都不应宣传为已证明的最优值。

下表把三个来源分开：`configs/flywheel/` 是通用示例，10 月 25 日十站轮次是已有实跑证据，本轮配置是根据证据和用户要求选定的下一轮设置。不能把下一轮的整套组合写成已经验证完成。比较基线为 `cdfed03`；表中的“原模板”指这次统一之前的 `configs/lookahead/`。

这次直接读取了 22 个源 run，以及 `branch_actions_none`、`branch_actions_recorded_timeline` 各 202 个分叉的已提交配置和浏览器指纹。其他任务变体与消融 arm 没有在本次浏览器审计中逐个重扫。旧配置中被脱敏为 `<omitted>` 的字段，使用 10 月 26 日保存的输入 YAML 或已提交环境指纹交叉核对，没有据此猜测实际值。

**最终选择：learned 鼠标、光标开启、`Asia/Shanghai`、`zh-CN` 按用户要求采用；窗口高度改回已有真实轮次使用的 720；其余关键默认值沿用实际记录并显式写出。** 原有回放规则集继续使用，不新增视频、广告或整站资源阻断。

### 浏览器、窗口与 DOM

| 配置或条件 | 候选差异 | 最终采用 | 采用谁、理由与验证范围 |
|---|---|---|---|
| `backend.provider_id` | 通用示例、原模板与实跑均为 `local_chrome` | `local_chrome` | 沿用采集链；本地 Chrome 具备归档与严格离线隔离能力。其他云后端的运行示例不构成此链的验证。 |
| `backend.headless` | 均为 `true` | `true` | 多轮实跑。录制和所有分叉保持一致；改成有头会改变启动参数指纹，不能直接用于旧源回放。 |
| `backend.lifetime` | browser runbook 的共享浏览器示例用 `experiment`；这条采集链实际是 `run` | `run` | 按采集链实跑采用。共享 sweep 是另一种实验配置；不能从另一份运行示例复制过来。 |
| `backend.window_width` | 均为 1280 | `1280` | 22 个源及本次读取的 404 个分叉一致。 |
| `backend.window_height` | 原 lookahead 模板为 860；十站实跑及框架默认均为 720 | **`720`** | 采用真实轮次。没有找到 860 的对应采集证据或用户要求。窗口大小参与启动参数兼容检查；四个新轮次配置一致。 |
| `viewport_width` / `viewport_height` | 均为 1280×720 | `1280` / `720` | 多轮实跑。窗口和视口是两个独立字段，即使本轮取相同数字也分别写出。 |
| `backend.executable_path`、浏览器版本 | 十站冻结 YAML 写了本机 Chrome 路径；模板自动发现 | 路径保持可在采集机器自动发现，开跑记录实际 Chrome 版本，同轮保持一致 | 22 个源指纹全部为 **`Chrome/143.0.7499.170`**。文档里冻后台、节流等历史测试提到的 Chrome 155 是另一次测试，不能当成十站轮次版本。当前兼容检查对 Chrome 版本变化只警告，仍应控制实验变量。 |
| `profile` | 均为 `standard` | `standard` | 冻结 YAML 与代码支持。它决定 watchdog 和采集能力；`security_allowed_domains`、`security_blocked_domains` 都为空，**没有视频或广告域名阻断**。 |
| `dom_cleaner.cleaner_type` / `dom_serializer.serializer_type` | 均为 `browser_use` / `text` | 沿用 | UI-TARS 的纯视觉观测不会因为配置了这两个 receptor 就收到 DOM 文本；`browser.dom_raw` 归档也不是模型输入。 |
| DOM 细项 | 实际 cleaner 使用 `descendants_occlude: false`、`enable_compound_components: true`、`include_shadow_dom: true`、`max_occlusion_nodes: 5000`；serializer `max_text_length: 200` | 保留默认，不新增调参 | 没有这些参数的同源对照，纯视觉策略不靠这组 DOM 文本参数做决定。记录有效配置即可，不必把无效轴堆进采集模板。 |
| 宿主外观、系统语言 | 十站实跑为 Light；旧实验曾因日夜模式导致滚动条不同 | 同轮固定一种宿主外观和系统语言，记录实际值；不要求改成 Dark | 已出现真实恢复差异。页面 color-scheme override 不能保证原生滚动条不受宿主外观影响。Light 是已有轮次事实，不是所有站点的最佳主题结论。 |

### 输入与模型画面

| 配置 | 候选差异 | 最终采用 | 采用谁、理由与验证范围 |
|---|---|---|---|
| `mouse_trajectory.trajectory_type` | 通用示例是 `instant`；十站实跑及原模板是 `learned` | **`learned`** | 用户明确选择；也有十站实跑。829 条人类轨迹的 9 项特征比较中，learned 有 7 项进入范围，拟合 WindMouse 3 项、默认 WindMouse 2 项、instant 1 项。这支持运动形态相似，不能写成已提升任务成功率或回放恢复率。 |
| `weights_dir` | 模板使用 `~/code/mouse-gan-torch/runs/v2_mdn_h128_d3/export`，旧轮次使用同一导出的绝对路径 | 同一 v2 导出 | 四份配置使用同一 `generator.npz` / `meta.json`。选定模型是带 stop head 的 MDN-RNN；目录名字不代表仍在用早期 GAN。训练采样率、归一化以 meta 为准，不擅自覆盖。 |
| `mouse_trajectory.seed` | 实跑和原模板均为 0 | `0` | 沿用已有采集。它不同于 pinned 的 V8 随机种子；后者不允许 0。 |
| learned 形状与点击时长细项 | 实跑使用默认 `num_candidates: 8`、`button_size: training`、`jitter: 0`、点击 hold median/min/max 为 92/35/400 ms，sigma 为 0.32 等 | 不调参数，保留导出元数据和现有默认 | 没有逐项采集 A/B。避免为了“更像人”任意改变训练坐标、速率或时长解释。 |
| `typing_rhythm.rhythm_type` | 全部为 `instant` | `instant` | 沿用多轮实际配置。用户要求的是 learned 鼠标；没有同源证据要求改成人类节奏打字。 |
| `screenshot_format` | 均为 `png` | `png` | 多轮实跑；JPEG 没用于这些采集轮次。 |
| `screenshot_quality` | 默认 80 | 保留默认，不作为本轮有效旋钮 | 仅影响 JPEG，PNG 下不生效。 |
| `screenshot_cursor` | 十站实跑与通用示例为 `false`；原 lookahead 模板为 `true` | **`true`** | 按用户要求。必须明确这是相对十站旧轮次的变化，不写成旧轮次已开启。它只给模型图片加光标，原始干净截图证据保留。没有本轮开/关光标帮助效果的 A/B。 |
| `screenshot_cursor_size` | 默认 32；旧轮次虽记录 32，但光标关闭 | `32` | 固定已有实现的默认大小。不能把“关闭时记录了 32”说成“32 像素光标已在该轮验证有效”。 |

输入仿真人类不是反爬保证：已有 reCAPTCHA v3 各组都为 0.9，缺乏区分度；真实 v2 测试各组都进入图片验证，包含最像人的那组；一次已登录小红书成功只能算小样本冒烟。

### pinned、后台标签页与启动参数

| 配置 | 候选差异 | 最终采用 | 采用谁、理由与验证范围 |
|---|---|---|---|
| `replayability.replayability_id` | 采集配置均为 `pinned`；反爬/评测通常使用 `none` | `pinned` | 本轮目标是可回放采集。pinned 会改变环境；不能把这个实验直接当自然浏览器反爬评测。 |
| `replayability.clock` | 更早 12 站链是 `per_document`；十站实跑与原模板为 `shared` | `shared` | 各文档独立时钟曾使爱奇艺跨标签页时间戳同步失控。shared 已用于后续多轮，但会让每次回放的请求时间戳变化，必须配现有站点规则。更早新闻站的 per_document 成绩不算 shared 重测。 |
| `replayability.random_seed` | 均为 7 | `7` | 沿用已测配置；source 与所有 fork 相同。 |
| `replayability.base_time` | 录制为 `run_start`，回放/分叉为 `from_archive` | 保持这个分工 | 已有链验证；HTTP 日期平移是独立设置。 |
| `replayability.timezone` | 十站指纹确认 **`UTC`**；原模板 `Asia/Shanghai` | **`Asia/Shanghai`** | 用户明确要求优先。这是下一轮变化；回放旧 UTC 源数据仍要遵守源指纹兼容，不能套新模板。 |
| `replayability.locale` | 十站指纹确认 override **`en-US`**；原模板 `zh-CN` | **`zh-CN`** | 用户明确要求优先。十站的 `browser_defaults.locale` 同时是 `zh-CN`，它是宿主默认语言，不等于 pinned override。两层值都应报告，不能仅说“没有写语言”。 |
| `device_scale_factor` | 默认与实跑为 1.0 | `1.0` | 保持截图和坐标几何一致。 |
| `reduced_motion` | 默认与实跑为 `true` | `true` | 固定已测环境偏好。只是 `prefers-reduced-motion: reduce`，网站是否尊重由网站决定，不会统一暂停视频。没有开/关对照。 |
| pinned `user_agent` | 十站实际为 `null`；较早 12 站链曾设置普通 Chrome 标识 | `null` | 采用十站真实配置，保留 Chrome 自己的 UA/client hints。携程早期正常、后来被拦的比较有多项混杂，不能据此认定改 UA 能解决。 |
| `backend.user_agent` | 默认及实跑为 `null` | `null` | 保持与 pinned 一致，避免 provider 和 pinned 身份冲突。 |
| `background_tabs` | 框架默认 `run`；发布采集模板和十站实际 `freeze` | `freeze` | 多轮实跑；曾阻止爱奇艺多标签页 storage 事件反馈循环。仅冻结此前有焦点、后来被离开的页，回到该页前恢复；从未被跟随的弹窗不会因此被冻结。返回后视频可能仍暂停，这是已知行为变化。 |
| `backend.background_throttling` | 默认及实际为 `false`；尝试过 `true` | `false` | 保持已测启动参数。节流能减慢计时器，但没阻止 storage 事件互相触发；不能替代 `freeze`。历史局部 Chrome 155 测试约 330 次写入/秒，freeze 后为 0，节流仍约 330；版本范围与十站 Chrome 143 分开说明。 |
| `new_tabs` | 默认、原模板和实跑均为 `follow` | `follow` | 采用已有行为。跟随用户动作触发的候选新页，不跟随任意自动弹窗。10 秒上限等待的是候选页首次导航提交，后面的完整 load/落定由 observation settle 处理，不能统称“等页面加载完 10 秒”。 |
| `backend.mute_audio` | 默认及实际为 `true` | `true` | 保留静音输出；它不会阻断音视频资源、暂停播放或删除媒体元素。 |
| `backend.disable_automation_controlled` | 默认及实际为 `true` | `true` | 固定已有启动行为；不把它当已证实的反爬优化。 |
| `backend.ignore_default_args` | 默认及实际为 `false` | `false` | 保留启动基线；去掉默认参数会影响弹窗等行为，需要新证据。默认参数禁用的是部分浏览器后台服务，不是统一屏蔽网站视频。 |
| 其他启动默认 | `deterministic_rendering: false`、`force_device_scale_factor: null`、`extra_args: null`、`disable_security: false`、`no_sandbox: false`、`disable_gpu: false`、`disable_dev_shm_usage: false` | 全部沿用；不添加新覆盖 | 本次读到的 426 个 run 配置一致；没有逐项 A/B。随机种子和 DPR 由 pinned 负责，不再叠加另一套设置。 |

macOS 上 `--lang`/`LANG` 不一定改变所有 frame 看到的浏览器默认语言。旧轮次实际为 pinned `UTC/en-US`，浏览器默认 `UTC/zh-CN`；下一轮选择 pinned `Asia/Shanghai/zh-CN`，仍要记录浏览器默认层，保持源与回放的宿主系统语言相同。

### 落定、对应与媒体恢复

表中的 `capture_state.settle_state.loaded` 是公共 reader 属性；浏览器原始 fragment JSON 存的是 `settle.main_document_loaded`。两者表达同一个已测加载事实，字段名不混写。

| 配置 | 候选差异 | 最终采用 | 采用谁、理由与验证范围 |
|---|---|---|---|
| `settle_quiet_ms` | 默认、十站实际 500 | `500` | 沿用多轮。40 站测过额外等待开销，但没有比较不同阈值。等待 load、视口有内容与静默窗口，不保证所有在途请求完成。 |
| `settle_max_wait_s` | 默认、实际 5 | `5.0` | 沿用。到上限仍按当前画面采集；DOM 一直变化导致 `capped` 不等于页面未加载，不能自动排除全部 capped。 |
| `capture_state_timeout_s` | 默认、实际 1 | `1.0` | 保持现有预算。超时事实是 unknown，不能当没有失败图片或画面稳定。 |
| `media_state_timeout_s` | 默认、实际 1 | `1.0` | 截图前后各读一次；加 capture_state 共 3 秒，小于当前 4 秒探针预算，没有调参证据。 |
| `retarget_tolerance_px` | 默认、实际 2 | `2.0` | 沿用元素/媒体位置对应容差，没有调参。提高它可能把不同对象误对应。 |
| `restorer.screenshot_max_distance` | 均为 5 | `5` | 不为提高通过率而放宽。dHash 对局部视频变化可能不敏感，必须同时保留媒体事实与真实图片。 |
| `restorer.media` | `align` 或对照 `none` | `align` | 实跑采用；fixture 和一个真实 31 秒菜谱视频有对照，两次重复得到相同结果。修复 ended 状态对齐后 27 个视频步骤过 26 个，none 过 10 个；这不是所有视频站的成功保证。 |
| `media_time_tolerance_s` | 默认、实际 1 | `1.0` | 固定已有媒体判定；没有阈值扫描。 |
| `media_seek_timeout_s` | 默认、实际 2 | `2.0` | 同上。必须真正 seek 完、位置落到目标并可呈现画面；只设置 currentTime 不算完成。 |
| `media_end_lead_s` | 默认、实际 0.5 | `0.5` | 自结尾前播放到自然结束，避免只 seek 到结尾留下旧帧；必须小于 seek timeout。真实菜谱视频验证过此修复，仍保留一次 seek 未落到目标的失败。 |
| `exclude_unloaded_checkpoints` | 现有配置为 `true` | `true` | 只排除明确 `capture_state.settle_state.loaded: false`。unknown、DOM 改变的 capped 不自动算 unloaded；也不能据此声称保留下来的全是稳定画面。没有 on/off 对照。 |
| `browser.frames` | 通用 flywheel 示例没有；lookahead 与十站实跑有 | `before_action: true`、`after_observation_delay_s: 1.0` | 保留动作前、观测约 1 秒后证据，供标注使用。不是 agent 新 observation；不能把延迟帧叫下一屏；缺失时保留 unknown。 |

### 网络请求匹配、归档与阻断语义

| 配置或策略 | 候选差异 | 最终采用 | 采用谁、理由与验证范围 |
|---|---|---|---|
| `network_source.source_id` | record 为 `live`；fork 为 `archive` | 保持 | 实际采集获取线上资源；离线分叉不补抓线上缺失内容。 |
| `network_source.isolation` | 默认、原模板、实际 `required` | `required` | 保持离线隔离；不用只依靠 Fetch 拦截的 `none_acknowledged`。 |
| `shift_dates` | 实跑与通用示例 `none`；原模板省略 | `none` | 显式沿用原始 HTTP 日期，和 `base_time: from_archive` 配套但不是同一个开关。 |
| `log_mode` | 默认、实际 `full` | `full` | 保留每次服务决策；`log_repeat_limit: 3` 只在 summary 模式有意义，本轮不用因此额外调参。 |
| `max_fulfill_body_bytes` | 默认、实际 67,108,864 | `67108864` | 64 MiB 单响应上限，控制 CDP 消息体积；超限产生 miss，不改成线上回源。没有上限 A/B。 |
| `fulfill_timeout_s` | 默认、实际 10 | `10.0` | 保持已有预算；超时失败，不能继续到真实网络。没有调参证据。 |
| `matcher.levels` | 默认与实际为 exact、normalized | `[exact, normalized]` | 多轮使用；fuzzy 未在这些采集验证，不为了提高命中率启用。`fuzzy_threshold: 0.5` 在未启用 fuzzy 时无效，保留默认即可。 |
| `matcher.site_rules` | 均为 `configs/replay_rules` | 沿用现有规则；准备时复制进轮次目录，每次运行再保存已加载快照 | 当前目录摘要与本次读取的 404 个分叉冻结摘要完全相同：`e467a2dd5e0fc7d5a05fe5d9190ebb4cd1aecdc40dd34dce2c8d9ea15d7a39dc`。没有另一个未经说明的新规则版本。 |
| `miss_policy_id` | 均为 `strict` | `strict` | 文档 miss 失败，主 frame 文档 miss 标发散；benign beacon/report/analytics miss 返回 204，其他子资源 miss 返回 404，并带 `x-replay-miss: 1`。始终不访问线上。**这不是录制时统一阻断媒体。** |
| `extra_benign_hosts` | 默认及实际 `null` | `null` | 不新增整站/视频域名白名单或 204 策略。现有 strict 内置分类和有证据的精确站点声明沿用。 |
| 响应用尽 `exhaustion` | 发布 baseline 显式 `repeat_last` | 沿用 | 是这组实验的复用策略，不是框架偷偷补的默认。状态接口若需要用尽即 miss，应另有证据定义，不能在本轮任意更换。日志保留 repeated 事实。 |
| 无 body 的 `unavailable` | 发布 baseline 显式 `serve_identical` | 沿用 | 只有同序列可用记录状态和正文都相同才代答；源网络失败不代答、录到错误码不改成 200。日志保留 substituted 和源结束原因。没有复用/不复用 A/B。 |
| 请求易变字段规则 | B 站时钟签名、爱奇艺访客/设备号、JSONP callback、nested `vinfoparam`/`bop` 等 | 沿用已限定端点、方法的现有规则 | 有逐端点请求和响应证据及反例测试。关键词、页码、排序、内容 ID、筛选、清晰度、byte range 等内容选择字段不能一起忽略。时间戳也不总是安全：同花顺 `end_time` 可能是分页边界。 |
| JSONP response 回调改写 | 请求忽略 callback 后仍需让返回包装匹配当前函数名 | 沿用 | 更早 12 站链一次比较多过约 6 个百分点；只支持明确包装形状。命中不等于页面必然成功消费响应。 |
| `page_urls` 规则 | B 站客户端写入 `vt`、同花顺 `sign` | 沿用 | 7 个同源分叉的检查点通过从 0 到 6；只作用于页面地址比较，两个 URL 都要匹配范围，其余字节保留。请求匹配不读这部分。 |
| `synthesize_204` | 现有规则含腾讯 Qidian sharing frame 的两个精确 POST 遥测端点等 | 不扩大范围 | 区分这些有证据的端点声明与 strict 的 benign miss 分类；不能改写成“腾讯视频资源全部阻断”。 |
| live 资源阻断 | 没有视频/广告/媒体资源类别的统一屏蔽 | 保持没有 | `standard` 域名限制为空；静音、reduced-motion、后台 freeze 各管一件事。离线未录到媒体字节而失败是证据覆盖问题，不能描述成录制主动禁视频。 |

| `archive.browser.network` 字段 | 十站 22 个源的实际值 | 最终采用及理由 |
|---|---|---|
| `max_inline_body_bytes` | `5000000` | 显式保留。小响应在完整下载前被 Chrome 持有；更大且不超过 64 MiB 的非文档响应走流式记录。不是按媒体类别禁止下载。 |
| `body_read_timeout_s` | `70.0` | 显式保留。过去提前放行仍在读的压缩脚本曾导致成功但空/截断的响应，使爱奇艺只显示骨架。没有理由重回提前释放或缩短预算。 |
| `command_timeout_s` | `1.0` | 显式保留，无参数对照。 |
| `drain_timeout_s` | `2.0` | 显式保留，每次收集等待已开始的正文读取；未完成仍记录 pending。 |
| `max_post_data_size` | `65536` | 显式保留，Network 事件内联 POST body 的上限。 |
| `require_replayable` | `true` | 显式保留这条 pinned 数据链要求。 |
| `allow_session_state` | `false` | 显式保留；从私有登录 seed 导入一个新建的 pinned context，不等同于复用持久 Chrome profile。 |
| `strip_request_headers` | `null` | 不新增额外剥离；Cookie、Authorization、Proxy-Authorization 仍由强制隐私规则剥离，不是可以关闭的默认。 |

归档最后的固定 5 秒 flush 是实现预算，不是可随意加到 YAML 的 knob。旧 IGN 在 8 个并发任务下有尾部 incomplete；降低并发对这个 flush 问题的效果没有单独对照。live 同时最多 3 个站点沿用已有操作证据：5 路 live 曾使下载资源争抢、爱奇艺停在骨架。不要把离线每 arm 的 8 workers 和 live 站点并发混成一个数字。

### 视频和动态页面：媒体区域究竟遮什么

`media_areas` 保留现有十条声明：腾讯视频播放/搜索、爱奇艺播放/搜索、IGN review/search/home/wiki、B 站 video/search。它们不是所有视频站的统一放行，也没有覆盖所有已知视频站的所有页面。

1. **录制不阻断、不暂停前台视频。** 原始截图、动作前帧和模型图片保持真实；`mute_audio` 只静音，`reduced_motion` 只是偏好，`freeze` 只作用于被离开的后台标签页。
2. **恢复先尝试媒体对齐。** `media: align` 仍执行。加密失败、没录到的分片、直播和错误对应不能靠 seek 补齐。既有嵌套请求规则虽让腾讯 player info、爱奇艺 stream 请求命中，但那次同源对照的检查点仍是腾讯 0/23、爱奇艺 0/6；请求命中率不是视频可播放证明。
3. **只在源和恢复页面 URL 都符合声明时改变比较。** 媒体状态仍测量、仍写证据，但不再据它判 `media_mismatch`。两张用于计算距离的图片副本遮掉双方测得的 video/audio 框与不可读 frame 框的并集；原图没有涂黑。URL、动作结果、文档 miss、框外像素仍检查。
4. **超过 75% 时不遮，退回完整图片比较。** 75% 是实现常量，不是待添加的 YAML 参数；恰好 75% 仍可遮。退回整图时，声明页的媒体状态仍不参与判失败，不会自动恢复全部媒体严格校验。这个保护来自腾讯首页 100% 和频道页 87% 不可读 frame：大面积涂黑可能把明显失败变成通过。现有声明没有把这两类腾讯页扩大纳入。
5. **通过率上升属于校验范围重新分类。** 334 个既有分叉换比较规则重算，腾讯检查点从 0/70 到 52/70；14 个分叉做同配置有/无规则的重放，腾讯检查点从 0/3 到 3/3。IGN 新增首页/wiki 后既有检查点从 42/64 到 53/64，12 个失败检查点再跑有 6 个恢复。这些是明确局部证据，不能写成视频播放保真提高或所有任务都可忽略视频内容。字幕、剧情、时刻等若与任务有关，人工审阅仍要看原图和媒体事实。

`stability --media-areas` 使用同样的声明和 75% 上限辅助定位自动变化；这仍不等于证明加载结束。10 月 26 日那次 60 条旧标注的 judge 改进是开发集回归；它改变的是审阅定义/判断，不是十站录制浏览器配置，也不是独立准确率测试。

证据路径：逐字段数据在 `tmp/config-reconciliation/browser-audit.json`、`browser-trace-values.json`、`browser-fingerprint-values.json`、`browser-other-defaults.json`。历史媒体对齐结果见 `.agents/skills/af-project-history/references/enhance.md:354`，请求/媒体规则的同源对照见 `configs/replay_rules/README.md:100`、`:168`、`:528`；当前规范见 `docs/architecture_design.md:3383`、`:3466`、`:3737`。更早五站原始轨迹已删除，本次使用保留下来的规范与历史报告记录，未重新运行 Chrome 或模型。

### 分叉恢复与预算

Agent、loop 与 memory 的共有预算见上面的 Teacher 表。分叉 `max_steps` 不限定恢复前缀长度；恢复后新策略步预算为 `min(1 + continue_steps, max_steps)`。

| 项目 | 候选差异 | 最终采用 | 采用谁、为什么 | 证据范围与限制 |
|---|---|---|---|---|
| 新决策预算 | 三臂 continue_steps 0；通用配置允许更多 | **continue_steps: 0** | 每个检查点只采一次新 policy 决定，随后观察结果，符合当前标注单元。 | `min(1 + continue_steps, max_steps)` 才是分叉的新策略步预算。单步 branch 的 max_steps 结局不是任务失败。 |
| 恢复器 | 都 prefix_replay；generic branch strict，collection warn | **prefix_replay / warn** | 取采集实跑；让不同前缀检查结果留下证据，再按检查点评定能否审阅。 | 509 个 checkpoint ok 中，321 个此前至少有一次 warn；全检查通过只有 188/932。strict 仍适合专门保真控制，并非框架不可用。 |
| 恢复节奏 | 当前 timeline；十站 paired none/timeline；更早有 latency | **recorded_timeline** | 默认选择更接近录制时序的一臂；none 留作专门控制，不每轮重跑。 | 同源配对 466 个：241 两臂都 ok，25 仅 timeline ok，2 仅 none ok，198 都失败；266/466 对 243/466，净增 4.94 个百分点。总 execution 78,868 秒对 23,633 秒，约 3.34 倍；不是墙钟工期。没有同批 latency 臂，不能声称胜过 latency 或达到最优。 |
| 历史来源 | 同任务 source；两类换任务 restored | **保持该分工** | 同任务在原上下文上重新采样；换任务必须 rebuilt restored facts，因为源 prompt 带原任务。 | restored 的实际动作和结果来自本次恢复；源回复只是带出处的材料。不得把它展示或统计成本次新 thinking。 |
| 去源回复 | 第三臂 omit_source_replies；不是所有变体默认 | **保留独立第三对照臂** | 与普通 variant 复用同一份任务文件与预算，继续观察源任务回复的影响；不把未经证明的消融替换成默认历史。 | timeline 下旧 judge 合理性 63/71 对 59/69、效果 39/71 对 38/69，条件入组不同且无人工 gold，不能宣布胜负；旧 effect 还包含当时的 done-only 口径。该 hook 只去掉送入 restored prompt 的源回复，不删 trace、不禁止当前模型原生思考。 |

### 选步、重复数与三臂成本

| 项目 | 候选差异 | 最终采用 | 采用谁、为什么 | 证据范围与限制 |
|---|---|---|---|---|
| 同任务选步 | generic all/3；停下的 BrowserUse 为 k4/offset0；当前与 UI-TARS 实跑 k4/offset1/2 | **every_k: {k: 4, offset: 1}，每点 2 个** | 取 UI-TARS 实跑与当前配置；稀疏覆盖全程，每点保留重复采样。 | 十站每 pace 为 101 个 checkpoint、202 条分叉；不是最佳抽样间隔的证明。 |
| 变体生成 | 当前和实跑均每源 2 个点、每点 2 个任务 | **steps-per-run 2，per-step 2** | 保留不同屏幕和不同信息需求的覆盖。 | 原健康名单下 18 源生成 36 点、72 个变体。实际分叉预算要看最终 `--list`，不能直接乘生成条数。 |
| 每个变体的重复数 | 当前每 variant 1；十站每 variant 2 | **普通 variant 与去源回复均为 1** | 取当前较小预算，理由是下一轮先覆盖不同信息需求；同任务臂已有重复决策样本。两 variant 臂各 1 仍保留同一任务上的消融对照。 | 以十站原任务列表计，单 pace 三臂由 202+132+132=466 降到 202+66+66=334，少 132 条、28.3%。代价是不能估计同一 variant 在同一臂内的采样方差；研究该问题时另设 2。没有证据说 1 的效果更好，也不是为了声称复刻旧轮次。 |
| 未加载检查点 | 两者 true；有时误将所有 settle capped 都剔除 | **exclude_unloaded_checkpoints: true** | 只按明确 `capture_state.settle_state.loaded: false` 排除；DOM-only capped 不等于未加载。 | 实跑 72 变体中 6 个被 loader 排除：Steam 135@7、35@6、35@20 三个点。前两点甚至记录 settled，但 loaded 为 false。当前 variants 生成与 loader 存在这层筛选差异。 |
| 源结局与不可分叉源 | 当前与实跑 any / skip；generic raise | **agent_outcome: any；on_unbranchable: skip** | 不把任务未完成等同无数据；必须同时满足闭合 trace、完整前缀和录制范围要求，并保留 skipped 理由。 | 源有 2 个 max_steps；单步 branch 常见 max_steps。any 不代表损坏轨迹也能分叉。 |
| 对照预算冻结 | 当前 README 只说先 list；实跑脚本先比较两 pace 列表 | **保存每臂列表和摘要；成对臂确认相同源点与变体** | 取实跑可复核操作；变体/去源回复共享同一文件，不重新生成各自的任务。 | 健康名单或 loaded 筛选改变会改变分母；新批统计另存，不回写旧报告。 |

### 归档与录制证据

除网络归档表中的值外，保持并显式填写：`browser.storage.command_timeout_s: 1.0`；`browser.dom_raw.self_capture_timeout_s: 10.0`；`browser.frames.capture_timeout_s: 3.0`、`max_action_wait_s: 1.0`。这些 archiver 的 `allow_session_state: false` 保持默认；新 context 的登录种子仍可使用，复用持久 profile 是另一项设置。值取自现有录制/默认预算，没有阈值优化对照。

| 项目 | 候选差异 | 最终采用 | 采用谁、为什么 | 证据范围与限制 |
|---|---|---|---|---|
| recording | 当前显式四通道；历史 YAML 多依赖默认 | **metrics / errors / trajectory / observations 全 true** | 取两者实际行为并显式化。timeline 依赖 action timing；review 历史与 thinking 依赖正文通道。 | 22 源都是 v7、complete，567 policy +22 terminal，共 589 观察。已有 v7 无需为新界面重录；提示身份变化须重录是另一件事。 |
| live archivers | generic 三类；collection 加 frames | **network + storage + dom_raw + frames** | 取 collection 实跑；纯视觉 agent 仍需 rawDOM 支撑重定位、诊断与后续坐标处理。 | 归档不是 agent 额外观察，不能把辅助帧当下一次 observation。 |
| offline archivers | 网络录制不能用在 archive source | **replay_log + storage + dom_raw + frames** | replay log 替换 network，其他同录制。 | 来源严格离线，miss 不临时上线补。 |
| replay log / matcher | 部分历史靠默认，当前 full / exact+normalized | **log_mode full；exact+normalized；site_rules；strict miss；isolation required；shift_dates none** | 保留可追溯的逐请求诊断；不为压缩日志使用 summary 丢掉近邻 miss 细节。 | fuzzy 没有采集证据；站点规则必须冻结到 run，仅忽略不选内容的字段。 |
| frames | 当前显式 before=true / after=1s；实际默认同值 | **before_action true，after_observation_delay_s 1.0** | 取现有采集行为。 | 公共 reader 全量审计：1050 实际 action_spans 对应 1050 动作前记录，其中 1043 captured、7 timeout；589 次 after_observation 全 captured。1050 不包括另 24 个 skipped 动作。帧缺失不可推定稳定。 |
| archive 子预算 | storage 1s、flush 5s、network body 70s | **保持这些值，不无证据调大** | 用真实缺口说明风险并独立重跑故障任务，避免把 task timeout 当所有故障的解决开关。 | 10/25 唯一 incomplete 是 Storage.getCookies 的 1s 超时，不是网络 flush 5s；10/24 的另几条缺口才与重页并行归档有关。 |

### 离线并发的采用决定

| 项目 | 候选差异 | 最终采用 | 采用谁、为什么 | 证据范围与限制 |
|---|---|---|---|---|
| offline 并发 | README说几个；10/25每臂 8；更早 variant 4 | **每臂最多 8 个 disjoint workers，臂间顺序** | 取十站实际吞吐配置。932 个任务中 931 complete，未出现 task timeout；凭一例 storage 故障改为 4 没有证据可解决，却预计明显延长工期。 | 8 不是性能最优证明；保存每 worker session/log，结束后检查 recording health，独立重跑 incomplete 项，保留原失败记录。出现持续资源故障时再降低实际 worker 数并记录该批差异。 |

### 导出与历史说明

| 项目 | 候选差异 | 最终采用 | 采用谁、为什么 | 证据范围与限制 |
|---|---|---|---|---|
| 默认交付 | 通用flywheel有SFT；lookahead轮次实际只到review | **本轮交付teacher traces与labels，SFT仍是独立后续流程** | 取实际采集目标，避免把可审阅分叉数当训练样本数。 | 旧报告可评507；其中包含唯一incomplete run里可读的checkpoint决定。全恢复检查无warn只有188/932，导出还会受其他过滤。 |
| export filters | generic exclude_restoration_diverged=true、document_misses0、exclude_unloaded=true | **保持严格过滤，不为产量关掉** | 训练要求与 review 的 checkpoint 入组不同。 | warn采集与严格export同时使用会损失大量分叉，真实十站尚未做该导出；不能说507都可训练。 |
| wait_actions | 默认 keep；新功能 drop_when_capped | **keep** | 保留未经过真实轮次验证的后处理为独立试验，防止改写 thought 与动作关联。 | drop_when_capped需helper及缓存，只做过fixture，不是采集默认。 |
| 隐私字段 | RUN主要提secrets_recorded policy | **原authenticated trace私有；导出产物独立检查，不以secrets_recorded:false推出可公开** | 当前22源该字段全false，但env_evidence_privacy全含contains_session_state。 | secret录制开关与环境cookie/Web Storage原始证据是不同范围；不把env目录一并发布。 |
| 10/26复核 | 新judge开发集局部改善 | **规则预筛+人工继续保留，judge-stability按需补充，不替换gold** | 新方法在原始分类一致率41/60低于旧45/60；解释六条备注后与二分类才提升，不能只挑有利口径。 | 60条开发集、人工框全空，不能报告定位IoU；不回写人工答案。 |

### 重算口径

- 932 分叉，509 个已提交 checkpoint 为 ok；只看 manifest complete 的 run 是 508。
- 既有 judge 报告入组 507：509 中有 1 个解析失败、1 个 completion-token ceiling 截断。报告入组允许那条 incomplete run 的可读 checkpoint；若新加“整 run complete”门槛，会变成 506，必须另标口径。
- 509 个 checkpoint ok 中 321 个此前有 warn；全检查 ok 为 188。
- 重算脚本和结果在 `tmp/config-reconciliation/compute_branch_audit.py`、`branch-metrics.json`、`branch-audit.json`。

### 各类网站的表现（10 月 24 日及更早的证据）

| 页面类型 | 例子 | 录制时 | 分叉恢复（注明轮次的可评判比例） | 要注意的 |
|---|---|---|---|---|
| 行情、选股 | 同花顺、东方财富 | 几乎都等到了落定，但约三分之一的画面 1 秒内又变了（行情在跳） | 最好，近九成 | K 线请求的 `end_time` 有时是客户端时间、有时是翻页边界，规则分不开；首页推荐词一直在滚。东方财富的滑块验证码离线能显示，拖动的判定请求带着时间和随机数，回放不了 |
| 视频站 | 腾讯视频、爱奇艺、B 站 | 播放页常等满 5 秒上限（视频一直在改变页面） | 腾讯视频近六成，爱奇艺约一半，B 站九成以上 | 离不开媒体区域规则。占满屏幕的自动预告（涂黑超过 75%）、shadow root 里 iframe 中的播放器处理不了；离线播放器常因加密的流请求不播；teacher 点了播放器上的广告或跳到别的视频站，之后的检查点全废（这一轮因此丢了约 7% 的分叉） |
| 多标签页 | 爱奇艺 | 不冻结后台标签页会把渲染进程拖死 | — | 一定要 `background_tabs: freeze`；新标签页 10 秒的采纳等待见上 |
| 搜索结果页 | B 站、爱奇艺、腾讯视频、同花顺 | 慢网络上常拍到"正在搜索" | 中等 | B 站 `vt`、同花顺 `sign` 靠页面地址规则；爱奇艺搜索框的轮播占位词随时间变，分叉比录制跑得快时，回车搜的是另一个词；腾讯视频搜索框回车会导航两次，记一次文档 miss |
| 单页应用、页面自己改地址 | B 站个人空间、同花顺问财 | 落定等待不等在途请求 | — | 页面写进地址的参数用页面地址规则；带签名的数据接口用请求规则 |
| 游戏资讯 | IGN | — | 八成多 | 攻略页用 `Math.random` 从 8 个视频里随机挑一个，挑到别的之后全部偏离；连按 PageDown 后有时滚到不同的位置 |
| 新闻、门户、论坛 | V2EX、21 世纪经济报道、起点、豆瓣读书、澎湃、网易新闻 | — | 原路径回放校验约八成通过：V2EX、21 世纪经济报道、起点、豆瓣读书全过，网易新闻约三分之一，剩下的主要是轮播和定时切换的内容 | 〔一次，12 站，脚本化模型，每站两次〕那时 pinned 还是每文档一个时钟，换成共享时钟之后没在这些站上重测 |
| 需要登录 | 小红书、B 站 | `storage_state` 种子 | B 站补了个人空间接口的规则后，30 次校验从只过几次到过 24–25 次 | 〔小样本，各一个账号〕B 站的登录信息接口（`/x/web-interface/nav`）按录制顺序应答，可能答给别的页面，没解决 |
| 有风控的站 | 携程、搜狗 | 携程那一轮三个录制几乎全是拦截页，已剔除；搜狗筛站时出验证码 | 拦截页上的"恢复"没有意义 | 度量报告只按应答头认 Cloudflare 拦截，健康检查也认不出这类页面；拟人鼠标救不了 |

### 审核与标注

- `python -m lookahead stability ... --media-areas configs/replay_rules`〔多轮〕：四到五成的观测在拍下之后 1 秒内就变了；有媒体在播的观测几乎全在媒体上变（上一轮百里有九十九），不加这个选项视频页几乎永远算不稳定。变化按 32 像素网格判，dHash 只作参考。
- `judge-forks` 的模型、请求预算与重问行为采用上面的独立 judge 表。恢复入组只看可读取的检查点证据；规则与模型的一致性、任务正确性与人工 gold 分开报告。
- `gold-queue` 抽 100 条，用 `spectragent view --root <轮次目录> --annotations <标注目录> --annotator <名字>` 标注。10 月 25 日十站轮次已建三类队列；10 月 26 日保存的稳定性标注有 60 条，并做了固定队列的开发集复核。它不是独立测试集准确率，也不表示分叉决策和 gold 区域标注已完成。

当前审阅交互（2026 年 10 月 27 日）按以下方式读证据；详细命令见 [lookahead/README.md](../lookahead/README.md)：

- **分叉决策**：先按分叉任务、检查点观察、完整回复与动作判断“这次决定合理吗”，再按动作后的画面和返回结果判断“出现预期结果了吗”。只有完成/回答动作的条目不问执行效果；新判断和旧答案复用都按适用题目保存，报告把这类效果列为 `not applicable`，不计入“否”或未回复。同任务另展开“与原决定比较”，并排看两边各自的检查点、动作与回复，独立判断同意图；两次操作后的画面另组折叠供参考，终止观测不算新决定。换任务不问同意图；恢复通过不代表两张原图完全一致。
- **下一步信息**：按“当前观察 → 第一个动作之前 → 下一次观察”看图。中图只是模型思考结束后的辅助帧，缺帧会说明；右图上的动作是基于右图作出的下一步决定。读完整的下一步回复与动作，只在右图 `next` 框出实际用到的信息；辅助图不能画框，前后端与建议校验都限制这个范围。
- **历史档案**：当前观察下的卡片只展示该 run 截至条目当前步的已提交历史，悬停预览、点击固定详情，再读完整观察、回复、已有 thinking、动作结果及同一步操作前截图。恢复步的源回复标为来源材料，不作本次新思考；实际模型请求及其历史窗口按需展开，与完整轨迹分开。未返回的 thinking、未采集或超时的帧仍是缺证据。已有队列需重新生成才有新布局；分叉队列可用 `--answers-from` 复用判断，无需只为更新界面重采或重问模型。队列版本校验和 trace 只读边界保持不变。这些交互变化不增加历史数据的覆盖率，也不代表人工标注已经完成。

### 已知不行与还没验证的

已知不行：

- 云浏览器后端录制或回放；`clock: per_document` 配多标签页同步的站点；用 `background_throttling: true` 代替冻结。
- 本轮采集不采用 `verify: strict`（大部分在前缀就被拒；专门保真对照仍可使用）；不放宽 `screenshot_max_distance` 来放过视频差异（阈值 5 以内已经放过过播放位置不同的画面，是媒体校验抓出来的）。
- 控制页面时间（Chrome 虚拟时间）：六站探针里五站在截图时失败，没有接入。
- 规则救不了的：选内容的字段、涂黑超过 75% 的全屏自动预告、shadow root 里 iframe 中的播放器、跳到录下字节之外的视频位置、WebSocket、录到的加载中页面（回放不按录下的时长延迟应答）。
- 靠拟人鼠标和打字通过 reCAPTCHA v2。

还没验证：`recorded_timeline` 与 `recorded_llm_latency` 的同源对照；当前整套新采集配置；`wait_actions: drop_when_capped`、导出的未加载筛选在真实一轮上的效果，以及检查点未加载筛选的增益对照；`send_keys` 执行浏览器快捷键之后 teacher 离站卡住的情况是否变少；pinned 写普通 Chrome 标识对风控站的影响；`matcher` 的 `fuzzy` 级别；`screenshot_format: jpeg`；`lifetime: experiment` 共享 Chrome 下的录制；整轮用 DeepSeek 官方接口做 judge；替换 teacher 路由后的整轮表现；半加载画面在评审标注里怎么算。

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
    tests/test_core/test_capabilities/test_browser_new_tab_live.py \
    tests/test_core/test_capabilities/test_browser_replay_live.py
  ```

- `pinned` 档位要求每个 run 从新建的 profile 启动：`user_data_dir`、不是 `Default` 的 `profile_directory`、`extra_args` 里的 `--user-data-dir` 都会被拒。profile 目录只由 YAML 决定，环境变量 `CHROME_USER_DATA_DIR` 不再起作用。provider 上与档位矛盾的旋钮也会被拒，值相同的放行：`deterministic_rendering: true`（它的 V8 种子与档位的 `random_seed` 不同时），与档位不同的 `force_device_scale_factor` 或 `user_agent`，`extra_args` 里由档位负责、值不同的开关。`random_seed` 至少是 1。评测 run 和 anti-bot 实验保持 `none`：pinned 会改变站点的行为，本身也是 anti-bot 能识别的特征。
- 每个手写的 capability 块都要写下面两个块。它们是生成 schema 里必填的单实例受体，不录制的配置也要写：

  ```yaml
  replayability: { replayability_id: none }     # 录制用 pinned
  network_source: { source_id: live }          # 回放用 archive
  ```

  这一档原来叫 `off`，现在叫 `none`，不用加引号；写 `off` 会被拒（PyYAML 按 YAML 1.1 把裸的 `off` 读成 `false`）。
- 归档器只写在 `trace_writer` 的 `archive` 列表里，键要全限定（`browser.network`）。写进 capability 块的 `archiver:` 在组装时被拒。
- 每次观测之前，browser capability 先等页面落定，录制、原路径回放、分叉都一样：主 frame 的文档加载完（`load`），之后连续 `settle_quiet_ms`（默认 500 ms）没有 DOM 变化、也没有资源加载完成，而且视口里看得见东西（文字或图形元素；只有 CSS 加载条的白页不算，`about:blank` 不等）；最多等 `settle_max_wait_s`（默认 5 s），到了就按当时的样子采集。只协商 DOM 的观测与要截图的观测等的是同一个等待。两个旋钮写在 capability 块上，发布的 YAML 不写，默认值录制与回放都适用，数字原样记进 `config.json`；`settle_max_wait_s: 0` 关掉等待，窗口比上限长时组装报错。**这是时间开销**：每次观测至少多等一个静默窗口（500 ms）；一直在变的页面（轮播、视频、ticker）和视口一直空着的页面每一步都等满上限。40 个真实站点上实测（architecture §4.14）：`navigate` 之后平均多等 1.75 s，滚动之后 1.98 s，点击引起导航之后 4.34 s（多数等满上限）。每个观测 fragment（`browser.dom`、`browser.screenshot`、`browser.browser_meta`）都带这次采集的 `settle` 记录：`waited_ms`、`outcome`（`settled`、`capped`、`unknown`）和 `waiting_for`（被上限截断时还没等到的 `load`、`dom` 或 `content`；只有 `content` 是页面静了、视口一直空着）；度量报告在归档开销一节按角色给出 p50、p95 和被上限截断的占比。
- 这些组合问题全部在 `prepare()` 报：归档器与档位、provider 与它的旋钮、profile、网络来源，以及回放时与源 run 的一致性（`prepare()` 只读源 run 的环境指纹）。错误在打开 trace、启动 Chrome 之前出现，每条点名两边，`spectragent run --dry-run` 也查得出来（回放配置要加 `--replay-archive DIR`）。`spectragent bench` 开跑前先用第一个任务检查一次（pre-flight）：被拒时一个任务也不跑、不建 trace，在 stderr 印出原因，退出码 2。setup 在启动 Chrome 之前再查一遍。

- 登录初态可通过任务 `BrowserSetup.storage_state` 文档或 JSON 路径输入；`pinned` 接受 fresh context + seed，仍拒绝持久 profile。选择 `browser.storage` 才记录 seed 正文为 `initial_state.json`；fingerprint 总记录 `initial_storage` 的 digest、是否非空与覆盖范围。回放 / 分叉读取初态，绝不从 step 0 动作后的 storage 快照推断。非空 seed 没录正文、摘要不符、显式 seed 冲突或持久 profile 未知 origin 覆盖，prepare 明确拒绝。正文不重复进 task_setup，原 JSON 后来删改不影响分叉。cookie、local、session（含 CHIPS / partition key / OOPIF）是声明覆盖；IndexedDB / SW / cache / 完整 profile 不在内。要在页面登录，把凭据与步骤放进 task_text，由 Agent 既有动作执行。

## 录制

`configs/flywheel/record_ui_tars_pinned.yaml` 是发布的录制配置：UITarsAgent（`coordinate_space: screenshot_pixels`，`max_actions_per_step: 4`），`replayability: { replayability_id: pinned, random_seed: 7 }`，`network_source: { source_id: live }`，trace hook 上绑三个归档器。录制前复制一份（下面记作 `my/record.yaml`），把 `trace_writer.trace_dir` 从发布值 `traces` 改成 `traces/recorded`：发布的分叉与原路径回放配置从这个目录找源 run，导出配置也读 `traces/recorded/*`。回放 run 和分叉 run 不能当分叉的源，和录制混在一个目录里时，分叉 sweep 在开跑前就报错（`on_unbranchable: raise`）。

```bash
export OPENAI_API_KEY="你的密钥"
uv run spectragent bench --config my/record.yaml \
  --benchmark online_mind2web --subset All --task-id <ID> --no-eval
```

基准数据不随仓库发布，数据目录怎么设见 [Browser agent 运行手册](browser_agent_runbook.md) 的"跑数据集"。单个自由文本任务用 `spectragent run --config my/record.yaml --start-url URL --task "..."`。数据集任务的 trace 目录是 `<trace_dir>/<task_id>_<8 位随机串>`，`spectragent run` 的是 `<trace_dir>/<run_id>`。飞轮的每一段都不需要评测结果；只有想按评测结果筛选源 run（分叉和导出的可选过滤 `require_eval`，默认 `null`）时，才在录制配置里加 `evaluator:` 块、去掉 `--no-eval`，判定会写进每个 trace 的 `evals/`。

```yaml
hook:
  - hook_id: trace_writer
    trace_dir: traces/recorded
    archive:
      - archiver_id: browser.network   # 每个请求和响应，含 body
      - archiver_id: browser.storage   # cookie 与 local / session storage
      - archiver_id: browser.dom_raw   # 每个提交的观测 cycle 的原始 DOM
```

`trace_writer.recording` 明确选择持久化内容。默认 metrics、errors、trajectory、observations 全开；指标实验可写：

```yaml
recording:
  metrics: true
  errors: true
  trajectory: false
  observations: false
```

这种轻量记录仍写磁盘，读取 token 和失败调用不需要解码图片。完全不配置记录器时用量/调用次数为未测量；provider 未报用量与明确零用量分别显示。`metrics.usage_coverage` 表示实际调用的 provider 覆盖，`metrics.recording_health` 表示持久化覆盖。写盘失败不会改变 Agent 执行结果，`missing_steps` 和 `complete_through` 说明缺口；洞后保存内容可以查看，分叉和导出只用满足范围与完整历史条件的部分。恢复写入在后台有界进行：pending_segments 默认 64、write_retries 默认 2、retry_delay 默认 0.05 秒。收尾分两段各自计时：最后一次归档收集最多 closeout_collection_timeout（默认 10 秒），之后 close_timeout（默认 5 秒）留给写完队列、写 result.json 并关闭 manifest，所以浏览器卡死的 run 也以 incomplete 关闭。某一步的归档收集失败或只收到一部分时，这一步照样记下（步记录、观测、调用），缺的那一路证据记成该步的一条记录问题，其余归档器的证据照常保存。

`archive` 是录不录环境证据的唯一开关：不写、写 `null` 或 `[]` 都不录。归档器只能写在这里，YAML 顶层写 `archiver:` 段会在组装时报错，提示把归档器写进 `trace_writer.archive`。`browser.network` 默认 `require_replayable: true`，与 `none` 档位一起绑定会被拒；要把一次评测 run 照原样存档，写 `require_replayable: false`，这样的归档不保证能回放。

证据都在 run 目录的 `env/` 下，只有绑定了归档器的 run 才有这个目录：

| 路径 | 内容 |
|---|---|
| `env/steps/NNN/browser.network/exchanges.warc.gz` | 第 NNN 步的提交收集到的交换，WARC/1.1，body 是解码后的字节；同一收集批次里重复的 payload 写成 revisit 记录 |
| `env/steps/NNN/browser.network/index.jsonl` | 同一步的索引：`kind: exchange`（每个交换恰好一行，带 `body_status`、请求开始与结束的墙钟时间 `started_at` / `ended_at`，以及结局事实 `error_text`、`body_note`、`from_cache`），`kind: body`（边下边收的大 body 收完时一行，指向它的 blob，带这次请求的 `ended_at` 与 `error_text`），`kind: pending`（收集时还在途，带 `started_at`） |
| `env/steps/NNN/browser.network/websocket.jsonl` | WebSocket 帧（方向、opcode、时间、内容），有帧时才有 |
| `env/steps/NNN/browser.storage/storage.json` | cookie 和每个 origin 的 local / session storage，值原样保存；状态变了才写，某一步的状态是那一步及之前最近的一份 |
| `env/steps/NNN/browser.frames/frames.json` | 绑定了 `browser.frames` 时才有：这一步收集到的每一帧（动作之前 `before_action`、观测之后 `after_observation`），结果是 `captured`、`superseded`（被动作或新的观测作废）、`timeout`、`failed` 或 `no_tab`，格式见 [architecture_design.md](architecture_design.md) §4.14 `browser.frames` |
| `env/blobs/sha256/<ab>/<rest>` | 内容寻址的文件，run 内只存一份：`browser.dom_raw` 的 `dom-<cycle>-<tab>.json.gz`，`browser.frames` 的 `frame-<cycle>-before-<i>.png` / `frame-<cycle>-after.png`，边下边收的大 body `body-<sha>.bin` |
| `env/run.browser.json`，之后的 `env/run.browser.1.json`…… | 环境指纹：Chrome 版本、浏览器的默认时区与语言（`browser_defaults`：没有 override 够得着的 frame 读到的值，启动后实测）、UA 与 pinned 带的 client hints、viewport、DPR、时区、语言、档位与参数（含 T0 和种子）、shim 摘要、启动开关摘要（含启动设的 `TZ`）、网络来源、脱敏策略与隐私标记；内容变了才写新的一版 |
| `env/trailing/KK/<archiver_id>/<name>` | run 结束时那次收集的证据，不归任何一步 |

`body_status` 的取值：`ok`、`redirect`、`too_large`（超过 64 MiB，回放默认能送出的上限，不论类型都不存）、`streamed_later`（声明的长度超过 `max_inline_body_bytes`，默认 5 MB：页面下载的同时边下边收，不扣响应；请求结束时 `kind: body` 行说收没收全）、`websocket`、`incomplete`（超时、请求中断，或 run 结束时没收完）。内联读把响应一直扣到读答回，最多 `body_read_timeout_s`（默认 70 s），慢的 body 也整个存下；等的期间这条是 `pending`（`waiting_for: body`）。读答回之前从不放行：放行不让页面早拿到 body，却会让之后断掉的压缩下载在页面一侧变成成功的空响应（爱奇艺首页因此一直停在骨架屏）。读答回之后下载失败的，记 `incomplete`，`body_note` 以 "the download failed after its body was read" 开头，`error_text` 是 Chrome 报的原因。`started_at` / `ended_at` 是墙钟 Unix 秒，与步记录的时间可比；`exchange` 行写出时请求还没结束的（body 在 Response 阶段就读完了、终结事件还没到），`ended_at` 是 `null`。没有正文的记录在回放时怎么答，由这些结局事实推出的源结局决定（见"离线回放一个 run"）。超过 5 MB 的文档例外，照样在暂停里读完（导航的 body 提交之后才开得了流）。UI-TARS 是纯视觉 agent，不协商 DOM，`browser.dom_raw` 会在每个观测 cycle 自己多采一次 DOM。要给前瞻模型做标签，在 `archive` 里再加 `- archiver_id: browser.frames`：每个动作碰页面之前拍一张视口截图（动作通常多等约 30–50 ms，最多 `max_action_wait_s`，默认 1 s；一个 tab 连续超时两次之后不再等），每次观测之后 1 s 再拍一张（`after_observation_delay_s`），agent 从来看不到这些帧；用 `spectragent.core.capabilities.browser.archive.frames` 的 `iter_frames(trace)` 与 `read_frame_image(trace, step, frame)` 读。`before_action` 帧按 `action_index` 对应那一步的 `action_spans`，`released_at - requested_at` 是录制给这个动作加的时间。各归档器的其余旋钮（`body_read_timeout_s`、`drain_timeout_s`、`strip_request_headers`、`self_capture_timeout_s` 等）见生成的 schema，拼错的键在校验时被拒。

pinned 的旋钮：`base_time`（录制用默认的 `run_start`，回放和分叉用 `from_archive`）、`clock`（默认 `shared`：整个 run 共用一个时钟；`per_document`：每个文档一个计数时钟，见"已知限制"）、`random_seed`、`timezone`（默认 `UTC`）、`locale`（默认 `en-US`）、`device_scale_factor`（默认 1.0）、`user_agent`（默认不固定，只固定 `Accept-Language`；写普通 Chrome 标识时版本要写本机 Chrome 的大版本，见下）、`reduced_motion`（默认 `true`）。回放会把这些参数、viewport 和 Chrome 启动命令行的摘要（调试端口、profile 目录和网络路由开关除外）与录制逐项比较，所以录制、回放、分叉三份配置的档位参数、`viewport_width` / `viewport_height` 和 backend 块（`headless`、窗口大小、`extra_args`……）都要相同。

颜色主题不需要 YAML 旋钮。pinned 录制在浏览器启动后、业务页面准备前测出当前媒体主题（`light` 或 `dark`），本次运行的所有 tab 与进程外 iframe 都固定该值，与 `reduced_motion` 同时生效。指纹的 `replayability.color_scheme` 记录测量值；回放和分叉直接沿用它，即使回放机器当前主题不同也不拒绝。录制测不到就明确失败；源归档缺少该字段或值非法时，在 `prepare()` / `--dry-run` 拒绝，必须重录，不猜浅色、不修改旧归档。`none` 保持宿主行为；网站自己的账户、Cookie 或按钮主题偏好仍由网站决定。

pinned 启动的 Chrome 带着环境变量 `TZ=<timezone>`。Chrome 的时区与语言 override 在一个渲染进程里只认最先设它的那个 frame，这个 frame 走了（iframe 被移除、打开弹窗的页面跳到别的站点），同进程的其余 frame 就落回浏览器的默认值；`TZ` 让默认时区就是 pin 的时区。默认语言定不下来（macOS 上 Chrome 取系统语言，`--lang` 和 `LANG` 都不起作用），所以录制把浏览器的默认时区与语言记进指纹，回放在默认值与录制不同的机器上被拒绝：`prepare()` 时比较本机系统设置读得出的（时区；macOS 上还有语言），启动浏览器之后、准备任何 tab 之前再按 Chrome 实测的比较一次。录制和回放在同一台机器上，或者系统语言相同的机器上，就不会碰到这一条。

浏览器标识与 client hints：页面读两处浏览器身份，UA 字符串，和 `navigator.userAgentData` 与请求头 `Sec-CH-UA*` 里的 client hints（brands、平台、是否移动端）。pinned 在浏览器启动之后、准备任何 tab 之前，先在一个后台的临时 `chrome://version` 页里读出浏览器自己的 client hints，再随 UA override 一起发给每个 tab 和 worker。条件是标识是 Chrome 自己的写法：不写 `user_agent`，或写普通 Chrome 标识，例如 live 配置用的 `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36`。这样页面读到的就是这个 Chrome 自己给的，打不到 opener 的新 tab 的第一个请求也一样。普通 Chrome 标识的大版本、平台或移动端与浏览器自己的不符时，setup 以 `BrowserCooperationError` 拒绝这个 run，点名两边；要启动浏览器才知道，所以 `--dry-run` 和链的开跑前检查都查不出。Chrome 自动更新之后改标识里的版本号就行。别的写法的标识不带 client hints：brands 为空，不发 `Sec-CH-UA`。指纹的 `replayability.client_hints` 记着发出去的值，`page.userAgentData` 是其中页面不问就读得到的那部分。回放在启动之后比较：pin 了标识时与录制的不同就拒绝，没 pin 时只记一条警告。在 override 带上 client hints 之前录的 pinned run，回放一律拒绝（那时页面读到的 brands 是空的），要重录。

页面打开的新 tab（`target=_blank` 链接、`window.open`）从第一个请求起就被扣住、录下、pin 住，不论它能不能访问打开它的页面；`create_tab(url)` 打开的 tab 先建成 `about:blank`，recorder 准备好之后再导航。

agent 的动作让当前页开了新 tab 时，下一次观测看到的就是这个新 tab（capability 的 `new_tabs`，默认 `follow`，与真实浏览器把新 tab 提到前台一样）；页面在没有用户点击的情况下自己开的 tab 不跟。录制、回放、分叉按同一条规则走，三份配置用同一个值（发布的配置都用默认值）：分叉的前缀回放重新执行录下的点击，焦点在同一次观测之前再跟过去一次，恢复校验比较的就是跟过去之后的那个 tab；两边的值不一样时，第一处跟随就以 `url_mismatch` 发散。焦点移过的那次观测，每个 fragment 都记着 `focus_moved`（原因、从哪个 tab 到哪个 tab），查 trace 时看这里。

实际输入平台是另一项运行事实：setup 从 Chrome 内部版本页测量 `macos` / `windows` / `linux`，保存为 run 指纹的 `input_platform`，并通过独立 `browser.platform` 观测告诉 Agent。browser 块的 `expected_platform` 是可选断言，默认 `null` 仍要求测量成功，不模拟平台。回放源字段缺失或非法在 prepare 拒绝；当前浏览器启动后实测平台不同也拒绝。旧归档可离线读，但不得填入猜测平台或改写旧文件来恢复执行。云 CDP 若禁止内部页，当前实现明确报测量失败，不回退到 Agent 宿主平台；本轮没有云服务实测。

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
uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml \
  --task "录制时的任务文本" --start-url <录制时的 start_url> \
  --replay-archive traces/recorded/<run>
```

任务文本和 `start_url` 在源 run 的 `config.json` 里（`task`、`task_setup.context.start_url`）。`replay_archive` 可以写相对路径，按启动目录解析：`AgentRuntime.execute` 在记进 trace、交给浏览器之前把它变成绝对路径，`--replay-archive` 和 Python 里自己组的 `env_context` 都一样，回放 trace 里记的是绝对路径。与录制配置相比，回放配置的 capability 块改了两处：

```yaml
replayability: { replayability_id: pinned, random_seed: 7, base_time: from_archive }
network_source:
  source_id: archive
  shift_dates: none                    # all：响应日期整体平移；独立于 base_time
  matcher: { site_rules: configs/replay_rules }
  miss_policy: { miss_policy_id: strict }
```

`trace_writer.archive` 里用 `browser.replay_log` 代替 `browser.network`，其余归档器不变。`browser.replay_log` 记回放日志，回放没有它就被拒；`browser.network` 只录线上流量，回放没有自己的线上流量（送出的每个字节都已经在源归档里），所以它在 `archive` 来源下被拒，报错点名 `browser.replay_log`。反过来，`live` 来源绑定 `browser.replay_log` 也被拒，因为没有回放可记。这几条都在 `prepare()` 里报，`--dry-run` 也一样。

- `prepare()` 打开源 run（只读已提交的部分），拿它的环境指纹比较本次配置（setup 启动 Chrome 之前再比一次）：档位和参数（`base_time` 除外）、shim、viewport、启动开关摘要。有一项不符就拒绝运行，每条点名两边的值。启动开关摘要只覆盖 provider（`local_chrome`）拼出的启动参数，调试端口和 profile 目录换成占位符，网络路由开关两边都去掉，可执行文件路径和 Chrome 版本都不在里面。`headless: true` 会加上 `--headless=new`，所以只改 `headless` 也会不符；要看着浏览器回放，录制和回放都得写 `headless: false`，也就是重新录制。Chrome 版本不同、档位没固定 UA 而浏览器 UA 不同、脱敏策略不同只警告，写进回放 run 的 `env/run.browser.json` 的 `network_source.warnings`。
- `isolation: required`（默认）给 `local_chrome` 加 `--host-resolver-rules=MAP * ~NOTFOUND` 和 `--no-proxy-server`（没有例外，`localhost` 也断：对着本机网站录的 run 照样从归档回放，回放时那个服务开着也连不上），并拒绝会绕过它们的旋钮：`proxy_server`、`proxy_bypass_list`、`extra_args` 里的路由开关、不是 `about:blank` 的 `startup_url`。写 `isolation: none_acknowledged` 时只靠 Fetch 拦截，preconnect 和 WebSocket 会连到真实网络。
- `base_time` 与 `shift_dates` 是独立变量，`from_archive/none`、`run_start/all`、`run_start/none`、`from_archive/all` 都可表达。`from_archive` 需要归档来源及可用源 T0；`all` 每个响应用同一偏移调整 Date、Expires、Last-Modified 和 cookie 绝对过期属性，偏移按服务时刻与录制 Date（缺失时用 WARC-Date）计算。缺 Date 时两种模式都会补锚点；正文中的日期、定时器与浏览器内部时间不因此一起平移。
- `matcher.levels` 是允许集合，固定按 exact → normalized → fuzzy 尝试；规则只能收窄，Range 同样遵守。通用规范化只整理格式，不猜时间戳、产品编号、尺寸或随机参数的内容语义。每条规则都必须显式写两项策略：`exhaustion: repeat_last | miss`（序列用尽之后：重复最后一条**可用**的响应，或记 `sequence_exhausted`）与 `unavailable: miss | serve_identical`（录下了却没有正文的记录怎么答，下一条）；最强候选耗尽不向弱级别换资源。规则可按 host、path/URL、method、port 和明确优先级选择；冲突或少写一项策略具名拒绝，框架不给默认值。发布的 `baseline.yaml` 写的是 `repeat_last` 与 `serve_identical`。规则文件格式见[README](../configs/replay_rules/README.md)。
- 没有正文的记录（页面自己取消的、run 结束时还在途的、浏览器从内存缓存复用的、recorder 读 body 超时的）照样占它在序列里的位置。`unavailable: miss` 交给 miss 策略（子资源回 404）。`serve_identical` 在下面几条都成立时用同一序列里一条可用记录**代答**：源里不是真实的网络失败（`network_failed`，例如被跨站读取保护拦下；这一条写死，不是开关）、序列里至少有一条可用记录、所有可用记录状态相同且正文摘要相同、被消耗那条若录下了状态码也与之相同（录下的 500 不会被 200 代答）。代答记录可以是录得更晚的那条。对页面取消的请求代答是有意的偏离：源页面没收到这次成功，回放页面收到了，日志与度量都单列。
- 规则在准备回放时读取并冻结；正文、逐文件摘要、集合摘要与来源随环境 run 记录提交。修改原目录不会改变当前 run，历史规则可从 RunTrace 读回并核对。改规则时同时检查错误命中反例，不能只看命中率。
- JSONP 回调名：页面按请求里的回调名（`?callback=NAME`）取回一段调用 `NAME(数据)` 的脚本，名字是页面自己起的（jQuery 是 `jQuery<随机数字>_<计数>`），回放时常与录制时不同。回放送出这样的应答时，把开头的函数名换成这次请求的回调名，否则页面拿不到数据。只在能确定是这种包装时才换：请求是脚本、XHR 或 fetch，应答以 `NAME(`、`typeof NAME === 'function' && NAME(`、`NAME && NAME(` 开头（前面可以有空白、注释、`;`，可以包在 `try {` 里），录下的请求里恰好一个参数的值是 `NAME`，这次请求的这个参数是一个合法的名字。其余情况原样送出。改写后 `Content-Length` 按新的字节算，`Content-MD5`、`Digest` 这类摘要头去掉。所以名字带随机部分的回调参数（豆瓣广告的 `callback`、乐视登录的 `jsonp`）可以放心写进站点规则的 `ignore_query`。设计见 [architecture_design.md](architecture_design.md) §4.14 "JSONP 回调名"。
- `strict` 处理归档里没有的请求：主 frame 的文档 `Fetch.failRequest` 并标 `diverged`；子 frame 的文档同样失败；beacon、ping、CSP 报告、内置的分析类主机、`extra_benign_hosts` 和站点规则 `synthesize_204` 命中的回 204；其余子资源回 404。回放编出来的应答都带 `x-replay-miss: 1` 头。归档里没有的 CORS 预检合成 204。

回放 run 的 `env/` 里没有 WARC，每步一个 `env/steps/NNN/browser.replay_log/replay.jsonl.gz`（gzip 压缩的逐行 JSON，约为不压缩时的十分之一；手看用 `gzip -dc <文件> | head`，程序经下面的读法读，它会解压；2026-10-23 之前写的不压缩的 `replay.jsonl` 不再读，读到会报错，重跑回放）；`env/run.browser.json` 多一个 `network_source` 块（`source_run_id`、`source_fingerprint_sha256`、`site_rules_sha256`、`site_rules_snapshot`、matcher、`miss_policy_id`、`shift_dates`、`isolation`、`warnings`）。回放 run 不能再当回放或分叉的源，要回放就用原来的录制。读回放日志：

```python
from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.replay.replay_log import diverged_steps, iter_replay_log
from spectragent.core.capabilities.browser.replay.rule_snapshot import recorded_site_rules

reader = open_archive("traces/<replay-run>")
rules = recorded_site_rules(reader)            # 校验并恢复实际规则；不读原目录
print(diverged_steps(reader))                   # 有主 frame 文档 miss 的步
for step, entry in iter_replay_log(reader):     # 含收尾批次（step 为 None）
    if entry.kind == "miss":
        print(step, entry.reason, entry.request_class, entry.frame, entry.url)
```

每行是一个 `ReplayLogEntry`。`kind` 是 `hit`、`exhausted`（序列用尽后，按 `repeat_last` 重复了最后一条可用的响应）、`substituted`（被消耗的记录没有正文，按 `serve_identical` 用同一序列一条正文相同的记录代答）、`synthetic`（合成的预检）、`miss` 或 `summary`（仅显式 `log_mode: summary` 时同一次收集里相同语义条目超过 `log_repeat_limit` 后折叠，保留 `count`、`folded_kind`、`first_captured_at`、`last_captured_at`；默认 `full` 保留逐次事件）。决定带 `rule_id`、`exhaustion_policy` 与 `unavailable_policy`；命中带 `record_id`、`run_id` 和 `level`，改写了 JSONP 回调名的还带 `callback_rewrite`（`parameter` 是哪个参数，`recorded` 是录下的名字，`requested` 是这次请求要的名字）。`substituted` 的 `record_id` 是被消耗的那条（没有正文），`served_record_id` 是实际送出的那条，`source_outcome` 是被消耗那条的源结局（`page_cancelled_before_response`、`page_cancelled_during_body`、`in_flight_at_run_end`、`memory_cache`、`recorder_timeout`、`network_failed`、`unknown`），`status` 是送出的状态。miss 带 `reason`（`no_match`、`sequence_exhausted`、`match_level_disallowed`、`too_large`、`body_unavailable`、`archive_unreadable`、`range_uncovered`、`websocket`、`unservable`、`fulfil_failed`）、`request_class`（`document`、`benign`、`subresource`）、`frame`（`main`、`sub`）、`answer` 和 `status`、`match_key`（超过 512 字符写成 `sha256:<hex>`）与 `body_key`（body 的规范参数，原样保存；只有规范化自己封顶时，即超过 4096 字符或 body 解析不了，才是一个摘要参数），以及最近的 3 个候选 `candidates`；`body_unavailable` 另带 `record_id` 与 `source_outcome`，`range_uncovered` 另带 `requested_range`（页面这次请求的 `Range` 头原文：一次缺口和它引起的几百次重试由此分得开），`repeated: true` 标出这次 miss 来自 `repeat_last` 在序列用尽之后的重复（被重复的那条没有可送出的正文），第一次落到这条记录的 miss 不带它。`fulfil_failed` 是归档里有、也送了，但 Chrome 没收那条 fulfil（`fulfil_error` 是它的报错）：页面看到的是失败，所以它是 miss，主 frame 的文档还标 `diverged`；`record_id` 是被拒的那条记录。

统计时用 `iter_decisions(reader)` 的第三项代表次数，不能把摘要行数当请求数；`entry.occurrence_times_complete` 表示是否保留逐次时间。

## 分叉

`configs/flywheel/branch_ui_tars_archive.yaml` 把录下的 run 放回第 t 步之前的状态，让 teacher（这份配置的 `llm`）重新决定第 t 步。任务来自 `task_loader: trace_branches`：`data_dir` 下每个通过筛选的源 run、每个选中的步展开 `branches_per_step` 个任务，任务 id 是 `<source_run_id>/s<t>/b<i>`。设计见 [architecture_design.md](architecture_design.md) §4.1 `BranchLoop`、§4.10 `trace_branches` 和 §4.14 "恢复：重定位与校验"。`trace_branches` 没有评测加载器，所以传 `--no-eval`；不传而配了 `evaluator:` 时，开跑前就会被拒。

`branch` loop 需要 loop 输入 `checkpoint`，即源 run 目录和要分叉的步。只有 `trace_branches` 会把它放进每个任务的上下文，同时放进去的还有源 run 的 `start_url` 和 `replay_archive`（源 run 目录）。`spectragent run` 没有能提供 `checkpoint` 的参数，用它跑分叉配置（包括原路径回放配置）时，`prepare()` 做完槽位校验就报 `IncompatibleLoopInputError`，加 `--dry-run` 也一样。

```bash
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --list     # 只列任务 id
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
```

```yaml
task_loader:
  - benchmark_name: trace_branches
    data_dir: traces/recorded          # 源 run 所在的目录
    step_selector: { selector_id: all }
    branches_per_step: 3               # 每个选中的步分 b0、b1、b2，各自采样
    agent_outcome: any                 # done | max_steps | error | timeout | vetoed：源 run 的 agent 怎么结束
    on_unbranchable: raise             # skip：跳过并记下原因
    exclude_unloaded_checkpoints: true # 页面还没加载完就采集的检查点不分叉，记下原因
```

源 run 由 `sources` 选出。它是相对 `data_dir` 的 glob 列表，默认 `["*"]`，即 `data_dir` 的直接子目录；匹配到的目录里没有 `manifest.json` 就不算 run，直接忽略。一个都匹配不到时（`data_dir` 不存在也一样），加载器只记一条 warning（`trace_branches: no recorded run under ... matches ...; no branch tasks`），展开出 0 个任务，不报错，所以开跑前先用 `--list` 确认任务 id。`data_dir` 不要指向 `traces/`：发布的回放配置也写到那里，不能分叉的回放 run 会混进源里；`traces/recorded` 这样的子目录本身没有 `manifest.json`，默认的 `*` 匹配到它也只会忽略，它下面的录制一个都找不到。只分叉一个 run 时，把 `sources` 写成 `["<那个 run 的目录名>"]`，或者给 `spectragent bench` 传 `--task-id <source_run_id>/s<t>/b<i>`。

步号从 0 开始。加载器从第 0 步往后扫描源 run 已提交的步，遇到第一个缺失的步号、或超出 trace 完整到的那一步（manifest 的 `complete_through`）就停；扫到的步里 `origin: policy`、`error_phase` 不是 `observe` 的是候选步。trace 从没关闭（manifest 还是 `writing`：录制还在跑，或进程死在收尾之前）的源一个候选步都没有，跳过并记原因。这一条是硬条件，关不掉：一个在第 t 步收证据失败而停下的录制（`incomplete`），仍然贡献第 t 步之前的每个检查点；只是收尾失败的录制一个检查点也不少。`step_selector` 决定在哪些步分叉：

| `step_selector` | 选哪些步 |
|---|---|
| `all` | 每个候选步 |
| `every_k`（`k`、`offset`） | 步号 t 满足 `t >= offset` 且 `(t - offset) % k == 0` 的候选步。按步号数，不按在候选里的位置：不是候选的步号直接跳过，不记原因。例如 k=2、offset=1 时，在 1、3、5、7……里选是候选的那些 |
| `explicit`（`steps: [2, 5]`） | 写了的步；某个源里不是候选的步跳过并记原因 |
| `last` | 每个源最后一个候选步 |

`agent_outcome`（读源 run 的 `result.json`，默认 `any`）和 `require_eval`（`{evaluator_id, label: 1, slug: null}`）筛选源。`exclude_runs` 也是筛选：它指向一个 `run-exclusions-1` 文件（`{"format": "run-exclusions-1", "runs": [{"run_id", "reason", "note"?}]}`，`~` 展开、相对启动目录），它列出的 run id 不当源，加载器的跳过记录里写着文件给的原因（以 `filtered:` 开头）。这份文件通常由 `python -m lookahead health` 写出（页面一直没渲染、录制时内容请求就失败了、最终回答说任务失败的录制），见 `lookahead/README.md`。有些源不能分叉：读不回来的（比当前 trace 格式旧的也读不回来，只能重录）、本身是分叉或回放 run 的、没有 `browser.network` 归档或环境指纹的、拿不到 setup context 的、与别的源共用 `run_id` 的。`on_unbranchable: raise` 时在开跑前一次列出全部，`skip` 时跳过并记原因。`on_unbranchable` 只管整个源：一个步不是候选，不算不可分叉；一个候选步都没有的源也只是跳过并记原因。

`exclude_unloaded_checkpoints`（默认 `true`，发布的两份分叉配置都写出来）在选步之后再排一遍：录制网络慢时，teacher 有时在主文档还没加载完的页面上做决定，而离线回放里同一步早已加载完，这样的步不当检查点。判断只读录下的 `capture_state`：每次采集都记下那一刻主文档是否已经到了它的 `load` 事件，还没到就算没加载完（reader 的 `capture_state.settle_state.loaded` 为 `false`），不论落定等待是在上限（`settle_max_wait_s`，5 秒）处结束还是早就停了；只是 DOM 一直在变（播放器、轮播、行情）不算。被排除的步记在加载器的跳过记录里，键 `<源路径>#s<t>`，原因写着 `captured before its initial load finished` 和等了多久；`--list` 列出的任务里没有它们。它在选步之后起作用：`last` 选中的最后一步没加载完，这个源就不分叉，不会往前补一步；任务变体落在这样的步上也跳过并记原因。没录 `capture_state` 的源（没协商它，或 agent 写了 `show_capture_state: false`）没有证据，检查点照常保留。写 `false` 不查。一个从没加载完的文档只在第一步花掉 5 秒上限，之后同一文档的步不再等它的加载，但每一步都照实记作没加载完；两步之间加载完了，下一步就记作已加载（[architecture_design.md](architecture_design.md) §4.4 “初次加载是否完成”）。

Loop 一侧：

```yaml
agent_loop:
  loop_id: branch
  max_steps: 30
  max_actions_per_step: 4          # 必须与源 run 相同
  continue_steps: 0                # 0：只决定第 t 步；N：之后再走 N 个 ReAct 步
  history_source: source          # source 精确原对话；restored 使用本次恢复事实
  restorer:
    restorer_id: prefix_replay
    verify: strict                 # warn：记下不通过的检查，继续
    screenshot_max_distance: 5     # 两张截图 64 位 dHash 的汉明距离上限
    pace: none                     # recorded_llm_latency：执行动作前等源 run 当时等模型的时长；recorded_timeline：按源 run 的时间线走
    media: align                   # none：不对齐媒体（对照组），媒体状态照样比较
    media_time_tolerance_s: 1.0    # 媒体播放位置与录下的差多少秒以内算通过
    media_seek_timeout_s: 2.0      # 一次对齐跳转（及随后的播放）必须完成的上限
    media_end_lead_s: 0.5          # 录下时已播完的视频从结尾前这么多秒播到结束；必须短于上一项
```

`pace` 决定恢复步之间等多久。源 run 每一步都等过模型（几秒到几十秒），页面自己的计时器（轮播、自动播放、倒计时、懒加载、延时弹窗）在这段时间里接着走；恢复步一个接一个执行，采集恢复观测时页面就落后于录制。`none` 不等；`recorded_llm_latency` 在执行第 k 步动作之前等源 run 第 k 步等模型的时长，观测、动作与记录本身的耗时不补，越往后漂得越远；`recorded_timeline` 从第 0 步的观测起复现源 run 的时间线：以两边第 0 步观测的采集时刻（这一步 env fragment 里最早的 `captured_at`）为锚点，每个恢复观测（含检查点）提前"本次恢复上一次观测的耗时"开始，好让它在源的采集时刻采集，每一步的第一个动作在源这一步第一个动作开始的时刻执行（`StepRecord.action_spans`），同一步后面的动作紧接着执行。已经落后就立刻做，从不追。起始页加载多久是网速，不复现。源 run 要执行动作的步没量过动作起点（`action_spans` 为 `null`，例如录制时 `recording.metrics` 关着）时，分叉在任何 setup 之前以 `IncompatibleCheckpointError` 点名这些步，不会悄悄换成别的 pace。等待不写进 trace。

`branch` loop 默认 `terminal_observation: true`。capability 的 `retarget_tolerance_px`（默认 2.0 CSS px）决定录下的元素在恢复出的页面里偏移多少还能被认出。

每张观测截图都带 `media` 元数据：截图前后各读一次页面上每个 `<video>` / `<audio>` 的精确播放位置、是否暂停、总时长、所在 frame、文档顺序与位置（读数上限是 capability 的 `media_state_timeout_s`，默认 1 s；读不到记为 unknown，从不让截图失败，录制时不改动页面）。视频在源 run 里按真实时间播放，模型思考时也在播；原路径回放不等模型，同一步的画面会停在别的位置，截图距离却可能在阈值之内。所以恢复校验比较媒体状态，`media: align`（默认，发布的两份分叉配置显式写出）在每一个被恢复的观测（含检查点）的采集里、页面落定之后、截图之前，把对应上的元素对齐到录下的状态：录下暂停的暂停并跳到精确位置；录下在播的跳转、播放，随即截图（这次采集的截图排在 DOM 之前，否则视频在 DOM 采集的几秒里接着播）；录下已播完的跳到结尾前 `media_end_lead_s` 再播到自然结束。跳转要在 `media_seek_timeout_s` 之内完成（`seeked`、不再 seeking、有可显示的帧）并停在目标上，否则跳回对齐之前的位置与播放状态、判这一步 `media_mismatch`。检查点之后的策略步和终止观测不对齐。`pace` 的两种等待都只能近似播放位置，不是媒体恢复的手段。

一个分叉 run 按这个顺序执行：

1. 开跑前对照源 trace：`agent_id`、闭集的 agent 旋钮（`prompt_template`、`coordinate_space`……）、memory、观测与动作的协商结果都要相同，源 loop 必须是 `react`，源 run 本身不能是分叉 run，任务变体（下面）不能配 `history_source: source`。不符就抛 `IncompatibleCheckpointError`，点名两边的值。teacher 的模型和采样参数可以与源 run 不同。
2. `history_source: source` 从源 trace 精确重建第 t 步之前的上下文；仅主决策回复及其解析重试经过 Agent 解析器，helper 不参与。`restored` 在恢复每步后通过 Agent 的公开合同构建实际历史，不重新生成源回复。
3. 浏览器回放源 run 的归档，`prefix_replay` 逐步重新执行录下的动作（元素 index 按位置和签名重新找到，tab id 按顺序和 URL 映射），并校验每一步。恢复出的步记成 `origin: restored`，带这一步的 `restoration` 检查。第 t 步（检查点）的观测也要校验，结果记在第 t 步的策略步上；strict 下它不通过时 teacher 不会被调用。
4. 配了 `prompt_contribution` hook 时，经 Agent 的公开异步提示入口生成附加贡献；第 t 步和之后的 `continue_steps` 步由 teacher 决定（`origin: policy`），最后再观测一次（`origin: terminal`）。

`verify: strict` 下第一个不通过的检查就结束 run：记一条 `error_phase: restore` 的恢复步，结果是 `agent_done=error`、`env_status=failed`，`error` 点名 `RestorationDivergedError`。`warn` 只把检查结果写进那一步，继续往下走。每个分叉是独立的任务，重新组装、启动新的浏览器和新的 profile，分叉之间不共享状态。一个分叉的策略步数是 `1 + continue_steps`（不超过 `max_steps`），每个策略步至少调用一次模型；`AgentMetrics.steps` 只数策略步。

分叉 run 写到 `traces/branches/<source_run_id>-s<t>-b<i>_<8 位随机串>/`，`config.json` 的 `branch` 记着源 run、检查点和分支编号。分叉与原路径回放配置的 `trace_writer.archive` 和回放配置一样用 `browser.replay_log`，每个分叉 run 的回放日志在它自己的 `env/steps/NNN/browser.replay_log/replay.jsonl.gz`。读校验结果：

```python
for record in RunTrace(Path("traces/branches/<run>")).step_records:
    check = record.restoration
    print(record.step, record.origin, check and check.verdict, check and check.reasons)
```

### teacher 采样

同一个检查点的几个分叉输入完全相同：同一个源、同一个检查点、同一份任务和环境。分支编号只记进 trace，teacher 看不到，所以分叉之间的差别全靠采样。发布的分叉配置因此把 teacher 的 `temperature` 设成 0.8（录制配置没写，用的是 openai 的默认值 0.2），每步分 3 个（`branches_per_step: 3`）。原路径回放配置不变，用录制时的模型和默认温度：分叉不要求 teacher 与源 run 不同。温度原样记在分叉 run 的 `config.json`（`resolved.agent[0].llm.temperature`）。分得够不够散，看度量报告的重复分叉率：同一个检查点上执行了相同动作的分叉占比。

`history_source: source` 默认严格重建源对话，使用源观测、原贡献及其位置，并逐次验证 Agent 主决策 prompt。`history_source: restored` 改用本次恢复的观测、实际 retarget 后动作及完整结果，通过 Agent 的公开历史方法构建消息。两者与恢复器的 `verify: strict | warn` 独立；第一策略步始终使用恢复后的 live 检查点观测，不为验证再拍一次。恢复步骤的旧回复位于 `source_history`，不能当成本次新 thought；`actions` / `action_results` 则描述本次恢复执行。

提示干预通过显式 `prompt_contribution` hook 配置，函数使用可导入的稳定引用，签名为 `def contribution(*, request: PromptRequest)` 或同签名 `async def`，返回字符串或 `PromptContribution`。Agent 决定贡献放在 system 或当前轮次，Hook 不写 memory。

```yaml
hook:
  - hook_id: prompt_contribution
    function: my_experiment.prompts:contribution
    parameters: {instruction: "Consider another useful next action."}
    scope: first_policy_step      # 默认：仅分叉首个策略步；each_policy_step 每个策略步
    helper_llms: []               # 可配置一个独立的 LLM，函数从 request.helper 调用
```

函数/参数/范围及 helper 配置是实验身份；正文按统一记录计划决定是否保留。完整轨迹保存实际 `prompt_contributions`，重建历史只读这些事实，不再次执行函数或 helper。解析重试复用该步已经生成的贡献。helper 调用按 `purpose=prompt_helper` 记录，用量计入实验合计，不能作为 Agent 主回复、思考或 teacher thought。函数失败和取消属于执行失败。

要注意的：

- 高温下坐标可能离开配置的网格，与模型有关。实测过一次（DMX 上的 qwen3.8-flash，`coordinate_space: normalized_1000`，温度 0.8；不是发布的 gpt-4o）：两次 UI-TARS 回复里有一次写的是截图像素坐标，不是 0 到 1000 网格上的点，这样的点击会落到别处。换模型或调高温度之后，先抽几个分叉 run，对照第 t 步的截图看回复（`StepRecord.llm_output`）里的点，再放量。
- 在指令下写出的 thought 可能提到这条指令或源 run 的回复。导出用 `thought_source: teacher` 时会原样带上（导出冻结，学生侧下一期再定）。
- 每个分支给不同的提示还没有做：等 live 数据显示重复的分叉多了再做。

### 任务变体：同一页面，不同需求

前瞻观测的训练数据要同一屏配不同的需求。任务变体让一个分叉在源 run 的第 t 步检查点上，换一份任务重新决策：环境照样恢复到第 t 步之前（同一个回放归档、同一个 setup context），只有任务文本换成变体的。变体写在一个 JSON 文件里，通常由 LLM 看着第 t 步的截图生成（生成不在框架里）：

```json
{"format": "task-variants-1",
 "variants": [
   {"source_run_id": "online_mind2web-task-001_1a2b3c4d",
    "step": 7,
    "variant_id": "v1",
    "task_text": "a different task that starts from this same screen",
    "note": "optional: how it was generated"}]}
```

- `source_run_id` 是源 run 的 run id，也就是普通分叉任务 id 的第一段（`--list` 列出的 `<source_run_id>/s<t>/b<i>` 里那段）。
- `step` 是检查点步号，必须是这个源的候选步（规则同上面的选步），而且是 `step_selector` 选中的；写 `step_selector: { selector_id: all }`，文件就完全决定检查点。文件没提到的步不分叉。
- `variant_id` 只能用字母、数字、`.`、`_`、`-`，同一个源、同一步里不能重复；它进任务 id。
- `note` 可省，原样记进 trace。

```yaml
agent_loop:
  loop_id: branch
  history_source: restored         # 必须：source 会在开跑前被拒
  # 其余同上面的分叉配置
task_loader:
  - benchmark_name: trace_branches
    data_dir: traces/recorded
    step_selector: { selector_id: all }
    task_variants: variants/task_variants.json   # 相对启动目录，~ 展开
    branches_per_step: 1           # 每条变体的分叉数
```

任务 id 是 `<source_run_id>/s<t>/v<variant_id>/b<i>`，同一条变体的几个分叉 `branch_group_id` 相同（`<source_run_id>/s<t>/v<variant_id>`），同一检查点的不同变体不算同一组。开跑前先 `--list` 看任务 id：

```bash
uv run spectragent bench --config my_variant_branch.yaml --list
```

`history_source: source` 逐次精确重放源 run 的 prompt，而源 prompt 里写着源任务，所以与任务变体不能同时用：`prepare()` 报 `IncompatibleCheckpointError`，消息点名 `task_variants` 和 `history_source`，`spectragent bench` 在 pre-flight 就退出（退出码 2），不启动浏览器、不写 trace。改成 `history_source: restored`。

文件本身不对（读不出、不是 JSON、`format` 不是 `task-variants-1`、缺字段或多字段、`step` 不是整数、`variant_id` 含别的字符或为空）时，第一次列任务就报错，点名文件与问题。单条变体用不了时跳过，原因记在加载器的 `skipped` 里，键是 `<source_run_id>/s<t>/v<variant_id>`：同一 `(source_run_id, step, variant_id)` 写了不止一次（每份都跳过）、`task_text` 为空、没有这个 run id 的源、源被筛掉或不可分叉、这一步不是候选或没被选中。

分叉 run 的 `config.json` 里，`task` 是变体文本，`branch.task_variant` 记着 `variant_id`、`task_text`、`source_task_text`（源任务）和 `note`。

评测与导出怎么对待变体：

- 变体没有 benchmark 参考答案，源任务的参考描述的是另一份任务。`spectragent eval` 用 `exact_match`、`browsecomp`、`lexbench_browser` 这类按参考评分的 evaluator 评它时，它记为"无法判定"（`task variant '<id>': no reference for this task`），不算判错，不进任务正确率的分母，trace 里不写判定；退出码因此是 1。`online_mind2web`（WebJudge）不读参考，照样评，评的是变体文本，判定的 `benchmark_details.task_variant` 记着变体与源任务。
- 导出切分仍按源任务（同一个源的变体与原分叉落在同一份切分里）。`decontamination` 的 `site_instruction` 同时比对变体文本和源任务文本：源任务是将来的评测任务时，从它的录制切出来的变体也被排除，原因写 `its source task's instruction (the run is a task variant)`。

要注意的：restored 历史里每个恢复步都带着源 run 当时的回复（含 thought），写成 `source_material.recorded_reply`。那些 thought 是为源任务想的，源任务的文字只会经它们引用而出现在 teacher 的提示里；提示也不说明这些源材料服务的是另一份任务。第 t 步本身的源回复不进提示。抽几个变体分叉看第一次策略调用的 prompt（`RunTrace.llm_messages(t)`），再决定要不要放量。

### 原路径回放

`configs/flywheel/same_path_ui_tars_archive.yaml` 是同一个分叉 sweep 的一种设置：`step_selector: last`、`continue_steps: 0`、`verify: warn`、`branches_per_step: 2`、`agent_outcome: any`、`on_unbranchable: skip`、`exclude_unloaded_checkpoints: true`（最后一步没加载完的源不回放，跳过记录里写着原因）。每个任务把一个录制恢复到它最后一个候选步，之前每一步都重放并校验；teacher 只在最后一步决定一次，每个任务一次模型调用；同一个源跑两遍，用来量回放确定性。度量报告的"原路径回放保真"就来自它。发布配置的 `data_dir` 已经是 `traces/recorded`，run 写到 `traces/same_path`：

```bash
uv run spectragent bench --config configs/flywheel/same_path_ui_tars_archive.yaml --no-eval
```

## 导出 SFT 数据

这一期交付的是 teacher 轨迹（录制、回放、分叉）。学生一侧是暂定的，挪到下一期再定；导出代码冻结，不加功能，下面写的是它冻结时的用法（[architecture_design.md](architecture_design.md) §4.15）。唯一的例外是用户 2026-10-25 裁决的等待动作后处理（`wait_actions`，见下面"去掉截断采集上的等待"）。

`spectragent export` 只读已提交的 trace，不起浏览器，也不调学生或 teacher 的模型（`wait_actions: drop_when_capped` 时调一个改写 thought 的辅助模型，见下）。学生是一个普通的 af agent：`configs/flywheel/ui_tars_student.yaml`（UITarsAgent，`coordinate_space: normalized_1000`）只被组装，从不运行；每个样本由它自己的 prompt 构建与 memory 渲染，目标轮由它的 `format_react_output` 写出，所以训练格式就是它服务时的格式（设计见 [architecture_design.md](architecture_design.md) §4.15）。

```bash
uv run spectragent export --config configs/flywheel/export_sft.yaml
```

导出 YAML 有一个 `exporter:` 块，可以再加 `eval_loader:` / `task_loader:` 块（见下面的去污染），没有 agent 和 environment。v1 唯一的导出器是 `exporter_id: openai_messages`，`sample: per_step` 与 `image_store: files` 也只有这一个取值。发布的 `export_sft.yaml` 不按评测结果筛（`filters.require_eval: null`），也不去污染（`decontamination.against: []`），所以直接用在 online_mind2web 的录制上就能导出；teacher 失败的轨迹也会进数据集，每个样本带着 `labels.step_status` 和判定标签，训练时可以再筛。开跑前按自己的数据改这几处：

- `sources_root` 和 `sources`：发布值是 `sources_root: traces`、`sources: ["recorded/*"]`。`sources` 是相对 `sources_root` 的 glob，按路径排序；`sources_root` 写相对路径时按工作目录解析。要导出分叉 run 就加上它们的目录，例如 `["recorded/*", "branches/*"]`，原路径回放的 run 在 `same_path/*`。分叉 run（含原路径回放的 run）的源 run 也必须在本次读到的源里，否则被筛掉，原因记为 `branch source '<id>' not among the sources`。列进来的录制 run 自己也会产出样本，没有只导出分叉的开关。
- `filters`：`agent_outcome`（默认 `any`；读源 run 自己的 `result.json`）；`require_eval`（默认和发布值都是 `null`，不读判定；要只留判分通过的 run 才写 `{evaluator_id, label: 1, slug: null}`，这要求录制时评测过）；`exclude_restoration_diverged: true`，有一步的恢复检查判为 `warn` 或 `diverged` 的 run 整个排除；`max_document_misses: 0`，回放日志里主 frame 文档 miss 超过这个数的 run 排除（`unlimited` 不限，没有回放日志的 run 算 0）；`exclude_sites`，主机名后缀匹配，度量报告会给出这份名单。对分叉 run，`agent_outcome` 看它自己的结局，`require_eval` 看它的源 run 的判定。不管筛选怎么写，trace 从没关闭的源都跳过，一个源只导出它的 trace 完整到的那些步。用 `verify: warn` 跑的原路径回放 run 只要有一步的检查没通过，就整个被 `exclude_restoration_diverged` 排除。`exclude_unloaded_steps: true`（默认，发布值也写出来）按步筛：录下的 `capture_state` 说这一步采集时主文档还没加载完（reader 的 `capture_state.settle_state.loaded` 为 `false`，与分叉的 `exclude_unloaded_checkpoints` 同一条规则），这一步不产生样本，但照样留在之后样本的上下文里，段不断；每个被筛掉的步记在 manifest 的 `steps.filtered`（`<源路径>#s<t>` → 原因）。没录 `capture_state` 的步照常导出。
- `decontamination`：`against` 列出学生将来评测用的 benchmark（`[{benchmark: <名字>, subset: <子集>}]`），只查列出来的这些，从不从评测器或判定推断。默认 `[]`：不去污染，manifest 记 `decontaminated: false`；`null` 不是列表，组装时报错。把录制所用的 benchmark 自己列进去会把这些录制全部排除（`ExportEmptyError`）。`match` 取 `task_id`、`site_instruction` 或 `either`。去污染和参考答案泄漏扫描经导出 YAML 自己的 `eval_loader:` / `task_loader:` 块读基准数据，没配的 benchmark 用注册加载器的默认值。
- `split`：`by: site`（源任务 `start_url` 的主机）或 `by: task`，从不按步切，同一组分叉总在同一个切分里。
- `translator`：`identity` 原样使用 teacher 执行过的动作，UI-TARS 录下的本来就是坐标动作。`index_to_coordinate` 把带元素 index 的 `browser.click`、`browser.type` 换成元素可见部分上的坐标动作，其余带 index 的动作不可译（`no_counterpart`），不带 index 的原样保留。它的 `occlusion: check` 要确认换算出的点没有被别的元素盖住，证据按优先级是 cleaner `skyvern_hit_test` 录下的命中测试、`browser.dom_raw` 的原始 DOM、BrowserUse cleaner 录下的 paint order（只认得出部分遮挡）。三样都没有时抛 `OcclusionEvidenceMissingError`；写 `occlusion_unknown: untranslatable` 则把这一步当作不可译。`occlusion: none` 不查遮挡（原来写作 `"off"`）。`untranslatable` 决定不可译的步怎么处理：`drop_step`（结束当前段）、`truncate`（结束这条轨迹）、`keep_as_text`、`fail`。
- `thought_source: teacher | none` 与 `thought_index_refs: drop | keep` 决定 thought 从哪来、提到元素 index 的 thought 是否丢掉。
- `wait_actions: keep | drop_when_capped`：发布值与默认值都是 `keep`，录下的等待动作原样导出。`drop_when_capped` 去掉 teacher 在被上限截断的采集上做的等待，见下面"去掉截断采集上的等待"。
- `length: { max_tokens: 65536 }`：学生每轮都保留截图，30 步的整段在 1280×720 上光图像约 36k token。有记录超出预算时，导出失败并点名这条记录，从不截断；训练器的上下文更短时，换一个 `max_steps` 更小的学生。
- `output_dir`：发布值 `datasets/sft_v1`，相对工作目录；已存在就拒绝。

学生对录制有要求。学生协商出的观测 fragment 必须是源 run 录下的子集，例如学生要 DOM 而录制是纯视觉时报 `IncompatibleFragmentError`；截图管线（viewport、格式、DPR）也要与录制一致，否则报 `StudentCaptureMismatchError`。用纯视觉 agent、`max_actions_per_step: 4` 录制不会丢样本；一步的动作数超过学生的 `max_actions_per_step` 时，这一步的不可译原因是 `exceeds_action_budget`。结束步也要看 agent 有没有说任务完成了没有：BrowserUse、Skyvern 录下的 `done` 通常带着这个结论（`success: true` 或 `false`），UI-TARS 学生的 `finished()` 写不出任何结论。说完成了的（`true`）照常导出，写成 `finished(...)`，`samples.jsonl` 里这个样本的 `render.claim_not_written` 是 `true`；说没完成的（`false`）不可译，原因是 `unrepresentable`；UI-TARS 自己录下的结束步没有结论，照常导出（[architecture_design.md](architecture_design.md) §4.1 "agent 的任务结论"、§4.15 "round-trip"）。

样本是至少执行了一个动作、没有出错的策略步。动作失败的步照样导出，标 `labels.step_status: action_failed`。分叉 run 导出它的策略步，上下文是源 run 的前缀；同一组里检查点步动作相同的样本标 `duplicate_of`，不删。产物先写进 `<output_dir>.partial/`，图片数、坐标范围、参考答案泄漏、凭证形状和长度的检查全部通过才改名：

| 文件 | 内容 |
|---|---|
| `train.jsonl`、`validation.jsonl` | 每行 `{"id", "messages"}`，assistant 轮带 `weight`（目标轮 1，较早的轮 0）；只写有样本的切分 |
| `train.samples.jsonl` 等 | 同序的 canonical 样本和渲染元数据（来源、翻译记录、量化误差） |
| `images/<sha256><后缀>` | 截图，每张只存一份 |
| `manifest.json` | 格式 `af-sft-4`，学生配置摘要，每个源与被跳过、被筛掉的原因，按原因的丢弃数，被筛掉的每一步与原因（`steps.filtered`），切分、去污染（`decontaminated`、`against`）与泄漏扫描的结果（`compared` 只数真的比过的样本；没比的按原因记在 `no_eval_loader` 和 `no_reference_answer`，online_mind2web 没有参考答案，它的样本都在后者）、等待动作的台账（`wait_actions`）；没有墙钟字段和绝对路径。度量报告经同一个模型（`schemas/dataset.py::DatasetManifest`）读它，键对不上时点名文件报错 |

同样的输入导出两次，输出逐字节相同。CLI 打印每个切分的样本数和按原因丢弃的步数；失败时退出码非 0，什么也不写。要评测训练出来的学生，复制一份学生配置，把 `llm` 指向训练好的模型，再加上 `trace_writer` hook；导出拒绝带 hook 的学生配置，所以评测用的那份单独放。

### 去掉截断采集上的等待

在慢网络上录制时，每次采集之前的落定等待（最多 `settle_max_wait_s`，5 秒）常被上限截断，teacher 看到还没加载完的页面就先 `wait`。学生在正常网络上服务，不该学这种等待。导出器的 `wait_actions: drop_when_capped` 只在导出的数据里去掉它们，录下的 trace 不变（[architecture_design.md](architecture_design.md) §4.15 "等待动作"）：

```yaml
exporter:
  - exporter_id: openai_messages
    # ……其余字段同上……
    thought_source: teacher
    wait_actions: drop_when_capped
    thought_rewrite_llms:                    # 改写受影响 thought 的辅助 chat 模型，只能有一个
      - { model_type: openai, model: gpt-4.1-mini, provider: { name: direct } }
    thought_rewrite_cache: datasets/thought_rewrites   # 改写缓存，相对工作目录
```

- **哪一步算截断**：策略步的观测里有 `capture_state`，它的落定结果是 `capped`。没录 `capture_state` 的步原样保留（没有证据就不改）。
- **去掉什么**：这一步里执行了而且成功的等待动作（`browser.wait`；动作类声明 `only_waits`，导出不按名字认）。失败的等待留下，它说明了后面的动作为什么被跳过；跳过的尾部里的等待也留下，它没执行。
- **只剩等待的步**整步消失：不产生样本，也不出现在之后样本的上下文里（观测轮和回复轮都没有，`history_steps` 里也没有），段不因它断开；学生看到的步号随之前移（`Step: N/M` 与反馈里的步号），样本的 `sample_id` 和 `step` 仍是源步号。还有别的动作的步留下，只少了等待。
- **改写 thought**：去掉了等待、留下了别的动作的步的 thought，以及紧接在消失的步之后那一步的 thought，交给 `thought_rewrite_llms` 的模型：只删掉说等待、说页面还在加载的话，其余逐字保留。改动了的标 `synthetic: true`（`outcome: rewritten`），模型原样交回的记 `reviewed_unchanged`。`thought_source: none` 时没有 thought 可改，不配模型也不配缓存。
- **必填与拒绝**：`drop_when_capped` 加 `thought_source: teacher` 时，`thought_rewrite_llms` 与 `thought_rewrite_cache` 必填，缺了组装时就报错；`keep` 或 `thought_source: none` 时写了这两项也报错，配置里不留一个从不调用的模型。
- **来源**：每个受影响的样本在 `samples.jsonl` 里带 `sample.wait_removal`：去掉的动作（run、步、动作序号、动作）、前面消失的步的动作、thought 的原文与改写、模型身份、提示版本与摘要、缓存键。`manifest.json` 的 `wait_actions` 汇总消失的步、去掉的动作、改写与原样交回的样本数和改写用的模型与提示。
- **缓存与确定性**：改写按内容存进 `thought_rewrite_cache`（每条一个 `<键>.json`）。同样的输入、配置和缓存导出两次逐字节相同；第一次导出填满缓存之后再导出，一次模型也不调。命中与调用次数只在 CLI 输出里（`thought rewrites: N from the cache, M model call(s)`），不进数据集，否则第一次和第二次导出的数据集就不同了。缓存里的条目读不回来或与它的名字对不上时，导出报 `ThoughtRewriteCacheError` 并点名文件，删掉它再导出。度量链两次导出用同一份 YAML，所以共用一个缓存，第二次全部命中。
- **观测不改**：学生协商了 `capture_state` 时，那一步的观测轮照样写着截断（`Settle: capped ...`）；不想让学生看到就用 `show_capture_state: false` 的学生。

## 度量报告

`spectragent flywheel` 把前面几段串成一条链，依次是录制、对照组（可选）、原路径回放、分叉、两次导出，最后算一份报告（设计见 [architecture_design.md](architecture_design.md) §4.16）。链在进程内依次调用 `spectragent bench` 和 `spectragent export` 的 `main`，每段的运行方式与手动执行相同。它只改各段 YAML 的路径字段（`trace_writer.trace_dir`，`trace_branches` 的 `data_dir`，导出器的 `sources_root`、`sources`、`output_dir`），把改好的配置写到 `<root>/configs/` 再运行。

链规格是一份普通 YAML（pydantic 模型 `ChainSpec`，多余的键被拒），ConfigComposer 不读它。不要把它放进 `configs/flywheel/`，那里每个文件都要能组装并通过 `prepare()`。

```yaml
name: first-live
root: traces/chains/first-live      # 规格里的相对路径都相对工作目录
evidence: live                      # live | rehearsal
tasks: {benchmark: online_mind2web, subset: All, task_ids: [<ID>, <ID>], limit: null}
token_limit: 2000000                # 所有运行段合计的 token 上限；默认 unlimited（不限）
record:    {config: my/record.yaml, timeout: 600, evaluate: false}
control:   {config: my/control.yaml, timeout: 600}        # 可选：录制配置加 archive: []
same_path: {config: my/same_path.yaml, timeout: 600}
branch:    {config: my/branch.yaml, timeout: 600}
export:    {config: my/export.yaml, repeat: 2}            # 2：顺带量导出确定性
report:    {planted: [{label: teacher api key, env: OPENAI_API_KEY}]}
```

`my/` 下是从 `configs/flywheel/` 复制来改过的文件。

```bash
uv run spectragent flywheel run --spec chain.yaml                     # 规格里配了的每一段
uv run spectragent flywheel run --spec chain.yaml --stages record     # 只跑列出的段，按链的顺序
uv run spectragent flywheel report --spec chain.yaml                  # 只读，随时重算
uv run spectragent flywheel report --root traces/chains/first-live --evidence live --out /tmp/report
```

`--stages` 可写的段名是 `record`、`control`、`same_path`、`branch`、`export`、`report`，用逗号分隔。

- `root` 放在 git 忽略的目录下，例如 `traces/` 或 `tmp/`；`runs/` 不在 `.gitignore` 里。
- 布局：`<root>/traces/{record,control,same_path,branch}/`，`<root>/datasets/{first,second}/`，`<root>/report/{report.json,report.md}`，`<root>/configs/`，`<root>/evals/<段>/`（写了 `evaluate: true` 的段），`<root>/chain.json`（每段的 argv、退出码、产生的 run 和起止时刻；`token_limit` 写了数字时，还有每段的 token 用量和停止记录，见下）。
- `same_path` 和 `branch` 的源是 `<root>/traces/record`，所以 `record` 先跑。重跑一个运行段会在已有的 run 旁边再加 run，并记一条 warning（`already holds runs; this stage adds to them`）。重跑 `record` 之后，`same_path`、`branch`、导出和报告都会把新旧录制一起读进来；想干净地重做，换一个 `root`。
- 导出从不覆盖：选了 `export` 时，只要 `repeat` 要写的 `datasets/first`（`repeat: 2` 时还有 `datasets/second`）有一个已经存在，链就在任何一段开跑之前报 `ChainConfigError`。要在链里重导，先删掉这些目录，再跑 `--stages export,report`。也可以复制 `<root>/configs/export_first.yaml`（链实际用的导出配置，路径都已写好），改掉 `output_dir` 和要改的筛选，用 `spectragent export` 导到别处。只想重算报告就用 `spectragent flywheel report`。
- 链用的导出 YAML 用 `filters.require_eval: null`（链默认不评测）和 `decontamination: {against: []}`（源本来就是被测 benchmark 的任务）；这两个就是默认值，发布的 `export_sft.yaml` 也这么写。
- 运行段退出码是 0 或 1、并且写出了新的 run，就算跑完；个别任务的基础设施失败是要度量的数据。退出码 4 表示这一段的 sweep 用完了链剩下的 token 额度，链在这里停下（见下一条）。其余情况链停在这一段，报 `ChainStageError`，之前的产物保留；退出码 2 表示这一段的 pre-flight 拒绝了它的配置，一个任务也没跑（原因在 stderr）；退出码 3 表示一个 guard 否决了这一段（比如录制撞上风控页），sweep 停在被否决的任务，这一段的 run 不会喂给下一段。
- token 上限：`token_limit` 是所有运行段合计能用的 token，默认 `unlimited`（不限，链不数用量，和以前一样）。写了数字时：
  - 用量从 trace 里数：每个 run 的每次模型调用记录里 provider 报的数（`total_tokens`，没给总数时 prompt 加 completion；失败的调用照样计费），不需要 metrics hook。provider 没报用量的调用计入调用数、不加 token，按 `traces/<段>/<run>: step S call K (状态)` 点名，不当作 0。
  - 链开跑前先数一遍链根下已有的 run，所以按段跑时前几段花掉的照样算；每个运行段把剩下的额度交给 `spectragent bench --token-limit`。sweep 每跑完一个任务就加一次；累计达到额度时，正在跑的那个任务照常跑完，之后的任务不开始。
  - 达到上限后，后面的运行段和导出段都不开始，报告照样按已有的数据算，`report.md` 开头写明是因为 token 上限停的（`report.json` 的 `token_use.limit_reached: true`）。`spectragent flywheel run` 打印停在哪一段、用了多少、上限多少，退出码 4；`chain.json` 的 `stopped_by_token_limit` 记着同样的内容，加上没开始的段和没报用量的调用。
  - 一个任务不会被打断，所以实际用量最多超出上限一个任务的用量。要接着跑，先调高 `token_limit`，再用 `--stages` 只跑没开始的段；上限没调高时，选中的运行段和导出段一个也不开始（报告照跑）。
  - 报告的 `token_use` 按角色列出 token、调用数和没报用量的调用，不论有没有上限；`report --root` 没有规格，不知道上限（`limit: null`）。
- `report --root` 没有规格，也就没有植入值，隐私一项的结论是 `insufficient`。
- `report` 写不了 `--out`（建不了目录、写不进去），或者数据集读不回来（别的版本导出的 manifest 或样本行），退出码是 1，stderr 点名是哪个目录或哪个文件（样本行带行号），不会打出 traceback。录制的 `result.json` 读不回来不会让报告失败：它在 `sections.sites` 里记作 `(unreadable result)`。

`report.json` 的 `metrics`：

| 键 | 回答的问题 | 怎么算 |
|---|---|---|
| `archive_overhead` | 开着归档每步多花多少时间和字节 | `record` 与 `control` 每步环境侧时间（`duration_ms` 减 `llm_latency_ms`，策略步，从第 1 步起）的 p50、p95 之差，没有对照组或两组档位不同时为 `null`；`env/` 去重前后的每步字节；`settle_wait`：每个跑过的角色，观测之前等页面落定的 p50、p95、被上限截断的占比和读不到页面状态的次数（从每个观测 fragment 的 `settle` 记录读，环境侧时间里包含这段等待） |
| `body_completeness` | 录下的交换有没有 body | `index.jsonl` 每条记录的最终状态：有 body，不需要 body（`redirect`、`websocket`），明确没有（`too_large`、`incomplete`） |
| `same_path_fidelity` | 原样重放时，恢复出的每一步与录制是否一致（按站点） | 恢复器判 `ok` 的步的占比；严格版另要求 URL 不变（量得到时）且截图 sha256 与源 run 同一步相同；每个 run 第一个不通过的步与原因；URL、元素签名与媒体状态的比较覆盖（`media_coverage`：真正比较了媒体状态的步占被校验步的比例）、原因里有 `media_mismatch` 的步数（`media_mismatches`）与媒体对齐结果按 `outcome` 的计数（`media_alignment`），以及媒体区域规则声明的页面上被校验的步数（`media_area_steps`：这些步不判媒体、截图距离遮掉了媒体框，通过率因此提高的部分是重新归类，不是保真提高），总体与按站点都给 |
| `restoration` | 分叉的前缀恢复得对不对 | `branch` 段校验通过的占比；不通过的按原因计数，重定位失败按 `not_found`、`ambiguous` 计 |
| `branch_coverage` | 分叉之后是否还留在归档里 | 检查点及之后没有主 frame 文档 miss 的分叉占比；其余 miss 按动作、resourceType、host、原因分布 |
| `replay_determinism` | 同样的输入回放两遍，结局是否一样 | 同一源 run、同一检查点、执行动作相同的分支两两比较终止截图的 sha256，另比较检查点步恢复出的截图 |
| `export_determinism` | 同样的 run 导出两遍是否逐字节相同 | `datasets/first` 与 `datasets/second` 的目录树摘要；`repeat: 1` 时为 `null` |
| `privacy` | 凭证有没有发给模型、写进数据集 | 分两道门计数。`model_requests`：每个 run 录下的每一次模型请求的 messages 里的文字（内联图片的 base64 不算文字）；`datasets`：`<root>/datasets` 下每个文件（gzip 先解开）。每道门报每个植入值出现的次数和含有它的请求数或文件数，以及凭证形状的命中数。`env/` 和 trace 的其余文件（步记录、观测、配置）留在本机，不扫描，所以站点页面里自带的公开 key 不算泄漏。读不回来的 run 列在 `skipped` 里 |

分母为 0 的值是 `null` 加原因，从不写成 0。`sections` 另有：按站点的统计（含回放改写了多少个 JSONP 回调名，`callback_rewrites`；按源结局分开计数的代答次数，`substitutions`；序列用尽之后 `repeat_last` 重复送出的次数，`reuses`；后两者都不算命中，原路径回放与分叉合计）和剔除名单（被 anti-bot 拦截、原路径保真低于 0.8、录制在第 0 步环境就失败的站点；模型调用失败从不让站点进名单），anti-bot 拦截（文档应答 403、429 或 503，且带 `cf-mitigated: challenge` 或 `server: cloudflare` 加 `cf-ray`），重复分叉率，POST 分叉的 miss，按角色和类别的存储字节与 gzip 压缩比，每个任务的字节和 10、20、300 个任务的投影，导出产出。`decisions` 是四个待决问题的建议：`miss_policy`（动了手的分叉里零文档 miss 的占比不低于 0.9 就保留 `strict`），`recording_coverage`（动了手的分叉的文档 miss 里 GET 未录 URL 的占比达到 0.5 就扩大录制时的覆盖），`site_rules`（一个端点（host、方法、路径）有 2 次以上近邻 miss 就建议给它写一条限定在这个端点的规则供人审，列出最常见的那组不同字段：查询参数用 `ignore_query`，body 字段用 `ignore_body_paths`），`retention`（每个任务占多少磁盘，与 `sections.retention` 是同一条建议）。

空分叉是从检查点起一个动作也没执行的分叉：teacher 的模型调用失败、回复没有动作（解析失败和解析出零个动作在记录里看起来一样），或者 teacher 决定之前 run 就结束了（恢复失败、观测失败）。它说明不了 teacher 的选择会走到哪里，所以不计入 `branch_coverage` 的占比和 miss、也不计入 `miss_policy` 与 `recording_coverage` 依据的数，按原因记在 `metrics.branch_coverage.empty`；重复分叉率不比较检查点步没有动作的分叉（`sections.duplicate_branches.not_compared`）。

每条建议是 `{status, verdict, rule, evidence, basis, sites, status_reason}`。`sites` 是证据来自几个不同的站点。只有 live 证据、而且来自至少 10 个站点（`SUPPORTED_MIN_SITES`），建议才是 `supported`；不到 10 个站点最多是 `partial`，原因写 "insufficient sample"；演练最多是 `partial`；没有数据时是 `insufficient`、`verdict` 为空。低于 `supported` 的原因都写在 `status_reason` 里。所以 2 到 3 个任务的 live run 只是冒烟测试：它证明整条链能跑通，不拿来定阈值和上面四个决定。`rule` 写出所用的阈值常量和它的值。阈值是 live 之前定的起点，留在 `spectragent/flywheel/report.py` 里，等一次覆盖足够多站点的 live run 校准；每步归档开销的上限 300 ms 例外，它在 `spectragent/core/archive/budget.py`，归档开销基准用的也是它。`evidence` 是指向 `report.json` 里具体值的 JSON pointer 和分母。报告格式是 `af-flywheel-report-12`（`-9` 加了原路径保真的媒体字段 `media_coverage`、`media_mismatches`、`media_alignment`；`-10` 把 `decision_inputs.site_rule_candidates` 改成按端点的 `endpoints`，body 字段也算；`-11` 改成按站点与端点、计回放 run 而不是请求，带与恢复失败的关联和不喂页面的请求 `not_feeding_page`；`-12` 给原路径保真加了 `media_area_steps`；旧格式的报告不读，重新生成）。`report.md` 由同一个模型渲染，列出每个角色被跳过的 run，也列出各项度量自己的 `skipped`（某项读不到证据而略过的 run）。报告只读链根下已提交的 trace 和数据集，同样的输入算两次逐字节相同。

链在报告之前导出，不会套用报告给出的剔除名单。`sections.sites.yaml` 是 `filters.exclude_sites` 形状的片段：把它放进导出 YAML，换一个 `output_dir`，手动再导一次。

## 离线截图差异诊断

```bash
uv run spectragent flywheel diagnose \
  --recordings traces/chains/first-live/traces/record \
  --replays traces/chains/first-live/traces/same_path \
  --out tmp/replay-diagnostics
```

打开输出里的 `index.html`，无需服务端或网络。`diagnostics.json` 保存同一份完整证据，`assets/` 保存原截图字节、预览、像素差图与区域框；原图按内容摘要去重。输出目录必须是新的，且不能位于两个输入目录内。参数错误退出 2，输出写入失败退出 1；已有目录不会覆盖。

诊断只经 RunTrace 读已提交的观测和来源关系，不启动浏览器或模型，不访问 `env/`，也不修改 trace。回放的 `branch.source_trace` 相对于 `--recordings` 匹配源 run；仅比较检查点及以前的源策略步，缺步会显示为“未提交”，分叉后新动作和终止图不混入这项比较。来源不匹配、trace 或图读不回来、截图缺失和尺寸不等都单独显示，尺寸不等不会通过缩放伪造可比性。

页面分开显示“录制与回放不同”和“重复回放之间的候选变化”。只有同源同步骤至少两张可读且尺寸一致的回放图，才能给出后者；只有一次回放时明确说证据不足。原始全图 SHA、dHash、同观测轮次的 URL 是否可比、已有恢复判定和缺失原因全部保留。缺失 URL 不算相同；旧截图未记 URL 时仍能做纯图像诊断。

默认以最大 RGB 通道差大于 16 认定像素变化，在 32 像素网格里变化比例至少 0.01 的格子被标出，再按四邻接合并区域框。可以用 `--pixel-threshold`（0–255）、`--grid-size`（正整数）、`--cell-fraction`（大于 0 至 1）调整；具体方法与参数写进产物。同源同步骤重复图的候选区域来自所有可比图对的变化网格并集，不来自源图与回放的单次偏离。

**候选不代表与任务无关。** 价格、新闻列表、搜索结果、当前选项和错误提示也可能变化。诊断不生成用于忽略差异的遮罩，不改变恢复 verdict、strict 分叉放行或站点剔除，也不报告遮盖后变好的通过率。先检查这些差异是什么，再讨论未来的比较策略。

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
| `browser.extract_content` 读一个元素返回的 HTML（动作结果，会进下一轮提示词） | 按同一条规则，把敏感控件的 `value` 属性和它里面的文本换成 `&lt;redacted&gt;`，其余原样；不给编号时返回的页面文本本来就不含字段的值 | 无 |
| 键入的文字 | 原样记在动作参数和 LLM 记录里；动作结果的 `typed_into_sensitive_field` 记下文字是否进了敏感控件（`true` / `false`，看不见时 `null`），提示词里不显示 | 无 |
| 从非新建 profile 启动的浏览器（登录态） | 绑定了归档器就拒绝；pinned 一律拒绝 | 每个归档器都写 `allow_session_state: true`，只能配 `none` 档位 |
| 配置和任务 setup context | `config.json` 只记标为 trace-safe 的字段，其余写 `"<omitted>"`；setup context 按值脱敏（凭证形状的键、URL 里的 userinfo 和凭证形状的查询参数） | `trace_writer` 的 `record_secrets: true` 原样记录；这样的 run 永远不会被导出 |
| `env/` 归档 | 永不导出，数据集里只有截图、动作、thought 和引用 | 无 |
| 导出写出的每个文本字节 | 过凭证形状的正则，命中就整个导出失败，不留输出 | 无 |

每个 run 的隐私标记在 `config.json` 的 `env_evidence_privacy` 和指纹的 `privacy` 块里（`contains_session_state`、`fresh_profile`、`level`）。凭证形状的正则 `SECRET_PATTERNS` 在 `spectragent/core/trace/secret_patterns.py`，有三处使用：secret guard 测试、导出的凭证扫描、度量报告的隐私一项。secret guard 用真实的组装配置植入凭证形状的值，跑真实的 TraceWriter（带一个归档用的假 capability），再扫描 run 留下的每个文件（gzip 先解开）：默认设置下一个都不能出现，`record_secrets: true` 时必须全部找到。改了脱敏相关的代码就跑它：

```bash
uv run pytest tests/test_core/test_trace/test_secret_guard.py
```

已知的残余：正则认的是凭证的形状，teacher 键入的、不像凭证的密码会原样成为训练目标（键入动作的结果已标出它进了敏感控件，导出还不用这个标记）；WebSocket 帧的内容和 URL（含查询参数）原样记录；`browser.extract_content` 不给编号时返回的页面文本会列出 `<select>` 的选项文字，被判敏感的下拉框也一样（那是页面写的选项，不是你选了哪一项）。需要公开分享的数据优先使用无需登录的公开站点；本轮带登录种子的采集按前述私有证据流程处理，不能据此声称原始 trace 可公开。

## 在真实站点上跑

本节是 `configs/flywheel/` 通用链的操作示例；前瞻采集的任务、三臂预算与登录种子采用前面的逐项决定及 `lookahead/README.md`。真实录制与分叉会消耗模型 token，纯报告重算不会。开跑之前先定下：模型路由（model id 和 `coordinate_space`）、任务列表、链的 token 上限、要不要对照组、要不要评测、磁盘预算。

1. 路由与密钥。`spectragent bench` 和 `spectragent flywheel` 都不读 `.env`（`spectragent run` 会读），先在 shell 里导出：

   ```bash
   set -a; . ./.env; set +a
   ```

   密钥只放在环境变量里，不要写进 YAML 的 `api_key`：链把每段的 YAML 原样复制到 `<root>/configs/`，隐私一项只看发给模型的请求和导出的数据集，不扫描那个目录。路由写在 `llm` 块（`model_type`、`model`，需要时 `base_url`）；`openai/direct` 在 YAML 未写 `base_url` 时由 SDK 读取 `OPENAI_BASE_URL`；其他 provider 可有独立默认地址，例如 `openai/deepseek` 默认官方接口。本轮 teacher 由准备脚本把私有地址显式写入副本，不依赖这个隐式回退。`coordinate_space` 要与模型实际输出的坐标网格一致，Qwen-VL 这类在 0 到 1000 网格上回答的模型用 `normalized_1000`。录制、原路径回放、分叉的提示、坐标系、memory 和动作上限保持一致，loop 按各段职责配置；分叉在开跑前核对兼容性，teacher 的模型和采样可以换（通用 GPT-4o 分叉示例用温度 0.8；本轮 DeepSeek 思考配置不发温度），原路径回放通常用录制时的模型和默认温度。
2. 选任务。先取 10 个，只跑录制、原路径回放和报告，看保真度和花费；再扩到 10 到 20 个任务跑整条链。通用匿名演练先选无需登录的公开任务；本轮前瞻采集则按冻结清单保留所需登录种子。被 Cloudflare 等拦截的画面不能当任务正常内容。online_mind2web 的每个任务有 `task_id`、`task_text`、`start_url`、`difficulty` 和 `metadata.reference_length`，可以离线挑：先挑 `easy`，每个可注册域名只取一个任务，跳过任务文本要求账号的，跳过已知重度反爬的站点。会不会被拦截离线判断不了，被拦截的站点会出现在报告的 anti-bot 一节和剔除名单里，代价是一次录制。`lexbench_browser` 的起始页在数据里不带协议头，加载器补成 `https://`；写成文字或几个站点的任务在开跑前的检查里被拒，挑任务时直接跳过。POST 请求也能命中录下的应答：方法、URL、正文经同一套脱敏和匹配规则比较。分叉产生未录制的提交内容时可能 miss，不能把所有 POST 任务判为必然失败。
3. token 上限。真跑的规格写一个具体的数，例如 `token_limit: 2000000`（默认 `unlimited` 不限）。链从每个 run 的模型调用记录里数用量，每跑完一个任务加一次；达到上限时，那个任务照常跑完，之后的任务和段（包括导出）都不开始，报告照样按已有的数据算，并写明是因为 token 上限停的，`spectragent flywheel run` 退出码 4（见"度量报告"）。按段跑时上限照样管住整条链：链每次开跑前先数链根下已有的 run。上限按估算加上余量来定；一个任务不会被打断，实际用量最多超出上限一个任务的用量。不含解析重试、传输重试与独立 helper 的策略调用预算：

   | 段 | 每个任务的调用数 |
   |---|---|
   | `record`、`control` | 策略步数 S，不超过 `max_steps` |
   | `same_path` | 每个源的 `branches_per_step`（通用示例为 2），各 branch 在最后一步作一次新决定 |
   | `branch` | 每个源的选中步数 × `branches_per_step`（通用示例为 3）× `min(1 + continue_steps, max_steps)`；提前 done 或失败会减少调用 |

   在 memory 没有达到容量上限且每步有一张图时，UI-TARS 第 k 步的调用带 k+1 张截图；实际请求还受所选 memory 与重试历史影响。单张 1280×720 截图算多少 token 因路由而异：先录一个任务，看报告的 `token_use`（每个角色的 token、调用数），再定上限。按这种算法粗估（没有实测）：gpt-4o 上 10 个 easy 任务的录制加原路径回放约 0.2 到 0.6 M token；20 个任务再加分叉（`every_k` 取 k=2，每步 3 叉）约 0.9 到 2.7 M token；对照组相当于每个任务再录一次。provider 没报用量的调用不计 token，报告和 `chain.json` 会逐个点名；从不报用量的路由会让上限形同虚设，开跑前先确认录下的调用带着 `usage`。

4. 按段跑，每段之后重算报告（只读，不花钱），先看 `token_use` 和 `inputs.roles.<段>.model_failures` 再继续：

   ```bash
   uv run spectragent flywheel run --spec chain.yaml --stages record
   uv run spectragent flywheel report --spec chain.yaml
   uv run spectragent flywheel run --spec chain.yaml --stages control      # 可选
   uv run spectragent flywheel run --spec chain.yaml --stages same_path
   uv run spectragent flywheel run --spec chain.yaml --stages branch
   uv run spectragent flywheel run --spec chain.yaml --stages export,report
   ```

   以模型调用失败结束的 run（429、超时、被拒的 schema，`error_phase: llm`）列在 `model_failures` 里。它们不会让站点进剔除名单。teacher 调用失败的分叉是空分叉，不计入 `miss_policy` 依据的占比，按原因记在 `metrics.branch_coverage.empty`；原路径回放在最后一步失败的 run 没有终止截图。预算里给重跑这些任务留出余量。
5. 磁盘。UI-TARS 在 memory 容量范围内携带此前截图；trace 里每张截图只存一份（`messages/images/`，报告存储一节的 `messages:images` 类别），调用记录里只写引用，所以 `messages/` 随不同截图的张数线性增长；此外还有 WARC 里的 body 和原始 DOM。截图只存一份之前，一个 12 步的 UI-TARS 录制在 `messages/` 里存了 78 份截图（32.9 MB），不同的只有 10 张（3.9 MB）；现在同一个录制的 `messages/` 约 3 MB。三个站点的冒烟链按每个任务各段合计约 74 MB（之前约 102 MB），300 个任务约 22 GB；真实页面上的字节以报告的存储和保留期两节的实测为准。
6. 评测默认关闭（`evaluate: false`，即 `--no-eval`）。给 `record` 段写 `evaluate: true` 并在录制 YAML 里加 `evaluator:` 块，每个录制的任务会多一次评测器调用，判定写到 `<root>/evals/record/` 和每个 trace 的 `evals/`。
7. 报告之后，把 `sections.sites.yaml` 放进导出配置的 `filters`，手动再导一次。

## 排错

- `Input tag 'False' found using 'replayability_id' does not match any of the expected tags: 'none', 'pinned'`：YAML 里写了裸的 `off`（旧名字），改成 `replayability_id: none`。翻译器的 `occlusion: off` 同理，改成 `occlusion: none`。
- `The top-level 'archiver' section is not accepted: archiver instances are written only inline, under trace_writer.archive. ...`：把顶层 `archiver:` 段里的条目挪进 `trace_writer` 的 `archive` 列表。
- 校验错误 `environment.0.capabilities.0.replayability`（或 `network_source`）下面写着 `Field required`：capability 块缺必填的受体块，补上 `replayability` 和 `network_source`。`network_source: archive` 块里还要写 `miss_policy: { miss_policy_id: strict }`。
- `UnresolvedWiringError: environment[0].capabilities[0].archiver: ...`：归档器写进了 capability 块，挪到 `trace_writer.archive`。
- `IncompatibleArchiveError`：`trace_writer.archive` 里同一个键写了两次、两个 hook 都带了归档列表，或者环境里没有提供这个归档器的 capability。每个归档器只写一次，只写在一个 trace_writer 上；消息点名请求方和环境实际提供的归档器。
- `BrowserCooperationError: BrowserCapability cannot run this configuration (N problem(s); ...)`：每一行是一个协作问题，点名两边。常见的有：云 provider 没有 `NETWORK_CAPTURE`，换 `local_chrome`；`browser.network require_replayable=true ↔ replayability: none`，录制改用 pinned，或给 `browser.network` 写 `require_replayable: false`；profile 不是新建的，去掉 `user_data_dir`、非默认的 `profile_directory` 或 `extra_args` 里的 `--user-data-dir`；pinned 与 provider 的旋钮冲突；`base_time: from_archive` 配了 `live` 来源；`network_source: archive ↔ task context: no replay_archive`，给 `--replay-archive`（只适用于 react 回放配置），或者经 `trace_branches` 任务运行；`live` 来源却给了 `replay_archive`；回放没有绑定 `browser.replay_log`，或者绑了 `browser.network`（都是把 `browser.network` 换成 `browser.replay_log`）；`live` 来源绑了 `browser.replay_log`；`isolation: required` 与代理或路由开关冲突；以 `start_url '<起始页>' ↔ browser setup` 开头的一行说任务的起始页不是一个完整的 http(s) URL（没有协议头、写成了文字或几个站点），改任务数据或 `--start-url`，或者换一个任务。
- 回放一致性不符：同一个错误里以 `source run '<id>'` 开头的行，例如 `replayability random_seed=7 ↔ this run's random_seed=1`、`viewport 1280x720 ↔ this run's viewport ...`、`launch_flags_sha256=... ↔ this run's planned launch flags ...`、`the source run is itself a replay`、`recorded no browser.network evidence`、`has no environment fingerprint`。让回放配置的档位参数、viewport 和 backend 块与录制一致，回放原始录制而不是回放 run。只警告的差异看回放 run 的 `env/run.browser.json` 的 `network_source.warnings`。
- `source run '<id>' browser default locale='zh-CN' ↔ this browser's default locale='en-US' (read from this host before launch)`（或 `timezone`，或结尾是 `(measured after launch)`）：这台机器上的浏览器会落回与录制不同的默认语言或时区。`read from this host before launch` 在 `prepare()` 就报，`measured after launch` 是启动浏览器之后、准备任何 tab 之前报的（系统设置在启动前说不出的那一项，比如 macOS 以外的语言），这时 trace 已经打开，记为环境失败。在默认语言（macOS 的系统语言）与录制相同的机器上回放；pinned 的时区由 `TZ` 定，档位参数相同就相同。`recorded no browser defaults`：源 run 是这一规则之前录的，没有记默认值，重新录制。
- `ArchiveOpenError: replay archive ... is not a directory`：`--replay-archive` 要指向源 run 目录，也就是含 `manifest.json` 的那个。`ReplayArchiveError` 说明源 run 的索引行读不回来，或者指向的 WARC 记录对不上，消息点名文件和记录 id；源归档已损坏，重新录制。
- 回放里有 miss：用 `diverged_steps` 找出有主 frame 文档 miss 的步，再看这些步的 `replay.jsonl.gz`。`no_match` 而 `candidates` 里有同 host、同 path、只差查询参数的记录，说明参数是每次访问都会变的值（时间戳、会话 id），给这个站点写 `ignore_query` 规则；文档 URL 本身带每次访问不同的值时，回放必然在那一步发散。`too_large`：body 超过 `max_fulfill_body_bytes`，或录制时就是 `too_large`。`body_unavailable`：录下的记录没有正文，`source_outcome` 说源里这个请求怎么结束；规则写了 `serve_identical` 却没有代答，说明序列里没有可用记录、可用记录的正文或状态不一致，或者源里就是网络失败；带 `repeated: true` 的是用尽之后的重复，不是新的缺口。`range_uncovered`：录制没覆盖请求的字节区间，`requested_range` 是页面要的区间；视频跳到录下字节之外时，播放器会以每秒十几次的速度重试同一个起点，按 `requested_range` 归并就分得出是一个缺口还是很多个。`fulfil_failed`：看 `fulfil_error`；`Invalid http status code or phrase` 以前出现在没有标准原因短语的状态码上（携程 h2 上的 432），回放现在给每个应答都配一个 Chrome 收的短语，再出现说明有新的形状，`record_id` 指向那条记录。`websocket` 和 `unservable` 见"已知限制"。报告的 `decision_inputs.site_rule_candidates` 按站点与端点汇总了所有回放 run 的近邻 miss，**数的是回放 run，不是请求**（每秒都发的遥测在一个 run 里只算一次；`requests` 是请求数，只作参考）：每个端点在几个 run 里出现（`runs`）、每个查询参数与 body 字段在几个 run 里不同、body 只剩摘要说不出字段的 run 数（`opaque_body`：只在规范化本身把 body 换成摘要时出现，超过 4096 字符或解析不了；回放日志原样保存 body 键，所以更早的 512 字符截短不再造成它），以及每一种一起变化的字段组合（`field_sets`，run 多的在前）。不喂页面的请求不进排名，按原因单独计数（`not_feeding_page`）：规则已经对它合成 204、Chrome 把它标成 `Ping`（beacon）或 `Image`、录下的应答是 204 或正文为空，都是按录制判断，不按域名。站点内的次序看它与恢复失败的关联：`failed_check_steps` / `passed_check_steps` 是带这个近邻 miss、且自己的恢复校验没过 / 过了的步数（每个 run 的每一步只算一次），`failure_association` 是失败占比的 95% Wilson 下界，高的在前；每一步都发的请求在恢复通过的步上也 miss，排在只在恢复失败的步上 miss 的请求之后。`failed_restoration_runs` 是恢复没过、在第一个没过的步或之前就有这个近邻 miss 的 run 数，只作次序：恢复多半在第 0、1 步就因截图距离或媒体状态没过，只看它会把落地页的遥测排在最前。判断（`decisions.site_rules`）只列每个站点前 3 个（`SITE_RULE_MAX_PROPOSALS_PER_SITE`），`n/m` 是失败的校验步 / 有近邻 miss 的校验步；全部排名在 `report.json` 里，`report.md` 只列建议。一个站点的恢复校验几乎都没过时（比如每步都是媒体状态不符），关联分不出谁，次序实际上就是失败校验步的多少。两边都按回放自己用的规则规范化，规则已经忽略的字段不会再列出来。它只是提示：写规则之前看这个端点的应答，确认这些字段不选内容（关键词、页码、排序、id、筛选都选内容）。不按链跑的回放也能这样汇总：建一个目录，`traces/record` 指向录制目录，`traces/branch` 里放各个分叉 run（符号链接即可），再 `spectragent flywheel report --root <目录> --evidence live --out <输出目录>`。候选只差一个回调参数（`callback`、`cb`、`jsonp` 这类，值是页面起的函数名）时，忽略它是安全的：回放会把应答里的函数名换成这次请求的。
- 回放日志里是命中，页面上却少了 JSONP 取来的内容（热门搜索、广告、搜索结果是空的）：看这个请求的日志条目有没有 `callback_rewrite`。没有而回调名与录下的不同，说明应答的包装不在能改写的形状里（回调名在路径里、`window.NAME(` 这类写法），或者命中的是同一接口不带回调参数的记录；这种请求只能照录下的字节送。报告的 `sections.sites.by_site.<站点>.callback_rewrites` 是每个站点改写的应答数。
- 恢复发散：`strict` 下 run 以 `RestorationDivergedError` 结束，`warn` 下那一步的 `restoration.verdict` 是 `warn`。`reasons` 列出这一步所有不通过的检查，按下面的顺序排列，第一个同时记在 `reason`：
  - `document_miss`：自上一次校验以来回放日志里有主 frame 文档 miss，处理同上一条。
  - `url_mismatch`：恢复出的 URL 与录下的不逐字相同，只在录了 `browser.browser_meta` 的 run 上比较。回放规则集的页面地址规则（`page_urls` 一节，发布的在 `configs/replay_rules/page_urls.yaml`）声明的查询参数不比：页面载入之后自己写进地址、没有请求带它、也不选内容的参数（哔哩哔哩搜索页的 `vt`、同花顺选股结果页的 `sign`）；比较时去掉了哪些参数、是哪条规则去掉的，记在 `restoration.evidence[*].url_ignored_query` / `url_rule_ids`。两个 URL 只差一个这样的参数时，先在源 run 的网络归档里确认没有请求带它（文档请求里有它就是导航地址，不是页面写的），再给这个页面写一条页面地址规则；关键词、页码、排序、id 与筛选从不写。
  - `retarget_not_found`：在 `retarget_tolerance_px` 之内找不到标签、标识属性和文本签名都相同的元素；源 run 当时用的 index 不在录下的 selector map 里（模型编出来的 index）也归这一类。
  - `retarget_ambiguous`：候选不止一个，常见于嵌套的相同元素。
  - `action_failed`：动作的成败与录制不同，或者执行时抛错。
  - `signature_mismatch`：两边可交互元素的签名多重集不同。
  - `media_mismatch`：媒体状态对不上，看 `restoration.evidence[*].media`。`comparisons` 是逐元素的对应（`matched` / `missing` / `extra` / `ambiguous`，元素记作 `<frame>#<顺序> <标签>`）和比较用的那次读数里两边的位置与暂停状态；位置差超过 `media_time_tolerance_s`、暂停状态不同、元素缺失或多出（隐藏的、视口外的也算）、对应有歧义、直播，都不通过。`recorded_unreachable_frames` / `restored_unreachable_frames` 是两次读数都没有应答的子 frame（其中有没有媒体不知道），比较成立时有一个就不通过。`alignment` 是这次观测之前做的每一个对齐动作：`aligned`、`already_aligned`、`failed`（跳转或播放没在上限内完成，或停在了目标之外；`rolled_back` 说回滚是否完成）、`not_restorable`（直播、对应有歧义或缺失）、`unknown`（录下的位置不知道，不参与判定）；`media: none` 时为 `null`。`comparisons` 为 `null`（`detail` 说为什么）是没比成：在截图带媒体元数据之前录的源永远不做媒体校验，只靠截图距离。对齐 `failed` 而回放日志里同一个媒体 URL 有 `range_uncovered`，是跳到了录下字节之外，录制时视频没播到那里，回放救不回来。视频站的播放状态往往做不到一致（流的密钥加密、自动播放的预览、离线加载不出来的播放器、叠放的 `<video>`）：回放规则集的媒体区域规则（`media_areas` 一节，发布的在 `configs/replay_rules/media_areas.yaml`）声明的页面上不判媒体，截图比较遮掉两边媒体事实测到的框（媒体元素与读不到的 frame），证据里多一个 `restoration.evidence[*].media_areas`（规则、遮掉的框、遮掉的占比、整图与遮掉之后的距离、`mask_applied`）。遮掉的超过 75% 时不遮，用整图距离。给一个页面写声明之前，先确认它的检查失败在媒体状态、或截图差别集中在播放器的框里（遮掉之后距离回到阈值之内）；非视频站从不声明。
  - `screenshot_distance`：恢复出的截图与源 run 同一步录下的截图，dHash 距离超过 `screenshot_max_distance`（实测值在 `restoration.evidence[*].screenshot_distance`；媒体区域规则声明的页面上是遮掉媒体框之后的距离，整图距离在 `media_areas.unmasked_distance`）。常见于动画和依赖时钟的内容。观测之前的落定等待（见"前提与限制"）在录制和回放里都等异步加载，比较的就是两边 agent 看到的观测；仍然不同时，看这一步观测 fragment 的 `settle` 记录：有一边 `capped`，说明那边的页面一直没静下来，被上限截断时还在变。还可以试 `pace: recorded_timeline`（按源 run 的时间线恢复，页面的计时器走到与录制相同的位置）或 `pace: recorded_llm_latency`。

  每一步的 `restoration.evidence` 记着各项的测量值，`restoration.actions` 记着每个录下动作的重定位方式和结果。值为 `null` 的项不参与判定，所以 UI-TARS 这类纯视觉 run 主要靠截图距离、文档 miss 和动作成败来判。
- `IncompatibleCheckpointError`：分叉配置与源 run 不匹配，在任何 setup 之前报，点名两边：源 trace 从没关闭，或检查点超出它完整到的那一步（`past the step its trace is complete through`），另一个 agent，不同的 `prompt_template`、`coordinate_space` 等旋钮或数字旋钮（如 memory 的 `max_messages`），不同的 memory 或协商结果，不同的 `max_actions_per_step`（`agent_loop.max_actions_per_step`），源 loop 不是 `react`，源 run 本身是分叉 run，`pace: recorded_timeline` 而源 run 有要执行动作的步没量过动作起点（`action_spans`，消息点名这些步）；开跑之后、任何动作之前还会查种子 prompt 与源 run 是否相同、录下的回复在这个 agent 上能否复现。分叉配置的 agent 块照抄录制配置（llm 一块可以不同）。
- `IncompatibleLoopInputError: Loop 'branch' loop inputs: requires checkpoint`：用 `spectragent run` 跑了分叉或原路径回放的配置。它们只能经 `spectragent bench` 的 `trace_branches` 任务运行，检查配置用 `--list`。
- 录制中途失败：录制按 fail closed 处理，两本账分开记。recorder 连接意外断开时，capability 立刻标记自己不存活，观测、动作和收集都抛 `RecorderLostError`；一次收集失败（例如 archive scope 丢过事件）同样让 run 在这一步停下。录制从不在缺少记录的情况下继续：这一步不提交，它的模型调用作为收尾调用落盘，manifest 记 `"status": "incomplete"`，`incomplete_reason` 以 `recording failed at step t:` 开头，`complete_through` 是 t-1；`result.json` 是 `agent_done: error`、`error: "recording failed at step t: ..."`。agent 恰好在那一步说了 done 时，`result.json` 保留 done 和答案，trace 照样是 `incomplete`。之前提交的步完好，可以拿来分叉、导出；要补全就重跑这个任务。
- run 成功、trace 却是 `incomplete`：尾部没记下来（收尾那次收集、收尾补写的欠账、终止观测那一步，或 `result.json` 写不进去），原因在 manifest 的 `incomplete_reason`。不抛错，run 的结局不变；收尾写不进去的东西丢掉，不重试。磁盘一直满着时连终态都写不进去，manifest 停在 `writing`（日志里有一条 ERROR）。
- sweep 的汇总：表头下面三行是三本账的计数（`agent outcome`、`environment`、`trace`），之后每个任务一行；`infrastructure failure: ...` 是环境坏了、模型在 provider 重试之后仍不可用（`the model call failed after the provider's retries`）或 trace 不完整，计入 `infrastructure_failures`，退出码 1。被否决的任务那一行写 `vetoed: ...`，最后一行 `vetoed: a guard stopped the sweep at ...`，退出码 3。
- 导出失败时什么都不写。`ExportEmptyError` 列出每个筛选和丢弃原因的计数，最常见的是写了 `require_eval` 却没评测过、`against` 列了录制所用的 benchmark，或者 `sources` 只列了分叉 run 而没列它们的源 run。`OcclusionEvidenceMissingError` 见"导出 SFT 数据"。`StudentCaptureMismatchError`：学生的截图管线与录制不同。`SampleTooLongError` 点名超长的样本。`CredentialLeakError`、`ReferenceLeakError`：凭证形状或参考答案进了输出。`output_dir ... exists`：换一个目录，或者删掉旧的。
- `spectragent bench: the pre-flight check refused the configuration for task ...`（退出码 2）：开跑之前，第一个任务的 `prepare()` 拒绝了配置，一个任务也没跑、没建 trace。冒号后面是原来的错误，点名两边：组合问题（`BrowserCooperationError`）、协商（`IncompatibleFragmentError`、`IncompatibleActionError`、`IncompatibleArchiveError`）、loop 输入、任务上下文里没有谁接受的 setup 键。改配置后重跑；只想看哪里不对可以先 `--dry-run`（`spectragent run`）。
- `spectragent bench: the sweep reached its token limit after task ...`（退出码 4）：sweep 的 runs 记下的用量达到了 `--token-limit`。那个任务跑完了，之后的任务没开始；汇总表的表头带 `token_limit_reached=1`，`tokens:` 一行是合计，每个任务那一行带它的 token 数，最后一行写着累计、上限和没开始的任务数。`no usage reported (not counted): ...` 是 provider 没报用量的调用，`tokens not counted: ...` 是 trace 读不回来的任务。
- `spectragent bench: the pre-flight check refused ... a token limit counts the tokens each task's trace records, and this configuration writes no trace`（退出码 2）：给了 `--token-limit`，配置里却没有 `trace_writer` hook，用量无从数起。加上 `trace_writer`，或者去掉上限。
- 链：`ChainConfigError` 在任何一段开跑之前报，原因可能是段 YAML 缺 `trace_writer`、`trace_branches` 或 `exporter` 块（或者有两个），`--stages` 写了不存在或规格里没配的段，或者数据集目录已经存在（怎么重导见"度量报告"）。`ChainStageError` 点名失败的段，`chain.json` 记着每段的 argv 和退出码。
- `spectragent flywheel: stopped at the token limit ...`（`run` 退出码 4）：链根下的 runs 用量达到了 `token_limit`，后面的段没开始，选了报告段时报告照样算了。`chain.json` 的 `stopped_by_token_limit` 写着在哪一段停的（`stage: null` 表示这次开跑之前就已经达到了）、用了多少、上限多少、哪些段没开始、哪些调用没报用量。要接着跑，调高 `token_limit`，再用 `--stages` 只跑没开始的段。

## 已知限制

- 嵌套的 dedicated worker（worker 里再起的 worker）的脚本在 Request 和 Response 两个阶段都不经过 Fetch：录不到，也回放不了。录制时记成 `incomplete`，回放时看得见的记成 `unservable`。
- WebSocket 不回放。录制时帧写进 `websocket.jsonl`；回放时握手记为 miss（`reason: websocket`），隔离下握手直接失败。
- 页面定时器发起的导航：观测开始前会等主 frame 在途的跨文档导航提交（最多 10 s），但检查之后才由定时器发起的导航跟踪不到。截图命令有 10 s 的上限，那一次采集失败后重发，这一步因此多花约 10 s。DOM 采集同样有 10 s 的上限，读文档戳的小命令 2 s：渲染进程不回应时，一次观测尝试最多花掉这些时间，不会卡满整个 30 s。
- 归档开着时，内联读的 body（5 MB 以内、没声明长度的，以及文档）会把响应扣到下载完成，页面拿不到渐进加载的 HTML。更大的 body 边下边收，不扣；但页面不读的 body（`fetch()` 之后从不读，或停住的视频）流不完，记成 `incomplete`。SSE 和长轮询被扣到 `body_read_timeout_s`，之后记为 `incomplete` 并放行；读还在跑，所以页面在录制时收不到它们的事件。
- pinned 的 `Date` 默认（`clock: shared`）是整个 run 共用的一个时钟：run 开始时读 T0，之后按真实时间前进，所有 tab 读到同一个时间。页面用 `Date` 拼进 URL 的时间戳与显示在页面上的时间因此在录制与回放之间按时序相差毫秒到秒，URL 只能经站点规则的 `ignore_query` 或模糊级匹配。`clock: per_document`（WPR 的每文档计数时钟）让同样的调用在回放里读到同样的值，但 tab 之间时间不一致：按时间戳同步多个 tab 的站点（iqiyi.com）会卡死渲染进程。录制与回放必须用同一个 `clock`。`Math.random` 与 `crypto` 是每个文档一条从同一种子开始的流，同一 run 的两个 tab 抽到相同的随机数。
- pinned 不固定 `performance.now` 和定时器。没有 `Last-Modified` 头时 `document.lastModified` 读真实时钟，不带参数的 `Intl.DateTimeFormat().format()` 也读真实时钟。`base_time: from_archive` 下，页面 JS 设的绝对过期时间的 cookie 可能被 Chrome 丢掉。
- 回放 `none` 档位的录制时，DPR 不比较（时区与语言就是浏览器的默认值，按上面的规则比较），HTTPS 升级的试探可能显示为主 frame 文档 miss。
- 时区与语言 override 的所有者：同一个渲染进程里只有最先设它的 frame 是所有者。所有者走了之后，同进程的其余 frame（同一个站点的第二个跨源 iframe、保留 opener 的同站弹窗）在 0.2–0.3 s 内落回浏览器的默认值；保留 opener 的跨站弹窗从第一个文档起就读默认语言。时区因为 `TZ` 仍是 pin 的；语言是机器的系统语言，录在指纹里，回放要求它相同，但落回发生在哪一次观测之前，录制和回放之间可能不同。
- `user_agent` 写的不是 Chrome 自己的写法时（自定义字符串、Edge、Firefox 的标识），pinned 不带 client hints：打不到 opener 的新 tab（不带 `rel` 的 `target=_blank`、`rel=noopener`、`window.open(..., 'noopener')`）的第一个请求带着 Chrome 自己的 `Sec-CH-UA`，之后的请求都不带，录制和回放相同，回放时这个请求不出浏览器。不写 `user_agent` 或写普通 Chrome 标识时没有这个问题。
- 写了 `user_agent` 的 pinned 用 `--user-agent` 启动 Chrome，Chrome 这时自己就把 client hints 的高熵值（架构、位数、完整版本列表、平台版本、形态）报成空的，override 带的也是空的。调用 `getHighEntropyValues` 或用 `Accept-CH` 要这些头的站点看得出它与真实的 Chrome 不同；brands、平台、是否移动端是一致的。
- 模糊匹配可能送错资源，日志里记着 `level: fuzzy` 和得分；耗尽由显式规则选择复用最后一条可用响应（`exhausted`）或缺失（`sequence_exhausted`）。
- `serve_identical` 代答页面在源里取消的请求时，回放页面收到了源页面没收到的一次成功；页面为什么取消（例如图片地址 2 ms 后换成了别的），网络证据看不到，回放页面可能因此走上源里没走的路。日志的 `substituted` 与报告的 `substitutions` 把它们单列。
- 媒体恢复的边界：直播、画布自己画的播放器、跳进录下字节之外的位置、对应有歧义或缺失的元素都不恢复，判 `media_mismatch`（媒体区域规则声明的页面上不判，见上面 `media_mismatch`；画布自己画的播放器在那里也遮不到）；影子树里的 iframe 与读数没有应答的跨站 frame 里的媒体不知道，这一步同样不可恢复。脚本跳转、播放之后播放器可能露出控制条，感知哈希看不出来。源页面在模型思考时播完、自动跳到下一条的，对齐救不回这次导航，表现为 URL 不同。
- JSONP 回调名只在几种确定的包装里改写（见"离线回放一个 run"）。回调名在路径或请求体里、包装是别的写法的，照录下的字节送出，页面拿不到数据，日志里却是普通的命中。
- 回放停在 Chrome 的错误页上时，错误页上写着没送到的 URL；URL 每次都变的站点，按截图比较的回放确定性必然不同。
- 页面落定的等待看不到窗口里只在途、还没完成的请求：点击之后一次慢的接口调用，页面又没有加载提示，视口里也已经有内容时，不等它，下一步才看到结果（视口空着时会等）。内容只认文字与图形元素：只由 CSS 背景图、伪元素文字或闭合 shadow root 组成的页面每一步等满上限；盖住内容的遮罩、还没画东西的 iframe 与 canvas 都算有内容。CSS 过渡与动画、canvas、进程外 iframe 里的变化不经过主文档的 DOM，不算变化。
- 进程外 iframe 的 storage 不读，记在 `storage.json` 的 `unread_frames` 里。
- anti-bot 只按应答头识别，不带这些头的拦截页面认不出来。度量只认 browser 的证据格式。
- token 上限只数 trace 里记下的用量：provider 没报用量的调用只计数、点名，不加 token；评测器的 judge 调用不在 trace 里，不计入（链默认不评测）。

## 定位入口

需要改这一层时，录制与回放看 `spectragent/core/capabilities/browser/` 下的 `recorder.py`、`archive/`、`replay/`、`cooperation.py` 和 `restoration.py`；trace 的读法在 `spectragent/core/trace/reader.py` 与 `spectragent/core/archive/source.py`；分叉在 `spectragent/core/loops/branch.py`、`spectragent/core/loops/restore/` 和 `spectragent/core/data_loader/task/trace_branches.py`；导出在 `spectragent/export/`；度量与链在 `spectragent/flywheel/`。`tests/_helpers/flywheel_rehearsal.py` 是演练怎么改写发布配置、怎么写链规格的完整例子。字段细节见 [configs/README.md](../configs/README.md)，单个浏览器 agent 怎么运行见 [Browser agent 运行手册](browser_agent_runbook.md)。


## 独立 Jev replay 审查

运行示例统一使用 `max_actions_per_step: 4`；record、same-path replay、branch 和学生配置保持相同预算。
当前 trace 为 v7，调用强制区分 `chat` 与 `decision`。旧轨迹保留原件，使用其写入版本的工具审阅；
新 reader 不自动补 `kind` 或转换旧格式。独立审查只读 source/replay，不改变恢复校验、源 token 计数或任务正确性评分。
设计见 [architecture_design.md](architecture_design.md) §4.18。

审查配置为普通组合根中的 `replay_reviewer` 层，模型从 `decision_model` 受体注入：
`configs/replay_review/jev_dmx.yaml`、`jev_direct.yaml`、`jev_openrouter.yaml`。
DMX 根为 `https://www.dmxapi.cn/typesafe`，官方根为 `https://api.typesafe.ai`；都由 provider 拼接 `/v1/systemone`。
OpenRouter 根为 `https://openrouter.ai/api`，拼接 `/alpha/decisions`。凭证由配置指定的环境变量提供；CLI 会读 `.env`。

输入 JSON 示例（路径相对输入文件所在目录）：

```json
{
  "format": "af-replay-review-input-1",
  "pairs": [{
    "source": "traces/source",
    "replay": "traces/replay",
    "source_step": 1,
    "replay_step": 1,
    "source_config": "record.yaml",
    "visual_required": true
  }]
}
```

```bash
uv run spectragent replay-review --config configs/replay_review/jev_dmx.yaml --pairs pairs.json --out reviews/new-job --dry-run
uv run spectragent replay-review --config configs/replay_review/jev_dmx.yaml --pairs pairs.json --out reviews/new-job --json
uv run spectragent view --root reviews
```

源配置须与 trace 中记录的 cleaner、serializer 和 DOM budget 一致。审查器使用这些已组合实例重建 raw DOM，
保留裁剪范围、链接/几何、原始截图及机器恢复证据；不读光标装饰后的模型图片。输入不足、损坏、对齐错误、
模型超时/HTTP/答案错误分别报告。目标是当前任务及下一批动作范围内的相关性，不是预测未录制的后续页面。
标签、预期答案和开发/留出划分放在另外的文件里，永不进入请求。

**三个必填块。** `jev` 审查器的 `evidence`、`questions`、`policy` 都必须在 YAML 里写全，代码不给默认值：

- `evidence`：每侧观测的文本字符上限 `max_text_chars`、链接与几何条目上限 `max_links`、是否附原始截图
  `include_screenshots`，以及 `done_answer: keep | withhold`。出厂为 `withhold`（去掉源 `done` 动作的答案文本，
  动作类型保留）和每侧 6000 字符、20 条：阈值就是在这个预算上拟合的，更大的预算在 DMX 路线上多数页面超出输入上限。
  回放服务事实和恢复对应关系里每条结果的动作效果（`effect`）不进请求；要让模型看到效果须新增一个证据选项并重新拟合。
- `questions`：版本标签 `version` 和问题 ID → 原生 Decision 问题的映射 `items`。出厂 `replay-relevance-2` 是三个独立的
  Noul：目标是否相同（`targets_equivalent`）、任务所需信息是否相同（`information_equivalent`）、证据是否足以比较
  （`evidence_sufficient`）。没有总体 Choice、置信度或 `verdict`；三个答案分别保存，不求平均。改写措辞时换版本标签。
- `policy`：`version: replay-triage-2`、参与路由的问题 `information_question`（必须是问题集里的 Noul）、默认阈值
  `thresholds{accept_information_min, investigate_information_max, fit}` 和覆盖列表 `overrides`（可为 `[]`，不可省略）。
  覆盖元素是 `{scope: site, domain: <注册域>, ...}` 或 `{scope: task, benchmark, subset, task_id, ...}`，其余字段与
  `thresholds` 相同。站点按参考观测 URL 的主机名等于该域或以 `.` 加该域结尾匹配（不用公共后缀表，多个命中时最长的域胜出）；
  任务按源 trace 的 benchmark、subset、task_id 精确匹配；任务覆盖优先于站点覆盖，二者都优先于默认。

每组阈值都带拟合来源 `fit`：数据集说明与摘要、拟合日期、拟合时的政策版本、所用问题集的摘要、所用证据选项全文及其摘要。
当前问题集或证据选项的摘要与某组 `fit` 不同时，组合直接失败并写出两组摘要：改了措辞或预算，就要重新拟合，或为当前
问题与证据写一条新的拟合记录。出厂默认阈值 0.45 / 0.25 来自开发任务上的 12 对标注，是暂定的分诊参数，随标注数据增加重新拟合，
不是准确率承诺。

**路由（`replay-triage-2`）。** 只有信息问题的原生 Noul 参与，门槛由政策版本固定，按顺序判定：

| 路线 | 条件 |
|---|---|
| `investigate_difference` | 回放有视口内图片未被服务（`viewport_image_not_served`），或信息值 ≤ `investigate_information_max`。缺图门槛先于阈值，模型调用失败或答案不合合同时也照样生效 |
| `candidate_acceptable` | 信息值 ≥ `accept_information_min`、恢复检查结论为 `ok`（`warn` 与 `diverged` 都算恢复警告），并且已测得没有视口内缺图 |
| `further_review` | 其余：介于两阈值之间、恢复警告、缺图事实无法测得、证据缺失、服务故障、答案合同错误 |

`reasons` 写明决定路线的条件（如 `viewport_image_not_served`、`information_at_or_below_investigate_max`、
`information_between_thresholds`、`restoration_warning`、`viewport_images_not_measurable`）。`candidate_acceptable`
只决定人工复核的先后（最后复核），从不跳过复核，也不在运行时放行或改变恢复结论。

视口内缺图事实有两个来源，都只看被审查回放步骤自己的那次采集：一是回放日志加 raw DOM（这次采集里被判 miss 的
`Image` 请求，其 URL 被主文档中与视口相交的元素引用；没有绑定 `browser.replay_log` 或 raw DOM 没有布局快照时无法测得）；
二是回放步骤的 `capture_state` 里可见图片加载失败，只数源步骤自己没有失败的那些（源步骤没有可用读数时全部计入，记
`capture_state_baseline: none`）。任一来源为真即为真；两个都无法测得时事实为 `null`，不当作没有缺图。
`facts.viewport_image_sources` 保存两个来源各自的读数。样式表里的 CSS 背景图与子框架里的图片两个来源都不覆盖。

新目录中的 `review-manifest.json` 索引配置、逐项判断、证据和调用。job 快照记录三个块的全文与摘要；每条 assessment 记录
`status`（审查是否执行成功，与路线分开）、`route`、`reasons`、信息值 `information`、实际使用的阈值 `thresholds`
（范围 `default` / `site` / `task`、数值与 `fit`）、机器事实 `facts` 和三个块的版本与摘要。汇总给出各 `status`、`routes`、
`reasons` 与 `threshold_scopes` 的计数。每个模型调用只有一份 latency/usage，一个批次的问题数不等于调用数；未知 usage
单独列出。`spectragent.replay_review.jev.route_result(result, settings, facts)` 可以离线重算路由，不需要浏览器、模型或源文件；
不会自动调用强模型。查看器的 Reports & datasets 中可查看该 job、信息值、所用阈值范围、路线、Noul、Box 和图片。
复审规则改动的效果时，用问题集、政策和证据选项摘要都相同的配置，两次结果才可比。

图片/Box 使用固定模型版本并校验尺寸。实测发现各路由对 null 的准入不同：Score 等级和 Noul criteria
不接受显式 null；OpenRouter instructions 也不能为 null。不支持的组合请求前报错；没有自动网络重试，
不为了通过而改写描述或删除模态。
页内锚点对齐保留完整导航 URL；截图/DOM 的文档 URL 只在 base URL 与 loader 都对应时允许省略 fragment。
在回放规则要求 `unavailable` 之前录的回放读不回它记下的规则快照，准备证据时报 `corrupt_evidence`：复审这些对要重新回放。
