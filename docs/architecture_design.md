# Spectragent — Architecture Design Document

> **Package**: `spectragent` (v0.3.0)
> **Target vision**: Spectragent — 通过统一的抽象和 config-driven 机制，支持所有场景的 benchmark 与 agent 评测（browser-use、tool-use、SWE agent 等）。

---

## 1. 设计哲学

### 1.1 核心原则

| 原则 | 实现方式 |
|------|---------|
| **Protocol-First** | 所有公共契约使用 Python `Protocol`（结构化类型），消费方永远不导入具体实现 |
| **Config-Driven** | 通过 `conscribe` 实现自动注册 + 类型化配置生成，YAML 配置驱动全部组件选择与组装 |
| **Strategy-Pluggable** | 每个可变维度都是独立的 Strategy 注册表，通过注册表 key 切换 |
| **Zero Global State** | 没有单例、没有全局可变状态，所有状态都是实例级别 |
| **Composition over Inheritance** | 依赖通过构造函数注入，Profile 机制组合组件栈 |
| **Record Once, Evaluate Many** | 运行时全量落盘（log 是一等公民），评测与运行完全解耦，支持后置多次评测 |
| **AOP-style Cross-cutting** | 横切关注点（tracing、metrics、monitoring）通过 Lifecycle Hooks 动态织入，业务逻辑（AgentLoop）无感知 |

公共 API 按真实使用者划界：组件消费者依赖已有 Protocol，扩展作者依赖基类明确提供的覆写点和共享方法，纯函数与值对象保持原有的数据合同。不能为每个 helper、属性或 Loop 子类新建一个 Protocol。仅在本组件内部使用的缓存、计数器和算法助手保持私有；类或文件的行数不是职责错误的证据。公开的读取快照不得反向修改持有方的执行、提交或记录健康状态；可变状态按运行或明确的对象实例持有，固定声明表使用不可变值。

### 1.2 类比：Spring-like Config-Driven

框架的组件发现和组装机制与 Spring 类似：

| Spring 概念 | Spectragent 对应 |
|------------|-------------|
| `@Component` / `@Service` | `metaclass=XRegistrar.Meta` + `__registry_key__` |
| Component Scanning | `discover("module.path")` 导入所有子模块触发注册 |
| `@Qualifier` | `__registry_key__` 字符串标识（如 `"swe_agent"`、`"openai.direct"`） |
| `@Autowired` interface | Agent 与 Loop 的装配边界；Loop 用 `__required_slots__` 检查 Agent 的实际方法 |
| `@Qualifier` / 跨 Bean 依赖 | `__wiring__` 跨注册表依赖声明（如 Agent 限定可配置的 Loop） |
| `application.yml` | `configs/*.yaml` |
| Auto-configuration | `conscribe.generate_layer_config_source()` 自动生成 discriminated-union 配置类 |
| `@ConfigurationProperties` | `__config_annotated_only__ = True` + `Annotated[type, Field(...)]` |
| AOP (`@Around`, `@Transactional`) | Lifecycle Hooks（`RuntimeHook`）通过 AOP Weaver + `__intercepts__` 透明织入，Loop 完全无感知 |
| AOP Proxy | `AOPProxy` — Weaver 在运行时包装 agent/env/llm，hook 拦截方法调用自动触发 |

---

## 2. 目标场景

框架需要覆盖的具体场景：

| 场景 | 环境 | Agent 范式 | 动作空间 | 观察模态 | 评测方式 |
|------|------|-----------|---------|---------|---------|
| **Browser Agent** (WebArena, Mind2Web) | 浏览器 (CDP) | ReAct | click, type, scroll, navigate | DOM, screenshot, URL | LLM-as-judge / 元素匹配 |
| **SWE Agent** (SWE-bench) | Docker 容器 (terminal + fs) | CodeAct / ReCode | bash, edit_file, submit | 终端输出, 文件内容, 错误信息 | 运行测试套件 |
| **Tool-Use Agent** (ToolBench, BFCL) | API 沙箱 | ReAct / function-calling | **动态的** — 每个 task 定义不同的可用 tools | 上一次 tool 调用的返回值 | 工具调用序列匹配 |
| **多工具推理** (GAIA) | 浏览器 + 计算器 + 代码解释器 | Plan-Execute / ReAct | 混合：navigate + calculate + run_code | 混合：页面 + 计算结果 + 代码输出 | 最终答案精确匹配 |
| **桌面 Agent** (OSWorld) | 桌面 VM (VNC) | ReAct (视觉) | click(x,y), type, hotkey | 桌面截图 | 最终状态检查 |
| **移动 Agent** | 手机 (ADB/Appium) | ReAct (视觉) | tap(x,y), swipe, input | 屏幕截图, view hierarchy | UI 状态断言 |

---

## 3. 分层架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        Config Layer                              │
│   configs/*.yaml → conscribe.build_composed_config(...)          │
│                  → generated/_composed.py (top-level union)      │
├──────────────────────────────────────────────────────────────────┤
│              ConfigComposer (IoC / AOP 胶水层)                    │
│   raw dict → top_level_type.model_validate                       │
│            → leaves-first instantiation                           │
│            → setattr-injection of wired receptors                 │
│            → ComposedRun (by_layer instance graph)                │
│   ⇣ AgentRuntime.from_composed(run)                               │
├──────────────────────────────────────────────────────────────────┤
│              Agent Layer (两个独立注册表 + __wiring__)             │
│  ┌────────────────────────┐  ┌─────────────────────────────┐    │
│  │ AgentRegistrar (flat)  │  │ AgentLoopRegistrar (flat)   │    │
│  │  swe_agent, browser_use│  │  react, codeact,            │    │
│  │  ui_tars, tool_agent   │  │  function_calling,          │    │
│  │                        │  │  plan_execute               │    │
│  │  __wiring__:           │←→│  __required_slots__:        │    │
│  │   agent_loop, caps,    │  │   {build_react_prompt,      │    │
│  │   observation, action, │  │    parse_react_output, ...} │    │
│  │   llm, memory          │  │                             │    │
│  └────────────────────────┘  └─────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ AgentRuntime (与 loop 范式无关的通用编排)                  │   │
│  │  prepare(): slot 校验 → obs/action 协商 → AOP 织入        │   │
│  │             → loop.weave(agent) [patch agent.run]         │   │
│  │  execute(task): lifecycle hook → wrapped_agent.run(task) │   │
│  │                → 超时控制 → 错误处理                        │   │
│  └──────────────────────────────────────────────────────────┘   │
├────────────────┬──────────────────┬──────────────────────────────┤
│  Memory        │  Hooks (AOP)     │  Schemas                     │
│  (simple,      │  (trace_writer,  │  (Action, Observation,       │
│   sliding,     │   metrics)       │   AgentResult, RunTrace,     │
│   budget,      │   live_monitor:  │   EvalResult, ...)           │
│   summary)     │   本地阶段监控    │                              │
├────────────────┴──────────────────┴──────────────────────────────┤
│  Environment (注册表, 组合 Capabilities, 前缀路由 actions)        │
│   `capabilities: dict[str, BaseCapability]` ← Composer 注入      │
├──────────────────────────────────────────────────────────────────┤
│  Capabilities (all peers, no hierarchy — action 执行 + obs 解析) │
│  每个 capability 通过 `__wiring__["backend"] = "<provider_reg>"`  │
│  声明执行层后端，由 ConfigComposer setattr-注入 provider 实例。   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐   │
│  │ Docker   │ │ Browser  │ │ Desktop  │ │ Android (ADB)    │   │
│  │ (code)   │ │ (CDP)    │ │ VM (VNC) │ │ tap, swipe,      │   │
│  │ bash,    │ │ navigate,│ │ click,   │ │ screenshot,      │   │
│  │ file ops,│ │ click,   │ │ keyboard,│ │ get_ui_tree      │   │
│  │ snapshot │ │ screenshot││ a11y_tree│ │                  │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘   │
│  ┌──────────────────┐ ┌──────────────────────────────────────┐  │
│  │ GPU profiling    │ │ Tool sandbox (API)                   │  │
│  │ gpu_run,         │ │ call_tool(name, params),             │  │
│  │ profile_kernel,  │ │ get_user_msg(), get_policy()         │  │
│  │ profile_system   │ │                                      │  │
│  └──────────────────┘ └──────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────┤
│  Backends / Providers (执行层适配器，按 capability 分目录)         │
│  core/capabilities/{cap}/backends/                                │
│   browser  → local_chrome / lexmount / skyvern /             │
│              browser_use / agentbay / airtop /     │
│              hyperbrowser                                    │
│   docker   → local_docker / ssh_docker (P2 占位)                  │
│   desktop_vm / android / gpu_profiling / tool_sandbox: 待 P3      │
├──────────────────────────────────────────────────────────────────┤
│  Benchmark = capabilities[]                                      │
│  SWE-bench=[docker]  WebArena=[browser]  τ-bench=[tool_sandbox]  │
│  KernelBench=[docker,gpu_profiling]  OSWorld=[desktop_vm]        │
│  GAIA=[browser,tool_sandbox] TheAgentCompany=[vm,browser,tool]   │
├──────────────────────────────────────────────────────────────────┤
│                     Core Infrastructure                          │
│  ┌──────────┐ ┌────────────┐ ┌───────────┐ ┌────────────────┐  │
│  │   LLM    │ │ Data Load  │ │ Evaluator │ │  Observation   │  │
│  │ (17+)    │ │ (task,     │ │ (exact,   │ │  Fragment      │  │
│  │          │ │  eval)     │ │  judge,   │ │  Registry +    │  │
│  │          │ │            │ │  test,    │ │  Negotiation   │  │
│  │          │ │            │ │  trace)   │ │                │  │
│  └──────────┘ └────────────┘ └───────────┘ └────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

> **`backend` 是 capability 的执行层配置**：每个 capability 通过 `__wiring__["backend"] = "<provider_registry>"` 声明后端注册表（如 BrowserCapability 用 `"browser_provider"`）。Backend 文件物理上属于 `core/capabilities/{cap}/backends/`，与 capability 同目录，从属关系一目了然。

> 完整的 Capability 架构示意图见 [`flat_capability_architecture.svg`](./flat_capability_architecture.svg)。
> 每个 capability 各自管理自己的 lifecycle（启动/销毁/清理），通过 `setup()` 调 `backend.open()` 取得 transport 端点（cdp_url / vnc_address / adb_device_id ...），`teardown()` 调 `backend.close()` 释放 session。
> Framework（AgentRuntime）负责：协商 observation/action → 织入 AOP → weave Loop → 评分。

---

## 4. 核心抽象详解

### 4.1 Agent-Loop 分离体系

**设计决策**：Agent（领域知识）和 Loop（执行策略）是两个独立注册表。**Loop 兼容性走 slot 校验，不是 Protocol**——每个 Loop 子类用 `__required_slots__: set[str]` 声明它需要 agent 提供哪些 method 名（如 `{"build_react_prompt", "parse_react_output"}`），`AgentLoopBase.weave(agent, **deps)` 用 `callable(getattr(agent, slot))` 校验。

**为什么 slot 而不是 `*Compatible(Protocol)`**：当一个新 Loop 只是 React 多一个 method（如 `parse_thought`）时，slot 方案就是子类 `ReactWithThoughtLoop(ReactLoop): __required_slots__ = ReactLoop.__required_slots__ | {"parse_thought"}`，零新 Protocol。Protocol 方案则要新增 `ReactWithThoughtCompatible(Protocol)`，导致 N×M Protocol 爆炸。slot 方案的代价是失去静态类型检查；但兼容性失败在 `prepare()` 阶段就 raise `IncompatibleError(missing_slots=[...])`，错误定位仍清晰。

```
Agent = 领域知识 (domain)  ← 一份代码，不因 loop 翻倍
Loop  = 执行策略 (strategy) ← __required_slots__ 声明它需要的 agent 方法
__wiring__ 声明跨注册表依赖 ← 配置生成时限定可选注册键和子配置
```

`__wiring__` 保护的是配置表面：用户只能选择声明范围内的注册实现，并填写该实现的子配置。它不证明两个选中的实现能协作。Agent 是否提供 Loop 所需方法由 `prepare()` 的 slot 校验判断；失败时 `IncompatibleError` 点名 Loop 和缺失方法。

**依赖方向：单向，无循环**

```
Loop ──调用──▶ Agent 的方法 (build_prompt, parse_output)
Loop ──驱动──▶ Environment (observe, act)
Loop ──weave──▶ agent.run = closure(loop._execute)  ← Loop 物理上"安装"到 agent
Agent 不持有 Loop 引用 — Agent 是被动的"知识提供者"
```

#### 两个独立注册表

```python
AgentRegistrar = create_registrar(
    "agent", BaseAgent, discriminator_field="agent_id", key_separator="."
)
AgentLoopRegistrar = create_registrar("agent_loop", AgentLoop, discriminator_field="loop_id")
```

Agent 的键可以分层（`browser_use.dom`），见下文"Agent 家族"。

#### Loop 协议 + slot 体系

`AgentLoop` Protocol 只定义 weave + _execute 这一对 entry point；具体的"对 agent 的要求"由每个 Loop 子类用 `__required_slots__` 声明。

```python
@runtime_checkable
class AgentLoop(Protocol):
    """执行策略契约 — 定义 agent 的编排流程"""
    loop_id: str

    def weave(self, agent: Any, **deps: Any) -> Any: ...
    async def _execute(self, agent: Any, task: str, *, ctx: dict[str, Any], **kwargs: Any) -> Any: ...


class AgentLoopBase(metaclass=AgentLoopRegistrar.Meta):
    """所有 Loop 实现的基类。"""

    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()
    __loop_inputs__: ClassVar[Mapping[str, LoopInput]] = {}   # 每个任务交给 Loop 的输入（M31，见下文）
    __requires_env__: ClassVar[bool] = True                   # 没有环境的 run：prepare() 报 MissingEnvironmentError（见下）
    __loop_fragments__: ClassVar[frozenset[str]] = LOOP_FRAGMENT_KEYS  # 本 Loop 建得出的 loop 来源 fragment（§4.4）

    def validate_slots(self, agent: Any) -> None:
        """callable(getattr(agent, slot)) — 缺 slot 即 raise IncompatibleError"""

    def check_loop_inputs(self, inputs: Mapping[str, Any]) -> None:
        """键与类型对照 __loop_inputs__ — 多的、缺的、类型不对的一次报全：IncompatibleLoopInputError"""

    def weave(self, agent: Any, **deps: Any) -> Any:
        self.validate_slots(agent)
        self.check_loop_inputs(deps.get("loop_inputs", {}))
        ctx = dict(deps)               # 只活在闭包里：loop 实例上什么都不存（没有 loop.ctx）
        loop = self
        async def run(task: str, **kw: Any):
            return await loop._execute(agent, task, ctx=ctx, **kw)
        agent.run = run                # instance-level monkey-patch
        return agent
```

**weave 是无状态的。** deps 只活在 `agent.run` 闭包里，同一个 loop 实例可以织到 N 个 agent 上，第 N 次 weave 不会改写第 1 个 agent 的环境；loop 实例上没有 `ctx` 属性（`test_shipped_config_integration.py` 断言它不存在）。`ctx` 的内容由 `AgentRuntime.prepare()` 决定（§4.2）：`env`、`llm`、`memory`、`act_fn`、`negotiated_obs`、`negotiated_actions`（M31）、`active_descriptors`、`step_recorder`、`task_id`、`loop_inputs`（M31：只读映射，Loop 不声明输入时为空）。

**Loop 的两条类级声明，`prepare()` 都查。**

- `__requires_env__`（默认 `True`）：Loop 要不要环境。要而这个 run 没有环境（`AgentRuntime` 的 `env` 是 `None`）时，`prepare()` 在 loop 输入校验之后、任何协商之前抛 `MissingEnvironmentError`（`core/loops/validation.py`，点名这个 Loop）；以前是在观测协商里对 `None` 取 `provided_fragments` 的 `AttributeError`。声明 `False` 的 Loop 可以不带环境跑：缺席的环境什么都不提供，观测、动作协商照常进行，需要环境的 fragment 或动作的 agent 由协商点名拒绝；这样的 run 没有动作空间（`active_descriptors` 为空，守卫拒绝一切动作），给了 setup context 的键也被拒绝（没有可以 setup 的环境）。v1 里没有这样的 Loop：`react` 与 `branch` 都要环境。
- `__loop_fragments__`（默认是 `core/loops/_loop_fragments.py` 的 `LOOP_FRAGMENT_KEYS`，即共享的 `loop_fragment_builders` 建的四个 `loop.*`，直接从那个函数读出来，两者不会漂移）：本 Loop 在 `env.observe()` 之后填哪些 loop 来源 fragment。Loop 也是 fragment 的提供方：观测协商在 Loop 一侧只提供它声明的这些（§4.4 "协商规则"），agent 要求一个已注册、但这个 Loop 不建的 loop 来源 fragment 时，`prepare()` 抛 `IncompatibleFragmentError`，点名这个 fragment 与这个 Loop（`provider="loop '<loop_id>'"`）。以前协商的 universe 是全部已注册的 `loop.*`，不管选的是哪个 Loop，缺的 builder 要到第 0 步 `populate_loop_fragments` 的 `KeyError` 才暴露，run 被记成环境失败。建别的（或一个都不建）的 Loop 覆盖它；子类里写成普通 `set` 的，类创建时冻结成 `frozenset`，写成字符串的在 import 时报错。不继承 `AgentLoopBase` 的结构化 Loop 不声明就是什么都不建。

具体 Loop 子类示例：

```python
class ReactLoop(AgentLoopBase):
    __registry_key__ = "react"
    __required_slots__ = {"build_react_prompt", "parse_react_output"}
    __optional_slots__ = {"get_react_output_model", "prepare",
                          "format_react_output", "format_action_fact",
                          "parse_react_thought"}   # 后三个：训练导出（M32，见下）

    async def _execute(self, agent, task, *, ctx, **_):
        # deps 每次调用经 ctx 传入，不存在 self 上
        run = LoopRun.from_ctx(agent, task, ctx, ...)   # 一个 run 的依赖与账本（core/loops/run.py）
        for step_idx in range(self.max_steps):
            result = await run.step_executor.execute_step(run, step_idx)   # observe → build_prompt → LLM → parse → act
            if result is not None:
                return result
        ...
```

#### 可选 slot：Loop 声明，谁用谁校验（M32）

`__optional_slots__` 列的是"有就用"的 agent 方法，用的人不一定是 Loop 自己。`prepare()` 的 slot 校验只查 `__required_slots__`；可选 slot 由**用它的那一方**在用之前校验：

| 可选 slot | 谁调用 | 何时校验 |
|---|---|---|
| `get_react_output_model()` | `ReactLoop` 自己（每次 LLM 调用的 `output_format`） | 不校验：没有就是文本调用 |
| `prepare(task, action_space, execution_limits=..., fragment_formats=...)` | `AgentRuntime`（种子消息，§4.2 `seed_messages`；`action_space` 是本 run 守卫执行的那个 `ActionSpace`，§4.5“有效动作空间是实验身份”；`fragment_formats` 只给声明了 `__fragment_formats__` 的 Agent，§4.4 “观测格式由提供方描述”） | 不校验：没有就没有种子消息 |
| `format_react_output(actions, *, thought=None) -> str` | 训练导出器（学生一侧，§4.15） | 只在导出组装学生时 |
| `format_action_fact(action) -> str \| dict` | 训练导出器（`keep_as_text` 的文本轮，§4.15） | 不校验：没有就写动作自己的 JSON（viewport 单位） |
| `parse_react_thought(llm_output) -> tuple[str, str] \| None` | 训练导出器（teacher 一侧，`thought_source: teacher`） | 只在导出读 teacher 的 thought 时 |

后三个都是 ReAct 的 slot，所以写在 `ReactLoop.__optional_slots__` 里（`BranchLoop` 继承）；不另立 `__inverse_slots__` 之类的新机制，也没有导出专用的错误类型。

- **`format_react_output`** 是 `parse_react_output` 的逆：把一步的（已翻译、全限定名的）动作和可选的 thought 写成本 agent 的回复文本，正是这个 agent 在服务时会被要求写出的那种。它交到的动作已经在模型的网格上：导出器先用本步的坐标系把录下动作的点放上去（`core/loops/coordinate_frames.py::actions_in_model_frame`，§4.1"坐标系归框架"），读回时再由框架换回 capability 的单位，所以它不读任何 Agent 状态，也没有调用顺序的规定。确定性：同样的输入同样的文本。动作名或某个参数这个格式写不出来时抛 `UnrepresentableActionError(action_name, params, detail)`（`core/loops/errors.py`，`ParseError` 的对偶），绝不静默丢掉；导出器把它当成 `untranslatable` 的原因 `unrepresentable`（§4.15）。round-trip 的定义（§4.15 "round-trip"）对每个实现都成立，`tests/test_agents/test_format_react_output_round_trip.py` 对每个定义了它的已注册 agent 类逐一检查。v1 只有 `ui_tars` 实现它：它的这个 slot 只有一行，调它的无状态编解码（`agents/ui_tars/codec.py::UITarsCodec.write`，下文"UI-TARS：胶水 agent 与无状态编解码"）；执行反馈与 restored 历史用同一个编解码写已执行的动作，点由框架放在那批动作所在步记录的坐标系上（§4.1"模型看到的每个点都写在它写点的那个网格上"）。
- **`format_action_fact`** 把一个动作写成本 agent 当作事实展示它的样子。交到的动作的点已经由框架放在它所在那一步的坐标系上（`fact_in_model_frame`）；在那里没有位置的点是说明它为什么没有位置的那句话（§4.1"坐标系归框架"）。UI-TARS 的执行反馈、restored 历史与训练导出的 `keep_as_text` 文本轮共用它（2026-10-22；编解码的 `write_fact`），所以文本轮与下一步反馈里的同一个动作写法相同，都不是 trace 的 CSS px。它不读 Agent 状态，任何时候、任何顺序调用都一样（原来的 `step` 参数与"在第 `step` 步的 `build_react_prompt` 之后调用"的规定删除）。没有这个 slot 的学生，导出写动作自己的 JSON。
- **`parse_react_thought`** 从一次录下的回复里读出 thought 及其来源字段，读不到返回 `None`。它是纯函数（只读传进来的回复，不读实例状态，所以写成 `@staticmethod`），导出器按源 trace 的 `agent_id` 从 `AgentRegistrar` 取 teacher 的类直接调用，不构造实例。`browser_use`：`thinking` > `next_goal` > `memory`（第一个非空的，记下是哪个字段；flash 模板只教 `memory`）；`ui_tars`：原生的 `Thought:`（到第一个 Action 行为止；编解码模块的 `read_thought`，不需要 run）；`skyvern.dom` / `skyvern.dom_vision`（根上的同一个 staticmethod）：回复 `actions` 里每个条目的 `reasoning` 按顺序以换行连接。
- **校验**：导出器组装学生之后先照常 `loop.validate_slots(agent)`（`prepare()` 已做），再查 `callable(getattr(agent, "format_react_output", None))`；`thought_source: teacher` 时对 teacher 的类查 `parse_react_thought`。缺了抛已有的 `IncompatibleError(agent_name, loop_id, missing_slots, needed_for=...)`。`needed_for` 是 M32 加的措辞参数：默认（运行装配）写 "required by loop 'react'"，导出写 "needed for training export (optional slots of loop 'react')"，不让一个可选 slot 的缺失读起来像 Loop 的必需 slot。

#### Loop 实现现状与规划

```
AgentLoop (Protocol)
│
├── ReactLoop (__registry_key__ = "react")  ← ✅ 已实现
│     ├── slots: {build_react_prompt, parse_react_output}
│     │           opt: {get_react_output_model, prepare,
│     │                 format_react_output, parse_react_thought}   ← 后两个只有训练导出用（M32）
│     ├── 循环：observe → build_react_prompt → LLM → parse_react_output → act
│     │
│     └── BranchLoop (__registry_key__ = "branch")  ← M31（P2，数据飞轮）
│           ├── slots：继承 ReactLoop，不另立 Protocol
│           ├── loop 输入：checkpoint（源 trace + 检查点步 t）
│           └── 恢复 0..t-1 → 第 t 步策略步 → continue_steps 个 ReAct 步 → 终止观测
│
├── CodeActLoop (__registry_key__ = "codeact")  ← P2 待实现 (M20)
│     └── slots: {build_code_prompt, parse_code_output}
│
├── FunctionCallingLoop (__registry_key__ = "function_calling")  ← P3 (M22)
│     └── slots: {build_funccall_prompt, get_tool_schemas}
│
└── PlanExecuteLoop (__registry_key__ = "plan_execute")  ← P3 (M24)
      └── slots: {build_plan_prompt, build_exec_prompt, parse_plan}
```

新增 Loop 时：
- **完全新策略** → 新 Loop 类 + 新 `__required_slots__`
- **现有策略的微调（如 React 多一个 method）** → 子类继承 + slot 集合并集，零新 Protocol

#### Agent 声明跨注册表依赖

```python
class SWEAgent(AgentBase):
    __registry_key__ = "swe_agent"

    # __wiring__: 声明可配置的 loop 范围、需要的 capability、observation
    # llm 继承自 AgentBase（auto-discovery，允许所有 LLM）
    __wiring__ = {
        "agent_loop": ("agent_loop", ["react", "codeact"]),
        "capability": ("capability", ["docker"]),
        "observation": ("observation", ["terminal"], ["filesystem"]),
    }

    # 满足 ReactLoop.__required_slots__ 的方法
    async def build_react_prompt(self, obs, *, context: ReactPromptContext): ...
    def parse_react_output(self, output): ...
    def get_react_output_model(self): return SWEReactOutput

    # 满足 CodeActLoop.__required_slots__ 的方法
    def build_code_prompt(self, obs): ...
    def parse_code_output(self, result): ...
```

conscribe 在 `build_layer_config()` 时将 `agent_loop` 字段自动约束为 `Literal["react", "codeact"]`，非法值（如 `agent_loop: plan_execute`）在 Pydantic 校验阶段即被拒绝。

#### `__wiring__` 的 MRO 合并

```python
class AgentBase:
    __wiring__ = {
        "agent_loop": "agent_loop",          # Mode 1: 允许所有已注册 loop
        "llm": "llm",                        # Mode 1: 允许所有已注册 LLM（简单 IoC）
    }

class SWEAgent(AgentBase):
    __wiring__ = {
        "agent_loop": ("agent_loop", ["react", "codeact"]),  # 收窄
        # llm 继承自 AgentBase，保持 auto-discovery
    }

class SimpleSWEAgent(SWEAgent):
    __wiring__ = {
        "agent_loop": ("agent_loop", ["react"]),  # 进一步收窄
    }
```

**LLM 作为简单 IoC**：LLM 不需要协商，config → 注册表解析 → 实例化 → AgentRuntime 注入 Loop.run()。每个 agent 可独立配置 LLM（实验场景：agent A 用 gpt-4o，agent B 用 gpt-4o-mini）。真有 agent 对 LLM 有限制（如只支持 function-calling 的 provider）再通过 `__wiring__` 收窄。

#### Agent 家族：抽象根 + 分层键（用户 2026-10-23 的裁决）

`AgentRegistrar` 带 `key_separator="."`。一个 Agent 家族由两部分组成：

- **家族根**：抽象类，`__abstract__ = True`，`__registry_key__ = "browser_use"`，本身不注册，YAML 里选不到。家族共有的东西放在根上：Loop 范围等共有的 `__wiring__` 条目、共有的配置字段、prompt 构建与输出解析。
- **成员**：注册的子类，键是 `<根>.<成员>`。键可由类名推出（根下的 `class Dom` 得到 `browser_use.dom`），也可写全（`__registry_key__ = "browser_use.dom_vision"`）。每个成员写出自己完整的 `__wiring__` 条目（它观测什么、声明哪些动作），与根上的条目经 MRO 合并（上文"`__wiring__` 的 MRO 合并"：子类条目整条替换继承来的同名条目）。一个成员是一个类、一种观测与动作组合，不靠实例字段在模式之间切换。`AgentRegistrar.children("browser_use")` 列出一族。

BrowserUse 与 Skyvern 各成一族，成员都是 `dom` 与 `dom_vision`。DOM 必开：有 DOM 清洗时 `click` 按元素序号，有截图时可另有 `click_coordinate`；纯视觉归 computer use 一类 agent（UI-TARS 等）。两族都已拆成这两个成员（下文"BrowserUse 家族"、"Skyvern 家族"）；UI-TARS 仍是不带点的单键。

**YAML 形状**：`agent_id` 仍是一个判别字段，`agent_id: browser_use.dom` 是一个扁平判别值。conscribe 生成 `agent_id: Literal['browser_use.dom']`，不像 LLM 的 `model_type` + `provider` 那样展开成嵌套块（那是两个判别字段 `discriminator_fields`，§4.7）。默认实例 id 是 `browser_use.dom#0`。

```yaml
agent:
  - agent_id: browser_use.dom
    agent_loop: { loop_id: react }
    llm: { model_type: openai, model: gpt-4o, provider: { name: direct } }
    memory: { memory_id: simple }
```

**prompt 按家族根取目录**：`agent_prompt_dir(agent_id)` 取第一个点之前的部分，`browser_use.dom` 与 `browser_use.dom_vision` 都读 `prompts/agents/browser_use/`，记录下的 `PromptRef.path` 也是 `agents/browser_use/<name>.md`。成员之间差的是观测与动作声明，不是模板教给模型的协议；一族一个目录，模板只有一份，不会出现各成员的副本互相漂移。不带点的键（`ui_tars`）自成一族，路径不变。

**家族根保持抽象**：已注册 agent 的子类如果不写自己的键，会被推出 `<父键>.<类名>`（`ui_tars` 下得到 `ui_tars.mobile`），父键就既是 agent 又是家族根，conscribe 的 `tree()` 拒绝这种形状。把一个 agent 拆成家族，就是把它的类改成抽象根；`tests/test_agents/test_agent_families.py` 对发布的 agent 检查没有键同时是两者。抽象根在 Python 里也构造不出来（2026-10-23）：YAML 本来就选不到它（conscribe 不注册抽象类），但直接 `BrowserUseAgent()` 以前能构造，run 到 `prepare()` 才因为只有成员才有的属性失败（`AttributeError: _SURFACE`）。现在 `AgentBase.__new__` 对自己类体里写了 `__abstract__ = True` 的类抛 `TypeError`，点名它的成员（派生自它的已注册 agent，带键与类名），例如 `'browser_use' (BrowserUseAgent) is an abstract agent family root and cannot be constructed; construct one of its members: browser_use.dom (BrowserUseDomAgent), browser_use.dom_vision (BrowserUseDomVisionAgent).`。规则只有这一处，各家族不各写一份；标记与 conscribe 一样只读类自己的类体，成员不写它，照常构造。

**BrowserUse 家族**（用户 2026-10-23 的裁决，取代 `observation_mode` 的 `dom` / `dom_vision` / `vision` 三个模式）。家族根 `BrowserUseAgent`（`agents/browser_use/family.py`）持有 Loop 范围、共有的配置字段、prompt 构建与回复解析；两个成员各写完整的 `observation` 与 `action` 条目：

| 成员 | 类（文件） | 进入模型上下文 | observation required / optional | action required / optional |
|---|---|---|---|---|
| `browser_use.dom` | `BrowserUseDomAgent`（`dom.py`） | DOM 文本 | `dom`、`screenshot`、`platform`、`task`、`browser_meta` / `step_info`、`history`、`capture_state` | `navigate`、`done`、`click`、`type` / `scroll`、`send_keys`、`extract_content`、`go_back`、`go_forward`、`switch_tab`、`wait` |
| `browser_use.dom_vision` | `BrowserUseDomVisionAgent`（`dom_vision.py`） | DOM 文本 + 截图 | 同上 | 同上，optional 另有 `click_coordinate` |

- `dom` 在两个成员上都是 required：配 `vision_only` profile 的 run 在协商时失败，报错写 `dom ← browser_use.dom`，不会静默降级成视觉实验。纯视觉删除：只看截图的是 computer use，归 UI-TARS 一类；它的 surface 片段 `_surface/vision*.md` 随之删除。
- `screenshot` 在两个成员上也都是 required：它不只是模型输入，`online_mind2web` / `lexbench_browser` 从 trace 里的截图打分。成员决定模型看到什么，不决定 run 记录什么。
- `browser_use.dom_vision` 的动作与原来的 `dom_vision` 模式相同：`click`、`type` 按序号，另有 `click_coordinate`（够到序号够不着的目标，如跨域 iframe 小部件），没有 `type_coordinate` 与 `drag`。两个点击动词互相独立，没有一个参数一半不适用的重载动词。
- 成员之间的行为差别只有两处，都是成员覆盖根上的一个方法：`dom_vision` 每步把截图附进用户消息（这一步的截图没有字节时只发文本，与 `dom` 相同），本 run 协商到坐标动作时在系统提示的 surface 之后加 `<coordinates>` 段（下文"BrowserUse 的反馈网格"）。`coordinate_space` 与 `vision_detail` 只在 `dom_vision` 上；`browser_use.dom` 不写点，没有这两个字段。`vision_detail` 默认 `auto`，与 `skyvern.dom_vision` 相同、描述也相同：点是从这张图上读的，`low` 会让 `click_coordinate` 靠猜（2026-10-23 之前默认 `low`）。
- surface 片段按成员取（`_surface/dom.md`、`_surface/dom_vision.md` 与各自的 terse 版），模板只有一份：成员不是选模板的一个轴，DOM 与 DOM + 截图的消融只差 surface 片段。每个模板都引用 surface，三个 `is_browser_use_model` 模板也是（§4.4 "观测格式由提供方描述"）。
- 历史窗口是根上的配置字段 `step_history_window`（默认 10，至少 1）：每步消息的 `<agent_history>` 重放最近多少步。它以前是 `init=False` 的私有常量，YAML 改不了，是一个看不见的实验变量；Skyvern 的对应字段 `action_history_window` 按动作计数（执行过的，与因同一步更早的动作失败而没执行的）。
- **提示词说实话**（2026-10-23，与 Skyvern 的同一处更正，下文"Skyvern 家族"）：`system_prompt` 与 `system_prompt_no_thinking` 原来说页面改变之后其余动作自动跳过（"the remaining actions are automatically skipped" / "the sequence is interrupted"），并让模型补做"因页面改变没执行"的动作；而当时的 Loop 执行一步里的每个动作，只有 `done` 或抛异常的动作才停下。2026-10-23 写成：每个动作即使前一个失败或改变了页面也会执行。用户 2026-10-24 的裁决把 Loop 改成在失败的动作处停下（下文"一步里有动作失败，这一步就停下"），提示词随之改成：动作失败，这一步就停下，后面的动作不执行，`<agent_history>` 写明是哪几个；改变页面的动作不让这一步停下，后面的动作照样执行，动作之间不重新读页面，新状态下一步才看到；页面在上一步中途改变时，之后的动作仍按改变前的页面执行，要按 `<agent_history>` 里的结果补做没生效的。四个 surface 片段原来说"序号每步重建"、页面改变后"同一个序号可能指向别的元素"；序号是稳定的 backend 节点 id（`dom/models.py::element_handle`），现在写成：元素在页面上就一直是那个序号，步与步之间不重新编号，本步没列出的元素用旧序号也够不到，被替换或删除的元素连同序号一起消失（看起来一样的新元素是新序号）。其余上游文字与语气不变；`<efficiency_guidelines>` 里"改变页面的动作放在最后"本来就对，保留。`test_browser_use_family.py` 对每个模板变体钉住这两点。`system_prompt_flash` 与三个 `is_browser_use_model` 模板本来就不说失败之后怎样，不改。

**Skyvern 家族**（用户 2026-10-23 的裁决：Skyvern 与 BrowserUse 对称）。Skyvern 有自己的 DOM 清洗流程（`dom_cleaner: skyvern` / `skyvern_hit_test`），所以两个成员都带清洗后的元素树，`click` 都按元素序号；差别只在模型还看到什么：

| 成员 | 模型看到 | 动作 |
|---|---|---|
| `skyvern.dom` | 元素树 | 只有按序号的动作（`click`、`type`、`done`；可选 `scroll`、`send_keys`、`go_back`、`go_forward`、`select_dropdown`、`extract_content`） |
| `skyvern.dom_vision` | 元素树 + 截图 | 同上，另有可选的 `click_coordinate`（够到元素树给不出序号的目标，如跨域 iframe 里的小部件） |

不设 `skyvern.vision`：去掉 DOM 清洗后 Skyvern 已无特有之处，纯视觉由 computer use 一类（UI-TARS 等）覆盖。

- **类与 wiring**（`spectragent/agents/skyvern/`）：`family.py` 是抽象根 `SkyvernAgent`（`__registry_key__ = "skyvern"`，`__abstract__ = True`），持有共有的 `agent_loop`（`react`、`branch`）与 `llm` 条目、全部共有配置字段、系统提示与每步消息的构建、回复的解析与动作历史；`dom.py`（`SkyvernDomAgent`）与 `dom_vision.py`（`SkyvernDomVisionAgent`）各写完整的 `observation` 与 `action` 条目。两个成员的观测都 required `dom`、`screenshot`、`platform`、`task`、`browser_meta`、`history`，optional `step_info`、`capture_state`：`skyvern.dom` 的模型看不到截图，但 trace 要留它、评估器按它打分，所以照样采集（原来截图是 optional）。`skyvern.dom_vision` 另有 `coordinate_space`（默认 `screenshot_pixels`）与 `vision_detail`（默认 `auto`：点是从这张图上读的，`low` 会让 `click_coordinate` 靠猜）。成员给不给模型看截图只在一处说：根的 `_screenshot_detail()` 返回 `None`（不看），`dom_vision` 返回它的 `vision_detail`；系统提示与每步消息都读它。
- **坐标归框架**：`dom_vision` 只声明 `coordinate_space`；本 run 协商到 `click_coordinate` 时框架给动作空间定坐标系（上文"坐标系归框架"），每步的坐标系由 Loop 建，回复里的点由框架换成 CSS px，动作历史里的动作经 `fact_in_model_frame`、效果行与指针经 `point_writer` 写在各自那一步的坐标系上。`screenshot_pixels` 时每步写明截图的像素尺寸；本步没有截图时写明"No screenshot could be shown this step."。没有点动作的 run（`skyvern.dom`，或没协商到 `click_coordinate` 的 `dom_vision`）没有坐标系，提示词不教网格，点照旧是 viewport 单位。
- **回复是闭合 schema**（`skyvern/reply.py`）：`prepare` 用本 run 协商出的 descriptor 经 `core/actions/action_union.py` 编出 per-run 回复模型 `{"actions": [{"reasoning": "...", "action": {"browser.click": {"index": 42}}}]}`：每个条目是一个动作和模型对它的理由（Skyvern 按动作给理由，`parse_react_thought` 读它），动作是闭合 union 的一员，只能写本 run 的全限定动作名、带它自己声明的参数。原来的扁平条目（`action_type` + `index` / `text` / `direction` / `keys`）给每个动作都开放那四个键：滚动的 `amount` 写不出，点击却能带上 `text`，参数校验（上文"参数不合 `Params` 的动作是解析错误"）随后把整条回复退回。结束也是一个动作：`done` 照协商出的 descriptor 写（`extracted_content`、`success`），顶层的 `is_done` / `success` / `extracted_content` 删除，Agent 里把 `done` 当特例的代码随之删除。`parse_react_output` 只读校验过的回复实例（provider 校验过的，或框架校验过的文本，下文"结构化回复不合输出模型，由框架措辞"）；构造 `Action` 用框架的 `action_union.action_of`：optional 参数写的 `null` 去掉（与省略相同，capability 的旋钮决定），required 参数的 `null` 保留（`done` 的 `extracted_content` 是 required 且可空，`null` 就是答案，去掉会变成缺参数的解析错误）。
- **动作历史**：每步用户消息里的 `<action_history>` 是 Loop 的 `history` fragment 的尾部，最近 `action_history_window` 个动作，每行一个 JSON：`{"action": ..., "result": ...}`，`result` 由共用的 `agents/rendering/action_effects.py::result_facts` 写，与 restored 历史（`restored_history_message`）同一份事实；抛异常的那个动作写成 `{"action": ..., "raised": <错误>}`；因同一步更早的动作失败而没执行的动作写成 `{"action": ..., "not_executed": "action N failed"}`（用户 2026-10-24 的裁决）。动作返回的内容（`extract_content` 的文本）因此到了模型那里：原来那一行只写 `name(params) -> ok`，端到端诊断里模型抽取了同花顺的 K 线区域却只读到"ok"，随即以"已成功抽取"结束（假成功）。返回内容按 `max_result_chars`（默认 4000）截断，截断处写明 `... [N of M characters shown]`；`result_facts` 与 `restored_history_message` 的 `max_content_chars` 参数是这个上限的同一个入口，Skyvern 在 live 与 restored 两处传同一个值，所以两边读起来一样（不传时不截断，BrowserUse 不变）。
- **提示词说实话**：系统提示原来说"动作按顺序执行，在第一个失败或改变页面的动作处停下"，而当时的 Loop 在 `success=False` 时照样执行后面的动作，只有 `done` 或抛异常的动作才停下；又说"序号每步重新分配"，而序号是稳定的 backend 节点 id（`dom/models.py::element_handle`），元素在页面上就一直是那个序号。2026-10-23 写成每个动作即使前一个失败或改变了页面也会执行；用户 2026-10-24 的裁决之后写成：动作按顺序执行，动作之间不重新读页面；动作失败，这一步就停下，后面的动作不执行，动作历史写明是哪几个；改变页面的动作不让这一步停下，所以可能改变页面的动作放在一步的最后；序号步与步之间不重新编号，不在树里的元素用旧序号也够不到。`<finishing>` 一节说明动作的结果下一步才在动作历史里看到，答案要用到某个结果时不要与那个动作同一步结束；`<step_rules>` 说明动作历史每一行的字段（`success` 只表示动作执行了；没执行的动作带 `not_executed`）。
- **有界记忆**（发布配置 `configs/skyvern_agent_local.yaml`）：每步用户消息都带一整棵元素树（实测 20–33K tokens），`memory: simple, max_messages: 200` 在约第 6 步超过 gpt-4o 的 128K 上下文。发布配置改为 `memory: { memory_id: sliding_window, window_size: 3 }`（system 之外只留最后 3 条消息：通常是上一步的观测与回复和本步的观测，至多两棵树），较早的动作由动作历史窗口携带；另设 `dom_max_serialized_chars: 200000` 作为单棵树的预算（实测页面 56–150K 字符，都不被截），更大的页面在行边界截断并在树里写明。

这次改动之前录下的 Skyvern run（`agent_id: skyvern`，回复是扁平条目）不能用 `history_source: source` 分叉、不能导出，也不能按新 `agent_id` 读出；不设兼容路径，重录（测试数据可丢弃）。

#### Loop 驱动示例（weave + agent.run）

```python
class ReactLoop(AgentLoopBase):
    __registry_key__ = "react"
    __required_slots__ = {"build_react_prompt", "parse_react_output"}
    __optional_slots__ = {"get_react_output_model", "prepare",
                          "format_react_output", "parse_react_thought"}

    async def _execute(self, agent, task, *, ctx, **_):
        # weave() 把 deps 放进 agent.run 的闭包，每次调用经 ctx 传进来
        env = ctx["env"]
        llm = ctx["llm"]
        memory = ctx["memory"]                     # agent.prepare 的种子消息已由 AgentRuntime.execute 写入
        act_fn = ctx["act_fn"]                     # guarded_act，见 4.5 节
        negotiated_obs = ctx["negotiated_obs"]

        for step in range(self.max_steps):
            # AOP hooks 在方法调用时自动触发 — Loop 完全不知道有 hook 存在
            obs = await env.observe(negotiated_obs)
            prompt_context = ReactPromptContext(step=step, phase="policy", policy_step=step)
            memory.add_messages(await agent.build_react_prompt(obs, context=prompt_context))
            completion = await llm.ainvoke(memory.get_context_messages())
            actions = agent.parse_react_output(completion.completion)
            for action in actions:
                result = await act_fn(action)
                if result.is_done:
                    return ...
```

**Loop 反向"安装"到 agent**：调用方拿到的不是 `loop.run(agent, ...)`，而是 `agent.run(task)`——`AgentLoopBase.weave(agent, **deps)` 在执行前把 `agent.run` monkey-patch 成一个调 `loop._execute(agent, task, ctx=...)` 的闭包，deps 只在这个闭包里。这样从外部看 agent 是一个自包含的可执行单元（`await agent.run(task)`），而 Loop 只是它内部的"编排策略"——这与"Loop 本质就是在调度 agent 的 method"的语义最贴近。

**关键设计：AOP 透明织入**。AgentRuntime 通过 Weaver 将 hook 拦截器织入 agent/env/llm 和作为 loop 角色的 StepRecorder 方法调用中。Loop 收到的是包装后的依赖，照常调用其方法；代理按 hook 的声明触发 handler。Loop 代码中零 hook 感知，零 emit 调用。

**AOP 拦截点与 Loop 的对应关系**：

| 拦截点 | advice | 触发时机 | 典型 Hook 用途 |
|--------|--------|---------|---------------|
| `env.observe` | after | 每步 observe 返回后 | 可选的环境观测 hook；当前 TraceWriter 不拦截此方法 |
| `llm.ainvoke` | after | 每次 LLM 成功返回后 | 可选成功通知；统一调用账由 around 单独负责 |
| `llm.ainvoke` | around | 每次 LLM 调用：返回、抛异常或被取消 | TraceWriter 在 `proceed()` 之前登记这次调用，按结局记为 completed / failed / cancelled，并测量等待时间；失败回复携带的用量保留在同一本调用账 |
| `loop.execute_step` / `loop.observe_terminal` | around | 进入实际步骤执行或终止观测 | 按稳定步骤身份开始计时；检查点与随后同编号策略步共享起点 |
| `loop.record_step` | around | Loop 交出完整 StepRecord | 截止步骤执行计时、封存本步调用与所选证据；有界后台任务经 WAL 持久化，manifest 是提交点 |
| `env.act` | around | 每次 action 执行：返回、抛异常或被取消 | TraceWriter 记下这次调用的起止墙钟与结局，归到正在执行的步（`StepRecord.action_spans`，§5.4）；无环境的 run 不声明此拦截 |
| `agent.build_react_prompt` | around | prompt 构建时 | 异步提示贡献与审计；`await proceed()`，显式 `ReactPromptContext` 描述历史与策略阶段 |

> **与 emitter.dispatch() 的权衡**：AOP 方案让 Loop 代码完全干净（零横切逻辑），但只能拦截实际存在的方法调用。如果某些 Loop（如 PlanExecuteLoop）需要发出不对应任何方法调用的语义事件（如 `plan_generated`），可以将其建模为 agent Protocol 方法（如 `agent.parse_plan()`），AOP 自然覆盖。

#### Loop 输入：`__loop_inputs__`（M31）

有的 Loop 每个任务都要一份只属于它的输入，例如 `BranchLoop` 要知道从哪个源 run 的第几步分叉。它不是环境 setup context（environment 按 `setup()` 签名路由那一份，谁都不接受的键在 `prepare()` 就报错，§4.3），也不是 agent 的领域知识，所以有一条自己的、有类型的通道：

```python
@dataclass(frozen=True)
class LoopInput:                     # core/loops/_registrar.py
    type: type                       # 值的类型，prepare 时检查
    required: bool = True
    description: str = ""

class BranchLoop(ReactLoop):
    __loop_inputs__ = {"checkpoint": LoopInput(Checkpoint, description="源 run 目录 + 检查点步")}
```

- **声明**：`AgentLoopBase.__loop_inputs__: ClassVar[Mapping[str, LoopInput]]`，默认为空，`ReactLoop` 不接受任何输入。
- **校验**：`AgentLoopBase.check_loop_inputs(inputs)` 把多出来的键、缺的必需键、类型不对的值一次报全，抛 `IncompatibleLoopInputError`（`core/loops/validation.py`，点名 loop 与每个键）。`AgentRuntime.prepare()` 在 slot 校验之后立刻调它（早于协商与织入），`weave()` 再调一次（weave 是公开方法，直接调用的人得到同样的错误），与 slot 校验的形状相同。所以把一个检查点交给 `react`，会在装配期直接报错，不会从头跑一遍。
- **送达**：校验过的输入以只读映射放进 weave 的 deps（`ctx["loop_inputs"]`）。Loop 实例上不存任何东西。
- **来源**：`TaskLoader.get_task_context(id)` 返回 `TaskContext{env, loop, branch}`（§4.10），`BenchmarkRunner` 把 `loop` 部分作为 `AgentRuntime.execute(loop_inputs=...)` 送进来。`prepare()` / `execute()` 原先那个把任意关键字原样转给 `weave` 的 `**kwargs` 通道已删除，由这条有类型的通道取代：拼错的关键字现在是 `TypeError`，不是 `ctx` 里一个没人读的键。

#### 一个 run 与一步：执行状态与公开步骤边界

`LoopRun`（`core/loops/run.py`）只保存一次执行的依赖与执行事实：Runtime 解析一次的 `RunIdentity`、env、llm、memory、守卫动作入口、协商、步骤接收器、已交步骤、实际动作历史和答案。每次 `_execute` 新建，Loop 实例没有 `ctx`。模型调用数、token 账本和总时钟均不属于 Loop；`ReactRun` 在 ReAct 层保存连续解析失败状态，Branch 的 run 在其上扩展恢复状态。

`AgentLoopBase` 提供通用的 `surrender`、`surrender_vetoed`、`end_step`、`build_result` 及终止观测的已定结局传递。所有路径通过 `self.surrender`，扩展 Loop 覆写同一个公开方法即可覆盖正常与失败交接。记录组件内部错误由该组件隔离，不能改变执行结局；外部取消仍原样传播，已定结果或否决由 `carry_outcome` 随取消传递。没有独立统计组件时，结果 usage、模型调用数均为 `None`。

`ReactLoop.execute_step(run, step_idx, *, observation=None, restoration=None)` 是公开的 ReAct 单步边界：observe → prompt → LLM 与步内重问 → 读回复（`read_reply`：结构化回复的框架校验、Agent 解析、点的换算与参数校验）→ 动作截断 → act → 交出步骤。Runtime 给 run 的 `step_executor` 是 Loop 的 AOP 代理，驱动代码经此接口执行，记录层可围绕步骤和交出边界测量；Loop 不实现 stopwatch 或 emitter。已有观测直接用于检查点，不增加 observe cycle。解析重试耗尽的步骤写 `error_phase="parse"` 和原始解析错误；解析成功但没有动作仍是正常空动作步骤。

**参数不合 `Params` 的动作是解析错误（用户 2026-10-23 的裁决）。** 回复解析出的动作里，会执行的那些（`max_actions_per_step` 以内）在执行之前逐个按它在本 run 被守卫执行成的那个动作的 `Params` 校验（`core/action_guard.py::params_refusal`，`ReactLoop.check_action_params` 调它）：缺必填参数、类型不对、`Params` 没有声明的参数（`extra="forbid"`）。有一个不合，整条回复就是一个 `ParseError`，原因逐个写明是第几个动作、哪个参数、错在哪，后面附上那个动作在本 run 接受的参数（它的 descriptor 的 prose），所以模型知道怎么改；它走现有的解析重试（`parse_retries_per_step`），用完之后照解析失败记录（`error_phase="parse"`，不执行，`actions` 为空）。不合的动作从不执行，也不变成失败的 `ActionResult`：一条回复要么全部按声明的参数执行，要么退回去重写，与 UI-TARS 的"一行读不出整条回复失败"同一个道理。校验放在 ReAct 而不是守卫的执行闭包里：守卫在执行时回答的是 `ActionResult`，而这条裁决要的是模型重写，解析重试本来就归 ReAct（§2.3）；判断"参数合不合声明"本身写在守卫旁边（`params_refusal`，与 `resolve_action_name` 同一个模块），任何 Loop 都可以调。本 run 不执行的动作名（守卫会拒绝的名字，`resolve_action_name` 为 `None`）同样在这里判（2026-10-24，随下面"失败的动作停下这一步"一起定）：原因写明第几个动作、它不是本 run 执行的动作，并列出本 run 的动作；这样一条回复里未知动作之后的动作不会在"它做成了"的假设下执行，模型在同一步里就改写，不浪费一步。只有本 run 宣告了动作（有 descriptor）时才判名字：没有环境的 run 没告诉模型任何名字，守卫照旧拒绝它收到的一切。守卫在执行时仍拒绝未知名字，那是不经过这道检查的调用方（恢复器重放录下的动作）的最后一道关。分叉按源历史重建时，录下的每条回复经同一个检查读回（`BranchLoop` 与实时步都经 `ReactLoop.read_reply` 读回复，它在解析之后调 `check_action_params`），所以源 run 当时退回重写的回复读回来仍是那次解析失败。没有注册 `Params` 的动作（按任务生成的工具）没有可校验的声明。Loop 从 `LoopRun.action_space`（Runtime 经 weave 交给它的、守卫执行的那个 `ActionSpace`）知道本 run 的动作。

**结构化回复不合输出模型，由框架措辞（2026-10-26）。** 有输出模型的 Agent（BrowserUse、Skyvern 两个家族）的回复由 provider 按本 run 的输出模型校验（`ChatModelBase.validate_structured`）；不合的回复抛 `StructuredOutputValidationError`，带着校验器看到的原文（`raw_output`）。原来 ReactLoop 把这段原文交给 Agent 的 parser，parser 用同一个模型再校验一次，把 pydantic 的报错原样交给模型：每个错误带文档链接与 `input_value`，写法与上面的参数校验不同，同一个错误写在文本协议的 Agent（UI-TARS）那里是一行带定位的说明，写在结构化 Agent 这里是一大段报错，parser 也没加任何领域知识。现在由框架措辞：`core/action_guard.py::reply_refusal(space, output_model, text)` 只读回复原文、输出模型与本 run 的动作空间（与 `params_refusal` 同一个模块、同一个口吻），所以实时步与分叉重读录下的回复得到同一段文字。原文不是 JSON 时是一行，带解码器给的位置（`the reply is not valid JSON: expected value at line 1 column 1`）；否则以 `the reply does not fit the output schema: ` 开头，每个错误都定位：动作条目按它在动作列表里的序号，写法与参数校验相同（`action 2, browser.type: text: Field required (browser.type takes index: …, text: …)`），未知动作名与名字检查相同（`action 1, browser.fly is not an action this run executes (its actions: …)`），形状不对的条目写明它是什么（`action 1, a string: each action is an object with exactly one key, …`），其他字段写它的路径（`next_goal: Field required`、`actions[0].reasoning: …`）。动作条目的错误按类型认，不读消息：闭合 union 的 `BeforeValidator`（`core/actions/action_union.py`）抛 `PydanticCustomError`，类型是 `action_shape` / `action_unknown` / `action_params`，`ctx` 带动作名与每个不合的参数，`loc` 里最后一个列表下标就是第几个动作（Skyvern 的 `actions[i].action` 嵌套也一样）；一个动作的几个参数错误合在它的一个错误里，不按 union 分支各报一遍。它走现有的解析重试，用完照解析失败记录，与参数不合同一条路；步内重问与下一步看到的是同一段文字（`parse_feedback`）。

于是 Agent 的 parser 只读校验过的回复。`ReactLoop.read_reply(run, reply, *, frame, output_model)` 是 ReAct 读一条回复的唯一入口，实时步与 `BranchLoop` 的 source 历史都经它：有输出模型的 run 里以文本到达的回复（provider 拒绝的那段原文，或调用方手里的文本，例如训练导出里学生 `format_react_output` 写出的回复）先由框架校验（`core/loops/react.py::validated_reply`），不合就是上面的 `ParseError`、不交给 Agent，合就交给 Agent 一个模型实例；然后 Agent 的 `parse_react_output` 把动作读出来，框架换算点、做名字与参数检查。BrowserUse 与 Skyvern 的 `parse_react_output` 只把本 run 输出模型的实例映射成动作（收到别的是调用方跳过了框架的校验，`TypeError`）；没有输出模型的 Agent（UI-TARS）照旧读自己的文本。不为 Agent 开自定义反馈的 slot：现在没有 Agent 需要。**这次改动之前录下的 run**，凡是有一步含结构化回复的解析失败，录下的 prompt 里是 Agent parser 原来的写法（pydantic 的报错），`history_source: source` 重建到那一步之后的 prompt 比较不再相同，分叉被拒（`IncompatibleCheckpointError`）；不设兼容路径，重录（测试数据可丢弃）。

**一步里有动作失败，这一步就停下（用户 2026-10-24 的裁决）。** ReAct 按回复的顺序执行 `max_actions_per_step` 以内的动作；一个动作的 `ActionResult.success` 为 false，这一步就停在它这里，后面的动作不执行（`ReactLoop.execute_step`）。原因：一步里后面的动作是假定前面的做成了才计划的。一次 15 个任务的录制里有 17 步是一个动作失败、后面的照样执行，其中 12 步是"`type_coordinate` 因为没有文本框获得焦点而被拒，接着 `send_keys` 回车"：teacher 把"输入，然后回车"写成一步，点击没点中输入框，回车按在没有焦点的页面上，爱奇艺于是搜了它轮播的占位词，进了一个错的页面，之后的步骤还取决于时机。

- **什么算失败**：结果的 `success` 为 false，不论是 capability 的失败还是守卫的拒绝（§4.5"`guarded_act()`"）：守卫拒绝也是这个动作没做成，后面的动作同样是按它做成计划的。ReAct 自己不会把本 run 不执行的名字交给守卫：那样的回复在执行任何动作之前就退回模型重写（上文"参数不合 `Params` 的动作是解析错误"），所以一步里被守卫拒绝的动作只会出现在没有宣告动作的 run 里。抛异常仍然结束 run（`error_phase="act"`）；`done` 仍然结束 run，它之后的动作不执行，这不算跳过：run 已经结束，没有下一步要告诉。
- **不是错误**：停下的一步照常交出，没有 `error_phase`，run 继续下一步，下一步重新观测。终止观测、步数与解析失败的连续计数都不受影响。
- **怎么记**：没执行的动作不得到伪造的 `ActionResult`。`StepRecord.actions` 照旧是这一步接受的整条回复（截断之后的），`action_results` 只有真的返回了的结果，最后一个就是失败的那个；新字段 `StepRecord.skipped_actions: int = 0` 数 `actions` 末尾没执行的几个（`StepRecord.skipped()` 取出它们）。模型校验：它大于 0 时最后一个结果必须是失败，且 `len(actions) == len(action_results) + skipped_actions`。没执行的动作留在 `actions` 里，因为它们是这一步接受的回复：`history_source: source` 的分叉按录下的 `actions` 比对重新解析出的回复，训练导出写的是 teacher 的整条回复。它们没经过守卫，所以 Loop 用守卫自己的规则（`core/action_guard.py::executed_name`）把它们改成全限定名，与执行过的动作一样；守卫会拒绝的名字照 agent 写的原样，与被拒绝的动作一样。
- **trace 格式不升**：这是一个带默认值的字段（§4.4 的演进规则，BACKWARD ✓）。缺少它读成 0，即"什么都没跳过"，这对这次改动之前录下的每一步都是实话：那时的 Loop 在失败之后照样执行后面的动作，`action_results` 里每个都有结果。所以这不是兼容路径：旧形状没有第二种解读，也不做转换；旧录制照常可读、可分叉、可导出。
- **模型下一步看到什么**：每个 Agent 按自己的历史格式写，原因是同一句 `action N failed`（`agents/rendering/skipped_actions.py`）。BrowserUse 在那一步的 `<results>` 之后加 `<not_executed>Action 2 (browser.send_keys) was not executed: action 1 failed.</not_executed>`（参数在同一步的 `<actions>` 里）；Skyvern 的动作历史给每个没执行的动作一行 `{"action": ..., "not_executed": "action 1 failed"}`；UI-TARS 的上一批反馈加 `"not_executed": {"actions": [...], "because": "action 1 failed"}`，这些动作不再算进 `actions_without_returned_results`，那里只剩可能执行过、没有返回结果的。没执行的动作的点同样由框架写在那一步记录的坐标系上（`fact_in_model_frame`，上文"坐标系归框架"）。什么都没跳过的步没有这些内容，提示词与以前逐字相同。系统提示：BrowserUse 的 `system_prompt` / `system_prompt_no_thinking` 与 Skyvern 的系统提示写明失败就停下（上文"BrowserUse 家族""Skyvern 家族"）；UI-TARS 在 `max_actions_per_step` 大于 1 时的动作合同写 "They run one after another in the order written, all aimed at the screenshot you are looking at now, until one fails: the actions after a failed one do not run, and the next observation says which. You see the screen again only after the batch stops."，一步一个动作时没有可停的，措辞不变。
- **代价**：系统提示变了，这次改动之前录下的 BrowserUse、Skyvern 与 `max_actions_per_step` 大于 1 的 UI-TARS run 不能再用 `history_source: source` 分叉（种子检查不符），用 `restored` 或重新录制；一步一个动作的 UI-TARS 录制不受影响。
- **分叉、导出与读者**：恢复只执行源执行过的动作，`restored` 历史怎样写没执行的动作见下文 `BranchLoop`；训练导出见 §4.15"可导出的步与 `CanonicalStepSample`"；评测器的 `TraceView.action_history` 不列没执行的动作（它们没有发生）；查看器的动作标记把它们列出、注明"没有执行"、不画在图上（§4.17"动作标记"）。

ReAct 的公开 `read_reply`、`cap_actions`、`remember_rejected`、`remember_reply`、`today_iso` 与 `loop_fragment_builders` 服务实时执行、分叉历史重建及冻结导出；相应旧私有入口删除，不转发。通用观测和消息读回在 `core/trace/reconstruction.py`，恢复层仅保留 `SourceRun` 分叉包装。通用读侧不反向依赖具体 Loop。

**步数只数策略步。** `AgentMetrics.steps` 数 `origin == "policy"` 的记录，恢复步和终止观测不占预算、不加入 Agent 的动作历史，也不继承源运行 token。完整轨迹的步级用量由统一记录组件汇总本步全部调用后填写。

**agent 的任务结论：与结束动作的执行分开（用户 2026-10-23 的裁决）。** 结束 run 的动作（`browser.done`）的 `ActionResult.success` 与其他每个动作一样，只表示它执行了：run 因此结束，不表示任务完成了。agent 对任务说了什么是另一件事实，由结束动作的结果单独携带：`ActionResult.reported_success`，`true`（完成了）、`false`（没有完成）或 `null`（没有说）。只有 `is_done` 的结果能带它（`schemas/action.py` 校验）；没有结论时这个键不写，所以其余动作的记录与以前逐字节相同。Loop 把它照抄进 `AgentResult.agent_reported_success`（`react.py`）；没有调用结束动作的 run（超时、`max_steps`、出错）也是 `None`，这条约束不变。它是 agent 的自报，不是判定：判定只来自 Evaluator 的 `EvalResult.predicted_label`（§4.11）。结论从 agent 那里来，各 Agent 照同一个合同：

- **BrowserUse**：模型经 `done` 的 `success` 参数说。参数说明是 "whether the task was accomplished; leave it unset if you cannot tell"，以前只有类型；写 `null` 与不写一样（Agent 在建动作之前去掉 null），是没有结论。系统提示照同一个三分法教（`system_prompt.md`、`system_prompt_no_thinking.md`、`system_prompt_flash.md`，用户 2026-10-23 的裁决）：已知有要求没做到、或被没解决的阻断错误挡住，写 `false`；每条要求都核实做到了，写 `true`；确实说不准，不写。上游原文把"不确定、无法核实"也教成 `false`，那样说不准的结果读起来就成了已知的失败；其余上游原文与语气不变。三个 `is_browser_use_model` 模板不教 `success`，只有动作表里的参数说明。这三个模板在这次改动之前的录制同样不能用 `history_source: source` 分叉（种子检查不符），用 `restored` 或重新录制。回归：`test_browser_use_prompt_action_vocabulary.py` 的 `test_teaches_done_s_success_as_the_action_reads_it`。
- **Skyvern**：与 BrowserUse 相同，经 `done` 的 `success` 参数说（`done` 是闭合回复里的一个动作，上文"Skyvern 家族"）；不写或写 `null` 是没有结论。系统提示的 `<finishing>` 照三分法教：做到了 `true`，做不到 `false`，说不准不写。原来的顶层 `success`（默认 `null`）随顶层字段一起删除。
- **UI-TARS**：`finished(content='...')` 没有表达结论的参数，模型也用它说做不到（一次 LexBench 录制的任务 18，答案以"无法完成"开头），所以结论是 `None`，不再硬写 `success=True`。动作空间与动词写法不变；系统提示里 `finished` 那一块的 `executes:` 行不再列出它设不了的 `success`（只差 `, success: bool (optional)`），所以这次改动之前的 UI-TARS 录制不能用 `history_source: source` 分叉（种子检查不符），用 `restored` 或重新录制。训练导出里，UI-TARS 学生写不出任何结论：teacher 说完成了的结束步写成它的 `finished(...)`、样本注明结论没写出，说没完成的仍不可译（§4.15 round-trip 第 4 条）。结论参数由动作声明：`browser.done` 的 `success` 带 `TaskClaim` 标记（`core/actions/params.py::claim_params`），它是动作里唯一不指挥环境的参数，导出按它认出结论，不按参数名。

原因：以前结束动作的 `success` 同时承担"执行了"与"agent 说完成了"两层意思。UI-TARS 的每个 run 都"自报成功"，包括说自己做不到的；一个 `done(success=false)` 在 trace 里记成一次失败的动作：执行错误（`StepRecord.execution_errors` 的 "action reported failure"）、训练导出的 `labels.step_status: action_failed`、动作标记的"（没有成功）"。不用动作效果（§4.5）承载结论：效果是动作执行前后测得的事实，`describe()` 不含任务判断，而结论正是 agent 的任务判断；它与答案 `extracted_content` 一样，是结束动作交出的内容。`AgentResult.schema_version` 因此升为 `4.0`，3.0 的结果在读取时被拒绝（"predates AgentResult schema 4.0"），不做转换；trace 格式版本不变（缺少 `reported_success` 就是没有结论，与动作效果同一条规则），旧录制按测试数据可丢弃的裁决重新生成。

**执行限制显式传递。** Loop 的 `execution_limits() -> ExecutionLimits` 是唯一来源；Agent 用 `__execution_limit_fields__` 声明消费字段（显式空集表示不适用），通过原有 `prepare(..., execution_limits=...)` 收到有类型的限制。Runtime 在 `prepare()` 检查提供与消费及显式参数是否匹配，失败先于 setup 和模型调用。未声明不能静默省略限制。Agent 自己决定系统提示和每轮提示的布局，Runtime 不拼领域提示。观测的格式说明走同样的形状：Agent 用 `__fragment_formats__` 声明提示词引用哪些 fragment 的格式（用 wiring 里的名字），经 `prepare(..., fragment_formats=...)` 收到提供方的原话（§4.4 “观测格式由提供方描述”）；不声明的 Agent 签名不变。

**UI-TARS 的稳定上下文与执行反馈。** Task 只写入 `prepare` 生成的 system 种子；首次观测的实测 `browser.platform` 另生成一次 system 消息，后续轮次不重复 Task 或平台段。每次 `prepare` 重置平台声明，因此执行、source/restored 历史重建及从非零步开始的独立导出段都得到自己的平台上下文；同一执行的平台变化明确拒绝，不能使用旧值继续。平台仍经环境 fragment 提供、记录，不从宿主机推断。

UI-TARS 要求现有 `loop.history`，每轮只把其中上一批动作及逐项返回结果渲染到 user 观测消息，与本轮截图和步数一起交给模型。返回结果按动作次序配对；没有返回结果的动作单列，不能伪造成功；因同一步更早的动作失败而没执行的动作另列 `not_executed`，带原因（上文"一步里有动作失败，这一步就停下"）；截断数量、错误阶段及错误保留。`typed_into_sensitive_field` 不进入提示。`success` 是执行器结果，不是页面目标实现的断言。单动作 `loop.action_result` 不扩展成另一份批次协议；Loop 不负责拼提示文字。

**UI-TARS：胶水 agent 与无状态编解码**（用户 2026-10-23 的裁决：agent 是胶水；导出与分叉调用无状态的模型编解码）。UI-TARS 分成两个模块：

- **Agent**（`agents/ui_tars/agent.py::UITarsAgent`）：`__wiring__` 声明观测与动词对应的动作（相对名），`coordinate_space` 声明模型写点的网格，`prepare` 构建系统提示（模板 `computer_use` / `grounding` / `mobile_use`；Available Actions 由编解码绑定到本 run 的动词生成，签名取动词的、说明与参数类型取 descriptor 的；输出合同按 Loop 的 `max_actions_per_step`；Coordinates 段按 `coordinate_space`），`build_react_prompt` / `build_restored_history` 构建每步消息。四个 ReAct slot（`parse_react_output`、`parse_react_thought`、`format_react_output`、`format_action_fact`）各一行，调编解码。Agent 的状态只剩本 run 的编解码、系统提示快照，以及本次执行第一次观测声明的平台。
- **编解码**（`agents/ui_tars/codec.py`）：UI-TARS 模型训练时的文本协议，回复文本 ↔（thought，模型网格上的框架动作列表），以及动作 → 模型文本。**每个动词一条声明**（`VERBS`）：动词名、它执行的动作（相对 id，经本 run 的绑定解析，`executed_descriptor`）、按提示教的顺序列出的关键字参数（教的写法、UI-TARS 模型的其他写法如 `start_box` / `end_box`、值的种类 `point` / `text` / `choice` / `keys`、提示里的示例值、填哪个动作参数、是否必填）。同一条声明驱动读（回复 → 动作）、写（动作 → 调用）与提示里的签名，取代原来十四个手写的读写函数。**点参数不写参数名**：一个点参数按顺序填它的动作声明的点（`ActionDescriptor.point_params`，§4.5"参数的含义"），所以哪些参数是点只在动作上写一次；动词写的点数与动作声明的点数不同时，编解码在建的时候（`prepare`）就拒绝（`ValueError`，点名动词与动作），否则这个 run 会把网格上的点当成 capability 的单位执行。**任务结论**：`finished` 没有表达结论的参数，读出的动作没有结论（`reported_success` 为 `None`），带结论（true 或 false）的动作写不出来；说完成了的结论由导出层去掉（§4.15 round-trip 第 4 条）。动作参数里没有任何参数填的（`type` 的 `clear`、`scroll` 的 `amount`、`wait` 的 `seconds`）同样写不出来，抛 `UnrepresentableActionError`。
- **无状态。** `UITarsCodec(space, writes_thought=...)` 是不可变对象，Agent 在 `prepare` 里按本 run 的有效 `ActionSpace` 建一次（`grounding` 模板不教 thought，所以不写 thought）：`read_actions(text)`、`write(actions, thought=...)`、`write_call(action)`、`write_fact(action)` 只读参数与这个对象，任何顺序、任何人调用都一样；`read_thought(text)` 是模块函数，不需要 run（导出在 teacher 的类上调 `parse_react_thought`）。`prepare` 之前的编解码不绑定任何动词（每个动词都是 unknown verb）。点始终是模型网格上的数，换算是框架的（上文"坐标系归框架"）。
- **谁调用。** 实时 Loop、`BranchLoop` 重放源回复（经 Agent 的 `parse_react_output` slot，与实时步同一个入口、同一个 AOP 边界）、训练导出（学生的 `format_react_output` / `parse_react_output` / `format_action_fact`）都经 slot 调到这同一个编解码。导出与分叉不直接 import 编解码模块：它们对任何 agent 都成立（BrowserUse、Skyvern 用各自的格式解析），slot 已经只是一行调用，绕过 slot 只会把 UI-TARS 写进框架；导出包冻结（§4.15）。

**坐标系归框架**（用户 2026-10-23 的裁决：Agent 是胶水，坐标系属于框架）。capability 以自己的单位执行并记录点（浏览器为 viewport 的 CSS px）；模型在截图上读点，写在它自己的网格上（截图的像素，或截图上的 0–1000，`schemas/observation/geometry.py::CoordinateSpace`）。Agent 只声明它的模型在哪个网格上写点，即它的 `coordinate_space`（`core/loops/coordinate_frames.py::DeclaresCoordinateFrame`），其余都是框架的（`core/loops/coordinate_frames.py`）：

- **run 的动作空间。** `AgentRuntime.prepare` 在守卫建好动作空间之后，按 Agent 的声明给它定坐标系（`framed_action_space`）：空间里有点动作（descriptor 有 `point_params`，§4.5"参数的含义"）时，`ActionSpace.coordinate_space` 记下这个网格，每个点参数的 descriptor 说明（prompt 的 prose 与 schema 的 `description`，同一行）改写成模型写它的单位，例如 `int -- x on a 0-1000 grid laid over the screenshot whatever its pixel size, from its left edge`。名字、绑定、哪些参数是点都不变，守卫照旧执行 capability 的单位。没有点动作的 run 从不让模型写点，不教网格：`coordinate_space` 为 `None`，点照旧是 capability 的单位（BrowserUse 的 `dom` 模式、`skyvern.dom`）。所以引用 descriptor 的每一处——BrowserUse 的动作表与闭合输出 schema、`params_refusal` 退回给模型的说明（§4.1"参数不合 `Params` 的动作是解析错误"）——都说模型的单位；BrowserUse 自己改写说明的代码（`_coordinate_relabel`、`action_union(relabel=...)`）删除。`ActionSpace` 是实验身份（§4.5），坐标系随它进 run 配置。**定了坐标系的 run 必须有能报几何的截图**（2026-10-23）：`prepare` 定完坐标系立即检查（`coordinate_frames.check_frame_geometry`）：协商出的 `screenshot` fragment（`fragment_bindings["screenshot"]`）的注册类要带 `ScreenshotGeometry` 的四个字段（`image_width`、`image_height`、`viewport_width`、`viewport_height`），否则抛 `ScreenshotGeometryError`（`TypeError` 的子类），点名 agent、网格、本 run 的点动作与那个 fragment 的键和类；本 run 根本没有协商截图时同样拒绝。这是 AGENTS.md §2.3"setup 会拒绝的，`prepare()` 就拒绝"在坐标系上的一条：配置本身决定了这个 run 永远放不下一个点，所以它在 `on_run_start` 开 trace 之前、环境 setup 之前失败，`spectragent bench` 的预检（第一个任务的 `prepare()`）也看得到，不再等到第一步。
- **每一步的坐标系。** Loop 在 Agent 构建本步 prompt 之前，按本步的观测建本步的坐标系（`step_frame` → `schemas/observation/geometry.py::PointFrame`：声明的网格铺在本步给模型看的截图上，即观测的 `screenshot` fragment）。截图量不出尺寸、没有给模型看的图像字节、可选截图采集失败或根本没有截图时，坐标系没有网格（`grid` 为 `None`），这一步的点都没有位置；截图 fragment 不报几何（不是 `ScreenshotGeometry`）是装配错误，由 `prepare` 拒绝（上一条）；`step_frame` 遇到不报几何的截图对象仍以 `TypeError` 停下这一步（`error_phase="prompt"`），只有绕过 `prepare` 构造的观测会走到这里。坐标系经 `ReactPromptContext.frame` 交给 Agent，并记在本步的 `StepRecord.frame` 上；没有定坐标系的 run 两处都是 `None`。
- **为什么记在步记录上，而不是另一个历史 fragment。** `StepRecord` 本来就既是 WAL 的步格式又是 `loop.history` 的载荷（§4.4）。坐标系是这一步的回复（`llm_output`，模型网格上的点）与它的动作（capability 的单位）之间的换算，正是这一步的事实。记在步上，下一步的反馈、分叉、训练导出与 trace 读者都按步读到它，不问 Agent，也不另起一份历史；只放进 fragment 的话 trace 里没有它，分叉与导出只能再去问 Agent 的配置。trace 格式因此升为 7（§5），旧 trace 读取时被拒绝。
- **分叉与导出。** `BranchLoop` 的 source 历史重建用源步记下的坐标系（重建的 prompt 要与源 prompt 逐字相同，所以就是源模型当时看到的那个网格）；restored 历史在本次恢复观测上建坐标系，记在那个恢复步上。训练导出按学生自己的声明、在录下的截图上建坐标系（与实时 run 同一个 `step_frame`；teacher 记下的是 teacher 的网格，不是学生的），交给学生构建 prompt，并记在学生历史里那一步的副本上。
- **进：回复里的点换成 capability 的单位。** Agent 的 `parse_react_output` 只读它的回复，点就是模型写的数（UI-TARS 读出 `click(point='(500, 858)')` 的 500 与 858，四个数的框取中心；BrowserUse 读它闭合 schema 里的整数），不做任何换算。Loop 在它之后、参数校验与执行之前，用本步的坐标系把每个动作陈述的点换成 capability 的单位（`actions_in_capability_units`，即 `PointGrid.to_viewport`）；守卫、动作与 trace 只见 capability 的单位，`llm_output` 留着模型写的原文。换算放在 ReAct 的解析边界，不放在守卫的执行闭包里：放不下的点（本步截图量不出尺寸）是要模型重写的回复，与参数不合 `Params` 同一个道理（上文"参数不合 `Params` 的动作是解析错误"），走解析重试，用完记成解析失败，而守卫在执行时只能答一个 `ActionResult`；参数校验也要校验执行的值（`Params` 里的点是 `int`，模型写的 `10.4` 或框的中心不是），所以必须在换算之后。规则只写在框架一处，与 `params_refusal` 一样任何 Loop 都能调：`BranchLoop` 重放源回复、训练导出读回学生的回复，都调这同一个函数。截图之外的点照旧夹到边上（`to_viewport` 的规则：UI-TARS-1.5 在它放大的输入上会答到边外几个像素，拒绝会白白花掉一次解析失败）；本步截图量不出尺寸时是 `ParseError`，原因写成 `action 1, click_coordinate: a point cannot be placed, because this step's screenshot has no known size`（以前两个 Agent 各说一句，现在框架说同一句）。
- **出：给模型看的每个点写在它所属那一步的坐标系上。** 坐标系从那一步的记录读：上一批动作与它们的效果读那一批所在步的 `StepRecord.frame`，指针读本步的 `ReactPromptContext.frame`，restored 历史读恢复步的坐标系。点由框架写：一个动作当作事实展示时，框架把它的点放到坐标系上（`fact_in_model_frame`），Agent 只按自己的写法写这个动作（UI-TARS 的调用语法、BrowserUse 的 `name(params)` 或 JSON）；效果行与指针的点经 `point_writer(frame)`（交给 `describe(write_point=...)` 与 `render_capture_state(..., write_point=...)`）；导出给学生的目标回复，框架先把录下动作的点放到学生的坐标系上（`actions_in_model_frame`，放不下是 `UnrepresentableActionError`），学生的 `format_react_output` 只管写。所以渲染过去的一步不需要任何 Agent 状态，以任何顺序渲染都一样：原来每个 Agent 各存一份按步号的网格（`StepGrids`）、要求按步序构建 prompt 的规定，以及 `format_react_output` / `format_action_fact` 的调用顺序规定都删除了，`agents/_feedback_points.py` 删除。

**模型看到的每个点都写在它写点的那个网格上**（用户 2026-10-22 的裁决，取代原来的"执行参数的坐标明确为 CSS viewport pixels，模型输出继续使用其配置网格"）。事实不变：trace 里的动作参数、动作效果（§4.5）与 `capture_state`（§4.4）照旧是 capability 的 viewport 单位（浏览器为 CSS px）；变的只是它们怎么呈现给模型：点的写法归框架（上文"坐标系归框架"），消息与动作的其余写法归 Agent（§4.4"观测增强按消费方分层"）。原因是实测：`coordinate_space: normalized_1000` 教模型在 0–1000 网格上写点，而反馈里的每个点都是 CSS px，teacher 模型（deepseek）于是把像素数写到网格上：输入框的像素 y 是 604，它写 `(500, 604)`，在 y=435 的空白处执行；一个金融站点上 25 次失败的文本输入有 15 次按像素读就落在输入框里，越靠屏幕下方偏得越远。这条合同对每个让模型在 `coordinate_space` 上写点的 Agent 都成立：UI-TARS，以及有坐标动作的 BrowserUse（同日跟进，下文"BrowserUse 的反馈网格"）。点由框架写在它所属那一步的坐标系上，两者只写各自的语法。UI-TARS 因此：

- **上一批动作**写成它自己的动作语法，例如 `click(point='(500, 858)')`：与 `format_react_output` 用同一个编解码写法（`UITarsCodec.write_call`），点由框架写在那一批所在步记录的坐标系上（`StepRecord.frame`，即那一批动作所依据的截图），所以模型看到的正是它在那张截图上会写出的那一行。批次的其余事实（每个结果的成败与错误、没有返回结果的动作、截断数量、错误阶段及错误）照旧。这个语法写不出来的动作保留原来的 `{"name", "params"}` 对象，点参数同样换成网格点：没有对应动词、本 run 没协商、参数值写不出，或者点在网格上没有位置（见下）。它们来自训练导出 `keep_as_text` 之后的历史，也来自 restored 历史（retarget 后实际执行的动作与录下的源动作）。哪些参数是点只看动作自己的声明（§4.5“参数的含义”），由框架认（`coordinate_frames.action_points`）：本 run 执行的动作读它的 descriptor 的 `point_params`（相对名经本 run 的绑定，`core/action_guard.py::executed_descriptor`），本 run 不执行的录下动作（导出里 teacher 的动作）读注册的 `Params`（`point_params`），两边都不认识的动作没有点；UI-TARS 的动词表不写哪些参数是点，从不按参数名或取值猜，所以没有标记的 `_x` / `_y` 参数从不被换算。UI-TARS 的调用语法只能把两个数写成点，所以一个点是"没有位置"那句话时，这个动作保留 JSON。哪个动词对应本 run 的哪个动作同样只问绑定（`executed_name`），不按名字的后缀对；本 run 不执行的动作写不成调用，拒绝的说明列出每个动词在本 run 执行成哪个动作。这一写法是 Agent 的公开可选 slot `format_action_fact(action)`（上文"可选 slot"；一行，调编解码的 `write_fact`），导出的 `keep_as_text` 文本轮也用它写。
- **效果行**由 `describe(write_point=...)`（§4.5）把它陈述的每个点写在同一个坐标系上（`write_point` 是框架的 `point_writer(那一步记录的坐标系)`）；**`capture_state` 的指针**由 `render_capture_state(observation, write_point=...)`（§4.4）写在本步的坐标系上（`point_writer(context.frame)`）。本轮没有可测尺寸的截图时（这一步模型本来也写不了点），指针写成 `a point not placeable (no screenshot of known size)`。
- **restored 历史**（下文"restored 历史"）里实际执行的动作和源材料里录下的动作，都写在本次恢复观测的坐标系上（`ReactPromptContext.frame`，也记在那个恢复步上；每个恢复步的动作就是在这张截图之后执行的）；原来的 "Executed coordinate parameters use CSS viewport pixels." 前缀删除。源回复 `recorded_reply` 本来就是 UI-TARS 在网格上写的原文，现在与录下的动作同一个网格。
- **系统提示**删除 "Execution feedback coordinates are CSS viewport pixels; write new action points on the output grid specified above."，改为一句：执行反馈里的点与模型写的点在同一个网格上。`screenshot_pixels` 的网格就是图像像素，所以同一条路径写出的是图像像素：DPR 1 时数字与以前相同；DPR 2 时是 CSS px 的两倍，这也修正了以前反馈写 CSS px、模型写图像像素的不一致。
- **距离不是点**：滚动的位移与滚动位置照旧是 viewport 的 px，位移写明单位（§4.5）。

**BrowserUse 的反馈网格**（2026-10-22，同一裁决）。BrowserUse 只在本 run 有坐标动作（`click_coordinate`，只有 `browser_use.dom_vision` 声明它）时才把 `coordinate_space` 教给模型：系统提示的 `<coordinates>` 段与坐标参数的说明都只在那时出现。判断就是框架有没有给本 run 的动作空间定坐标系（`ActionSpace.coordinate_space`，即本 run 的动作里有没有坐标动作），与带不带 `<coordinates>` 段是同一个条件；`browser_use.dom` 不声明坐标动作，所以它的 run 总是第二种：

- **有坐标动作的 run**：`<agent_history>` 里每一步的 `<actions>` 仍是 BrowserUse 自己的 `name(params)` 写法，动作陈述的点参数由框架写在那一步记录的坐标系上（历史窗口有多步，每步读它自己的 `StepRecord.frame`）；`<effects>` 的效果行经 `describe(write_point=...)` 写在同一坐标系上（按编号的 `click` 的落点也是点，编号本身不是）；`<browser_state>` 里 `capture_state` 的指针经 `render_browser_state(..., write_point=...)` 写在本步的坐标系上；`<llm_output>` 是模型自己的回复，本来就在网格上。restored 历史里实际执行的与录下的源动作写成动作的 JSON，点参数由框架换成本次恢复观测坐标系上的点；原来的 "Executed coordinate parameters use CSS viewport pixels." 前缀删除（没有坐标动作的 run 也删：那里没有坐标参数可说）。`<coordinates>` 段加一句 "Every point in the agent history and the browser state is on this same grid."。`screenshot_pixels` 写的是图像像素，DPR 2 时是 CSS px 的两倍，与模型写的点一致。没有可测尺寸截图的步、截图之外的点照下文照实说。
- **没有坐标动作的 run**：模型不写点，也没被教任何网格，提示里只有一套坐标，所以反馈照旧是 viewport 单位：效果行写 `(x, y)`，指针写 `(x, y) in viewport pixels.`，每步的提示词不变（restored 历史只少了上面那句前缀）。`coordinate_space` 在这样的 run 里不起作用（字段说明写明）。

BrowserUse 不实现 `format_action_fact`：这个 slot 只有训练导出读，而 BrowserUse 没有 `format_react_output`，做不了学生；它的两种写法（历史的 `name(params)`、restored 的 JSON）是 Agent 私有的。哪些参数是点同样只看动作的声明，由框架认（BrowserUse 不另列坐标动作的名单；本 run 有没有坐标动作读框架给动作空间定的坐标系，点参数的说明已由框架写成网格单位，上文"坐标系归框架"），两边都不认识的动作没有点。

**较早一步的坐标系从哪里来：那一步的记录。** 渲染一步的点时读它的 `StepRecord.frame`（UI-TARS 只渲染上一批，BrowserUse 的历史窗口每步各读各的），本步的读 `ReactPromptContext.frame`；Agent 不存任何按步号的状态，所以实时执行、branch 的 source 历史重建（源步记下的坐标系）、restored 历史（恢复步记下的坐标系）与训练导出的每一段（学生在录下截图上的坐标系，记在学生历史里那一步的副本上）走的是同一条路，渲染的先后也无关。**在坐标系上没有位置的点照实说，从不退回像素，也从不夹到边上**（框架的同一句话，`core/loops/coordinate_frames.py` 的 `NOT_PLACEABLE` / `OUTSIDE_THE_SCREENSHOT`）：那一步没有可测尺寸的截图时（坐标系没有网格；导出里因此被 `keep_as_text` 的步、恢复观测的截图量不出尺寸），点写成 `not placeable (no screenshot of known size)`；点在截图所示区域之外时（`to_grid` 会把它夹到边上，从而写错），写成 `outside the screenshot`。动作的 JSON 里这两个点参数取这句话，效果行与指针写成 `a point …`。这样导出与分叉都照常继续，不因一个量不出的点中止。没有定坐标系的 run（`frame` 为 `None`）写 capability 的单位。

**旧录制**：格式 7 之前的 trace 没有记下每一步的坐标系，读取时就被拒绝（§5）；没有兼容路径（测试数据可丢弃），重新录制。坐标系进框架之后，三个 Agent 的系统提示与每步提示与之前逐字相同（`tests/test_agents/test_prompt_context.py`、`test_focus_moved_prompts.py` 钉住的摘要没有变）。

#### 终止观测：`terminal_observation`（M31）

`ReactLoop.terminal_observation: bool = False`：run 结束之后再看一次世界，记成一条 `origin="terminal"` 的 StepRecord（不调 LLM、没有动作，步号接在最后一条记录之后）。这个开关是通用的：`online_mind2web` 按截图打分，最后一个动作之后的那张截图它同样用得上。

- **何时**：done、`max_steps` 用尽、连续解析失败到上限，以及 `BranchLoop` 的步数预算用尽。**从不**在基础设施失败（`_abort`）、动作抛异常、恢复发散和任何 `RunAbort` 路径上。动作返回失败的结果不结束 run，只让那一步停下（上文"一步里有动作失败，这一步就停下"），与终止观测无关。
- **结果在前**：终止的 `AgentResult` 在决定结局时就建好，终止观测不改它的 `agent_done`、`env_status`、`answer` 与 `execution_ms`（由 Runtime 在终止观测结束后填写，包含这次观测）。记录失败只改变记录健康，不改写此结果。
- **不致命**：observe 抛普通异常时记一条带 `error_phase="observe"` 的终止记录，run 的结局不变。guard 在这次 observe 上否决时交出终止记录并上抛（否决优先于已定结果）。记录组件自身错误只进入记录健康；不会追加到执行错误、吞掉否决或替换答案。取消仍原样传播并携带已定结局。
- **分层**：`AgentLoopBase.observe_terminal(env, negotiated_obs, *, step)` 公开捕获并构造终止记录；`observe_after_result(run, result)` 使用同一公开 `surrender` 完成交接与取消的结果传递。ReAct 的 `with_terminal` 仅决定是否启用。终止观测只填环境 fragment，没有 Agent 读取的 loop fragment。


#### `BranchLoop`：从检查点分叉

`BranchLoop` 本轮限定 ReAct：恢复 `0..t-1`，在本次 live 检查点观测上执行策略步，再执行 `continue_steps` 个 ReAct 步。`history_source: source | restored` 默认 `source`，与恢复器的 `verify: strict | warn` 独立；前者选择 Agent 历史的事实来源，后者判断环境发散是否允许继续。

**组装检查**：检查源 trace 已闭合、所需前缀和轨迹/观测证据完整、检查点是 policy 步、源是普通 ReAct run、Agent 与 memory 配置、动作与观测协商和有效动作约束一致，以及恢复器自己对源的要求（`Restorer.source_problems`，例如 `pace: recorded_timeline` 要每个要执行动作的恢复步都量过动作起点）。改变观测协商的 Agent 开关（例如 `show_capture_state`，§4.4）因此也是分叉身份：取值与源录制不同就不能分叉。`restored` 还要求 Agent 公开的异步 `build_restored_history(observation, *, history, context)` 槽；不能以 `warn` 放过合同缺失。模型与采样可改变。**任务变体**（§4.10 `trace_branches` 的 `task_variants`：同一个检查点、另一份任务）的检查点输入带 `task_variant`（变体 id）；这时 `history_source: source` 在组装期被拒，`IncompatibleCheckpointError` 的消息点名 `task_variants` 与 `history_source` 两个设置：source 历史逐次精确重放源 prompt，而源 prompt 带着源任务，种子检查只会在开跑之后才失败。拒绝发生在 `weave`，所以 `spectragent bench` 的 pre-flight（第一个任务的 `prepare()`）就停下，不建环境、不开 trace；变体必须用 `restored`。变体文本经 run 的 `task` 送到 Agent，与任何任务一样；Loop 只读变体 id。源来源、检查点、分支序号及任务变体是任务 provenance；历史来源、恢复策略、提示贡献配置、helper 模型和有效动作约束是实验身份。

**source 历史**：先验证 prepare 种子，按源观测、原始提示贡献及其位置重建每步 prompt。只重放 `purpose=agent_decision` 的主决策及解析重试；每次主调用前精确比较 prompt，调用后校验解析动作、截断及回复。helper 的回复不进入 Agent parser。恢复历史不执行贡献函数、不调用 helper；源贡献证据缺失时明确拒绝。种子和重建不符不受环境 `warn` 影响。然后恢复环境，第一策略步始终使用恢复器已经校验的 live 观测，不额外重拍。

**restored 历史**：恢复器交回本次 live 观测、retarget 后实际执行动作及完整实际结果（含恢复时实际测得的动作效果）。源步在失败的动作处停下、跳过了后面的动作（`StepRecord.skipped_actions`，上文"一步里有动作失败，这一步就停下"）时，恢复同样不执行它们；本次恢复执行了源执行过的每个动作、最后一个在这里也失败了，那几个动作就是因为同一个原因没执行：恢复器把它们 retarget 到本次页面上（不执行），交回在 `RestoredStep.skipped` 上，恢复步的记录把它们写成自己的跳过部分，所以之后的 prompt 与 source 历史用同一种写法说它们没执行，指的是本次页面上的元素。别的情况（`warn` 下成败或尝试与源不同、某个没执行的动作在本次页面上找不到唯一目标）恢复步只记执行了的，源跳过的动作只留在源回复里。每条恢复事实交给 Agent 的公开历史槽，Agent 按自己的消息格式表达（三个 Agent 共用的 `agents/rendering/prompt_context.py::restored_history_message(history, *, show_action_effects, context, action_space, write_action, max_content_chars)`：动作与效果行的点由框架写在恢复步的坐标系 `context.frame` 上，Agent 只给出动作的写法 `write_action`，默认是动作的 JSON；见上文"坐标系归框架"；每个结果是 `action_effects.py::result_facts` 写的事实，`max_content_chars` 给动作返回的内容（`extracted_content`）设上限，截断处写明 `... [N of M characters shown]`，不传时不截断），Loop 不改私有字段；恢复步记录随后进入 `loop.history`，Skyvern 的动作历史窗口就从这里读出实际执行的动作、结果（`result_facts`，返回内容按 `max_result_chars` 截断，与 restored 历史同一个上限）与效果行，不另存一份 Agent 私有状态。源回复、thought 和动作作为带来源的材料保留，不能改编号伪造新回复；实际结果不能被源结果覆盖。缺失动作结果、时序或目标身份不一致、Agent 不能表达时明确失败，不能回退 source。所有前缀步骤仍是 `origin=restored`，没有本次模型调用，也不继承源 usage。恢复校验及发散始终保存。恢复步骤的顶层 `actions` / `action_results` 只保存实际返回结果的精确配对；抛异常而无返回值的动作尝试仍完整保留在 `restoration.actions`，不伪造返回结果、不把后一个结果配到前一个动作。任务变体的分叉也是这样：每个恢复步的源回复（含 thought）照样作为源材料进入历史（UI-TARS 与 Skyvern、browser-use 共用的 `agents/rendering/prompt_context.py::restored_history_message` 把它写成 `source_material.recorded_reply`），而那些回复是为源任务做的决定，源任务文本只会经它们引用而出现；检查点那一步的源回复只给提示贡献函数（`ReactPromptContext.source_reply`），Agent 不渲染。消息不说明源材料服务的是另一份任务；历史格式在变体功能里没有改（2026-10-21 实测，`tests/test_core/test_loops/test_branch_context_identity.py`）。要去掉这些源回复，在 hook 里配 `omit_source_replies`（2026-10-22，用户的做法：挂在 Agent 的提示构建上，不改历史格式）：它 blocking `around` 拦截 Agent 的 `build_restored_history`，交给 Agent 的历史副本 `source_reply` 为空，录下的源动作与实际恢复执行照常；trace 不变，步记录的 `source_history` 由 Loop 写、仍带源回复作出处，已录的模型调用就是模型实际看到的提示。它是对照臂，不是默认。

**恢复步骤的公开执行边界**：`BranchLoop.execute_step` 在本 run 的恢复迭代器有效时推进一个恢复步骤，恢复结束后委托 ReAct 策略步；迭代器与检查点结果只保存在 `BranchRun`。两种历史来源、有无记录器都经过同一入口。AOP 在真实步骤开始计时，到 `record_step` 交接为止；恢复失败和取消也交出本步记录。检查点 capture 属于第 t 步，随后 policy 同编号继续，只有一个步骤提交；记录器保留已开启的起点，不能重新起表丢掉 capture，也不能多算一条检查点步。记录采集和持久化开销另归对应步，不滚入下一恢复步。

**提示贡献合同**：`schemas/prompt.py` 的 `ReactPromptContext` 显式带 step、phase（history/policy/terminal）、policy_step、is_branch 与该步贡献；每次构建属于本 run，不通过 Hook 实例计数猜阶段。`build_react_prompt(observation, *, context)` 是异步槽，Loop 必须 await。`PromptContribution` 记录 text、placement（system/turn）、稳定来源与代码身份；Agent 决定放入适合自己的系统内容或当前轮次并遵守自身消息协议。贡献的实际内容/位置随 trajectory 保存，轻量计划不强迫保存正文。

现有 HookRegistrar 注册 `prompt_contribution`，用 blocking `around` 拦截 Agent builder 并将贡献通过公开 context 传入；不是 after 返回值或私改 memory。配置 stable `package.module:function`，组装时解析 callable 并校验显式参数合同及 async 目标。函数同步或异步均可；可用 wired `helper_llms`（零或一个）接收独立 helper。默认只在分叉第一个 policy 步生成一次，可选每个 policy 步。解析重试复用同一步已构建提示；history 阶段只读原贡献，terminal 不生成。缓存归本次 run/step 的 context，不保存在 Hook 计数器。贡献进入 memory 后是否仍出现由明确 Agent/Memory 合同决定。

函数/辅助调用失败或取消是实际执行失败，沿既有执行策略传播；记录器失败不改变它。helper 调用带 caller/purpose/model/step 和成功、失败、取消及未知 usage，实验用量包含所有实际调用并可按用途拆分；helper thinking 和注入文字均不是 Agent thought。Runtime 在准备期通过公开的 `PromptModelOwner.helper_llms` 收集辅助依赖，执行最终收尾统一关闭主模型与这些 helper；不能靠反射寻找模型，也不能因前序 hook 失败跳过清理。Hook 不再并行持有另一套模型关闭时机。

查看器、指标和独立 judge 区分 source 材料、restored 执行、policy 决策和 terminal 页面证据；任务正确性、有效判定覆盖、恢复保真分别表达。冻结导出仅适配公开异步入口与调用用途筛选，不增加学生策略。删除旧 `branch_instruction` 枚举及 Loop 内固定文本，不保留兼容入口。

**恢复器**（`RestorerRegistrar`，`restorer_id`，`core/loops/restore/`）：

```python
class Restorer(Protocol):
    restorer_id: str
    def source_problems(self, source: SourceRun) -> list[str]: ...   # 组装期：本恢复器要源 trace 有、而它没有的事实，一句一个；[] 是能恢复
    def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]: ...   # 异步生成器：Loop 拿到第 t 步（或交出恢复步失败）时要能关掉它

@dataclass(frozen=True)
class RestorationContext:            # 窄的：没有 LLM、没有 memory、没有 recorder
    env: Any                         # 织过的 environment：observe / retarget / verify_restoration / align_next_observation
    act_fn: Callable[[Action], Awaitable[ActionResult]]   # guarded_act
    negotiated_obs: Any
    source: SourceRun                # 源 trace 的类型化读取：record(k)、observation(k)、calls(k)、step_info(k)
    checkpoint_step: int             # t
```

恢复器只恢复**环境**，从不碰 memory，所以以后的 `capability_checkpoint`（Docker 的 snapshot/restore、Android 模拟器快照，经新增的 `RestorableCapability` 协议接入）是同一个接口下的另一个策略。逐条交回而不是最后一次性返回，是为了每恢复一步就提交一步：恢复到一半超时或崩溃，trace 里也已经有前面的步。

`prefix_replay`（v1）对每个 k < t：（`media: align` 时先 `env.align_next_observation(recorded_obs_k, media_seek_timeout_s=…, media_end_lead_s=…)`，为下一次观测预约媒体对齐，§4.3 / §4.14）→ `env.observe(negotiated_obs)` → `env.verify_restoration(recorded_obs_k, live_obs_k)`（`strict` 下这一步已经不通过就不再执行动作）→ 对第 k 步录下的、源 run 执行过的每个动作（`zip(actions, action_results)`：源 Loop 跳过的、`done` 之后的与抛异常之后的都不执行；这次改动之前录下的步在失败之后照样执行过后面的动作，恢复也照样执行它们；名字是 `guarded_act` 留下的：全限定，被它拒绝的是 agent 写的原名）：`env.retarget(action, recorded_obs_k, live_obs_k)` → `act_fn(重定位后的动作)` → 比较动作成败与录下的结果（`strict` 下一个动作的成败与录下的不同、或执行抛错，就是发散：这一步余下的动作不再执行）；最后再 observe 一次并校验 obs_t。旋钮：`verify: strict | warn`（默认 `strict`）、`screenshot_max_distance`（截图感知哈希的汉明距离上限，默认 5，64 位 dHash）、`pace: none | recorded_llm_latency | recorded_timeline`（恢复步之间等多久，默认 `none`，见下文"恢复节奏"）、`media: none | align`（默认 `align`：每一个被恢复的观测，含检查点 t 的那一个，在采集里落定之后、截图之前把对应上的媒体元素对齐到录下的状态；`none` 不对齐，留作对照；发布的原路径与分叉配置显式写 `align`）、`media_time_tolerance_s`（媒体位置差的容差，默认 1 s）、`media_seek_timeout_s`（一次对齐跳转必须完成的上限，默认 2 s）、`media_end_lead_s`（录下时已经播完的元素，从结尾前这么多秒开始播到自然结束，默认 0.5 s，必须短于 `media_seek_timeout_s`，否则构造时报错并点名两个旋钮）。对齐只在恢复前缀里发生：恢复器在第 t 步交回之后不再预约，Loop 从不碰页面上的媒体，检查点之后的策略步与终止观测都是真实时间。每一步的观测次数与源 run 相同（一步一个 cycle，§4.4）。校验结果的原因按固定顺序判定，见 §5.3 `RestorationCheck`；重定位失败在 `warn` 下照录下的动作原样执行，在 `strict` 下就是发散。

**恢复节奏 `pace`**。源 run 的步与步之间隔着模型思考的时间（实测 3 到 35 s），恢复步却一个接一个执行，所以页面自己的计时器（轮播、自动播放、倒计时、懒加载、延时弹窗）在采集恢复观测时落后于录制，恢复校验因此不通过。三个取值：

- `none`（默认）：不等。
- `recorded_llm_latency`：执行第 k 步动作之前等待第 k 步录下的模型等待时间（`StepRecord.llm_latency_ms`），让异步加载有与源 run 相同的时间落定。观测、动作与记录本身的耗时不在其中，所以恢复的时间线仍与源 run 漂开。
- `recorded_timeline`：从第 0 步的观测起复现源 run 的时间线，只用源 trace 已提交、与平台无关的事实，经 `SourceRun`（`observation(k)`、`record(k)`）读取：
  - **采集时刻**：一次观测的采集时刻是这一步 env 来源 fragment 里最早的 `captured_at`（`FragmentBase.captured_at`，墙钟；采集失败留下的 `FragmentFailure` 不是采集，不算）。源与本次恢复用同一个定义。
  - **锚点**：源锚点是源第 0 步观测的采集时刻，live 锚点是本次恢复第 0 步观测的采集时刻。第一次采集之前的时间（起始页加载了多久）是网速，这个旋钮故意不复现。
  - **观测**：第 k 步（1 ≤ k < t）与检查点 t 的观测之前，目标 = live 锚点 +（源第 k 步的采集时刻 − 源锚点）。观测在"目标 − lead"开始，lead 是本次恢复上一次 `env.observe` 调用的耗时（恢复器在调用前后用单调时钟量）。
  - **动作**：第 k 步第一个动作执行之前（它的重定位之后、`act_fn` 之前），目标 = live 锚点 +（源第 k 步第一个 `ActionSpan.started_at` − 源锚点；`StepRecord.action_spans`）。只对齐一步的第一个动作，同一步后面的动作照旧紧接着执行。`action_spans` 为 `[]` 的步（源这一步没有调用过 `env.act`，例如动作都被 `guarded_act` 拒绝）没有可对齐的时刻，不等。
  - **从不加速**：开始时刻还没到才等；已经落后就立刻做，不追。
  - **缺事实就拒绝，不猜，也不退回别的 pace**：要执行动作的恢复步 `action_spans` 为 `null`（没量，例如源的记录计划没选 `metrics`）时，`prefix_replay` 的 `source_problems` 点名这些步，`BranchLoop` 在组装期把它并进 `IncompatibleCheckpointError`，任何 setup 之前就停下（`restore()` 开始时用同一句话再查一次，手工调用的恢复器也不会走到一半才发现）。观测不在源的记录范围里时，`SourceRun.open` 已经拒绝。一步的 env 来源 fragment 全部采集失败（只有协商的 env fragment 全是可选时才可能）就没有采集时刻：源的那一步在恢复读到它、观测之前以 `IncompatibleCheckpointError` 点名；本次恢复第 0 步的观测如此则没有 live 锚点，恢复以错误结束。
  - 等待是执行的节奏，不是记录：不往 trace 写任何东西。

两种等待都只能近似媒体的播放位置——起播延迟与落定抖动消不掉，源视频自己停住也复现不了——不是媒体恢复的手段。`pace` 与 `verify`、`media` 互相独立；它是恢复器的字段，随 `resolved` 进入分叉 run 的配置身份（`config_hash`）。

### 4.2 AgentRuntime（通用编排）

**设计决策**：从原 `BaseReactAgent.run()` 提取与 loop 范式无关的逻辑（约 90%）为独立 AgentRuntime。AgentRuntime 负责协商、AOP 织入、Loop weave、超时、错误处理，Loop 只负责编排。**API 是两段式**：`prepare()` 装配并返回 wrapped agent；`execute(task)` 加 lifecycle/timeout 包装 `agent.run(task)`。`ComposedRun` 是多实例图；调用方用 `AgentRuntime.for_agent(run, agent_id, env_id=...)` 选定 Agent 与环境，Loop、LLM 和 Memory 则来自该 Agent 的受体（见 §4.2.1）。

```python
class AgentRuntime:
    """与 loop 范式无关的通用编排。每次 prepare/execute 是独立 run 上下文。"""

    def __init__(self, run: ComposedRun, agent: Any, *, env: Any | None = None) -> None:
        self._run = run
        self._agent = agent
        self._env = env
        self._hooks: list[RuntimeHook] = list(run.hooks)

    @classmethod
    def for_agent(cls, run: ComposedRun, agent_id: str,
                  *, env_id: str | None = None) -> "AgentRuntime":
        # 多个 environment 时 env_id 必填；只有一个时可省略。
        return cls(run, run.instance("agent", agent_id),
                   env=run.instance("environment", env_id) if env_id else run.optional_single("environment"))

    # ── prepare(): 装配阶段 ─────────────────────────────────────────
    def prepare(self, *, task_id="unknown", action_filter=None,
                loop_inputs=None,                  # 没有 **kwargs：Loop 的每任务输入走有类型的 loop_inputs
                env_context=None) -> Any:          # 本任务的环境 setup context（TaskContext.env），给 3d 的检查
        agent, env = self._agent, self._env
        loop, llm, memory = agent.agent_loop, agent.llm, agent.memory

        # 1. Slot 校验：loop.__required_slots__ ⊆ agent 的 callable 方法
        loop.validate_slots(agent)
        # 1b. Loop 输入校验（M31，§4.1）：多的 / 缺的 / 类型不对 → IncompatibleLoopInputError
        loop.check_loop_inputs(loop_inputs or {})
        # 1c. Loop 要环境而这个 run 没有（§4.1 "Loop 的两条类级声明"）→ MissingEnvironmentError；
        #     不要环境的 Loop：缺席的环境什么都不提供，下面的协商照常进行
        if env is None and type(loop).__requires_env__:
            raise MissingEnvironmentError(loop.loop_id)

        # 2. Observation 三方协商（见 §4.4）：universe = 环境提供的 + 本 Loop 的 __loop_fragments__
        negotiated_obs = negotiate_observation(agent, loop, env)

        # 2b. Agent 提示词引用的格式说明（§4.4 "观测格式由提供方描述"）：声明了 __fragment_formats__ 才有；
        #     激活了而提供方不描述的 → FragmentFormatError。只读配置，交给 seed_messages
        self._fragment_formats = quoted_fragment_formats(agent, negotiated_obs, env)

        # 3. Action 两方协商（见 §4.5）
        negotiated_actions = negotiate_action(agent, env)

        # 3b. Memory 协议协商（Loop ⊆ Memory，见 §4.8）
        negotiate_memory(loop, memory)

        # 3c. 归档协商（hook ↔ environment，见 §4.5）：只校验，不绑定
        consumers = [h for h in self._hooks if isinstance(h, ArchiveConsumer) and h.archive]
        if consumers:
            negotiated_archive = negotiate_archive(consumers, env)   # IncompatibleArchiveError
            archivers = list(consumers[0].archive)                   # 协商保证至多一个消费者
        else:
            negotiated_archive, archivers = None, []                 # 对 env 零调用
        self._negotiated_archive = negotiated_archive
        self._env_evidence_privacy = (                               # 没绑定归档器时为 None
            sorted({m for a in archivers for m in a.privacy_markers}) if negotiated_archive else None
        )

        # 3d. 同一路由先确认 owner，再由 owner 准备执行输入与记录投影；不获取 backend 资源
        self._prepared_setup = PreparedSetupContext({}, {})
        if env is not None:
            self._prepared_setup = env.prepare_setup_context(env_context)
            env.check_setup(self._prepared_setup.execution, negotiation=negotiated_obs, archivers=archivers)
        elif env_context:
            raise ValueError(...)                                    # 没有环境，就没有可以 setup 的

        # 4. AOP 织入 — Weaver 包装四个角色：agent / env / llm，以及 StepRecorder（role="loop"）
        weaver = Weaver(self._hooks)
        wrapped_agent = weaver.wrap(agent, role="agent")
        wrapped_env = weaver.wrap(env, role="env")
        wrapped_llm = weaver.wrap(llm, role="llm")
        wrapped_recorder = weaver.wrap(self._recorder, role="loop")  # record_step → 一次提交完整 step
        weaver.raise_for_unclaimed_roles()

        # 5. 收窄 action space + guarded_act（不带环境的 run：没有 descriptor，守卫拒绝一切动作）
        narrowed = wrapped_env.get_action_space(
            active_actions=negotiated_actions.active_actions
        )
        action_space, act_fn = build_guarded_act(
            wrapped_env.act, narrowed, action_filter,
        )

        # 6. Loop weave — patch wrapped_agent.run = closure(loop._execute)
        loop.weave(
            wrapped_agent,
            env=wrapped_env, llm=wrapped_llm, memory=memory,
            act_fn=act_fn, negotiated_obs=negotiated_obs,
            negotiated_actions=negotiated_actions,     # M31：BranchLoop 对照源 run 的协商、补全动作前缀
            active_descriptors=active_descriptors,
            step_recorder=wrapped_recorder,
            task_id=task_id,
            loop_inputs=MappingProxyType(dict(loop_inputs or {})),
        )                                              # BranchLoop.weave 在这里做检查点的装配期校验

        # 7. 拦截点审计 —— 必须在 weave 之后：agent.run 到这一步才存在
        weaver.audit_intercepts()

        # 8. 绑定归档器 —— prepare 的最后一步，作用在原始 env 上（不经 AOP 代理）
        if negotiated_archive is not None:
            env.bind_archivers(archivers)
        return wrapped_agent

    # ── execute(task): 一站式驱动 ───────────────────────────────────
    async def execute(self, task, *, timeout=300, task_id="unknown", benchmark="", subset="",
                      run_id=None, env_context=None, loop_inputs=None, branch=None,
                      action_filter=None, config=None, raise_on_error=True):   # 没有 **kwargs
        prepared_agent = self.prepare(task_id=task_id, action_filter=action_filter,
                                      loop_inputs=loop_inputs, env_context=env_context)
        env_context = self._prepared_setup.execution            # 复用 prepare 的结果，不二次读取输入文件
        self._agent.memory.clear(); self._recorder.clear()       # 一个 ComposedRun 可以跑多个任务
        self._agent.memory.add_messages(self.seed_messages(task))  # M32：种子消息是公开方法，见下

        # 在 env setup 之前打开 trace：setup 失败也要落进 trace
        await self._dispatch_lifecycle("on_run_start",
            agent=self._agent, env=self._env,                    # env：原始环境，不是 AOP 代理
            task=task, config=config or {}, ...,
            archive_negotiation=self._negotiated_archive,      # 没有归档消费者时为 None
            env_evidence_privacy=self._env_evidence_privacy,   # 有序、去重的隐私标记；没绑定归档器时为 None
            task_setup=self._prepared_setup.recordable,       # owner 决定的记录投影；TraceWriter 只 snapshot / redact
            branch=branch)                                     # M31：分叉来源（BranchProvenance），非分叉 run 为 None

        self._finish_started = False
        try:
            try:
                await self.setup_environment(env_context)
            except Exception as exc:
                if isinstance(exc, RunAbort):           # setup 里的否决也是否决
                    result = ...   # agent_done=VETOED
                    await self._close_out_vetoed(result, exc)
                    raise                               # 不看 raise_on_error
                result = ...   # agent_done=ERROR, env_status=FAILED
                await self._finish(result, error=exc)
                if raise_on_error:
                    raise
                return result

            try:
                result = await asyncio.wait_for(
                    prepared_agent.run(task), timeout=timeout
                )
            except TimeoutError as timed_out:
                # 切断 Loop 的那次取消可能带着 Loop 已经定下的结局（RunEnded，见下文）
                ended = _outcome_under(timed_out)
                if isinstance(ended, RunAbort):         # 否决在先：照否决收尾、重抛
                    ended.add_note(...)                 # 超时只是否决上的一条 note
                    result = ...   # agent_done=VETOED，error 点名否决并注明超时
                    await self._close_out_vetoed(result, ended)
                    raise ended from timed_out
                if isinstance(ended, AgentResult):      # Loop 已建好的终止结果：照它收尾、返回
                    result = ended.model_copy(...)      # error 仍点名根因，后面注明记录被超时切断
                    await self._finish(result)
                    return result
                result = ...   # agent_done=TIMEOUT
                await self._finish(result, error=TimeoutError(result.error))
                return result
            except Exception as exc:
                if isinstance(exc, RunAbort):
                    result = ...   # agent_done=VETOED
                    await self._close_out_vetoed(result, exc)   # guard 的否决：总是上抛，不看 raise_on_error
                    raise
                result = ...   # agent_done=ERROR，保留实际执行错误；记录故障由记录器隔离
                await self._finish(result, error=exc)
                if raise_on_error:
                    raise
                return result
            else:
                await self._finish(result)   # 在 try 之外：收尾失败不会被当成 run 失败再收尾一次
                return result

        except asyncio.CancelledError as exc:
            # 外部取消（Ctrl-C、sweep 被取消），不论落在 setup 还是 run 期间
            if not self._finish_started:   # 收尾已经开始：不再二次派发
                result = ...   # agent_done=ERROR, error="Execution cancelled"
                try:
                    await self._finish(result, error=exc)
                except Exception:
                    logger.exception(...)  # 收尾失败只记日志：取消优先
            raise              # 原样交还调用方，不看 raise_on_error
        finally:
            try:
                await self._drain_background()     # 兜底：收尾没走到它自己的 drain 时
            finally:
                await self.teardown_environment()  # 永远在 on_run_end 之后；失败只记日志

    async def _finish(self, result, error=None):
        """收尾：on_run_end → （有异常时）on_run_error → drain 后台 handler。"""
        self._finish_started = True
        await self._dispatch_lifecycle("on_run_end", result=result, env=self._env)
        if error is not None:
            await self._dispatch_lifecycle("on_run_error", error=error,
                partial_steps=self._recorder.records, env=self._env)
        await self._drain_background()

    async def _close_out_vetoed(self, result, veto):
        """被否决的 run 收尾：收尾失败只记日志、作为 note 附在否决上，从不顶替它。"""
        try:
            await self._finish(result, error=veto)
        except Exception as failure:      # CancelledError 不在此列：取消仍然优先
            logger.exception(...)
            veto.add_note(...)
```

**为什么两段式**：`prepare()` 是纯装配（slot 校验 → 协商 → setup 之前的检查 → AOP 织入 → loop weave），返回值就是一个 self-contained 的 agent —— 测试和需要细粒度控制的调用方可以直接 `await prepared_agent.run(task)`，跳过 timeout/lifecycle 包装。`execute()` 只是 prepare + 围绕 `agent.run` 加 timeout 和 lifecycle hook。两段都是独立的、零全局状态的 run 上下文，并行多个 run 互不干扰。

**种子消息是 Runtime 的公开方法。** `seed_messages(task) -> list[BaseMessage]` 在 `prepare()` 之后调用 Agent 原有 `prepare(task=..., action_space=..., execution_limits=..., fragment_formats=...)`（`action_space` 是 Runtime 公开的有效动作空间的副本：prompt 广告的 descriptors 与说明短名含义的绑定，与守卫用的是同一份；`fragment_formats` 是第 2b 步解析出的格式说明）；是否需要限制、需要哪些格式说明，已在装配时由显式消费声明检查，不按参数名称机会性传入。不写 memory、不碰环境、不调用模型，实时执行和冻结导出使用同一入口。

**两种耗时。** `AgentMetrics.execution_ms` 由 Runtime 唯一计时：即将 setup 前开始，Loop 返回或执行路径结束、进入结束 lifecycle 之前停止；包括 setup、恢复、策略、终止观测及执行时正常记录等待，排除结束 hook、close-out 与 teardown。所有终止路径使用实测值，普通超时不填配置预算。`timeout` 仍只罩 `agent.run`。任务驱动的 `TaskOutcome.elapsed_ms` 包含任务加载、装配、完整收尾、释放资源与结果回读；不提前写进尚未收尾的 `result.json`。不宣称步骤 duration 之和等于 execution 或任务总耗时。

**执行内模型资源收尾只有一个入口。** Runtime 在 `prepare()` 从主模型和 `PromptModelOwner.helper_llms` 明确收集本次执行的原始模型，织入后仍保留这一份资源清单；`execute` 最外层 `finally` 调 `close_models`，按对象去重并独立、有界地尝试全部关闭。普通结束 hook 失败不能跳过任何 helper。提示 Hook 提供和绑定领域依赖，不另在 `on_run_end` / `on_run_error` 关闭同一模型；独立 Python 使用通过 `model_session` 管理自己的执行边界。

**setup 会拒绝的，`prepare()` 就拒绝（用户 2026-10-13 的裁决：两个开关的组合由 `prepare()` 校验，sweep 开跑前先过一次 `prepare()`）。** 能否运行由配置与任务数据决定的问题，都不留到 setup：

- 第 3d 步（上面）把本任务的环境 setup context、观测协商结果与将要绑定的归档器交给 `Environment.check_setup`（§4.3 "setup 之前的检查"）：谁都不接受的 setup 键在这里报 `ValueError`；实现了 `SetupCheck` 的 capability 检查自己的那一份（browser：§4.14 "协作检查一览"的全部行，含回放与源 run 的一致性检查，读的只是源 run 的环境指纹；profile 的 watchdog 解析不了的已协商 fragment；任务的起始页是不是一个 http(s) URL，§4.10 "起始页"）。只校验：不 setup、不绑定、不收窄、不连浏览器；
- 所以这些错误和其余装配错误一样，在 `on_run_start` 打开 trace 之前、任何东西启动之前从 `prepare()`（`execute()` 里也一样，不看 `raise_on_error`）出去，`spectragent run --dry-run` 也查得出来（`--start-url` / `--replay-archive` 是它的任务上下文）；
- setup 照样再查一遍（capability 的 `setup()` 调同一个 `check_setup`，带着那时已绑定的归档器）：直接调 setup 的调用方、`prepare()` 之后才注入控制客户端的测试，仍在启动任何东西之前被拒绝；
- 需要读盘的只有回放的一致性检查：`load_fingerprint` 只读源 run 的环境指纹（`env/run.browser.json`）并数 `index.jsonl` 文件，从不读索引行或 WARC；setup 才把整个归档加载进来用于服务（§4.14 "一致性检查与回放指纹"）。

**sweep 先过一次 `prepare()`。** `BenchmarkRunner.run`（`core/benchmark_runner.py`）在跑任何任务之前，为第一个选中的任务组装栈并调一次 `prepare(task_id=, loop_inputs=, env_context=)`（`BenchmarkRunner.preflight`）：组装失败、读不到任务上下文、`prepare()` 拒绝，都抛 `PreflightError`（点名任务，原错误作为 `__cause__`、原样写进消息，所以报错仍点名双方），一个任务也不跑、一个 trace 也不打开；`spectragent bench` 把它印到 stderr，退出码 2（`PREFLIGHT_EXIT_CODE`；0 与 1 是跑过了的 sweep），度量链（§4.16）因此在这一段停下。通过之后第一个任务就跑在 pre-flight 组装的那个栈上，所以每个任务仍然恰好组装一次栈。之后的任务各自 `execute()` 时照样 `prepare()`，只有某个任务自己的数据有问题时才在那个任务上报（记在它的 `TaskOutcome.error`，不打开 trace）。

**sweep 的汇总：三本账、基础设施失败、否决（用户 2026-10-13 的裁决）。** 每个任务的 `TaskOutcome` 带三本账：agent 怎么结束（`result.json` 的 `agent_done`：done / max_steps / error / timeout / vetoed）、环境撑没撑住（`env_status`）、trace 记全没有（run 结束之后读回它的 manifest：`complete` / `incomplete` 加原因 / `writing`，读不回来是 `unreadable`）。`format_table` 先印这三本账的计数，再逐个任务印一行。**基础设施失败**（`infrastructure_error`，计入 `failed`，退出码 1）都看得见：环境坏了（`env_status=failed`）；模型在 provider 自己的重试之后仍不可用（run 以 `error` 结束在一次模型调用上：最后一个策略步 `error_phase == "llm"`，它失败的那次调用记录不是截断——截断是本 run 自己的 token 上限，不算不可用；读的是 trace 里记下的调用，不解析 `error` 文本）；trace 不完整、没关闭或读不回来。agent 在健康的环境里用完了步数、并且全部记下了，是一次完整的尝试（`ok`）。**否决**不是基础设施失败：被否决的任务照样是一个 outcome（`vetoed`，结果从它的 `result.json` 读回），表里有它那一行和一行 `vetoed: ...`，sweep 在它之后停下，`spectragent bench` 退出码 3（`VETO_EXIT_CODE`），度量链因此在这一段停下（§4.16），被否决那一段的 run 不会被当作正常输入交给下一段。

**sweep 的 token 上限（用户 2026-10-14 的裁决）。** `spectragent bench --token-limit N`（`BenchmarkRunner(token_limit=)`）：至少为 1 的整数，或 `unlimited`（默认：一份用量也不读，汇总与退出码和没有这个参数时完全一样）。有上限时，每个任务结束、读回它的 trace 账之后，再从它的调用记录数它用掉的 token（`core/trace/usage.py::recorded_usage`，规则见 §4.16 "token 上限"；不依赖完整轨迹），加进 sweep 的累计。provider 没报用量的调用计入调用数、不加 token，在表里逐个点名（任务、第几步第几次调用、状态），不当作 0 悄悄吞掉；trace 或它的调用记录读不回来的任务，token 记为没数到并写明原因，sweep 照常继续。累计达到上限（大于或等于）时，达到它的那个任务已经跑完，之后的任务一个也不开始：表头带 `token_limit_reached=1`，表里多一行 token 合计，每个任务那一行带它的 token 数，最后一行写明在哪个任务之后达到、累计多少、上限多少、几个任务没开始；stderr 也写一行；退出码 4（`TOKEN_LIMIT_EXIT_CODE`）。同一个任务既被否决、又让累计达到上限时按否决算（退出码 3）；达到上限的 sweep 里有基础设施失败时退出码仍是 4（失败照样列在表里），所以退出码的先后是 2、3、4、1、0。与上限交叉的三条：

- **一个任务从不被上限打断。** sweep 一次只跑一个任务，"已经在跑的任务"就是让累计达到上限的那一个：它照常跑完、收尾、记账，所以一次 sweep 最多超出上限一个任务的用量（至多 `max_steps` 次调用）。以后的 runner 若同时跑几个任务，规则不变：达到上限后不再开始新任务，已经在跑的照常跑完并计入。
- **pre-flight。** 它不调模型、不花 token，照常在第一个任务之前跑；上限从不在开跑之前拒绝 sweep（它至少为 1，第一个任务总会开始）。有上限时 pre-flight 多查一条：配置里要有写 trace 的 hook（带 `run_dir` 的 hook，与找 trace 目录的 `_trace_dir_of` 同一个鸭子类型），否则 `PreflightError`、退出码 2：没有 trace 就数不到用量，上限会形同虚设。
- **评测。** 停下之后只给跑过的任务评测（与否决一样）；评测器的 judge 调用不在任务的 trace 里，不计入上限（度量链默认不评测）。

**归档：先校验，最后绑定。** 归档轴（§4.5）的协商与绑定分在 `prepare()` 的两头：

- `negotiate_archive` 紧跟 `negotiate_memory`，在任何织入之前运行，所以绑定错误（`IncompatibleArchiveError`）和另外几条协商一样在装配期暴露。`spectragent run --dry-run` 也调 `prepare()`，干跑就能发现；
- `env.bind_archivers(...)` 是 `prepare()` 的最后一条语句，在拦截点审计之后：capability 的状态一旦改变，后面就不再有会失败的装配检查；
- 它仍然在 `on_run_start` 和 `setup_environment` 之前，因为 capability 在 setup 时就要决定建不建自己的归档采集通道（browser 的 recorder 连接与 archive scope，M29）。`bind_archivers` 不获取任何资源：干跑也会走到这一步，而干跑从不 setup；
- 将要绑定的归档器在第 3d 步就交给了 `check_setup`，所以与归档器有关的协作行（browser：归档器与档位、provider 能力、profile，§4.14 "协作检查一览"）在绑定之前、织入之前就报了；capability 的 `bind_archivers` 仍在绑定前把这些行再查一遍（它是公开方法），先查、后改状态，抛错时它什么也没绑定，错误从 `prepare()` 出去，干跑同样能发现；
- 没有任何 hook 声明非空的 `archive` 时，runtime 对 environment 不做任何归档调用，所以没有这些方法的 environment（测试里的 stub）照常工作；
- runtime 只认 `ArchiveConsumer` Protocol（`core/archive/protocols.py`），从不认识 `TraceWriter`。

协商结果留在 runtime 上：`negotiated_archive` 与 `negotiated_observation` / `negotiated_actions` 并列，`prepare()` 之后可读，没有归档消费者时为 `None`。它连同 `env_evidence_privacy`（没绑定归档器时为 `None`）、`task_setup` 一起按名字传给 `on_run_start`：runtime 是唯一同时握有这些事实的组件，由它解析，hook 只按名字接收、不自己推导（记录到哪些 `TraceConfig` 字段见 §5.3）。"有没有绑定归档器"就是 `archive_negotiation` 本身，没有另外一个布尔量。

**任务上下文的三部分各走各的路（M31）。** 一个任务的 `TaskContext{env, loop, branch}`（§4.10）由调用方（`BenchmarkRunner.run_task`）拆开交给 `execute`：`env` → `env_context`（相对的 `replay_archive` 先解析成绝对路径，§4.10；→ `prepare()` 的 setup 之前的检查 → 原样记进 `task_setup` → `setup_environment`，按 capability `setup()` 签名路由，§4.3）；`loop` → `loop_inputs`（→ `prepare()` 校验后进 weave 的 `ctx["loop_inputs"]`，§4.1）；`branch` → `branch`（一个 `BranchProvenance`，按名字传给 `on_run_start`，记成 `TraceConfig.branch`，§5.3；runtime 不解读它）。三者之间不互相渗透：环境拿不到检查点，Loop 拿不到 setup context，trace 只记来源。

**每条终止路径都派发 lifecycle，外部取消也一样。** `on_run_start` 之后，done / max_steps / timeout / 异常 / 外部取消都经 `_finish` 收尾：先以一个真实的 `AgentResult` 调 `on_run_end`，有异常再调 `on_run_error`，最后 drain 后台 handler；env teardown 留在 `finally` 里，永远在 `on_run_end` 之后（env-state 打分要活着的环境）。外部取消（`asyncio.CancelledError`：Ctrl-C、sweep 被取消）构造 `agent_done=ERROR`、`error="Execution cancelled"` 的结果，`_finish` 之后**原样重抛**，调用方的 `asyncio.timeout` / `TaskGroup` 语义不受影响；取消若在 `_finish` 已经开始之后才到，不再二次派发。缺了这一条，被取消的 run 没有 `result.json`，manifest 永远停在 `writing`，进行中那次 LLM 调用的 prompt 也落不了盘（§5.4）。进程被直接 kill 不在此列：那要求 prompt 预写（write-ahead），目前不做。

**收尾只做一次，取消永远优先。** 被取消的 run 收尾时，blocking hook 自己失败了（某个 hook 的 `on_run_end` 抛错；`TraceWriter` 自己收尾时从不抛错，它把写不进去的东西记进 trace 的 `incomplete_reason`，§4.6），这个错误只记日志，重抛的仍是原来那个 `CancelledError`：否则 Ctrl-C 会变成一个普通的 `OSError`，sweep 的 `except Exception` 把它记成一次任务失败、接着跑下一个任务，调用方的 `asyncio.timeout` 也只会看到 `OSError` 而不是 `TimeoutError`。被否决的 run 同理（下一段）。其余路径（完成、超时、其他异常、setup 失败）上收尾失败，hook 的错误照原样上抛，不看 `raise_on_error`；run 不会被再收尾一次。成功路径的 `_finish` 因此放在 `try` 的 `else` 里：放在 `try` 之内时，hook 在 `on_run_end` 里抛的错会落进 `except Exception`，同一个 run 被当成失败再收尾一次，每个 hook 都收到两次 `on_run_end`。

**否决总是上抛，记录失败顶替不了它。** `execute` 只看从 Loop 里出来的是不是 `RunAbort`。Loop 重抛否决之前先把被否决的那一步交给 `record_step`；那次记录若失败（归档收集坏了、磁盘满），记录失败绝不能取代否决从 Loop 里出来，否则 `raise_on_error=False` 会把它当成普通失败吞成结果，sweep 接着跑下一个任务。Loop 把它作为 note 附在否决上照样重抛；基础设施失败的终止结果同理，`error` 仍然点名根因（§4.6 "记录失败不顶替 run 已经定下的结局"）。收尾失败同样顶替不了它：`execute` 对否决和对取消一样处理（`_close_out_vetoed`）：`_finish` 抛出的 `Exception`（某个 blocking hook 收尾失败）只记日志，并作为又一条 note 附在否决上，重抛的仍是 `RunAbort`，sweep 照样停下。磁盘一直满着时，`TraceWriter` 收尾要把这一步还登记着的调用（归档时还有遗留批次和尾部）作为 trailing 提交，照样失败——它不抛错，写不进去的那一样丢掉，原因记进 trace 的 `incomplete_reason`（连终态都写不进去时 manifest 停在 `writing`），§4.6。**setup 里的否决也是否决**：环境 setup 期间抛出的 `RunAbort` 不被当成普通的 setup 失败，照样以 `agent_done=vetoed` 收尾、越过 `raise_on_error=False` 上抛，sweep 照样停下。取消落在否决的收尾里时，取消优先：`CancelledError` 是 `BaseException`，这一层不接它。

**run 自己的超时顶替不了已经定下的结局。** 优先级从高到低：外部取消 > 否决 > run 自己的超时、收尾失败、记录失败。Loop 在结局离开之前要先记录 run 结束在的那一步，这次交接可能要等：归档时 `TraceWriter` 正是在这里收集环境证据，收的恰好是 guard 拒绝的那个重页面。run 的超时（`wait_for`）若在这时到期，取消落在 Loop 里。Loop 不吞取消（它也可能是 Ctrl-C 或调用方自己的 `asyncio.timeout`），可是只原样重抛的话，`wait_for` 把它变成 `TimeoutError`，`execute` 返回一个普通的超时结果（不看 `raise_on_error`），sweep 记下一个超时任务、接着对 guard 刚拒绝的站点跑下一个，日志和 trace 里都找不到 guard 说过停。所以 Loop 重抛的是同一个取消，但让它带上结局（`core/loops/errors.py`：`carry_outcome` 把一个 `RunEnded` 放到取消的 `__context__` 链最前面；在 `except RunAbort` 里等待时，否决本来就在那条链上）。`execute` 只在 `except TimeoutError` 里读这条链（`carried_outcome`，经 `wait_for` 的 `TimeoutError.__cause__`，3.11 与 3.12 一样）：带着否决，就照否决收尾（`agent_done=ERROR`，`error` 点名否决并注明超时在记录被否决那一步时到期，超时作为 note 附在 `RunAbort` 上），然后重抛它，不看 `raise_on_error`；带着 Loop 已建好的终止结果（基础设施失败、动作失败、最后一次容许的解析错误），就照这个结果收尾并返回它，`error` 仍点名根因，后面注明记录被 run 的超时切断，不会把死掉的浏览器记成"模型太慢"。只有切断的是一个还在进行的 run，结果才是 `agent_done=TIMEOUT`。外部取消不读这条链，照旧原样重抛：结局只留在它的 traceback 里。ReactLoop 的做法见 §4.6 "记录失败不顶替 run 已经定下的结局"，新 Loop 的义务见 `af-add-a-loop` guide §3.3。

**AOP 织入流程**：Weaver 读取所有 hook 的 `__intercepts__` 声明（如 `Intercept("ainvoke", role="llm", advice="after")`），为 agent/env/llm 和 StepRecorder 生成 `AOPProxy` 包装。Loop 调用 `llm.ainvoke()` 时，代理触发 TraceWriter 的 `around` handler（调用前登记、结束时记下结局）；Loop 调用 `recorder.record_step()` 时，代理触发 TraceWriter 封存本步并交给有界持久化任务。Loop 代码零 hook 感知。

**Lifecycle vs AOP 两层 hook 接口**：
- **Lifecycle 回调**（`on_run_start/end/error`）：由 AgentRuntime 在结构边界直接调用
- **方法拦截**（`__intercepts__` + `before_/after_/around_`）：由 AOP Weaver 透明织入

### 4.2.1 ConfigComposer（IoC / AOP 胶水层）

**设计决策**：conscribe 提供"YAML → typed Pydantic config"的 schema 层；`AgentRuntime` 消费 wired 实例图。两者之间需要一层 IoC 胶水，把 conscribe `build_composed_config(inline_wiring=True)` 输出的 discriminated-union model 实例化成"已连接好"的对象图——这层就是 `ConfigComposer`。它是文档此前没有显式描述、但代码里必备的层（参见 §3 架构图）。

```python
@dataclass(frozen=True)
class ComposedRun:
    """Fully assembled, setattr-injected object graph ready for AgentRuntime."""
    by_layer: dict[str, dict[str, Any]]    # {layer: {instance_id: instance}}
    top_level_cfg: BaseModel | None = None

    def instances(self, layer: str) -> list[Any]: ...
    def ids(self, layer: str) -> list[str]: ...
    def instance(self, layer: str, instance_id: str) -> Any: ...
    def single(self, layer: str) -> Any: ...
    def optional_single(self, layer: str) -> Any | None: ...
    @property
    def hooks(self) -> list[Any]: ...


class ConfigComposer:
    """4 步 pipeline：discover → schema → validate → instantiate-and-inject。"""

    def __init__(self, *, registrars, discover_roots=("spectragent.agents",
                                                       "spectragent.core",
                                                       "spectragent.schemas")):
        for root in discover_roots:
            discover(root)                                  # 1) 触发自动注册
        self._composed = build_composed_config(             # 2) 生成 schema
            registrars, inline_wiring=True
        )

    def compose(self, raw: dict | BaseModel) -> ComposedRun:
        cfg = (raw if isinstance(raw, BaseModel)
               else self.top_level_type.model_validate(raw))   # 3) Pydantic 校验

        by_layer = {layer: {} for layer in self._registrars}
        for layer in self._composed.top_level_layers:           # 4) leaves-first 实例化
            for entry in (getattr(cfg, layer, None) or []):
                self._instantiate(entry, by_layer, ...)
        return ComposedRun(by_layer=by_layer, top_level_cfg=cfg)
```

`by_layer` 使用配置中的实例 `id`，省略时用 `<registry_key>#<ordinal>`；同一实现的多个实例保持独立。没有单数的 `agent/loop/env/llm/memory` 便利属性。调用方用 `AgentRuntime.for_agent(run, agent_id, env_id=...)` 选定一个 Agent，Loop、模型与 Memory 来自该 Agent 自己的受体。

所有运行、评测、导出、配置检查和生成入口共用 `core/registries.py::collect_registrars()`。它按稳定顺序返回新的注册表映射，不持有组件实例或跨 run 缓存；实现发现仍由 Composer 或生成入口执行。`scripts/generate_all.py` 只负责生成产物，不再承载其他运行入口依赖的私有目录函数。

**Wired receptor 的判定与注入**：
- 统一使用 conscribe 2.0 的公开 `introspect.wiring_slots` / `receptor_slots` / `init_param_names`。构造参数优先，随后读取 MRO 中的非 `ClassVar` 类级注解；继承的参数按 `**kwargs` 转发链读取，注解在声明它的模块中解析。
- 受体的 `shape` 决定 single / list / dict 注入，识别 `Annotated`、`Optional` 和集合类型。只在 `__init__` 中注解的可选列表也按列表注入，不再要求额外的类级注解。
- 纯协商声明（agent 的 `observation` / `action`，capability 的 `observation` / `archiver`）使用 dict wiring：`{"registry": "observation", "keys": [...], "optional_keys": [...], "declare_only": True}`。不产生描述符或存根，不内联成实例配置；仍生成可选 `Literal` 字段供校验。YAML 填入合法名称时 Composer 仍以 `UnresolvedWiringError`（"negotiation-only entry"）拒绝，因为该值没有运行时接收者。
- 普通 list / dict 受体省略或写 `null` 时，第二遍 `_resolve_deferred` 从目标层的实例池中取允许的实现；池为空时保留主人自己的默认值，主人没有默认值才设空容器。`[]` 永远是显式空，`capability.actions: []` 不构造任何动作。
- **只能内联的层**由 `create_registrar(..., inline_only=True)` 声明：`ArchiverRegistrar` 是当前唯一用例。conscribe 在运行时模型、生成 Python 与 JSON Schema 中一致禁止顶层 `archiver:`，包括 `[]` 与 `null`；Pydantic 错误类型 `inline_only_layer`，位置 `archiver`，提示 `hook[trace_writer].archive`。Composer 使用 `result.top_level_layers` 遍历可写顶层段，内联层受体省略或 `null` 时保留主人的默认值，绝不从实例池中取别的 hook 的归档器。删除本地 `__inline_only__` 声明和重复的拒绝逻辑。归档仍只有 `trace_writer.archive` 一个开关。
- **配置面的拼写规则**（用户 2026-10-13 的裁决；注册名、选项值与旋钮都遵守）：
  - "不启用"拼作 `none`（`replayability: none`、导出的 `occlusion: none`、`shift_dates: none`、`thought_source: none`）；默认值本身做一件正常事的保留描述性名字（`live`、`instant`、`identity`）；`isolation: none_acknowledged` 是书面承认会漏流量，保留。注册名与选项值都不许是 YAML 1.1 读成布尔或 null 的词（on / off / yes / no / true / false / null / ~，任何大小写）：所有入口都用 PyYAML 的 `safe_load`，它按 YAML 1.1 读，裸写的这些词到校验时已经是 `False` 或 `None`。`tests/test_core/test_composer/test_yaml_safe_values.py` 扫描每个注册表的全部键，以及运行配置、链规格、站点规则三份 schema 里的全部选项值
  - `null` 只表示"这个可选项没设"（可选的子块与覆盖值：`require_eval`、`user_agent`、`length`……），默认为 null 的旋钮都在说明里写明没设时是什么（`None (the default): ...`）。数值上限不用 null：不限写 `unlimited`，默认值写出真实的值（`dom_max_elements`、`dom_max_serialized_chars` 默认 `unlimited`，导出的 `max_document_misses` 默认 `0`）；共用的拼写与读法在 `schemas/_types.py`（`UNLIMITED`、`Unlimited`、`limit_value()`，代码内部仍用 `None` 表示不限）。类型写成 `Annotated[int, Field(ge=1)] | Unlimited`：界写在 `int` 那一支上（写在外层 pydantic 会拿它去比 `"unlimited"`）。conscribe 2.0 在运行时模型、生成的 `_composed.py` 和 JSON Schema 中都保留嵌套的界。"按 provider 自己的策略"这类默认写成显式的词：`reasoning_effort: auto`
  - `[]` 永远是空（上面省略受体的那一条规则）。非空集合默认值使用不可变的 `tuple[str, ...]` 默认参数；`sources` 默认 `("*",)`、`require_archive` 默认 `("browser.network",)`，生成 schema 显示真实数组默认值，YAML 仍写列表，`null` 不再代替这两个默认值

**注入方式**：init param → 进 `__init__` 的 kwargs；class_hints-only → 通过 `setattr(instance, name, value)` 注入（conscribe `WiredField` descriptor 接受这次写入并把值放到 instance dict，遮蔽自身）。

**与 AgentRuntime 的边界**：`AgentRuntime` 只接收 `ComposedRun`，**不**直接看 conscribe schema、**不**自己解析 YAML。`ConfigComposer` 通过 conscribe 的公开反射接口读取受体和容器形态，不复制上游的反射规则。普通用户 `BaseModel` 子配置保持已校验的模型实例传入构造函数；只有嵌套判别器块按字段展开。增删 wiring grammar、调整 discover 路径、加新 layer 都只动 Composer。

**只有一个根，每一层都是可选的（M32 实测）。** 生成的 `ComposedConfig` 每个允许顶层配置的层都是 `list[<layer union>] = []`（`inline_only` 层除外），没有必需层：只写 `evaluator:` 的 YAML 能组装；只写一个顶层层、且该层带单实例受体（`task_loader: [{benchmark_name: trace_branches, step_selector: {...}}]`）也能组装，受体照样是类型化的嵌套联合，拼错的键被拒（`tmp/flywheel/m32_design/probe_root.py`）。所以训练导出不另立根：`exporter` 与 `action_translator` 是这个根的两个可选顶层层（与 `evaluator` 同一先例，§4.15），一份只有 `exporter:` 的 YAML 照样由 `ConfigComposer(registrars=collect_registrars())` 组装；学生的运行配置是另一份普通的运行 YAML，由导出器再组装一次。

**生成与验证（conscribe 2.0）。** 唯一入口仍为 `spectragent config generate`。严格 discover 排除 `spectragent.generated` 与 `spectragent.scripts`，无需先删除旧产物；所有已收集层必须非空。先记录注册表指纹，再构建、渲染完整源代码和 schema（Pydantic 渲染可能规范化数值约束），渲染成功后逐文件原子替换，最后写指纹。存根调用 `write_layer_stubs(prune=True)`，只清理上游识别的过期生成存根；所有现有受体均在源码中声明，所以当前不需要 `.pyi`。测试将 conscribe 警告视作错误，用公开 `isolated_registries` 隔离假实现，并在干净进程中比较运行时模型、生成 Python、JSON Schema 及所有发布 YAML 的校验结果。

### 4.3 Environment 抽象

**设计决策**：Environment 通过 `__wiring__` 注入 Capabilities，是 Capability 的组合者和路由器。Environment 自身走 conscribe 注册表。多 capability 场景通过前缀路由 action（`browser.click` → browser capability）。

```python
EnvironmentRegistrar = create_registrar("environment", Environment, discriminator_field="env_type")

@runtime_checkable
class Environment(Protocol):
    env_type: str                              # 类身份（registry key）
    id: str | None                             # 实例身份：同一 run 里两个 browser 环境靠它区分

    # 已注入的 capabilities（实例 id → capability），由 ConfigComposer 通过 setattr 写入
    capabilities: dict[str, BaseCapability]

    # 能力发现（从注入的 capabilities 聚合）
    def capability_ids(self) -> frozenset[str]: ...
    def has_capability(self, cap_id: str) -> bool: ...

    # 三条 provides 轴：同样从 capabilities 聚合，同样是全限定键
    @property
    def provided_fragments(self) -> frozenset[str]: ...
    @property
    def provided_actions(self) -> frozenset[str]: ...     # negotiate_action 一直在读它；M28 补进 Protocol
    @property
    def provided_archivers(self) -> frozenset[str]: ...   # 归档轴（§4.5）

    # 按需观察 — 一次调用 = 一个 observation cycle，委托给 capabilities 的 resolver
    async def observe(self, negotiated: NegotiationResult) -> NegotiatedObservation: ...

    # 动作执行 — 前缀路由到对应 capability；不带任何路由句柄（子上下文是 capability 自己的状态）
    async def act(self, action: Action) -> ActionResult: ...

    # Action space — 聚合所有 capabilities 的 descriptors，可按 active_actions 收窄
    def get_action_space(self, *, active_actions: frozenset[str] | None = None,
                         **context) -> list[ActionDescriptor]: ...

    # 归档 — 按前缀路由绑定（prepare 的最后一步）；由归档消费者在自己的提交点拉取（§4.6）
    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None: ...
    async def collect_archive(self) -> ArchiveBatch: ...

    # 检查点恢复（M31）— 只有恢复器调用；没实现 RestorationSupport 的 capability 得到恒等 / 不校验的默认
    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget: ...
    async def verify_restoration(self, recorded: Any, restored: Any) -> list[RestorationEvidence]: ...

    # setup 之前的检查 — prepare() 调：setup 会拒绝的，现在就拒绝（下文）；不获取任何资源
    def check_setup(self, context: Mapping[str, Any] | None = None, *,
                    negotiation: Any = None, archivers: Sequence[ArchiverBase] = ()) -> None: ...

    # 生命周期 — setup 的 context 按 capability 路由，不广播（capability 自己内部调 backend.open/close）；
    # teardown 在 setup 半途失败、或根本没有 setup 之后调用也必须安全
    async def setup(self, context: Mapping[str, Any] | None = None, *,
                    negotiation: Any = None) -> None: ...
    async def teardown(self) -> None: ...

    @property
    def is_alive(self) -> bool: ...
```

**三条 provides 轴在 Protocol 上一样声明。** `provided_actions` 过去只在 `EnvironmentBase` 上，`negotiate_action` 却一直从环境上读它：结构化实现 `Environment` 的人照着 Protocol 写，类型检查通过，到协商时才失败。M28 把它补进 Protocol，第三条轴从第一天起就三样一起声明（`provided_archivers`、`bind_archivers`、`collect_archive`），不再复制那个不对称。代价写明：`Environment` 是 `@runtime_checkable`，多了这几个成员，一个没有归档面的结构化环境（测试里的 `SurfacelessEnv`）`isinstance(env, Environment)` 就变成 `False`。代码里没有任何地方做这个判断（runtime 只在有归档消费者时才碰这三个成员），以后也不要靠它分辨环境，否则不归档的 run 对 env 零调用这条保证就破了。

**setup 的任务上下文按名字路由。** `Environment.setup(context, *, negotiation)` 不广播 context：`EnvironmentBase.route_setup_context`（`environments/_registrar.py`）读每个 capability 的 `setup()` **签名**，扁平的键（`{"start_url": ...}`，`TaskSetup.to_context()` 发出的形状）只交给签名里点名了它的 capability；挂在 capability 实例 id 或 `capability_id` 下的映射（`{"browser": {...}}`）只属于那一个 capability；谁都不点名的键抛 `ValueError`，列出各 capability 声明的键。同一套路由在 `prepare()` 里就跑一遍（`check_setup`，下一段），setup 之前再跑一遍。`**kwargs` 什么都不声明，所以什么都收不到，也就不再是绕过检查的出口（`test_capability_isolation.py` 禁止 capability 的 `setup` 带它）；`negotiation` 是框架保留的参数，不是任务上下文的键。所以 capability 的 setup 一律写成 `async def setup(self, *, negotiation=None, <它接受的任务上下文键>=None)`，加一个任务上下文的键就是给 `setup()` 加一个具名参数，契约不会与实现漂移。实现了的先例是 browser 的 `start_url`；M30 加 `replay_archive`（回放的源 run 目录，§4.14）。任务上下文的词表在 §4.10；M31 把它拆成 `TaskContext{env, loop, branch}` 三部分，只有 env 那一部分走这条路由（loop 部分是 Loop 的输入，§4.1；branch 部分只进 trace，§5.3）。

**owner 确定后的 setup 输入准备（`prepare_setup_context`）。** `Environment.prepare_setup_context(context)` 是公开的环境操作，先经同一个 `route_setup_context` 确认每个 key 的真实 capability owner，再把 effective slice 交给实现可选 `SetupContextPreparation` 的 capability。该 Protocol 只有一个操作：`prepare_setup_context(slice) -> PreparedSetupContext(execution, recordable)`；普通 capability 不实现它，自己的值在两份投影中原样保留。字段属于谁由 setup 路由决定，不能在 Runtime、schema 或记录组件根据 `storage_state` 这样的名字猜 domain。相同的 flat key 仍按现合同广播给每个点名它的 owner，namespaced key 与 flat key 按原输入顺序覆盖；转换 / 剔除造成多 owner 的值不同，环境仅将那一个字段按各 owner 的 instance namespace 重新装配，既有 alias 保留。未变 flat 字段与只属于一个 owner 的 flat 字段保留 flat 形状。

`AgentRuntime.prepare` 每次建立一份 run-owned `PreparedSetupContext`，`execute` 复用其 `execution` 做检查与 setup，将 `recordable` 交给生命周期的 task_setup 记录；不二次读取输入文件。Runtime 不认识 browser 字段。`TraceWriter` 只 snapshot / redact 接到的 recordable projection，不导入 browser 或 task-schema 过滤器。value 的两个字段固定，普通 domain 的 opaque 值保留其自身可变性语义，不声称任意输入深不可变；需要冻结文件 / 可变对象的 capability 自己做。Browser 的实现冻结自己路由到的 storage seed、绝对化自己的 replay archive 路径，且仅从自己的记录投影剔除 seed 正文 / 原路径；例如 `OtherCapability.setup(storage_state="opaque-cache-id")` 的值照常执行和记录，`browser#0` 或别名实例收到的 browser seed 才会转换。不存在按 key 名递归删除的路径。

**setup 之前的检查（`check_setup`）。** 一个配置能不能和这个任务一起跑，由配置与任务数据决定，所以 `AgentRuntime.prepare()` 在协商之后、织入之前就问（§4.2 第 3d 步）：`EnvironmentBase.check_setup(context, *, negotiation, archivers)`（`environments/_registrar.py`）依次

1. 确认 capability 集合能路由（`_by_capability_id()`，两个实例撞同一个 `capability_id` 报错）；
2. 用 setup 的同一套路由分 context（`route_setup_context`：谁都不接受的键、命名空间里不接受的键都报 `ValueError`）；
3. 用 `bind_archivers` 的同一套路由分将要绑定的归档器（`_archiver_groups`：前缀对应不到 capability、capability 不归档、不提供这个键，都记一条，一次抛 `UnroutableArchiverError`）；
4. 按 capability 顺序，把每个实现了可选 Protocol `SetupCheck`（§4.9）的 capability 自己的那一份交给它：`cap.check_setup(<它路由到的 context>, negotiation=<观测协商结果>, archivers=<分给它的归档器>)`。没实现的 capability 只经过前三步的路由检查。

它不改任何状态：不 setup、不绑定、不 `apply_negotiation`。`Environment` Protocol 上有这个方法，每个 run 都调它（与归档面不同：那三个成员只在有归档消费者时才被碰），所以结构化实现 Protocol 的环境也要实现它（测试里的 `SurfacelessEnv` 原样转发）。`setup()` 自己仍按名字路由，capability 的 `setup()` 再跑一遍自己的检查（browser 如此），所以绕过 `prepare()` 直接 setup 的调用方也在启动任何东西之前被拒绝。

**Environment 通过 `__wiring__` 注入 capabilities**：

```python
class BrowserEnvironment(EnvironmentBase):
    __registry_key__ = "browser"
    __wiring__ = {
        "capabilities": ("capability", ["browser"]),
    }

class DockerTerminalEnvironment(EnvironmentBase):
    __registry_key__ = "docker_terminal"
    __wiring__ = {
        "capabilities": ("capability", ["docker"]),
    }

class CompositeEnvironment(EnvironmentBase):
    __registry_key__ = "composite"
    __wiring__ = {
        "capabilities": ("capability", ["browser", "docker", "tool_sandbox"]),
    }
```

> **wiring key 与字段名同名**：`__wiring__["capabilities"]` 配合 `EnvironmentBase` 上的 `capabilities: dict[str, BaseCapability]` class annotation——`ConfigComposer._resolve_wired` 通过 `get_type_hints` 看到 dict 注解，走 multi-instance 分支，注入 `{capability_id: instance, ...}`。同样的模式用在 `agent.llm`（single）和 `capability.backend`（single）。

**前缀路由实现**：

```python
class EnvironmentBase(metaclass=EnvironmentRegistrar.Meta):
    __abstract__ = True

    # capabilities 是 wired receptor，ConfigComposer 调 setattr(env, "capabilities", {...})
    capabilities: dict[str, BaseCapability]

    def __init__(self) -> None:
        self._fragment_router: dict[str, BaseCapability] | None = None
        # 收到非空归档器组的 capability，按 capability 顺序；bind_archivers 每次整体替换
        self._archiving: list[BaseCapability] = []

    def capability_ids(self) -> frozenset[str]:
        return frozenset(self.capabilities)

    def get_action_space(self, *, active_actions=None, **context) -> list[ActionDescriptor]:
        # 描述符的 name 已经是 dotted full key（如 "browser.click"）；不再做运行时前缀
        descriptors = []
        for cap in self.capabilities.values():
            for desc in cap.get_action_descriptors(**context):
                if active_actions is not None and desc.name not in active_actions:
                    continue
                descriptors.append(desc)
        return descriptors

    async def act(self, action: Action) -> ActionResult:
        # 路由：按 action.name 的 leading segment 找 capability
        prefix, sep, _ = action.name.partition(".")
        if not sep:
            return ActionResult(success=False, error=f"Action must be fully qualified: {action.name!r}")
        cap = self.capabilities.get(prefix)
        if cap is None:
            return ActionResult(success=False,
                                error=f"No capability handles prefix '{prefix}'")
        return await cap.execute_action(action)

    async def observe(self, negotiated) -> Any:
        # env 拥有 observation cycle：计数器在这里 +1，一次 observe = 一个 cycle。
        if self._fragment_router is None:
            self._build_fragment_router()
        slices = self._observation_slices(frozenset(negotiated.env_sourced))
        # 直接读，不用 getattr(..., frozenset()) 兜底——缺字段等于"没有必需
        # fragment"，会把失败的 required DOM 降级成一条记录，必须 fail closed。
        required = frozenset(negotiated.required_fragments)
        self._observation_cycle += 1
        cycle = self._observation_cycle

        # Phase 1：先向每个参与的 capability 宣告 cycle，再解析任何 fragment。
        begin_errors = await asyncio.gather(
            *(self._begin_cycle(cap, cycle) for cap, _keys in slices)
        )
        # Phase 2：capability 之间并发（互相独立的世界），
        # 单个 capability 内部串行（它的 fragment 共享同一次 capture）。
        harvested = await asyncio.gather(
            *(
                self._resolve_slice(cap, keys, begin_error)
                for (cap, keys), begin_error in zip(slices, begin_errors, strict=True)
            )
        )
        # 失败按 fragment 隔离，语义由协商结果裁决：required 失败在所有
        # capability 跑完后抛 ObservationFailedError（一次报全）；optional
        # 失败落成 FragmentFailure，其余 observation 照常成立。绝不伪造填槽。
        ...
        obs_model = build_observation_model_from_result(negotiated)
        return obs_model(**field_values)

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        # 先校验、后绑定：任何一个键路由不了，就一个 capability 也不碰
        by_cap = self._by_capability_id()
        groups: dict[str, list[ArchiverBase]] = {}
        problems: dict[str, str] = {}
        for archiver in archivers:
            prefix = archiver.archiver_id.partition(".")[0]
            ...  # 前缀没有 capability / capability 不是 ArchivingCapability / 不在它的 provided_archivers 里
                 # → 记进 problems；否则 groups[prefix].append(archiver)
        if problems:
            raise UnroutableArchiverError(problems, capability_ids=frozenset(by_cap))
        self._archiving = []
        for cap in self.capabilities.values():
            if not isinstance(cap, ArchivingCapability):
                continue
            group = groups.get(cap.capability_id, [])
            cap.bind_archivers(group)              # 空列表 = 清掉之前的绑定
            if group:
                self._archiving.append(cap)

    async def collect_archive(self) -> ArchiveBatch:
        if not self._archiving:
            return ArchiveBatch()                  # 什么都没绑定：不碰任何 capability
        # 每个 capability 一个任务；_collect_one 捕获 Exception 并把它返回，
        # 不用 gather(..., return_exceptions=True)——那会把 CancelledError 也吞掉
        outcomes = await asyncio.gather(*(self._collect_one(cap) for cap in self._archiving))
        ...  # 全部跑完才裁决：异常，或批次里有 capability_id 不是自己的文件或缺口，都算该 capability 失败
             # 失败的 capability 换成一条点名它的 ArchiveGap；健康的批次（连同它们自己的缺口）照样保留
        return ArchiveBatch.merge(batches)
```

**归档的路由与收集**（`core/environments/_registrar.py`，绑定的错误类在 `core/environments/errors.py`）：

- `provided_archivers`：实现了 `ArchivingCapability` 的 capability 各自 `provided_archivers` 的并集；其余 capability 什么也不贡献。
- `bind_archivers(archivers)`：按每个键的第一段经 `_by_capability_id()` 找 capability（与 `act()` 同一套路由）。下面任何一种情况都记一条问题，全部收集完一次抛 `UnroutableArchiverError(problems, capability_ids)`（`problems`：键 → 原因，按键排序写进消息；`capability_ids`：本环境的 capability id），抛错时没有任何 capability 被调用：前缀对应不到 capability；对应的 capability 不实现 `ArchivingCapability`；键不在该 capability 的 `provided_archivers` 里。校验通过后，**每个** `ArchivingCapability` 都收到自己的那一组，分不到的收到空列表，这会清掉它之前的绑定；收到非空组的 capability 按 capability 顺序记进 `_archiving`。绑定不获取任何资源（它可能发生在干跑里、setup 之前）。
- `collect_archive()`：什么都没绑定时直接返回 `ArchiveBatch()`，一个 capability 也不碰。否则每个绑定了的 capability 一个任务**并发**收集；每个任务都跑完才裁决结果，不留孤儿任务。一个 capability 抛 `Exception`、交回的不是 `ArchiveBatch`，或交回的文件或缺口里有 `capability_id` 不是它自己的（后两条记成点名该 capability 的 `ArchiveContractError`，最后一条还点名那些 `archiver_id`），都算它失败：它在合并结果里只留一条 `ArchiveGap`（`archiver_id` 是它的裸 id，`error` / `detail` 是那个异常），一个文件也不留。健康 capability 交回的批次照样保留，连同它们自己点名的缺口；按 capability 顺序把各批次合并成一个 `ArchiveBatch`。capability 的失败从不让这次收集抛错：记录器把每条缺口记成一条记录问题，与 agent 的结局无关（§4.6）。`CancelledError` 原样传播，包括某个 capability 自己抛出它的情形：`gather` 这时会让兄弟任务接着跑，所以先取消还没结束的任务、等它们收场，再原样重抛，同样不留孤儿任务。
- 收集不带任何参数：environment 从不把步号（也不把 cycle）交给 capability。"第 t 步的提交取走的就归第 t 步"是记录器那一侧的规则（§4.6）。

**检查点恢复的路由（M31）**（`core/environments/_registrar.py`）：恢复器（§4.1 `BranchLoop`）经 environment 调用两个只读的方法，capability 侧的协议是 §4.9 的 `RestorationSupport`；另有一个为下一次观测预约对齐的方法，capability 侧的协议是 §4.9 的 `RestorationAlignment`（列在最后）：

- `retarget(action, recorded, restored) -> Retarget`：与 `act()` 同一套前缀路由（`action.name` 的第一段经 `_by_capability_id()` 找 capability）。路由到的 capability 实现了 `RestorationSupport` 就交给它；没实现、前缀不带点、或前缀找不到 capability 时返回恒等的 `Retarget(action=<原动作的副本>, method="identity")`。重放要复现源 run 的执行，连同它被 `guarded_act` 拒绝的那些动作：原样交给 `act_fn`，由它再拒绝一次，成败比较照样成立。`retarget` 从不执行动作，也不改环境。
- `verify_restoration(recorded, restored) -> list[RestorationEvidence]`：按 capability 顺序扇出到每个实现了 `RestorationSupport` 的 capability，各交回一份证据（带自己的 `capability_id`）；没有任何实现者时是空列表，即"没有可校验的"。`recorded` 与 `restored` 都是本次协商的整个 `NegotiatedObservation`，capability 按自己 fragment 的本地名读它要的那几个（协商保证本地名不撞）。
- 两者都只量、不判：阈值与 `strict | warn` 是恢复器的策略（§4.1），capability 只报告测到了什么，没测的字段为 `None`。environment 不缓存、不记录任何东西。
- `align_next_observation(recorded, *, media_seek_timeout_s, media_end_lead_s) -> None`（2026-10-20 裁决的媒体对齐）：按 capability 顺序扇出到每个实现了 `RestorationAlignment` 的 capability，没有实现者时什么也不做。它不在这里改环境：capability 只记下"下一次观测要对齐到 `recorded`"，对齐在那一次观测的采集里、落定之后、截图之前执行，做了什么由紧接着的 `verify_restoration` 作为证据交回（§4.14）。预约只管紧接着的那一次观测：`verify_restoration` 会清掉没用上的预约，所以它不会漏到检查点之后的观测里。只有恢复器调用它，而且只在 `media: align` 时。

**fragment 的格式说明**（`core/environments/_registrar.py`，§4.4 "观测格式由提供方描述"）：`fragment_formats() -> dict[str, str]`：按 capability 顺序合并每个实现了 §4.9 `FragmentFormats` 的 capability 交出的说明（全限定键 → 文字）；没实现的 capability 什么都不描述。capability 只为自己的 fragment 说话：交出的键不在 `<capability_id>.` 前缀下时抛 `ValueError`，不让它替别的 capability 的 fragment 写说明。只读配置：`AgentRuntime.prepare()` 第 2b 步在 setup 之前调它，而且只在 Agent 引用的 fragment 这个 run 确实激活了时才调。

**录下的采集与本配置是否一致（M32）**（`core/environments/_registrar.py`）：训练导出用学生的运行配置组装一个从不 setup 的环境，再问它"你按现在的配置采，会不会采出源 trace 录下的那种观测"：`compare_capture(recorded: Mapping[str, FragmentBase]) -> CaptureComparison`（`recorded` 是一步录下的 env 来源 fragment，键全限定）。按每个键的前缀经 `_by_capability_id()` 分给 capability，交给实现了 §4.9 `CaptureCheck` 的那些，按 capability 顺序合并它们的 `mismatches` 与 `unchecked`；没实现的 capability、找不到 capability 的键都不贡献（"没有可比的"）。只读配置与传进来的 fragment：不 setup、不连浏览器、不缓存。

**Observation cycle 由 env 宣告，不由 capability 推断**：`Environment.observe` 持有单调递增的 cycle 计数器，每次 observe 自增一次，并在任何 fragment 解析之前调用每个参与 capability 的 `begin_observation(cycle)`。这正是 capability 敢批量抓取的前提——browser 一次 `BrowserStateRequestEvent` 同时拿到 DOM + screenshot + tab list，然后在本 cycle 内把每个 key 服务任意多次，因为下一个 cycle 会被**告知**而不是被**猜到**。此前这条规则只以"我是否已经服务过这个 key"的形式活在 `BrowserCapability` 内部：那是用推断代替调用方本来就握有的信息，并且对任何读同一 fragment 两次的调用方都是错的。同一步内重复读同一个 fragment 是**同一次** observation，不是下一次。

**全限定 dotted key**：每个 action 的 `__registry_key__` 是 `"<source>.<id>"`（如 `browser.click`），所以无论 single-cap 还是 composite env，`action.name` 都已经带前缀，`act()` 只做路由不做改写——单 cap 场景也透明工作。Composite 场景（如 GAIA）的 `browser.click` / `tool_sandbox.call_tool` 通过同一段路由分发到对应 capability。

**但"声明"不用全限定键（capability-relative wiring）**：注册 key、`ActionDescriptor.name`、`act()` 路由、trace 文件名全部保持全限定——上面那段路由一个字都不改。唯一让出前缀的是 **agent / loop 的 `__wiring__` 声明面**：写 `"click_coordinate"` 而不是 `"browser.click_coordinate"`。理由是同一个字符串原本被要求同时承担三件互不相干的工作——(a) 注册表全局唯一、(b) 运行时分发路由、(c) agent 声明自己需要什么。(a)(b) 确实需要 capability 前缀，(c) 不需要："我会按坐标点击"这句领域知识天生与 capability 无关。让 (c) 继承前缀的后果是每个 agent 被焊死在一个 capability 上——UI-TARS 是 computer-use 模型，自己的 config 提供 COMPUTER_USE / MOBILE_USE / GROUNDING 三种模式，却只能对着 `browser` 协商。

- **展开**：`ActionRegistrar` / `ObservationRegistrar` 用 `key_separator="."` 创建，conscribe（≥1.3.0）在 config 生成期把不带分隔符的 wiring key 展开成所有尾段匹配的全限定 key，于是生成的 `Literal[...]` 覆盖每个可能的 provider。conscribe 只做**加宽**，不做**裁决**——生成期它不可能知道某次装配持有哪些 capability。
- **裁决**：协商期收窄。`negotiate_action` / `negotiate_observation` 在声明空间里跑集合运算，再把每个声明 key 绑定到本次 environment 里唯一提供它的全限定 key。绑定表（short → 全限定）只在协商期算一次，挂在协商结果上（`action_bindings` / `fragment_bindings`）；下游一律读表，不得自己再推一遍。
- **消歧靠拒绝**：一个 relative key 若被本 environment 里两个 capability 同时提供（`browser.screenshot` vs `desktop.screenshot`），抛错并把两个候选全限定名都报出来。按 wiring 列表顺序挑一个、或让 agent 声明 capability 亲和度，都被明确否掉：前者是静默降级，后者把 F10 刚拆掉的耦合又装回去。
- **逃生舱**：在 wiring 里直接写全限定 key 即钉死 provider（含消歧场景），这也是上面那条报错给出的修法。
- **收窄发生在唯一一处**：`build_guarded_act`。它本来就包住了每一个 action，所以 agent 吐出的 relative name 在进 `env.act` 之前就已经变回全限定名；此后整条链路（路由 / descriptor / WAL）只见全限定键。规则本身只有一份：同模块的 `resolve_action_name(name, active_names, bindings)`，守卫对经手的每个动作应用它；需要知道守卫把一个动作叫成了什么的调用方（`BranchLoop` 重放时拿重新解析出的动作与录下的比较，§4.1）也问它，不自己再推一遍。

**Agent 声明 capability 需求**（通过 `__wiring__`，替代 `required_capabilities` property）：

```python
class BrowserUseDomAgent(BrowserUseAgent):  # browser_use.dom；agent_loop 与 llm 在家族根上
    __wiring__ = {
        # 一个成员写出它完整的需求，不在别的声明上做增减。
        "observation": ("observation",
                        ["dom", "screenshot", "platform", "task", "browser_meta"],
                        ["step_info", "history", "capture_state"]),
        "action": ("action",
                   ["navigate", "done", "click", "type"],
                   ["scroll", "send_keys", "extract_content", "go_back",
                    "go_forward", "switch_tab", "wait"]),
    }

class GAIAAgent(AgentBase):
    __wiring__ = {
        "agent_loop": ("agent_loop", ["react", "plan_execute"]),
        "observation": ("observation", ["dom", "screenshot", "tool_result"], []),
    }
```

注意两处：(1) 没有 `capability` 条目——写它只会把生成的 config 收窄到某个 capability，运行时没人读；capability 归属由协商在 action / observation 两个轴上回答。(2) 每个成员写出完整的集合：`dom` 在两个 BrowserUse 成员上都是 required，所以"跑 DOM 却配了 vision_only profile"在协商期大声失败，而不是静默降级成视觉模式（§4.1 "BrowserUse 家族"）。

运行前 AgentRuntime 自动验证兼容性：Agent 需要的 fragment / action ⊆ Environment 提供的集合。

### 4.4 Observation Fragment 体系 + 协商

**设计决策**：Observation 不再是万能袋，而是 Fragment 注册表 + `__wiring__` 三方协商 + Resolver 模式。Fragment 是语义不可分的一组字段，独立注册在 conscribe；Env(Capability)/Agent/Loop 通过 `__wiring__` 声明 provides/requires；config 时 Avro 式协商 → 动态生成 Observation model → Capability 的 Resolver 按需计算。

**观测增强按消费方分层（2026-10-20 裁决，取代 2026-10-19 的“所有使用 BrowserCapability
的 Agent 统一获得浏览器观测”；已实现，2026-10-20 合入 main）。** 10-19 的表述把 UI-TARS 也算进浏览器统一
观测，这是误记：UI-TARS 是跨平台的 computer-use Agent，只消费不带平台假设的通用合同；
Skyvern、BrowserUse 是浏览器专用 Agent。新增信息按消费方分三类，全部经现有 fragment、
Resolver、同周期采集、协商与持久化合同交付，**协商规则不变**，不绕过协商，不给其他
capability 的 Observation 强加浏览器字段：

| 信息 | 合同 | 消费方 |
|---|---|---|
| 动作效果：滚动位移、落点与命中、焦点、导航结果等 | `ActionResult.effect`，由动作类的嵌套 `Effect` 模型定义（§4.5“动作效果”） | 所有 Agent，渲染 `effect.describe()` |
| 跨平台采集状态：落定、前台界面变化、指针、可见资源加载失败 | `capture_state` fragment（下文“跨平台采集状态”） | 所有 Agent，以 optional 声明 |
| 浏览器页面状态：URL、标题、tabs | 现有 `browser.browser_meta` | 仅浏览器专用 Agent，required |
| 媒体播放状态：每个媒体元素的位置、暂停、总时长（2026-10-20 裁决） | `browser.screenshot` 的 `media` 元数据（下文“截图的媒体状态”） | 恢复校验与对齐；浏览器专用 Agent 仅在 `show_media_state: true` 时渲染 |

呈现归 Agent，但同一事实只有一种写法：跨平台事实由 `agents/rendering/capture_state.py::render_capture_state(observation, *, can_wait, write_point=...)`
渲染，浏览器页面状态由 `agents/rendering/browser_state.py::render_browser_state(observation)` 渲染并在内部复用前者，
先例是 `render_browser_platform`。点的写法归框架（2026-10-22 与 2026-10-23 的裁决，§4.1"模型看到的每个点都写在它写点的那个网格上"、"坐标系归框架"）：
`write_point` 是一个把 viewport 点写成文本的函数（`schemas/action.py::PointWriter`，与动作效果的 `describe` 共用），
指针经它写出（`render_browser_state(observation, *, show_media_state, can_wait, write_point=...)` 原样转交；`can_wait` 见下文“初次加载是否完成”）；默认写 `(x, y) in viewport pixels.`。
Agent 传的是框架的 `point_writer(context.frame, viewport=...)`：本 run 没有定坐标系时（`skyvern.dom`、没有坐标动作的 BrowserUse）它就是默认，提示词不变；
UI-TARS、有坐标动作的 BrowserUse 与 `skyvern.dom_vision` 得到本步坐标系上的写法，指针与它们写的点在同一个网格上。事实本身仍是 viewport 单位，trace 不变。两个函数按相对名结构化读取 fragment，不 import capability 或 resolver
类；Skyvern、BrowserUse 删除各自的 `browser_meta` 渲染，改用后者。ActionResult 的执行成败、动作效果、
后续观测和任务是否达成保持独立；未测量不等于零，批次后的状态不归因到其中某一个动作，敏感值不回显。
媒体播放状态（2026-10-20 裁决）是 `browser.screenshot` 的附带元数据（下文“截图的媒体状态”），
与像素同一次采集，所有协商了截图的 Agent 都带着它、默认不进提示词；浏览器专用 Agent 可经
`render_browser_state` 渲染它，开关是 Agent 的 YAML 字段 `show_media_state`（默认 `false`，显式实验变量）。
UI-TARS 不变。后续工作记录在 `.agents/skills/af-project-history/references/enhance.md`。

#### 跨平台采集状态 `capture_state`

通用基类 `CaptureStateFragment`（abstract）位于 `schemas/observation/`，只定义下面四项事实及其缺失
语义，不含平台词汇；各 capability 用自己的前缀注册具体 fragment，本轮是 `browser.capture_state`，以后
`android.capture_state`、`desktop.capture_state` 继承同一基类。Agent 用相对名 `capture_state` 声明为
optional：环境提供就激活，不提供照常运行。**它从不让采集失败**（2026-10-20 裁决）：浏览器一次采集服务全部
已协商的 browser fragment，缺任何一个就整次重采并最终整体失败（截图也一起失败），所以 resolver 把探测超时、
出错或回复不合形状都记成 fragment 内对应事实的 `unknown`（附简短 `detail`），永远应答。只有整次采集失败时，
这个 optional 槽位才会和其他 browser fragment 一起成为 `FragmentFailure`；`capture_state` 自己的问题不会牵连
截图。以后可能允许对 optional fragment 做部分采集，届时再改这条合同。

| 事实 | 含义 | 缺失语义 |
|---|---|---|
| 落定 | 采集前等待的结果 `settled` / `capped` / `unknown`、等待时长、尚未落定的项（capability 自定义标签）；`unknown` 时附读不到状态的原因 `detail`；被采集界面的初次加载是否完成 `loaded`（下文“初次加载是否完成”） | 等待关闭时为 `None`；`loaded` 不知道时为 `None` |
| 前台界面变化 | 采集前 capability 因环境自身行为移动了焦点：页面打开了新界面，或当前界面自行关闭；从哪到哪 | 没有移动为 `None`；Agent 自己的切换动作不算 |
| 指针 | 同一界面最后一次确认的指针位置（viewport 单位；提示里由 Agent 的 `write_point` 写，见上文），状态 `known` / `unknown` / `unavailable`（平台没有指针） | 与截图光标展示开关无关 |
| 可见资源加载失败 | 采集时视口内加载失败的可见资源数量及简短标识；本轮浏览器只测可见图片 | 测不到为 `unknown`，平台不支持为 `unavailable`；`0` 只表示测得为零 |

离线回放审查也读回放步骤记下的“可见资源加载失败”（只数源步骤没有的），作为视口内缺图事实的第二个来源（§4.18）。

**初次加载是否完成：`SettleState.loaded`（用户 2026-10-25 的裁决）。** 录制走真实网络，慢的时候 teacher 会在主文档还没加载完的页面上做决定，而同一步的离线回放从归档取、早已加载完；这样的步当分叉检查点或导出样本，学的是一个回放里并不存在的状态。15 个真实录制、409 个策略步的实测：落定 63%；只因 DOM 一直在变而到上限（`pending` 只有 `dom`）34%，大多是永远静不下来的页面（播放器、轮播、行情）；`load` 没等到而到上限 3%。裁决：（1）只有“主文档没有加载完”算加载不完整的确定证据，只是 DOM 没静下来不算；（2）在这样的页面上采集的步不能选作分叉检查点，也不能成为导出样本；（3）明白告诉模型这次采集时还没加载完，并告诉它可以等。录下的 trace 不变：排除只发生在派生的选择里（分叉任务加载、导出），每个被排除的步都带原因报告。

- **事实**：`SettleState.loaded: bool | None`，平台中立的一项：被采集界面的初次加载在采集时是否已经完成；消费方（渲染、加载器、导出）只读它，从不解读 capability 的 `pending` 标签。`False`：初次加载还在进行，采集到的是一个还在第一次加载的界面——加载不完整的确定证据；`True`：初次加载已经完成；`None`：不知道（capability 说不出来）。它是采集时记下的、关于界面的事实，不是从等待怎么结束推出来的，所以没有哪个 `outcome` 蕴含它的取值，也没有校验器把两者绑在一起：capability 可以不再等一个永远完成不了的加载，这时连 `settled` 的等待之后采集到的也可能是还在加载的界面（下一条）；`capped` 与 `unknown` 之后三种取值也都可能。字段是必填的，每个 capability 都要说。等待关闭时整个 `SettleState` 是 `None`，同样没有证据。
- **浏览器**：每次采集在落定等待结束时（紧接着采集之前）记下 `PageSettle.main_document_loaded`（§4.14 “记录”）：主 frame 当前文档是否已经到了它的 `load` 生命周期事件，读这个 tab 的 `MainFrameLoad`（落定等待自己用的那个跟踪器，`navigation.py`；启用生命周期事件时 Chrome 会把当前文档的事件重报一遍，所以接管的 tab 也知道）。跟踪器还没听到主 frame 的任何生命周期事件、没有跟踪器、或 tab 已经关闭，记 `None`，从不记成 `True`。它与等待的结果无关：一个文档的加载在所有观测里加起来最多等一个上限（§4.14 “上限”），加载永远结束不了的文档在花掉上限的那一次采集记 `capped`、`waiting_for: [load, dom, content]`，之后同一文档的采集不再等它的加载、可能记 `settled`，但每一次都记 `main_document_loaded: false`；两次采集之间 `load` 来了，下一次就记 `true`；换了新文档，从新文档重新算。`unknown` 的等待（探针读不到页面）照样记这项事实。`BrowserCaptureStateObs.settle_state` 的 `loaded` 就是这个记下的值。字段带默认值 `None`（没有这个键的记录就是不知道），按“兼容性演进规则”（fragment 内新增有 default 的字段，BACKWARD ✓）trace 格式版本不升；这项改动之前的录制已全部删除，测试数据重新生成。
- **读录下的步**：`core/trace/sources.py::unloaded_capture_reason(trace, step)`，分叉任务加载器与导出共用：在源 run 协商的 env 来源 fragment 里找本地名是 `capture_state` 的那一个（协商不让两个 fragment 落到同一个本地名上，所以至多一个），读这一步录下的它，`settle_state.loaded is False` 时给出原因，否则 `None`。run 没协商它、这一步没录它、它的采集失败了、或者没有等待，都是没有证据：不排除，也不拒绝。
- **使用者**：提示词（下一条）；`trace_branches` 的 `exclude_unloaded_checkpoints`（§4.10 “没加载完的检查点”）；导出的 `filters.exclude_unloaded_steps`（§4.15 “源级检查与筛选”）。两个开关默认都是 `true`，发布的飞轮配置都显式写出。
- **提示词**：`render_capture_state(observation, *, can_wait, write_point=...)` 在 `loaded is False` 时，`<capture_state>` 块的第一行先写一句平白的话 `The content had not finished loading when this was captured (its initial load was still in progress).`；本 run 的有效动作空间里有等待动作时，同一行接着写 `You can use the wait action.`；下一行起照旧是技术细节（`Settle: capped: the wait ended at its limit after 5002 ms; still pending: load, dom, content.`）。`loaded` 是 `True` 或 `None` 时块不变。渲染函数与跨平台的 UI-TARS 共用，措辞不含平台词汇（写 content，不写 page）。`can_wait` 由 Agent 给：它在 `prepare` 里拿到本 run 的有效 `ActionSpace`，用框架的命名助手 `executed_name(space, "wait")`（§4.5）问自己的 `wait` 在这个 run 里是否可执行（UI-TARS 问它按同一个助手建起的编解码：`"wait" in codec.verbs`）；渲染函数不碰动作空间，`render_browser_state(..., can_wait=...)` 原样转交。Skyvern 不声明 `wait`，它的模型只读到那句事实。用 `history_source: source` 从这项改动之前的录制分叉时，源 prompt 里没有这句话，种子与历史的精确重放会在没加载完的那些步上对不上（任何提示词改动都是这样；录制是可替换的测试数据）。

浏览器由一个新的 Resolver watchdog 提供 `browser.capture_state`，与其他 fragment 同一次采集、共用同一次
落定等待。落定与前台界面变化从同一次采集的 `PageSettle`、`FocusMove` 投影，不另测一遍；两者仍留在每个
浏览器 fragment 上作为 trace 证据。指针读同 tab 的 `PointerPosition`。BrowserCapability 本身不改：新增
fragment 就是新增一个 Resolver watchdog。

配置全部在 YAML 里：探测类事实（例如可见图片加载失败）的时间上限是 BrowserCapability 的
`capture_state_timeout_s`（默认 1 s，最多 3 s；它加上两倍的 `media_state_timeout_s` 必须小于一次采集留给探测的 4 s，见 §4.14 "命令上限"），超时记为 unknown；每个 Agent 的 `show_capture_state`（默认 `true`）为 `false`
时，经“实例在类声明空间内收窄”排除 optional 的 `capture_state`，这个 fragment 不采集也不渲染，协商规则不变。

**这个开关属于分叉与导出的实验身份**（2026-10-20 裁决）。它改变协商出的 fragment 集合，而 `BranchLoop`
要求与源录制的观测协商完全一致（§4.1“组装检查”），训练导出要求学生协商出的每个 env fragment 源里都录过
（§4.15“片段与采集检查”）。所以 `show_capture_state` 取值不同的录制之间不能互相分叉，也不能混在一起导出；
录制时没有 `capture_state` 的旧 run 也不能被协商它的 Agent 分叉。

#### 截图的原图与模型视图

浏览器 capability 的 `screenshot_cursor`（默认 False）与 `screenshot_cursor_size`（默认 32 CSS px，16–96）决定模型图片里是否显示放大、高亮的光标。它们属于 capability 的 `Annotated` 标量配置与实验身份，不属于 Agent 或 profile，也不改变 DOM 的 `debug_highlight`。

`ScreenshotObs.image_bytes`、`trace_binary()` 和 `RunTrace.screenshot()` 始终是干净的原始截图；恢复的 dHash 与 evaluator 继续读原图。fragment 的 `cursor` sidecar 元数据保存截图时同 tab 最后一次成功确认的 `mouseMoved` 坐标（CSS px）及呈现大小和编码质量；`cursor=None` 是关闭，`cursor.position=None` 是已启用但位置未知。冷启动用于轨迹的猜测位置不算事实，wheel 的目标坐标也不算鼠标移动。移动尚未确认、超时、失败或取消时，指针位置回到 unknown：未收到确认不能证明浏览器没有移动；下次成功确认才恢复 known。每个 tab 的 `PointerPosition` 由同 tab 输入控制器更新、截图 resolver 经 snapshot 回调读取，不采集逐 tick 轨迹；不同 tab 和 run 不共享它。

现有图片消费者通过通用 `ModelImage.model_image_bytes()` / `read_model_image()` 取得公开的模型视图，Agent 不认识浏览器绘图配置；纯文本 Agent 保持自身的输入模态。已知位置时，浏览器 fragment 用固定矢量和 Pillow 纯函数，从原图与少量元数据确定性重建图像，按实际图像像素与 CSS viewport 的两轴比例绘制，不改尺寸或坐标网格。位置未知时保持原图字节。PNG/JPEG 格式来自实际字节，JPEG 模型视图用记录的质量重编码。缓存只属于 fragment，所有原图、几何和光标输入变化都使缓存失效。`from_trace` 恢复原图和元数据后可重建同一模型视图，不在 JSON 中保存第二张 bitmap。

完整 LLM 调用记录沿用 `PromptImageRef` 保存实际发送的模型图片，内容寻址；环境 screenshot artifact 仍只有干净原图。是否记录图片仍由 `RecordingPlan` 各通道决定，光标开关不额外启用 prompt 记录。训练导出用已有 `CaptureCheck` 比较呈现开关、大小和 JPEG 质量，不能默默把源图呈现为另一种输入。Branch 的 `history_source: source` 按源 fragment 的元数据重建同一模型视图，再精确比较原调用；`restored` 使用实际恢复观测的视图。新 policy 步读取当前 capability 的配置，呈现开关是显式实验变量，不把它扩成所有 branch 环境配置都必须相同的限制。

#### 截图的媒体状态（2026-10-20 裁决）

小红书任务 30 的诊断（本地的 `tmp/media_playback_20261020/`）：视频在源 run 里按真实时间播放，模型思考的几十秒也在播；原路径回放不等模型时间，同一步的画面停在不同位置（源 00:41、回放 00:10），截图感知哈希的距离却可能在阈值之内。播放位置因此是要记下、要比较、能对齐就对齐的环境状态（§4.14 "恢复：重定位与校验"）。

- **放在哪里**：`ScreenshotObs.media: MediaState | None`（`core/capabilities/browser/media.py`），与 `source_url`、`cursor` 同类的 sidecar 元数据：与像素同一次采集，随截图 sidecar 写入，由 fragment 自己读回。`None`（trace JSON 里不写这个键）是没测：这个字段之前的证据、手建的 fragment。
- **测什么**：页面上每个 `<video>` / `<audio>` 元素一条 `MediaElementState`：所在 frame（主 frame 是 `main`；子 frame 写成路径，每一段是该 frame 在父窗口 frame 列表（`window[i]`）里的序号，如 `main/1`：跨源的父窗口也读得到这个序号，所以跨站 frame 从里面算出的路径与父文档看到的相同）、它在该 frame 的媒体元素里的文档顺序、标签、顶层视口里的位置框（CSS px；子 frame 里的元素加上各级 frame 内容框的偏移）、来源种类 `url` / `blob` / `stream`（`srcObject`）/ `none` / `other`、`url`（只在 `url` 种类时写；`blob:` 地址每次加载都变）、总时长（未知为 `None`）与 `live`（总时长无限：直播或流）；以及截图**之前**、**之后**各一次读数 `MediaReading{current_time, paused, seeking, ready_state, ended}`：`current_time` 是元素的精确 `currentTime`，不是网站显示的 mm:ss（实测一秒之内画面可以差 7–9 位 dHash）。自己的读数没有应答的子 frame（超时、出错、找不到会话）记成 `unreachable_frames`（frame 路径与位置框）：里面有没有媒体不知道。截图前后两次读数里它都没有应答才算；应答了其中一次，它的媒体就是知道的，用那一次的读数。影子树（shadow root）里的 iframe 不在父窗口的 frame 列表里，算不出自己的路径，读数也就从不应答：父文档把它列成 `<父路径>/unplaced-<k>`，它是读不到的 frame，从不当作没有媒体。
- **怎么测**：截图 resolver 在 `Page.captureScreenshot` 之前、之后各读一次。每次读数里**每个 frame 在它自己的 isolated world `af-media` 里读自己的文档**（每个文档一个，按名字复用，页面脚本看不到它）：tab 自己渲染的 frame（主 frame、同源与同进程的子 frame）经 tab 的会话（`Page.getFrameTree` 列出它们），进程外的跨站 frame 经那个 frame 自己的 CDP 会话（2026-10-21 裁决；会话由 capability 为每个 tab 持有的唯一一个 `FrameTargets` 发现，不论 profile 是否采集跨站 DOM；profile 采集跨站 DOM 时 DOM resolver 用的也是这一个：Chrome 只把一个会话上已经附着的 frame 报给那个会话上的第一次 `Target.setAutoAttach`，同一个 tab 上的第二个登记表永远听不到它们——实测 standard profile 下媒体读数曾因此把每个跨站 frame 都记成读不到）。所有 frame 一起读、共用一个时限；主 frame 没有应答，这次读数整个记为未知；某个子 frame 没有应答，它进 `unreachable_frames`。每次读数的上限是 `BrowserCapability` 的 `media_state_timeout_s`（默认 1 s，最多 1.5 s；每次采集花两次，两倍的它加上 `capture_state_timeout_s` 必须小于一次采集留给探测的 4 s，见 §4.14 "命令上限"）；超时、出错或回复不合形状，这次读数记为 `unknown`（`read_before` / `read_after`，附 `detail`），从不让截图失败；测不到的值是 `None`，不是 0。录制行为不变：不暂停、不跳转、不改任何媒体元素。
- **谁读它**：恢复校验比较两边的媒体状态，恢复时的媒体对齐以录下的媒体状态为目标（§4.14）；回放规则集的媒体区域规则声明的页面上，恢复校验不判媒体状态，只用它测到的位置框（媒体元素与读不到的 frame）把截图比较里的媒体区域遮掉（§4.14 "页面上的媒体区域"）。提示词默认不含它；Skyvern、BrowserUse 的 `show_media_state: true` 时，`render_browser_state` 在页面状态之后写一段 `<media_state>`（每个元素的 frame、顺序、标签、当前时间 / 总时长、播放或暂停、是否直播；没测到就明说没测到）。这个开关改变提示词，是显式实验变量；它不改变协商。UI-TARS 不变。
- **测不到的**：画布自己画的播放器没有媒体元素，只能靠截图距离发现；自己的读数没有应答的 frame 里的媒体不知道（上面的 `unreachable_frames`，恢复校验据此判这一步不可恢复，§4.14）；影子树里的 iframe 永远读不到（同上，`<父路径>/unplaced-<k>`），页面里有这样的 iframe 时每一步都不可恢复（2026-10-21 裁决：接受这个保守的限制；要读它们得改用 CDP 的 frame owner 给 frame 命名，不用 `window[i]` 序号，等真实站点出现误判再做）；直播没有可比较的位置。

#### 观测格式由提供方描述（用户 2026-10-23 的裁决）

**规则**：fragment 的文本格式由写出这段文本的一方描述，Agent 的提示词原样引用这段描述，不再自己描述格式。浏览器的 `browser.dom` 文本由配置的 DOM 序列化器写出（§4.12），格式说明也归它：每个序列化器实现 `describe_format() -> str`（`DOMSerializerProtocol` 的方法），用几行模型读得懂的话说明元素行的样子、哪些元素可以操作、动作怎样用序号指代一个元素（动作的 `index` 参数），以及它会写出的其余每一种标记；说明按实例写，旋钮（`max_text_length`、`max_href_length`）算在内，只说它真会输出的东西。换序列化器，提示词随之改变，格式知识只在一处。起因：Skyvern 的提示词教 `[42]<button …>` 前缀，发布配置（`configs/skyvern_agent_local.yaml`）用的 `html` 序列化器写的却是 `data-idx="42"`；单元测试喂给 Agent 的是手写的 `[1]<button>`，没有东西比对两边。这是"Agent 是胶水"的一步：格式是 capability 一侧的知识，Agent 只负责把它放进自己的提示词。

**合同**：说明是一个 run 的配置事实，走 run 级的通道。

| 环节 | 位置 | 做什么 |
|---|---|---|
| 序列化器 | `dom/serializers/__init__.py::DOMSerializerProtocol.describe_format` | 本实例输出的格式说明 |
| capability | 可选 Protocol `core/capabilities/base.py::FragmentFormats.fragment_formats() -> Mapping[str, str]`（§4.9） | 全限定 fragment 键 → 说明；browser 声明了 `browser.dom` 时返回 `{"browser.dom": dom_serializer.describe_format()}`，`vision_only` profile 没有 DOM resolver，什么都不描述 |
| environment | `Environment.fragment_formats() -> dict[str, str]`（§4.3） | 实现了 `FragmentFormats` 的 capability 的并集；capability 描述了不在自己前缀下的键时抛 `ValueError` |
| Agent 声明 | `__fragment_formats__: ClassVar[frozenset[str]]`，用 `__wiring__` 里的名字（BrowserUse、Skyvern：`{"dom"}`） | 本 Agent 的提示词引用哪些 fragment 的格式 |
| Runtime | `AgentRuntime.prepare()` 第 2b 步（§4.2）：`core/negotiation/fragment_formats.py::quoted_fragment_formats(agent, negotiated_obs, env)` | 按协商结果的 `fragment_bindings` 找到每个声明名的全限定键；这个 run 没激活的（optional 而环境没有、实例排除了）没有条目，也不问环境；激活了而提供方不描述的抛 `FragmentFormatError`（点名 Agent、fragment 与提供方）；声明不是名字集合、名字不在观测 wiring 里、`prepare` 没有显式的 `fragment_formats` 关键字参数，同样 `FragmentFormatError`。都在 setup 之前、任何模型调用之前 |
| Agent 收到 | 原有的 `prepare(..., fragment_formats=...)` 种子 slot（§4.2 `seed_messages`），只读映射，键是声明名 | 系统提示原样引用 |

这与执行限制是同一种显式消费声明（§4.1 "执行限制显式传递"）：不声明的 Agent（UI-TARS）签名不变、什么也收不到，Runtime 不按参数名机会性传入。

**为什么走 `prepare()`，不走 fragment。** 候选有三条。放在协商出的 `browser.dom` fragment 上：格式由配置决定，每步都一样，放进每步的观测就是每步重复同一段文字，trace 里每个 `browser.dom` 文件也多一份；而系统提示只在 `prepare()` 里建一次，那时还没有观测，说明只能挪进每步的用户消息。另两条（经 `prepare()` 交给 Agent、capability 提供的 fragment 元数据）其实是同一条路的两端：capability 是来源（它持有配置好的序列化器实例），`prepare()` 是通道。所以来源是 capability 的 `FragmentFormats`，通道是 Agent 的 `prepare` 种子 slot，与动作轴对称：capability 的 action descriptor 经 `get_action_space` 与守卫成为 `ActionSpace`，也是从 `prepare` 进 Agent。Agent 不 import 序列化器类，也不按序列化器名分支，只按自己 wiring 里的名字（`dom`）取一段文字。说明只读配置：不 setup、不连浏览器，干跑与训练导出的学生装配（§4.15）都拿得到。

**提示词**（只换格式段落，其余原文不变）：

- **Skyvern**：`<element_tree>` 一节以 `{{ dom_format }}` 引用说明，删去手写的 `[42]` 示例与"没有 `[index]` 前缀的行不可交互"；保留"序号是指代元素的唯一方式，动作写成 `"index": 42`"与其余规则。发布配置因此第一次教对了 `html` 的写法。
- **BrowserUse**：四个 DOM surface 片段（`dom`、`dom_vision` 与两个 terse 版）各有一个槽位 `{dom_format}`，由 `_render_surface` 填入（片段里不允许别的占位符，`test_prompt_tree.py` 钉住）；删去手写的格式、示例与 `*[`、`|SCROLL|`、`|SHADOW(...)|` 的说明（`|SCROLL|` 与 `text` 实际写出的 `|scroll element|` 不符，`|SHADOW(...)|` 标的是 shadow host，不是 shadow root 里的元素）；"按数字 `[index]` 指代"改为"按本步列出的序号指代"。terse 版原来的一行压缩说明（`[index]<type>text</type>`，与输出也不符）换成同一段说明。三个 `is_browser_use_model` 模板原来不描述 surface（检查点学过 DOM 格式），配 `html` 序列化器时模型读到一种没人说明、也没学过的格式；2026-10-23 起它们也以 `{surface}` 引用 terse 片段，与其他模板一样教本 run 序列化器写的格式，`browser_use.dom_vision` 还带截图一句与 `<coordinates>` 段。没有选在 `prepare()` 拒绝"微调模板 + `html`"：那要 Agent 认出是哪个序列化器写的 DOM，正是本节不许的按序列化器分支；引用说明则让这个组合成为写在提示词与 trace 里的显式实验条件。纯视觉的 `vision` 片段随纯视觉成员删除（§4.1 "BrowserUse 家族"）。
- **UI-TARS** 不变。

Skyvern 与 BrowserUse DOM 模式的系统提示变了，所以这次改动之前的这些录制不能用 `history_source: source` 分叉（种子检查不符），用 `restored` 或重新录制。每步的用户消息不变。BrowserUse 三个微调模板引用 surface 之后同样如此。

**测试**：`test_browser_dom_serializer_format.py`：每个序列化器的说明点名自己的序号标记、不提别的序列化器的，真实 `DOMState`（BrowserUse 清洗器跑合成 CDP 载荷、Skyvern 清洗器跑提交的真实 Chrome 采集）里每个显示的已索引元素都带这个标记；`text` 写出的每一种标记都在说明里，`html` 的说明不提它不写的标记；说明跟着实例旋钮。`test_dom_format_prompts.py`：发布 YAML 换成 `text` / `html` 组装，走真实的 Runtime 装配，配置的清洗器产出的真实 `DOMState` 经配置的序列化器进观测：系统提示含说明原文，它教的标记就是观测里每个元素带的，别的标记不教；换序列化器时系统提示只在说明处不同；发布的 Skyvern 配置教 `data-idx`。另有解析规则（`test_negotiation/test_fragment_formats.py`）与 environment 汇总（`test_environments/test_fragment_format_routing.py`）。

**未处理**：训练导出的采集一致性检查（`CaptureCheck`，§4.9、§4.15）不比较序列化器，学生配置的说明可能与源 run 录下的 DOM 文本格式不同；DOM 预算（`dom/budget.py`）在文本上加的截断说明与 `dom_viewport_only` 的裁剪不在说明里，BrowserUse 提示词里"只列出可视区域里的元素"仍是 Agent 自己的话。

#### Fragment 注册表

```python
ObservationRegistrar = create_registrar(
    "observation", ObservationFragment,
    discriminator_field="fragment_id", key_separator=".")

class ObservationFragment(BaseModel, metaclass=ObservationRegistrar.Meta):
    """所有 observation fragment 的基类"""
    __abstract__ = True
    __source__: ClassVar[Literal["env", "loop"]] = "env"  # 默认由 Env(Capability) 提供
    fragment_id: str = ""    # 真实字段，默认取 __registry_key__；见 §5 provenance

# ── 各 fragment 独立定义、独立注册（key 全限定） ──────────────

class DOMObs(ObservationFragment):
    __registry_key__ = "browser.dom"
    element_tree: DOMState
    selector_map: DOMSelectorMap

class ScreenshotObs(ObservationFragment):
    __registry_key__ = "browser.screenshot"
    image_path: str          # 二进制数据用文件路径引用
    viewport_width: int
    viewport_height: int

class TerminalObs(ObservationFragment):
    __registry_key__ = "docker.terminal"
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    cwd: str

class ToolResultObs(ObservationFragment):
    __registry_key__ = "tool_sandbox.tool_result"
    tool_name: str
    result: Any
    success: bool

class ActionResultObs(ObservationFragment):
    __registry_key__ = "loop.action_result"
    __source__ = "loop"           # Loop 自己填充，不依赖 Env
    last_action: str
    success: bool
    error: str | None = None
```

> **`loop.` 前缀不是 `__source__` 的重复编码。** 两者划出同一个分区（`loop.*` ⟺ `__source__="loop"`），但用途不重叠，都留着：`__source__` 决定**行为**（动态 model 里该字段 Optional、由 Loop 而非 `env.observe()` 填），前缀决定**身份**——它是注册表里的唯一 key、协商 universe 的成员、以及落盘文件名里的 provenance 段。去掉前缀会让 fragment key 不再统一是 `{source}.{id}`，`expand_relative_keys` 的投影多一条特例，而 `000_task.json` 也就不再说得出是谁写的。`__source__` 是**类**属性，读 trace 的人手上只有 key。

#### `__wiring__` 三方声明

conscribe 0.6 的 `__wiring__` Mode 2 扩展为三元素 tuple 区分 required/optional：

```python
# 扩展 Mode 2（三元素）：required + optional
"observation": ("observation", ["terminal"], ["filesystem"]),
#                              ^^^^^^^^^^    ^^^^^^^^^^^^^
#                              required      optional
```

**语义由注册表身份决定**：
- Capability 的 wiring → **provides**
- Agent / Loop 的 wiring → **requires**
- Capability 的 `__wiring__["archiver"]` → **provides** 归档器（全限定键，`browser.network`）

归档轴没有类级的 requires：它的需求来自 trace hook 实例的配置（`archive` 列表），不来自任何类的 `__wiring__`（§4.5 "唯一的不对称"）。

**provides 写全限定键，requires 写短名**——provides 是"我这个 capability 注册了哪些 fragment"，那是注册表事实；requires 是领域知识，与 provider 无关（见 §4.3 capability-relative wiring）。

```python
# ── Capability：声明"我能提供什么"（全限定） ──────────────
class BrowserCapability(BaseCapability):
    __wiring__ = {
        "observation": ("observation",
                        ["browser.dom", "browser.screenshot", "browser.console_log"]),
    }

class DockerCapability(BaseCapability):
    __wiring__ = {
        "observation": ("observation", ["docker.terminal", "docker.filesystem"]),
    }

# ── Agent：声明"我需要什么"（capability-relative） ──────────────
class UITarsAgent(AgentBase):
    __wiring__ = {
        "observation": ("observation", ["screenshot"], []),
        # 纯视觉 agent，不需要 DOM → DOMWatchdog 不激活。
        # 也没写 browser：同一份 wiring 对 desktop_vm / android 一样成立。
    }

class SWEAgent(AgentBase):
    __wiring__ = {
        "observation": ("observation", ["terminal"], ["filesystem"]),
    }

# ── Loop：声明"编排逻辑需要什么" ──────────────
class ReactLoop(AgentLoopBase):
    __wiring__ = {
        "observation": ("observation", ["action_result"], []),
    }
```

#### 协商规则（Avro-style resolution）

配置校验先保证 wiring 引用的 fragment 键在允许范围内；`AgentRuntime.prepare()` 再读取选中实例的三方声明，执行协商：

```
env-sourced ∧ Agent/Loop required ∧ Env 提供  → 激活 resolver，填充到 Observation
env-sourced ∧ Agent/Loop optional ∧ Env 提供  → 激活 resolver，填充到 Observation
env-sourced ∧ Env 提供 ∧ 无人请求             → 不激活 resolver（性能优化，如 UITars 不算 DOM）
env-sourced ∧ Agent/Loop required ∧ Env 不提供 → IncompatibleFragmentError（运行前指出缺失项和请求方）
loop-sourced ∧ 有人请求 ∧ 本 Loop 建得出       → 激活，Loop 在 env.observe() 之后自行填充
loop-sourced ∧ optional ∧ 本 Loop 建不出       → 不激活
loop-sourced ∧ required ∧ 本 Loop 建不出       → IncompatibleFragmentError（点名这个 fragment 与这个 Loop）
```

"本 Loop 建得出"读的是选中 Loop 的 `__loop_fragments__`（§4.1 "Loop 的两条类级声明"；`AgentLoopBase` 默认是共享 builder 建的四个 `loop.*`）：协商的 universe 是环境提供的 fragment 加上这个 Loop 声明的 loop 来源 fragment，而不是全部已注册的 `loop.*`。一个 required 的声明对不上 universe、却对得上一个已注册而本 Loop 不建的 loop 来源 fragment 时，报错的提供方是 Loop（`IncompatibleFragmentError.provider == "loop '<loop_id>'"`，`provided` 是这个 Loop 建的那些），不是环境。不带环境的 run（§4.1）里环境一侧的 universe 是空的。

错误信息示例：
```
IncompatibleError: Cannot pair SWEAgent with BrowserEnvironment.
  SWEAgent requires env-sourced observation fragments: {'terminal'}
  BrowserEnvironment provides: {'dom', 'screenshot', 'console_log'}
  Missing: {'terminal'}
```

#### 实例在类声明空间内收窄（instance demand）

**规则一句话**：一个**配置好的实例**可以在类 `__wiring__` 声明的空间内，把 optional 提升为 required、或排除某个 optional；不能丢掉类里的 required，也不能扩大空间。协商规则本身一个字不改。

这是 `design_discussion.md` §2.5 Q3 已决定的"配置覆盖"路径的落地形态。接入点只有两个：

- `AgentBase.negotiation_demand(registry) -> (required, optional)`。默认返回类 wiring；需要收窄的 agent 覆盖它。
- `negotiate_observation` / `negotiate_action` 在 agent 那一侧改用 `demand_of(agent, registry)` 取这对集合。Loop 那一侧仍然读 `type(loop)`——loop 要的东西不归 agent 收窄。

边界由框架强制，不靠约定：`demand_of()` 把任何覆盖的返回值再过一遍 `bounded_demand()`。这是整条协商路径上唯一新增的自由度，它不该是靠"每个实现自觉调用"来守的那个。

边界之所以是硬的，有两个独立理由：

1. conscribe 把类 `__wiring__` 编译成生成 config 里的 `Literal`。声明空间之外的 key 是任何 YAML 都写不出来的 key，用它跑出来的 run 无法从自己的 config 复现。
2. 类级 required 是 agent 说"没有它我干不了活"。实例把它丢掉，就是把一次响亮的协商失败换成一次静默降级——正是本设计反复删除的失败模式。

违反任一条抛 `DemandOutsideWiringError`。这是 agent 类的编程错误，不是用户的配置错误：它在 `prepare()` 阶段、环境启动之前就炸。

**失败要点名是谁在要。** 协商把各方的 required 并在一起再求解，缺了一个键时结果里已经看不出是谁要的，所以 `demand_of` 之外还有一个 `demand_label(owner)`，它只做一件事——给诊断取名字。名字就是 registry key（`browser_use.dom`、`react`）：YAML 里选它用的那个字符串，家族成员的键本身就说出了它观测什么、声明哪些动作。不用 `__repr__`：dataclass 自动生成的 `__repr__` 开头是**类名**，而且会把 LLM 客户端整个 dump 进错误信息，`object` 的 `<... at 0x…>` 在进程之间都不稳定。BrowserUse 以前为模式写过一个以 registry key 开头的 `__repr__`（`browser_use(observation_mode='dom')`），`demand_label` 整段采用它；模式变成家族成员之后，这条规则随之删除。

标签只走到错误信息里：`_observation_rules` / `_action_rules` 多收两个 `*_source` 形参，它们不进 `resolve_demand`，改不了协商结果。`IncompatibleFragmentError` / `IncompatibleActionError` 于是能写出

```
Missing: ['dom']. Requested by: dom ← browser_use.dom.
Environment provides: ['browser.browser_meta', 'browser.screenshot'].
```

—— 是 agent 要了 `dom` 还是 Loop 要了它，修法不同（换 agent 成员还是换 `profile`），没有这一行，用户只能猜改哪一个。

**使用者：`show_capture_state`**。三个 Agent（BrowserUse 家族根、Skyvern、UI-TARS）在它为 `false` 时排除 optional 的 `capture_state`（`agents/rendering/capture_state.py::capture_state_demand`），这个 fragment 不采集也不渲染（上文"跨平台采集状态"）。

收窄不用来在观测模式之间切换。BrowserUse 以前这样做：一个类声明三种模式的并集，私有的模式表在并集上写 promote / exclude 的差量，`observation_mode` 选一行。任何真实配置都不是那个并集，差量写错了得出的集合仍然合法、只是悄悄不对，为此又要另一层校验。根源是 `__wiring__` 不能随配置值变化。2026-10-23 起观测与动作的组合是类本身：每个家族成员写出完整的 `__wiring__`（§4.1 "BrowserUse 家族"），conscribe 把它编进生成的 config，trace 的 `agent_id` 直接说是哪一种，收窄只剩下"少要一个不渲染的 optional fragment"。

#### Resolver 模式（IoC 激活，消除 if-else）

Capability 内部通过 Resolver 按需提供 fragment，不需要在 observe() 里写 if-else。

**Browser Capability：Watchdog 即 Resolver**

```python
class DOMWatchdog(BrowserWatchdog):
    level = "tab"
    provides = frozenset({"browser.dom"})

class ScreenshotWatchdog(BrowserWatchdog):
    level = "tab"
    provides = frozenset({"browser.screenshot"})

# BrowserCapability 从注册表发现 watchdog，按 level 建立 scope 连接；
# negotiated fragments 决定一次 BrowserStateRequestEvent 激活哪些 resolver。
# 这一次采集之前先决定焦点在哪个 tab（§4.14 "页面新开的标签页"：跟过去 agent 的动作
# 打开的 tab、离开自己关掉的 tab，移了就记在每个 browser fragment 的 focus_moved 上），
# 再等页面落定（§4.14 "观测之前等页面落定"）：一次采集一个等待，
# 所有 fragment 共用，记在每个 browser fragment 的 settle 上。
```

**新增 observation fragment = 新增一个 Resolver Watchdog。BrowserCapability 不改。**
不提供 fragment 的执行型 watchdog 可以共享实例。`JavaScriptExecutionWatchdog`
声明 `level="shared"`、`provides=∅`，把每个 tab 的观测 scope 连接到该 tab 的 JS
执行 scope；它不发布第二份 `browser.dom`。agent-cdp 的 scope↔handler 连接是 N:M：
一个 scope 可连多个 watchdog，一个 watchdog 可连多个 scope。按任务选择连接和队列策略，
不把当前 DOMWatchdog 按 tab 持有 selector map 的实现选择当成中间层限制。

**非浏览器 Capability：直接实现 Resolver**

```python
class DockerCapability(BaseCapability):
    _resolvers = {
        "terminal": "_resolve_terminal",
        "filesystem": "_resolve_filesystem",
    }

    async def resolve_fragment(self, frag_key: str, **kwargs) -> ObservationFragment:
        resolver = getattr(self, self._resolvers[frag_key])
        return await resolver()

    async def _resolve_terminal(self) -> TerminalObs:
        result = await self._docker.exec(self._container, self._last_command)
        return TerminalObs(stdout=result.stdout, stderr=result.stderr,
                           exit_code=result.exit_code, cwd=result.cwd)
```

#### 动态生成 Observation Model

协商完成后，从 active fragments 组合出嵌套结构的 Pydantic model（每个 fragment 作为子 model，`obs.terminal.stdout`）：

```python
def build_observation_model(active_fragments: set[str]) -> type[BaseModel]:
    fields: dict[str, Any] = {"timestamp": (float, Field(default=0.0))}
    for frag_key in sorted(active_fragments):
        fragment_cls = ObservationRegistrar.registry.get(frag_key)
        fields[frag_key] = (fragment_cls, ...)
    return create_model("NegotiatedObservation", **fields)

# 例：active_fragments = {"terminal", "action_result"}
# 生成：
# class NegotiatedObservation(BaseModel):
#     timestamp: float = 0.0
#     terminal: TerminalObs          # obs.terminal.stdout
#     action_result: ActionResultObs  # obs.action_result.success
```

#### 兼容性演进规则

| 变更 | 兼容性 |
|------|--------|
| Env 新增一个 fragment 到 provides | BACKWARD ✓ |
| Agent 去掉一个 optional fragment | FORWARD ✓ |
| Fragment 内新增有 default 的字段 | BACKWARD ✓ |
| Fragment 内删除有 default 的字段 | FORWARD ✓ |
| Fragment 内改字段类型 | BREAKING ✗ — 需新建 key（如 `dom_v2`） |

### 4.5 Action 体系 + 对称协商 + 前缀路由

**设计决策**：Action 与 Observation 完全对称。每个 action 是独立的 conscribe 注册单元（`ActionRegistrar` + `ActionBase`），agent 通过 `__wiring__` 声明 `(required, optional)` 的动作名，capability 通过 `__wiring__` 声明 `provides` 的动作名，AgentRuntime 协商出 `active set`。Action 的副作用/状态配置属于 capability 端——agent 只声明"我要 scroll 能力"，不关心 scroll 滚多少像素。多 capability 场景通过前缀路由消歧（`browser.click`）。`guarded_act()` 保证 prompt 与实际能力永远一致。

#### 对称对照表

| 维度 | Observation | Action | Archive |
|---|---|---|---|
| 注册器 | `ObservationRegistrar` (`"observation"`) | `ActionRegistrar` (`"action"`) | `ArchiverRegistrar` (`"archiver"`, `discriminator_field="archiver_id"`, `key_separator="."`) |
| 基类 | `FragmentBase` (pydantic bridge) | `ActionBase` (dataclass + conscribe metaclass) | `ArchiverBase`（普通类 + conscribe metaclass；框架层只有身份与声明：`archiver_id`、`source`、`privacy_markers`；旋钮是子类的 `Annotated` 构造参数；采集行为由所属 capability 自己的 archiver 基类定义，框架从不调用） |
| 注册单元 | 每个 fragment 一个类 (`DOMObs` / `ScreenshotObs` / ...) | 每个 action 一个类 (`ClickAction` / `ScrollAction` / ...) | 每个归档器一个类（M29：`browser.network` / `browser.storage` / `browser.dom_raw`；只作标签的截图 `browser.frames`；回放日志 `browser.replay_log`，§4.14） |
| 注册 key 命名空间 | `source.id` dotted key (`browser.dom` / `browser.screenshot` / `docker.terminal` / `loop.action_result`) | `source.id` dotted key (`browser.click` / `browser.scroll` / `sandbox.bash`) | `source.id` dotted key（`browser.network`） |
| Agent wiring | `"observation": ("observation", [required], [optional])` — 默认写 capability-relative 短名（`screenshot`），写全限定键即钉死 provider | `"action": ("action", [required], [optional])` — 同左（`click_coordinate` / `browser.click_coordinate`） | 无。requires 不在任何类的 `__wiring__` 上，而是 `ArchiveConsumer.archive`——trace hook 实例的归档器列表（YAML `hook[].archive`，M29 起），**永远全限定**，没有 relative key |
| Capability wiring | `"observation": ("observation", [provides])` | `"action": ("action", [provides])` | `"archiver": ("archiver", [provides])` |
| Environment 聚合 | `provided_fragments` (全限定键集合) | `provided_actions` (全限定键集合) | `provided_archivers`（全限定键集合） |
| 协商函数 | `negotiate_observation(agent, loop, env)` → `ObservationNegotiationResult` | `negotiate_action(agent, env)` → `ActionNegotiationResult` (loop 不参与) | `negotiate_archive(consumers, env)` → `ArchiveNegotiationResult`（注册为 `archive`） |
| 协商失败 | `IncompatibleFragmentError` | `IncompatibleActionError` | `IncompatibleArchiveError` |
| 绑定表 | `fragment_bindings`（短名 → 全限定键） | `action_bindings`（短名 → 全限定键） | `archive_bindings`（archiver key → `capability_id`） |
| wiring 提取 | `extract_wiring(cls, "observation")` | `extract_wiring(cls, "action")` | 只有 provides 一侧：`_wiring_keys_for(cls, "archiver")` |
| 统一入口 | `negotiate(target_class, wiring_name, rules, **kwargs)` — 见 `spectragent/core/negotiation/engine.py` | 同左 | 同左（`negotiate(ArchiveNegotiationResult, "archiver", _archive_rules, ...)`） |
| 副作用/状态配置位置 | Capability 端 (e.g. `DOMWatchdog.cleaner_type` 是 profile 的一部分) | **Capability 端** (e.g. `ScrollAction.default_amount` 是 capability 持有的 action 实例的 conscribe 字段) | **记录器端**：旋钮在 trace hook 持有的归档器实例上（如 `max_inline_body_bytes`），绑定时把实例交给 capability |
| 运行时使用 | `env.observe(negotiated, session_id)` 只解析 active fragment | `guarded_act` 拒绝 non-active action；`active_descriptors` 只暴露 active set | `prepare()` 末尾 `env.bind_archivers(...)`；归档消费者在自己的提交点拉取 `env.collect_archive()`（§4.6） |
| Loop 参与协商 | 是（`ActionResultObs` 等 loop-sourced fragment） | **否**（action 都是 env-sourced，loop 只是驱动器） | **否** |

#### 归档：第三条 provides 轴

环境证据（§5.1）沿用 observation / action 的同一套形状：每个归档器独立注册，键是全限定的 `<capability>.<id>`；capability 在 `__wiring__["archiver"]` 里声明 provides，`provided_archivers` 由它派生（与 `declared_fragments` 同一种做法，`CapabilityBase` 的默认实现，§4.9）；environment 聚合；`negotiate_archive` 在 `prepare()` 里校验，结果是绑定表，下游只读这张表。

**唯一的不对称**：requires 来自 trace hook **实例**的配置，而且永远全限定。记录什么是每次 run 的选择（用户裁决 D3，2026-10-09），它既不是某个 agent 类的领域知识（§4.1 "BrowserUse 家族"：成员决定模型看到什么，不决定 run 记录什么），也不是记录器类的固有属性。所以没有类级 `__wiring__` 需求，也就没有 relative key 要绑定：`ArchiveConsumer.archive` 里每个归档器的 `archiver_id` 本身就点名了 provider。

**协商规则**（`core/negotiation/archive.py`）。`negotiate_archive(consumers, env)` 接收的是 runtime 筛出来的、`archive` 非空的 `ArchiveConsumer` 对象，不是一个扁平的归档器列表，因为请求方是谁、同一个键是不是跨消费者重复，只有从消费者那里才看得出来。`consumers` 为空是编程错误（`ValueError`），`archive` 里的元素不满足 `Archiver` Protocol 同样是编程错误（`TypeError`，点名消费者与元素类型）。消费者按身份（`is`）比较，不按相等：同一个对象列两次算一个消费者，两个相等的不同对象仍是两个。环境一侧的事实是 `env.env_type`，以及 `env.capabilities` 里每个 capability 的 `(capability_id, provided_archivers)`，不实现 `ArchivingCapability` 的 capability 视为一个也不提供；报错里"环境提供了什么"是这些集合的并集（按构造等于 `env.provided_archivers`）。请求方标签用 `demand_label(consumer)`（`trace_writer`）。每个请求的键按第一段找 capability，该 capability 必须提供这个键。下面的问题**全部收集完一次报出**，每条一行，按字典序排序，合成一个 `IncompatibleArchiveError`：

| 问题 | 消息（逐字） |
|---|---|
| 前缀对应不到 capability | `{req} requests archiver {key!r}, but environment {env!r} has no capability {prefix!r} (its capabilities: {cap_ids}).` |
| capability 不提供这个键 | `{req} requests archiver {key!r}, but environment {env!r} provides archivers: {env_provided} (capability {cap!r} declares {cap_provided}).` —— `{cap_provided}` 为空时写 `none`，否则是有序列表 |
| 同一个键在一个列表里出现两次 | `{req} lists archiver {key!r} more than once; an archive list names each archiver at most once.` |
| 同一个键出现在两个及以上消费者里 | `archiver {key!r} is requested by more than one hook ({req_a}, {req_b}); a run binds each archiver once.` —— 标签按消费者顺序、逗号分隔 |
| 不止一个消费者声明了非空 `archive` | `more than one hook requests archivers ({req_1}: {keys_1}; {req_2}: {keys_2}); a run binds the archive of at most one hook.` —— 按消费者顺序、`; ` 分隔。v1 只绑定一个消费者：`collect_archive` 取走即遗忘，证据只能交给一个调用方 |
| 本 run 没有 environment | `{req} requests archivers {keys}, but this run has no environment.` |
| 消费者不是 blocking 的（`blocking=False`） | `{req} requests archivers {keys} but is not blocking (blocking=False); a non-blocking hook's handlers finish after the step is committed, so the evidence it pulls would reach no trace: an archive consumer must be blocking.` —— 读 `getattr(consumer, "blocking", True)`：没有声明的当作 blocking，与 Weaver 的读法一致（§4.6） |

`{env!r}` 是 `env_type`，所有列表都是有序的 `list` repr。`IncompatibleArchiveError` 带三个与 `IncompatibleFragmentError` 同构的属性：`missing`（前缀对应不到或不被提供的键；没有 environment 时是全部请求的键）、`provided`（环境提供的全部归档器；没有 environment 时为空）、`requested_by`（键 → 请求它的消费者标签，有序）；另有 `problems`：消息里那些行，排序并去重（两个标签相同的消费者可能产生一模一样的一句话，只报一次）。重复键、多消费者与不是 blocking 的消费者这三类问题只出现在消息里，不进 `missing`。最常见的一条长这样：

```
IncompatibleArchiveError: trace_writer requests archiver 'browser.network', but environment 'browser' provides archivers: [] (capability 'browser' declares none).
```

成功时返回 `ArchiveNegotiationResult`（`@dataclass(frozen=True)`，注册到 `NegotiationResultRegistrar`，`result_type == "archive"`）：`active_archivers: frozenset[str]`（全限定键）、`archive_bindings: Mapping[str, str]`（archiver key → 提供它的 capability 的 `capability_id`，即 registry key，不是 capability 实例 id）。

**capability 配置上的 `archiver:` 字段**：capability 的 `__wiring__["archiver"]` 条目没有 slot，所以 conscribe 在 capability 的配置上生成一个可选的 `archiver: Optional[Literal[...]] = None`，和 `observation:` 一样；YAML 填了它，Composer 抛 `UnresolvedWiringError`（§4.2.1）。归档器在 trace hook 上选，不在 capability 上选。`tests/test_core/test_composer/test_archive_provides.py` 钉住这一点。

#### ActionDescriptor

`ActionDescriptor` 仍然保留，作为 prompt 构建的中间表示：

```python
class ActionDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str          # 全限定 key，来自 ``ActionBase.action_id``
    description: str
    params: dict[str, str] = Field(default_factory=dict)           # prompt 展示的 prose，由 schema 渲染
    params_schema: dict[str, Any] | None = None                    # provider 约束的 JSON Schema
```

`name` 由 `descriptor()` 写成 `self.action_id` 而不是字面量，所以"广告给模型的名字"和"路由用的 key"不可能漂移。

**`params_schema` 现在是填满的，而且 `params` prose 由它派生。** 每个 action 声明一个扁平的 `Params` 模型（`ClassVar[type[BaseModel]]`，`extra="forbid"`，只允许 `int`/`float`/`str`/`bool`/字符串枚举/`| None`），三样东西全部由它渲染，源头只有一处：

- `params_schema` —— agent 编译成封闭输出 schema 时读的那份；
- `params` prose —— prompt 展示的那份（`ActionBase.describe()` 一次渲染两者，所以"教给模型的参数"和"schema 约束的参数"不可能描述成两组）；
- **参数级 fragment 依赖**。

在此之前 `params_schema` 在整棵树的每个 action 上都是 `None`——契约的机器可读那一半根本不存在，于是想要封闭 schema 的 agent 无从编译；而 prose 是手写的、`execute()` 读的是裸 dict，两份描述之间没有任何东西把它们绑在一起。

**参数级 fragment 依赖（capability 侧知识）**：`index` 寻址 selector map，而 selector map 只有协商激活了 `browser.dom` 才存在——这是 browser capability 自己知道的事实，不是发出 click 的 agent 知道的。所以它声明在参数上，用**全限定**键（与 agent `__wiring__` 的 relative 键刻意相反）：

```python
class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: Annotated[int, AddressesElement("browser.dom")]   # AddressesElement 是 RequiresFragment 的子类
```

一处声明，两级生效：

- **required** 参数依赖 F ⇒ 整个 action 依赖 F（`browser.click` / `browser.type` / `browser.select_dropdown` 依赖 `browser.dom`）；
- **optional** 参数依赖 F ⇒ F 未激活时**把这个参数从 descriptor 里删掉**（视觉 run 里 `browser.scroll` 还在，它的 `index` 不在）。

**参数的含义：点与元素地址（2026-10-23）。** 哪些参数合起来是一个点、哪个参数指名一个元素，也只在参数上声明一次（`core/actions/params.py`），读的人一律读这一处，不再各自按参数名或动词另写一份（以前 `_x` / `_y` 的命名约定、BrowserUse 的坐标动作表、UI-TARS 动词的点参数、恢复校验的 index 动作名单、查看器标记里写死的参数名各写一遍）：

- `PointAxis(point, axis)`：一个点的一条轴，写在两个参数上（`coordinate_x: Annotated[int, PointAxis("coordinate", "x")]`、`coordinate_y` 同理；`drag` 有 `start` 与 `end` 两个点）。`point_params(Params)` 按每个点第一条轴的声明顺序给出 `(x, y)` 参数对；一个点缺一条轴或同一条轴写两次是 `TypeError`。没有这个标记的参数不是点，名字像也不是。
- `AddressesElement(fragment)`：参数的值指名这个 fragment 里的一个元素（`browser.click` 的 `index` 是 `browser.dom` 的 selector map 的键）。它是 `RequiresFragment` 的子类：元素离开它的 fragment 没有意义，所以上面的两级依赖照旧从这一个标记得出。`element_params(Params)` 给出这些参数。
- **经 descriptor 交给 Agent。** `ActionBase.describe` 把两者随 prose 与 schema 一起渲染进 `ActionDescriptor.point_params` / `element_params`，并与参数一起收窄（视觉 run 的 `scroll` 没有 `index`，也就没有元素地址）。Agent 从 `prepare` 交给它的 `ActionSpace` 里读本 run 的 descriptor；读录下动作的一方（恢复的 capability 读它自己的动作实例，导出、查看器标记、lookahead 读注册的 `Params`，`core/actions/_registrar.py::registered_params`）读同一个标记。
- **动作的含义：只等待（2026-10-25）。** 一个动作只让时间过去、不碰环境（`browser.wait`），写在动作类上一次：`ActionBase.only_waits: ClassVar[bool]`（默认 `False`，`WaitAction` 为 `True`）。读录下动作的一方经 `core/actions/_registrar.py::is_wait_action(name)` 按全限定名读它，从不按名字或后缀认；训练导出的 `wait_actions: drop_when_capped` 靠它认出要去掉的等待（§4.15 "等待动作"）。
- **Agent 的命名只问绑定。** `core/action_guard.py::executed_name(space, name)` 是 `resolve_action_name` 套在 `ActionSpace` 上：一个名字在本 run 被守卫执行成哪个全限定动作，拒绝的是 `None`；`executed_descriptor(space, name)` 给出那个动作的 descriptor。UI-TARS 编解码绑定动词、Skyvern 的 `done`、两个 Agent 的点参数查找都用它，不再按名字的最后一段去 descriptors 里对（两个 capability 都提供的短名不绑定任何一个，按后缀对会挑中一个守卫会拒绝的）。

删除发生在 descriptor 的 `**context` 通道上：`AgentRuntime.prepare` Step 4 调用 `env.get_action_space(active_actions=..., fragments=negotiated_obs.active_fragments)`，环境把它转发给每个 capability、再转发给每个 `action.descriptor(**context)`——与 §4.9 里 `task_tools` 走的是同一条通道。协商规则不变；不读 `fragments` 的 capability 行为与从前完全一致。

守门是静态扫描：`tests/test_core/test_actions/test_action_params_schema.py` 对每个 agent × 每个模式检查"激活动作推导出的依赖 ⊆ 同一模式 observation 的 required"。运行时一致性检查推迟到第二个会用到 DOM 的 capability 出现时再加。

**YAML 旋钮进 description，不进 `default`**：`clear_by_default` 这类当前值被插进参数的 description（prompt 和 schema 两边都看得到），但 descriptor 的 schema 里**不写 `default`**——默认值归 capability，在 `execute()` 里应用，写一个 schema default 等于声明一个"省略该参数时会得到的值"，而那个值不是它。

作用域要说清楚：这是 **descriptor 那一层**的保证。agent 把 descriptor 编译成 per-run 输出模型时，每个 optional 参数会重新带上 `default: null`——strict dialect 把所有属性变成 required，`null` 于是成了模型"弃权"的唯一写法。这个 null 到不了 `execute()`：agent 在构造 Action 前把它抹掉，所以"省略"和"写 null"到达 capability 时完全一样——这才是 `clear: null` 覆盖 `clear_by_default` 被堵住的地方。抹掉的规则在框架一处：`core/actions/action_union.py::action_of(member)` 只去掉 optional 参数的 null，required 参数的 null 保留（`done` 的 `extracted_content` required 且可空，写 `null` 是"没有答案"的答案）。BrowserUse 与 Skyvern 都经它构造 Action。BrowserUse 原来用 `model_dump(exclude_none=True)`，把这个 null 也抹掉，参数校验再以 `extracted_content: Field required` 退回整条回复，没有答案可交的任务结束不了；2026-10-23 改用 `action_of`（`test_browser_use_family.py` 钉住 `done(extracted_content=null)` 被接受）。

**Skyvern 的 `reasoning` 不进动作参数（2026-10-23）。** Skyvern 每个动作条目的 `reasoning` 是模型对这个动作的想法，留在回复里（`parse_react_thought` 读它；闭合回复里它与 `action` 并列在条目上，§4.1"Skyvern 家族"），不再被 Agent 塞进动作参数：动作只带它的 `Params` 声明的参数，否则执行之前的参数校验（§4.1“参数不合 `Params` 的动作是解析错误”）就会把它退回去。导出层原来在消费端删掉它的补丁（`_project`、`dropped_params`）随之删除（§4.15）。这次改动之前录下的 Skyvern run 不能以 `history_source: source` 分叉（源回复重新解析出的动作不再带 `reasoning`，与录下的动作逐字比较不同），也不能导出（录下的动作参数带着 `reasoning`，不合 `Params`）；不设兼容路径，重录。

**封闭 schema 与 strict dialect**：agent 编译出的 per-run 模型是 strict-compatible 的（`anyOf` 分支、每支一个 required 属性、无 `oneOf`/`$ref`/开放 map），这也意味着它**第一次**真的会以 `strict: true` 发出去。于是 `SchemaOptimizer.create_optimized_json_schema(..., strict=True)` 必须把 strict 拒收的数组关键字（`minItems`/`maxItems`/`uniqueItems` 等）一并去掉：`action` 字段上写 `Field(min_length=1)` 是完全正常的写法（空 action 列表因此在本地就被拦下），但它编译出的 `minItems: 1` 会让请求在**schema 上**返回 400——模型连 prompt 都没读到。strict 是一个 dialect，不是调用方可以只用一半的开关，所以去除写在 optimizer 里一次，而不是每个 provider 各配一个旋钮。`default` 不在拒收名单里，也因为 strict 下所有属性都 required 而永远不会被应用，所以保留。

**它不负责转成 provider 的 function-calling schema。** 原先的 `to_tool_schema()` 已删除：它把 `params` 里的人类说明（`"int -- element index from selector_map"`）直接塞进 JSON Schema 的 `"type"`，14 个已发布 action 全部产出非法 schema，而且没有任何 consumer（只有 `react` 一个 loop 注册在案）。真正面向 provider 的类型是 `core/llm/tools.py` 的 `ToolDefinition`，两者的合并留给引入 FuncCall loop 的那次改动——那时才会有真实 consumer 来定义需求。合并前必须先解决的一个冲突见 `docs/action_descriptor_vs_tool_definition.md`。

#### ActionRegistrar + ActionBase

每个 action 是一个独立的注册类，副作用/状态作为 `Annotated` 字段写在类里：

```python
ActionRegistrar = create_registrar(
    "action", BaseAction, discriminator_field="action_id",
    key_separator=".",   # 让 conscribe 能展开 capability-relative wiring key
)

@dataclass
class ActionBase(metaclass=ActionRegistrar.Meta):
    """所有 action 的基类。"""
    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    Params: ClassVar[type[BaseModel]]   # 每个具体 action 必须声明
    Effect: ClassVar[type[ActionEffect] | None] = None   # 可选：执行后测得的事实（见“动作效果”）

    @property
    def action_id(self) -> str:
        return self.__registry_key__

    def descriptor(self, **context: Any) -> ActionDescriptor: ...

    def requires_fragments(self) -> frozenset[str]:
        """本 action 跑不起来就缺的 fragment（由 required 参数的标记推导）。"""

    def describe(self, description, *, hints=None, fragments=None, **_context):
        """从 Params 一次渲染出 prose + schema。具体 action 一般只调它。"""

    async def execute(
        self,
        action_params: dict[str, Any],
        *,
        capability: Any,   # capability 自身注入，action 可以访问内部状态
        session_id: str,
    ) -> ActionResult: ...


# ── 具体 action 示例：ScrollAction 带副作用配置 ──
@dataclass
class ScrollAction(ActionBase):
    __registry_key__ = "browser.scroll"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        direction: Literal["up", "down", "left", "right"]
        amount: int | None = None
        # 唯一读 selector map 的参数，而且是 optional——所以没有 DOM 的 run
        # 保留这个 action、丢掉这个参数。
        index: Annotated[
            int | None,
            AddressesElement("browser.dom"),
            Field(description="scroll inside specific element"),
        ] = None

    # ⭐ capability 端可配置的副作用参数
    default_amount: Annotated[int, Field(description="Default scroll px")] = 300
    max_amount: Annotated[int, Field(description="Cap per call")] = 2000

    def descriptor(self, **context) -> ActionDescriptor:
        return self.describe(
            f"Scroll the page (default {self.default_amount}px).", **context
        )

    async def execute(self, params, *, capability, session_id):
        scope = capability._scopes[session_id]
        amount = min(params.get("amount", self.default_amount), self.max_amount)
        event = ScrollEvent(
            direction=params.get("direction", "down"),
            amount=amount,
        )
        scope.emit(event)
        await event
        return ActionResult(success=True)
```

**注意**：`ActionBase` 使用 `@dataclass` + conscribe metaclass 的组合（参考 `AgentBase`），不需要 pydantic bridge，因为 action 对象本身不是数据传输对象，而是携带配置的执行器。

#### 动作效果：`Effect` 与 `Params` 对称（2026-10-20 裁决，已实现）

`Params` 是动作的输入合同，`Effect` 是它的输出事实合同。动作类可以声明嵌套的 `Effect` 模型，执行时自己测量，放进 `ActionResult.effect`。`ActionResult` 只多这一个通用槽位，不出现任何 capability 的字段：

```python
PointWriter = Callable[[int, int], str]     # schemas/action.py：viewport 点 → 它在提示里的写法

def write_viewport_point(x: int, y: int) -> str:
    return f"({x}, {y})"                     # 默认写法：viewport 单位，今天的文本

class ActionEffect(BaseModel):              # schemas/action.py，通用基类
    model_config = ConfigDict(frozen=True, extra="forbid")
    effect_id: str                           # 产出它的动作的全限定 key，如 "browser.scroll"
    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str: ...
                                             # 一行确定性的事实文本，供 prompt 使用

class ActionResult(BaseModel):
    success: bool                            # 执行成败；结束动作也一样（§4.1"agent 的任务结论"）
    error: str | None = None
    ...
    reported_success: bool | None = None     # 只在 is_done 的结果上：agent 对任务的结论，None 是没有说
    effect: ActionEffect | None = None       # None：该动作没有 Effect，或没执行到可测量处
```

- **读回不新增注册表。** `effect_id` 就是产出它的动作 key。trace 读取侧（`RunTrace` 读 `steps/NNN.json`）按 `effect_id` 在现有 `ActionRegistrar` 查到动作类，用它的 `Effect` 还原类型；查不到或校验失败是 `TraceSchemaError`，不降级成 dict。`schemas/` 不 import 动作类。多个动作可以共用一个效果类（`click`、`click_coordinate`、`drag` 共用指针效果）。trace 格式版本不变：缺少 `effect` 与“不产出效果”同义，旧测试数据按 2026-10-14 的裁决重新生成。
- **`describe()` 合同。** 只由本对象的类型化字段确定性派生，一行英文事实，不含任务判断；敏感字段既不回显内容，也不报长度；unknown / unavailable 要明说。所有 Agent 渲染这一行，不把效果字段直接 dump 进 prompt：跨平台 Agent 因此不需要认识任何具体效果类型，浏览器专用 Agent 仍可另读字段。
- **点由渲染方决定写法**（2026-10-22 裁决，§4.1"模型看到的每个点都写在它写点的那个网格上"）。`describe` 接受可选的关键字参数 `write_point`：效果陈述的**每一个点**（指针效果的落点与拖拽的松开点）都经它写出，其余文字不变；默认 `write_viewport_point` 写 viewport 单位的 `(x, y)`，与以前逐字相同。Agent 传的是框架的 `point_writer(那一步记录的坐标系)`（§4.1"坐标系归框架"）：`skyvern.dom` 与没有坐标动作的 BrowserUse 的 run 没有坐标系，得到的就是默认，提示词不变；UI-TARS、有坐标动作的 BrowserUse 与 `skyvern.dom_vision` 得到那一步坐标系上的写法，效果行与它们写的点在同一个网格上。**距离不是点**：滚动的位移与滚动位置不经 `write_point`，照旧是 viewport 的 px，位移写明单位（`moved the page by 600 px (y 0 → 600)`）。合同的其余部分不变：同一个 `write_point` 下仍是确定性的一行英文事实，`write_point` 只改点的写法，不加判断。每个 `Effect` 的 `describe` 都必须接受这个关键字（Agent 统一传它），`tests/test_core/test_actions/test_action_effect_contract.py` 对每个已注册动作的 `Effect` 检查。
- **命中元素与焦点写角色和可读名，所有 Agent 都一样**（2026-10-20 裁决）。UI-TARS 这样的纯视觉、跨平台 Agent 也照样渲染；桌面与 Android 的无障碍接口给出同类事实，这不是浏览器专有信息。没有单独控制这部分细节的开关，要去掉只能用 `show_action_effects` 关掉整行。
- **测量语义。** 效果在这一个动作执行期间、执行前后测得，所以能归因到它；批次之后的页面状态属于 observation。`success` / `error` 仍只表示执行成败：测量失败只把对应字段记为 unknown，不改变成败，也不中止动作。唯一由测得事实决定成败的是输入前的焦点判断（下面 `type` / `type_coordinate`）：它是执行前的条件，只有测得的“没有文本框聚焦”才拒绝，读不出结论照旧执行。测得为零、unknown、unavailable 三者区分。测量有时间上限，不能明显拖长动作。
- **浏览器本轮范围。** 指针类 `click`、`click_coordinate`、`drag`：实际落点、命中元素（角色与可读名）、执行后焦点。按编号寻址的点击和滚动，先把元素滚进视口再按：`DOM.scrollIntoViewIfNeeded` 后若元素中心仍在视口外（部分可见、比视口还高），用脚本把它居中再量；仍在视口外（例如固定在屏幕外的已收起面板）就拒绝并说明原因，不在视口外派发一次 Chrome 会丢掉、却报告成功的按下。文本输入类 `type`、`type_coordinate`、`send_keys`：实际进入的控件、值是否变化（不回显内容）、实际发出的键、执行后焦点。`send_keys` 的效果另说明这个组合键是不是浏览器自己的快捷键（`KeyPressEffect` 的 `shortcut`，用户 2026-10-24 的裁决，§4.13 "浏览器自己的快捷键"）：实测平台上执行了的命令（后退、前进、重新加载、关闭与切换 tab：命令、前后 URL 或 tab、文档是否加载完成；键没有发给页面），或本浏览器不显示的界面（地址栏、新 tab 页……）、另一平台的快捷键（键照常发给页面，效果行加一句）。`type`、`type_coordinate` 发出任何键之前先判断焦点所在的元素收不收文字（用户 2026-10-22 批准）。判断读元素自己的标记（比较 `localName`，XHTML 文档的 `tagName` 是小写）：浏览器自己编辑的是没有只读、禁用的 `textarea` 与 `input`（按钮、复选框、单选框、文件、颜色、滑块、隐藏这些类型除外；日期类照 Chrome 的行为可键入，算在内）和可编辑内容（`contenteditable`、`plaintext-only`、`designMode`，包括 body 本身就是编辑器的 iframe 富文本编辑器）；不是编辑器、但自己声明 ARIA 角色 `textbox` / `searchbox` 的元素是自己处理按键的部件，同样收（带 `aria-readonly` / `aria-disabled` 为 true 的不收）；声明了别的角色的元素不收（例如角色是 `combobox` 的轻 DOM 组件）；没有角色、也不是文本控件的元素，只有它挂着 closed shadow root 时才说不准（焦点可能在里面的输入框上，脚本看不见，DevTools 的 DOM 代理看得见：`DOM.describeNode` 列出 `shadowRootType: closed`，不用先开 DOM 域，实测），否则不收；`<embed>`、`<object>`、`<fencedframe>` 的内容自己管焦点，说不准。焦点经开放 shadow root 与同源 iframe 找到。焦点不在任何元素上，或在不收文字的元素上（按钮、链接、只读输入框），就不清空、不输入，动作失败，错误写明焦点实际在哪，例如 `No text field took focus at the clicked point: nothing has focus, so no text was typed.`（不写坐标：动作那一行已经按 Agent 的网格写明了点，错误里再写一份 CSS 像素会与它矛盾，§4.1）、`Typing into element 12 found no text field with focus: focus is on button "Search", which does not take typed text, so no text was typed.`（按编号时也不说“元素 12 不是文本框”：12 可能就是一个输入框，只是焦点被页面拿走了）、`... focus is on textbox "From", which is read-only, ...`；效果照样报告（`target`、空的 `keys`、`text_entered` 为 false、执行后焦点），`typed_into_sensitive_field` 为 `None`（没有输入文字）。`clear: false` 一样。被拒的 `type_coordinate` 照样等它的 `post_delay_ms`：点击已经发生了。原因：没有可编辑元素聚焦时，清空的全选选中整页文字，文字也没有去处，以前动作却报告成功，模型看不出点偏了（一次 15 个任务的真实录制里 82 次 `type_coordinate` 有 47 次如此，下一张截图整页高亮）。清空的全选只在浏览器自己编辑的字段上带 Chrome 的 `selectAll` 编辑命令；自己处理按键的 `textbox` 部件收到同样的按键、不带这个命令，由部件自己处理——带着它，Chrome 会选中整页。页面在点击或 `DOM.focus` 之后稍晚才把焦点交给输入框（定时器、打开动画）时，每 50 ms 再读一次焦点，最多等 BrowserCapability 的 YAML 字段 `type_focus_wait_s`（默认 0.5 s，至多 5 s）；只有还没有文本框聚焦时才等。这段等待从 `action_effect_timeout_s` 里出、必须小于它（否则构造时拒绝，点名两个字段），等待之外的部分留给之后的探针（值是否变化、执行后焦点）。算作等待的只有每次的停顿和读焦点在哪：某次重读读出焦点在某个元素上之后，读这个元素本身（它的标记、收不收文字、closed shadow root）可以用留给之后的那部分，等待到点不会把它截断。原因：截断了，一个一直聚焦着、不收文字的按钮就成了“读不出收不收文字”而照旧输入（清空的全选选中整页），拒绝与否取决于等待恰好在哪一刻到点（实测：真实 headless Chrome 里把 `type_focus_wait_s` 从 200 ms 到 404 ms 按 0.5 ms 步长扫，在按钮和角色是 `combobox` 的组件上共输入 832 次，有 10 次如此；`TestTypingNeedsATextField` 连跑 200 遍，有 8 次被拒的用例报告成功）。判断与测量共用 `FieldProbe` 的同一次读取，只有测得的“没有文本框”才拦：连焦点在哪都读不出（探针失败或超出上限）的那次读取丢掉，判定与效果都以最后一次读出焦点在哪的读取为准；读得出焦点在某个元素上、却读不出它收不收文字（它自己的读数失败、焦点在别的站点的 frame 里、在 closed shadow root 或嵌入内容里）——照旧输入，与按编号点击只在测得元素中心仍在视口外时才拒绝同理。`send_keys` 不做这个判断：模型自己发的全选组合键是它自己的选择。这次改动之前录下的 run 里，焦点不在文本框上的输入步当时记为成功；分叉或原路径回放重放它们时如实失败，恢复校验记 `action_failed`（`strict` 下判为发散），不另设兼容路径（§4.14“已知限制”）。回归：`test_browser_actions/test_text_input_effects.py` 的 `TestTypingNeedsATextField`（脚本化 CDP）、`test_browser_input/test_action_watchdog_typing.py` 的 `TestTheFocusWaitIsConfig`，与 `test_browser_type_focus_live.py`（真实 headless Chrome：空白处输入失败、页面没收到任何 keydown、`getSelection()` 为空；按钮、只读输入框、`aria-readonly` 的部件、角色是 `combobox` 的组件被拒；文本框、textarea、contenteditable、同源 iframe 里的文本框、body 即编辑器的 iframe、350 ms 后才获得焦点的输入框、closed shadow root 里的输入框照常输入；`textbox` 部件收到清空的按键、页面没有被选中）。`scroll`：被滚动的是页面还是元素、前后位置与位移、是否到边界；没有容器移动时，只有滚轮能到的每个容器原本都已在该方向的边界上，才记测得的零位移，若还有容器有余量而在测量上限内没看到移动，之后的位置与是否到边界记 unknown（滚轮可能在上限之后才到，用户 2026-10-20 的裁决）。导航类 `navigate`、`go_back`、`go_forward`、`switch_tab`：前后 URL 或目标 tab、文档是否加载完成。`select_dropdown`：当前选中项。`wait`：实际等待时长。`done`、`screenshot`、`extract_content` 不产出效果；`done` 交出的 agent 任务结论不是效果，在结果自己的 `reported_success` 里（§4.1"agent 的任务结论"）。
- **历史、分叉与导出。** 效果随 `StepRecord.action_results` 持久化，Agent 经 `loop.history` 读到。`history_source: restored` 使用恢复执行时实际测得的效果，不复制源记录；导出与离线渲染读已提交的效果，不重新测量。
- **配置全部可见。** 测量上限是 BrowserCapability 的 YAML 字段 `action_effect_timeout_s`，超时的字段记为 unknown；输入等焦点的 `type_focus_wait_s` 是它的一部分。是否把效果那一行渲染进 prompt，是每个 Agent 的 YAML 字段 `show_action_effects`（默认 `true`）；关掉时 trace 照样记录效果，所以消融的两组测量开销相同。开关归渲染模型输入的一方：截图里的光标由 capability 画，所以 `screenshot_cursor` 在 capability 上；效果文本由 Agent 渲染，所以这个开关在 Agent 上。默认值按实现时实测的开销定，写进字段说明。

#### Capability 持有 action 实例

每个 Capability 声明 `actions: list[ActionBase]` 字段（conscribe 从 YAML 注入的 discriminated union list）和 `__wiring__["action"]` 声明提供的 action 名：

```python
class BrowserCapability(CapabilityBase):
    __registry_key__ = "browser"
    __wiring__ = {
        "observation": ("observation", ["dom", "screenshot", "browser_meta"]),
        "action": ("action", ["click", "type", "navigate", "scroll",
                              "send_keys", "go_back", "go_forward",
                              "switch_tab", "select_dropdown",
                              "extract_content", "done", "click_coordinate"]),
        "backend": "browser_provider",
    }

    actions: list[ActionBase] = field(default_factory=list)
    _actions_by_name: dict[str, ActionBase] = field(init=False, repr=False)

    def __post_init__(self):
        # YAML 没配 actions 时，从 wiring 派生默认全集
        if not self.actions:
            provided = self.__wiring__["action"][1]
            self.actions = [
                ActionRegistrar.get(name)() for name in provided
            ]
        self._actions_by_name = {a.action_id: a for a in self.actions}

    def get_action_descriptors(self, **context) -> list[ActionDescriptor]:
        return [a.descriptor() for a in self.actions]

    async def execute_action(self, action: Action, **kwargs) -> ActionResult:
        impl = self._actions_by_name.get(action.name)
        if impl is None:
            return ActionResult(
                success=False, error=f"Unknown action: {action.name}",
            )
        sid = kwargs.get("session_id") or self._default_session_id
        self._cycle_cache.pop(sid, None)
        return await impl.execute(
            action.params, capability=self, session_id=sid,
        )
```

#### YAML 驱动的行为调参

`actions` 字段是 discriminated union list，每个条目通过 `action_id` 路由到对应 action 类，其他字段作为构造参数注入：

```yaml
agent: browser_use.dom
capability:
  browser:
    backend: local_chrome
    actions:
      - action_id: scroll
        default_amount: 600     # capability 端配置行为
        smooth: true
      - action_id: click        # 用默认参数
      - action_id: type
      - action_id: navigate
      - action_id: done
    # 没列出的 action（如 send_keys、go_back）不会实例化，
    # 等价于 ablation 收窄
```

不写 `actions`（或写 `null`）得到 wiring 提供的全部动作；`actions: []` 一个动作都不建（`[]` 永远是空，§4.2.1），任何要求动作的 agent 都在协商时失败。

#### 协商流程

AgentRuntime 在启动前运行对称的 action 协商：

```python
def negotiate_actions(
    env_provides: set[str],
    agent_required: set[str],
    agent_optional: set[str],
) -> ActionNegotiationResult:
    missing = agent_required - env_provides
    if missing:
        raise IncompatibleActionError(
            required=agent_required, provided=env_provides,
        )
    active = agent_required | (agent_optional & env_provides)
    return ActionNegotiationResult(active_actions=frozenset(active))


# AgentRuntime 里：
def _negotiate_actions(self, agent, env):
    env_provides = set(env.provided_actions)  # 聚合各 capability
    agent_req, agent_opt = extract_action_wiring(type(agent))
    return negotiate_actions(env_provides, agent_req, agent_opt)
```

**结果对比**：Loop 不参与 action 协商（action 都是 env-sourced），所以 `negotiate_actions` 签名只有 env + agent 两方。

#### `guarded_act()` — Prompt 与能力一致性保证

协商得到的 `active_actions` 用于收窄 `active_descriptors`，然后传给 `build_guarded_act`：

```python
# AgentRuntime.execute() 中
negotiated_actions = self._negotiate_actions(agent, env)

all_descriptors = env.get_action_space()
active_descriptors = [
    d for d in all_descriptors
    if d.name in negotiated_actions.active_actions
]
action_space, act_fn = build_guarded_act(
    wrapped_env.act, active_descriptors, action_filter=None,
)
# action_space.descriptors 的独立快照用于 prompt，act_fn 是 guarded 的执行闭包
```

`build_guarded_act` 的闭包在每次调用时用 `resolve_action_name` 给 `action.name` 定名：在 active set 里就是它自己；短名经绑定表解析到仍在 active set 里的全限定名，就地改写；其余直接返回 `ActionResult(success=False, error=...)`，名字保持 agent 写的样子。

**结果：prompt 说有什么 action，实际就能用什么 action。配置错误在 `AgentRuntime._negotiate_actions` 阶段就会 raise `IncompatibleActionError`，而不是等到 LLM 第一次输出之后才暴露。**

> **Historical note**：`action_filter` 作为顶层 YAML 键曾经是这里的主 ablation 机制。在对称协商方案下，ablation 的首选路径是 agent wiring 的 `required/optional` 二分 + capability 的 `actions` 列表收窄；`action_filter` 仅作为可选的额外 override 保留，不是主设计。

**有效动作空间是实验身份。** `ActionSpace`（`schemas/action_space.py`）记录原始 `action_filter`（`None` 与 `[]` 严格区分）、经过协商和 capability 参数收窄后最终交给 prompt 的 `descriptors`、guard 允许的全限定 `allowed_actions` 和 `action_bindings`。`build_guarded_act` 返回这一个对象与执行闭包；prompt 与 guard 使用同一结果。Runtime 随后按 Agent 声明的坐标系给它定坐标系（`coordinate_space`，点参数的说明写成模型的单位，§4.1"坐标系归框架"；名字、绑定与守卫的执行单位不变），然后在准备阶段公开它，经运行开始合同交给记录组件，`TraceConfig.action_space` 与配置摘要纳入这份事实；来源 YAML 的身份单独保留。source 历史分叉比较这些真实约束，不能只比过滤前协商结果或因为字段缺失跳过校验。

#### Function-Calling 集成

Function-calling 是 **Loop 的职责**：ActionDescriptor → LLM `tools` 参数 → LLM 输出 `tool_call` → `Action(name, params)` → `guarded_act()` → `ActionResult` → `tool_call_result` 回传。

```python
class FunctionCallingLoop(AgentLoopBase):
    async def run(self, agent, env, task, **kwargs):
        active_descriptors = kwargs["active_descriptors"]
        act_fn = kwargs["act_fn"]
        # descriptor → ToolDefinition 的转换尚未实现，且有一个已知冲突：
        # provider 的 tool name 文法不接受点号，而 descriptor.name 是全限定
        # key。见 docs/action_descriptor_vs_tool_definition.md。
        tools = [to_tool_definition(desc) for desc in active_descriptors]

        for step in range(self.max_steps):
            completion = await self.llm.ainvoke(messages, tools=tools)
            for tool_call in completion.tool_calls:
                action = Action(name=tool_call.function.name,
                              params=tool_call.function.arguments)
                result = await act_fn(action)  # guarded_act
                self.memory.add_tool_result(tool_call.id, result)
```

#### Composite 前缀路由示例

```
GAIA 场景：
  BrowserCapability actions: click, type, navigate, done
  ToolSandboxCapability actions: call_tool, get_user_msg

  CompositeEnvironment 聚合后 agent 看到:
    browser.click, browser.type, browser.navigate, browser.done,
    tool_sandbox.call_tool, tool_sandbox.get_user_msg

  LLM 输出 browser.navigate → Env 拆前缀 → BrowserCapability.execute_action(navigate)
```

**单 capability 场景不加前缀**，不触发前缀逻辑。

**CompositeEnvironment** 通过前缀路由到不同 capability（实现见 4.3 节 Environment）。

### 4.6 Lifecycle Hooks 体系（AOP）

**设计决策**：TraceWriter、LiveMonitor 等横切关注点通过 AOP Weaver 透明织入。Loop 不持有任何 hook/emitter 引用，调用 `env.observe()`、`llm.ainvoke()` 或 `recorder.record_step()` 时，代理按已声明的拦截点触发对应 handler。hook 走 conscribe 注册表，通过 YAML 配置启用和参数化。

**两层 hook 接口**：

1. **Lifecycle 回调**（结构边界）：`on_run_start/end/error`，由 AgentRuntime 在运行开始/结束/异常时直接调用；外部取消也是一条终止路径，同样派发（§4.2）。
2. **方法拦截**（AOP 织入）：`__intercepts__` 声明 + `{advice}_{method}` handler，由 Weaver 自动织入目标对象的方法调用。

```python
HookRegistrar = create_registrar("hook", RuntimeHook, discriminator_field="hook_id")

class RuntimeHook(Protocol):
    hook_id: str

    @property
    def blocking(self) -> bool:
        """此 hook 是否必须完成后 agent 才继续"""
        ...

    # ── Lifecycle 回调（AgentRuntime 直接调用）──
    async def on_run_start(self, *, agent, env, loop, llm,          # 对象：给需要和它们说话的 hook
                           task, task_id, benchmark, subset, run_id, config,
                           agent_id, loop_id, env_type, llm_model,  # 值：runtime 解析好的 run 配置
                           resolved_config, observation_negotiation, action_negotiation,
                           archive_negotiation, env_evidence_privacy,
                           task_setup, branch, **kwargs) -> None: ...   # payload 会长，**kwargs 是签名的一部分
    async def on_run_end(self, *, result: AgentResult, env) -> None: ...         # 每条终止路径，含外部取消
    async def on_run_error(self, *, error, partial_steps, env) -> None: ...     # 超时 / 异常 / 取消，紧跟 on_run_end
```

`env` 在三个回调里都是**原始**环境（不是 AOP 代理），而且都还活着：teardown 永远在 `on_run_end` / `on_run_error` 之后。

**AOP 拦截声明**：Hook 通过 `__intercepts__` 声明要拦截的方法、目标角色和 advice 类型：

```python
class Intercept:
    method: str           # 目标方法名（如 "observe", "ainvoke"）
    role: str             # 目标角色（"env", "agent", "llm", "loop"）
    advice: AdviceType    # BEFORE, AFTER, AROUND

class AdviceType(Enum):
    BEFORE = "before"     # 方法调用前执行，接收 args/kwargs
    AFTER = "after"       # 方法调用后执行，接收返回值
    AROUND = "around"     # 包装整个调用，接收 proceed 可调用对象
```

Handler 命名约定：`{advice}_{method}` — 如 `after_observe`、`after_ainvoke`、`around_build_react_prompt`。

**统一事实采集。** `trace_writer` 是唯一记录组件，不再有独立 `metrics` hook 或 Loop 内用量账。它在 `around_ainvoke` 发起前登记稳定调用身份，在成功、失败、取消时结算同一本调用账。调用带 run/step/call 身份、caller、purpose、model、开始结束时间与 provider 实际报告的用量。未配置统计时结果是 `None`；启用统计但没有调用是测得零；调用没有 usage 是缺失，有 usage 且为零是已测得零。失败回复报告的用量与缓存计数照实累计。

**Weaver 织入流程**：

```python
# AgentRuntime.prepare() 中：
weaver = Weaver(active_hooks)
wrapped_env = weaver.wrap(env, role="env")
wrapped_llm = weaver.wrap(llm, role="llm")       # llm.ainvoke → around 里登记 LLM 调用
wrapped_agent = weaver.wrap(agent, role="agent")
wrapped_recorder = weaver.wrap(StepRecorder(), role="loop")  # record_step → 完整 step 落盘

# Loop 收到 wrapped 依赖，照常调用其方法；不直接调用 Hook
loop.weave(wrapped_agent, env=wrapped_env, llm=wrapped_llm,
           step_recorder=wrapped_recorder, ...)
```

`AOPProxy` **懒解析**：wrapped 方法在首次属性访问时构建，并在底层属性被重新绑定时重建。这是必须的——`agent.run` 要到 `Loop.weave()` 之后才存在，wrap 时预构建会把框架的招牌拦截点 `Intercept("run", role="agent")` 判死。未被拦截的属性访问透明委托给原始对象；如果某个角色没有任何拦截器，`weaver.wrap()` 直接返回原始对象（零开销）。

代价是校验也变懒了，所以装配期补了两道检查，都在 `AgentRuntime.prepare()` 里：`weaver.raise_for_unclaimed_roles()`（wrap 之后）抓「声明了没人 wrap 的 role」，`weaver.audit_intercepts()`（**`loop.weave()` 之后**，这是答案第一次可知的时刻）抓「role 被 wrap 了但拦截仍然永远不会触发」——本次运行没有该对象（env-less run 里的 `env` 拦截）、目标没有该属性（`env.obsserve` 拼写错误）、advice 与方法形状不匹配。两者都 raise：拦到空处的 hook 与没配的 hook 等价，配置错误应当在装配期暴露，而不是等到第一次 LLM 调用。有环境时才需要拦截环境的 hook 因此在无环境时不声明 `env` 拦截，而不是让审计放行：`live_monitor` 由 CLI 绑定监控状态时按环境是否存在选择（§4.17）；`trace_writer` 实现 `EnvironmentInterceptor`（`core/hooks/base.py`），Runtime 在构造 Weaver 之前经 `declare_environment_presence` 调用它的 `intercept_environment(present)`。`config explain` 列出的拦截出自同一判断：有 agent 时就是 `prepare()` 做的那次，没有 agent、不 prepare 时按配置里有没有 environment。

`AOPProxy` 的透明度边界也是显式的：CPython 的特殊方法查找走 `type(obj)`，不经过 `__getattr__`，所以代理只对**具名属性**透明。`__bool__` / `__eq__` / `__hash__` 转发给 target（它们过去会静默给出错误答案）；`__enter__` / `__exit__` / `__aenter__` / `__aexit__` / `__iter__` / `__aiter__` 在 target 实现时转发、否则抛 `UnsupportedProxyProtocolError`（`TypeError` 子类，消息里点名 target）。这之外的 dunder 一律不声明——声明 `__call__` 会让 `callable(proxy)` 对任何 target 都返回 True，用一个安静的谎话换掉一次响亮的失败。

**`blocking` 的唯一语义 = 并发**：`blocking=True` 内联 await handler，被拦截的方法要等它做完才返回；`blocking=False` 把 handler 交给后台任务，调用立刻继续。错误隔离是**推论**而非第二重语义——非阻塞 handler 抛错时它所修饰的调用早已返回，没有可传播的对象，因此只能记日志。三种形状不成立，装配期以 `UnsupportedAdviceError` 拒绝：同步目标上的 `blocking=False`（没有可延后的调用）、`around` 上的 `blocking=False`（handler 本身就是那次调用，返回值经由它的 `proceed()`）、handler 与目标的 sync/async 形状不匹配。

推论要写清楚：**`blocking=False` 的 handler 进不了 trace**。它在 WAL commit 点之后才结束，算出来的东西落不到任何产物上。所以 `trace_writer` 的快照和封存 handler 必须是 blocking，而 `blocking=False` 只适合下游无人读取的异步旁路（本地 `live_monitor` 的内存捕获见 §4.17）。这些后台任务由 `BackgroundTasks` 持有强引用，并在 `AgentRuntime._finish()` 里 env 拆除之前 drain（默认 5s，超时则取消并 WARNING 点名 hook）——「fire and forget」不等于「fire and never run」：裸 `asyncio.create_task()` 丢掉句柄时，事件循环只持弱引用，handler 可能被 GC 掉，也可能因为没人等而根本没执行。

**已知 Hook 实现**：

| hook_id | 状态 | blocking | `__intercepts__` | 职责 |
|---------|------|----------|-------------------|------|
| `trace_writer` | ✅ | True | `around_ainvoke(llm)`、`around_act(env，仅有环境时)`、`around_execute_step(loop)`、`around_observe_terminal(loop)`、`around_record_step(loop)` | 单一调用账与执行计时，含每个动作的起止时间；步骤交接时封存所选事实和原始 env 的归档批次，有界后台任务按原身份提交/重试。收尾只把未交出步骤的调用与真正尾部证据写为 trailing；错误进入记录健康，不控制执行。 |
| `prompt_contribution` | ✅ | True | `around_build_react_prompt(agent)` | 以显式步骤/阶段生成并传递贡献，Agent 负责消息位置；helper 调用独立归因，由 Runtime 统一收尾。 |
| `omit_source_replies` | ✅ | True | `around_build_restored_history(agent)` | restored 历史的对照臂：交给 Agent 的恢复历史不带源回复（thought），动作与实际恢复执行照常；trace 的 `source_history` 不变（§4.1）。 |
| `live_monitor` | 本地终端监控 | True | `around_observe(env)`, `around_act(env)`, `around_ainvoke(llm)`，步骤记录与 lifecycle | 只在内存中捕获开始、完成、失败和取消；界面独立刷新，不在 handler 中做文件、网络或终端 I/O。无环境时不声明 env 拦截。见 §4.17。 |

**Hook 的构造和身份。** conscribe 2.0 支持 dataclass 受体，保留默认值，不再把 `WiredField` 当成 dataclass 默认参数。`TraceWriter` 保留具名 `Annotated` 参数的手写构造函数，便于初始化记录状态；`HookBase` 是 `@dataclass(eq=False)`，hook 按身份比较。受体卫生测试只拒绝真正的 `WiredField` 默认值，不再禁止 dataclass 构造参数。

**与 emitter.dispatch() 的权衡**：

AOP 方案让 Loop 代码完全干净 — 没有 `emitter` 参数，没有 `dispatch` 调用，甚至不知道 hook 的存在。代价是只能拦截实际存在的方法调用。对于不映射到方法调用的语义事件（如 PlanExecuteLoop 的「规划完成」），解决方案是将该语义建模为 Agent Protocol 方法（如 `agent.parse_plan()` → `after_parse_plan` hook），AOP 自然覆盖。

#### 记录器从环境拉取证据（先例）

环境证据（§5.1）是生产代码里第一个"hook 通过 Environment Protocol 方法拉数据"的地方。它不是 emitter 旁路，规则是：

1. **只在原始环境上拉。** 记录器从 `on_run_start(env=...)` 拿到的是没有被 AOP 包装的环境，拉取就调它的 `collect_archive()`，从不经过代理，所以没有 hook 会拦截这次拉取，也不会递归。
2. **只在自己的封存边界拉。** 步骤交接时通过 `around_record_step` 拉取一次并封存归属，再交后台 WAL；收尾至多再收取一批真正尾部证据。持久化重试复用封存批次，不再次驱动环境采集。
3. **从不驱动环境。** 不调 `act` / `observe`，不调任何会改变环境的方法；`collect_archive` 只交出 capability 已经采到的东西。
4. **environment 永远拿不到步号。** "第 t 步的提交取走的东西就归第 t 步"：第 t 步动作引发的网络往来、第 t 步开头观测时的原始 DOM 与存储快照，都在第 t 步的 `record_step` 之后被取走，所以归第 t 步。cycle 也不等于步号，capability 只给每条证据带上自己的 `captured_at` / `cycle` / id。

runtime 通过 `ArchiveConsumer` Protocol（`core/archive/protocols.py`）发现记录器：它只有一个属性 `archive: list[ArchiverBase]`，与 YAML 受体同名（M29）。`archive` 非空的 hook 就是本 run 的归档消费者，v1 至多一个（§4.5）；它必须是 blocking 的，因为 `blocking=False` 的 handler 进不了 trace（见上）：`negotiate_archive` 把不是 blocking 的消费者作为一条问题报出（§4.5 的表），所以它在 `prepare()` 就被拒绝，不会在提交点之后悄悄丢掉证据。

**执行结果与记录健康分开。** 初始化、采集、编码、WAL 提交和收尾的记录故障都由记录器隔离，不能决定 Loop 是否继续，不能伪装成模型或动作失败，也不能覆盖真实错误、guard 否决和外部取消。没有 `RecordingFailedError` 终止路径，也没有记录器控制的停机策略。源归档读取失败仍是环境执行失败，不属于本次记录写入故障。

`record_step` 是封存边界，不是持久化成功边界。已封存段的 step、call、动作、观测和已交接归档批次身份固定；封存之后的归档拉取失败、被拒或带缺口，只损失那部分证据，不撤销封存，这一步照样提交（§5.4）；后台补写仍提交原段。只有未交出步骤的调用进入 trailing。记录器保持 blocking 以保证快照和封存顺序；持久化和恢复重试由有界记录任务承担，不占用 `agent.run` 等待链。队列容量、重试次数与收尾等待均为显式配置；资源耗尽报告丢失范围，仍尝试后续可保存的步骤。

manifest 只索引实际提交内容。`complete_through` 是连续完整前缀，`missing_steps` 是预期步骤中未提交的索引；洞后已提交步骤仍可查看和汇总。缺口补齐后可恢复完整，历史 `recording_problems` 不消失。持久化持续失败时内存 `recording_health` 和日志仍说明问题，不承诺把磁盘错误写进坏掉的磁盘。

### 4.7 LLM 抽象

**Chat 与 Decision 共享身份和生命周期，分别注册。** 公共 `BaseInferenceModel`
Protocol 声明 `model/model_type/provider/name`、显式 `inference_kind: chat | decision`
与 `aclose`，不声明宽泛调用方法。`BaseChatModel` 的消息、tools、泛型
`ChatInvokeCompletion[T]` 合同保持；`BaseDecisionModel.ainvoke(request: DecisionRequest)
-> DecisionResult` 接收结构化 state、独立 typed questions 和图片 assets。
实现基座 `ModelBase` 只共享 `ClientResources` 与关闭逻辑；`ChatModelBase` 和
`DecisionModelBase` 分别绑定 `LLMRegistrar/llm` 与 `DecisionModelRegistrar/decision_model`。
两轴都用 `model_type.provider` 嵌套判别器，自动发现，均纳入 `collect_registrars()`。
现有 Agent、chat judge、prompt helper 的受体继续是 Chat，不扩大为公共模型类型。
直接 Python 装配也须在使用消费者前检查明确的调用协议，不能靠同名 `ainvoke` 猜测。
通用用量类型名为 `ModelInvokeUsage`，保留原有未知/零/缓存/图片语义，不留旧名别名。`completion_reasoning_tokens` 是 `completion_tokens` 里属于推理的那部分，只取响应自己报的数（OpenAI 兼容回复的 `completion_tokens_details.reasoning_tokens`、Responses API 的 `output_tokens_details.reasoning_tokens`、Gemini 的 `thoughts_token_count`（它已计入 `completion_tokens`）、OCI 的 `completion_tokens_details`）；没报就是 `None`，不是 0（Anthropic 的 thinking 计在 `output_tokens` 里，不拆分）。它默认 `None`，此前写下的调用记录照常读取，trace 格式版本不变。它只在调用记录里（完整记录与轻量调用指标），步与 run 的合计不加它：合计的分项把没报当 0，会把不拆分的 provider 读成没有推理。

**Decision 原生合同。** `DecisionRequest` 保留字符串、对象或数组 state；问题以 ID
映射到 Choice / Score / Noul / Box 的判别联合。instructions 与 criteria 中的描述保留
JSON 结构和原生 null；所选路由不支持的 null 组合在发送前明确拒绝。2026-10-19 的真实请求确认 DMX/OpenRouter 都拒绝 null Score 等级，OpenRouter 还拒绝 null instructions；Noul criteria 按 API 的非 null 描述合同校验。选择项最多 255，Score 有 2–10 档。答案 ID、type 与请求逐项
对应；Choice 保留 choice/全部 probabilities/confidence，Score 保留连续 score、结构化
legend、全部 probabilities/confidence，Noul 保留概率，Box 保留归一化四边和 confidence。
校验有限数、概率范围、选项和等级集合、argmax、等级期望的舍入误差及 Box 边界；
不重算覆盖 confidence，不把 Noul 阈值化。问题独立求值，依赖关系由调用者组织。

图片使用带原生 `format/data_base64` 的显式 assets；引用可以出现于 state、instructions
和 criteria 的字符串值，不能仅出现在对象键。资产可带来源摘要和变换说明，这些属于
逻辑输入证据，不发送给 provider。provider 校验格式、尺寸、引用、模型版本能力和
请求体预算；不静默丢弃或缩放。Box 需要图片，目标是否存在由独立 Noul 问题回答。
首版只实现 PNG/JPEG 的 JSON 传输，不增加 multipart、音频、视频或长上下文开关。

`typesafe.direct` 发送 `<base_url>/v1/systemone`，base_url 是服务根：官方为
`https://api.typesafe.ai`，DMX 为 `https://www.dmxapi.cn/typesafe`；图片/Box 带官方
实验请求头。`typesafe.openrouter` 发送 `<base_url>/alpha/decisions`，默认根为
`https://openrouter.ai/api`，把逻辑图片引用编译为 Decisions 支持的 image_url parts。
路由不共享未经转换的 HTTP 请求。返回同时保留请求模型、实际服务模型、原生 usage
及实际返回的 ID/provider 等元数据；不存在的值保持未知。HTTP、超限、不支持输入、
答案校验失败各有类型化错误，答案错误不进入 ReAct 的聊天解析重试。
每次 `ainvoke` 默认只发送一次请求，无隐藏重试；失败的已知 usage 仍保留。客户端
懒创建、复用、借用 transport 与有界清理统一遵守上面的公共生命周期合同。

协议依据为 [TypeSafe API](https://docs.typesafe.ai/api)、
[结构化字段](https://docs.typesafe.ai/primitives/advanced)、
[图片](https://docs.typesafe.ai/images)、[Box](https://docs.typesafe.ai/primitives/box)
及 [OpenRouter Decisions](https://openrouter.ai/docs/guides/community/multimodal-decisions)，
核对日期 2026-10-19。路由的实现覆盖与真实请求验收分别报告。

```
BaseChatModel (Protocol)
    ├── model: str
    ├── model_type: str         # "openai", "anthropic", "google", ...
    ├── provider: str           # "direct", "azure", "aws", ...
    ├── name: str
    ├── aclose(*, timeout=5.0) → tuple[ClientCleanupDiagnostic, ...]
    └── ainvoke(messages, output_format?) → ChatInvokeCompletion[T]
         │
         ├── OpenAICompatBase          ← openai.direct, openai.azure, openai.groq,
         │                               openai.deepseek, openai.cerebras,
         │                               openai.openrouter, openai.vercel
         ├── AnthropicCompatBase       ← anthropic.direct, anthropic.aws
         ├── ChatGoogle                ← google.default
         ├── ChatBedrock               ← bedrock.default
         ├── ChatOllama                ← ollama.default
         ├── ChatMistral               ← mistral.default
         └── ChatOCI                   ← oci.default
```

**双层判别器**：`model_type.provider`（如 `openai.azure`）。

LLM 实例**只有一条构造路径**：YAML 写 `model_type` + `provider` 判别键 → `ConfigComposer`
（`core/composer.py`）解析成判别联合配置 → 查 `LLMRegistrar` 拿 provider 类 → 实例化 → 按
`__wiring__` setattr 注入 `agent.llm`。**没有模型昵称表，没有 `get_llm_by_name()` 这类字符串工厂**——
模型的 wire id 直接写在配置里。

**客户端属于一次执行，由持有方显式收尾。** `BaseChatModel.aclose(*, timeout=5.0)` 是公开、具类型的
生命周期合同，返回 `tuple[ClientCleanupDiagnostic, ...]`；空元组表示本次没有未完成清理。第一次请求才创建
客户端，同一次任务或独立 judge 批次的后续调用与请求重试复用它；构造、prepare 与 dry-run 不打开连接。
Runtime 在任务的所有结束路径清理其实际持有的 agent 模型；独立评测入口负责自己的 judge，提示贡献者负责自己
显式持有的辅助模型，互不扫描其他消费者。直接 Python 调用使用 `core.llm.lifecycle.model_session(model)`
异步上下文，或在 `finally` 调用 `await model.aclose()`。一个执行内可以多次调用，但不能在仍有调用使用时提前关闭。

provider 作者通过公开 `ChatModelBase.client_resources: ClientResources`（`core/llm/client_resources.py`）
声明 `get_or_create`、`own`、`use` 和 `run_sync`；缓存字典、关闭任务与使用计数是该组件的私有实现，消费者不读它们。

同一基类的 `use_strict_schema` 与 `validate_structured` 是 provider 作者共享的结构化输出入口；`OpenAICompatBase` 的 `request_client`、`standard_ainvoke`、`common_model_params`、`tool_kwargs`、`wire_messages`、`takes_schema_in_prompt`、`prompt_schema_response_format`、`get_usage`、`get_usage_from_responses`、`effective_reasoning_effort` 和 `is_listed_reasoning_model` 服务已有 provider 子类的复用与覆写。它们不扩张模型调用者的 `BaseChatModel` Protocol。身份字段使用 `model/model_type/provider/name`，不保留重复的 `model_name` 别名。由环境变量或模型家族推导的请求默认值是局部执行值，不写回配置字段。

**OpenAI 兼容路由的结构化输出有两条路径，由模型家族和端点决定，不由配置决定。**`standard_ainvoke` 默认发 `response_format: json_schema`；`takes_schema_in_prompt()` 为真时，把编译后的 schema 作为指令写进 system 消息（追加到开头的 system 消息，没有就新插一条），请求带 `prompt_schema_response_format()`（默认不带 `response_format`），回复同样经 `strip_code_fence` → `validate_structured`，截断与空回复的处理与另一条路径相同。默认判断是模型家族的 `supports_json_schema`（`claude`、`deepseek` 为 False）；端点可以覆写方法，不能加配置开关：`openai.vercel` 另加两个沿用的触发条件，`openai.deepseek`（DeepSeek 官方 API，实测没有 `json_schema`，只有 `json_object`）要求 `json_object`。DeepSeek 的输出预算按 `max_tokens` 发出（它不认 `max_completion_tokens`），思考时不发不生效的 `temperature`，从不发 `frequency_penalty`，`reasoning_effort` 用它自己文档里的 `none | low | high | max`。`finish_reason` 为 `length` 抛 `ModelOutputTruncatedError`；为 `insufficient_system_resource` 或 `aborted`（服务端自己停下）抛 `ModelGenerationInterruptedError`，带已生成的部分文本，不当作模型能改的校验失败。实测与报错原文见 `.agents/skills/af-add-an-llm-provider/references/guide.md` §11。

所有权按实际构造记录：provider 自建 SDK/HTTP transport 由 provider 关；通过 Python 注入的 transport 默认
借用，由注入者关。SDK wrapper 的 close 若会关闭借用 transport，就不能调用它；不支持的注入必须明确拒绝。
同步与异步资源分别登记，逐项独立有界清理；一项失败不跳过其余。默认清理预算每个模型 **5 秒**，覆盖等待正在使用
的请求和资源关闭，不改模型请求 timeout 或重试次数，也不新增 YAML 资源作用域。清理只取消未完成的关闭尝试，
不能把取消请求当成已释放；同步关闭用独立 daemon worker，避免阻塞事件循环或解释器的默认线程池收尾。仍在使用的
资源延后到调用退出再关闭，并明确报告未完成。所有自建 transport 从执行缓存移除，即使清理失败也不进入下一次执行；
同一 Python 模型对象的顺序新执行重新建客户端，跨 asyncio 事件循环也一样。借用 transport 的后续复用由注入方决定。

关闭失败、超时和未完成通过普通执行日志可见，并作为类型化诊断返回，完全没启用记录组件也能发现；它们不是模型请求
失败、动作错误或重试理由。成功回复及 usage 不变，原调用异常不被覆盖，外部取消仍传播。重复清理不重复关闭已处理的
资源，也不为清理创建客户端；已有未完成诊断保留到新执行开始，不能把第二次清理的无操作冒充释放成功。`close_models(...)` 给 Runtime、judge 和辅助模型 owner 提供同一有界收尾入口；每个
模型独立清理，不能让一个坏模型拦住其他模型。这里没有全局池、引用计数、多 agent 共享模型或私有 SDK 字段探测。

已核实的 SDK 关闭合同（版本是本次验收环境，不是统一共享实现的假设）：

| provider | 实际所有权与公开关闭入口 |
|---|---|
| OpenAI 系、Anthropic 系、Groq | OpenAI 2.29.0、Anthropic 0.86.0、Groq 1.1.1 的 `close()` 都关闭其 HTTPX transport；借用 transport 时只释放 wrapper 引用。Azure 自建 HTTPX 在 SDK 构造前登记，SDK 构造失败也收尾。DeepSeek/Cerebras 的 `client_params.http_client` 与字段注入统一解析，冲突拒绝。 |
| Google | google-genai 1.68.0 的 `Client.close()` 与 `Client.aio.aclose()` 分别登记；SDK 明确保留 `HttpOptions.httpx_client` / `httpx_async_client` / `aiohttp_client` 的借用对象。嵌套 `client_args` / `async_client_args` 的 transport、mounts、connector 注入拒绝，提示改用明确客户端注入。 |
| Mistral / browser_use | HTTPX 0.28.1 自建 `AsyncClient.aclose()`；Python 注入的 `http_client` 借用。Mistral 客户端构造失败后已创建的 transport 仍登记收尾。 |
| Ollama | 要求 ollama >= 0.6.2，验收 0.6.3 的公开 `AsyncClient.close()`；0.6.1 无此入口。注入 `client_params.transport` / `mounts` 时，所有权包装仅转发公开 HTTP 请求，SDK 关闭不向借用 transport 转发。SDK 不支持的 `http_client` 明确拒绝。 |
| Bedrock | boto3/botocore 1.42.72 的新建 client `close()`；Python 注入的 Session 本身借用，`session.client()` 创建的客户端仍由执行拥有。 |
| OCI | OCI 2.168.2 的公开 `base_client.session.close()`；本 provider 实际创建的 principal signer 按具体 SDK 类型登记公开 session，逐项独立关闭。SDK 构造内部抛错且未交回对象的资源不可访问，不探私有字段，也不声称框架已释放它。 |

同步 SDK 请求取消时线程可能仍在执行：`run_sync` 保留该请求的资源使用权直到线程实际返回，不能仅因 asyncio
等待者取消就提前关闭 transport。Python 无法强制终止一个拒绝返回的 SDK 线程或拒绝取消的关闭协程；预算耗尽时
报告 `in_use` / `timeout`、隔离该次资源，不能宣称强制释放成功。独立评测中 `EvaluationRunner.run` 是一次 judge
批次，`evaluate_trace` 是一次单轨迹执行；`LLMJudgeEvaluator` 通过 `EvaluatorLifecycle.aclose` 显式释放自己的
judge，不把评测伪装成 AgentRuntime 任务。直接调用 evaluator 的 Python 代码同样负责结束它的生命周期。

**模型的推理：响应带回什么就记什么，不索取，不回喂。**（用户 2026-10-14 的裁决）响应里本来就带着的推理，provider
填进 `ChatInvokeCompletion.thinking`；只以不透明形式返回的推理填进 `redacted_thinking`。每条返回路径（文本、
结构化输出、工具调用）都填，各家读的是：

| provider | `thinking` | `redacted_thinking` |
|---|---|---|
| OpenAI 兼容（`OpenAICompatBase` 及其子类的 chat completions 路径） | 回复消息的 `reasoning_content`（DeepSeek、Qwen、vLLM 的字段名，dmxapi.cn 上的 `qwen3.8-flash` 就用它），没有时取 `reasoning`（OpenRouter 的字段名，Groq、Cerebras、Vercel 也用它）。两者都不在 OpenAI SDK 的消息模型里，按 SDK 保留的额外字段读 | — |
| `openai.azure` 的 Responses API 路径 | `reasoning` 条目的推理文本，没有文本时取它的摘要 | 条目的 `encrypted_content`（只在请求 `include` 它时才有，本 provider 从不请求） |
| `anthropic.direct` / `anthropic.aws` | `thinking` 块的文本（签名不是推理，不记） | `redacted_thinking` 块的 `data` |
| `google.default` | 第一个候选里带 `thought` 标记的部分 | — |
| `bedrock.default` | `reasoningContent.reasoningText.text` | `reasoningContent.redactedContent`（字节，记为 base64） |
| `ollama.default` | `message.thinking` | — |
| `mistral.default` | `thinking` 内容块里的文本 | — |
| `oci.default` | generic 格式回复消息的 `reasoning_content`；Cohere 格式没有这个字段 | — |
| `browser_use.default` | 无：回复只有 `completion` 和 `usage` | — |

合成规则只有一份（`core/llm/views.py::join_reasoning`）：多段推理按响应里的顺序以换行相连；空的、只有空白的、
不是字符串的段丢掉；什么都不剩就是 `None`，从不是空字符串；保留的段原样，不修剪。另外三条：

- **不为它改请求。** 没有任何为拿到推理而加的参数：不设 Gemini 的 `include_thoughts`，不发 Anthropic 的 `thinking`、
  Ollama 的 `think`、OpenRouter 的 `include_reasoning`。推理只在响应本来就带着时出现；`reasoning_effort`、
  `thinking_budget` 这些既有旋钮照旧，含义不变。`tests/test_core/test_llm/test_model_reasoning.py` 钉住 OpenAI
  兼容与 Anthropic 各条路径的请求体，与记录推理之前的完全相同。
- **不回喂，agent 不读。** Loop 只把 `completion` 写进记忆，agent 解析的也只是 `completion`，所以推理进不了下一轮
  提示。回复本身也不能夹带推理：Anthropic 的回复取第一个不是推理的块（结构化路径原先直接取 `content[0]`，
  thinking 块在前时整块被字符串化，校验失败后作为模型自己的回复喂回给它）；Gemini 的 `response.text` 本就跳过
  thought 部分；Mistral 的 thinking 块不进回复文本。`tests/test_core/test_loops/test_reasoning_stays_out_of_the_prompt.py`
  让两次只差回复里有没有推理的 run（经真实的 OpenAI 兼容 provider、三个已发布的 agent 各一次）比较发给模型的
  每个请求，逐字节相同。
- **只记录。** 它随调用记录落盘（§5.3），训练导出不读它（要不要当 teacher 的思路，下一阶段再定）。失败的调用
  （回复被拒、被截断）没有 `RecordedCompletion`，它的推理不记。

### 4.8 Memory 抽象

**当前状态**：Memory 走 `MemoryRegistrar`（`memory_id`, flat），AgentBase 通过 `__wiring__["memory"] = "memory"`（Mode 1，自动发现）选择并注入实例。配置校验保证 `memory_id` 和所选策略的子配置有效；Loop 通过 `memory.add_message` / `memory.get_context_messages` 使用该实例。

```
MemoryRegistrar = create_registrar("memory", BaseMemoryManager, discriminator_field="memory_id")

BaseMemoryManager (Protocol)
    ├── add_message(message)
    ├── get_context_messages() → list[BaseMessage]
    ├── clear()
    └── supports_protocols: frozenset[MessageProtocol]
         │
         ├── SimpleMemory          所有消息，有上限
         ├── SlidingWindowMemory   最近 N 条消息
         ├── TokenBudgetMemory     在 token 预算内尽量保留
         └── SummaryMemory         LLM 压缩摘要（规划中，未实现）
```

**消息协议兼容性在装配期检查**：Loop 用 `__required_message_protocols__` 声明需求，Memory 用 `supports_protocols` 声明支持范围。`AgentRuntime.prepare()` 校验 `Loop requires ⊆ Memory supports`；失败时 `IncompatibleMemoryError` 点名 Loop、Memory 和缺失协议。`__wiring__` 在此处负责选择并注入 Memory，不把 `user_assistant` / `tool_call` 等协议名当作 `memory_id`。目前 ReAct 要求 `user_assistant`；`simple` 支持它，`sliding_window` 和 `token_budget` 还支持 `tool_call`。未来 Loop 的具体消息形状仍须在实现时定义。

### 4.9 Capability 抽象（ActionGroup + Resolver 合体）

> **设计来源**：经过对 8 大 benchmark domain 分析，得出结论：**没有 "base sandbox"，只有 capabilities**。Browser agent 的世界是浏览器（CDP），Code agent 的世界是 Docker 容器，Mobile agent 的世界是 Android emulator——它们之间不共享 base，只是 framework 层编排逻辑相同。
>
> **核心合并**：Capability = DD 的 ActionGroup（action 描述 + 执行） + DD 的 Resolver（observation fragment 按需解析）。每个 Capability 同时负责自己 scope 内的 action 执行和 observation 提供。Environment 组合 capabilities，做前缀路由。
>
> 架构示意图见 [`flat_capability_architecture.svg`](./flat_capability_architecture.svg)。

```python
CapabilityRegistrar = create_registrar(
    "capability", BaseCapability,
    discriminator_field="capability_id",
)

@runtime_checkable
class BaseCapability(Protocol):
    @property
    def capability_id(self) -> str: ...

    # 别的 capability 执行的 action，是否让本 capability 当前的 capture 失效？
    # 默认 False：capability 之间是互相独立的世界，一次 browser.click 不会改变
    # Android 模拟器显示的内容，就不该为它付一次 ADB 往返。只有两个 capability
    # 真的共享同一块表面时（desktop VM 的屏幕上正跑着那个 browser）才置 True，
    # env 随后在每次外部 action 之后为它开一个新的 observation cycle。
    __invalidated_by_foreign_actions__: ClassVar[bool]

    # === Action 接口 ===
    # provided_actions 从 __wiring__["action"] 派生（对称 provided_fragments）
    @property
    def provided_actions(self) -> frozenset[str]: ...

    # 持有一组实例化的 action（从 YAML 注入或默认全集）
    actions: list[ActionBase]

    def get_action_descriptors(self, **context) -> list[ActionDescriptor]: ...
    async def execute_action(self, action: Action) -> ActionResult: ...

    # === Observation 接口（原 Resolver） ===
    @property
    def provided_fragments(self) -> frozenset[str]: ...

    # 打开 observation cycle *cycle*，丢弃上一次 capture。由 Environment.observe
    # 在解析本 cycle 任何 fragment **之前**对每个参与 capability 调用一次。
    # 两次调用之间的所有 resolve_fragment 属于**同一次** observation。
    # 默认 no-op：逐 fragment 抓取的 capability 不持有 cycle 状态。
    # 在这里抛错等价于本 capability 整个切片解析失败。
    async def begin_observation(self, cycle: int) -> None: ...

    async def resolve_fragment(self, fragment_key: str) -> ObservationFragment: ...

    # === 执行层 backend（IoC 注入，见 §4.9.x）===
    # 每个 capability 子类通过 __wiring__["backend"] = "<provider_registry>"
    # 声明它可挂哪一类 backend；ConfigComposer setattr 注入 provider 实例
    backend: BaseProvider | None

    # === 生命周期 ===
    # 任务上下文按 setup() 的签名路由（§4.3）：具体 capability 在 negotiation 之后追加它接受的
    # 每个键，各是一个具名参数（browser：cdp_url / start_url / replay_archive …，§4.10）；
    # 没有 **kwargs——它什么都不声明，也就什么都收不到；negotiation 是框架保留的参数
    async def setup(self, *, negotiation: Any = None) -> None: ...   # 内部调 backend.open()
    async def teardown(self) -> None: ...                          # 内部调 backend.close()

    @property
    def is_alive(self) -> bool: ...


# core/archive/protocols.py —— 可选的归档面：只有提供归档器的 capability 才需要真正实现
@runtime_checkable
class ArchivingCapability(Protocol):
    @property
    def provided_archivers(self) -> frozenset[str]: ...                # 全限定键
    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None: ...
    async def collect_archive(self) -> ArchiveBatch: ...               # 取走自上次收集以来的全部证据
```

**归档面是单独的 Protocol，不进 `BaseCapability`。** 它是可选的：不归档的 capability 不该为它实现任何东西，而往 `@runtime_checkable` 的 `BaseCapability` 加成员会改变每个结构化实现者的 `isinstance` 结果。`CapabilityBase`（`core/capabilities/_registrar.py`）带着惰性默认实现满足它：

- `provided_archivers`：从 `__wiring__["archiver"]` 派生（`_wiring_keys_for(type(self), "archiver")`，与 `declared_fragments` 同一种做法）；
- `bind_archivers([])` 什么也不做；非空的绑定抛 `NotImplementedError`，点名类名和那些键，所以声明了 provides 的 capability 必须同时覆盖 `bind_archivers` 和 `collect_archive`；
- `collect_archive()` 返回空的 `ArchiveBatch()`。

**一次收集交出它收到的，点名它没收到的。** `ArchiveBatch` 有两个字段：`files`（证据）与 `gaps`（`ArchiveGap`：`archiver_id`、`error` 类名、`detail`，`detail` 从不为空）。缺口的 `archiver_id` 与 run 记录的写法相同：缺了某个归档器的证据写它的全限定键（`browser.dom_raw`），缺的不是某一个归档器的（整个 capability 的收集、它所有归档器共用的通道）写裸的 capability id。带缺口的批次不完整，不管它带了多少文件；一个归档器失败从不连带别的归档器已经收到的证据。

**归档契约**（`core/archive/` 定义类型，capability 实现行为；`tests/_helpers/archive_contract.py` 对每个提供归档器的 capability 检查能从外面检查的那些条：替换与清空绑定、不带参数、交出并忘掉、未绑定不采、只交已绑定归档器的证据、每批至多一份 run 记录、来源信封、采不到就抛错或点名缺口，以及这些证据经 TraceWriter 提交、读回、崩溃窗口、收集失败（步照样提交）与收尾的行为，和另一个归档 capability 同处一个环境时 run 记录仍按 capability 计数、编号与去重。"不无限阻塞"、"对生产者原子交接"与"在途条目恰好交出一次"取决于 capability 的内部时序，与 capability 无关的 harness 造不出来，由各 capability 自己的测试钉住，browser 的在 M29，用 fake CDP）：

- `bind_archivers(archivers)` **替换**整个绑定（空列表 = 解除），不获取任何资源；runtime 只在 setup 之前绑定，capability 在 `setup()` 里读绑定，决定建不建自己的采集通道。收到的是 trace hook 持有的归档器实例，旋钮就在实例上。
- `collect_archive()` 不带任何参数（没有步号，没有 cycle），交出自上次收集以来采到的全部证据并忘掉它们，批次从此归调用方。不无限阻塞；在没有 setup、或 setup 半途失败的 capability 上调用也安全（没采到东西就返回空批次）；什么都采不到时**抛错**，只缺一部分时交出其余的，并在 `gaps` 里点名缺的那部分；从不返回一个假装完整的批次。
- 它可能在共享环境里另一个 agent 正在动作时被调用，所以"交出并清空"对 capability 自己的生产者必须是原子的（例如在一条同步语句里把缓冲区换出去）。
- 收集边界上还没完成的条目（还在读的响应体）留到之后的批次，恰好交出一次，不重复也不丢；收尾那次收集是最后一次：capability 要么在有限时间内把它们收完，要么把它们标成未完成交出（怎么标是 capability 自己的事，M29 的 `body_status`）。
- 每个 `ArchiveFile` 自带来源信封 `meta: ArchiveProvenance`：`captured_at`（墙钟 Unix 时间）、适用时的 `cycle`、capability 作用域的 `ids`（session / frame / loader id 等）。文件与缺口的 `capability_id`（`archiver_id` 的第一段）必须是这个 capability 自己——environment 把别的前缀当作该 capability 的收集失败；步级文件必须属于一个已绑定的归档器键，缺口必须点名一个已绑定的归档器键或持有归档器的 capability（记录器检查，§5.4）。
- 每个批次里每个 capability 至多一份 run 记录（`scope="run"`，JSON，不内容寻址；`archiver_id` 可以是裸的 capability id，也可以是它的一个已绑定归档器键）。内容变化才需要再发；与上一版字节相同的再发会被记录器丢掉（§5.4）。

**"最后一次收集"capability 无从知道。** `collect_archive()` 不带参数，收尾那次收集发生在 `on_run_end` 里，teardown 在它之后（§4.2），capability 分不出哪一次是最后一次。所以上面"收尾那次要么收完、要么标成未完成"在实现上落成**每一次**收集都遵守的同一条规则：在有限时间内等还在读的条目，仍未完成的条目留到之后的批次，并在本批写一条轻量的"未完成"标记（不是证据本身，只是状态）；条目完成后它的证据恰好交出一次。哪个条目的最后一条是"未完成"标记，它就是 run 结束时还在途的那个。Browser 的落法（`index.jsonl` 的 `pending` 行）见 §4.14。

BrowserCapability 是第一个实现归档面的 capability：recorder 连接、archive scope、归档器基类与运行期会话、回放档位、证据格式与隐私，全部在 §4.14。

**`ArchiveFile` 与 `ArchiveBatch`**（`core/archive/types.py`，frozen dataclass，不挂受体）：

```python
ArchiveScope = Literal["step", "run"]

@dataclass(frozen=True)
class ArchiveProvenance:
    captured_at: float                                      # 墙钟，与 StepRecord.timestamp 可比
    cycle: int | None = None
    ids: Mapping[str, str] = field(default_factory=dict)    # capability 作用域的标识

@dataclass(frozen=True)
class ArchiveFile:
    archiver_id: str              # 全限定键（run 记录也可以是裸 capability id）；进路径，作为 provenance
    name: str                     # 归档器自定的文件名：单个路径段，如 "exchanges.warc.gz"、"storage.json"
    media_type: str               # 如 "application/json"
    data: bytes
    meta: ArchiveProvenance
    content_addressed: bool = False   # True → env/blobs/sha256/，run 内去重
    scope: ArchiveScope = "step"

    @property
    def capability_id(self) -> str: ...   # archiver_id.partition(".")[0]

@dataclass(frozen=True)
class ArchiveGap:                 # 一次收集没能交出的证据，以及原因
    archiver_id: str              # 缺了谁的：全限定归档器键，或裸 capability id（不属于某一个归档器的损失）
    error: str                    # 异常类名
    detail: str                   # 原因，从不为空
    @classmethod
    def of(cls, archiver_id: str, error: BaseException) -> "ArchiveGap": ...   # 空消息记成类名
    @property
    def capability_id(self) -> str: ...   # archiver_id.partition(".")[0]

@dataclass(frozen=True)
class ArchiveBatch:               # 一次收集取走的全部文件，和它点名的缺口
    files: tuple[ArchiveFile, ...] = ()   # 传入 list 时转成 tuple
    gaps: tuple[ArchiveGap, ...] = ()     # 有缺口的批次不完整
    @property
    def is_empty(self) -> bool: ...       # 既没有文件也没有缺口
    @classmethod
    def merge(cls, batches: Iterable["ArchiveBatch"]) -> "ArchiveBatch": ...   # 文件与缺口都保持顺序

class ArchiveContractError(ValueError): ...   # 收集到的批次违反契约（由消费者 / environment 抛出）
```

`ArchiveFile.__post_init__` 在构造时校验，违反就抛 `ValueError` / `TypeError`：`archiver_id` 非空，不含 `/` 或 `\`，不是 `.` / `..`；`scope == "step"` 要求带点的 `<capability>.<id>` 键；`scope == "run"` 允许裸 capability id，要求 `content_addressed is False` 且 `media_type == "application/json"`；`name` 是单个路径段（非空，不含 `/` 或 `\`，不是 `.` / `..`）；`archiver_id` 与 `name` 都必须能编码成 UTF-8，且编码后不超过 255 字节（`MAX_SEGMENT_BYTES`，ext4 等文件系统的 `NAME_MAX`：trace 在按字符计长度的 APFS 上写成，拷到按字节计的文件系统上也得放得下）；`media_type` 是 `type/subtype`（`/` 两侧都非空）且能编码成 UTF-8；`data` 是 `bytes`；`meta` 是 `ArchiveProvenance`；`content_addressed` 是 `bool`；`scope` 只能是那两个字面量。`ArchiveProvenance` 同样在构造时校验：`captured_at` 是有限的数（不是 `bool`），`cycle` 是 `int` 或 `None`，`ids` 是 str → str 的映射、每个键和值都能编码成 UTF-8，复制一份后只读暴露。规则写在 `core/archive/types.py` 的 `segment_problem` / `text_problem` 里。所有这些字符串最后都进 trace 的 JSON 索引或路径：孤立的代理字符（宽松解码留下的 `"\udc80"`）没有 UTF-8 编码，超长的名字会让文件系统报 `ENAMETOOLONG`，两者都会让带着它的提交失败，而且每次重试都以同样方式失败，所以在 capability 那里就拒绝。`archiver_id` 与 `name` 都是单段，所以 `env/` 下不会出现"一个路径既是文件又是目录"，也就不会在 WAL 里半途失败；拼出来的完整路径仍由消费者按 `encoding.check_relative_path` 再查一遍，上面的段规则也再查一遍（frozen dataclass 仍可能被绕过构造校验伪造出来）。

**检查点恢复面（M31）也是单独的、可选的 Protocol**（`core/capabilities/base.py`；它同时管校验，所以叫 `RestorationSupport`，不叫 `ActionRetargeting`）：

```python
@runtime_checkable
class RestorationSupport(Protocol):
    # 录下的动作在恢复出的环境里该怎么执行。只算不做：不执行、不改环境、不缓存
    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget: ...
    # 恢复出的观测与录下的观测差多少。只量不判：阈值与 strict / warn 是恢复器的事
    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence: ...
```

- 类型在 `spectragent/schemas/restoration.py`：`Retarget{action: Action | None, method: "identity" | "geometry_signature" | "tab_order_url", failure: "not_found" | "ambiguous" | None, detail}`（`action is None` 当且仅当 `failure` 不为空）；`RestorationEvidence{capability_id, url_match, recorded_url, restored_url, url_ignored_query, url_rule_ids, signature_match, signature_missing, signature_extra, screenshot_distance, document_misses, media, media_areas}`，每个量都可以是 `None`（这个 capability 不测它，或这一次测不了，例如本 run 没有激活截图）。`recorded_url` / `restored_url` 是两边观测到的原样地址；`url_match` 是去掉站点页面地址规则声明的查询参数之后的比较，`url_ignored_query` 是比较时从任一边去掉的参数名、`url_rule_ids` 是去掉它们的规则（两者为空列表就是逐字比较；§4.14 "恢复：重定位与校验"）。`media: MediaEvidence | None`（2026-10-20 裁决）：`comparisons: list[MediaComparison] | None`（两边每个媒体元素的对应关系 `matched` / `missing` / `extra` / `ambiguous`、是否直播、比较用的那次读数、两边的位置与暂停状态；`None` 是没比成：一边没有媒体状态或两次读数都不知道，`detail` 说为什么）、`recorded_unreachable_frames` / `restored_unreachable_frames`（两边自己的读数没有应答的 frame 路径，2026-10-21 裁决）与 `alignment: list[MediaAlignment] | None`（这次观测之前做的每一个对齐动作与结果；`None` 是没有预约对齐）。`MediaAlignment.outcome` 是 `aligned` / `already_aligned` / `failed`（跳转或播放没在上限内完成，已经尝试回滚）/ `not_restorable`（直播、对应有歧义、恢复出的页面里没有这个元素）/ `unknown`（录下的位置或状态不知道，没有目标）。`media_areas: MediaAreaEvidence | None`（2026-10-23 裁决）只在回放规则集的媒体区域规则声明的页面上有值：`rule_ids`（作用域同时匹配两边页面地址的规则）、`areas: list[MaskedArea{side: recorded | restored, kind: media_element | unreachable_frame, element, x, y, width, height}]`（遮掉的框，顶层视口 CSS px，裁到视口之内）、`masked_share`（遮掉的像素占图的比例，两张图取大的）、`unmasked_distance` / `masked_distance`（整图与遮掉之后的距离）、`mask_applied` 与 `detail`；有值时恢复器不判 `media`（照样记着），`screenshot_distance` 是遮掉之后的距离，遮掉的超过上限时是整图的距离（§4.14 "页面上的媒体区域"）。
- 为什么需要：browser 的元素 index 就是 Chrome 的 `backendNodeId`（`dom/models.py` 的 `element_handle`），新启动的浏览器里同一个元素的 index 不同，录下的 `click(index)` 不能原样重放；`switch_tab` 的 `target_id` 是每个浏览器新生成的 CDP targetId。坐标动作与导航动作不需要（pinned 回放下布局确定，截图哈希兜底）。
- 不进 `BaseCapability`，`CapabilityBase` 也不给默认实现：默认（恒等、不校验）在 environment 那一侧（§4.3 "检查点恢复的路由"），不实现的 capability 什么都不用写。
- capability 不知道 WAL、不知道步号：它拿到的是两个观测对象，读的是 fragment（browser 读 `browser.dom` 的自包含 `selector_map`、`browser.browser_meta`、`browser.screenshot`），不 import `core.trace`（§10）。不从两个观测里读的只有观测装不下的事实：browser 回放服务的主 frame 文档 miss（capability 记着上一次读到的总数，报自上一次 `verify_restoration` 以来的增量），自上一次 `verify_restoration` 以来做过的媒体对齐（下面 `RestorationAlignment`），以及回放服务冻结的规则集里的页面地址规则（比较 URL 时去掉哪些查询参数）与媒体区域规则（哪些页面不判媒体、截图比较遮掉媒体框；两者都不改变调用次数的约定）。所以这个调用不是幂等的，恢复器对每个恢复出的观测恰好调用一次、按顺序（`prefix_replay` 正是如此）。Browser 的实现见 §4.14 "恢复：重定位与校验"。

**为下一次观测预约对齐（2026-10-20 裁决）也是单独的、可选的 Protocol**（`core/capabilities/base.py`）：

```python
@runtime_checkable
class RestorationAlignment(Protocol):
    # 下一次观测是恢复出的、要对照 recorded 的那一次：在它的采集里、落定之后、截图之前，
    # 把能对应上的媒体元素对齐到 recorded 里录下的状态。这里只记下预约，不碰环境
    async def align_next_observation(self, recorded: Any, *, media_seek_timeout_s: float,
                                     media_end_lead_s: float) -> None: ...
```

- 为什么在采集里做：对齐若在落定之前做，在播的视频会在落定的几秒里又走掉；若在 Loop 里做，Loop 就得认识页面上的媒体。capability 在自己的采集周期里做，恰好落在"等页面落定"与"截图"之间。
- 预约只属于紧接着的那一次观测；那次观测的 `verify_restoration` 交回对齐记录并清掉预约（用上与否），所以检查点之后的策略步与终止观测从不对齐。
- 不进 `BaseCapability`，`CapabilityBase` 不给默认；不实现的 capability 什么都不用写，environment 的扇出跳过它。它与 `RestorationSupport` 分开，是因为能重定位与校验的 capability 不一定有可对齐的东西。

**采集一致性检查（M32）也是单独的、可选的 Protocol**（`core/capabilities/base.py`）：

```python
@dataclass(frozen=True)
class CaptureComparison:              # core/capabilities/base.py
    mismatches: tuple[str, ...] = ()  # 每条点名 fragment、设置项与两边的值
    unchecked: tuple[str, ...] = ()   # 配置期比不了的设置项与原因（不是差异）

@runtime_checkable
class CaptureCheck(Protocol):
    # 本 capability 按当前配置采集，会不会采出 recorded 里属于它的那些 fragment？
    # 只读配置与传进来的 fragment：不 setup、不连 backend、不缓存
    def compare_capture(self, recorded: Mapping[str, FragmentBase]) -> CaptureComparison: ...
```

- 为什么需要：训练导出用学生的运行配置渲染样本（§4.15），图像却来自源 trace。学生配置的截图管线与录制时不同（格式、viewport、DPR、图像尺寸），训练图像就与学生服务时看到的不同，而导出本身看不出来，只有 capability 知道它的配置会采出什么。
- Browser（`core/capabilities/browser/capture_check.py`，`capability.py` 只委托）：对 `browser.screenshot` 比较 `viewport_width/height`（配置 vs 录下的）、`screenshot_format`（配置 vs 录下字节的 `image_media_type`）、DPR（`replayability` 的 `device_scale_factor` vs 录下的 `image_width / viewport_width`）以及由它推出的图像尺寸（viewport × DPR）。`none` 档位用宿主机的 DPR，配置期不可知：DPR 与图像尺寸进 `unchecked`，不算差异。其余 fragment 不比较。
- 与 `RestorationSupport` 同样不进 `BaseCapability`、`CapabilityBase` 不给默认；路由与"没人实现就没有可比的"在 environment 一侧（§4.3）。

**fragment 的格式说明也是单独的、可选的 Protocol**（`core/capabilities/base.py`，2026-10-23 裁决，§4.4 "观测格式由提供方描述"）：

```python
@runtime_checkable
class FragmentFormats(Protocol):
    # 本 capability 按当前配置写出的 fragment 文本长什么样：全限定键 → 给模型读的几行说明。
    # 只说自己提供的 fragment；只读配置：不 setup、不连 backend，协商之前也能调
    def fragment_formats(self) -> Mapping[str, str]: ...
```

- 为什么需要：写出文本的一方才知道文本的格式；Agent 在自己的提示词里另写一份，换了写的一方就漂移（Skyvern 教 `[42]`、`html` 写 `data-idx="42"`）。
- Browser（`BrowserCapability.fragment_formats`）：声明了 `browser.dom` 时返回 `{"browser.dom": self.dom_serializer.describe_format()}`（§4.12）；`vision_only` profile 没有 DOM resolver，返回空。
- 与 `CaptureCheck`、`SetupCheck` 同样不进 `BaseCapability`、`CapabilityBase` 不给默认；汇总在 environment 一侧（§4.3）。

**setup 之前的检查也是单独的、可选的 Protocol**（`core/capabilities/base.py`）：

```python
@runtime_checkable
class SetupCheck(Protocol):
    # 本 capability 的 setup 会拒绝的，现在就拒绝：读配置、路由给它的 context、观测协商结果、
    # 将要绑定给它的归档器；不连 backend、不获取资源、不改状态
    def check_setup(self, context: Mapping[str, Any], *, negotiation: Any = None,
                    archivers: Sequence[ArchiverBase] = ()) -> None: ...
```

- 为什么需要：配置与任务数据决定的"能不能跑"，要在 `AgentRuntime.prepare()` 里就报（§4.2 第 3d 步），不能等到 setup——那时 trace 已经打开，run 被记成一次环境失败，sweep 对每个任务都重复同一个错误。只有 capability 知道自己的这些规则。
- `Environment.check_setup` 负责路由（§4.3 "setup 之前的检查"）：context 按 `setup()` 签名分、归档器按前缀分，capability 只拿到自己的那一份；没实现的 capability 只经过路由检查。
- capability 的 `setup()` 可以再调一次同一个方法（防御性复查，带着那时已绑定的归档器）：browser 如此。
- Browser 的实现（`BrowserCapability.check_setup`）：profile 的 watchdog 解析不了的已协商 fragment（profile 当场重新解析），任务的起始页不是一个 http(s) URL（`start_page.py::start_page_problem`，§4.10 "起始页"），§4.14 "协作检查一览"的全部行（`cdp_url` 取路由来的 context，没有再取旋钮），回放时源 run 与本配置的一致性（只读源 run 的环境指纹）；一次报全，合成一个 `BrowserCooperationError`。`replay_archive` 指向的不是已提交的 run 目录时抛 `ArchiveOpenError`。
- 与 `RestorationSupport`、`CaptureCheck` 同样不进 `BaseCapability`、`CapabilityBase` 不给默认。

#### 4.9.x Capability ↔ Backend 生命周期

**设计决策**：`backend` 是 capability 的执行层配置——回答"这个 capability 通过什么 backend 提供 transport 端点"。Browser 选 `local_chrome` / `lexmount` / `skyvern` 等；Docker 选 `local_docker` / `ssh_docker`；Desktop VM 选不同的 VNC 后端。Backend 通过与 `agent.llm` / `env.capabilities` 完全同构的 IoC 路径注入。

**装配链路**：

```
YAML
└── capability:
    - capability_id: browser
      backend:
        provider_id: local_chrome     ← discriminator
        headless: true                ← provider 自身的 conscribe 字段

ConfigComposer.compose()
├── 看到 capability config 里 backend 字段是 BaseModel（discriminated union）
├── 递归 _instantiate(backend_config) → LocalChromeProvider(...)
└── setattr(browser_capability, "backend", local_chrome_instance)
                                ↑
                  (capability 的 class-level annotation
                   `backend: BaseBrowserProvider` 标识 receptor;
                   conscribe WiredField 描述符接受 setattr)

BrowserCapability.setup(*, negotiation, cdp_url, start_url, replay_archive, storage_state, risk_* …)
├── network_source 拦截（archive）时：先经 core/archive 的 open_archive(replay_archive) 打开源 run
│     （ArchiveReader，只读已提交的部分），加载它的指纹与网络索引（§4.14 "回放"）
├── 协作检查 + 回放一致性检查（§4.14）：需要 recorder 连接的配置与 provider / cdp_url / 注入客户端 /
│     provider 旋钮是否相容、隔离、归档指纹与本 run 配置是否一致，任务的 start_url 是不是一个
│     http(s) URL（§4.10）；不相容就在启动任何浏览器之前抛错，一次报全、点名双方
├── cdp_url 优先级:
│     1. setup 的 cdp_url 参数         (任务上下文按名字路由来的 per-run override)
│     2. self.cdp_url                  (conscribe field)
│     3. self.backend.open()           ← 取 ProviderSession.cdp_url
│        （provider 声明了 LAUNCH_ARGS 时是 open(launch_args=档位的开关 + 需要时的隔离开关,
│         launch_env=档位的启动环境)）
├── 持有 self._session = ProviderSession(...)  以便 teardown 释放
├── 控制连接 CDPClient(cdp_url).start()（loopback 端点 proxy=None、compression=None：cdp_client.transport_options）
├── 需要时开 recorder 连接 + browser 级 archive scope + 回放服务（§4.14），在第一个 tab 之前
└── 创建 watchdogs / 初始 tab / 用 browser.navigate 打开 start_url（先等提交、再等 load，load 不来只记警告，§4.14）/ 等等

BrowserCapability.teardown()
├── recorder 显式清理（§4.14：关 auto-attach → 放行扣着的请求 → Fetch.disable → detach → 关连接）
├── close watchdogs / scopes / 控制 CDPClient
└── if self._session is not None: await self.backend.close(self._session)
```

**本地启动的资源所有权。** `local_chrome` 在业务 CDP 操作前验证本次 subprocess、连接端点、有效 profile 与 `ProviderSession` 是同一份启动事实。端口返回 `/json/version` 不是所有权证明：占用端口、子进程提前退出、profile 将启动转交旧进程、并发抢端口都必须明确失败。provider 核验实际 browser 进程身份，保留创建时的 process handle；关闭只终止该 handle 对应实例，不能按端口、进程名或复用 PID 猜所有权。失败、取消和正常结束均尝试回收本次拥有的进程与临时 profile，用户指定的持久 profile 从不删除。证明与资源账归本地 provider，不进入通用 Environment 协议。

已确认本次 browser 主进程退出后，临时 profile 的删除对退出阶段的文件增删允许至多 5 次尝试，等待合计 0.75 秒；仅重试 ENOENT/ENOTEMPTY，目录已消失视为完成。其它 I/O 错误或持续变化仍抛出并保留资源账供 close 重试。该等待上限不声称能终止阻塞的系统 I/O，不扩展到持久 profile。

**启动者消失时 Chrome 随之退出。** 以前 Chrome 只在 `close()` 里被停掉；启动它的 Python 进程突然死掉（SIGKILL、测试框架杀掉挂住的测试、看门狗杀掉 run、强行中断）时，Chrome 被 PID 1 收养、一直跑下去，占着内存与调试端口（2026-10-23 本机有三个，分别跑了 13、14、31 小时，都用 af 的临时 profile）。现在 macOS 与 Linux 上 `local_chrome` 启动的 Chrome 活不过启动它的进程：

- **系绳**：每次启动多带 `--remote-debugging-pipe`。Chrome 从描述符 3 读 DevTools 消息，读到文件尾就像收到 SIGTERM 一样关闭。af 从不在这条管道上发消息，它只是系绳：写端只在启动它的进程里（`os.pipe()` 的描述符不可继承，别的子进程拿不到），这个进程不论怎样结束，内核都关掉写端，Chrome 随之退出、helper 跟着走。CDP 连接照旧走端口。Python 不能把描述符换个号交给子进程，所以启动命令是 `/bin/sh -c 'exec "$@" 3<&0 4>/dev/null 0</dev/null' sh <chrome> <开关…>`：shell 把系绳（它的 stdin）放到 3，Chrome 的回复（4）丢进 `/dev/null`，stdin 换成 `/dev/null`，再用 `exec` 把自己换成 Chrome。创建出来的进程就是 browser 进程，PID 相同，上面的所有权核验不变，shell 不与 Chrome 并存。shell 执行不了 Chrome 时退出码是 126 或 127（macOS 的 sh 对不存在的绝对路径也给 126，实测，所以两者不区分），`open()` 报"Chrome 可执行文件不存在或不能执行"；以前直接启动时是 `FileNotFoundError` / `PermissionError`。`metadata["command_line"]` 是 Chrome 自己运行的 argv，不含这层 shell；`--remote-debugging-pipe` 是启动开关表的固定一项（下面 `launch_flags`），`ignore_default_args` 管不到，与端口、profile 开关一样。
- **自己的进程组**：Chrome 以 `process_group=0` 启动，另成一个进程组，组号就是 browser 的 PID，helper 都在组里。`close()` 只给 browser 发 SIGTERM，让它像对用户一样关闭、把持久 profile 写完；`shutdown_timeout` 之后 SIGKILL；看到它退出之后再给整个组发 SIGKILL，收掉还没走的进程（helper 自己也会走，但 browser 被 SIGKILL 之后实测要 0.5–4 s；一个自己不退出的组员也收得掉，测试里用放进组里的 `sleep` 代替卡住的 helper）。组 SIGKILL 只在本 provider 刚看到 browser 退出时发：组里还有成员时，组号不会分给别的进程，信号只到 Chrome 自己的进程或谁也不到；在 `close()` 之前就已退出的 browser 不发（什么时候退出的不知道，组号可能已经是别人的），这与"不能按复用 PID 猜所有权"一致。组里只剩等待收尸的僵尸时，macOS 的 `killpg` 回 `EPERM`（实测），与 `ESRCH` 一样当作没有可杀的。另成进程组还意味着终端的 Ctrl-C 只到 af：由 af 的清理按顺序关 Chrome（录制收尾时 Chrome 还在），af 自己也死了的话由系绳负责。
- **代价**：不多一个常驻进程；每次启动多一次 `exec` 的 shell（几毫秒）与一对管道描述符，启动开关表多一项（以前录下的 `launch_flags` 与现在的不同，回放的一致性检查会拒绝，这些数据重录即可）。
- **实测**（2026-10-23，Chrome 155.0.8059.86，macOS，探针不入库）：同时带端口与 `--remote-debugging-pipe` 时两者都能用；系绳关闭后 Chrome 以退出码 0 退出，headless 默认开关约 1.2 s，`ignore_default_args` 约 7 s，headful 约 4 s，Chrome 还没启动完就关闭约 5.5 s；每次 helper 都在 browser 之后 0.1 s 内走完。没有系绳时，启动者被 SIGKILL 之后 10 s，browser 与 6 个 helper 都还在。Linux 没有在本机实测；Chrome 的管道处理在 POSIX 上是同一份代码。
- **残余**：启动者突然死掉时，临时 profile 留在原处（`$TMPDIR/chrome-profile-*`，macOS 在 `/var/folders/…/T/`，Linux 通常是 `/tmp`；实测一个 6–50 MB），因为没有进程活得比启动者久、在它死后删除；正常 `close()`、启动失败与取消照旧删除。要在突然死亡时也删，得每次启动多一个看守进程，没有做。启动者 fork 而不 exec 出的子进程（`multiprocessing` 的 fork 启动方式）会继承写端，Chrome 等它们也结束才退出；af 自己不这样 fork。Windows 上 `--remote-debugging-pipe` 传的是句柄而不是描述符 3/4，没有做：那里 Chrome 照旧直接启动、不另成进程组，启动者突然死掉时 Chrome 留下。

**外部端点是借用连接。** 显式 `cdp_url` 继续受支持；选了 `local_chrome` backend 不代表端点由它创建。没有本次 `ProviderSession` 或实验所有者的 context lease，就只释放 AF 的连接、scope、bridge 与订阅，不关整个浏览器、不杀外部进程、不删 profile、不批量关闭已有 tab。录制、pinned、archive replay 仍按 provider 能力与 setup 合同检查，不为任意外部端点伪造共享录制能力。

**同一实验进程内共享 Chrome。** `local_chrome.lifetime: run | experiment` 是资源寿命事实，默认 `run`；`experiment` 每 run 新建 BrowserContext，cookie、Web Storage、tab、权限设置与销毁均以该 context 为边界，各 run 有独立控制连接、recorder 参与者、archive channel、采集缓冲与 WAL。离线回放必须用 `run`。持久 profile 与 `experiment` 不能混用：新 context 不继承磁盘 profile 登录态，初态须显式导入 storage seed。任意外部 `cdp_url` 与实验管理模式不能同时选择。

`core/experiment.py::ExperimentScope` 是显式 `async with` 所有者，唯一的通用扩展合同是 `ExperimentParticipant.bind_experiment(scope, identity=...)`。`scope.bind(ComposedRun)` 遍历实际 `by_layer` 实例，同一对象只绑定一次；绑定只装配，不启动进程或连接网络。资源按 `(layer, instance_id)` 识别，同一 identity 配置不同明确拒绝，配置相同的 fresh composed runs 借用同一所有者。资源首次实际使用才懒启动。scope 退出逐资源、有界地清理，继续尝试其它资源，不覆盖原执行异常或取消；超时明确报告尚未完成并保留可重试所有权。没有全局缓存、按端点猜测的进程池或运行统计。

通用 `run` CLI 与 `BenchmarkRunner.run` 打开该 scope，单独 `run_task` 自己拥有一次 scope。Python 并发调用方显式创建 scope，将各个新 compose 的结果交给 `scope.bind`，分别驱动 Runtime，并在离开 scope 前等待所有 run 完成。通用驱动只消费实验生命周期合同，不 import browser；Runtime 不知道 Chrome。`config check/explain` 与 `prepare()` 始终不启动资源。现有 YAML backend 块设置 `lifetime: experiment` 即选择共享寿命，不新增 provider registry 或重复的实验配置。

Browser 的三个所有者分别是：provider 继续证明、持有并关闭实际进程/profile；`SharedBrowser` 持有 provider session 与一根 browser-wide early-attach broker，首次 acquire 固定 launch arguments/environment，后续不一致立即拒绝；`BrowserContextLease` 代表一个 run 的 context，结束 run 只释放 lease。实际启动 metadata 通过快照进入各 run 指纹，不因 capability 没有独占 `ProviderSession` 而丢掉启动开关摘要。`BrowserCapability.browser_context_id`、`BrowserArchiveContext.browser_context_id`、`RecorderTarget.browser_context_id` 是统一分区身份，不把它推入通用 Environment。

**浏览器连接合同。** `browser.commands` 保留 command-only 的 `BrowserCommands`，并承载可批量发送及订阅事件的 `BrowserConnection` 与 `PendingCommand`；后两者由原 recorder 接口移至真实的连接边界，录制与初态恢复共用，不再以具体 `CDPClient` 或独立/共享实现的 union 作为消费合同。helper 不因此取得关闭连接的所有权。

**共享录制的 early attach 只有一个所有者。** `RecorderBroker` 持有 browser-wide `Target.setAutoAttach(waitForDebuggerOnStart=true)`，按 target 的 `browserContextId` 和已证明的 parent-session 关系分发给本 run 的 `ContextRecorderClient`。slice 实现 `browser.commands.BrowserConnection`，复用 RecorderConnection 的 batch → resume → await、参与者和暂停请求处理。slice 的 browser-level auto-attach 开关只启停本 context 参与；关闭一个 run 不关闭根 socket，不改变其它 run 的录制。相同 target 多份 session 按原合同处理，不能为去重漏掉 SW 主脚本。主动创建新 tab 仍先 `about:blank`、等 ready、再导航。

这不是对任意外部 Chrome 的零干扰保证：CDP TargetFilter 只有 type，没有 context 过滤。未参与 context（包括默认 context）的新 target 仍可能短暂暂停，broker 立即 resume/detach，不配置、不归档、不关闭其页面。该行为属于显式实验所有权。Chrome 155 实测证明 `autoAttachRelated(page/tab)` 和发现后按 context 追赶漏 popup 首请求/首脚本及 SharedWorker 首个 fetch，不能冒充完整共享录制。进程崩溃影响全部共享 run，各 run 的记录健康与结算仍独立；既有嵌套 worker 主脚本等采集限制不因此消失。

控制连接与 recorder slice 共用 `BrowserContextAccess`，发送前验证 session/target/context，过滤 tab 查询与 browser-level 事件，context 定向 cookie/permission 操作，拒绝 run 的全浏览器关闭。它防止合作的 AF 扩展误跨 run，不声称沙箱化任意 Python。watchdog 复用原 CDP Protocol，不按 watchdog/domain 扩增 Protocol；同 tab/OOPIF 的 observation → JS scope 协作保留。

**浏览器内部 agent-cdp 合同。** AF 使用已发布的 agent-cdp 0.5.0 公共接口，不保留旧版兼容分支。scope 是调度范围：tab 观测调用同 tab JS scope、共享 watchdog 按绑定服务多个 tab、当前 tab 的 OOPIF 映射均合法；跨 tab 或失效 session 的元素/JS 在派发前拒绝。watchdog 按 `(scope, session_id)` 绑定与解绑，关闭一个 tab 仅清它自己的连接、bridge、状态；迟到事件不能复活已关闭 tab。session-bound bridge 只认信封路由，需父 session 的工厂显式 `with_session=True`。tab/JS scope 不保留事件历史，避免 DOM/截图被历史引用。

事件到方法的连接显式声明 handler/observer 参与与业务结果合同；动作要求实际执行者，日志、guard、expect 观察者不算执行者。`wait()` 只表示结算，动作成功还需 `raise_for_status()` 与 AF 结果校验；合法 `None` 必须由该动作合同明确允许，含 error 的结果明确失败。动作发起者退出等待时调用 `cancel_execution(include_running=False)` 撤销未开始的处理，普通等待者退出不撤销共享执行；已经开始的副作用不能声称回滚。关闭中一个资源失败也继续尝试其它资源，最后汇总清理失败。

**`__wiring__["backend"]` 字段类型**：Mode 1 字符串（`"browser_provider"`），表示"任意已注册到这个 registry 的 provider"。如果某个 capability 想限制 backend 子集，可改 Mode 2/3 的 tuple 形式（如 `("browser_provider", ["local_chrome", "skyvern"])`）——conscribe 把 backend 字段约束为对应的 discriminated union 子集。

**Backend 文件归属**：所有 browser provider 文件物理上属于 `spectragent/core/capabilities/browser/backends/`，与 `BrowserCapability` 同目录；`BrowserProviderRegistrar` 也在 `backends/_registrar.py`。这让"backend 是 capability 的从属配置"在文件结构上一目了然。其他 capability 的 backend 同理：`spectragent/core/capabilities/docker/backends/`、`spectragent/core/capabilities/desktop_vm/backends/` 等。

**Provider 能力声明（`__capabilities__`）**：每个 provider 类用 `__capabilities__: ClassVar[set[str]]` 静态声明自己支持什么，常量在 `backends/capabilities.py`。这是 provider 对"支持什么"的**静态**声明，与"这次 run 打不打开"无关；后者永远由 capability 上的策略决定（`replayability`、`network_source`），provider 上没有与策略并列的开关。capability 只读这个集合决定能不能把某件事交给它，从不按 `provider_id` 分支、也不探测 provider 的字段；provider 一侧的事实（profile 是否新建、哪些旋钮与开关矛盾）由常量承诺的 Protocol 方法自己报。M29 新增两个、M30 新增一个，v1 只有 `local_chrome` 声明：

| 常量 | 承诺 | 谁要求它 |
|---|---|---|
| `NETWORK_CAPTURE` | 浏览器进程经这个 provider 启动，独占时由 run teardown 关闭，共享时由显式 ExperimentScope 关闭；CDP 端点接受 browser 级录制客户端（`waitForDebuggerOnStart` 的 auto-attach、Network、Fetch）；provider 实现 `NetworkCaptureProvider`：`persistent_profile() -> str \| None`（profile 不是每 run 新建时，点名是哪个旋钮——只有配置能让 profile 不是新建的，从不靠环境变量，`config.json` 才看得出来；它决定 §5.6 的 `logged_in` 与指纹的 `fresh_profile`；声明了 `NETWORK_CAPTURE` 却答不出的 provider 被协作检查拒绝，从不当作新建的） | 任一 browser 归档器被绑定；`replayability` 不是 `none`；`network_source: archive`——也就是一切需要 recorder 连接的配置（§4.14） |
| `EXPERIMENT_CONTEXTS` | 实现 `SharedBrowserProvider`（`lifetime` 与只读 `managed_browser`），以 `ExperimentParticipant` 绑定实验所有者；每 run 借用独立 context lease，只有实验 scope 关闭进程。配置绑定纯装配、资源懒启动 | `local_chrome.lifetime: experiment`；共享执行与独立录制，离线回放不使用它 |
| `LAUNCH_ARGS` | `open(*, launch_args: Sequence[str] = (), launch_env: Mapping[str, str] \| None = None)` 接受本次 session 额外的 Chrome 开关与环境变量（加在浏览器从 provider 进程继承的环境上）；provider 实现 `LaunchArgsProvider`：`launch_arg_conflicts(launch_args) -> list[str]`（自己的哪些旋钮与这些开关矛盾，每条点名旋钮），`planned_launch_flags(launch_args, launch_env) -> list[str]`（M30：`open(...)` 将会写进 `metadata["launch_flags"]` 的那张表，环境变量写成 `NAME=value` 排在最前，启动之前就能算——回放的一致性检查据此在启动浏览器之前比较开关摘要），以及 `planned_browser_defaults(launch_env) -> BrowserDefaults`（它启动的浏览器会落回的默认时区与语言，从本机 OS 读，读不出的那项是 `None`，§4.14 "一致性检查"） | `replayability: pinned`（它的启动开关与 `TZ` 只能从这条路进浏览器）；`NETWORK_ISOLATION`（隔离开关也走这条路） |
| `NETWORK_ISOLATION` | provider 启动的浏览器能被切断网络：capability 经 `open(launch_args=...)` 交给它 `chrome_switches.NETWORK_ISOLATION_ARGS`（`--host-resolver-rules=MAP * ~NOTFOUND` 与 `--no-proxy-server` 两个 argv 元素，不带 shell 引号；没有任何例外，`localhost` 也断，§4.14 "回放"）；provider 实现 `NetworkIsolatingProvider`：`isolation_conflicts() -> list[str]`（自己哪些旋钮会让隔离失效，每条点名旋钮：`local_chrome` 是 `proxy_server`、`proxy_bypass_list`、`extra_args` 里任何一个网络路由开关（与某个隔离开关一字不差的重复除外：它什么都不改）（`chrome_switches.NETWORK_ROUTING_SWITCHES`：`--host-resolver-rules`、`--proxy-server`、`--no-proxy-server`、`--proxy-pac-url`、`--proxy-auto-detect`、`--proxy-bypass-list`）、不是 `about:blank` 的 `startup_url`——它在任何 recorder 连上之前就从命令行加载）。声明它的 provider 也声明 `LAUNCH_ARGS` | `network_source: archive` 且 `isolation: required`（默认；§4.14 "回放"） |

- `backends/base.py` 的约定本来就允许 `open()` 接收 per-session 的显式关键字参数；`launch_args` 与 `launch_env` 是其中两个。capability 只对声明了 `LAUNCH_ARGS` 的 provider 传它们；`none` 档位什么都不传，云 provider 的 `open()` 调用与以前完全一样。`local_chrome` 把 `launch_env` 加在 Chrome 从本进程继承的环境上（没有就原样继承），写进 `metadata["launch_env"]`。
- `local_chrome` 把 `launch_args` 接在 `extra_args` 之后，然后把同一个开关的多次出现合并成一个：所有 `--js-flags` 合成一个（V8 flags 以空格拼接），所有 `--disable-features` / `--enable-features` 各合成一个（逗号拼接、去重）。Chrome 对这几个开关只认最后一次出现，原先 `CHROME_DEFAULT_ARGS` 与 `disable_security` 各带一个 `--disable-features`，后者静默吃掉了前者。合并写在 `backends/chrome_switches.py`（`merge_chrome_switches`），归 Chrome 命令行这一层，不是某个 provider 的私有函数。
- `ProviderSession.metadata` 记录确切的命令行（`command_line`：Chrome 自己运行的 argv，不含把系绳放到描述符 3 的那层 shell，§4.9.x "启动者消失时"）与去掉易变值的开关表（`launch_flags`：最前面是启动设的环境变量（`TZ=UTC`），然后是开关，不含可执行文件与启动 URL，`--user-data-dir` 的路径与 `--remote-debugging-port` 的端口换成占位符；macOS 与 Linux 上总有 `--remote-debugging-pipe`）。环境指纹只记后者的摘要，而且去掉网络路由开关之后才算（M30：录制时 fixture 的 `MAP` 规则与回放时的隔离开关都不进摘要，隔离单独记，§4.14），命令行本身从不进 trace（`proxy_server` 可能带着凭证）。
- **后台 tab 的节流（`local_chrome.background_throttling`，默认 `false`）**：`CHROME_DEFAULT_ARGS` 里的 `--disable-background-timer-throttling`、`--disable-backgrounding-occluded-windows`、`--disable-renderer-backgrounding`（`CHROME_BACKGROUND_UNTHROTTLED_ARGS`）是第一个提交连同其余默认开关一起从 Puppeteer / browser-use 的自动化默认值拿来的（源码注释 "based on browser-use + Puppeteer best practices"，没有别的记录）。Puppeteer 带它们，是因为脚本驱动的页面可能不在前台（并行驱动几个 tab、窗口被别的窗口盖住），它的计时器与渲染不该因此变慢。af 只在焦点 tab 上动作与观测，焦点 tab 由 `Target.activateTarget` 提到前台，所以这三项只管焦点离开的那些 tab。`background_throttling: true` 不带这三项，Chrome 像对用户一样节流后台 tab；这三项由这个旋钮决定，`ignore_default_args` 管不到（它丢掉其余默认开关）。默认值不带旋钮时三项留在原来的位置，启动开关表与之前逐项相同。
  - **实测**（2026-10-23，Chrome 155.0.8059.86，headless，macOS，本机页面；探针不入库）：headless 里焦点离开的 tab 就是后台 tab：`document.visibilityState` 为 `hidden`，`document.hasFocus()` 为 false，`requestAnimationFrame` 不再跑（两种设置都一样）。10 ms 的 `setInterval`：焦点 tab 每秒 100 次（两种设置都一样）；离开的 tab 带这三项每秒约 10 次（在后台 5 分钟之后约 5 次），不带每秒约 1–2 次，在后台约一分钟之后每分钟只醒 1 次（与 Chrome 的 intensive wake-up throttling 同样的节奏；1 s 的链式 `setTimeout` 也从每分钟约 57 次降到 1 次，看了 7 分钟一直如此；带这三项时 7 分钟里一直是每分钟约 55 次）。渲染进程优先级：两种设置下离开的 tab 的渲染进程都从 47 降到 4（`ps` 的 `pri`，用一个忙循环 worker 认出是哪个进程），所以这台 macOS 上看不出 `--disable-renderer-backgrounding` 的作用。`--disable-backgrounding-occluded-windows` 只管 headful Chrome 的窗口被别的窗口盖住（Chromium 对这个开关的说明；headless 没有窗口，没有实测）：打开节流的 headful run，窗口被盖住时焦点 tab 也会被当作后台。
  - **它止不住 `storage` 事件的互相改写**：两个 tab 经 `localStorage` 互相改写、谁听到对方的副本谁就写一个更新的（永不收敛的平局规则），节流开不开每个 tab 都约每秒 310–335 次写——`storage` 事件不是计时器，节流管不到。iqiyi 也一样：只开节流时五个 iqiyi tab 照样失控，冻结离开的 tab 才止住（§4.14 "离开焦点的 tab" 的 iqiyi 实测）。
  - **默认 `false`，即保持之前的行为**：节流只放慢离开的 tab 的计时器，止不住已知的那种失控（上面与 §4.14），默认打开换不来已知的好处；焦点 tab 两种设置下一样，打开只改变 agent 回到一个 tab 之前它在后台做了多少事，以及 headful 时被盖住的窗口；改默认会改变所有发布的配置的页面行为。要像真实浏览器那样节流时显式打开。
  - **录制与回放**：三项在 `launch_flags` 里，录制与回放的启动开关摘要不同就拒绝（§4.14 "一致性检查与回放指纹"），所以不另加指纹项。云 provider 不接受启动开关，没有这个旋钮。
- 显式 `cdp_url`、测试注入的控制客户端，都意味着浏览器不是 capability 拥有的：与任何需要 `NETWORK_CAPTURE` 的配置同时出现时报错，点名双方：`prepare()` 里（`check_setup`；`cdp_url` 来自配置或任务上下文）就报，setup 再查一遍（`prepare()` 之后才注入的客户端由它报）。

**6 个具体 Capability（全部平级，无层级）**：

| capability_id | 协议 | 核心 Actions | Observation Fragments | 典型 Benchmark |
|--------------|------|-------------|----------------------|---------------|
| `docker` | Docker API | `bash(cmd)`, `read/write_file()`, `snapshot/restore()` | `terminal`, `filesystem` | SWE-bench, Terminal-Bench |
| `browser` | agent-cdp (CDP) | `navigate()`, `click()`, `type()`, `scroll()`, `extract()`, `done()` | `dom`, `screenshot`, `console_log` | WebArena, BrowseComp, Mind2Web |
| `desktop_vm` | VNC + AT-SPI / PyWinAuto | `screenshot()`, `click(x,y)`, `keyboard()`, `scroll()`, `get_a11y_tree()` | `desktop_screenshot`, `a11y_tree` | OSWorld, macOSWorld |
| `android` | ADB + gRPC | `tap()`, `swipe()`, `screenshot()`, `get_ui_tree()` | `mobile_screenshot`, `ui_tree` | AndroidWorld, MobileWorld |
| `gpu_profiling` | Docker(no-GPU) + bare-metal harness IPC | `gpu_run()`, `gpu_compile()`, `profile_kernel()`, `profile_system()` | `gpu_metrics` | KernelBench, TritonBench |
| `tool_sandbox` | HTTP / in-process | `call_tool(name, params)`, `get_user_msg()`, `get_policy()` | `tool_result` | τ-bench, ToolBench, AppWorld |

**Capability 通过 `__wiring__` 声明 observation provides**：

```python
class BrowserCapability(CapabilityBase):
    __registry_key__ = "browser"

    __wiring__ = {
        "observation": ("observation", ["dom", "screenshot", "browser_meta"]),
        # 对称 observation wiring：action provides 声明此 capability 能提供哪些动作
        "action": ("action", ["click", "click_coordinate", "type", "navigate",
                              "scroll", "send_keys", "go_back", "go_forward",
                              "switch_tab", "select_dropdown", "extract_content",
                              "done"]),
        # 归档 provides（全限定键，M29 起）：没有 slot；requires 在 trace hook 的 archive 列表里
        "archiver": ("archiver", ["browser.network", "browser.storage", "browser.dom_raw",
                                  "browser.frames", "browser.replay_log"]),
        # 回放档位受体（M29，§4.14）：策略即开关，none 是默认；YAML 必须写一个
        "replayability": ("replayability", ["none", "pinned"]),
        "backend": "browser_provider",
    }

    # 持有一组实例化的 action —— 从 YAML 注入或 __post_init__ 派生默认全集
    actions: list[ActionBase] = field(default_factory=list)
    _actions_by_name: dict[str, ActionBase] = field(init=False, repr=False)

    def __post_init__(self):
        if not self.actions:
            provided = self.__wiring__["action"][1]
            self.actions = [ActionRegistrar.get(name)() for name in provided]
        self._actions_by_name = {a.action_id: a for a in self.actions}

    def get_action_descriptors(self, **context) -> list[ActionDescriptor]:
        # 遍历 self.actions 派生 descriptor，不再是独立的静态 list
        return [a.descriptor() for a in self.actions]

    async def execute_action(self, action, **kwargs) -> ActionResult:
        # 委托给注册的 action 实例，没有 dict 查表 / if-else
        impl = self._actions_by_name.get(action.name)
        if impl is None:
            return ActionResult(
                success=False, error=f"Unknown action: {action.name}",
            )
        sid = kwargs.get("session_id") or self._default_session_id
        self._cycle_cache.pop(sid, None)  # 失效 observe 缓存
        return await impl.execute(
            action.params, capability=self, session_id=sid,
        )

    async def resolve_fragment(self, frag_key, **kwargs) -> ObservationFragment:
        # 委托给对应 Watchdog（见 4.4 节 Resolver 模式）
        ...


class DockerCapability(CapabilityBase):
    __registry_key__ = "docker"

    __wiring__ = {
        "observation": ("observation", ["terminal", "filesystem"]),
        "action": ("action", ["bash", "edit_file", "submit"]),
        "backend": "docker_backend",
    }

    actions: list[ActionBase] = field(default_factory=list)
    # __post_init__ / get_action_descriptors / execute_action 与 BrowserCapability
    # 完全同构 —— action 类本身在 spectragent/core/capabilities/docker/actions/
    # 里独立注册。


class ToolSandboxCapability(CapabilityBase):
    __registry_key__ = "tool_sandbox"

    __wiring__ = {
        "observation": ("observation", ["tool_result"]),
        "action": ("action", ["call_tool"]),   # 静态入口
    }

    async def setup(self, *, negotiation: Any = None,
                    task_tools: list[dict] | None = None) -> None:
        # task_tools 来自 TaskLoader.get_task_context()，按 setup() 签名里的名字路由到这里（§4.3），
        # 每个 task 不同。设计中（M23 未实现）；已实现的同类先例是 browser 的 start_url
        self._task_tools = {t["name"]: t for t in task_tools or []}

    def get_action_descriptors(self, **context) -> list[ActionDescriptor]:
        # Tool-sandbox 的动态 tool 通过 CallToolAction 的 descriptor 暴露
        # （name 仍然是 "call_tool"，但 descriptor.params_schema 根据 task 变化）
        return [a.descriptor(task_tools=self._task_tools) for a in self.actions]
```

**Benchmark = capabilities[] — 声明式组合**：

```python
BENCHMARK_CAPABILITIES = {
    "swe_bench":        ["docker"],
    "kernelbench":      ["docker", "gpu_profiling"],
    "mle_bench":        ["docker", "gpu_profiling"],
    "webarena":         ["browser"],
    "browsecomp":       ["browser"],
    "osworld":          ["desktop_vm"],
    "androidworld":     ["android"],
    "tau_bench":        ["tool_sandbox"],
    "gaia":             ["browser", "tool_sandbox"],
    "theagentcompany":  ["desktop_vm", "browser", "tool_sandbox"],
}
```

**Cross-cutting concerns 在 framework 层处理，不在 capability 内**：

- **日志/trajectory 记录** — Hook（TraceWriter）自动记录每步事件
- **环境证据的落盘** — 归档消费者（TraceWriter）在自己的提交点拉取、写进 `env/`（§4.6、§5）
- **超时/资源控制** — AgentRuntime 对 loop.run() 加 timeout
- **评分** — AgentRuntime 按 benchmark 注册的 evaluator 执行

Capability 本身不需要知道日志、超时、评分的存在。环境证据是唯一的分工：**被拉取时产出**环境证据是 capability 的职责（只有它够得着自己的世界），**持久化**不是：capability 不知道 WAL，不知道步号，也不知道 `env/` 的布局。

**GPU Profiling Capability — DockerCapability 的插件**：

CUDA bench = Code bench + GPU 能力。GPUProfilingCapability 不自己管 Docker——它和 DockerCapability **共用同一个容器**。Agent 在 Docker 里写代码（通过 DockerCapability 的 bash/edit_file），然后调用 GPU 命令（通过 GPUProfilingCapability）。

```
KernelBench 配置: capabilities = [docker, gpu_profiling]
                                   │              │
                                   │              └── gpu_run() 执行流程：
                                   │                  1. 从共享 Docker 容器中取出代码
                                   │                  2. 在裸机 GPU 上编译/运行/profiling
                                   │                  3. 返回结果（JSON 指标 / stdout）
                                   │
                                   └── Agent 在这里写代码（bash, edit_file）

GPUProfilingCapability
├── 不管 Docker 生命周期（DockerCapability 管）
├── 知道怎么从容器里拿文件
├── Actions:
│   ├── gpu_compile()       → 取代码 → 裸机编译
│   ├── gpu_run()           → 取代码 → 裸机 GPU 执行 → 返回 stdout
│   ├── profile_system()    → 取代码 → nsys 分析 → 解析 .nsys-rep → JSON
│   ├── profile_kernel()    → 取代码 → ncu 分析 → 解析 .ncu-rep → JSON
│   └── gpu_verify()        → 取代码 → correctness check
│
└── Bare-metal layer (Harness daemon)
    ├── CUDA toolkit + nsys + ncu
    ├── GPU 独占调度（profiling 期间无干扰）
    └── 每 task 后 GPU reset
```

### 4.10 Data Loader 抽象

```
BaseTaskLoader (Protocol)           BaseEvalLoader (Protocol)
    ├── benchmark_name: str             ├── benchmark_name: str
    ├── env_type: str                   ├── task_ids() → list[str]
    ├── task_ids() → list[str]          └── __getitem__(id) → EvalReference
    ├── __getitem__(id) → TaskDefinition
    └── get_task_context(id) → TaskContext{env, loop, branch}   ← 任务特定上下文（M31 拆成三部分）
```

**去掉 "Browser" 前缀**，支持所有环境类型的 benchmark。

**任务上下文的词表**。`get_task_context(id).env` 的键交给 `AgentRuntime.execute(env_context=...)` → `setup_environment`，按 capability `setup()` 签名里的名字路由（§4.3）；谁都不接受的键在 `prepare()` 就报错（`check_setup`，§4.3），不打开 trace。现有的键：

| 键 | 谁接受 | 含义 |
|---|---|---|
| `start_url` | browser | 任务的落地页，agent 第一次观测之前打开（`TaskSetup.to_context()`；`spectragent run --start-url`）。必须是一个完整的 `http://` 或 `https://` URL：没有协议头、协议头不是这两个、没有主机、含空白的，在 `prepare()` 报错（`check_setup`，§4.9），点名这个起始页，不打开 trace。数据写成别的形状的，由读它的加载器在 trace 记下它之前补全（下面"起始页"） |
| `storage_state` | browser | `BrowserStorageState` 或其 JSON 路径；`BrowserSetup` 建模，`Environment.prepare_setup_context` 先按实际 owner 路由，再由 browser 的 `prepare_setup_context` 读取并冻结为 typed 文档（相对启动目录、`~` 展开），setup 再冻结 canonical bytes。只接受 owned fresh context；显式外部 CDP、注入 control、持久 profile 与 seed 组合均在 prepare 拒绝。cookies 是 context 级，local 以真实 storageKey 为键，session 以 logical tab + storageKey 为键；首请求、首个业务脚本前生效（§4.14） |
| `risk_control`、`risk_control_types` | browser | 现有词表；接受、记 WARNING、还不处理 |
| `cdp_url` | browser | 显式 CDP 端点（per-run override；与需要 recorder 连接的配置互斥，§4.14） |
| `replay_archive` | browser（M30） | 回放的**源 run 目录**（一个 trace 目录，含 `manifest.json`）。`network_source` 拦截（`archive`）时必须给、不拦截（`live`）时不许给，都在 `prepare()` 报错（`check_setup`）、点名双方。capability 经 `core/archive` 的 `open_archive` 打开它，只读已提交的部分（§4.14 "回放"）；`spectragent run --replay-archive DIR`。相对路径可以写，意思是相对启动目录（af 从不切换工作目录，所以就是当前目录；`~` 先展开）：`AgentRuntime.prepare` 经 Environment 路由后，由实际 browser owner 的 `prepare_setup_context` 把它解析成绝对路径，execute 复用同一 execution / recordable 结果做检查、记录和 setup（§4.3；输入文件在这一次准备中读取，不重复读取），对每个调用方都一样：CLI、任务加载器、自己组 `env_context` 的 Python 调用方；按 capability id 挂的那一块（`{"browser": {...}}`）里的也一样。trace 因此记下 setup 打开的那个目录（用户 2026-10-13 的裁决） |
| `task_tools` | tool_sandbox（设计中，M23） | 每个 task 的工具定义（§13.5） |

context 在实际 capability owner 准备出的 `recordable` projection 上按值级规则脱敏记进 `TraceConfig.task_setup`（§5.6）；初态唯一恢复权威是 `browser.storage` 的 `initial_state.json`，指纹始终声明初态是否非空、digest 与覆盖范围，未选 storage 归档也不冒充空初态，所以 `replay_archive` 是路径字符串，不是打开的对象；分叉任务（M31）从源 trace 里重建环境靠的就是这份记录。上面的键都属于 `TaskContext.env`，路由不变（下一段）。

**登录的两条路径。** 预置登录态沿上面的 `storage_state` 输入，或在 `none` 档位显式选择 provider 的持久 profile；需要操作网页登录时，把账号、密码和登录步骤放进现有 `task_text`，Agent 用自己的 prompt 和既有 type / click 动作执行。框架不自动登录，不新增 credential store、加密机制或 `TaskContext.agent` / `agent_inputs` 通道。`BrowserSetup.login_required` / `login_type` 只保留 benchmark 元数据，不路由 setup、不触发自动登录；无实现的 `credentials_ref` 现在明确拒绝，旧 `LoginSnapshot` 删除，不能再拿它声称保存了分区登录态。

**起始页：加载器补全，浏览器只认完整的 URL（用户 2026-10-14 的裁决）。** 2026-10-14 零 token 筛选 LexBench L1 的中文站点（本地的 `tmp/flywheel/followup/live2/screening.md`）发现：`lexbench_browser` 的九个 split（包括它自己那份 Online-Mind2Web）没有一个起始页带协议头，几乎都是裸主机名（`www.bilibili.com`，少数带路径：`s.weibo.com/top/summary`）；全集有 10 个任务写的是文字或几个站点（L1 的 7 个与 L2 的 3 个，例如 `www.xiachufang.com或其他美食网站`、`jd.com + tmall.com + suning.com`）。加载器原样交出、setup 原样导航，SecurityWatchdog 把它当作"不安全的协议"拦下，每个 LexBench 任务都在 setup 以 `agent_done: error` 结束、一次模型调用都没有，sweep 的 pre-flight 也看不出来。规则：

- **补全**：`lexbench_browser` 任务加载器（`core/data_loader/task/lexbench_browser.py::complete_start_page`，在 `normalize_raw` 里，新旧两种记录形状都经过它）把一个**裸主机名**补成 `https://…`：由 ASCII 字母、数字、连字符组成、至少有一个点的主机名，可带端口，后面可带以 `/`、`?`、`#` 开头的部分，整串不含空白、全是 ASCII。别的一律原样：已经带协议头的、文字、几个站点。
- **为什么在加载器、在 trace 记下它之前**：trace 记下的 `task_setup.context.start_url` 就是交给 setup 的那一个，而飞轮报告的站点、导出的 `split.by: site` 与 `exclude_sites`、去污染的 `site_instruction` 都按 `urlsplit(start_url).hostname` 认站点（`core/trace/sources.py::TaskIdentity.site`、`export/split.py`）。裸主机名取不出主机名：只在浏览器里补，每个 run 仍会被算成"没有 start_url"，站点数是 0。
- **为什么只在 `lexbench_browser`，不在 `TaskLoaderBase`**：别的数据都写完整的 URL（`online_mind2web` 的 300 个任务全是 `https://`，`Hard` 的 77 个在其中；`browsecomp` 没有起始页；`trace_branches` 用源 run 记下的那一份）。裸主机名是 LexBench 数据自己的写法（它上游的字段叫 `target_website`，是网站，不是 URL）；放进共用的基类，就会把一份本该写 URL 的数据里的错误悄悄改掉，而那种错误应该由下一条点名。
- **补不全的在 `prepare()` 被拒**：`BrowserCapability.check_setup`（§4.9 `SetupCheck`，§4.14 "协作检查一览"）拒绝不是一个 http(s) URL 的起始页，消息点名这个起始页和原因（没有协议头、协议头是别的、没有主机、含空白、不是合法的 URL），trace 不打开。sweep 的第一个任务是这样时 pre-flight 停下（退出码 2）；否则只在那个任务上报（`TaskOutcome.error`）。`spectragent run --start-url` 不补全：只写主机名，`--dry-run` 就报错。setup 自己再查一遍（同一个 `check_setup`），绕过 `prepare()` 直接 setup 的调用方也在启动浏览器之前被拒。
- 智能体自己发出的 `browser.navigate` 不经过这条检查；一个没有协议头的 URL 由 SecurityWatchdog 拦下，消息说缺协议头（`Navigation BLOCKED: missing scheme in …`），被禁止的协议头（`javascript:`、`file:` 等）仍是 `unsafe scheme`。profile 不装 SecurityWatchdog 时（`vision_only`），Chrome 自己拒绝这样的 URL，导航同样失败（§4.14 "`browser.navigate`"）。

**`TaskContext`：一个任务交给三个不同的消费者的三样东西（M31）**（`spectragent/schemas/task.py`）：

```python
class TaskContext(BaseModel):
    env: dict[str, Any] = {}                # → execute(env_context=...)：上表的键，按 setup() 签名路由
    loop: dict[str, Any] = {}               # → execute(loop_inputs=...)：Loop 声明的输入（__loop_inputs__，§4.1）
    branch: BranchProvenance | None = None  # → execute(branch=...)：分叉来源，只进 trace（TraceConfig.branch，§5.3）
```

- `TaskLoaderBase.get_task_context(id)` 返回 `TaskContext(env=task.setup.to_context())`，任务不存在时返回空的 `TaskContext()`；`BaseTaskLoader` Protocol 的返回类型同步改成 `TaskContext`，不留返回 dict 的旧形状。
- `BenchmarkRunner.run_task` 把三部分分别交给 `AgentRuntime.execute`（§4.2）；开跑之前 `BenchmarkRunner.preflight` 用第一个任务的 loop 与 env 部分调一次 `prepare()`（§4.2 "sweep 先过一次 `prepare()`"）。`spectragent run` 只组 env 部分（`--start-url`、`--replay-archive`），`--dry-run` 也把它交给 `prepare()`。
- loop 部分的词表：`checkpoint`（`Checkpoint{trace_dir: 源 run 目录的绝对路径, step: t, task_variant: 变体 id | null}`）→ `BranchLoop`。交给不声明它的 Loop 在 `prepare()` 报 `IncompatibleLoopInputError`。`task_variant` 不为空表示这个分叉在任务变体下决策（下面 `task_variants`），`BranchLoop` 据此拒绝 `history_source: source`（§4.1）。
- branch 部分：`BranchProvenance{source_run_id, source_trace（源 run 目录相对 sources 根的 POSIX 路径）, source_benchmark, source_subset, source_task_id, checkpoint_step, branch_index, branch_group_id, task_variant}`（`spectragent/schemas/restoration.py`），按任务变化，不进配置摘要。`task_variant` 是 `BranchTaskVariant{variant_id, task_text, source_task_text, note}`，只有任务变体的分叉才有；没有时不写进 `config.json`。

**分叉任务：`trace_branches` 加载器**（`core/data_loader/task/trace_branches.py`，`benchmark_name: trace_branches`）把已录制的 run 展开成分叉任务。它读源 trace 只经 `RunTrace`，只读已提交的部分。

- **旋钮**：`data_dir`（sources 的根目录，默认 `traces`）；`sources: list[str]`（相对根的 glob，展开后按路径排序；必须相对根、不含 `..`，构造时检查；一个匹配到的路径不是目录或没有 `manifest.json` 就不是 run，忽略）；`step_selector` 受体（`StepSelectorRegistrar`，`selector_id`：`explicit{steps}` / `every_k{k, offset}` / `all` / `last`，`core/data_loader/task/step_selector/`）；`branches_per_step`（≥ 1）；`agent_outcome`（`any` 默认 / `done` / `max_steps` / `error` / `timeout` / `vetoed`：源 run 的 agent 怎么结束，读 `result.json` 的 `agent_done`，只在设了时读；没有 `result.json` 的源过不了设了值的筛选）；`require_eval`（`RequireEval{evaluator_id, label: 1, slug: null}`，pydantic dataclass，在 `core/data_loader/task/require_eval.py`：至少一份该评测器的 `verdict_source == "graded"` 的判定，其 `predicted_label == label`，给了 `slug` 时只看那一份；经 `RunTrace.evals()` 读，§5.5，不读 `result.json`）；`require_archive`（默认 `["browser.network"]`，`[]` 不要求任何归档：源 run 绑定了这些归档器、且它们的 capability 有已提交的 run 记录即环境指纹）；`on_unbranchable: raise | skip`（默认 `raise`）；`task_variants`（任务变体文件的路径，`~` 展开、相对启动目录，默认 `null`：每个分叉沿用源任务；见下面"任务变体"）；`exclude_runs`（排除文件的路径，`~` 展开、相对启动目录，默认 `null`：不排除任何源；见下面"排除的录制"）；`exclude_unloaded_checkpoints`（默认 `true`：留下初次加载没完成就采集的检查点之外的；见下面"没加载完的检查点"）；`subset` 只是标签。`sources` 与 `require_archive` 在 Python 使用不可变元组默认参数，conscribe 2.0 将真实的数组默认值写入生成配置；YAML 中省略采用默认值，`[]` 表示空，`null` 不被接受。
- **两本账（用户 2026-10-13 的裁决）**：trace 自己那本账是**硬条件**，不是筛选、关不掉：检查点永远是源 trace 已关闭、而且完整到的那一步。`writing` 的 trace（还在写，或进程死在收尾之前）没有可分叉的步（记在 `loader.skipped`，原因 `no branchable step: its trace was never closed ...`）；`incomplete` 的 trace 只有 `complete_through` 之前（含）的步是候选，所以一个在第 t 步记录出洞的 run 仍然贡献 0..t-1 的每个检查点，一个被限流打断的 30 步 run 前 25 个检查点照样能用，尾部失败（收尾收集、终止观测）的 run 一个检查点也不少。agent 那本账是**筛选**：`agent_outcome`，默认 `any`。
- **筛选与前提**：`exclude_runs` / `agent_outcome` / `require_eval` 是筛选，不符的源跳过（记 INFO，也记在 `loader.skipped`，原因以 `filtered:` 开头；筛掉的源从不触发抛错）。前提不满足的源按 `on_unbranchable` 抛错或跳过，原因都记在 `loader.skipped`（源路径 → 原因）：读不回来的 trace（manifest、配置、步记录、`result.json`（`agent_outcome` 设了时）或 `evals/` 坏了；比当前格式旧的 trace 也在此列，`RunTrace` 只读 format 5，§5.5）；源 run 本身是分叉 run（`config.branch` 不为空）或回放 run（`task_setup` 里有 `replay_archive`）；`require_archive` 不满足；环境 setup context 拿不到（见下）；源 run 的 `run_id` 与另一个通过筛选的源重复（共享这个 `run_id` 的每个源都不可分叉，因为它们的任务 id 会撞）。`raise` 一次列出全部不可分叉的源（一个 `BenchmarkDataError`），这次扫描不发布任何任务；`skip` 记 WARNING。
- **环境 setup context**：取源 `TraceConfig.task_setup.context`（M28 起记录）；没有这份记录、或它的脱敏台账不空（值被换成了 `<redacted>`，照它重建的环境不是源环境）时，按源 run 的 `(benchmark, subset, task_id)` 用源 benchmark 的 TaskLoader 重新解析（一次扫描里每个 `(benchmark, subset)` 只建一个加载器）；都不行就是不可分叉。分叉任务的 env 部分 = 这份 context，加上 `replay_archive = 源 run 目录的绝对路径`。
- **选步**：候选是已提交的、`origin == "policy"` 的步 t，要求 0..t 每一步都已提交、trace 完整到第 t 步（`t ≤ complete_through`）、第 t 步的观测已记录（`error_phase != "observe"`）；`step_selector` 在候选里选，候选之外的步（如 `explicit` 写了超出某个源长度的步）跳过并记原因，键是 `<源路径>#s<t>`（没提交、前缀有缺口、超出 trace 完整到的那一步、是恢复或终止步、观测没记录）。选步策略是纯函数：`all` 返回全部候选；`every_k` 按**步号**数（`offset`、`offset + k`……里是候选的那些），不按候选里的位置，所以一个源缺了第 4 步时 `k: 2` 仍然选 0、2、6；`explicit` 原样返回写了的每一步（至少一步，构造时检查，因为生成的配置不带 `min_length`），由加载器判断哪些是候选；`last`（M33）只返回最后一个候选，每个源 run 恰好一步，长短不一的源也一样（`explicit` 写不出"每个源的最后一步"），度量闸门的原路径回放段用它（§4.16）。一个源一个候选都没有时记在 `loader.skipped`，不抛错。
- **展开**：每个选中的 t 展开 `branches_per_step` 个任务，任务 id `<source_run_id>/s<t>/b<i>`，`branch_group_id = <source_run_id>/s<t>`（任务 id 去掉 `/b<i>`：输入完全相同的那组分叉）；`TaskDefinition.task_text` 是源 run 的任务文本，`env_type` 是源 run 的，`difficulty` / `domain` / `tags` 取自源 `TraceConfig.extra`（结果照样按它们切片），`setup` 在 env 类型有对应 setup 变体时照 env 部分写一份（只是描述；跑的始终是 `get_task_context` 的 env 部分）；`get_task_context` 给出 env（上面那份）、loop（`checkpoint`）与 branch（`BranchProvenance`），每次给一份副本。
- **任务变体："同一页面，不同需求"（2026-10-21）**：前瞻观测的训练数据要同一屏配不同的需求，选区模型才学会看任务、不只看图。`task_variants` 指向一个 JSON 文件（由别处、通常是 LLM 生成）：`{"format": "task-variants-1", "variants": [{"source_run_id", "step", "variant_id", "task_text", "note"?}]}`，`source_run_id` 是加载器给源 run 用的那个 run id。设了它，**文件决定检查点**：一条变体的 `step` 必须是这个源的候选、且是 `step_selector` 选中的（`all` 时文件完全决定），否则这条变体跳过并记原因；每条可用的变体展开 `branches_per_step` 个任务，任务 id `<source_run_id>/s<t>/v<variant_id>/b<i>`，`branch_group_id = <source_run_id>/s<t>/v<variant_id>`（同一变体的几个分叉输入相同；同一检查点的不同变体不是一组，重复分叉率与导出的 `duplicate_of` 都不拿它们互比），`TaskDefinition.task_text` 是变体文本，`checkpoint` 带 `task_variant`，provenance 带 `BranchTaskVariant`（变体 id、变体文本、源任务文本、`note`，写进 trace 的 `config.json` 的 `branch`）；env 部分（setup context、`replay_archive`）、loop 的其余部分、`env_type` 与切片字段都是源的。文件按格式严格读（`core/data_loader/task/task_variants.py`）：读不出、不是 JSON、`format` 不是 `task-variants-1`、缺字段、多字段、类型不对（`step` 必须是整数）、`variant_id` 不是一个由字母数字与 `. _ -` 组成的非空串（它进任务 id），都在第一次扫描时报 `BenchmarkDataError`，点名文件与问题，不发布任何任务。文件内容按条判断，不能用的跳过、记在 `loader.skipped`，键是 `<source_run_id>/s<t>/v<variant_id>`：同一 `(source_run_id, step, variant_id)` 列了不止一次（每一份都跳过，不知道该用哪一份）、`task_text` 为空、没有一个可读的源有这个 run id、它的源被筛掉或不可分叉（原因带上源自己的那条）、这一步不是候选（与 `explicit` 同样的原因）或选步策略没选它。变体从不触发 `on_unbranchable` 的抛错；文件里没提到的候选步不产生任务，也不记原因。变体分叉要求 `history_source: restored`（§4.1，source 历史在组装期被拒）。变体没有 benchmark 参考：独立评测不拿源任务的参考给它评分，按参考评分的 evaluator 把它记为评测失败，只有不读参考的 judge（WebJudge）用变体文本评它（§4.11）。导出时它的任务身份仍是源任务的（benchmark、task id、站点都是源的，切分随源任务），`TaskIdentity` 另记 `source_task`（源任务文本），`site_instruction` 去污染同时比对变体文本与源任务文本：变体文本是新的，但它的画面录自源任务，源任务是评测任务时变体一并排除（`core/trace/sources.py::TaskIdentity.instructions`，`export/split.py`，§4.15）。
- **排除的录制（2026-10-23）**：有的录制不能当数据用：页面一直没渲染出来（整段只有骨架屏，agent 一直在等、刷新），或录制时内容请求就失败了，录下的是一个残缺的页面；从它们分叉，检查点恢复得"很成功"（画面本来就不变）却毫无意义，还污染分叉评判与标注队列。判断由别处做（通常是对录制的离线体检，例如仓库顶层 `lookahead/` 的 `health` 命令），加载器只执行：`exclude_runs` 指向一个 JSON 文件 `{"format": "run-exclusions-1", "runs": [{"run_id", "reason", "note"?}]}`，`run_id` 是加载器给源 run 用的那个 run id。文件列出的源被筛掉，`loader.skipped` 里的原因是 `filtered: the exclude_runs file lists its run id: <reason>`（同一个 run id 列了几次，原因依次用 `; ` 连起来）；被排除的源的任务变体照"任务变体"的规则跳过并带上这条原因；列了但没有一个源有的 run id 不起作用，也不记。文件按格式严格读（`core/data_loader/task/run_exclusions.py`）：读不出、不是 JSON、`format` 不是 `run-exclusions-1`、缺字段、多字段、类型不对、`run_id` 或 `reason` 为空，都在第一次扫描时报 `BenchmarkDataError`，点名文件与问题，不发布任何任务。
- **没加载完的检查点（用户 2026-10-25 的裁决，§4.4 “初次加载是否完成”）**：`exclude_unloaded_checkpoints: true`（默认；发布的飞轮配置显式写出）时，选步之后对每个选中的候选（有任务变体时，文件点名且被选中的那一步也一样）读它录下的观测：`capture_state` 的 `settle_state.loaded` 是 `false`——采集时初次加载还没完成，浏览器上就是主文档还没到它的 `load` 事件，不论这次等待怎么结束——这一步就不当检查点。它记在 `loader.skipped`，键与其他不用的步相同（`<源路径>#s<t>`；变体是 `<source_run_id>/s<t>/v<variant_id>`），原因写明这一步在初次加载完成之前采集、附录下的等待结果与时长、点名 `exclude_unloaded_checkpoints`，并记 INFO。只是 DOM 没静下来的步照常是检查点。它作用在选步之后：`last` 选中的最后一步没加载完就不分叉这个源，不往前补一步；`every_k` 不因此重新数。没有证据就保留检查点、不拒绝：源 run 没协商 `capture_state`、这一步没录它、它的采集失败了、或者等待关着。读录下的 fragment 失败（`TraceSchemaError`）是这个源不可分叉，按 `on_unbranchable` 处理。`false` 时不读。源 trace 不变；判定经 `core/trace/sources.py::unloaded_capture_reason`，与导出的 `filters.exclude_unloaded_steps` 同一条规则（§4.15）。
- **惰性加载**：构造时不读盘（`task_variants` 与 `exclude_runs` 文件也一样），第一次 `task_ids()` / `__getitem__` / `get_task_context` 时才读这两个文件、扫描源，所以组装配置（每个任务都会重组一次）不做 I/O。一个源都没匹配上时任务列表为空并记 WARNING，点名根目录与 glob。
- **受体写法**：`TaskLoaderBase` 在 `__post_init__` 里加载、而受体在构造之后才注入，所以这个加载器不是 dataclass：手写 `__init__`（接受继承来的 `subset` / `data_dir` 与上面的旋钮），`step_selector` 是类级注解的单实例受体加 Python 侧默认（`all`），与 `ArchiveNetworkSource.miss_policy` 同一写法。
- **v1 没有 `trace_branches` 的评测数据加载器**：`spectragent bench` 跑分叉 sweep 时不解析评测加载器（没配置评测器、或给了 `--no-eval`，就从不查 `EvalLoaderRegistrar`）；配置了评测器而没有 `eval_loader:` 块时就在开跑前报错，点名这个 benchmark。这条规则对任何没注册评测加载器的 benchmark 都成立，不是 `trace_branches` 的特例（分叉结果怎么变成训练信号，M32 之前定）。

### 4.11 Evaluator 抽象

```python
EvaluatorRegistrar = create_registrar("evaluator", BaseEvaluator, discriminator_field="evaluator_id")

class BaseEvaluator(Protocol):
    @property
    def evaluator_id(self) -> str: ...
    def config_signature(self) -> dict[str, Any]: ...
    @property
    def verdict_slug(self) -> str: ...
    async def evaluate(self, trace: RunTrace, reference: Any, **kwargs) -> EvalResult: ...

# 可选的类级声明（evaluator/base.py::needs_reference 读它，没写就是 True）：
#     __needs_reference__: ClassVar[bool] = False
# 判定只依据 trace 记下的任务文本与证据、从不读参考行；只在任务没有参考时（任务变体的分叉）起作用。
```

Evaluator 的输入是**完整的 RunTrace**（不是 AgentResult），因此可以访问任意深度的运行数据。
每个 evaluator（包括仅结构化满足 Protocol 的实现）公开 `config_signature()` 与稳定的 `verdict_slug`：无旋钮时签名显式返回 `{}`，有旋钮时写出影响判定的可复现设置，slug 随之变化。Runner 不猜测缺失身份，也不把读取签名的异常吞掉后记成空签名。落盘的 `EvalResult` 只接受当前 `schema_version: "2.1"`；构造新判定时 `verdict_source` 默认 `graded`，读回已存判定时该字段与 `evaluator_config` 必须实际存在，旧数据不能被默认值伪装成已评分。

**现状：独立评测阶段可启用 judge，与数据飞轮的回放保真判定解耦（用户 2026-10-14 的裁决，取代 2026-10-13 的休眠约定）。** judge 只读取已提交的轨迹和明确提供的任务评测材料，在独立阶段判断任务正确性；它的模型、判定依据和运行失败必须可追溯，报告单独展示任务正确率与有效判定覆盖率，不把模型错误、证据不足或未评测算成任务失败，也不把 agent 的 `done` 当作答对。恢复与分叉阶段不调用 judge、不用 judge 覆盖恢复校验的结论。时效任务的判定以录制时点与轨迹中的证据为准，不拿当前网站内容或模型的当前知识替代录制事实。飞轮的每一段（录制、回放、分叉、导出、度量）都不需要评测结果：分叉与导出里按判定筛源的过滤都是可选项，默认不读判定（`trace_branches` 的 `require_eval` 与导出器的 `filters.require_eval` 默认都是 `null`，发布的 `export_sft.yaml` 也写 `null`），度量报告从不读判定；去污染只认导出配置里明确列出的 benchmark（默认 `[]`：不去污染，数据集 manifest 记 `decontaminated: false`），不再从判定推断（§4.15 "切分与去污染"）；`execution_dag.md` 里评测器指向分叉与导出两个模块的边因此是可选边。剩下的一处依赖在数据一侧、不在判分器：导出的参考答案泄漏扫描读 benchmark 的评测数据加载器（§4.15 "校验"）。"Record Once, Evaluate Many" 不变：已提交的 trace 随时可以拿去评测。

| evaluator_id | 评测方式 | 适用 benchmark |
|-------------|---------|---------------|
| `exact_match` | 答案精确匹配 | GAIA, BrowseComp |
| `llm_judge` | LLM-as-judge 评分 | WebArena, Mind2Web |
| `test_suite` | 运行测试套件 | SWE-bench |
| `tool_trace` | 工具调用序列匹配 | ToolBench, BFCL |
| `visual_diff` | 截图对比 | OSWorld |
| `multi_criteria` | 多维度打分 | WebArena |

**独立入口**：`spectragent eval --config <yaml> --traces <run或父目录> [...] --out <新目录>` 只组合一个 `evaluator` 和一个 `eval_loader`（judge 仍经 evaluator 的 `llm` 受体），复用 `EvaluationRunner`。它只把显式提供的 trace 作为分母，不自动把整个 benchmark 未提供的任务补成失败；原 runner 显式 `expected_task_ids` 的覆盖策略保留。分叉的参考按 `BranchProvenance.source_benchmark/source_subset/source_task_id` 找，判定身份仍是被评的分叉 run。**任务变体的分叉例外（2026-10-21）**：它跑的是另一份任务（`BranchProvenance.task_variant`，§4.10），源任务的参考描述的不是它，所以 `EvaluationRunner` 从不去找。按参考评分的 evaluator（`exact_match`、`browsecomp`、`lexbench_browser`，以及任何没有声明的）对它报 `NoReferenceError`（`task variant '<id>': no reference for this task`，点名源任务），记为评测失败：`verdict_source: evaluation_error`，不进有效判定的分母、不算判错，trace 的 `evals/` 里不写判定。声明 `__needs_reference__ = False` 的 evaluator 照样评它：目前只有 `online_mind2web` 的 WebJudge（关键点从 trace 记下的任务文本生成，从不读参考行），用的就是变体文本；它收到的 `EvalReference` 只写分叉 run 自己的任务 id 与 benchmark，不带任何评测数据，判定的 `benchmark_details.task_variant` 记变体（id、两份任务文本、`note`）、源任务身份与 `reference: null`，不写 `source_reference`。一个批次的报告分别列输入 run、有效判定、判对、判错、评测失败；任务正确率只用有效判定作分母，同时显示有效判定覆盖，零有效判定为 `null`。新输出目录保留该批的判定副本、失败与脱敏配置；trace 内的同 slug 判定沿用现有最新结果语义。退出码 0 表示本批全部输入得到有效判定（任务判错不使进程失败），1 表示至少一份无法判定，2 表示配置或输入拒绝。

LexBench 图像 judge 没有任务文字、没有可用截图、回复没有有效的 0–100 总分，都属于评测错误，不制造任务失败。请求使用录制时间与已录下的 `loop.step_info.today` 解释时效文字；这些是提示中的可追溯证据，不是当前网站事实。实际 judge 模型与路由身份、模板快照、录制时间及已报告的 `eval_usage` 随独立结果保存；模型失败时已知的提示与用量也保留，未知用量不声称为零。此入口不启动浏览器、不访问当前页面、不修改恢复保真指标或训练导出。

`online_mind2web` 的并行图像 judge 属于一次独立评测：一条调用失败或外部取消时，请求其他调用取消，并在有限预算内等待。未结束的调用仍由 evaluator 持有直到实际结束，诊断记录其索引；已完成回复、提示快照和已报告用量随异常保留。普通清理失败不替代原错误或取消；等待清理期间新到的外部取消优先于先前的普通错误（原错误作为 cause），若原先就是取消则保留首次取消。Runner 随后仍执行其模型生命周期清理。

**评测器经 `TraceView` 读 trace，`TraceView` 认得步的来源（M31）**（`evaluator/trace_view.py`）。一个 trace 的步有三种来源（`StepRecord.origin`，§5.3）：

- `action_history` 只含 `origin == "policy"` 的步的动作，即这个 run 的 agent 自己做的决定；恢复出来的步（分叉 run 的前缀）是重放源 run 的动作，不混进来。它只从步记录读（`AgentResult` 不再带一份拼好的字符串，5.0 起），每个动作写成 `name(params)`，评测器用的只有这一个写法（`trace_view.py::written_action`）。
- `restored_action_history` 单列恢复出来的动作，每条带 `[restored]` 标签；非分叉 trace 为空。`online_mind2web` 的最终判定按顺序给出两者（先带标签的恢复前缀，再是策略动作），于是非分叉 trace 的 prompt 与以前逐字节相同。
- `screenshots` 仍是每个已提交步的截图，所以恢复出来的步的截图与终止观测的截图（`origin == "terminal"`）都在里面；终止观测的那张正是最后一个动作之后的状态。
- `step_count` 读 `AgentMetrics.steps`，只数策略步。

### 4.12 DOM Pipeline 抽象

```
DOMWatchdog (browser.dom 唯一 resolver)
    └── DOMCleanerProtocol.capture(tab-bound DOMCaptureSource)
          ├── browser_use: CDP Document + DOMSnapshot + AX 并行采集
          └── skyvern: 观测 scope → JS 执行 scope → 页内树遍历
                       → DOM.describeNode 关联 backendNodeId / session
    └── DOMCleanerProtocol.clean(capture) → DOMState
          ├── browser_use: simplify→optimize→paint-order→bounds→index
          ├── skyvern: 属性裁剪→frame/shadow 组装→索引
          └── skyvern_hit_test（M32）: skyvern 的全部 + 页内逐元素命中测试
                       → 记 hit_test；covered_elements: drop 时不给全被盖住的元素编号
    └── DOMBudgetPass → DOMSerializerProtocol → str + selector_map

Serialization (可插拔策略；每个都自带 describe_format() 格式说明)
    ├── text: 每个元素或文本一行、tab 缩进；可操作元素前缀 [N]（N = selector map 键），
    │         另有 *[N]（上一步没有）、|scroll element| / |scroll element[N]、
    │         |SHADOW(open|closed)|、|IFRAME| / |FRAME|、<!-- SVG content collapsed -->
    └── html: 精简 HTML、两空格缩进；可操作元素的第一个属性是 data-idx="N"，
              shadow root 与 frame 的内容写在宿主元素里，不加标记
```

**序列化器自带格式说明（用户 2026-10-23 的裁决，§4.4 "观测格式由提供方描述"）。** `DOMSerializerProtocol` 除 `serialize(state) -> str` 外还有 `describe_format() -> str`：本实例输出的文本长什么样、哪些元素可以操作、动作怎样用 `index` 指代一个元素，以及它写出的其余每种标记，几行话，按实例的旋钮写（`text` 的 `max_text_length`、`html` 的 `max_href_length`）。BrowserCapability 经可选的 `FragmentFormats` 把它作为 `browser.dom` 的说明交出，BrowserUse 与 Skyvern 的提示词原样引用。两个序列化器的模块文档串与 `describe_format` 说同一件事，改输出就要一起改：`test_browser_dom_serializer_format.py` 在真实 `DOMState` 上检查说明里的标记就是输出里的标记。`text` 只给可操作（`is_interactive`）的已索引元素写 `[N]`：BrowserUse 清洗器给不可操作的滚动容器也编号（进 selector map，`browser.scroll` 可以用它的 `index`），`text` 却把它写成不带序号的 `|scroll element|`，说明照实告诉模型这一行不能操作；`html` 对每个已索引元素都写 `data-idx`。

`DOMCaptureSource` 是每次观测、按 tab 构造的运行期能力；配置层只有一个带 YAML knobs 的
cleaner 实例，不在它身上保存某个 tab 的 scope 或 CDP session。Skyvern 的页内脚本由本项目
独立实现，每次执行都在同一 JS world 构树并返回远程节点引用，采集端在释放引用前用
`DOM.describeNode` 取得现有动作链需要的节点身份。每个可识别的 frame（含 tab root、
same-process 与 OOP root）都在所属 CDP session 的 isolated world 执行；OOP frame
使用独立索引命名空间。导航后重建
运行期上下文，不复用旧页面 bootstrap。JS 路径失败时报告 DOM 观测失败，不静默降级为
BrowserUse snapshot。两种策略最终都交出同形的 `DOMState`，由同一个预算、serializer、
selector map 和 trace 路径消费。

**`skyvern_hit_test`：带命中测试的 Skyvern 变体（M32，用户 2026-10-09 裁决）。** 单独可选的 cleaner（`DOMCleanerRegistrar` 键 `skyvern_hit_test`，`SkyvernHitTestCleaner(SkyvernCleaner)`，`dom/cleaning/skyvern_hit_test.py`；加进 `BrowserCapability.__wiring__["dom_cleaner"]` 的允许键）。它针对的是 M18 的一个缺陷：Skyvern 把被吸顶导航盖住的链接也编了号，而 Skyvern 的 JS 路径没有任何 paint order（`snapshot={}`），归档也补不出来。

- **采集**：与 `skyvern` 完全相同的页内提取（`EXTRACTION_SCRIPT` 一字不改），之后在同一个远程对象上多调一次 `Runtime.callFunctionOn`（`HIT_TEST_FUNCTION`，`dom/js/skyvern_extraction.py` 里另一个常量）：对每个可交互的 ref，取元素可见矩形（`getBoundingClientRect` ∩ 本 frame 视口）的中心做 `elementFromPoint`（元素在 shadow root 里时用那个 root 的 `elementFromPoint`）；中心被盖住时按固定顺序试可见矩形内 5×5 的内点网格（各边内缩 2 CSS px，行优先），第一个命中的就是可见点。"命中"= 最上层元素是它本身、它的后代（沿 parentNode / ShadowRoot.host 上溯），或一个 `control` 指向它的 `<label>`。结果放在 `TargetCapture.raw_dom["skyvern_hit_tests"]`（按 ref），点以元素自身矩形左上角为原点的偏移给出，因为页内脚本不知道自己的 frame 在顶层视口里的位置。`capture_skyvern(source, *, hit_test=False)`：`skyvern` 不传它，CDP 调用序列不变。
- **清洗**：先跑 `SkyvernCleaner.clean`（不改它的任何代码），再后处理：每个有结果的已索引元素记 `DOMElementNode.hit_test = HitTest(center_topmost: bool, visible_point: tuple[float, float] | None)`。`visible_point` 是顶层视口的 CSS px（元素 `bounds` 的左上角 + 页内给的偏移 − 顶层文档滚动；`bounds` 已含 iframe 偏移），中心命中时就是中心。完全不在视口里的元素没有命中测试（`hit_test=None`，照常编号，离屏不算被盖住）。`covered_elements: Literal["drop", "keep"] = "drop"`：`drop` 时中心与网格都没命中（`center_topmost=False` 且 `visible_point=None`）的元素从 `selector_map` 里拿掉、`element_index` 置空，agent 看不到它的编号（这是开这个变体的目的，agent 可见）；`keep` 照常编号，命中测试照样记下。
- **进 trace**：`hit_test` 是 `DOMElementNode` 的新字段，元数据 `TRACE_OMIT_NONE`（`dom/models.py` 的第三种字段策略：值为 `None` 时编码器不写这个键，解码时缺键读成默认值），所以 `skyvern` 与 `browser_use` 的 `DOMObs` JSON 逐字节不变，`skyvern_hit_test` 的自包含 `selector_map` 条目带着它进 trace，训练导出的遮挡判断（§4.15）直接读。
- **已知限制**：命中测试只在元素自己的文档里做，父文档里的内容盖住整个 iframe 时测不出来；进程外 iframe 同理。每个在视口里的可交互元素一次到二十六次 `elementFromPoint`；实测（headless Chrome，fixture 的 `sticky` 页，8 个可交互元素，`test_browser_skyvern_hit_test_live.py::test_what_the_hit_test_costs_per_observation`）一次 DOM 观测的中位耗时 `skyvern` 51.3 ms、`skyvern_hit_test` 50.0 ms，差异在噪声内；元素多、被盖住的多的页面上网格探测才显出代价。

`cleaner.capture()` 交回的 `TargetCapture`
（BrowserUse：`DOM.getDocument` + `DOMSnapshot` + AX；Skyvern：页内 JS 的原始输出加
`DOM.describeNode` 的节点身份）连同 DPR，经 `WatchdogContext.raw_capture_sink(capture, dpr)`
交出去，与 `selector_map_setter` 是同一种 capability 借给 watchdog 的回调。DOMWatchdog 不知道
归档的存在：sink 默认什么都不做，只有绑定了需要原始 DOM 的归档器（`browser.dom_raw`）时，
capability 才给它一个真正的实现，把采集放进本 cycle 缓存的待定槽位；`_populate_cycle` 成功
（本 cycle 的全部 fragment 都到手、文档戳一致）时才把它提交，失败与重发的尝试丢弃，归档里
只有 agent 真正看到的那一次。提交时 capability 补上 cycle、tab、viewport、DPR、cleaner 键和
文档戳。`TargetCapture` 因此多了 `loader_id` / `url`（根 frame 的，两条采集路径本来就读
`Page.getFrameTree`），它们也是 cycle 一致性检查的文档戳（§4.14 "同一 cycle 的证据描述同一个
文档"）。DOM 没有参与协商的 run（UI-TARS 纯视觉）由归档器自己采，见 §4.14 的 `browser.dom_raw`。

**敏感控件的判定只有一份。** `dom/sensitivity.py` 是"这个表单控件的值算不算秘密"的唯一 Python
规则（`INPUT` 的 type 为 password / file / hidden，`autocomplete` 以 `cc-` 开头或为
`one-time-code`，id / name / autocomplete 命中 OTP / 验证码一类的提示正则）。Skyvern 页内脚本
（`dom/js/skyvern_extraction.py`）里的同一条规则不改写、不生成，用测试与它对齐：提示正则的字面量
逐字相等，并在真实 Chrome 里用同一张控件表比对两边的判定。Python 这一份有四处用：归档对原始采集的
脱敏（§4.14 隐私）；BrowserUse 清洗器建树时打码（下一段）；键入动作在敲字之前判定焦点所在的元素，
判定记进 `ActionResult.typed_into_sensitive_field`（§4.14 隐私、§5.6）；`browser.extract_content`
读一个元素时，返回的 HTML 先按这条规则打码（下面"动作结果同样打码"）。

**观测路径同样打码（用户 2026-10-13 裁决）。** BrowserUse 清洗器在把 CDP 原始数据建成
`DOMElementNode` 树的同时套用这条规则：敏感控件及它下面每个节点的 `value` 属性、它下面的文本
（user-agent shadow root 里键入的值、`<textarea>` 的内容、`<select>` 的选项）、控件内部节点的
无障碍名字，都换成 `<redacted>`；控件自己的无障碍名字（它的标签）保留，空值保持为空。对清洗器读到
的部分，换的位置与占位符和归档的原始 DOM 脱敏相同，所以离线用 `capture_from_document` 重洗
`env/` 的原始 DOM，得到的状态与现场相同（测试钉住）。在建树时换、而不是先拷贝采集再脱敏：采集与
归档共用，不能改；深拷贝一份约 1.9 万个 DOM 节点、0.9 万个无障碍节点的合成采集实测约 170 ms，
建树时多一次判定几乎不花时间。于是提示词（`serialized_dom`）和 trace 里的 `browser.dom` fragment 都不带敏感值；模型看到
`value=<redacted>` 只说明页面在标记里写了值（键入的值不在标记里，BrowserUse 的提示词本来就不显示）。
Skyvern 的页内脚本本来就不把敏感值放进树。判定有意宽（`pin` 命中 `shipping`）：被判敏感的
`<select>` 的选项与输入框的值 DOM agent 看不到，与 Skyvern 一致。验证：
`test_browser_dom_masking.py`（合成页面，含离线重洗）与 `test_browser_archive_live.py` 的 DOM
观测路径隐私测试（真实 Chrome）。

**动作结果同样打码（用户 2026-10-13 同意）。** `browser.extract_content` 给了元素编号时返回
`DOM.getOuterHTML` 的结果，它成为动作结果，进下一轮提示词（BrowserUse 把它渲染进历史），页面写进标记的
密码 `value` 会跟着进去。所以 ActionWatchdog 先用 `dom/sensitivity.py::withhold_sensitive_markup`
在序列化的 HTML 上按观测路径的位置打码：敏感控件及它里面每个元素的 `value` 属性、它里面的文本
（`<textarea>` 的内容、`<select>` 的选项与 `<selectedcontent>` 里抄的那一项）与注释，换成按 HTML 转义的
占位符 `&lt;redacted&gt;`（Chrome 155 序列化时在文本与属性值里都这样转义 `<` / `>`，实测）；空值与空白文本
保持原样，丢了 `value` 的起始标签按 Chrome 的写法（`name="value"`，转义）重写，其余字节一个不动，没有
敏感控件的 HTML 原样返回。`<template>` 的内容与 `<noscript>` 里的控件（开着脚本时序列化成原文）照样是标记，
照样打码；`<script>` / `<style>` 的原文是代码，不动。元素本身在敏感控件里面（敏感 `<select>` 的
`<option>`）时，它自己的标记里没有控件，按它被编号时那棵观测树的祖先判定，里面的值全部打码。HTML 解析
不了（标准库的解析器遇到某些畸形声明会断言失败）就整段换成占位符：查不了的不交出去。不给编号时返回的
页面 `innerText` 不含任何输入框、文本框的值（Chrome 155 实测），只列出 `<select>` 的选项文字，被判敏感的
也在内：那是页面写的选项列表，不是用户的选择，残余，写明。验证：`test_browser_markup_masking.py`（共用
的控件表、表单、`<template>` / `<noscript>`、敏感 `<select>` 的 `<option>`、处理器）与 DOM 观测路径的
真实 Chrome 隐私测试（读写进标记的密码字段，返回的 HTML 里是占位符，trace 里一次也不出现）。

### 4.13 Input 拟人化层（mouse_trajectory + typing_rhythm）

Browser capability 内部除 DOM pipeline 之外，另有两个平级的 conscribe 注册表，管的是**输入侧**：光标怎么移动（`MouseTrajectoryRegistrar`，`core/capabilities/browser/input/trajectory.py`）、字怎么敲出来（`TypingRhythmRegistrar`，`core/capabilities/browser/input/rhythm.py`）。它们服务的是 anti-bot 敏感的评测场景——一次点击的曲线、停留、按键长短本身就是被检测系统读取的行为信号。

```
agent 的一次 click ─→ ActionWatchdog ─→ HumanMouse ─→ CDP Input.dispatchMouseEvent
                       │ (框→点的唯一接缝)  │ (input/mouse.py)
                       │                    └── MouseTrajectoryProtocol (Strategy)
                       │                         ├── instant   单点直跳 == 旧行为（默认）
                       │                         ├── windmouse 引力/风力物理模型 + Fitts 定时
                       │                         ├── bezier    三次贝塞尔 + Fitts 定时
                       │                         └── learned   人类录制数据训练的模型（numpy 推理）
                       │
agent 的一次 type  ─→ ActionWatchdog ─→ HumanKeyboard ─→ CDP Input.dispatchKeyEvent
                                         │ (input/keyboard.py)
                                         └── TypingRhythmProtocol (Strategy)
                                              ├── instant 整串一次 Input.insertText == 旧行为（默认）
                                              └── human   逐字符 keydown/keyup，flight / hold 建模

             两条链共用一个时钟：play_on_deadline（input/_schedule.py）
```

**两个注册表而不是一个**，因为它们是互相独立的轴：一次 run 完全可能要曲线光标 + bulk insert，或者反过来；而且两者产出的东西不同（waypoints vs keystrokes）。合成一个「humanization level」枚举会让两根轴不可分离，也让第三根轴（滚轮节奏、触屏手势）加不进来。这就是 §1.1「每个变化轴一个注册表」在这一层的落地。

#### 实际输入平台与按键原义

用户 2026-10-14 裁决：检测实际浏览器平台并告知 Agent；可以配置预期平台，不符就拒绝运行。撤回将 Ctrl/Cmd+A 不分平台统一成全选的方案。平台是浏览器所在环境的事实，不是模型品牌或 Agent 宿主机的事实，也不是 UA 伪装的一部分。

Browser capability 在 setup 时、模型做任何决定之前测量实际桌面输入平台（`macos` / `windows` / `linux`）。Chrome 的内部版本页所报告的操作系统可作为当前实现的事实来源；`Browser.getVersion.userAgent`、页面的 `navigator.platform` / 可覆盖的 client hints，以及 Agent 宿主机 `sys.platform` 都不能在测量失败时偷偷代替它。检测失败或不支持的结果要明确拒绝，不能把 unknown 当 Linux。可选配置 `expected_platform`（默认 `null`，未要求某个平台）是断言，不是模拟开关；配置值与实测不同时在 setup 拒绝，点名二者，不能让真实 macOS 冒充 Windows。

同一份实测结果供输入控制器使用，也作为独立的环境观测 `browser.platform` 交给三个浏览器 Agent：UI-TARS 仍不需要 DOM，模型看到截图与明确的平台说明。UI-TARS 是跨平台的 computer-use Agent，平台说明写成“Computer-use agent; environment: browser. Actual input platform: …”，环境名取自 fragment 自己的键前缀（`browser.platform` → `browser`），不 import 浏览器类型；Skyvern、BrowserUse 仍写“Actual browser input platform”，两者的按键原义规则相同；截图来源 URL 仍不渲染进视觉提示。平台字段由现有 fragment/resolver/三方协商通路提供，不另建第四条配置或观测通道。平台不能仅写进 trace 而不告诉决策模型，也不能只放进 prompt 而不记录。该事实写入 browser 的 run 指纹，归档回放须与源平台相同；缺少平台证据的源不能按新契约猜测执行，旧文件保留作历史分析，不回写。

`send_keys` 是字面键名和组合键：Ctrl 保持 Control，Meta/Cmd/Command 保持 Meta/Command（Alt/Option 保持 Alt），修饰位与页面收到的 DOM 键事件必须一致，执行器不把错误平台的 Ctrl+A 改成 Meta+A。不认识的修饰键、空键名或空组合分段在派发前拒绝，动作结果明确失败，不能只写日志后报告成功。`send_keys` 只接受三种输入：命名键、单个字符、用 `+` 连接的组合键；加号键本身就是字符 `+`：单独的 `+`，或组合末尾的 `++`（`Control++`，用户 2026-10-20 的裁决），别处的 `+` 都是分隔符，`ctrl+`、`+a`、`++` 这类空分段照旧拒绝；其他字符串（例如整段 URL 或一句话）同样在派发前拒绝，错误信息写明“send_keys 只接受按键或组合键，输入文本请用 type”。页面没有变化不算输入错误。对于浏览器 CDP 不自行执行的原生编辑快捷键，只有与实测平台匹配、且不带其他修饰的组合才能补对应编辑命令：macOS 的 Meta+A、Windows/Linux 的 Control+A 可带 `selectAll`；另一平台的组合不强制全选，也不改写修饰键。命令只附在真实 `keyDown` 上，不再发送可打印的 `char`；由 Chrome 的默认行为执行，因此页面的 `keydown.preventDefault()` 仍能取消它，不另用 JS 选择范围绕过页面。除此之外，`send_keys` 只认一张固定的、点名的浏览器快捷键表（用户 2026-10-24 的裁决，下面“浏览器自己的快捷键”）：不推断任意组合键想做什么，也不顺带扩展复制、粘贴、剪切。

`type` / `type_coordinate` 的 `clear=True` 则是明确的语义操作“清空这个字段”，不是模型提供的原始组合键；它可以根据同一份实际平台事实发正确的全选与删除序列，不再自行从 UA 或宿主机猜平台。清空只在确认焦点落在收文字的字段上之后才发（§4.5“动作效果”）：没有这样的字段聚焦时，全选会选中整页，所以那时一个键也不发，动作失败。`selectAll` 编辑命令只附在浏览器自己编辑的字段上；自己处理按键的 `textbox` 部件收到完整的全选与退格按键、不带命令（`HumanKeyboard.clear_focused(select_all_command=False)`），否则 Chrome 会选中整页。回归要分别证明平台检测抗 UA 伪装、预期值不符/未知拒绝、模型确实收到平台、同平台正确快捷键与另一平台组合保持区分，以及回放平台不一致拒绝。云 CDP 是否允许读取内部平台事实必须实测；不支持时如实失败，不能用本机平台兜底。

输入端由 `WatchdogContext.input_platform` 注入 `ActionWatchdog` 和 `HumanKeyboard`，缺值直接拒绝；键盘控制器不再自行查询 UA、`navigator.platform` 或宿主机。named-key 的按下失败或取消后仍尝试有界 `keyUp`（2 秒），重复取消不使清理永等，外部取消（清理期间才到来也一样）优先于普通输入错误；其余清理失败不替代原输入异常。`send_keys` 的 handler 错误交回 ActionResult，空错误消息也必须带异常类型，不报告成成功。

#### 浏览器自己的快捷键（用户 2026-10-24 的裁决）

af 发出的按键事件只到页面，Chrome 自己的快捷键从不执行（下面"按键事件不带原生按键码"：带原生按键码时 Chrome 会拿去匹配它的菜单，打开 `chrome://settings/help`，所以不发）。在真实桌面上训练的 teacher（UI-TARS，真实浏览器里 Cmd+[ 就是后退）却一直按浏览器的快捷键：最近三轮录制每轮 15 条里有 8–11 条按过，每轮 43–56 步（Alt+Left、Meta+[、Meta+W、Meta+L、Meta+T、Meta+R、Ctrl+Tab、F6、Meta+Shift+T……）。效果行只写 `Pressed Meta+W with nothing focused; nothing is focused now.`，离开站点（广告、跨站链接）之后 teacher 回不去，白费 6–25 步；"输入被拒、再按回车"的情况约一半是先按 Meta+L、再在截图上方正中它以为是地址栏的地方输入网址。裁决：一张小的、固定的、点名的表，不做一般的意图推断。

- **表**（`core/capabilities/browser/input/shortcuts.py`，纯函数 `browser_shortcut(chord, platform)`）：只按实测的输入平台读（`input_platform`，上面"实际输入平台与按键原义"；从不从 UA 推断，不改写另一平台的修饰键）。绑定是 Chrome 自己的：键盘快捷键帮助页（<https://support.google.com/chrome/answer/157179>，2026-10-24 核对）与它的快捷键表（Chromium 的 `chrome/browser/ui/cocoa/accelerators_cocoa.mm`、`chrome/browser/global_keyboard_shortcuts_mac.mm`，Windows / Linux 的 `chrome/browser/ui/accelerator_table.cc`）；帮助页列得不全（例如没有 macOS 的 Meta+R、Control+Tab、Meta+Shift+]），以快捷键表为准。字母不分大小写；Shift 加方括号按它下面的键认（`Meta+Shift+}` 就是 `Meta+Shift+]`）。

  | 命令 | macOS | Windows / Linux |
  |---|---|---|
  | 后退 | Meta+[、Meta+ArrowLeft | Alt+ArrowLeft |
  | 前进 | Meta+]、Meta+ArrowRight | Alt+ArrowRight |
  | 重新加载 | Meta+R | Control+R、F5 |
  | 强制重新加载（不用缓存） | Meta+Shift+R | Control+Shift+R、Shift+F5、Control+F5 |
  | 关闭 tab | Meta+W | Control+W、Control+F4 |
  | 下一个 tab | Control+Tab、Meta+Shift+]、Control+PageDown、Meta+Alt+ArrowRight | Control+Tab、Control+PageDown |
  | 上一个 tab | Control+Shift+Tab、Meta+Shift+[、Control+PageUp、Meta+Alt+ArrowLeft | Control+Shift+Tab、Control+PageUp |
  | 第 1–8 个 tab | Meta+1 … Meta+8 | Control+1 … Control+8（Linux 另有 Alt+1 … Alt+8） |
  | 最后一个 tab | Meta+9 | Control+9（Linux 另有 Alt+9） |

- **执行，与对应动作同一段代码**（`actions/_browser_commands.py`）：实测平台上表里的组合由 `browser.send_keys` 自己执行。后退、前进经 `GoBackEvent` / `GoForwardEvent`，即 `go_back` / `go_forward` 的 `ActionWatchdog._navigate_history`；重新加载经 `RefreshEvent`（`Page.reload`，强制的带 `ignoreCache`；它的回复在新文档提交之前就到，所以等主 frame 的 `loaderId` 变了才读之后的 URL，上限 `action_effect_timeout_s`，没等到就记 unknown，从不把离开的文档当成重新加载出的那个）；切换 tab 经 `BrowserCapability.switch_tab`，即 `switch_tab` 动作的那一段，跟随窗口照样从新焦点重开。下一个 / 上一个 / 第几个 tab 按 `browser_meta` 列出的顺序（tab 打开的先后）数：CDP 拿不到 Chrome 的 tab 条，模型看到的也只有这个顺序（Chrome 自己把页面开的 tab 插在 opener 右边，它的 tab 条可能是别的顺序）。下一个、上一个首尾相接，与 Chrome 一样。执行不了的命令——没有可后退的历史、只开着一个 tab 时按下一个、第几个超过已开的 tab 数（这三种 Chrome 什么也不做）、关最后一个 tab——都是失败的动作，错误点名这是浏览器的哪个快捷键，例如 `Meta+[ is the browser's Back shortcut, and it did not run: Cannot go back: no navigation history entry`。
- **关闭 tab** 没有现成的动作，实现一次（`BrowserCapability.close_focused_tab`）：`Target.closeTarget` 关掉焦点 tab，等到 Chrome 不再列出它（至多 2 s；页面的 `beforeunload` 对话框会让它留着，这时动作失败、焦点不动）；焦点按焦点 tab 自己关掉时的同一条规则移动（§4.14 "页面新开的标签页"的 `tabs_after_close`：还开着的 opener，否则最近打开、还开着的 tab），经 `_move_focus`（关掉的 tab 不冻结）；跟随窗口像 `switch_tab` 之后那样从新焦点重开；capability 为关掉的 tab 持有的东西经 `_release_tab` 释放。只剩这一个 tab 时拒绝，run 需要一个页面：`... is the only tab open, and the run needs a page, so it was not closed`。不记 `focus_moved`：这是 agent 自己的动作。与 Chrome 的差别：Chrome 关掉当前 tab 时依次选这个 tab 开的 tab、它的 opener 开的别的 tab、opener、tab 条上的邻居（Chromium `chrome/browser/ui/tabs/tab_strip_model.cc` 的 `DetermineNewSelectedIndex`），而且用户切到无关的 tab、或不经链接的导航之后就忘掉 opener 关系；挑哪一个子 tab、兄弟 tab 与邻居要 tab 条的顺序，忘掉的时机要看用户手势，CDP 都拿不到，所以与页面自己关掉 tab 时用同一条规则，不另做一份近似。
- **键发不发给页面：不发。** Chrome 把关闭 tab、新 tab、新窗口、切换到下一个 / 上一个 tab 保留给浏览器，页面根本收不到这些键（`kReservedCommandIds`）；后退、前进、重新加载、第几个 tab 则先交给页面，页面没处理（没有 `preventDefault`、没有被编辑命令用掉）才执行。CDP 不告诉调用方页面处理没处理一个键，所以"页面没处理才执行"做不出来；先发给页面再照样执行，会让自己处理这些组合的页面做两遍。所以表里的命令一律不把键发给页面，效果的 `keys` 为空。代价：自己定义了这些组合的页面（少见）收不到它们。唯一的例外是 macOS 的 Meta+ArrowLeft / Meta+ArrowRight：Chrome 把它们当作"延迟"的快捷键，只在页面没用这个键时才后退、前进，而在文本框里它们把光标移到行首、行尾。所以先读焦点（`FieldProbe`），只有测得没有收文字的元素聚焦（`entry` 为 `none`）时才后退、前进；文本框、`textbox` 部件、只读字段，以及读不出的，键照常发给页面。
- **不模拟的**：要浏览器界面、而这个 headless 浏览器不显示的快捷键——地址栏（macOS 的 Meta+L；Windows / Linux 的 Control+L、Alt+D、F6，以及在地址栏里搜索的 Control+K / Control+E）、新 tab（Meta+T / Control+T）、重新打开关掉的 tab（Meta+Shift+T / Control+Shift+T）、新窗口与隐身窗口（Meta+N、Meta+Shift+N / Control+N、Control+Shift+N）、历史（Meta+Y / Control+H）、下载（Meta+Shift+J、Meta+Alt+L / Control+J）、书签管理器（Meta+Alt+B / Control+Shift+O）、查找栏（Meta+F / Control+F、F3）。键照常发给页面，效果加一句它是什么：`Meta+L is the browser's address bar shortcut; this browser shows no address bar, so nothing in the browser happened.`（新 tab 写 `new-tab page`，重新打开写 `tab strip`，查找写 `find bar`……）。opt-in 的 `ui_navigation_guard` 照旧否决地址栏、新 tab 与新窗口的快捷键（§4.14），与这里无关。
- **另一平台的快捷键**（macOS 上的 Alt+ArrowLeft、Windows 上的 Meta+[）：照旧发给页面，不执行，也不改写修饰键（macOS 上 Alt+ArrowLeft 在文本框里是按词移动光标的编辑键，照旧发出）。只有测得没有收文字的元素聚焦时，效果加一句实测平台的绑定，例如 `On macOS the browser's Back is Meta+[.`；在文本框里它常是实测平台的编辑键（macOS 的 Control+F 右移一个字符），不加。
- **效果**：`send_keys` 的 `Effect` 是 `KeyPressEffect`，即 `TextInputEffect` 加一项 `shortcut`（`effects.py`，按 `outcome` 区分三种）：`ShortcutRan`（组合键、命令、第几个 tab；导航类是前后 URL，tab 类是前后 tab，关闭时是关掉的 tab 与焦点移去的 tab；文档是否加载完成。这时 `keys` 为空、`text_entered` 为 false、`value_changed` 为 `unavailable`，`target` 是按下时的焦点，`focus_after` 是之后显示的 tab 上的焦点）、`ShortcutWithoutUi`、`ShortcutOfAnotherPlatform`；不是表里的组合时是 `None`。`describe()` 仍是一行：执行了的是 `Meta+[ ran the browser's Back; the URL changed from … to …; the document has finished loading.`、`Meta+W ran the browser's Close tab; closed tab A (…), and the focus moved to tab B (…); …`（URL 变化与 tab 切换的写法与 `go_back`、`switch_tab` 的效果共用）；另两种是原来那一行后面加上面那一句。组合键照效果的惯例按规范写法写（`Shift+Meta+R`）。读文本输入效果的读者（查看器、lookahead）照样读它。
- **动作说明**：`send_keys` 的描述（三个 Agent 的提示词都引用它，UI-TARS 的 `hotkey` 也是）多两句：后退、前进、重新加载、关闭 tab 与切换 tab 的浏览器快捷键能用，两个平台的主要绑定都写出（描述在 `prepare()` 时生成，那时还没测平台）；没有地址栏和新 tab 页，它们的快捷键什么也不做。Agent 的提示词不另改：没有一处与新行为矛盾。
- **录制、回放与分叉**：动作照常重放（恢复对 `send_keys` 是恒等，§4.14 "恢复：重定位与校验"），所以回放与分叉执行的是同一个命令，切换、关闭 tab 之后焦点所在的也是同一个 tab。这次改动之前录下的 run 里，这些组合键当时只发给了页面，记为什么也没发生；重放时它们执行命令，下一个被恢复的观测如实与录下的不同（后退、前进、关闭与切换 tab 表现为 `url_mismatch` 或 `screenshot_distance`），这一步判为发散。不进回放指纹，不为旧录制另设兼容路径，与 2026-10-22 的输入前焦点判断、`new_tabs` 同一个做法：不一致有恢复校验看得见的信号（§4.14 "已知限制"）。
- **回归**：`test_browser_input/test_shortcuts.py`（纯函数：三个平台的每个绑定、agent 常写的拼法、不模拟的界面、另一平台的组合点名实测平台的绑定、表外的键）；`test_browser_actions/test_browser_shortcuts.py`（脚本化 CDP：每个平台的后退、前进、重新加载与强制重新加载都执行、键不发给页面、效果行；重新加载没在上限内提交时之后的 URL 是 unknown；没有历史时失败并点名快捷键；macOS 的 Meta+ArrowLeft / ArrowRight 在文本框里归页面、没有焦点时后退；不模拟的界面与另一平台的说明、文本框里不加说明；trace 往返）；`test_browser_shortcut_tabs.py`（fake CDP 上真实的 capability：三个平台的下一个 tab、首尾相接、第几个与最后一个、超出与只有一个 tab 时失败、跟随窗口重开、关闭回 opener 或最近打开的、之后的观测不记 `focus_moved`、关最后一个被拒、Chrome 留着 tab 时失败、macOS 上的 Control+W 只发给页面）；`test_browser_shortcuts_live.py`（真实 headless Chrome，本机页面：后退与前进真的移动历史，重新加载与强制重新加载得到新文档（`performance` 报 `reload`、页面变量没了），地址栏快捷键的键到了页面，macOS 上文本框里的 Meta+ArrowLeft 归文本框、tab 不后退，切换 tab 之后新焦点 `visible`、离开的页面没收到键，关闭回 opener 而不是更近打开的 tab，关最后一个被拒）。

#### 策略本身就是开关

`humanized` 是**策略的属性**，不是配置项：`MouseTrajectoryBase` / `TypingRhythmBase` 默认返回 `True`（会生成路径 / 会建模时序的策略，按构造就是拟人的），只有 `instant` 覆盖成 `False`。消费者读这一个布尔来决定路径之外的行为——冷启动位置、滚轮是否分段缓动、键入 / 选择前是否先把光标移过去，以及 `instant` 下整串一次 `insertText`。

**它旁边不允许再出现第二个开关**（defect F4）：`BrowserCapability` 上曾经有一个 `humanize_mouse: bool` 与 `mouse_trajectory` 受体并列。YAML 里同时写 `trajectory_type: windmouse` 和 `humanize_mouse: false`，配置校验全过，运行时静默走旧的直跳——用户配了一个策略，框架执行的是另一个。同一个语义有两个声明点，就必然存在互相矛盾的组合，而矛盾的那个组合没有任何地方会报错。所以规则是：**策略即开关**，`humanized` 只能从策略读，config 面上不得出现与之并列的布尔。`typing_rhythm` 从第一天起就是这个形状。

同理，两者都是 `BrowserCapability.__wiring__` 上的**受体**（wired receptor），不是 `Literal` 字符串：策略以**构造好的实例**注入，它自己的 knobs（windmouse 的 gravity / wind、bezier 的控制点、`human` rhythm 的 median_iki / hold 分布、learned 的 weights_dir / num_candidates，以及下面两节的 click-hold 与 aim 分布参数）因此全部经 conscribe 进入生成的 config，YAML 可达且被校验。曾经的形状是 `Literal` 加一个未类型化的 overrides dict，conscribe 把后者整个从 schema 里丢掉——knobs 存在，没有任何东西能设置它们。这与 `dom_cleaner` / `dom_serializer` 是同一类 defect（见 §4.12），`tests/test_core/test_capabilities/test_receptor_hygiene.py` 是结构性守卫。

但**受体身份是必要而非充分条件**：注册表还必须被 Composer 收集（`core/registries.py::collect_registrars`），否则 conscribe 无从知道这个字段是一个判别联合，只能把它发成裸 `str`——knobs 依然不可达，而且更坏：一个拼错的 `trajectory_type` 不会被校验挡住，会静默退回 `instant`（非拟人）。`mouse_trajectory` 正是这样漏过一轮的，`collect_registrars` 里现在留着记录此事的注释。新增任何一根策略轴都要同时做这两件事。

#### 两个 Protocol

```python
@runtime_checkable
class MouseTrajectoryProtocol(Protocol):
    def generate(self, start_x, start_y, end_x, end_y, *,
                 target_w: float = 0.0, target_h: float = 0.0) -> list[TrajectoryPoint]:
        """返回路径 waypoints；**最后一点必须精确等于** (round(end_x), round(end_y))。"""

    def click_hold_ms(self) -> float:
        """按下到抬起的时长，每次点击抽一次。humanized == False 必须返回 0.0。"""

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """本次点击在 width x height 目标框内相对**框心**的落点偏移，每次点击抽一次。
        humanized == False 必须返回 (0.0, 0.0)；任何策略对没有尺寸的框都必须返回 (0.0, 0.0)。"""

    @property
    def humanized(self) -> bool: ...


@dataclass(slots=True, frozen=True)
class TrajectoryPoint:
    x: int
    y: int
    dt_ms: float = 0.0     # 本点**之前**的间隔；第一点为 0


@runtime_checkable
class TypingRhythmProtocol(Protocol):
    def generate(self, text: str) -> list[Keystroke]:
        """每字符一个 Keystroke，长度与字符不变——节奏只决定*何时*，绝不决定*什么*。"""

    @property
    def humanized(self) -> bool: ...


@dataclass(slots=True, frozen=True)
class Keystroke:
    char: str
    gap_ms: float = 0.0    # 上一键抬起到本键按下的 flight time
    hold_ms: float = 0.0   # 本键 keydown → keyup 的 dwell
```

`click_hold_ms`（本轮新增）与 `aim_offset_px` 挂在 Protocol 上而不是 `TrajectoryPoint` 上，理由是同一条：它们**不是任何单个 waypoint 的属性**——按住的时候什么都不动，落点偏移是整次点击一个数——但它们确实是拟人化，而策略是这一层唯一的开关。把**行为**放在策略上还有一个副产品：**RNG 永远不必离开拥有它的对象**。曾经有一版把 `seed` 加进 Protocol，好让 watchdog 自己复刻一条同源的随机流；那是把实现管线塞进公共契约，下一个消费者就会来要 `derive_rng(label)`。正确的读法是：第二个消费者想要相关联的随机性时，说明那个**行为**应该搬到策略上，而不是把 RNG 句柄导出去。

两个新成员都**没有 base 默认值**——`MouseTrajectoryBase` 直接 raise，并报出继承了这个拒绝的类名——因为看起来安全的默认值恰好就是 defect 本身：`0.0` 的 hold 是同一毫秒内按下并抬起，`(0.0, 0.0)` 的 aim 是每次都精确命中算术中心（实测落点误差 0.78 px，人类是 3-31 px）。`humanized` 可以有默认值，时长和偏移不能。分布的实现是共享的（`input/_timing.py::sample_click_hold_ms` 的 lognormal、`input/_aim.py::sample_aim_offset_px` 的截断正态），但**分布的参数是每个策略自己的 `__init__` 参数**，因此是 YAML 可达的 config 字段，而不是模块常量。

> conscribe 在类注册时**不**做 Protocol 一致性检查——少一个成员照样注册成功，到第一次点击才炸。注册表全量的 `isinstance(strat, MouseTrajectoryProtocol)` 测试把它变成测试失败（`tests/test_core/test_capabilities/test_browser_input/test_trajectory.py`），给 Protocol 加成员时要同时扩那个循环。

#### 端点不变量：轨迹的终点就是被请求的那个点

`generate()` 契约里「最后一点必须精确等于 `(round(end_x), round(end_y))`」是硬约束；`HumanMouse.move_to` 对应地**只裁剪中间点**到 viewport，终点原样下发——所以 off-viewport 的目标依然精确，trace 里记的点、overlay 画的点、实际 press 的点是同一个。

这条不变量决定了「点到框里哪儿」该放在哪：**不在策略里**。测量给出的是一个**框**，点击需要的是一个**点**；策略若自己挪动终点，trace / overlay / `ActionResult` 就会对这次点击到底落在哪产生分歧。所以策略返回一个**偏移**（`aim_offset_px`），由 `ActionWatchdog._aim_inside` —— 整个 capability 里唯一一处「测量框 → 点」的接缝 —— 施加，交给轨迹的已经是一个确定的精确点。

这个接缝还拥有两件策略不可能知道的事：

- **窗口**。border box 从不被裁到 viewport：`getBoundingClientRect` 对比窗口还高的 banner 返回它的整个高度，`DOM.scrollIntoViewIfNeeded` 也只保证元素可达。所以 `_visible_span` 先把框**关于框心对称地**缩到真正可见的部分，再交给策略——对称而非各边独立裁剪，因为喂进去的是一个**中心对称**的分布，非对称窗口会把落点偏向恰好可见的那一侧；框心本身已在屏幕外时返回 0，也就是退回精确框心（这一切存在之前的行为）。少了这一步，散布出去的点会落在负坐标或屏幕外，Chrome 把事件丢掉而 action 照样报 success。
- **谁该被散布**。每一个来自**测量**的框都走这里（index click、type / select 前的光标趋近、滚轮的落点）——它们带着同一个 tell。而 `click_coordinate` / `drag` 的点来自**模型自己**，那是 agent 的意图，也没有测量框可言，不过这条接缝。

#### 一个时钟：`play_on_deadline`

**拖动的按钮状态属于一次手势。** `HumanMouse.drag` 从按下到松开之间的每个 `mouseMoved` 必须显式携带实际按住的 `button` 名字及 `buttons` 位掩码（左键 `left` / 1、右键 `right` / 2、中键 `middle` / 4）；普通移动为 `none` / 0。两个字段从同一个手势参数推导，不能各自配置。仅补位掩码也不够：真实 Chrome 中事件虽显示 `buttons=1`，缺少 `button=left` 的原生 range 和 pointer capture 仍不能完成拖动。仅发出过 `mousePressed` 不够：未带位掩码的移动会让页面读到 `buttons=0`，原生 range 和 pointer-capture 拖动可能停在中途，而 CDP 命令仍全部成功。按钮状态沿这次拖动的移动调用传递，不是全局开关；按下或移动异常、取消时也尝试松开；释放命令有独立的 2 秒清理上限，重复取消不取消这次有界清理，超时后取消未应答的 CDP 调用，避免 scope 的 teardown 一直等释放。清理期间到来的外部取消优先于普通输入错误；其余清理失败不得覆盖原输入异常或取消，正常路径释放失败则动作失败。这个上限不是轨迹节奏的第二个时钟。输入 tick 仍不写进 agent WAL。

`dt_ms` / `gap_ms` / `hold_ms` 是**计划，不是 sleep 时长**。`input/_schedule.py::play_on_deadline` 是这一层唯一的调度器：它在发出任何东西之前就把全部间隔折算成**绝对 deadline**（slot *i* 到期于 `start + sum(dt[: i + 1])`），然后逐个 yield。调用方在两次 yield 之间 await 的东西（一次 CDP 往返，headless Chrome 实测 ~17 ms/`mouseMoved`、~12 ms/`mouseWheel`）因此花在它所属的 slot **里面**，而不是加在它**上面**。

先 sleep 间隔、再 await 派发，等于给每个 tick 加一次传输成本。实测后果：50 Hz 的 learned 路径被放成 32.6 Hz，windmouse（ease-out 尾巴每几毫秒就要一个 waypoint）被放慢到 **5.06 倍**，12 ms 的滚轮步被拉到 28 ms。改成绝对截止时间后 windmouse 1.10×、learned 1.01× @ 49.7 Hz（defect F14）。**这一层的时间就是它的全部意义**——learned 模型是按「在录制速率下匹配人类速度」挑出来的，滚动的步进节奏正是 anti-bot 系统读的东西——所以 wall time 必须跟着 schedule 走，不能跟着 transport 走。

跟不上的时候，规则是**丢点，不拖时间**：一个 slot 的整段时间在它能被发出之前就已经过去，就跳过它（掉帧，不是慢放），总时长与速度曲线保持诚实，只是采样变粗。**最后一个 slot 永远不会被跳过**——这正是端点不变量在调度器一侧的保证。「被跳过的 slot 意味着什么」属于调用方：`move_to` 干脆不发那个位置；`wheel` 把 delta 折进下一个真正发出的步（增量仍精确求和到 `(dx, dy)`）；`HumanKeyboard.type_text` 与 `HumanMouse.click` 则把积压的事件一起补发——按键可以迟到，但绝不能被丢（否则键就一直按着）。点击尤其如此：`click` 的 schedule 是 `[0.0, click_hold_ms()]` 配 `events = [pressed, released]`，补发的写法让「比 hold 还慢的传输」只会**缩短** hold，结构上不可能放出一个从未按下过的 release。

被 yield 而调用方选择不动作的 slot 依然花掉了它的时间——这就是「重复 waypoint」表达静止的方式：路径里一个与前点相同的坐标，调度器等它、`move_to` 不派发事件，于是 learned 模型里「点击前停在目标上」的那段留白按原样保留，而不被摊成 1 px 的微动。

#### 按键事件不带原生按键码

`send_keys` 与 `HumanKeyboard` 共用 CDP 修饰键位常量（Alt=1、Control=2、Meta=4、Shift=8），不在 watchdog 重写一份映射。`ctrl`/`control`、`meta`/`cmd` 的别名不改变真实修饰键含义；页面收到的 `ctrlKey`/`altKey`/`metaKey`/`shiftKey` 必须与请求一致，后一条普通按键没有修饰键污染。

af 发出的每一个 `Input.dispatchKeyEvent`——`browser.send_keys`（`ActionWatchdog._on_send_keys`）、`human` 节奏的逐字键入与 `type` / `type_coordinate` 的清空（`HumanKeyboard.type_text` / `clear_focused`）——都只带 `key`、`code`、`windowsVirtualKeyCode`、`modifiers` 与 `text`（`commands` 照旧），**不带 `nativeVirtualKeyCode`**。以前三处都把 Windows 按键码同时填成原生按键码。

- **为什么**：2026-10-14 为 LexBench L1 复筛时发现（本地的 `tmp/flywheel/followup/live3/rescreen.md` §4），在 macOS 上，带着原生按键码、页面又没有自己处理的按键，Chrome 会拿去匹配它自己的菜单快捷键，打开一个没有 opener 的 `chrome://settings/help`（"关于 Chrome"）并提到前台：agent 的页面从此是后台页面（`document.hidden` 为 true，600 ms 里的动画帧从约 38 帧降到 1 帧），一直到 run 结束；af 不跟这个 tab（它没有 opener），截图照常，靠动画帧绘制的页面却画不出来（腾讯视频的空白结果页多半就是它），网站也读得到"页面在后台"。16 次站点访问里出现了 10 次，最常见的是在搜索框里回车、表单原生提交。
- **实测**（2026-10-14，Chrome 155 headless，本机页面；探针在本地的 `tmp/flywheel/followup/enter_key/`）：73 种按键序列——`send_keys` 认识的每个键名、字母、数字、标点、带修饰键的组合、逐字键入（含 `\n`、`\t`）、清空——各在文本框聚焦与没有可编辑元素聚焦时发一次。带原生按键码时 29 种打开了它：回车（原生提交表单）、空格、Escape、字母、数字、Page Up / Down 在哪儿都会；Backspace、Delete、方向键、Home、End 在没有可编辑元素聚焦时会（所以清空落在一个焦点被抢走的页面上也会，机核那次就是这样）；回车用 macOS 自己的 Return 码 36 同样会。不带原生按键码时，普通启动与 pinned 的启动下 73 种都不会，而页面在每个 keydown / keypress / keyup 上读到的 `key`、`code`、`keyCode`、`which`、`charCode`、字段的值与导航都与带着时逐项相同：页面读到的这些都来自 `key`、`code`、`windowsVirtualKeyCode` 与 `text`，原生按键码只喂给平台自己的快捷键处理。所以规则是不发，不分平台，也不在按键之后把 agent 的 tab 提回前台（那只是掩盖）。
- 带修饰键的组合本身的浏览器语义不变：表单里的 `meta+enter` / `shift+enter` 像真实浏览器一样把提交结果开在新 tab / 新窗口里（页面内容，不是浏览器自己的页面；Chrome 报告了 opener 时由"页面新开的标签页"的规则决定跟不跟）。
- 所以 Chrome 自己的快捷键从不经按键事件执行：后退、前进、重新加载、关闭与切换 tab 由 capability 按固定的表执行，键不发给页面（上面"浏览器自己的快捷键"）。
- **回归**：`test_browser_input/test_key_events.py`（不用 Chrome：三条路径发出的每个按键事件都不带原生按键码，`key` / `code` / `windowsVirtualKeyCode` / `text` 不变）；`test_browser_keys_live.py`（真实 Chrome，fixture 的 `keys` 页，经 capability 自己的动作：文本框里回车原生提交、`send_keys` 认识的每个键与几种组合在没有可编辑元素聚焦和在文本框里、React 式的处理函数收到 `key` 为 Enter 且 `keyCode` 13、`human` 节奏键入与带回车的提交、两种节奏下在没有字段的地方输入（2026-10-22 起动作失败、一个键也不发）——之后浏览器只列出 agent 的 tab，页面 `visibilityState` 是 `visible`，动画帧在跑；把原生按键码放回去，这个文件里 23 条失败（2026-10-22 重测：以前的 24 条里有一条是没有字段时的清空，它现在不再发键）；`test_browser_form_submit_live.py` 的回车提交（录制的形状）另断言那一步 `browser_meta` 的 tab 列表只有结果页。

#### `learned`：meta.json 对模型参数有最终解释权

`learned` 的 `weights_dir` 指向一个导出目录（`generator.npz` + `meta.json`）。**里面是哪个模型、以什么 tick 率、在多大的归一化窗口里训练的，一律从 `meta.json` 读，不从 YAML 读**（defect F7）：`format` / `kind` 决定走哪套 numpy 推理；`hz` 与训练窗口只要 meta 声明了就以 meta 为准，而 `mouse_trajectory` 块里写了不同的值会在**构造时**报错并同时指出双方，而不是静默地把模型按错误的速率或错误的尺度回放。理由与受体那条同源：配置不得与它所指的权重互相矛盾，而当两者必有一错时，权威属于产出权重的那一侧。

新训练出来的模型（比如一个 diffusion 导出）**不是一个新策略**，而是 `learned` 的一个 backend，按导出格式分派——这与 capability 的 backend 是同一个模式。真正的新策略是另一种运动模型：加一个类文件，加一个 wiring key，消费者零改动。

numpy 来自可选 extra `learned-mouse`，只在构造 `LearnedTrajectory` 时导入，绝不在模块导入时——所以没装 extra 的机器上注册与 config 生成照常工作。`input/_timing.py` 和 `input/_aim.py` 被 `learned` 导入，因此这两个模块必须保持 **numpy-free**。

### 4.14 回放与归档中间层（recorder 连接、`replayability`、browser 归档器）

Browser 实现了 §4.5 的第三条 provides 轴（`browser.network` / `browser.storage` / `browser.dom_raw` / `browser.frames`；回放 run 用 `browser.replay_log` 代替 `browser.network`），另有三根自己的环境策略轴：`replayability`（`none` / `pinned`，录制的可回放档位）、`network_source`（`live` / `archive`，网络从哪里来，M30）与挂在 `archive` 来源上的 `miss_policy`（`strict`，M30）。它们共用一条独立于 agent 的 CDP 连接，即 **recorder 连接**（也叫 early-attach 连接）。这一层的设计依据来自两轮实测（S1、S2a，Chrome 153.0.8010.53，headless，macOS；探针脚本在本地的 `tmp/flywheel/spikes/`）。前半部分是录制（M29），"回放"之后的几节是离线回放（M30）。下面引用的 Chrome 行为都会随版本变化，`requires_chrome` 的回归测试把它们钉成版本金丝雀。

```
BrowserCapability
├── 控制连接（agent 的）：tab attach、观测 scope、JS scope、动作、viewport/DPR
│     永不设置 waitForDebuggerOnStart: true
└── recorder 连接（RecorderConnection，browser/recorder.py；只在需要时打开）
      ├── browser 级 Target.setAutoAttach{autoAttach, waitForDebuggerOnStart, flatten}
      ├── 每个新 target：整批准备命令（replayability → 回放服务 → 归档器 → Fetch / 递归 auto-attach）按序写出，
      │     紧接着写 resume（finally 里），之后才等全部回复
      ├── 暂停类事件 → 直接派生的 task（finally 里 continue / resume；archive 来源回放时未应答的请求一律 failRequest，从不 continue），从不进 scope 队列
      ├── 回放服务 ReplayServer（network_source: archive，M30）：Request 阶段的 Fetch 暂停的主人，
      │     匹配 → fulfill / failRequest，在暂停它的那个 session 上应答；决定写进回放日志 → browser.replay_log 的 replay.jsonl.gz
      └── 贡献者声明的事件 → 本地分发器（逐贡献者准入：带 session id 的小事件，字节进它的侧存储）→ browser 级 archive EventScope
                                                       ← ArchiveWatchdog（shared，provides ∅）
                                                         → 已绑定归档器的运行期会话
```

#### 为什么是第二条连接

- **消息上限**：控制连接的 `max_frame_size` 是 100 MiB（af 自己的设置），超过就以 1009 关掉整条连接，所以一个大响应体或大上传（`Fetch.requestPaused` 把 POST body 带两份：`postData` 和 base64 的 `postDataEntries`）不能走控制连接，否则会带走 agent。Chrome 自己的上限分方向：它**读入**的一条 CDP 消息至多 100 MiB（DevTools HTTP server 的读缓冲，约合 75 MiB 的 base64 body），超过直接断开 socket，这只约束发给 Chrome 的消息，也就是回放的 `Fetch.fulfillRequest`（`max_fulfill_body_bytes`，下面"回放"）；它**发出**的一条消息至多 256 MiB（发送缓冲），超过的那条回复被悄悄丢掉、连接不断（2026-10-12 实测，Chrome 155：150 MiB 的 body 照常收到，200 MiB 的 body 没有回复）。所以 100 MiB 不限制录制：recorder 客户端不设帧上限，录制时哪些 body 存不下，由 `browser.network` 自己的分档决定（下面"体积分档"）。
- **队头阻塞**：大帧会堵住 agent 的命令回复。
- **暂停**：被 Fetch 扣着的请求一直卡住页面，直到有人应答；这条通道需要自己的事件处理，不能排在观测后面。
- **早接管**：只有 browser 级 auto-attach 加 `waitForDebuggerOnStart`，才能在弹窗、worker、OOPIF 的第一个请求和第一个脚本之前做好准备；控制连接要到焦点移过去（`switch_tab`，或下面"页面新开的标签页"的跟随）才接管弹窗。

recorder 连接的 `CDPClient` 用 `compression=None`（websockets 默认与 Chrome 协商 permessage-deflate，大帧慢 13–48 倍）、`proxy=None`（websockets 默认读 `http_proxy`，loopback 的 CDP 流量会绕道本机代理）、`max_frame_size=None`。控制连接对 loopback 端点（`localhost`、`127.0.0.0/8`、`::1`）同样用 `proxy=None` 与 `compression=None`（`cdp_client.transport_options`）：M29 实测（Chrome 153，同一个 target 经两个客户端各取 9 次）23 MB 的 `DOMSnapshot.captureSnapshot` 中位数 1066 ms 对 deflate 的 1237 ms（p90 1083 对 1318），300 个元素的页面 15.2 对 18.0 ms，两边的回复 sha256 相同。逐字节不变而且确实更快，所以关掉。远端端点保持 websockets 的默认（环境代理、压缩），没有在真实网络上测过。

#### 什么时候打开

- 绑定了任一 browser 归档器、`replayability` 不是 `none`，或 `network_source` 拦截（`archive`，M30）：setup 在建第一个 tab 之前打开它。回放必须绑定 `browser.replay_log`（下面的协作检查），所以回放时 archive scope 与 ArchiveWatchdog 总在。
- 只开了 `replayability`、没有归档器：**只做 shim** 的模式：不建 archive scope，不装 ArchiveWatchdog，不开 Fetch，不挂任何 Network 事件处理器（pinned 为了让缓存禁用与 SW 旁路生效会在各 session 上发 `Network.enable`，事件到了 recorder 客户端无人接收、直接丢弃）。
- 两者都没有：不开第二个客户端，不建 archive scope，控制连接上一条命令也不多发。"`archive` 为空就零开销"由 fake CDP 测试钉住（M28 验收里在 M28 时空成立的那一条，在这里重新断言）。

#### 双客户端：事实与规则

| 事实（Chrome 153） | 规则 |
|---|---|
| recorder 的 Response 阶段 Fetch 在 site isolation **开着**时覆盖了 fixture 的全部请求：文档、子资源、302 跳转、带 body 的 POST、OOPIF 的文档（暂停在父页面 session）与子资源（iframe session）、dedicated worker 的请求（暂停在父页面 session）、SW 脚本与它的 fetch、控制连接接管之前的 `window.open` 弹窗 | 不用 `--disable-site-isolation-trials`（它还会改变 OOPIF 的观测）。原先要求加这个开关的那条规则已删除，待用户确认 |
| 持有 browser 级 `waitForDebuggerOnStart` 的客户端**还连着却不再应答**（挂住的 recorder）时，已暂停的 target、Fetch 扣着的响应、**此后浏览器里的每一个新 target**（任何客户端、任何站点）都卡死；被暂停的 target 要等所有等待它的客户端都 resume 才运行，控制连接放不开 recorder 的暂停。socket 真正关闭（即使没做 CDP 清理），Chrome 就 detach 它的 session、新 target 照常运行（M29 复测；S1 记成"不干净的断开"的那次，其实是当时的 `CDPClient.stop()` 从未关闭 socket） | **fail closed**：存活靠推送，丢失即失败；接收循环自己出错时关闭 socket，不留一个不应答的客户端；teardown 先显式清理；只在 capability 自己拥有的浏览器上打开（`NETWORK_CAPTURE`，§4.9.x） |
| 显式清理（关 auto-attach → 放行扣着的请求 → 每个 session `Fetch.disable` → `Target.detachFromTarget` → 关连接）之后，新 tab 正常加载 | teardown 的固定顺序；幂等；在建了一半的实例上也安全 |
| agent-cdp 0.5.0 的 `PausedTarget` 模式 A 按信封 session 正确恢复目标；旧版 browser 级命令会让目标继续暂停，没有恢复另一个 target | recorder 采用模式 B 保持整批准备先写出、再发 resume 的顺序；callback 收到 resume 应答后才清待恢复标记，失败保留供 cleanup 重试 |
| browser 级 auto-attach 也接上 Chrome 内部 target（`browser_ui`、`background_page`、扩展的 `service_worker`）；同一个 SW 会被接上两次（browser 级、注册它的页面之下），而 SW 主脚本的请求**只**在页面之下那个 session 上被拦截（M29 实测：那个 session 没开 Fetch 或被 detach，主脚本就哪儿也不暂停） | 只准备 `page` / `iframe` / `worker` / `shared_worker` / `service_worker` 且 URL 不是 `chrome:` / `chrome-extension:` / `devtools:` 的 target，其余只 resume 再 detach；同一 target 的第二个 session **照样准备**，不丢；于是它的请求可能每个 session 各暂停一次，由贡献者按 target 与请求去重记录（`RecorderConnection.sessions_for_target`） |
| 等待中、还没有页面进程的 target 只立即应答 Chrome 自己处理的命令（`Fetch.enable`、`Target.setAutoAttach`）；`Page.*`、`Emulation.*`、`Network.*`、`Runtime.*` 要等它 resume、第一个文档提交之后才按顺序应答，resume 自己的回复最后到；排着的命令却在那个文档的第一个脚本之前全部生效。这样的 target 是打不到 opener 的 tab（不带 `rel` 的 `target=_blank`（Chrome 88 起默认 noopener）、`rel=noopener`、`window.open(url, '_blank', 'noopener')`）和等待中的 SW；保留 opener 的弹窗先在 opener 的进程里有一个活的 `about:blank`，10–30 ms 内全部应答（2026-10-14 实测，Chrome 155：等 6 s 也只有那两条应答；文档晚 2 s 到，回复就晚 2 s；两个文档、dedicated worker、跨站 iframe 上 shim、时钟、种子、时区、语言、UA、减少动画、缓存禁用都在第一个脚本之前生效，录到的网络完整；探针在本地的 `tmp/flywheel/followup/newtab/`） | 等待中的 target：整批命令按序写出，紧接着写 `Runtime.runIfWaitingForDebugger`，之后才等回复，resume 的也在内（下面"逐 target 的准备流水线"第 3 步）；没有为 SW 单设的规则。以前先等回复（逐条 1 s）再 resume：每个打不到 opener 的 tab 白等 1 s、记 10 条假失败 |
| `Target.createTarget{url}` 在 recorder attach 之前就发出了文档请求：Fetch 没扣住、Network 事件里没有，带着宿主机的 `Accept-Language`、默认 UA、没有 `no-cache`；页面一侧的设置只是碰巧赶上了解析器（2026-10-14 实测，Chrome 155） | capability 自己开的 tab 一律在 `about:blank` 建，recorder 准备好它（`RecorderConnection.wait_prepared`）之后再导航：`create_tab(url)`；setup 的初始 tab 本来就是已存在的或 `about:blank` 的 |
| recorder session 上不先 `Page.enable`，`Page.addScriptToEvaluateOnNewDocument` 返回 ok 却从不执行 | frame 先 `Page.enable`，再注入 |
| dedicated worker 上没有 `Fetch.enable`；它的请求暂停在父页面 session 上 | 不给 dedicated worker 发 Fetch；永远在暂停请求的那个 session 上应答 |
| 第二个客户端的 `Emulation.setTimezoneOverride` 失败（"already in effect"） | 每个 override 只有一个所有者（下表） |
| 时区与语言的 override 在一个渲染进程里也只有一个所有者：最先设它的那个 frame。同进程的另一个 frame 再设语言得到 "Another locale override is already in effect"，再设时区返回成功却不成为所有者，都不记日志；所有者离开（被移除的 iframe、跳到别的站点去的 opener）之后 0.2–0.3 s 内，其余 frame 落回浏览器进程的默认时区与语言（`navigator.language` 按 session 设，不受影响）；保留 opener 的跨站弹窗准备时还在 opener 的进程里，到了自己的进程，从第一个文档起就读默认语言（时区照样 pin 住）。默认时区听启动时的 `TZ`；默认语言在 macOS 上没有办法定（`--lang`、`LANG` / `LC_ALL` / `LANGUAGE` 都不起作用，带 `-AppleLanguages` Chrome 起不来）（2026-10-14 实测，Chrome 155） | pinned 启动浏览器时设 `TZ=<timezone>`（`launch_env()`，与启动开关走同一条路，下面"`replayability`"）；浏览器的默认时区与语言记进环境指纹（`browser_defaults`），回放拒绝默认值与录制不同的宿主机（下面"一致性检查与回放指纹"）；剩下的写进"已知限制" |
| 嵌套的 dedicated worker 脚本在 Request 与 Response 两个阶段都不经过 Fetch | 已知限制：既录不到也回放不了；从 Network 事件看得到的，记成未完成，不当作完整 |

#### 所有权与存活

- **只在明确拥有的浏览器上**：需要 recorder 连接的配置要求 provider 声明 `NETWORK_CAPTURE`（§4.9.x）。本地 `lifetime: run` 由 run 拥有进程；`experiment` 由显式实验 scope 拥有进程与 broker，每 run 拥有 context lease。任意显式 `cdp_url`、注入的控制客户端、六个云 lite provider，仍在 `prepare()` 报错（`check_setup`；`bind_archivers` 绑定前再查与绑定有关的那部分，setup 启动前再查全部），点名双方，一次报全。云 provider 录制另行设计。
- **存活是推送的**：recorder 客户端丢失时保留原始 `RecorderLostError` 供 `collect_archive` 报告，普通录制不使控制连接的命令失败，也不改写正在完成的动作结果。recorder 同时承担 archive replay 或固定 target 配置时，下一次环境操作明确拒绝已失效的执行合同；这属于环境执行不可用，与单纯采集/落盘失败的记录健康分别表达。清理仍释放本次拥有的资源。
- **控制连接永不设置 `waitForDebuggerOnStart: true`**（它的 iframe auto-attach 一直是 `false`），所以 recorder 是唯一需要 resume 的一方。

#### setup 与 teardown

setup：协作检查（`check_setup`，`prepare()` 查过的同一个函数，再查一遍）→ 回放时加载整个源归档（`open_replay`）→ `backend.open(launch_args=..., launch_env=...)`（只对声明了 `LAUNCH_ARGS` 的 provider）→ 控制连接 → 需要时建好 recorder 连接（还不 attach）：它的客户端、archive scope（`__archive__`，与 `__browser__` 平级，归 recorder 的生命周期）与 `ArchiveChannel`；绑定了归档器时，在第一个 tab 主 frame 的 isolated world 里量浏览器的默认时区与语言（任何 override 都还没到，`backends/browser_defaults.py`；回放时在这里与源 run 的比较，不同就拒绝，下面"一致性检查与回放指纹"）；档位要 client hints 时（pinned），在一个后台的临时 `chrome://version` tab 里读浏览器自己的 client hints（上面"`replayability`"）；`replayability` 的本 run 计划（计划用不了读到的 hints，或回放时与源 run 的不同，就在这里拒绝）、已绑定归档器的运行期会话（`archive/run.py` 的 `BrowserArchiveRun`）→ global scope 与 watchdog（ArchiveWatchdog 按绑定安装）→ `RecorderConnection.open()`：设 browser 级 auto-attach，等已存在的 target 准备完（ready barrier）→ 初始 tab → `start_url`。watchdog 在 `open()` **之前**：ArchiveWatchdog 必须从 recorder 的第一个事件起就消费 archive scope，因为 `open()` 期间已存在 target 的 attach 事件必须有实际接收者；缺少处理者是归档缺口，不能被成功 flush 掩盖。

启动期对输入平台、client hints、颜色偏好及默认时区/语言的四种测量共用浏览器包内 `commands.py::BrowserCommands`，只要求带超时的 `send`。控制连接和 recorder 连接都能满足它；测量不要求事件订阅或连接状态，所以不使用 `agent-cdp.CDPCommandProtocol` 的更宽合同，也不为每种测量各立一个 Protocol。

这四种测量都经得起一次慢的回答：每种测量有一个总预算（`capability.py` 的 `_STARTUP_READ_TIMEOUT_S`，30 s，与启动时等 Chrome 端点的 `cdp_wait_timeout` 一样），其中每一次读最多等 Chrome `_STARTUP_READ_ATTEMPT_TIMEOUT_S`（5 s），没等到就放弃这次、再读一次，直到预算用完。平台与 client hints 在临时 `chrome://version` tab 里一直读到页面填好（开 tab 与 attach 只做一次，用剩下的预算）；颜色偏好与默认时区/语言每次重来一遍 attach → 读 → detach（`commands.py::measure_in_attempts`），只在超时时重来，Chrome 拒绝或答得不对就立即失败。预算用完仍没有回答时照旧拒绝（`BrowserCooperationError`；录制时默认时区/语言量不出照旧记 `null`），错误写明预算、读了几次、每次多长。依据：安静的机器上每种测量 2–300 ms（2026-10-23 实测，Chrome 155，headless）；同一天机器 load average 95–195 时，新 tab 的渲染进程 5 s 内没给出 client hints，pinned 录制在启动时失败（那时每种测量只有一个 5 s 的预算）。

teardown：标记不存活 → recorder 显式清理（`Target.setAutoAttach{autoAttach:false, waitForDebuggerOnStart:false}` → 继续所有还扣着的请求 → 每个 session `Fetch.disable` → `Target.detachFromTarget` → 停掉 recorder 客户端）→ 关闭归档会话与 archive scope → 原有的 bridge / watchdog / scope / 控制连接 → `backend.close()`。每一步都容错地往下走，第二次调用什么也不做。

#### 协作检查一览

"能配什么"由生成的配置约束，"能否协作"在这里查（2026-09-24 的裁决）。全部由 `browser/cooperation.py` 的一个函数算出、一次报全，每条点名双方。每一行都只取决于配置与任务数据，所以**全部在 `prepare()` 报**（用户 2026-10-13 的裁决）：`BrowserCapability.check_setup`（§4.9 `SetupCheck`，经 `Environment.check_setup`，§4.2 第 3d 步）带着本任务的 setup context（`cdp_url`、`replay_archive`、`start_url`）、观测协商结果与将要绑定的归档器算全部行，加上回放的一致性检查；错误在 `on_run_start` 打开 trace 之前出去，`spectragent run --dry-run` 也查得出来。取决于启动的这个浏览器本身的只能在启动之后、准备任何 target 之前定（表里写着"启动之后"的两处：OS 在启动之前说不出的默认值，pinned 的标识背后浏览器自己的 client hints）。之后还有两次防御性复查，调的是同一个函数：`bind_archivers` 在绑定前查与绑定有关的行，setup 在启动浏览器之前把全部再查一遍（拦住绕过 `prepare()` 直接 setup 的调用方，与 `prepare()` 之后才注入控制客户端的测试）：

| 组合 | `prepare()` 之后谁再查 |
|---|---|
| 任一 browser 归档器 + provider 没有声明 `NETWORK_CAPTURE`（六个云 lite provider） | `bind_archivers`、setup |
| 任一 browser 归档器（或非 `none` 的档位）+ provider 声明了 `NETWORK_CAPTURE` 却没实现 `persistent_profile()`（说不出 profile 是否新建） | `bind_archivers`（档位：只有 setup）、setup |
| 任一 browser 归档器 + 显式 `cdp_url`（配置里的或任务上下文里的）或注入的控制客户端 | `bind_archivers`（配置里的 `cdp_url`）、setup |
| `browser.network`（`require_replayable: true`）+ `replayability: none` | `bind_archivers`、setup |
| 任一 browser 归档器 + profile 不是新建的 + 该归档器 `allow_session_state: false` | `bind_archivers`、setup |
| 任一 browser 归档器 + profile 排除了 `archive` watchdog | `bind_archivers`、setup |
| `replayability: pinned` + provider 缺 `LAUNCH_ARGS` 或 `NETWORK_CAPTURE` / 显式 `cdp_url` / 注入的控制客户端 / provider 报告的旋钮冲突 / profile 不是新建的 | setup |
| `replayability: pinned` `base_time: from_archive` + `network_source: live`（T0 只能来自正在回放的归档） | setup |
| `network_source: archive` + 没有 `replay_archive`；`network_source: live` + 给了 `replay_archive` | setup |
| `network_source: archive` + 没有绑定记录回放日志的归档器（声明 `replay_log` 需求的那个：`browser.replay_log`；按需求判断，不按 `archiver_id` 分支）——miss 必须总有记录 | `bind_archivers`（绑定了别的归档器时）、setup |
| `network_source: archive` + 绑定了记录线上流量的归档器（声明 `fetch_response` 需求的那个：`browser.network`）——回放没有自己的线上流量，送出的每个字节都已经在源归档里；报错点名 `browser.replay_log`（用户 2026-10-13 的裁决：`browser.network` 永远是"录流量"） | `bind_archivers`、setup |
| `network_source: live` + 绑定了记录回放日志的归档器（`replay_log` 需求：`browser.replay_log`）——live 来源什么也不回放 | `bind_archivers`、setup |
| `network_source: archive` + recorder 所有权（provider 缺 `NETWORK_CAPTURE`、显式 `cdp_url`、注入的控制客户端，与上面同一组行） | setup |
| `network_source: archive` `isolation: required` + provider 没有声明 `NETWORK_ISOLATION`（或缺 `LAUNCH_ARGS`）——除非写了 `isolation: none_acknowledged` | `bind_archivers`、setup |
| `network_source: archive` `isolation: required` + provider 报告的隔离冲突（`local_chrome`：`proxy_server`、`proxy_bypass_list`、`extra_args` 里的网络路由开关、不是 `about:blank` 的 `startup_url`） | `bind_archivers`、setup |
| 回放一致性：源 run 的环境指纹与本 run 的配置不一致，含本机 OS 说浏览器会落回的默认时区与语言（下面"一致性检查"；`prepare()` 只读源 run 的指纹） | setup，启动浏览器之前（之后才加载整个归档）；默认时区与语言在启动之后、准备任何 target 之前按实测再比一次（OS 说不出的那项在这里定）；pin 了标识时 override 带的 client hints 也在那里比（启动之前读不到） |
| `replayability: pinned` + 普通 Chrome 标识与浏览器自己的 client hints 不符（大版本、平台、移动端）；或读不到浏览器自己的 client hints | 只在 setup，启动之后、准备任何 target 之前（取决于启动的这个浏览器，`prepare()` 不知道；上面"`replayability`"） |
| 已协商的 browser fragment + 当前 profile 装的 watchdog 解析不了它（profile 在构造之后被改过；当场重新解析） | setup |
| 任务的 `start_url` + setup 只打开一个完整的 http(s) URL：没有协议头、协议头不是 `http`/`https`、没有主机、含空白、不是合法的 URL（`start_page.py`，§4.10 "起始页"；这一行与配置无关，只看任务数据） | setup，启动浏览器之前 |

#### 逐 target 的准备流水线

每个 `Target.attachedToTarget` 在派生的 task 里处理（它本身就是暂停类事件）：

1. 去重与过滤（上表）。
2. 收集本 target 的命令：`replayability` 的本 run 计划先给（frame：`Page.enable`、`Page.addScriptToEvaluateOnNewDocument(shim)`、时区 / 语言 / UA / 媒体 override、`Network.enable` 后的 `Network.setCacheDisabled` 与 `Network.setBypassServiceWorker`；worker：`Runtime.evaluate(shim)`、`Network.enable`、`Network.setUserAgentOverride`、`Network.setCacheDisabled`），然后是回放服务（M30，只在 `network_source: archive` 时存在：`Network.enable`、`Network.setCacheDisabled`，frame 上再加 `Network.setBypassServiceWorker` 与拒绝 SW 注册的注入脚本），再给每个已绑定归档器的会话（`browser.network`：`Network.enable{maxPostDataSize}`；`browser.replay_log` 什么都不发），最后是 recorder 自己的：所有贡献者的 Fetch pattern 合成**一次** `Fetch.enable`（只在有 Fetch 的 target 上；回放服务的 Request 阶段 pattern 也走这条路），递归的 `Target.setAutoAttach{autoAttach, waitForDebuggerOnStart, flatten}`。
3. 先写出，再 resume，最后才等回复。等待中的 target：整批命令按收集的顺序一条接一条写到 socket（`CDPClient.post`：写出就返回它的回复，回复之后再等；同一个 task 里先后写出的命令按这个顺序到 Chrome，不靠 task 的启动顺序），紧接着写 `Runtime.runIfWaitingForDebugger`，写之前一条回复也不等。之后等每一条回复，resume 的也在内，最长等到 recorder 自己可能扣住这个 target 第一个文档的时间（最长的贡献者预算加 1 s 宽限，或默认的 15 s）再加 15 s（`RESUMED_REPLY_MARGIN_S`）：没有页面进程的 target 等第一个文档提交才应答，而那个文档的 Response 阶段暂停正是 recorder 扣着的。失败与超时记日志、记在 target 上（`RecorderConnection.failures`，只作诊断），从不抛错；`close()` 取消时，没等到的回复丢掉。不在等待的 target（`open()` 时已存在的，ready barrier 等它们）照旧：整批写出，逐条在命令超时（1 s）之内等。`Runtime.runIfWaitingForDebugger` 用 `PausedTarget` 模式 B 放在 `finally` 里：命令建不出来、写不出去也照样 resume。实测（2026-10-14，Chrome 155，fixture，每种打开方式 3 次，从点击动作开始计到 fixture 收到文档请求）：打不到 opener 的 tab 21–70 ms（以前 1.02–1.05 s），每个 tab 0 条超时失败（以前 10 条；文档 2 s 才回的那个以前还多一条 resume 超时）；保留 opener 的 tab 与以前一样 20–55 ms，各有一条真实的失败（`setLocaleOverride` "already in effect"，上面的事实表）。Playwright 的 `FrameSession._initialize`（`crPage.ts`）也是把 `Page.enable`、注入脚本、各种 override 与 `Runtime.runIfWaitingForDebugger` 一起发出、再一起等。
4. capability 自己开的 tab（`create_tab(url)`）在 `about:blank` 建，`RecorderConnection.wait_prepared` 等到 recorder 准备完它（attach、整批应答、resume）再导航，所以第一个请求被扣住、录下、pin 住（上面的事实表）。

贡献者互不认识，recorder 也不按归档器 id 分支：它只问每个贡献者"这个 target 要发什么""你要哪些事件"。贡献者实现 recorder 的 `RecorderParticipant` 协议（`target_commands`、`fetch_patterns`、`event_methods`、`admit_event`、`on_event`、`pause_budget_s`、`on_paused`、`on_cycle`、`serves_requests`（M30），基类全部默认 no-op / `False`）：`replayability` 的本 run 计划是一个，回放服务是一个（M30），每个已绑定归档器的运行期会话各是一个。recorder 交给 archive scope 一侧的东西叫 `ArchiveChannel`（scope、贡献者、发出 / 处理计数与收集屏障），它只在绑定了归档器时存在。

#### 事件：暂停的进 task，元数据进 scope，字节进侧存储

- **暂停类事件**（`Fetch.requestPaused`、`waitingForDebugger` 的 `Target.attachedToTarget`）在 recorder 的回调里直接派生受跟踪的 task，**绝不进 scope 队列**：普通 scope 队列有容量限制，拒收一个 `requestPaused` 就可能让页面卡住。暂停的主人（Response 阶段是 `browser.network` 的会话）在超时之内读 body。超时是主人自报的预算（`pause_budget_s`，`browser.network` 是 `2 × command_timeout_s + body_read_timeout_s`：POST body 的补取与开不了的流各一条命令，再加内联读）再加 1 s；不报预算的主人用 recorder 的默认 15 s，旋钮因此不会被一个更短的全局上限悄悄截断；**continue 由 recorder 在 `finally` 里发**，不依赖主人记得。回放时规则相反（M30）：只要有一个贡献者声明 `serves_requests`（回放服务），recorder 对 Request 阶段任何没有被应答的暂停（主人超时、主人抛错、找不到主人、teardown 时还扣着的）一律发 `Fetch.failRequest{errorReason: "Failed"}`，**从不 continue**：今天唯一的回放服务属于 `archive` 网络来源，它不把请求放到线上（不同于 mitmproxy 的默认 `forward` 与 WARC-Bench 对 Sentry / Datadog 的 `route.continue()`）。这是 `archive` 来源自己的规则，不是框架的定律（下面"回放：网络来源与 miss 策略"）。主人自己发出 fulfill / fail 并成功之后才把 `PausedRequest.answered` 置位，所以发送失败的那个仍由 recorder 兜底失败。
- **元数据事件**：哪些 CDP 事件进 archive scope，由贡献者自己声明（`event_methods()`；`browser.network` 声明 `Network.*` 的请求 / 响应 / ExtraInfo / 结束 / 失败与 `webSocket*`），recorder 在 `open()` 时订阅当时贡献者声明的并集。和 Fetch pattern 一样，采什么在 recorder 打开时定下；它自己只认 target 的 attach / detach 簿记。recorder 模块里的本地分发器在客户端的接收循环里把每个原始事件交给声明了它的贡献者准入（`admit_event(method, params, session_id)`，同步）：贡献者返回要交给自己的小事件（带 session id），或 `None`；分发器在事件上记下它的 `consumer`，发进 archive scope。ArchiveWatchdog 在 scope 的循环里把准入的事件只交给准入它的那个贡献者（它可能指向那个贡献者的侧存储），target 簿记交给每个贡献者。准入时抛错与处理时抛错一样记进 `ArchiveChannel.errors`，下一次收集因此抛错。不用 `CDPEventBridge`：它的事件工厂拿不到 session id（让 agent-cdp 的 bridge 带上 session id 是以后的增强，M29 不给它打补丁）。
- **字节从不随事件走**（archive scope 不保留事件历史）：响应体、POST body、WebSocket 帧的内容、原始 DOM 采集放在会话自己的侧存储里。流式 body 的块（`Network.dataReceived` 的 `data`）在准入时直接追加进它所属的流，准入什么也不交出，块从不进 scope；Chrome 对每个请求都发 `dataReceived`（没开流的不带 `data`），不属于任何流的在准入时丢掉。准入就是分界：`browser.network` 的准入（`archive/network_model.py` 的 `admit_network_event`）把 `requestWillBeSent` 的 `postData` / `postDataEntries` 与帧的 `payloadData` 留在会话的侧存储里（按准入事件的 `event_id`，事件处理时取回），归档从不保存的值根本不进 scope：剥离的请求头只留名字、值置空（名字照样进台账），ExtraInfo 只留 `requestId`、`headers`（与 `statusCode`），`associatedCookies` / `blockedCookies` / `exemptedCookies` 都不带，响应里重复的原始请求头（`requestHeaders` / `requestHeadersText`）去掉。原始事件本身不被修改（多个贡献者共用它）。
- **收集前缀**：archive scope 显式配置 `queue_maxsize=65536`、`max_history_size=0`，业务标签通过 `metadata` 传入，未知参数直接拒绝。`ArchiveChannel.flush()` 使用 agent-cdp 原生 `scope.flush()` 固定已接收前缀，不发占队列名额的空事件。公开 `scope.stats` 与 flush 的逐投递结果区分完成、失败、取消、拒收、未完成；同时核对 AF 实际发出/处理的元数据与原始 DOM 轮次，收集失败不能伪装成成功空归档。它只报告记录健康/缺口，不把采集或落盘故障写成动作失败。

#### override 的所有者

| 设置 | 所有者 | 怎么设 |
|---|---|---|
| viewport + DPR | 控制连接 | `Emulation.setDeviceMetricsOverride`（`deviceScaleFactor` = `replayability.device_scale_factor`（属性）；`none` 是 0，保留平台 DPR） |
| 时区、语言 | recorder | `Emulation.setTimezoneOverride` / `setLocaleOverride`（每个 frame target，含 OOPIF）。一个渲染进程里只有最先设的那个 frame 是所有者，其余 frame 在它离开后落回浏览器的默认值（上面的事实表）：默认时区由 `replayability.launch_env()` 的 `TZ` 定成 pin 的时区；默认语言是宿主机的，记进指纹（`browser_defaults`）、回放时比较 |
| UA、UA metadata（client hints）、`Accept-Language` | recorder | frame：`Emulation.setUserAgentOverride`；worker：`Network.setUserAgentOverride`（worker 继承页面的 UA，但不继承 `Accept-Language`：不单独设，worker 里的 `navigator.language` 是宿主机的；macOS 上 `--lang` 对 worker 不起作用）。标识是 Chrome 自己的形式时（`user_agent: null`，即浏览器自己的 UA；或普通的 Chrome 标识，`client_hints.py` 的 `chrome_identifier`），两条 override 都带 `userAgentMetadata`：浏览器自己的 client hints，setup 在启动之后、recorder 准备任何 target 之前读（下面"`replayability`"）。页面里的 `navigator.userAgentData` 与 `Sec-CH-UA*` 因此就是这个 Chrome 在同样的启动下自己给的：低熵的 brands（它自己版本的，带它自己的 GREASE brand）、平台与移动端，以及给了 `--user-agent` 启动的 Chrome 自己报的空的高熵值（架构、位数、完整版本列表、平台版本、形态；实测）；每个请求都一样，打不到 opener 的 tab 的第一个请求也一样（它带的本来就是 Chrome 自己的），录制与回放相同。别的形式的标识（自定义字符串、Edge、Firefox……）不带：`navigator.userAgentData.brands` 为空、不发 `Sec-CH-UA`，例外是打不到 opener 的 tab 的第一个请求，它带着 Chrome 自己的 brands（这个头在 tab attach 之前就算好了，任何 per-tab 命令都改不了），录制与回放相同，回放时这个请求也不出浏览器（2026-10-14 实测，Chrome 155；已知限制） |
| 媒体特性（减少动画、录制时测出的颜色主题） | recorder | `Emulation.setEmulatedMedia`；颜色主题录回沿用同一值（下文） |
| 注入脚本（shim） | recorder | frame：`Page.enable` 之后 `Page.addScriptToEvaluateOnNewDocument{runImmediately: true}`（准备时已经在的文档，比如初始 tab 的 about:blank，也被 pin）；worker：resume 之前 `Runtime.evaluate` |
| 缓存、SW 旁路 | recorder | `Network.enable` 之后的 `Network.setCacheDisabled(true)`、`Network.setBypassServiceWorker(true)`（M29 实测：没有 `Network.enable` 的 session 上这两条返回成功却不生效）；pinned 档位发，回放服务不论档位也发（M30：回放时 SW 一律拦住，否则 SW 代理的导航以 `XHR` 暂停在 SW 的 session 上，主 frame 文档 miss 就分不出来）。不做每 run 一次的 `Network.clearBrowserCache`：pinned 的 profile 每 run 新建（协作检查拒绝别的），缓存本来就是空的，而清空一个空缓存实测每次 setup 要 3.9–9.3 s |
| V8 随机种子、`--lang`、`--force-device-scale-factor` 等启动开关；浏览器进程的默认时区（`TZ`） | `replayability.launch_args()` / `launch_env()` | 经 `LAUNCH_ARGS` 交给 provider（§4.9.x）；两者都进启动开关表（`launch_flags`），回放在启动之前比较 |
| `Emulation.setVirtualTimePolicy` | 不用（实验性接口） | — |

#### `replayability`：回放档位（`ReplayabilityRegistrar`）

`core/capabilities/browser/replay/`：`replayability.py`（Protocol、注册表、基类、`ReplayabilityPlan`、`default_replayability()`）、`none.py`、`pinned.py`、`shims.py`。它是 `BrowserCapability.__wiring__["replayability"]` 上的受体：类级注解加 Python 默认（`none`），与 `typing_rhythm` 同一种形状。单实例受体在生成的 schema 里是必填的，所以**每一个手写的 capability 块**都写 `replayability: {replayability_id: none}`。这一档原名 `off`，用户 2026-10-13 的裁决改成 `none`：YAML 1.1（PyYAML）把裸的 `off` 读成 `false`，以前必须加引号（§4.2.1 的拼写规则）。

```python
@runtime_checkable
class ReplayabilityProtocol(Protocol):
    @property
    def replayable(self) -> bool: ...              # none → False；策略本身就是开关，旁边没有布尔
    @property
    def needs_archive_clock(self) -> bool: ...     # base_time: from_archive → True（协作检查用）
    @property
    def device_scale_factor(self) -> float: ...    # 给控制连接的 DPR；none → 0.0（平台 DPR）
                                                   # 属性而不是方法：在 pinned 上它就是同名的 YAML 旋钮
    def launch_args(self) -> tuple[str, ...]: ...    # 这个档位拥有的 Chrome 开关；none → ()
    def launch_env(self) -> Mapping[str, str]: ...   # 这个档位给浏览器进程设的环境变量；none → {}
    @property
    def needs_client_hints(self) -> bool: ...      # plan 要不要浏览器自己的 client hints；pinned → True
    @property
    def needs_color_scheme(self) -> bool: ...      # plan 要不要录制时测得的媒体主题；pinned → True
    def plan(self, *, t0_ms: int, browser_user_agent: str,
             run_started_ms: int | None = None,   # run 开始的墙钟时间；None：就是 T0
             client_hints: ClientHints | None = None,
             color_scheme: ColorScheme | None = None) -> ReplayabilityPlan: ...
        # 本 run 的计划（也是一个 recorder 贡献者）：逐 target 的命令、指纹字段、
        # override 带的 client hints 与用不了它们的原因（problems）；配置实例不持有 run 状态
```

- **`none`**（默认）：什么都不做。
- **`pinned`**：旋钮 `base_time: Literal["run_start", "from_archive"]`、`clock: Literal["shared", "per_document"]`（默认 `shared`，见下面 shim 一条）、`random_seed: int`（≥ 1：V8 把 0 当作"用系统随机数"）、`timezone`（IANA，构造时校验）、`locale`（语言标签，构造时校验）、`device_scale_factor`、`user_agent: str | None`（None：沿用浏览器自己的 UA，只 pin `Accept-Language`）、`reduced_motion: bool`。第零条规则：录制与回放用完全相同的 shim、开关、种子、UA、client hints 与 viewport。`Accept-Language` 写成不带 q 值的列表（`en-US,en`），Chrome 自己加 q 值。
  - 启动开关只由它自己的设置生成：`--js-flags=--random-seed=<seed>`、`--lang=<locale>`、`--force-device-scale-factor=<dpr>`、`--disable-back-forward-cache`、`--disable-features=HttpsUpgrades`（Chrome 153 默认先把可注册域名的 http 导航升级成 https 试探；录制与回放都关），以及 `--disable-background-networking --disable-component-update --disable-field-trial-config`（它们在 `CHROME_DEFAULT_ARGS` 里，`ignore_default_args` 会把它们丢掉，pinned 重新加回）；写了 `user_agent` 时另加 `--user-agent=<ua>`（worker 与网络栈也看到同一个 UA，provider 自己的 `user_agent` 旋钮也因此能被报成冲突）。每个 run 必须是新建的 profile。
  - 启动环境只有一项：`TZ=<timezone>`（`launch_env()`，用户 2026-10-14 的裁决）。时区 override 在一个渲染进程里只有一个所有者，所有者离开之后其余 frame 读浏览器进程的默认时区（上面的事实表），`TZ` 让它就是 pin 的时区（实测：`TZ=Asia/Tokyo` 启动，没有任何 override 的页面读到 `Asia/Tokyo`；`:Asia/Tokyo` 也行，`JST-9` 这样的 POSIX 规则让 Intl 报不出时区）。它与开关走同一条路：capability 经 `open(launch_args=..., launch_env=...)` 交给声明了 `LAUNCH_ARGS` 的 provider，`local_chrome` 把它加到 Chrome 从本进程继承的环境上，并写在 `launch_flags` 的最前面（`TZ=UTC`，与 shell 的写法一样），所以启动开关摘要包含它，回放在启动之前就比较。语言没有对应的办法：macOS 上 Chrome 的应用语言取 Cocoa 为 Chrome 包选的本地化，`--lang`、`LANG` / `LC_ALL` / `LANGUAGE` 都不起作用，带 `-AppleLanguages` 的命令行 Chrome 直接退出（退出码 13，实测）；所以默认语言是宿主机的，记进指纹、回放时比较（下面"一致性检查与回放指纹"）。
  - shim（`replay/shims.py`，Python 字符串常量，移植自 WPR `deterministic.js`，BSD-3，保留 notice）：无参 `Date` 与 `Date.now()` 读 `clock` 选的时钟，带参数的 `Date` 构造是原生的。
    - `shared`（默认）：**整个 run 共用的一个时钟**：run 开始时读 T0，之后按真实时间前进，即 `T0 + (performance.timeOrigin + performance.now() − run 开始的墙钟时间)`（两个引用在任何页面脚本运行之前取好；每个文档与 worker 的 `timeOrigin + now()` 是同一个时钟，所以同一 run 的所有 tab 读到同一个时间，忙等照样能结束）。`base_time: run_start` 下 T0 就是 run 开始的墙钟时间，页面读到的是宿主机的时间；`from_archive` 见下一条。
    - `per_document`：WPR 的时钟，每个文档一份：从 T0 起、每 25 次构造前进 50 ms（**不冻结**，忙等能结束）。同样的调用在录制与回放里读到同样的值，代价是 tab 之间时间不一致（下一条）。
    - `clock` 进指纹的参数，回放必须与录制相同（与其余参数同一条规则，下面"一致性检查与回放指纹"）。
  - 其余：带种子的 `Math.random` 与 `crypto.getRandomValues`；`crypto.randomUUID` 是合法的 RFC 4122 v4；拒绝 `ServiceWorkerContainer.prototype.register`（配合 `setBypassServiceWorker`）；防重复注入。时区偏移交给 timezone override（不学 WPR 硬改 `getTimezoneOffset`）；`performance.now` 与定时器不改（WPR 的模型，残余的不确定性写明）。`shim_sha256` 是模板源码的摘要，进指纹。
  - **为什么默认不是 WPR 的计数时钟**（2026-10-22 的修复；iqiyi.com 的录制以 `ObservationFailedError` 结束：`Page.captureScreenshot got no reply within 10s` 两次）。WPR 的时钟每个文档一份：从 T0 起、每 25 次构造前进 50 ms。同一 run 的 tab 因此各读各的时间：run 开始 5 分钟后打开的文档又从 T0 读起，频繁调用 `Date` 的页面跑到几小时之后。iqiyi.com 的 tab 经 `localStorage` 互相同步搜索历史与 SDK 缓存，按时间戳判断谁的副本更新（`storage` 事件 → `setCache` / `syncFromLocalCache` → 写回 `localStorage`，暂停在卡住的 tab 上取到的 JS 调用栈）；各 tab 时钟不同，这个交换永远收敛不了：第二个 iqiyi 搜索页打开几秒之内，所有 iqiyi tab 的页面时钟每墙钟秒前进几百到上千秒（请求里的时间戳参数），渲染进程被 `storage` 事件占满（CPU 155–173%），页面一侧的任何命令都不再应答，观测的截图两次超时，run 结束。复现（脚本化的模型，同一条路径：搜索页 → 新 tab 打开剧集页 → 在剧集页顶栏搜索，打开第三个 tab；探针脚本不入库）：发布时的 shim 3/3 卡死，不绑定任何归档器也一样；`none` 与去掉 `Date` 覆盖的 shim 各 1/1、`shared` 2/2 正常跑完（第三个 tab 打开之后又跑了 6–7 步，约 3.5 分钟）；四次原始失败的网络归档里都有同样的时钟赛跑。只开一个 iqiyi 搜索页的路径不卡。不同文档要读到一致的时间，只能读它们共用的真实时间，所以"同一调用序列读到同样的值"与"所有 tab 一致"不可兼得：默认选后者（`shared`），要逐字节重放页面时钟的回放实验显式选 `per_document`（M30 的回放验收就是这样，`test_browser_replay_live.py` 的 `EXACT_CLOCK`）。
  - **`shared` 的代价**：页面用 `Date` 拼进 URL 的时间戳（`?t=…`）与显示在页面上的时间，在录制与回放之间按时序相差毫秒到秒：URL 只能经站点规则的 `ignore_query` 或模糊级匹配，显示的时间让同路径回放的元素签名与截图对不上。`per_document` 下每个文档最初几次读 `Date` 恰好是 T0，这类请求能精确命中（本地已有的分叉回放里有 413 次这样的命中，另有 6,504 次带时间戳的请求本来就没命中）。实测：M30 的回放验收换成 `shared` 时有 4 条失败（fixture 的时钟页在回放里晚 1.3 s、两次回放的截图不同、新 tab 第一个脚本的报告请求成了 miss），所以它们显式选 `per_document`。回放的页面时钟只在 run 开始与录制对齐，之后按回放自己的节奏前进（回放用录下的回复，通常比录制快）。
  - **观测一侧救不回这种卡死**，所以修在 shim，观测路径不加恢复步骤：实测（Chrome 155，本地页面）主线程卡在一个长任务里时，`Runtime.evaluate` 与 `Page.captureScreenshot`（`fromSurface: false` 也一样）都不应答，`Target.activateTarget` 与 `Page.bringToFront` 应答，之后的截图照样不应答；只有 `Runtime.terminateExecution`（终止页面脚本，改变页面状态）能让它恢复；一串各 50 ms 的任务则不挡任何命令。
  - **残余：iqiyi 的 tab 多了，同步照样失控，与时钟无关**（同一天实测，脚本化的模型，路径延长成 搜索页 → 剧集页 → 搜索页 → 剧集页 → 再开第三个搜索页）：第五个 tab 打开 5 s 之内，所有 iqiyi 渲染进程同时升到 150–190% CPU，各自的内存在约 45 s 里从约 0.6 GB 涨到约 2 GB，暂停取到的调用栈还是 `window.onstorage` → `setItem`，所有 tab 都不再应答，机器负载升到 80–139、swap 用满，观测以 `ObservationFailedError` 结束。`none`（没有任何 shim）与 `shared` 都这样（各 1/1），真实模型的一次重跑（task 18，六个 iqiyi tab，`shared`）也以同样的方式结束。所以 iqiyi 的跨 tab 同步自己就会在 tab 足够多时失控；计数时钟只是把门槛降到了 agent 最常走到的三个 tab。这个门槛之上 run 照样会死：观测一侧救不回（上一条），能止住它的办法都改变页面状态。用户的裁决（2026-10-23）：两种都做成配置，默认都不开——`background_tabs: freeze`（失去焦点的 tab 冻结，回到焦点之前设回运行，§4.14 "离开焦点的 tab"）与 `local_chrome.background_throttling: true`（不再带 `--disable-background-timer-throttling` 等三项，§4.9.x "后台 tab 的节流"）。实测只有冻结止得住：五个 iqiyi tab、只冻结的 run 跑完全部步数，只节流的照样失控（§4.14 "离开焦点的 tab" 的 iqiyi 实测）。在焦点 tab 不应答时关掉同源的其他 tab 没有做。
  - **残余**：`Math.random` 与 `crypto` 仍是每个文档一条从同一种子开始的流，同一 run 的两个 tab 抽到相同的随机数。本次没有站点因此出错（去掉 `Date` 覆盖、保留随机数的 shim 在 iqiyi 上正常跑完），没有改。
  - 回归：`test_browser_pinned_clock_live.py`（真实 Chrome，本地页面，`shared`：一个调用了 20 万次 `Date` 的 tab 与一秒后打开的 tab 都读宿主机的时间；两个 tab 经 `localStorage` 按时间戳同步、拒绝"来自未来"的副本，各写一次就收敛；回放那样平移的时钟在 V8 里读 T0 再按真实时间前进。反面对照：同样的两个 tab 在 `per_document` 下互相改写、永不收敛，实测 2 s 里每个 tab 写 18 次，第一个 tab 比宿主机快 396 s）；`test_browser_recorder_live.py` 的 `TestPinnedShims`；`test_browser_replay_isolation.py`（回放的 shim 带录制的 T0 与回放自己的开始时刻）；`test_browser_replayability.py`；`test_browser_replay_live.py`（`per_document` 的逐字节重放）；`test_browser_new_tab_live.py` 也选 `per_document`：新 tab 第一个脚本读到的恰好是 T0，证明 shim 的 `Date` 先于任何页面脚本到位（`shared` 下原生的 `Date` 读到的是同一个时间，作不了证据；两种时钟是同一个 `Date` 替换）。
  - `base_time: from_archive` 只与回放归档的网络来源一起有效：T0 取自源 run 指纹里的 `replayability.t0_ms`，setup 在 `plan()` 之前解析好，shim 因此在任何 target 准备之前就带着录制时的 T0（M30）；`plan()` 同时收到回放自己开始的墙钟时间（`run_started_ms`），页面在回放开始时读到录制的 T0，之后按真实时间前进；与 `network_source: live` 一起在 `prepare()` 报错（`check_setup`）。
  - 协作检查（`prepare()` 时，setup 再查一遍；点名双方，一次报全）：provider 没有声明 `LAUNCH_ARGS` 或 `NETWORK_CAPTURE`；显式 `cdp_url`；注入的控制客户端；provider 报告的旋钮冲突（`local_chrome`：`deterministic_rendering=true` 的 V8 种子与 pinned 的 `random_seed` 不同、`force_device_scale_factor` 与 pinned 的 DPR 不同、`user_agent` 与 pinned 的不同、`extra_args` 另设了 pinned 拥有的开关；每一行都是值相同就放行，`deterministic_rendering` 也一样（用户 2026-10-13 的裁决），它的 `--deterministic-mode` 与 `--font-render-hinting=none` 不是 pinned 拥有的开关）；profile 不是新建的（`persistent_profile()` 非空：`user_data_dir`、非默认的 `profile_directory`、`extra_args` 里的 `--user-data-dir`；profile 目录只由 YAML 决定，以前的环境变量 `CHROME_USER_DATA_DIR` 已删掉（同一次裁决），因为它让 run 是否从新建的 profile 启动在 YAML 和 `config.json` 里都看不出来）；`base_time: from_archive` 而网络来源是 `live`。
  - **client hints**（`core/capabilities/browser/client_hints.py`，用户 2026-10-14 的裁决）：pinned 的 UA override 不带 `userAgentMetadata` 时，页面读到的 `navigator.userAgentData.brands` 是空的、请求不带 `Sec-CH-UA`，而同版本的真实 Chrome 会列出 brands，网站看得出标识不一致（2026-10-14 复筛实测，Chrome 155）。所以 `needs_client_hints` 为真：setup 在启动之后、recorder 准备任何 target 之前读浏览器自己的 client hints（`measure_client_hints`：`navigator.userAgentData` 只在 secure context 里有，初始 tab 的 `about:blank` 不是（实测），所以在一个后台的临时 `chrome://version` tab 里，从 isolated world 读 `getHighEntropyValues`，读完关掉，等 Chrome 不再列出它才返回，那时还没有接管任何 tab），交给 `plan()`。标识是 Chrome 自己的形式时（`user_agent: null`，或 `Mozilla/5.0 (<平台>) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/<版本> [Mobile ]Safari/537.36`，后面什么都不跟；Edge、Opera、`HeadlessChrome` 都不算），frame 与 worker 的 override 都带上读到的值，原样；普通 Chrome 标识的大版本、平台（`Macintosh` → `macOS` 等）或移动端与读到的不符时，计划带着 `problems`（每条点名两边），setup 以 `BrowserCooperationError` 拒绝这个 run，准备任何 target 之前；预算内读不到也拒绝（override 否则什么都不带；预算与重读见下面"setup 与 teardown"）。别的形式的标识不带。实测（Chrome 155，macOS）：headless Chrome 自己的 brands 是 `"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"`（不是 `HeadlessChrome`），平台 `macOS`，不是移动端；不给 `--user-agent` 启动时高熵值是真实的（`arm`、`64`、`155.0.8059.80`、`27.0.0`、`Desktop`），给了 `--user-agent` 启动（写了 `user_agent` 的 pinned）时 Chrome 自己就把高熵值报成空的，带上它们的 override 与 Chrome 不带 override 时的头和页面读数逐项相同；带着 Chrome 自己 hints 的 override 下，Fetch 在 Request 阶段送出的请求（回放）也带着同样的 `Sec-CH-UA*`。计划的指纹多一个 `client_hints`（override 带的值，CDP `UserAgentMetadata` 的形状；不带时是 `null`），环境指纹的 `page.userAgentData` 是它的低熵部分；回放比较它们（下面"一致性检查与回放指纹"）。
  - 已知取舍：pinned 会改变活站点的行为，本身也是 anti-bot 能抓到的特征（用户已接受）；评测 run 与 anti-bot run 保持 `none`。

#### 颜色主题：录制时测量，录回沿用同一值

用户 2026-10-14 裁决：不默认指定浅色。`pinned` 在录制启动后、准备任何业务 target 之前，测出这个浏览器当前的 `prefers-color-scheme`（`light` 或 `dark`），随后在本次运行的每个 frame target 上固定该值；这样即使宿主机中途切换外观，录制期间也不会换主题。它是本 run 测到的事实，不是一个从宿主机变化中重新读取的每步配置。

该值作为 `replayability.color_scheme` 写进环境指纹。回放在启动前从源归档读出它，所有 frame target（含新标签页与进程外 iframe）都用源值进行媒体模拟，保留现有减少动画的设置。回放宿主机的当前主题可以不同，不因此拒绝回放；回放自身的指纹记录实际沿用的源值。测量失败时明确拒绝 pinned 录制，源归档没有该字段或值非法时明确拒绝 pinned 回放，不能把缺失猜成浅色，也不回写旧归档。`none` 档位保持宿主机行为。

这只固定浏览器的媒体主题；站点自己存在 Cookie、账户偏好或页面按钮里的主题选择仍由站点状态决定，不声称所有网页都强制成同一种颜色。主题测量与准备由 browser capability 和 replayability 的现有准备通路负责，不放进 Loop 或 TraceWriter。真实 Chrome 回归须覆盖录制测量、异主题宿主上的回放、新 tab / iframe，以及媒体主题与已有 reduced-motion 设置同时生效。

实现：`color_scheme.py::measure_color_scheme` 在启动时第一个页面的 `af-color-scheme` isolated world 中分别查询深色、浅色媒体条件，只接受恰好一个匹配；随后 detach 测量 session。`ReplayabilityProtocol.needs_color_scheme` 声明计划是否需要这个事实，capability 在录制时测量，在回放启动前经 `replay/consistency.py::replay_color_scheme` 从已加载的源指纹取值。它作为 `plan(color_scheme=...)` 的本 run 参数进入 `ReplayabilityPlan`，不进入 YAML 参数。`pinned` 将主题和现有 reduced-motion 特性合成同一条 `Emulation.setEmulatedMedia`，由 recorder 的准备链发送到每个 frame target；`none` 不测、不发、不新增主题指纹。

#### Browser 归档器：配置实例与运行期会话

- **`BrowserArchiverBase(ArchiverBase)`**（`archive/base.py`）：配置实例只持有旋钮（YAML 构造、trace hook 持有、`bind_archivers` 交给 capability），从不持有 session 或 scope。它声明要哪些通道（`needs: ClassVar[frozenset[ArchiveNeed]]`，取值 `"fetch_response"`（线上流量：拦截的网络来源拒绝有它的归档器）/ `"raw_dom"` / `"observation_cycle"` / `"replay_log"`（M30：记下回放服务的决定，`browser.replay_log` 有它；回放的协作检查按它找记录回放日志的归档器，live 来源拒绝有它的归档器）/ `"action_boundary"`（每次 `env.act` 碰页面之前，归档侧直接把编了号的 `ActionBoundary` 交给要它的会话，动作在预算内等它们，`browser.frames` 有它；见下面 `browser.frames`）），并用 `open(ctx) -> BrowserArchiveSession` 造出本 run 的运行期会话（纯构造，不发 CDP）。每个 browser 归档器都有 `allow_session_state: bool = False` 旋钮。
- **`BrowserArchiveContext`**（frozen，仿 `WatchdogContext`，capability 在 setup 时组装）：recorder 句柄（按 session 发命令、target ↔ session 映射、浏览器版本）、archive scope、原始采集仓库、tab 列表、当前 cycle、时钟、隐私事实（profile 是否新建）、回放服务的日志（`replay_log: ReplayLogFeed | None`，M30，只在回放时有：`drain()` 交出可直接写成 JSON 行的条目，归档一侧因此不 import 回放的类型）。
- **`BrowserArchiveSession`**（运行期会话的基类，是一个 `RecorderParticipant`，方法默认 no-op）：`target_commands(target)` / `fetch_patterns(target)`（准备流水线的贡献）、`on_event(event)`（archive scope 里的元数据，同步）、`on_paused(paused)`（暂停的主人才收到：Response 阶段是要 `fetch_response` 的会话）、`on_cycle(cycle)`（只给要 `observation_cycle` 的会话）、`collect() -> list[ArchiveFile]`、`aclose()`；另有两个不经 recorder、由 `BrowserArchiveRun` 直接调用的成员：`on_cycle_committed(cycle)`（cycle 提交时同步告知每个会话，失败的 cycle 不告知）与 `on_action_boundary(boundary)`（只给 `wants_action_boundaries` 的会话，带 `action_boundary_budget_s`；下面 `browser.frames`）。
- capability 遍历已绑定的实例与它们的会话，**不按 `archiver_id` 分支，不查注册表**（receptor hygiene Rule 2）。通道在 setup 时按当时绑定的需求并集打开；setup 之后再绑定只改变谁收事件、谁被收集（会话按实例保留，新实例开新会话，解绑的会话随 run 关闭），不给已准备好的 target 补开通道（runtime 只在 setup 之前绑定）。setup 时没有绑定任何归档器、没有开 archive 通道的 capability，之后再绑定归档器在 `bind_archivers` 报错，因为那样的绑定什么也采不到。
- **运行期的归档侧**（`archive/run.py`，`BrowserArchiveRun`）：capability 在 setup 时建它、teardown 时关它；它持有 channel、本 run 的会话、`RawCaptureStore` 与指纹状态，`collect` 按下面的顺序收集。capability 只负责接线（建 scope、组装 context、调用它），不在自己的模块里写归档逻辑。
- **原始 DOM 的待定槽位**在 `_ObserveCycleCache.raw`（§4.12）：DOM resolver 的 sink 与 dom_raw 自采（经 context 的 `park_raw`）都放进当前 cycle 的槽位；每次尝试开始时清空（重发的那次替换上一次），`_populate_cycle` 成功时由 `BrowserArchiveRun.commit_raw` 补上 cycle、tab、viewport、cleaner 键后提交进 `RawCaptureStore`，失败的 cycle 什么也不留。sink 只在 setup 时打开了 `raw_dom` 通道时才装；装了之后，当前没有绑定要 `raw_dom` 的归档器时它什么也不放。
- **ArchiveWatchdog**（`watchdogs/archive_watchdog.py`，`level="shared"`，`provides=∅`，先例是 `JavaScriptExecutionWatchdog`）：**按绑定安装，不按 profile**。`BrowserWatchdog.should_install(ctx)` 是新的类方法（默认 True），capability 在建 watchdog 的地方求值；ArchiveWatchdog 只在 `ctx.archive` 存在（recorder 开着且绑定了归档器）时返回 True，从没有 recorder 的 context 也能惰性地建出来。它在 archive scope 上把准入的事件交给准入它的会话（target 簿记交给每个会话），并给每一个记一次"已处理"（屏障由 `ArchiveChannel` 自己应答）；某个会话处理事件时抛错，记进 `ArchiveChannel.errors`，不打断别的会话。有会话要 `observation_cycle` 时，它覆盖 `bind_tab_scopes(observation_scope, js_scope, *, session_id)`，把 tab 观测 scope 的 `BrowserStateRequestEvent` 跨 scope 连到 archive scope（`target_scope=`），`unbind_tab` 丢掉每 tab 的状态。tab 的 scope 仍然只有观测与 JS 两个，为每 tab 第三个 scope 设计的 `TabScopes` 组合对象不做：archive scope 是 browser 级的（弹窗的流量在控制连接接管它之前就到了），那个动机已经不存在，到下一个真正 per-tab 的 scope 再泛化 `bind_tab_scopes`。
- **`collect_archive()`**：没 setup 或什么都没绑定 → 空批次；否则入口查存活（recorder 丢了就抛错，什么也采不到）→ 屏障 → 按绑定顺序收集每个会话（每个会话在一条同步语句里换出自己的缓冲区）→ 指纹 run 记录（内容变了才再发）→ 批次。每一步都有界。损失点名到它所属的那一层，不连带别人：屏障没落定、或上次收集以来有会话处理事件时抛过错（`ArchiveChannel.errors` 增加了），是整个 capability 的缺口（`ArchiveGap` 写裸 id `browser`：这种损失不属于某一个归档器），会话照样收集；提交了的 cycle 缺原始 DOM，是每个要 `raw_dom` 的已绑定归档器的缺口；某个会话的 `collect` 抛错，是它那个归档器的缺口（如 `browser.dom_raw` 自采超时），其余会话的证据照样交出。指纹不是可选的：有步级证据的批次一定带着它（见下面"WARC 引用限于一次交出批次"），所以测不出指纹时整次收集抛错，environment 把它记成整个 capability 的缺口。
- **实测开销**（Chrome 155，fixture 站点除 `nested_worker` 外的 12 个场景、在同一个浏览器里各访问两次，`none` 档位，DOM agent，三个归档器全开；`test_browser_archive_live.py::test_overhead_benchmark` 写出 `ArchiveOverheadReport`）：每步（导航 + 观测 + 收集）p50 多 ≈ 10–25 ms、p95 多 ≈ 90–100 ms；上限 p50 300 ms（`core/archive/budget.py::ARCHIVE_MAX_P50_OVERHEAD_MS`，度量报告的归档开销规则用的是同一个常量，§4.16；基准可用 `SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS` 临时覆盖）。字节（这次 run 收集到的全部证据，含收尾那次收集，除以步数）：去重前每步 ≈ 1061 KB，去重后 ≈ 537 KB（2026-10-13 起两个 6 MB 的 body——`large` 场景的脚本与 XHR——都边下边收、存下；之前只补取脚本、XHR 记 `too_large`，那时是 537 / 275 KB）。去重前的算法：每份内容寻址文件都计入，每条 revisit 记录按它指向的那条完整 response 记录的存储长度计入（payload 与压缩相同，只差几个头部字节）。省下的字节几乎全来自内容寻址：两个 6 MB 的 body 两次访问各收一遍，各只存一份（每步 ≈ 524 KB）。revisit 每步只净省 ≈ 83 B：gzip 后一条 revisit 记录约 510–530 B，比几十字节的 JSON 或脚本的完整记录还大，这些 payload 写成 revisit 反而各多花 50–90 B；净省主要来自 Range 视频的 206 body 和较大的 HTML 页面。

#### `browser.network`

- **在 Fetch Response 阶段定 body 怎么取**：`requestPaused` 时（Chrome 扣着响应）按体积分档；内联读用 `Fetch.getResponseBody`，Chrome 扣着响应直到读答回、最多 `body_read_timeout_s`（默认 70 s：Response 阶段的读取包含下载本身，不是 Lighthouse 那种读缓存的 1 秒；其余 recorder 命令用 `command_timeout_s`，默认 1 s），然后 recorder 在 `finally` 里 `Fetch.continueResponse`。不依赖 `Network.getResponseBody` 的缓冲区（桌面版总量 200 MB、单个 20 MB，先进先出淘汰，跨进程导航整体丢失）。
- **读答回之前不放行**（2026-10-23 修复，取代 2026-10-22 的"放行之后接着等同一个读"）：内联读把响应一直扣到 `Fetch.getResponseBody` 答回，预算只有一个 `body_read_timeout_s`（默认 70 s，等于原先 10 s 的扣留加 60 s 的放行后等待）。实测（Chrome 155，headless，本机的慢服务器，探针在本地 scratchpad；Chromium `devtools_url_loader_interceptor.cc` 的 `ContinueInterceptedRequest` 与 `ResponseBodyComplete` 是同一件事的源码）：
  - **提早放行不让页面早拿到任何东西**：读一旦开始，Chrome 要等整个 body 下完才交给页面，放没放行都一样。一个 5 s 里分六块送来的 body，不录时页面 11 ms 拿到第一块、之后每秒一块；读着的时候 0.5 s 放行，页面 5.03 s 才一次拿到整个 body。
  - **提早放行会把下载失败藏起来**：压缩过的 body（`Content-Encoding: gzip`）放行之后连接断了，页面拿到的是一个**成功的空响应**：`loadingFinished`、`encodedDataLength` 0，`<script>` 触发 `load` 而不是 `error`（8 次里 8 次；不压缩的 body 照样报错）。扣着的时候断，页面拿到的是网络错误本身（`net::ERR_INCOMPLETE_CHUNKED_ENCODING` / `net::ERR_CONNECTION_RESET`，8 次里 8 次）。原因：放行之后，Chrome 在读用的管道关闭时就把读到的字节交给页面，用的完成状态是还没收到失败时的默认值（成功、0 字节）；压缩的 body 经 DevTools 的解码器，源头一断解码器先关管道，失败状态后到。
  - 动因：爱奇艺首页（LexBench L1 任务 18，2026-10-23 的两轮录制）整个 run 停在骨架屏。五个站并行录制、都经本机代理，`www.iqiyi.com` 的十几个主脚本下得很慢（约 100 KB/s，单个 11–33 s），超过 10 s 的扣留被放行；其中一直没收到字节的那些流在开始后 30–40 s 中途结束（第一轮 1 个 `channel_7bd12dde`，第二轮 11 个同一刻；谁结束的没有查明：那一刻 af 没有动作，限速到 50 KB/s 的复现里 75 s 也没有出现），页面拿到空脚本，app 的入口 chunk 是空的，再也没有渲染。录下来的也错：`body_status: ok`、0 字节，回放会把空脚本当成功送出。同一形态也见于视频页的播放器 chunk（`background_tabs: freeze` 那轮的任务 208，6 个），爱奇艺自己的加载器上报 `result: success`、`transfer: 0`，于是不重试。
  - 读超过预算仍没答的，记 `incomplete`、`body_note` 是读超时的那句，然后放行；这时读还在跑，上面的隐藏照样会发生（残余，下面"已知限制"）。`Network.getResponseBody` 在 `loadingFinished` 之后取不行：它把非 UTF-8 的文档按字符集解码成文本交回（1875 字节的 GBK 交回 2675 字节的 UTF-8），存下来回放时字节就错了。bilibili 的搜索结果（新 tab 里打开，文档 15.9 s 才下完，gzip、54 KB、没有 `Content-Length`）在 10 s 时被放弃过，回放每次都在这一步让文档失败（2026-10-22 的 15 任务数据里 6 个分叉）；现在它在扣留之内整个存下。
  - **读到的不一定是整个 body**：`Fetch.getResponseBody` 在下载失败时也照样答回，给的是失败之前到了的字节（实测：连接断开时答回 0 字节或已到的部分，命令本身成功）。放行之后 Chrome 把失败紧跟着响应交给页面（`responseReceived` 之后就是 `loadingFailed`），而交换本来就等 `responseReceived`（下面"什么时候定下来"）：写出之前到的 `loadingFailed` 报的是下载失败（不是页面取消、不是被拦、不是 CORS 拒绝）时，这条记 `incomplete`，`body_note` 以 "the download failed after its body was read" 开头、括号里是失败原因，`error_text` 是 Chrome 报的原因，回放读作 `network_failed`。两个事件恰好被一次收集隔开时，这条仍记成 `ok`（残余，窗口是毫秒级）。
- **体积分档**（按声明的 `Content-Length`；64 MiB 以内的 body 不论类型都存下，用户 2026-10-13 的裁决）：
  - 不超过 `max_inline_body_bytes`（默认 5 MB），或者没声明长度：**内联读**，Chrome 扣着响应直到整个 body 下载完、交给 recorder，再放行（上一条；超过 `body_read_timeout_s` 的读记 `incomplete` 后放行）。
  - 超过它、不超过 `MAX_BODY_BYTES`（64 MiB，即回放默认的 fulfil 上限 `DEFAULT_FULFILL_BODY_BYTES`，测试钉住两者相等；这个上限不来自 CDP 消息的大小，见上面"消息上限"）：**边下边收**。在暂停里对报告这个请求的 session（dedicated worker 的请求暂停在页面 session、在 worker session 上报告）发 `Network.streamResourceContent`，然后放行；页面自己的那一次下载经 `Network.dataReceived` 的 `data` 一块一块到 recorder，不重新下载、不扣响应。exchange 行记 `streamed_later`、不等 body；请求结束（`loadingFinished` / `loadingFailed` / target detach）时判断 body：没压缩的按声明的长度判断收没收全（不管终结事件是哪个：页面读完最后一个字节后取消 reader，Chrome 报 `loadingFailed`（`net::ERR_ABORTED`，实测）），压缩的看 `loadingFinished` 或线上字节数（`encodedDataLength` 之和）够不够声明的长度；收全的 body 作为内容寻址的 blob 交出，索引里一条 `kind: body` 行指回原记录，没收全的 `kind: body` 行写 `incomplete` 和原因。开流失败（命令报错）就在暂停里改为内联读，`body_note` 写明。**文档例外，照样内联读**：导航的 body 只有提交之后才开得了流，而提交被这个暂停挡着（实测：主 frame 的命令要等提交才答，在暂停里等它，导航就一直卡着，探针等满了自己设的 10 s 超时；先发后放行对主 frame 管用，12 与 40 MiB 都收全，但 iframe 的文档在命令执行前已经加载完，命令报 "has already finished loading"）；大于 5 MB 的文档少见，所以留在一定收全的内联读上。HEAD、204、304 这类按协议没有 body 的响应也不开流。
  - 超过 64 MiB：`too_large`，从不读；没声明长度或压缩过的 body 解码后超过 64 MiB，读完也记 `too_large`（不存）。
  - 一个 chunk 解码失败、或者流式收到的超过 64 MiB：这个 body 不存，记 `incomplete` / `too_large`。
  - 实测（2026-10-13，Chrome 155，headless，fixture 之外的探针：页面用 `fetch()` 加 reader 读一个大 body，比较不录、内联读（把内联上限提到 64 MiB 就是这样）、边下边收三种）：

    | body | 速率 | 不录：首字节 / 读完 | 内联读：首字节 / 读完 | 边下边收：首字节 / 读完 | recorder 事件循环最长卡顿（内联 / 流） |
    |---|---|---|---|---|---|
    | 8 MiB | 8 MiB/s | 4–5 / 977–978 ms | 1076–1077 / 1076–1077 ms | 4–7 / 979–980 ms | 23–27 / 1–2 ms |
    | 32 MiB | 8 MiB/s | 4 / 3976–3981 ms | 4303–4309 / 4304–4309 ms | 6–7 / 3977–3980 ms | 113–118 / 2 ms |
    | 60 MiB | 8 MiB/s | 2 / 7475–7476 ms | 8096–8129 / 8097–8131 ms | 6 / 7478–7479 ms | 219–225 / 2 ms |
    | 60 MiB | loopback | 4–35 / 22–54 ms | 655–710 / 657–711 ms | 11–17 / 563–565 ms | 233–247 / 8 ms |

    每格是两次运行的范围。内联读让页面在整个 body 下载完、经 CDP 交出之前一个字节都拿不到（60 MiB 在 8 MiB/s 下首字节晚 8.1 s），一条约 80 MiB 的回复还把 recorder 的事件循环卡住约 0.23 s，那段时间别的暂停都等着；按默认的 10 s 读超时，再慢一点的下载干脆存不下。边下边收在真实网速下与不录一样，只在 loopback 上多花 CPU（60 MiB 时页面读完晚约 0.5 s；50 ms 后开始的一个小请求，流时晚约 0.5 s，内联读晚约 0.6 s）。所以选边下边收（探针在本地的 scratchpad，不进仓库）。页面一侧的结果都不变，recorder 收全、sha256 一致：XHR、`fetch().arrayBuffer()`、`fetch()` 加 reader、`<script>`、POST 的应答、gzip（流的是解码后的字节，`encodedDataLength` 之和等于声明长度）、206、dedicated worker 的 `fetch()`（流开在 worker 的 session 上）。
  - 残余：页面不读的 body 流不完（`fetch()` 之后从不读 body，或停住的视频），它的记录一直 `pending`，run 结束时当作 `incomplete`；内联读时 Chrome 会自己把 body 读完。每次收集只等内联读（`drain_timeout_s`），从不等流：流什么时候完由页面的下载决定，可能永远不完。一个内联读也只等一次：等过一次还没答的是慢下载（最多 `body_read_timeout_s`），每次收集都等它，就给它跨过的每一步都加上 `drain_timeout_s`。
- **元数据**：移植 Playwright `crNetworkManager.ts`（Apache-2.0，保留 notice）的事件模型：Fetch 与 Network 事件到达顺序不定，按 `networkId` 配对；重定向链每一跳一条记录（3xx 没有 body，`redirected_from` 串起来）；`*ExtraInfo` 配对（原始的 `Set-Cookie` 只在这里出现）；worker 的请求在 worker session 上 `requestWillBeSent`、在父页面 session 上暂停。
- **POST body**：`Network.enable{maxPostDataSize: 65536}`；来源依次是 `requestPaused` 的 `postDataEntries`（暂停走 task，从不进 scope），`requestWillBeSent` 的 `postDataEntries` / `postData`（在准入时就留进会话的侧存储，事件本身不带 body，见上面"字节从不随事件走"），最后是 `Network.getRequestPostData`。
- **WebSocket**：握手记成一条交换（`body_status: websocket`），帧写进本步的 `websocket.jsonl`（方向、opcode、时间、内容）。
- **304 永远不覆盖 200**：304 原样记录（空 body）；回放只把它当作带条件头（`If-None-Match` / `If-Modified-Since`）的请求的候选，从不拿它顶替 200（M30）。
- **Range**：206 按收到的样子记录：部分 body、`Content-Range` 原样保留、索引里解析出 `content_range`；超过内联上限的 206 与别的 body 一样边下边收。从不为 206 另取整个资源；回放用全部 206 记录拼出每个 URL 的字节区间，没覆盖的区间是 miss。
- **格式**：每步一个 WARC/1.1 `exchanges.warc.gz`（warcio，核心依赖，Apache-2.0；每条记录一个 gzip member）。request 与 response 记录以 `WARC-Concurrent-To` 关联；body 存解码后的字节，HTTP 头里去掉 `Content-Encoding` / `Transfer-Encoding`、按解码长度写 `Content-Length`（原值记在索引里）；同一收集批次里重复的 payload 写成 `revisit` 记录（`identical-payload-digest`，指向第一次的 response 记录）。另配一个步索引 `index.jsonl`：
  - `kind: exchange`：每个交换恰好一次，在它定下来的那次收集里。字段：`record_id`、`response_record_id`、两条 WARC 记录的 `offset` / `length`、`redirected_from`、method、url、status、resourceType、frameId、loaderId、session / target、`paused_session_id`（Response 阶段暂停它的 session；没停到读 body 那一步的为空）、initiator、时间（`started_at`：请求开始的墙钟时间；`ended_at`：请求结束的墙钟时间，即终结事件 `loadingFinished` / `loadingFailed` / 下一跳的重定向 / target detach 到达时，CDP 的单调时间戳按这个请求的 `wallTime − timestamp` 换成墙钟；行写出时请求还没结束的（body 已在 Response 阶段读完、终结事件还没到）为 `null`）、**原始请求事实**（剥离后的请求头、POST body 的 sha256 与长度、content-type；脱敏后的 body 在 WARC 的 request 记录里）、`payload_sha256`、`revisit_of`、`content_range`、`body_status`（`ok` / `redirect` / `too_large` / `streamed_later` / `websocket` / `incomplete`）、结局事实（`error_text`：Chrome 报的失败原因，或 recorder 写下的 target detach；`body_note`：recorder 没存下 body 的原因，例如读 body 超时；`from_cache`：响应来自浏览器缓存）、脱敏台账。
  - **不写规范匹配键**：匹配键由 M30 在加载时用唯一的规范化实现从原始事实算出，规则变了也没有过期的键。
  - `kind: body`：一个 `streamed_later` 的 body 流完（它的请求结束时），指回 `record_id`：`ok` 带 `payload_sha256` / `payload_length`（body 是那个内容寻址的 blob），否则 `incomplete` / `too_large` 带 `note`。流式交换的 `exchange` 行在请求结束之前就写了，所以请求的结束写在这一行：`ended_at`（与 `exchange` 行同一定义）与 `error_text`（请求以失败结束时 Chrome 报的原因，或 target detach）。
  - `kind: pending`：这次收集时还没定下来的交换（请求已发出，响应或 body 还在途），只有状态与请求开始的墙钟时间 `started_at`（还不知道时为 `null`）、不带证据；之后它的 `exchange` 行恰好出现一次。`exchange` 行已经写过、body 还在流式接收的 `streamed_later` 记录也每次收集一条（`waiting_for: streamed_body`），直到它的 `kind: body` 行交出。一个 `record_id` 的最后一行是 `pending`，它就是 run 结束时还在途的那个，读者当作 `incomplete`（§4.9 "最后一次收集"）。每次收集先在 `drain_timeout_s` 之内等正在读的 body 与补取。
  - `pending` 行的开始时间与 `exchange` / `body` 行的结束时间（用户 2026-10-20 的裁决）是证据：量得出源页面一个请求活了多久、多久之后取消，是评估"暂不回答、等页面自己取消"（下面"回放的残余"）的前提。回放的录制顺序暂不用它们（下面"加载"）。
- **什么时候定下来**：终结事件（下一跳的 `redirectResponse`、`loadingFinished`、`loadingFailed`、WebSocket 握手应答）到了、body 读完；**或者** body 已在 Response 阶段读完：页面从不读取 body 的 `fetch()` 根本不会有 `loadingFinished`（M29 实测，Chrome 155：GraphQL、弹窗、SW 的 fetch 原本都永远停在 `pending`）；读到的可能是下载失败之前的那部分，失败紧跟着 `responseReceived` 到（上面"读答回之前不放行"）。两种等待只值一次收集：这种交换的 `responseReceived`，以及任何交换的原始响应头（`responseReceivedExtraInfo`，`Set-Cookie` 只在这里，Fetch 的 `responseHeaders` 与 `responseReceived` 都不带它，实测）。屏障保证收集之前发出的事件都已处理，所以一次收集的宽限足够；Chrome 不发的，到期照样写出（`raw_response_headers: false`）。从缓存来的响应不等 ExtraInfo（crbug 1340398）。目标 detach 时它还在加载的交换记为失败。
- **body 是解码后的**：`Fetch.getResponseBody` 返回解码后的字节（实测：`Content-Encoding: gzip`、`Content-Length: 50` 的响应读出 1600 字节明文）；索引的 `original_encoding` 记原来的 CE / TE / CL。Fetch 看到的请求头里有 `Cookie`（Chrome 155），与其他来源一样剥离。`Network.dataReceived` 的 `data` 同样是解码后的字节（gzip 实测）。没有 `networkId` 的暂停一律不记、原样放行，是防御性的后备（归档器自己不发任何请求）。
- **WARC 里只放已知的响应**：`ok`、`redirect`、`websocket`（101）有 response（或 revisit）记录；`too_large`、`streamed_later`、`incomplete` 只有 request 记录，状态与响应头写在索引行里，因为空的或不完整的 payload 会骗过不读索引的人。流式收到的 body 是内容寻址的 blob（`body-<sha>.bin`，原样不压缩：64 MiB 不可压缩的数据写进 gzip WARC 要约 1.8 s CPU，实测），`kind: body` 行在它的 `exchange` 行写出之后才交出。
- `require_replayable: bool = True`：与 `replayability: none` 一起绑定 → `prepare()` 报错（`check_setup`；`bind_archivers` 与 setup 再查一遍），点名归档器与档位。
- **只录线上流量**：`browser.network` 永远是"录流量"（用户 2026-10-13 的裁决），不随网络来源换意思。回放（`network_source: archive`）没有自己的线上流量，送出的每个字节都已经在源归档里，所以绑定它被协作检查拒绝（按 `fetch_response` 需求，`prepare()` 就报），报错点名回放要绑定的 `browser.replay_log`（下面）。
- 其他旋钮：`strip_request_headers`（大小写不敏感，追加在默认三个之后）、`max_post_data_size`、`allow_session_state`。原先补取大 body 的上限 `max_streamed_body_bytes` 与补取本身（`Network.loadNetworkResource` 再下载一遍，只对静态 GET）一起删掉：边下边收覆盖所有类型，64 MiB 以上一律 `too_large`。

**WARC 引用限于一次交出批次。** 相同 payload 的 revisit 只引用同一 collection 的 response；每次 collection 建独立 payload 索引。记录组件可能丢失某一批后继续运行，后续批次不能依赖它未提交的 WARC。跨批内容寻址 blob 的持久化去重仍由 WAL 对已提交内容负责，归档器不维护第二份提交账。有步级证据的每批同时交出当前环境指纹，即使内容未变；WAL 对已提交的 run 记录去重。一批只缺某个归档器的证据（`gaps` 点名它）时，其余归档器的文件照样随这一步提交：每个归档器的文件在一批里自成一体（`browser.network` 的 revisit 只指向它这一批自己的 WARC），所以缺了别的归档器不会留下悬空引用。空且未变化的收集仍为空。流式 body 后到而原 exchange 未提交时，读侧明确拒绝缺失关联，不补造完整交换。

#### `browser.storage`

`BrowserStorageState`（`schemas/browser_storage.py`）限定 cookies 与 local / session Web Storage，不声称复制 IndexedDB、service worker、cache 或完整磁盘 profile。cookies 通过 `Storage.getCookies(browserContextId=run_context)` 读取，CHIPS 的 `partitionKey` 原样保留；local 按 Chrome `Storage.getStorageKey(frameId)` 返回的真实 opaque key 去重，session 按 **logical tab + storageKey** 读取。每个 frame 记录 origin 与最近 parent 优先的 ancestor origins，重建时据此建立实际分区并验证 key，绝不通过解析 key 猜分区。OOPIF 由自己的 recorder target session 读取，父 tab frame tree 即使列出它也不能替代其 sessionStorage namespace。logical tab 身份在本 run 的初态、采集与 hydration 之间一致；关闭或不可读的 frame / tab 放进 typed `unavailable`，不伪造完整快照。

启动 seed 在业务导航之前、recorder capture 之前，由 control 连接临时开启 Fetch Request 拦截，全部 synthetic 空文档 / 嵌套 iframe 本地 fulfill；它们建立 origin 与 ancestor partition，写入后退回 about:blank、等待提交，再撤掉 Fetch / autoattach / 临时 sessions。无真实网络请求、业务脚本或常驻 reset/init 脚本。session 所属 tab 保留，setup 按 seed 声明顺序选择首个 tab，不依赖 `Target.getTargets` 的返回顺序。`StorageTab.url` 是快照来源信息，hydration 不自动导航它；初次业务导航仍只由 `start_url` 决定。失败和取消关闭本次 hydration 新建的 tabs，Runtime 的 teardown 继续释放本 run 资源。

immutable canonical 初态通过 `browser.storage/initial_state.json` 交给现有 Archive/WAL；发生状态变化的批次再次携带同一初态的 content-addressed 引用，避免依赖此前未提交的批次。它是**首请求前**的初态，不能用 step 0 动作后的 `storage.json` 替代。环境指纹 `initial_storage` 始终保存 `sha256`、`nonempty`、`coverage: cookies_local_session` 与 `origin_coverage: fresh | seed | observed_frames`。fresh 无 seed 是已知空态；显式 seed 是已声明范围；持久 profile 的未知 origin 无法穷举，只能记启动时可见 frames / cookies，标 `observed_frames`，这种源拒绝 offline replay / branch，不假称可恢复。

回放和分叉只经 `ArchiveReader` 读取已提交 initial_state 与 fingerprint，检查摘要；非空初态缺文件、覆盖不足、或另给的显式 seed 与源初态冲突，都在 prepare 拒绝。已知 fresh 空态允许在未选 storage 归档时按指纹重建为空；非空 seed 即使没有选择 storage 归档也可以正常执行 Agent，但其归档以后明确拒绝缺少初态的回放。recordable task context 不复制 seed 正文 / 原路径，所以源 JSON 后来删改不影响 branch，也不会因 task setup 的 credential 字段脱敏把必要值改坏。

每次**收集时**另读当前状态，记 `storage.json`（动作之后）；只有状态变化才写，采集时间不参与去重。它用于逐步环境证据，不能成为启动 seed。cookie 和 storage 的值原样保存（用户 2026-10-13 裁决，§5.6），声明 `contains_session_state`；browser 台账仍为空、只声明规则版本。`allow_session_state` 仍只表示接受持久 profile。`pinned` 允许物理 fresh context + 显式 seed，仍拒绝持久 profile。

Chrome 155 的 fixture 回归 `test_browser_storage_seed_live.py` 验证首请求 cookie / 首脚本、同源多 tab session、两第三方分区 / CHIPS / OOPIF session，以及 JSON 源路径删改后的 capture → offline replay → branch；失败 / 取消只清自己的 hydration tabs。它不代替真实账户的业务验收。

#### `browser.dom_raw`

- DOM 参与了协商：持久化本 cycle 已经采到的原始采集（§4.12 的 sink，只要提交了的 cycle），不重新采；归档路径**不发任何 `DOM.*` 命令**（backendNodeId 是懒分配的，多一次采集可能扰动 `serialized_dom`）。
- 纯视觉（UI-TARS：没有协商 `browser.dom`）：ArchiveWatchdog 把观测 scope 的 cycle 事件跨 scope 交给会话，会话在 recorder 的 session 上自己采一次（与 BrowserUse 同一个采集函数：`DOM.getDocument` + `DOMSnapshot`（含 paint order）+ AX + DPR + 文档戳），结果同样只在 cycle 提交时才进仓库。代价是每步多一次完整的 DOM 采集，在 archive scope 里与截图并行，cycle 等它做完（M29 实测，fixture 站点：自采本身 p50 ≈ 6 ms、p95 ≈ 10 ms，在会话的 `on_cycle` 外面计时；大页面上会更多），上限 `self_capture_timeout_s`（默认 10 s），超时或失败让下一次收集抛错。自采不跟进进程外 iframe（`frames=None`）。归档一侧的文档戳不触发 cycle 重发，所以开不开归档，观测都一样。
- Skyvern 的 JS 路径归档 JS 的原始输出。
- 落盘：每个提交的 cycle 一份 gzip 的 JSON（键排序、集合转成有序列表，`mtime=0`），内容寻址，带 tab、loaderId、url、viewport、DPR、cleaner 键；cycle 与采集时间在来源信封（`ArchiveProvenance`）里、不在内容里，所以没变的文档跨步只存一次；重新导航之后 node id 都变了，同一个页面不会去重（实测）。脱敏在构造 `ArchiveFile` 之前完成（下面的隐私一节）。`archive/dom_raw.py` 的 `capture_from_document` 把归档内容还原成 `TargetCapture`，离线换 cleaner 用；JSON 的键都是字符串，它把 Skyvern 按整数 ref 建的两张表（`skyvern_nodes`、`skyvern_hit_tests`，`skyvern_capture.INTEGER_KEYED_MAPS`）还原成整数键，否则两个 Skyvern cleaner 按 ref 查节点全部落空（测试：归档往返之后，两个 cleaner 离线清洗出的序列化文本、选择器表的 backend id 与命中测试都与在线相同）。
- 用途：离线换 cleaner / serializer 重算观测（BrowserUse cleaner 在给定 capture、viewport、DPR、前一步 selector map 时是纯函数）；用 paint order 离线判断遮挡（M32）。

#### `browser.frames`

训练"前瞻观测"模型要的标签证据，agent 自己从来看不到（用户 2026-10-21 的需求）：一步里多个动作之间的中间画面（UI-TARS 一步最多发 4 个动作，每步只观测一次，后面几个动作之前的画面没有任何观测拍到），以及观测之后大约 1 s 的第二张截图，用像素判断观测到的那一帧当时是不是还在变（上面"观测之前等页面落定"只看加载、DOM 变动、资源加载与视口里有没有内容，从不比较图像）。它是一个归档器、不是 observation fragment：绑定在 `trace_writer.archive` 里才录，从不渲染进 prompt，恢复校验也从不读它（2026-10-13 的裁决：恢复只比较 agent 看到的东西）。

- **两种帧**，都是聚焦 tab 视口的干净截图（`Page.captureScreenshot {format: png}`，不画光标：光标只画进给模型的那份副本，§4.4），在 recorder 连接上这个 tab 的 session 里拍，所以和观测截图同样的像素尺寸，也不占控制连接：
  - `before_action`：每次 `env.act` 碰页面之前（任何滚动到可见、指针移动、按键之前）拍一张。一步的第一张是"模型想完之后的画面"，后面的是动作之间的画面。
  - `after_observation`：每个提交了的观测 cycle 之后 `after_observation_delay_s`（默认 1.0 s，`null` 关掉）再拍一张。
- **新通道 `action_boundary`**（`ArchiveNeed` 的第五个值，`browser.frames` 有它）。`BrowserCapability.execute_action` 一开始就调 `BrowserArchiveRun.action_boundary(tab=..., control_session_id=..., action=..., tab_open=...)`，**每一次调用都通知**，包括聚焦 tab 已经自己关掉、动作马上失败的那种（`tab_open=False`，帧记 `no_tab`）。归档侧按观测 cycle 给调用编号（`ActionBoundary.cycle` / `action_index`，不论有没有归档器要边界都计数），再在动作自己的 task 里把 `ActionBoundary` 依次直接交给每个 `wants_action_boundaries` 的会话（与 `commit_raw`、`cycle_committed`、`collect` 一样直接调用，不经 archive scope：排队的投递可能被悄悄取消、已关的 tab 没有 scope 可发，而且会话必须知道自己被放弃了才能把帧记下来）。会话超过自己的 `action_boundary_budget_s` 再加 0.5 s（`ACTION_BOUNDARY_GRACE_S`）就被取消；被取消（或整个 run 被取消）的会话照样把这一帧记成 `timeout`，`detail` 写动作等了多久。帧的事不会让收集失败、不会丢掉这一步，也不会让动作失败：`ArchiveChannel.errors` 不为帧计数。通知完成之后 capability 才再查一次 tab（等待期间 tab 自己关了，动作照样以"closed itself"失败），最后才开跟随窗口（§4.14 "页面新开的标签页"），所以拍帧期间页面自己开的 tab 不算 agent 的。setup 打开起始页走 `_run_action`，不通知。capability 不按归档器 id 分支，也从不知道步号。
- **与动作记录对齐**：`action_index` 是这次 `env.act` 调用自本 cycle 以来的序号，在 ReAct 一步只观测一次的流程里，它就是那一步 `StepRecord.action_spans` 的下标（每次 `env.act` 一个 span，按调用顺序）。按序号与 `action_spans` 对齐，不要与 `StepRecord.actions` 对齐：被 `guarded_act` 拒绝的动作根本到不了 `env.act`，两边都没有它。
- **提交的 cycle**：`_populate_cycle` 成功时，`BrowserArchiveRun.commit_raw` 之后紧接着 `cycle_committed(cycle, tab, control_session_id)`，同步告知每个会话（`on_cycle_committed(CommittedCycle)`，默认 no-op）；失败的 cycle 不告知。
- **动作等多久，有上限**：拍帧的等待发生在 `env.act` 里面，所以算进 agent 的超时、`duration_ms`、`execution_ms` 和每个 `ActionSpan`（§5.4）。因此它有界：一个动作等它的帧（加上一张在它开始前已经到点的 `after_observation` 帧）合计最多 `max_action_wait_s`（默认 1.0 s，上限 5 s），会话被放弃时最多再加 0.5 s；`capture_timeout_s`（默认 3 s，上限 5 s）约束每一张截图。同一个 tab 在没有成功截图的情况下超时两次（`TIMEOUTS_BEFORE_NOT_WAITING`）之后，这个 tab 上的动作不再等：帧立刻记 `timeout`（`detail` 以 "not waited for" 开头），同时在后台发一次探测截图（同一时刻每个 tab 最多一次），第一次成功之后恢复等待。每条 `before_action` 记录都带 `released_at`：这个归档器放动作走的墙钟时间，所以录制给动作加的时间是 `released_at - requested_at`，消费者可以从 `ActionSpan` 的时长里减掉它。
- **时序与作废规则**：`after_observation` 帧的请求时间是 cycle 提交时刻加延迟。到点之前开始了一个动作（动作开始时间早于请求时间），或者开始了新的观测 cycle，它就作废：取消，在索引里记 `superseded`，`superseded_by` 是 `action` 或 `observation`，没有图像。动作开始时已经到点的帧先拍完，动作才执行；在动作的等待额度内拍不完，就取消并记 `timeout`。收集时还在等的帧，收集先等它，最多剩余的延迟加 `capture_timeout_s`，所以第 *t* 步观测之后的那一帧落在第 *t* 步的批次里；只在一步没有任何动作、而且模型比延迟答得快时才真的要等（UI-TARS 的 `finished()` 也是一个动作，`browser.done`）。过了这个时间仍在途的帧恰好进后面某一个批次一次，`cycle` 说明它跟在哪次观测之后。teardown 取消还在等的帧与探测。
- **失败照实记**：tab 在 recorder 连接上没有 session、或者动作前 tab 已经自己关掉，记 `no_tab`；超时（截图超时、动作等待额度用完、不再等的 tab、被放弃的会话）记 `timeout`；其他失败（CDP 报错、回复里没有图像）记 `failed`；都带 `detail`，都不是悄悄少一帧，也都不让收集失败。recorder 连接丢了，收集照常以 `RecorderLostError` 报错（这是 recorder 的健康，不是某一帧的事）。
- **落盘**：每次非空的收集交出每张拍到的 PNG（内容寻址，`media_type: image/png`，名字 `frame-<cycle>-before-<i>.png` / `frame-<cycle>-after.png`，在一个 run 里唯一；同样的画面只存一份）和一份步级索引 `frames.json`：

  ```json
  {"format": "browser-frames-2",
   "frames": [{"kind": "before_action", "cycle": 12, "action_index": 0, "action": "browser.click",
               "tab": "<target id>", "requested_at": 1761000000.123, "captured_at": 1761000000.160,
               "released_at": 1761000000.161, "outcome": "captured", "superseded_by": null,
               "name": "frame-12-before-0.png", "sha256": "...", "width": 1280, "height": 720,
               "detail": null}]}
  ```

  `kind` 是 `before_action` / `after_observation`；`cycle` 是这一帧跟在后面的观测 cycle；`action_index` 是那次 `env.act` 调用自那次 cycle 起的序号（从 0 起，`after_observation` 为 `null`），`action` 是动作名（`after_observation` 为 `null`）；`requested_at` 是动作开始时间或 cycle 提交时间加延迟，`captured_at` 是截图回复到达的墙钟时间（没拍到为 `null`），`released_at` 是放动作走的墙钟时间（只有 `before_action` 有，其余为 `null`）；`outcome` 是 `captured` / `superseded` / `timeout` / `failed` / `no_tab`；`name`、`sha256`、`width`、`height` 只在拍到时有值（PNG 头读不出尺寸时宽高为 `null`）。条目按定下结果的先后排列。索引文件的 `captured_at` 是最后一条定下结果的时刻。格式 `browser-frames-1`（没有 `released_at`）的索引，读取时被拒绝，要重录。
- **读取**：`archive/frames.py` 里的纯函数，只经 `ArchiveReader`（不 import `core.trace`，`test_archive_boundaries.py` 规则 5）：`read_frames(reader, step) -> list[FrameRecord]`、`iter_frames(reader, *, include_trailing=True)`（收尾批次的 step 是 `None`）、`read_frame_image(reader, step, record) -> bytes`（按名字和 sha256 在那一步的文件里找；没拍到的帧报 `ValueError`）。
- **协作**：任何网络来源都允许（录制，以及离线回放和分叉的 `archive` 来源）；`requires_replayable` 为 False；隐私标记沿用基类（截图不带 cookie 与 storage）；`allow_session_state` 与其他归档器同义。截图拍的是页面当时的样子：`debug_highlight`（默认关）打开时它画进页面的提示也会出现在帧里。
- **实测开销**（Chrome 155.0.8059.86，headless，1280x720 PNG，fixture 的 `frames` 页；`test_browser_archive_frames_live.py::test_what_a_frame_costs`）：`before_action` 拍摄 p50 ≈ 33 ms、最大 ≈ 50 ms，`released_at - requested_at` 相同，动作因此多等这么久（不绑定时 ≈ 0）；同一 session 上直接连发 `Page.captureScreenshot` 也是 p50 ≈ 31–34 ms，所以几乎全是 Chrome 出一帧的时间，通知本身可以忽略；`after_observation` 拍摄 p50 ≈ 15 ms。UI-TARS 一步 4 个动作约多 130 ms。字节：每帧一张视口 PNG，与观测截图同样大（`screenshot_watchdog.py` 实测 1280x720 的维基百科文章约 240 KB），相同的画面只存一份。
- 验证：`test_browser_archive_frames.py`（fake CDP：动作等它的帧、作废、超时、失败、`no_tab`、每次调用都编号（已关的 tab、超时、失败都不跳号）、跟随窗口在拍帧之后才开、动作的等待额度、连续超时后不再等与探测恢复、被放弃或被取消的边界只记 `timeout` 而收集照常成功、收集等待的上限、读回）；`test_browser_archive_frames_live.py`（真实 Chrome 上跑发布的录制配置加 `browser.frames`，脚本化模型先点后等：点击前的帧与这一步的观测截图逐像素相同，等待前的帧是点击之后的画面、与下一步的观测相同，尺寸与观测截图相同，每条 `before_action` 的 `requested_at ≤ captured_at ≤ released_at`；模型比延迟慢时 `after_observation` 拍到，快时被动作作废）。

#### `browser.replay_log`

回放 run 的证据：回放服务的每个决定（下面"回放日志"）。原先由 `browser.network` 在回放时换模式来记，用户 2026-10-13 裁决拆成独立的归档器（`archive/replay_log.py`），每个归档器只有一个意思。

- `needs = {"replay_log"}`：协作检查按这个需求找它，`network_source: archive` 没有它就报错（miss 必须总有记录），`network_source: live` 绑定它也报错（live 来源什么也不回放）；两条都在 `prepare()` 报（`check_setup`），`bind_archivers` 与 setup 再查一遍（上面"协作检查一览"）。
- context 里带着回放服务的日志（`BrowserArchiveContext.replay_log`，只在回放时有），`open(ctx)` 交出 `ReplayLogSession`：不要 Fetch pattern、不订阅事件、不发命令；每次收集在一条同步调用里把这段时间的日志换出来，写成本步的 `replay.jsonl.gz`（下面"回放日志"），这段时间没有决定就什么也不写。没有回放日志的 context 里 `open` 抛错（协作检查在那之前就拒绝了这种配置）。
- 不写 WARC / `index.jsonl` / `websocket.jsonl`：命中的记录用 `record_id` 指回源归档。
- 旋钮只有 `allow_session_state`（每个 browser 归档器都有）。日志模式与折叠（`log_mode` / `log_repeat_limit`）是回放服务的，仍是 `archive` 来源的旋钮。
- 隐私标记是基类的 `MARKER_BROWSER_REDACTED`：日志里的 body 键由脱敏后的 body 算出，没有 cookie、没有 `Set-Cookie`，所以不声明 `contains_session_state`。

#### 环境指纹（`env/run.browser.json`）

capability 自己的 run 记录（`archiver_id="browser"`，`scope="run"`，`archive/fingerprint.py`），第一次收集时发出，内容变了才再发：Chrome 版本（`Browser.getVersion`）、浏览器的默认时区与语言（`browser_defaults`：没有 override 够得着的 frame 读到的值，setup 在启动之后、recorder 准备任何 target 之前在第一个 tab 主 frame 的 isolated world 里实测的 Intl 时区与语言，`backends/browser_defaults.py`；pinned 下时区就是 pin 的（`TZ`），语言是宿主机的；量不出来时记 `null`，这个 run 不能再被回放）、UA 与 UA metadata、viewport、DPR、时区、语言（pinned 取参数，UA metadata 是 override 带的 client hints 的低熵部分（brands、移动端、平台），不带时是 `null`；`none` 在第一个 tab 主 frame 的 isolated world 里经 recorder 的 `Runtime.evaluate` 实测：`Page.createIsolatedWorld`（`af-fingerprint`），页面脚本看不到这次读取、也改不了它读到的值（getter 陷阱、改写的 `JSON.stringify` 都不起作用，M29 实测）；每个文档（loaderId）只测一次，同一文档上的收集只多一条 `Page.getFrameTree`；残余：测量本身仍在页面的渲染进程里执行，主线程忙或对话框开着时这一次会等到 1 s 超时、换下一个 tab）、replayability 档位与参数（T0、种子……）、`shim_sha256`、override 带的 client hints（`replayability.client_hints`：CDP `UserAgentMetadata` 的形状，不带时是 `null`）、启动开关摘要（`ProviderSession.metadata["launch_flags"]` 去掉网络路由开关（`chrome_switches.NETWORK_ROUTING_SWITCHES`）之后的 sha256；`launch_flags` 最前面是启动设的环境变量（`TZ=…`）；录制时 fixture 的 `MAP` 规则与回放时的隔离开关都不进摘要，两边才可比；`command_line` 从不进指纹）与 pinned 自己的开关表和启动环境（`replayability_args`、`replayability_env`，单独存）、离开焦点的 tab 冻不冻（`background_tabs`：`run` / `freeze`，§4.14 "离开焦点的 tab"）、网络来源（M30：`network_source` 块，`live` 只有 `{"source_id": "live"}`，回放见下面"回放指纹"）、脱敏策略（`BROWSER_REDACTION_POLICY`，以及每个绑定归档器自报的 `redaction_facts()`：剥离的请求头）、隐私标记（`contains_session_state`、`fresh_profile`、`level`：有归档器保留会话态时是 `session_state`，否则 `redacted`）。`navigator.userAgentData` 只在 secure context 里存在（实测：tab 停在纯 http 的页面上时是 `null`），所以 UA metadata 记最近一次测到的，指纹不随页面来回翻。

#### 隐私（§5.6 的 browser 部分）

- 请求头 `Cookie` / `Authorization` / `Proxy-Authorization` 与 `strip_request_headers` 追加的那些，在**每一个**来源剥离，从不写盘：Fetch 的请求头、`requestWillBeSent`、`requestWillBeSentExtraInfo`（连同 `associatedCookies` 的值）、WebSocket 握手；台账记名字（`rule: request_header`）。
- 响应头 `Set-Cookie` 保留（回放的 cookie jar 需要）；`browser.network` 因此声明 `contains_session_state`，指纹同样标出。`responseReceivedExtraInfo` 里 `blockedCookies` / `exemptedCookies` 的 cookie 值不记。
- POST body：urlencoded / multipart / JSON 里名字命中共享字段名规则的值换成 `<redacted>`，写台账（`rule: field_name`）。`redact_post_body(body, content_type)` 从 `archive/redaction.py` 导出，M30 先用它脱敏线上请求再算匹配键，录制与回放用同一个函数。
- 原始 DOM：敏感控件（`dom/sensitivity.py`）的 `value` 属性（`DOM.getDocument` 与 Skyvern 的 `DOM.describeNode`）、它下面 user-agent shadow root 里的文本（`pierce: true` 会把输入值带出来）、`DOMSnapshot` 的 `inputValue` / `textValue`、AX 的 value，在构造 `ArchiveFile` 之前换掉并记台账（`rule: input_value`）。
- DOM 观测：BrowserUse 清洗器建树时按同一条规则把同样的位置换成 `<redacted>`（§4.12），提示词与 `browser.dom` fragment 都不带敏感值；Skyvern 的页内脚本本来就不带。
- 动作结果：`browser.extract_content` 读一个元素时返回的 HTML 按同一条规则、同样的位置打码（`&lt;redacted&gt;`，§4.12 "动作结果同样打码"），它进下一轮提示词，也进 `steps/NNN.json`；不给编号时返回的页面文本不含字段的值。
- `storage.json` 原样保存 cookie 与 local / session storage 的值（用户 2026-10-13 裁决，§5.6）；`browser.storage` 声明 `contains_session_state`，`allow_session_state` 只表示"接受非新建的 profile"。
- 两个新规则名（`request_header` / `input_value`）加进共享台账的 `RedactionRule`；browser 的规则版本是 `BROWSER_REDACTION_POLICY`（`<PRIVACY_POLICY>+browser-2`，写进每个 browser 台账的 `policy`），规则源码由测试按摘要钉住；与 `PRIVACY_POLICY` 一样，改规则就升版本。`browser-2`（2026-10-13）删掉了 cookie 值的哈希和它的规则名 `value_hash`。
- **键入标记**：`browser.type` / `browser.type_coordinate` 在敲字之前读焦点所在的元素（穿过开放的 shadow root 与同进程的 iframe），按同一条规则判定，动作结果的 `typed_into_sensitive_field` 记下判定：`true` / `false`；焦点在看不进去的 frame 里（进程外的 iframe、跨源）或读焦点失败时是 `null`（不知道，不是"不敏感"）；封闭的 shadow root 里看到的是宿主，判为 `false`。它进 `steps/NNN.json`；agent 只渲染动作结果的成败与错误，提示词逐字节不变（测试钉住）；导出怎么用它随学生侧一起推迟。键入的文字本身照旧原样留在动作参数与 LLM 记录里（§5.6）。
- **会话态（`logged_in` 的定义）**：没有任何地方声明 `logged_in`，所以定义为"浏览器不是从新建的 profile 启动的"，即 provider 的 `persistent_profile()`（`NETWORK_CAPTURE` 的承诺之一，§4.9.x）点名了一个旋钮（只看配置，从不看环境变量）。这时只要绑定了归档器，每个归档器都必须写 `allow_session_state: true`，否则报错，点名 provider 的旋钮与归档器（pinned 本来就拒绝非新建的 profile）。预置 seed 可用于登录页；在页面登录由 Agent 的既有动作完成。
- 残余，写明：WebSocket 帧的内容原样记录（没有字段规则可用）；字段名规则只认凭证形状的名字；URL 原样记录（回放按 URL 匹配），query 里的凭证不脱敏；`browser.extract_content` 不给编号时返回的页面文本列出 `<select>` 的选项文字，被判敏感的也在内（页面写的选项列表，不是用户的选择）。
- 验证（`test_browser_archive_live.py`）：经 `Network.setExtraHTTPHeaders` 植入 `Authorization` 与 `X-Api-Key`（后者在 `strip_request_headers` 里）、经 `Network.setCookie` 植入 cookie、一个 urlencoded POST 的密码、脚本设置的密码输入值，fixture 收到了每一个；植入的 cookie 值只出现在 `storage.json`（原值），其余植入值在 trace 目录里（gzip 解开）一次也不出现，三种台账规则都出现；正向对照：WARC 里的标记请求能找到。DOM 观测路径另有一条（`test_privacy_guard_on_the_dom_path`，BrowserUse 清洗，绑定 `browser.dom_raw`，观测像运行期那样提交进 trace）：写进标记的、脚本设置的、`display:none` 的密码，在提示词与 trace 目录里一次也不出现；经 `browser.type` 敲进去的只出现在 `steps/000.json` 的动作参数里，它的结果带 `typed_into_sensitive_field: true`，敲进普通文本框的是 `false`；`browser.extract_content` 读写进标记的那个密码框，返回的 HTML 里是占位符，读普通文本框返回它写进标记的值；普通文本框的值照常出现在观测里（正向对照）。`Proxy-Authorization` 无法这样植入：Chrome 155 让每个带它的请求以 `net::ERR_INVALID_ARGUMENT` 失败；它的剥离由 fake CDP 测试覆盖（ExtraInfo 与 Fetch 两个来源）。

#### 同一 cycle 的证据描述同一个文档（M18 修复，与归档无关）

M18 发现 `browser_meta` 的 URL 比同一 cycle 的 DOM 和截图早约 0.25 s：在同一个串行的观测 scope 里它排在 DOM 与截图前面，而整个采集里没有一处记 loaderId。修法：

- `BrowserStateSummary` 多一个可选的文档戳 `document: DocumentStamp(loader_id, url)`。DOM 用采集时读到的根 frame；截图在 `Page.captureScreenshot` 之后读一次 `Page.getFrameTree`；`browser_meta` 以最低的连接优先级最后运行，从 `Page.getFrameTree` 读主 frame 的 url 与 loaderId（`BrowserMetaObs.loader_id`，有默认值的新字段；标题仍取自导航历史）。告诉 agent 的 url 是 frame 的 `url` 加上 `urlFragment`：CDP 的 `Frame.url` 不带 fragment，把它单独放在 `urlFragment` 里，只取 `url` 会让 hash 路由的页面（`/app#/inbox`）丢掉路由（M29 实测，Chrome 155）；出错页仍报 `unreachableUrl`（它本身带 fragment）。
- `_capture_once` 比较本次尝试的戳：已知的有两个以上、且 loaderId 不同 → 记日志，在原有的 `_OBSERVE_MAX_ATTEMPTS` 预算之内重发；预算用完仍不一致就抛错，点名每个 watchdog 的戳，与"缺 fragment 就抛错、从不交出空 fragment"是同一条规则。少于两个戳时不比较（不带戳的假 summary 照样通过）。
- 归档一侧（`dom_raw` 的自采）的戳只记录，不参与这个判断。

#### 观测不与动作发起的导航赛跑（M33 修复，与归档无关）

M31 发现、M33 实测（Chrome 155，fixture 上 BrowserUse `dom_vision`，脚本化的模型；探针在本地的 `tmp/flywheel/m33_design/`）：点击表单的提交按钮（GET 或 POST）之后，下一次观测的 `BrowserStateRequestEvent` 30 s 超时（"2 handler(s) still pending; completed: DOMWatchdog"，未完成的是截图与排在它后面的 `browser_meta`），这一步以 `error_phase="observe"` 结束。两件事凑在一起：

- 点击不等它发起的导航：动作在 `Input.dispatchMouseEvent` 返回后就结束，观测约 5 ms 后开始，`Page.captureScreenshot` 发在 `Page.frameRequestedNavigation` / `Page.frameStartedNavigating` 与提交（`Page.frameNavigated`）之间；
- pinned 的 `--disable-back-forward-cache` 下，表单提交的跨文档导航替换文档时，Chrome 对这一窗口里的 `Page.captureScreenshot` 要么报 `Not attached to an active page`（裸 CDP 探针），要么一直不回应（af 的 run：没有回应，直到事件超时）。

对照：`none` 档位 3/3 通过；只加 `--disable-back-forward-cache`、不开 recorder 也不 pinned，GET 与 POST 都失败；pinned 里只去掉这一个开关 4/4 通过；去掉其余开关（逐个或成组）或 recorder 除 `Page.enable` 之外的全部命令，仍然失败（去掉 `--js-flags` 的 4 次里有 1 次碰巧通过，因为这是一场赛跑）；链接点击在每种设置下都通过；在观测开始前等 0.5 s，GET 与 POST 都通过。recorder、shim、归档器都不相关。

修法在观测这一层（`BrowserCapability` 的观测 cycle 与 `ScreenshotWatchdog`），不在 pinned：那个开关是回放档位的确定性契约（与指纹一起，§4.14 `replayability`），赛跑本身与它无关，它只是让赛跑必输：

- **跟踪**（`core/capabilities/browser/navigation.py`）：`MainFrameNavigation` 是每个 tab 一份的纯状态，不碰 CDP——主 frame 的请求（`requested`）或跨文档的开始（`started`）置起在途，提交（`committed`）或停止加载（`stopped`）清掉；子 frame 与同文档类型（`sameDocument` / `historySameDocument`）不算，不知道主 frame id 时什么都不算。`NavigationFeed` 在控制连接上按该 tab 的 session 把 `Page.frameRequestedNavigation`（只认 `disposition: currentTab`：开新 tab、新窗口或下载不替换这一页）、`Page.frameStartedNavigating`、`Page.frameNavigated`、`Page.frameStoppedLoading` 喂给它，回调在接收循环里按线上的顺序执行。`_attach_tab` 拿到主 frame id 之后建它（`Page.enable` 已经发过），`close_tab`、attach 的回滚与 `teardown` 拆掉；它还听自己 session 的 `Target.detachedFromTarget`（发在 browser session 上、参数里点名这个 session），页面自己关掉的弹窗从不经过 `close_tab`，detach 时它自行解除全部回调并置为已落定，不再有人等它（子 session——iframe、worker——detach 不算）。
- **等待**：`_capture_once` 每次尝试之前 `wait_settled(_NAVIGATION_SETTLE_TIMEOUT_S)`（10 s，模块常量，与 `_OBSERVE_MAX_ATTEMPTS` 同一写法）；到上限仍在途就记一条警告、照常采集（下一条兜底）；上限按尝试算。没有在途导航时这一步立即返回、不让出事件循环（fixture 上实测：链接的提交早于下一次观测，所以链接点击之后也不在这里等）；任何档位上观测时仍在途的跨文档导航都会被等：`none` 档位的表单提交同样等到提交（实测约 15 ms），真实站点上应答慢的链接也一样，看到的是新文档而不是旧的。这一步之后、采集之前还有页面落定的等待（下一节），它在每个档位、每次观测上都花时间，所以观测的时序不再与这个修复之前相同。
- 实测（裸 CDP，Chrome 155，带 pinned 的这个开关）各种交互在该 tab 的 session 上发什么：表单提交（GET / POST）、链接、`location.href` 是请求 → 跨文档的开始 → `frameNavigated` → `frameStoppedLoading`，点击最后一个回复比请求早约 1 ms 到；文本框里回车时请求早于按键的最后一个回复，动作返回时导航已经在途；204 与下载是请求 → 开始 → `frameStoppedLoading`，没有提交；锚点链接（含 `href="#"`）、`pushState` 没有请求，只有 `navigatedWithinDocument`；`mailto:` 是请求 → 开始 → `frameStoppedLoading`；连不上的主机提交的是出错页（`chrome-error://chromewebdata/`），之后 Chrome 自己重载一次（开始 → 停止加载）；`javascript:` 链接、被 `preventDefault()` 拦下的提交、`target=_blank`（链接或表单）、`<a download>` 在这个 session 上没有请求。
- **命令上限**：观测截图的 `Page.captureScreenshot` 有自己的上限（`screenshot_watchdog._SCREENSHOT_COMMAND_TIMEOUT_S`，10 s，远小于事件的 30 s；`browser.screenshot` 动作不受限）：Chrome 不回应的那一次算这次尝试缺 fragment，由原有的 `_OBSERVE_MAX_ATTEMPTS` 重发吸收，不再吃掉整个事件的超时，晚到的回复由客户端丢弃。它兜住跟踪覆盖不到的：检查之后才发起的导航（页面的定时器）。实测：把等待拿掉，表单提交之后那一步 10.1 s、第二次尝试成功（修之前 30 s 后以 `observe` 失败）。观测路径上其余要等 Chrome 回复的命令也都有上限（2026-10-13）：DOM watchdog 的整次采集（cleaner 选的全部命令，进程外 frame 在内）`dom_watchdog._DOM_CAPTURE_TIMEOUT_S` 10 s，超了这次尝试缺 DOM fragment、同样由重发吸收；小的读取——`browser_meta` 的 `Page.getNavigationHistory` 与 `Page.getFrameTree`、截图之后的 `Page.getFrameTree`——`watchdogs/base.py` 的 `OBSERVATION_READ_TIMEOUT_S` 2 s（`observation_read`），超了只少一个文档戳（或 `browser_meta` 的 URL 与标题），不让整个 fragment 失败。一次尝试最坏 10 + 10 + 3 × 2 = 26 s，仍在事件的 30 s 之内，重发还有余地（测试钉住这个不等式）。上限是 YAML 旋钮的探测只能用剩下的 4 s（`capability.CAPTURE_PROBE_ROOM_S`，2026-10-21 裁决）：`capture_state` 的页内探测一次、截图前后的媒体读数两次，`capture_state_timeout_s + 2 × media_state_timeout_s` 不小于 4 s 的配置在构造 capability 时就被拒绝并点名两个旋钮（两个旋钮各自的上限是 3 s 与 1.5 s，默认 1 + 2 × 1 = 3 s），所以探测超时只让它自己的事实记为 unknown，从不让整次采集超出事件。渲染进程不回应（对话框开着、页面脚本占满主线程、文档正在替换）时，一条命令因此最多花掉一次尝试，而不是整个事件。
- **回归**：`test_browser_form_submit_live.py`（真实 Chrome，fixture 的 `form_submit` 页，`replay_driver.record_config` 的形状：pinned + 三个归档器、BrowserUse `dom_vision`、按元素 id 点击的脚本化模型）：GET 与 POST 表单的点击提交、文本框里回车提交（`browser.send_keys`），提交之后那一步 5 s 之内完成（这个修复时实测约 0.1 s，那时还没有下一节的落定等待）、URL 是结果页、loaderId 换了、DOM 里是回显的字段、没有 `observe` 错误；`none` 档位、不开归档的同样流程照旧通过。不用 Chrome 的部分在 `test_browser_navigation_settle.py`（含弹窗自己关掉时 feed 的解除）与 `test_browser_observation_bounds.py`（各条命令上限，以及卡住的 DOM 采集只花一次尝试）；`test_browser_popup_close_live.py` 在真实 Chrome 里让被接管的弹窗自己 `window.close()`，它的 feed 随 Chrome 的 detach 解除，打开它的 tab 照常跟踪。

#### 观测之前等页面落定（与归档无关）

2026-10-13 的 live 冒烟（本地的 `traces/chains/live-smoke/`）把录制与回放的截图逐张对比，发现录制时观测在页面还没渲染完就拍了：nvidia 第 2 步是一张白页，megabus 第 0 步只有首图、订票表单还没出来。录制走真实网络、慢；回放从归档送、快，所以同一步回放时拍到的是渲染完的页面，恢复校验的截图距离不通过（原路径保真 48%，30 个分叉里 25 个以 `RestorationDivergedError: screenshot_distance` 拒绝），而 agent 在每一个 browser run 里都对着没渲染完的页面做决定。用户的裁决（2026-10-13）：每次观测之前等页面落定，录制与回放一样；恢复校验比较的就是 agent 看到的观测，没有只为校验而拍的另一张。

- **等待是观测的一部分**：`_capture_once` 每次尝试先等在途的主 frame 导航（上一节），再等页面落定（`core/capabilities/browser/settle.py` 的 `wait_for_settle`），然后才发出那一个采集所有协商 fragment 的 `BrowserStateRequestEvent`。所以只协商 DOM 的观测、只要截图的观测、两样都要的观测等的是同一个等待，每个 fragment 共享它；判断只看页面状态，从不拍图或比图。半渲染的 DOM 同样会让 agent 看错、让恢复校验的元素签名对不上。
- **落定按顺序判三件事**：(1) **加载**：主 frame 的当前文档收到了 `load` 生命周期事件。`navigation.py` 的 `MainFrameLoad` 是每个 tab 一份的纯状态，由同一个 `NavigationFeed` 在控制连接上按 loaderId 喂给它（`Page.lifecycleEvent`；新的 loaderId 就是新文档；从没听说过的文档不等）。`_attach_tab` 在 feed 建好之后才发 `Page.setLifecycleEventsEnabled`：Chrome 在打开生命周期事件时把当前文档已经到过的事件再报一遍（实测：`commit` … `load`），中途接管的 tab 因此也知道自己加载完没有。(2) **静默窗口**：之后连续 `settle_quiet_ms`（默认 500 ms）没有 DOM 变化（`MutationObserver`：子节点、属性、文本）也没有资源加载完成（`PerformanceObserver` 的 `resource` 条目）。窗口从等待到达页面的那一刻起算，之前发生的不算：动作的后果（点击发出的请求、请求回来之后的渲染）都在动作返回之后才开始。两个观察者装在独立的 isolated world `af-settle` 里（`Page.createIsolatedWorld`，同名的 world 每个文档一个，按名字复用），页面脚本看不到也干扰不了；一条 `Runtime.evaluate`（`awaitPromise`）等页内的 promise 在窗口满足且视口有内容（下面第 (3) 条）、或预算用完时自己结束。一个资源加载完成就是"值得等的网络活动"：图片、字体不改 DOM 也改画面，请求的回应通常随即被渲染；窗口里什么都没发生、只有一个还在途的请求（轮询、流、beacon），不等。(3) **内容**：视口里看得见东西：至少一段看得见的非空白文字，或者一个看得见的图形元素（`img`、`video`、`canvas`、`svg`、`iframe`、`frame`、`embed`、`object`、`input`、`textarea`、`select`、`button`：表单控件自己画出一个框，不论标签在不在视口里），它落在视口里的部分两个方向都不小于 16 CSS px（`_CONTENT_MIN_GRAPHIC_PX`：再小就是细线、1×1 的跟踪像素、画成图片或 SVG 的加载条）。CSS 涂的颜色不算内容：几 px 高的加载条、骨架屏、整页的底色都是背景。窗口静了而视口空着的页面不算落定，在同一个上限之内接着等；内容出来时页面通常又在变（渲染本身就是 DOM 变化），要再静一个窗口。来由：2026-10-22 的前瞻数据（本地的 `tmp/lookahead_15task_pixels_20261022/`）341 次观测里有 2 次记成 `settled`，截图却是只有顶端一条 3 px 加载条的白页——同花顺问财的搜索结果页，一个单页应用，`load` 之后主体脚本还在下载，其间 DOM 不变、也没有资源完成，静默窗口就满足了，约 1 s 之后的截图里界面已经出来；其中一张进了人工标注队列，标注者看到的是白屏。用户的裁决（2026-10-22）：视口空着的页面不算落定。判断在同一条探针里：窗口静了就看一次，空着就每 100 ms（`_CONTENT_RECHECK_MS`）再看，因为内容也可能不经 DOM 变化出现（CSS 动画或过渡把已有的元素显出来）。看一次是按文档顺序走 DOM（进入开放的 shadow root），找到第一个看得见的内容就停：`display: none`、`opacity: 0`、被 `content-visibility` 跳过的子树整棵跳过（`checkVisibility`；`display: contents` 自己没有盒子，照样进去），图形元素里面不再走。一段文字看得见：它的矩形（`Range.getClientRects`）落在视口里的部分两个方向都不小于 4 CSS px（`_CONTENT_MIN_TEXT_PX`），父元素 `visibility: visible`，父元素裁剪溢出时只算裁剪框里的部分，所以 `font-size: 0` 与读屏专用文字的 1×1 框不算（Bootstrap 的 CSS 转圈里那句 `visually-hidden` 的 "Loading..."）。图片要 `complete`（还在下载的不算），`svg` 要有子元素（空的画布不算）。一次最多走 5,000 个节点（`_CONTENT_MAX_NODES`），走满了还没找到就算有内容：只有滚过了大量已渲染内容的长页面才走得到这么远，那不是空壳，而这个上限让看一次的开销在任何 DOM 上都有界（下面"实测"）。`about:blank` 是浏览器自己的空文档，没有会来的内容，算有内容、不等。
- **上限**：整个等待最多 `settle_max_wait_s`（默认 5 s），到了就照常采集：轮播、视频、ticker 这类永远不静的页面、视口一直空着的页面（真的空白的页面、一直等不来内容的加载壳）都按当时的样子拍，从不无限等。一个文档的加载在所有观测里加起来最多等一个上限，加载永远结束不了的页面只在第一次付这个上限；静默窗口每次观测都量。`settle_max_wait_s: 0` 关掉整个等待，观测立即采集；`settle_quiet_ms: 0` 只等加载与内容；窗口比上限长的组合在构造时报错，点名两个旋钮（窗口永远量不满，每次观测都会等满上限）。读不到页面状态（不知道主 frame、文档拒绝 isolated world、回复不是探针的）就立即采集；渲染进程忙到不回应探针（页面脚本占满主线程、对话框开着），在上限时采集。等待期间页面换了文档（客户端跳转），在同一个上限之内等新文档。两个旋钮是 `BrowserCapability` 的标量，原样记进 `config.json`（§5.6）；发布的 YAML 不写它们，默认值录制与回放都适用。
- **记录**：每次采集的等待记成 `PageSettle{waited_ms, outcome: settled | capped | unknown, waiting_for: [load | dom | content], detail, main_document_loaded}`（`capped` 时 `waiting_for` 是等待没看到落定的那几项，按判断的顺序：上限到时页面没满足的条件，以及等待没走到的条件——`load` 没等到是 `[load, dom, content]`，探针没回应是 `[dom, content]`，一直在变的有内容的页面是 `[dom]`，只有 `[content]` 是页面静了、视口一直空着；`unknown` 时 `detail` 说读不到的原因；`main_document_loaded` 是等待结束时主 frame 当前文档是否已到 `load`，与等待怎么结束无关，读不到为 `None`，见 §4.4 “初次加载是否完成”），同一份放在这次采集的每个 browser fragment 上（`observations.py` 的 `BrowserCaptureFragment.settle`：`browser.dom`、`browser.screenshot`、`browser.browser_meta`）。放在 fragment 上，而不是 `loop.step_info` 或 `StepRecord`：等待是 capability 采集过程的一部分，与 `captured_at` 一样是采集的出处；Loop 不知道浏览器等了多久，`StepRecord` 是所有 capability 共用的；而无论 run 协商了哪个 browser fragment，读者都能在它上面找到。等待关掉时字段是 `None`，trace 的 JSON 里不写这个键，没有等待的 fragment 写出的字节与以前相同。度量报告在归档开销一节按角色给出 p50 / p95 与上限截断的占比（§4.16，`settle.py::recorded_settle` 读它）。
- **实测**（2026-10-13，本机网络，Chrome 155 headless、桌面 UA、`prefers-reduced-motion: reduce`、每个站点一个新 profile，40 个真实站点；脚本与数据在本地的 `tmp/flywheel/settle/`）：每个站点按时间记下全部 DOM 变化与资源完成、主 frame 的生命周期事件，以及导航之后 0.5–12 s、滚动之后 0.25–6 s 的截图，再按这些时间线模拟各种等待规则。"拍全了"指采集时刻之后的截图与最终截图的 dHash 距离都不超过 5（恢复器的默认阈值）；12 s 内画面从没稳定下来的站点（轮播）不计。窗口 500 ms、上限 5 s：

  | 场景 | 立即采集（这个修复之前） | 加上等待 | 等待 p50 / 平均 / p95 | 上限截断 |
  |---|---|---|---|---|
  | `navigate` 之后（观测从 `load` 开始，26 个站点） | 11/26 拍全 | 21/26 | 1.14 / 1.75 / 5.0 s | 2/26 |
  | 点击引起的导航之后（从新文档提交开始，32 个站点） | 0/32 | 18/32 | 5.0 / 4.34 / 5.0 s | 21/32 |
  | 滚动之后（同一个文档，38 个站点） | — | 37/38 | 1.20 / 1.98 / 5.0 s | 6/38 |

  取舍的依据，同样的数据：窗口 300 ms 时 navigate 之后只有 17/26 拍全；1000 ms 与 500 ms 同为 21/26，平均多等 1.2 s。上限 3 s 时点击导航之后只有 11/32，8 s 时 21/32、平均多等 1.6 s。再加 Chrome 的 `networkAlmostIdle`（至多两个请求在途 500 ms）作为第三个条件：navigate 之后平均多等约 1 s（1.75 → 2.71 s），拍全的站点数不变（21/26），点击导航之后也不变（18/32），所以不用它；在 40 个站点里，`networkIdle` 有 19 个、`networkAlmostIdle` 有 9 个在导航之后 18 s 之内从没出现。只算 DOM 变化、不算资源完成：navigate 之后 17/26、滚动之后 33/38。在真实的 `BrowserCapability` 上沿录制的路径复跑（pinned）：nvidia 点开搜索之后的观测等了 0.67 s，拍到渲染完的搜索面板（关掉等待时拍到的是还在淡出的 cookie 对话框，第二次点击落在它上面，搜索面板没打开）；megabus 第 0 步等了 1.29 s，订票表单在图里。
- **内容这一条的实测**（2026-10-22，本机网络，Chrome 155 headless，每次一个新 profile）：同花顺问财那个 URL（`search.10jqka.com.cn/search/result?w=300750…`）作起始页，经 `BrowserCapability` 的观测各采集 5 次。只有前两条时，2 次拍到只有加载条的白页、1 次只拍到侧栏文字，都记成 `settled`（等了 0.84–0.89 s）；加上这一条，5 次都拍到渲染出的页面（未登录的 profile 弹出登录框），都是 `settled`，等了 2.0–3.8 s。每 100 ms 读一次那一页：文档到达之后视口空了 3 s 多（55 个元素、没有文字），其中 DOM 有 2 s 多一点没变。看一次的开销：本地的空页面不到 0.1 ms，问财的空壳（32 个节点）0.2 ms；十个真实站点（百度、Hacker News、bilibili、GitHub、网易、Wikipedia、京东、知乎、Amazon、豆瓣）在顶部、滚到中间、滚到底各看一次，走 8–2,992 个节点就找到内容，最多 4.5 ms（最长的是豆瓣滚到底）；走满 5,000 个节点约 10 ms（20,000 段文字、滚过了它们）。
- **回归**：`test_browser_settle.py`（fake CDP：加载跟踪与它的总预算、静默且有内容的页面早结束、忙碌的页面在上限结束、静了而视口空着的页面在上限结束并记 `[content]`、不回应的页面、读不到状态立即采集（含回复里没有内容判断）、等待中换了文档、探针带着内容检查与它的具名上限、等待先于采集、每个 fragment 同一份记录、只协商 DOM 也等、`0` 关掉、每次尝试各等一次、trace 往返）；`test_browser_settle_live.py`（真实 Chrome，本地页面在 `load` 之后经三个各 300 ms 的请求才渲染主体：截图里有它，只协商 DOM 的观测里也有它；关掉等待的对照组两样都没有；永远不静的页面在上限采集；只有一条 3 px 加载条、唯一的请求 1.5 s 后才回来的页面，截图里有回来的内容，记成 `settled`；一直空着的页面在上限采集、记 `[content]`；一组小页面上的内容判断：文字、`display: contents` 里的文字、开放 shadow root 里的文字、图标、canvas、标签在视口外而框露出一截的按钮、`about:blank` 算有内容，CSS 加载条、CSS 转圈加读屏文字、`opacity: 0` 的根、空 `svg`、3 px 高的 `svg`、还在下载的图片、一大块 CSS 颜色、视口之外的文字不算）。加载条与一直空着的两个页面在只有前两条的规则下不通过（白页被拍下、空页面记成 `settled`）。fixture 上的录制、回放、分叉、导出与归档测试（`replay_driver.record_config`、`archive_bench.LiveRecording`）把窗口设成 100 ms：fixture 的页面是静态的、从本机送，短窗口不丢它们要验的东西，整套测试也不因此慢下来；默认值由上面两个测试和用发布配置跑的演练（§4.16）覆盖。点击视口外元素的 fixture 页面（`effects_site` 的 `far*.html`）开头有一行看得见的文字：只有一个视口外按钮的页面视口是空的，每次都会等满上限。演练里 drift 站点的每次回放现在把错误页的 URL 请求两次：观测等页面落定时 run 还在，赶上了 Chrome 约 1 s 后对错误页的自动重载（第二次在终止观测的收集里）。

#### `browser.navigate`：先等提交，再等 `load`，`load` 不来就照常继续（与归档无关）

2026-10-14 筛选 LexBench L1 的中文站点时（本地的 `tmp/flywheel/followup/live2/screening.md`），第一财经与经济观察网的首页早已画出来，`load` 事件却一直没来（有资源一直加载不完），setup 等了 30 s 之后报 "could not open start_url"。原因是两个各 30 s 的时钟：`NavigateToUrlEvent.event_timeout`（调用方 `await event` 的上限，agent-cdp 也拿它限制 handler 本身）从发出事件就开始计时，生命周期等待（`lifecycle_timeout_ms` → `timeout_ms` → 30 s）在 `Page.navigate` 回应之后才开始，所以总是前者先到，handler 注释里承诺的"等不到就记一条警告、照常继续"永远走不到。这对每一个会导航的 agent 都成立。用户的裁决（2026-10-14）：主 frame 的导航提交了而 `load` 没按时到，记警告、照常继续；从没提交的导航仍然失败；调用方的等待与生命周期等待分开。

- **两段等待，先后各有上限**（`ActionWatchdog._on_navigate`）：(1) **提交**：Chrome 在导航提交或失败时才回应 `Page.navigate`，最多等 `timeout_ms`（`browser.navigate` 的旋钮，默认 30 s）；(2) **生命周期**：回应里的 `loaderId` 那个文档到达 `wait_until`（默认 `load`），最多等 `lifecycle_timeout_ms`（不写时同 `timeout_ms`），从回应开始计时。两个上限都在事件上（`NavigateToUrlEvent.commit_timeout_s` / `lifecycle_timeout_s`）。
- **结果**由 handler 决定，失败时它回复 `{"error": ...}`（与点击、输入同一个约定，`actions/_common.py::handler_error`），`NavigateAction` 据此返回 `success=False`：
  - 按时回应、等到了 `wait_until` → 成功；
  - 按时回应、`wait_until` 没按时到 → 记一条 WARNING（点名 URL、阶段与等了多久），成功：页面已经是新文档，后面的观测照常采集；
  - 回应里没有 `loaderId`：同文档导航（只改了 fragment），没有新文档可等 → 立即成功；
  - 没按时回应 → 失败：`navigation to '<url>' did not commit within 30 s`；
  - Chrome 拒绝这条命令（例如它认为无效的 URL）→ 失败，带 Chrome 的错误；
  - 回应带 `errorText`（CDP：只在导航失败时出现）→ 立即失败，带这段文字。
- **实测**（Chrome 155，本机页面；探针在本地的 `tmp/flywheel/followup/start_pages/probe_nav.py`）：正常页面的回应在提交时到，`load` 随后；一张图片的响应永远不结束时，回应与 `DOMContentLoaded` 照常到，`load` 永远不来；服务器一直不回应时 `Page.navigate` 一直没有回应；连不上的端口回应带 `errorText`，随后提交 Chrome 的出错页（`chrome-error://chromewebdata/`，它也有 `load`）；204 与下载回应带 `errorText: net::ERR_ABORTED` 和一个 `loaderId`，却什么都不提交（没有 `frameNavigated`、没有生命周期事件），所以等它们的生命周期只会等满上限。连不上的主机因此从"成功、页面是出错页"变成"失败、页面仍是出错页"：setup 打开不了起始页时以 `could not open start_url` 结束，不再让 agent 对着出错页花模型调用；agent 自己的导航失败时，下一次观测看到的仍是出错页。
- **调用方的等待比两段加起来更长**：事件不写 `event_timeout` 时由它自己推出：提交上限 + 生命周期上限 + `NAVIGATION_EVENT_MARGIN_S`（10 s），默认 70 s。handler 的答复因此总在调用方放弃之前到，上面每一种结果都由 handler 决定，而不是看哪个时钟先到。
- **与观测的落定等待**（上一节）是两份预算：导航的生命周期等待属于动作，落定等待属于观测，`MainFrameLoad` 只记观测为一个文档等过多久。`load` 永远不来的页面因此一共多花：导航的生命周期上限（默认 30 s）一次，之后第一次观测的落定上限（默认 5 s，记为 `capped`、`waiting_for: [load, dom, content]`）一次；之后的观测不再等它的 `load`，只量静默窗口。`load` 在上限之内到了的页面，导航等到它，观测的落定只量静默窗口。
- **回归**：`test_navigate_lifecycle.py`（fake CDP：提交超时、Chrome 拒绝、`errorText`、同文档导航、生命周期超时只记警告、事件上限由两段推出）；`test_browser_navigate_live.py`（真实 Chrome，本地页面里一张图片的响应永远不结束：setup 在生命周期上限之后记警告并成功，第一次观测在落定上限处采集、第二次不再等 `load`；服务器一直不回应的导航在提交上限之后失败，setup 报 could not open start_url；204 立即失败）。

#### 页面新开的标签页：焦点跟过去（与归档无关）

2026-10-14 为 LexBench L1 的中文站点做零 token 筛选时（本地的 `tmp/flywheel/followup/live2/screening.md`）发现：agent 的焦点只有 `browser.switch_tab` 能移动，UI-TARS 又没有这个动作，于是点了一个在新标签页打开的链接，下一次观测看到的还是原来的页面，像是点击没有发生。选出的站点里这样的链接很多（机核 81/81、起点 886/960、AcFun 367/451、腾讯视频 44/80）。用户的裁决（2026-10-14）：动作让当前页打开了新标签页，就像真实浏览器一样把它提到前台，下一次观测看到新页；对所有 agent 一样，录制、回放、分叉按同一条规则走；capability 上加一个封闭选项（跟随 / 不跟随），默认跟随。

- **选项**：`BrowserCapability.new_tabs: follow | stay`（默认 `follow`；封闭的集合，原样记进 `config.json`）。`stay` 就是这之前的行为：焦点留在原来的 tab，新 tab 列在 `browser_meta` 的 tab 列表里，只有 `switch_tab` 能过去。录制、回放与分叉要用同一个值（发布的配置都用默认值）；它不进环境指纹，两边不一致时，第一处该跟随的地方就以 `url_mismatch` 发散。焦点离开的 tab 照常运行还是冻结，由另一个选项 `background_tabs` 决定（下面"离开焦点的 tab"）。
- **跟哪个 tab**（`core/capabilities/browser/tabs.py` 的 `tabs_to_follow`，纯函数）：从上一次观测之后 agent 的第一个动作开始、到下一次观测采集为止（"跟随窗口"），由窗口开始时焦点所在的 tab 打开的 page target（`Target.targetCreated` 的 `openerId` 是它），采集时还开着，Chrome 也没有报告它是在没有用户激活的情况下打开的（`Page.windowOpen{userGesture: false}`），里面最后打开的那一个：真实浏览器里每个前台新 tab 都盖在前一个上面。没有用户激活的那些（页面加载时自己弹的、计时器弹的、一次点击开的第二个窗口）真实浏览器的弹窗拦截会拦下；af 的 Chrome 关着拦截（`--disable-popup-blocking`），它们照样开着、列在 tab 列表里，焦点不过去。跨站 iframe 里的链接开的 tab 收不到 `Page.windowOpen`（它发在 iframe 自己的 session 上，控制连接不听），按"不知道"处理，照样跟。窗口里 `switch_tab` 成功之后，窗口从新焦点重新开始：agent 自己选了看哪里，之前开的 tab 不再跟，新焦点之后开的照样跟。不跟的：观测采集之后、下一个动作之前页面自己开的 tab（计时器、广告）；窗口开始时焦点所在 tab 之外的 tab 开的；setup 打开起始页时开的（起始页不是 agent 的动作，走内部的同一条执行路径 `_run_action`，不开窗口）。
- **自己关掉的 tab**（`tabs_after_close`）：焦点所在的 tab 自己关掉（`window.close()`）时，焦点回到打开它的 tab（还开着的话），否则回到最近打开的、还开着的 tab。这比真实 Chrome 简单：Chrome 先去被关掉的 tab 自己打开的 tab，再去与它同一个 opener 打开的 tab，然后是 opener，最后是 tab 条上的邻居，并在无关的 tab 切换后忘掉 opener 关系；agent 的 run 通常只开一两个 tab，那时两条规则落到同一个 tab，所以照这条简单规则（用户 2026-10-24 的裁决）。这一条与选项无关：`stay` 下焦点也没有可以留的地方。一个 tab 都不剩时这次观测失败，错误点名那个 tab（整个 cycle 一个错误，同一个 cycle 的其余 fragment 拿到同一个异常）。焦点所在的 tab 已经关了、观测还没把焦点移走时执行的动作立即失败，错误说那个 tab 自己关了、下一次观测看到焦点移去的 tab。agent 用浏览器的关闭 tab 快捷键关掉焦点 tab 时（§4.13 "浏览器自己的快捷键"，`BrowserCapability.close_focused_tab`），焦点按同一条规则移动，在那个动作里就移，不记 `focus_moved`；只剩一个 tab 时拒绝关闭。
- **怎么知道**：控制连接在 setup 时、接管初始 tab 之前开 `Target.setDiscoverTargets{discover: true, filter: [{type: "page"}]}`（已经开着的 tab 在它的回复之前就报上来），`TabRegistry` 按打开的先后记下每个 tab（`type: page`，没有 `subtype` 的 target）、它的 opener、URL、是否已关，以及与它配对的 `Page.windowOpen`：同一个 opener 的 session 上、它之前 2 s 之内到的那一条（实测先到约 10 ms；控制连接接管的每个 tab 都开着 `Page.enable`，所以收得到）。浏览器不接受 discovery 时记一条 WARNING，照这之前的样子跑：不跟随、不离开关掉的 tab、tab 列表按 `Target.getTargets` 的顺序。
- **什么时候移**：`execute_action`（agent 的动作）在没有窗口时开一个；观测的第一个 fragment 先决定焦点（`_settle_focus`）：窗口开着、或者已知焦点 tab 关了时，先发一次 `Target.getTargets`（屏障：此刻开着的 tab 的 `targetCreated` 都在它的回复之前到；它也说出哪些 tab 还开着），再按上面两条规则移；之后才是等在途的导航与等页面落定（上面两节），所以两样都在跟过去的 tab 上做。等完、采集之前再按同一规则查一次（只读事件，不发命令）：等待期间才开的 tab（点击的处理函数先发请求、回来再 `window.open` 的页面）或刚关掉的焦点 tab 也照规则处理，然后在新焦点上重新等，不算一次重发。一次观测最多移 3 次焦点（页面每被跟一次就再开一个 tab 时，不让观测一直等下去）。采集完窗口关闭，无论这次采集成败。
- **怎么移**：与 `switch_tab` 同一段代码（`_move_focus`）：懒接管（`_attach_tab`）、`Target.activateTarget`（实测：新 tab 变 `visible`，原来的变 `hidden`，与真实浏览器一样）、丢掉它旧的 cycle 缓存；`background_tabs: freeze` 时，要去的 tab 若是焦点离开它时冻结的，先设回运行，再 `Target.activateTarget`，之后冻结离开的 tab（下面"离开焦点的 tab"）。跟随的接管有上限（`_FOLLOW_ADOPT_TIMEOUT_S`，10 s，与等在途导航同一个上限）：第一个导航还在途的 tab，第一条页面一侧的命令（`Page.enable`）要等那个导航提交才应答（实测：文档 2 s 才回，`Page.enable` 2.0 s 后才应答，不论 tab 能不能够到 opener），所以接管本身就等到了第一次提交；之后 `Page.setLifecycleEventsEnabled` 让 Chrome 把当前文档的生命周期事件再报一遍，落定等待由此知道它加载完没有。接管不了的（超时、tab 已经没了）这次运行里不再试，换更早的候选，都不行就留在原处并记 WARNING。接管过、又被页面关掉的 tab，它的 bridge、导航跟踪、watchdog 与各张映射在下一次决定焦点之后释放（与 `close_tab` 同一段代码 `_release_tab`，只是不再关 target）；它的两个 scope 空着留到 teardown 一起关：agent-cdp 关一个 scope 实测约 250 ms，不该算在发现 tab 已关的那次观测上。
- **trace 里看得出焦点移了**：这次采集的每个 browser fragment 上都有同一份 `focus_moved: FocusMove{reason: new_tab | tab_closed, from_tab, to_tab}`（tab 是 CDP targetId，即 `browser_meta.tabs` 的 `tab_id`；`from_tab` 是这次观测开始时的焦点，`to_tab` 是采集的那个 tab），与 `settle` 一样放在 `BrowserCaptureFragment` 上：采集之前移了焦点，是这次采集的出处，只协商截图的 run（UI-TARS）也找得到。没有移、或者移了又回到原处时是 `None`，trace 的 JSON 里不写这个键，字节与以前相同。agent 自己的 `switch_tab` 不记在这里，它就是那一步的动作。所以离线读者在第 t 步观测的任一 browser fragment 上就看得到：采集之前焦点从哪个 tab 移到哪个、为什么（`new_tab`：上一步的动作让原来的 tab 开了这个 tab）。
- **agent 不变**：没有 agent 渲染 `focus_moved`，提示词只因为观测本身看到了新页面而不同；同一个观测，UI-TARS 的提示词与这之前逐字节相同（测试按这之前那个提交上测出的摘要钉住），三个 agent 有没有这条记录提示词都相同。`switch_tab` 照旧可用。
- **tab 列表按打开的先后**：`browser_meta.tabs` 与 `switch_tab` 的候选按 `TabRegistry` 的先后排，不再按 `Target.getTargets` 的顺序（实测不是打开的先后：打不到 opener 的 tab 排在 opener 前面，`window.open` 开的排在后面）；所以 `switch_tab` 的 `target_id: null`（"最近打开的 tab"）名副其实，恢复时按位置重定位 `switch_tab`（下面"恢复"），两边的位置也是同一个意思。
- **回放与分叉**：规则不看网络来源。`archive` 回放里同一个动作开同一个 tab（第一个文档从归档送出，见上面的事实表与 `test_browser_replay_live.py`），焦点照样跟过去。前缀回放经 `act_fn` 重新执行录下的动作、经 `env.observe` 观测，窗口与跟随和录制时一样，所以焦点的移动被重现；恢复校验比较的是采集到的那个 tab 的 URL、元素签名与截图，没有重现的移动表现为 `url_mismatch` / `screenshot_distance`。`core/loops/restore/` 从不指名哪个 tab，没有要改的地方。
- **实测**（2026-10-14，Chrome 155 headless；探针在本地的 `tmp/flywheel/followup/follow_tabs/`）：不带 `rel`、`rel=noopener`、`rel=opener` 的 `target=_blank` 链接，`window.open` 带不带 `noopener`，`target=_blank` 的表单，同站与跨站 iframe 里的链接，`targetCreated` 的 `openerId` 都是打开它的 tab（iframe 里的链接另有 `openerFrameId`），都在点击的回复之前到；`Page.windowOpen` 在打开它的 tab 的 session 上，早 7–13 ms，`userGesture` 对点击是 `true`（处理函数 800 ms 之后、或等一个 fetch 回来再开的也是），对页面加载时自己弹的与一次点击开的第二个窗口是 `false`，跨站 iframe 里的链接没有；下载开的 tab 约 75 ms 后被 Chrome 关掉；204 开的 tab 留在空白页；接管自己关掉的 tab 之后，`Target.detachedFromTarget` 与 `targetDestroyed` 同时到，之后发到它 session 的命令立即报 "Session with given id not found"。
- **残余**：跟过去的 tab 第一次布局用的是窗口大小，接管时才收到 viewport 的 override（`local_chrome` 默认窗口 1280×720，与默认 viewport 相同）；接管超过 10 s 的 tab（第一个文档一直不回）不跟，agent 看到的是没有变化的原页面；204 开的空白 tab 也会被跟过去（真实浏览器同样显示它），没有 `switch_tab` 的 agent 只能等它自己关掉；同一个点击开的多个 tab，后到的 `Page.windowOpen` 与先到的 tab 不再配对（2 s 之后过期），极少见的乱序会让一个没有激活的 tab 被当成"不知道"而跟过去。
- **回归**：`test_browser_tabs.py`（纯函数：打开的先后、配对与过期、窗口、opener、已关、没有激活、关掉之后去哪、trace 往返）；`test_browser_follow_tab.py`（fake CDP：跟过去并记下、多个取最后、没有激活不跟、不知道照跟、窗口外不跟、别的 tab 开的不跟、起始页不开窗口、`stay`、等待中才开的、`switch_tab` 重开窗口、接管超时换上一个、关掉回 opener / 最近的、`stay` 也离开关掉的 tab、对关掉的 tab 动作失败、一个 tab 都不剩、tab 列表的顺序、浏览器拒绝 discovery、teardown 放开回调）；`test_browser_follow_tab_live.py`（真实 Chrome，fixture 的 `new_tab` 页：四种打开方式都跟过去、`stay`、跟过去的 tab 自己关掉、计时器与加载时弹的不跟、`switch_tab` 照旧；录一个跟过新 tab 的 run，离线回放沿同一路径、分叉的前缀恢复逐步校验通过且焦点的移动被重现）；`tests/test_agents/test_focus_moved_prompts.py`（三个 agent 的提示词不受这条记录影响，UI-TARS 的提示词按这之前的摘要钉住）。

#### 离开焦点的 tab：照常运行或冻结（`background_tabs`）

iqiyi.com 的 tab 经 `localStorage` 互相同步，开到五个 tab（其中三个搜索页）时同步失控：每个 iqiyi 渲染进程都占满 CPU、长到约 2 GB，观测的截图拿不到回复，run 以 `ObservationFailedError` 结束；与 pinned 的时钟无关（§4.14 replayability 一节的"残余"）。观测一侧救不回，止住它的办法都要改变页面状态。用户的裁决（2026-10-23）：两种办法都做成配置，一个是 `local_chrome` 的启动选项 `background_throttling`（§4.9.x "后台 tab 的节流"），一个是这里的 capability 选项，在焦点改变时用 CDP 冻结离开的 tab，与 provider 无关。

- **选项**：`BrowserCapability.background_tabs: run | freeze`（默认 `run`；封闭的集合，原样记进 `config.json`）。`run` 就是这之前的行为：离开的 tab 在后台照常运行。`freeze`：焦点离开一个 tab 时（跟随它打开的新 tab、agent 的 `switch_tab`），新 tab 提到前台之后，对离开的 tab 发 `Page.setWebLifecycleState{state: frozen}`；焦点回到一个这样冻结的 tab 之前（`switch_tab`，或焦点 tab 自己关掉之后回到 opener / 最近打开的 tab），先发 `{state: active}`，再 `Target.activateTarget`。都在 `_move_focus` 里（`_freeze_tab` / `_resume_tab`），所以跟随、`switch_tab`、离开关掉的 tab 走同一条路。焦点 tab 自己关掉时离开的是一个已关的 tab，没有可冻结的；焦点从没到过的 tab（没跟随的弹窗、`new_tabs: stay` 下的新 tab）不冻结（用户的裁决只说失去焦点的 tab）。冻结或设回运行的命令失败只记 WARNING：冻结失败的 tab 照常运行，设回运行失败时 `Target.activateTarget` 照样让它恢复（实测，下面）。命令上限 `_LIFECYCLE_TIMEOUT_S`（2 s）：它由浏览器进程应答，不经页面（实测约 1 ms，tab 的主线程卡在一个 4 s 的任务里时也一样，冻结在那个任务结束后生效）。
- **冻结做了什么**（实测，2026-10-23，Chrome 155 headless，本机页面；回归 `test_browser_background_tabs_live.py`）：页面先收到 `freeze` 事件，之后它的计时器、脚本、`storage` 事件、网络回调都不再跑，进程外 iframe 一起停；设回运行时收到 `resume`，冻结期间排队的事件照常送达。两个经 `localStorage` 永不收敛地互相改写的 tab（每个约每秒 330 次写），离开的那个冻结之后，焦点 tab 的写降到 0，`Runtime.evaluate` 往返仍在毫秒级；只节流（`background_throttling: true`）照样每秒三百多次。冻结之前发出、冻结期间才回来的请求：recorder 在 Fetch 的 Response 阶段照常暂停它、`Fetch.getResponseBody` 照常应答（0.5 ms）、照常放行，页面要到设回运行之后才读到它（`Network.loadingFinished` 也在那时才到）。冻结时在途的主 frame 导航照常提交。冻结的 tab 能被 `Target.closeTarget` 关掉。
- **页面看得出的代价**：正在播放的（不静音的）`<audio>` / `<video>` 冻结时被暂停，设回运行、回到前台之后仍是暂停；`run` 下它在后台照常播放（静音的媒体 Chrome 在后台本来就暂停，回到前台自己恢复，两种设置一样）。所以焦点回到一个冻结过的 tab 时，agent 看到的视频是停着的。计时器在冻结期间一拍不走，页面上的倒计时、轮询回来时停在离开的那一刻。这是 `freeze` 改变页面状态的地方。
- **什么会把冻结的 tab 唤醒**（实测）：对它 `Page.captureScreenshot`（截一张就让它恢复运行；之后再发 `frozen` 不起作用，要先 `active` 再 `frozen`），以及对它 `Target.activateTarget`。其余命令都在约 1–5 ms 内应答、不唤醒它：主 world 与 isolated world 里的 `Runtime.evaluate`、`Page.getFrameTree`、`Page.createIsolatedWorld`、`Storage.getStorageKey`、`DOMStorage.getDOMStorageItems`、`DOM.getDocument`、`DOMSnapshot.captureSnapshot`、`Target.getTargets`，以及另开一个 session 的 `Target.attachToTarget`、`Page.enable`、`Emulation.setDeviceMetricsOverride`。
- **谁都不等冻结的 tab，也不唤醒它**：截图只截焦点 tab——观测、`browser.frames`（两种帧都拍动作或观测时的焦点 tab）、媒体的读取与对齐都只在焦点 tab 上；`browser_meta` 的 tab 列表来自 `Target.getTargets`，冻结的 tab 照样列出（`is_active` 为 false），列表不加字段，提示词不变；`browser.storage` 遍历每个 target 与 frame，它的四种读在冻结的 tab 上都照常应答、不唤醒它（实测：一次收集期间冻结的 tab 没有跑任何一拍，`unavailable` 为空）；指纹探针读第一个应答的 tab，同理；`browser.dom_raw` 只读观测的 tab；`browser.network` 的 Fetch 暂停由浏览器进程处理（上面），流式读的 body 要等页面读，本来就不在收集时等；离线回放的 `Fetch.fulfillRequest` 同理。所以冻结的 tab 不让任何收集多等一次超时。
- **残余**：`browser.frames` 的拍摄超时之后，动作不再等它（`max_action_wait_s`），那条 `Page.captureScreenshot` 仍在途；动作是 `switch_tab`、或观测随后跟随新 tab 时，它可能在冻结之后才到，把刚冻结的 tab 唤醒，那个 tab 就照常运行到焦点下一次回到它。只在焦点 tab 的截图本来就超时时发生，没有改。
- **teardown**：capability 拥有的浏览器（provider 启动的、实验共享浏览器里本 run 的 BrowserContext）连同冻结的 tab 一起关掉，不发命令；不拥有的（显式 `cdp_url`、测试注入的控制客户端）tab 留在 run 之后，teardown 把本 run 冻结的、还开着的 tab 设回运行，失败与其余清理失败一起报。
- **录制、回放与分叉**：规则不看网络来源；前缀回放经同一个 `_move_focus` 重做焦点的移动，冻结也随之重现。`background_tabs` 进环境指纹（`env/run.browser.json` 的 `background_tabs`），离线回放与录制不同就拒绝（下面"一致性检查与回放指纹"）：录制里冻结的 tab 什么也没请求，回放让它运行就会去要录制里没有的东西，`storage` 的互相改写也回来了；反过来，焦点回去时媒体与计时器的状态也不同。与 `new_tabs` 不同，这一项不一致没有 `url_mismatch` 那样的信号，所以由指纹拒绝；指纹里没有这一项的源 run（这之前录的）也拒绝，要重录。
- **trace 里看得出哪些 tab 冻结了**：规则是确定的——`freeze` 下，焦点离开过、还开着、焦点没回去的 tab 就是冻结的。焦点的历史在 trace 里（agent 的 `switch_tab` 动作、观测 fragment 上的 `focus_moved`），`config.json` 有 `background_tabs`。
- **iqiyi 实测**（2026-10-23，Chrome 155 headless，`replayability: none`，四个录制归档器，脚本化的模型，每步"思考" 15 s；路径同上一节的残余：搜索页 → 剧集页 → 剧集页顶栏搜索开第三个 tab → 剧集页 → 再搜一次开第五个 tab；探针与脚本不入库，同一时刻只跑一个 run，因为两个并行的 run 让剧集页的顶栏常常渲染不出来，脚本到不了五个 tab）：
  - 两项都不开（之前的默认）：第五个 tab 打开 5 s 之内所有 tab 都不再应答（旁路连接每 2 s 一次、上限 1 s 的 `Runtime.evaluate`），渲染进程 CPU 合计到 683%（四个各 130–160%），最大的一个长到 1.6 GB，机器负载 212，观测的截图两次拿不到回复，run 在第 10 步以 `ObservationFailedError` 结束（1/1，与 10-22 的两次相同）。
  - 只开节流：一样，第五个 tab 打开几秒之内全部不应答，渲染进程合计 658%、最大 0.9 GB，持续 24 s 后被探针的保护杀掉（不让它拖垮同机的其他 run），run 以 `ObservationFailedError` 结束（1/1）。
  - 只冻结：五个 tab 都开了，之后照常跑完全部 22 步（`done`）；第五个 tab 打开之后渲染进程合计最多 108%（中位数 2%），四个冻结的渲染进程 0.0%，最大的 515 MB，负载 ≤ 12；旁路连接对每个 tab 的 480 次 `Runtime.evaluate` 都应答（最慢 7 ms）；录制完整，`browser.storage` 每步读到五个 tab、没有 unavailable，`browser.frames` 的帧都拍到。另两次只冻结的 run 剧集页的顶栏一直没渲染出来，脚本搜不了，没到五个 tab，不算；这与冻结无关：同样的打开路径（搜索页点第一个结果，跟随过去）单独实测，`run` 3 次里 2 次、`freeze` 3 次里 1 次顶栏 30 s 内没渲染，把冻结的搜索页设回运行也没让它渲染出来，`navigator.locks` 里没有持有或等待的锁。
  - 两项都开：与只冻结相同（五个 tab，22 步跑完，合计最多 115%，最大 454 MB，475 次应答最慢 20 ms）。
  - 所以止住 iqiyi 的是冻结；节流不起作用（与本机页面上的结论一致：`storage` 事件不是计时器）。
- **回归**：`test_browser_background_tabs.py`（fake CDP，按 Chrome 155 的实测给每个 tab 记冻结状态，截图或 `activateTarget` 唤醒它：跟随时先 `activateTarget` 新 tab 再冻结原来的、`switch_tab` 回去时先设回运行再 `activateTarget` 再冻结离开的、切到焦点 tab 本身什么也不冻结、焦点 tab 关掉时不冻结它而回去的 tab 先设回运行、冻结的 tab 被页面关掉后忘掉、`stay` 下没跟随的 tab 不冻结、`run` 不发生命周期命令、冻结失败只记 WARNING 且焦点照移、tab 列表列出冻结的 tab 而不唤醒它、借用的浏览器 teardown 时设回运行、拥有的浏览器不发命令、四个录制归档器的一次收集读了冻结的 tab 却没唤醒它也没截它、指纹记下 `freeze`、YAML 默认 `run` 并原样记进 `config.json`、集合外的值被拒）；`test_browser_background_tabs_live.py`（真实 Chrome，本机的永不收敛同步页：`run` 下互相改写不停、只节流也不停而离开的 tab 的计时器每秒只醒约一次、冻结后焦点 tab 的写为 0 且应答、`switch_tab` 回去后它恢复运行而另一个冻结、带 `browser.storage` 与 `browser.frames` 的一次收集不唤醒冻结的 tab）；`test_browser_replay_consistency.py`（`background_tabs` 不同、源指纹没有它，都拒绝）；`test_browser_backend_capabilities.py::TestBackgroundThrottling`（三项的去留、`ignore_default_args`、启动开关摘要随之不同）。

#### 任务通过页面交互完成，不构造 URL 捷径

用户 2026-10-14 补充本轮浏览器任务约束：不能通过拼接搜索页、详情页或接口 URL 完成任务。给定的起始页仍由 setup 打开；策略通过页面已有控件完成搜索、筛选与导航。模型提示明确这条协议，执行前的 browser 专用 `ui_navigation_guard` hook 通过既有 AOP `before act` 与 `RunAbort` 拒绝 URL 捷径，记录为协议否决，不伪装成任务执行错误或任务答错。

该 hook 是实验 YAML 显式选择的策略，不全局改变其他任务：拒绝策略的直接 `browser.navigate` / `browser.create_tab`；拒绝地址栏、新空白标签页和新窗口的输入快捷键；拒绝输入动作把一个完整 URL 或裸主机路径作为输入值。点击页面链接、浏览器已有页面的后退/前进、页面内普通输入、选择、滚动与拖动仍允许。可见链接自己开新页仍按既有 `new_tabs` 规则跟随。守卫识别的是动作的名字与参数，不另读 DOM，也不驱动动作；不声称能理解任意文本中的 URL 拼接意图，无法由动作判定的协议遵守情况仍需检查原回复与轨迹。已运行的旧轨迹只补做审计，不改写原始任务和证据。

#### 回放：网络来源与 miss 策略（M30）

两根 browser 内部的策略轴，都在 `core/capabilities/browser/replay/`，与 `replayability` 同一种受体形状（策略即开关，单实例受体在生成的 schema 里必填）：

- **`NetworkSourceRegistrar`**（`network_source`，判别字段 `source_id`；`network_source.py`：`NetworkSourceProtocol` / 注册表 / `NetworkSourceBase` / `default_network_source()`）。`BrowserCapability.__wiring__["network_source"] = ("network_source", ["archive", "live"])`，类级注解加 Python 默认（`live`）。**每一个手写的 capability 块**写 `network_source: {source_id: live}`。
  - `live`（`live.py`，默认）：什么都不拦截，没有旋钮；`intercepts` 为 `False`。
  - `archive`（`archive_source.py`，`ArchiveNetworkSource`）：从一个已录制 run 的归档回放网络。**它从不把请求放到线上**：这是 `archive` 来源自己的规则，不是框架的定律（用户 2026-10-13 的裁决）。要"miss 时转去真实网络"，那是一个新的网络来源（`NetworkSourceRegistrar` 的新键），不是 `archive` 的旋钮，也不是 miss 策略：miss 策略的决定只有 fulfill 与 fail 两种应答。**不是 dataclass**、手写 `__init__`：它带一个受体 `miss_policy`（`__wiring__ = {"miss_policy": ("miss_policy", ["strict"])}`，类级注解 + `default_miss_policy()`）。旋钮：`matcher: MatcherConfig`（嵌套的 pydantic 子配置：`levels: tuple[Literal["exact", "normalized", "fuzzy"], ...] = ("exact", "normalized")`（至少一级、不重复）、`fuzzy_threshold: float = 0.5`（[0, 1]）、`site_rules: str | None = None`，即规则目录；使用 pydantic dataclass，生成代码导入原类型并保留校验；conscribe 2.0 同样支持用户的 `BaseModel` 类型，不再需要绕开它）、`isolation: Literal["required", "none_acknowledged"] = "required"`、`shift_dates: Literal["none", "all"] = "none"`、`max_fulfill_body_bytes`（默认 64 MiB，校验上限 72 MiB：Chrome 读入的一条 CDP 消息 ≤ 100 MiB，base64 之后约 75 MiB 的 body）、`fulfill_timeout_s`（默认 10 s）、`log_mode: full | summary`（默认 `full`，逐事件），`log_repeat_limit`（默认 3，仅 `summary` 模式使用）。
- **`MissPolicyRegistrar`**（`miss_policy`，判别字段 `miss_policy_id`；`miss_policy.py`）：v1 只有 `strict`（`strict.py`），这根轴存在是因为用户要求留口子。`decide(miss) -> MissDecision`：请求类别（`document` / `benign` / `subresource`）、怎么应答、是否标 `diverged`。
- 生成配置是三层嵌套的判别联合：capability → `network_source` → `miss_policy`（`archive` 块里必须写 `miss_policy: {miss_policy_id: strict}`）。`network_source` 与 `miss_policy` 都进 `collect_registrars()`。

```yaml
network_source:
  source_id: archive
  isolation: required            # 默认；none_acknowledged = 明说不隔离
  shift_dates: none              # none | all；独立于 base_time，四组合均合法
  matcher: {levels: [exact, normalized], fuzzy_threshold: 0.5, site_rules: configs/replay_rules}
  miss_policy: {miss_policy_id: strict}
```

**隔离由来源推出，provider 上没有并列的布尔**（不做 `local_chrome.network_isolation` 这样的开关：`source: live` + 隔离开着会让每个请求莫名失败，`source: archive` + 隔离关着只靠单向检查才能发现）。`isolation: required` 时 capability 把 `chrome_switches.NETWORK_ISOLATION_ARGS`（`--host-resolver-rules=MAP * ~NOTFOUND` 与 `--no-proxy-server`）接在档位的开关之后经 `LAUNCH_ARGS` 交给声明了 `NETWORK_ISOLATION` 的 provider（§4.9.x；v1 只有 `local_chrome`），provider 的 `isolation_conflicts()` 报出会让隔离失效的旋钮；不支持的 provider 在 `prepare()` 报错（`check_setup`）、点名 provider 与来源，除非写了 `isolation: none_acknowledged`；那时不传隔离开关，只靠 Fetch 拦截（会有 Fetch 看不见的外联，下面"回放实测"）。`--no-proxy-server` 是必需的：macOS 上 Chrome 用系统代理、不读 `http_proxy`，有代理时 resolver 规则只作用于代理主机，一个 localhost 代理就能绕过黑洞。

**黑洞没有例外，`localhost` 也断**（用户 2026-10-13 的裁决）。原先的规则多一条 `EXCLUDE localhost`，放过精确的 `localhost`：绕过 Fetch 的流量（preconnect、WebSocket、嵌套 worker 的脚本）若指向它，回放时仍能连到本机还开着的服务。Chrome 155 实测，只写 `MAP * ~NOTFOUND` 时 `http://localhost:P`、`http://LocalHost:P`、`http://127.0.0.1:P` 的导航都是 `ERR_NAME_NOT_RESOLVED`、服务器一个连接也没收到，WebSocket 也一样；CDP 连接照常工作，因为 resolver 规则只管页面发出去的连接，DevTools 服务器是接进来的（provider 经 `127.0.0.1` 上的调试端口连 Chrome）。经 Fetch 的请求本来就在 DNS 之前于 Request 阶段暂停、从归档送出，所以对着本机网站（例如 docker 起的静态站）录下的 run 照样能回放，回放时那个服务开着关着都一样：`test_browser_replay_live.py` 的 localhost 页对着 `http://localhost:P` 录制，隔离回放时服务器还开着，页面与脚本全部从归档命中、一个 TCP 连接也没有，preconnect 与 WebSocket 都连不上；对照组（`isolation: none_acknowledged`）里这两条连接到了服务器。哪一天真要放行某个本机名字（例如推迟的 loopback blob 服务器），由需要它的功能带上自己的窄规则，不给全局留口子。

#### 回放的数据通路：`ArchiveReader`

capability 不知道 WAL：`replay_archive`（任务上下文的键，§4.10）是**源 run 目录**，由 `core/archive/source.py` 的 `open_archive(run_dir) -> ArchiveReader` 打开。`ArchiveReader` 是 `core/archive` 里的 Protocol，`RunTrace` 结构化地满足它（不 import 它）：

```python
class ArchiveEntry(Protocol):          # RunTrace 的 ArchiveRecord 满足它；每个成员都是只读 property
    path: str; archiver_id: str; name: str; media_type: str; scope: str          #（写成 property，
    sha256: str; content_addressed: bool; captured_at: float                      # dict / Literal 字段
    ids: Mapping[str, str]; run_version: int | None                               # 才能协变地匹配）

@runtime_checkable
class ArchiveReader(Protocol):
    @property
    def run_id(self) -> str: ...
    def iter_archive(self, archiver_id: str, *, include_trailing: bool = False
                     ) -> Iterator[tuple[int | None, ArchiveEntry]]: ...
    def archive_files(self, step: int, archiver_id: str) -> Sequence[ArchiveEntry]: ...
    def archive_run_file(self, capability_id: str) -> ArchiveEntry | None: ...
    def read_extra(self, path: str) -> bytes: ...
    def blob(self, sha256: str) -> bytes: ...
    def archive_lines(self, path: str, model: type[M]) -> list[M]: ...   # 逐行 JSON → model（§5.5）
```

所有读取都经 M27/M28 的已提交访问器：manifest 没列的 `env/` 文件（崩溃窗口里的、手工放进去的）对回放不可见、永远不会被送出；回放只读源目录，回放前后源目录的树哈希不变。`open_archive` 是 `core/archive` 里唯一 import `core.trace` 的模块（§10 的例外，包 `__init__` 不 import 它）；browser 的 `replay/` 只从框架 import `core.archive`。路径不是目录、或目录里没有 manifest（从没提交过）时 `open_archive` 抛 `ArchiveOpenError`，点名路径；manifest 在却读不了，是 trace 层自己的 `TraceSchemaError`，点名文件。

**加载（`archive_index.py`，setup 时、启动浏览器之前）**：`ArchiveNetworkSource.open_replay(reader) -> RecordedArchive`：源 run 的环境指纹（`archive_run_file("browser")`，连同它的 sha256）、`browser.network` 的每一份 `index.jsonl`（步与 trailing，`archive_lines`）与它们的 WARC、站点规则。索引只有原始请求事实（M29），**匹配键在加载时用唯一的规范化实现重算**，规则改了也没有过期的键。每条可服务的记录带 `run_id`（源 run），按录制顺序编号（请求开始的墙钟时间 `started_at`，同时间按索引行的顺序；只留下 `pending` 行的排在最后：`pending` 行如今带开始时间，但回放的排序不变，等"暂不回答"的设计定下来再定，用户 2026-10-20 的裁决）。同一次提交的 `index.jsonl` 与 `exchanges.warc.gz` 按 `iter_archive` 的迭代结构配对（同一次提交的文件连续给出、名字不重复），不看路径：

- `ok` / `redirect`：response（或 revisit）记录在同一步的 `exchanges.warc.gz` 里（索引给 offset / length）；revisit 的 payload 按 `revisit_of`（第一条 response 的 `WARC-Record-ID`，位于同一收集批次的 WARC 里）取，HTTP 头仍用 revisit 自己的。WARC 里存的是解码后的 body，响应头已去掉 CE / TE、`Content-Length` 是解码长度。
- `streamed_later`：只有 request 记录，状态与响应头在索引行里；body 是之后某次收集的 `kind: body` 行指向的内容寻址 blob（`blob(payload_sha256)`）。没有 `ok` 的 body 行就按 `body_unavailable` 处理。
- `too_large`、`incomplete`、最后一行是 `pending` 的：有记录、没有 body（原因 `too_large` / `body_unavailable`），命中它们怎么应答见下面"匹配"。`websocket` 的握手从不当作候选（WebSocket 不经 Fetch 暂停）。
- **源结局**：每条 `body_unavailable` 的记录带 `source_outcome`，说明源里这个请求怎么结束，只由索引行已有的字段推出（判据按下面的顺序，第一条成立的为准）：
  - `in_flight_at_run_end`：这条记录的最后一行是 `pending`（run 结束时还在途，含 body 还在流式接收的）。
  - `page_cancelled_before_response`：失败原因（流式交换读 `body` 行的 `error_text`，其余读 `exchange` 行的）是 `net::ERR_ABORTED`，且没有状态码：页面在服务器回答之前就取消了。
  - `page_cancelled_during_body`：`net::ERR_ABORTED`，有状态码：响应头已到，页面在 body 传完之前取消。
  - `network_failed`：别的 Chrome 失败原因（例如跨站读取保护拦下的 `net::ERR_BLOCKED_BY_ORB`）：源页面看到的是真实的网络失败。recorder 自己写的 target detach 不算网络失败。
  - `memory_cache`：没有失败原因，`from_cache` 为真且没停到 Response 阶段（`paused_session_id` 为空）：浏览器从内存缓存直接复用，源页面拿到了 body，recorder 没机会读。
  - `recorder_timeout`：没有失败原因，`body_note` 是 recorder 的读没答的那句（扣着响应的 `body_read_timeout_s` 里没答）：源页面很可能拿到了 body（超时后 recorder 放行，Chrome 在下载完成时交给页面）。
  - `unknown`：以上都不成立（target detach、recorder 读 body 的命令报错、流式 chunk 解不开、没有任何结局事实）。本地 11 个录制的 209 条没有 body 的记录里没有出现过。它不是网络失败，所以与其余结局一样在代答范围内（用户 2026-10-20 的裁决，下面"匹配"）。

  `too_large` 的记录不带源结局：它不是"没有正文"，是正文太大、从不存。
- 206（`content_range`）：同一 URL 的全部 206 记录与 200 记录拼成字节区间图；304 只给带条件头的请求当候选。
- 索引行解析不了、指到的 WARC 记录对不上（类型或 `WARC-Record-ID`）→ `ReplayArchiveError`，点名源 run、文件与记录 id；setup 因此失败，浏览器没有启动。

#### 匹配（`canonical.py`、`matcher.py`、`site_rules.py`）

**规范化（`canonical.py`，唯一实现，加载与线上请求共用）**：

- **先脱敏**：线上请求的 body 先过 M29 的 `redact_post_body(body, content_type)`（`archive/redaction.py`，录制时存进 WARC 的就是它的输出），再算任何键。归档里只有 `<redacted>`，所以带密码或 token 的 POST 照样精确命中。
- **URL**：scheme 与 host 小写，去掉默认端口与 fragment，适用规则 `parse_query` 声明的参数换成它的子字段（下面"字段里的字段"），query 参数排序。格式规范化不猜内容语义：时间戳外形、`ts` / `bust` 子串、`width` / `height`、`r` / `timestamp`、缓存参数与回调名都不在全局删除或抹值；需要忽略的字段必须由适用请求的显式规则声明。两个产品编号或尺寸不能仅因外形相似变成同一资源。
- **body**（移植 cdxj-indexer `postquery.py`，Apache-2.0，保留 notice；不依赖 cdxj-indexer / surt）：加 `__wb_method=<METHOD>`；urlencoded 与 multipart 变成参数（multipart 不看 boundary，文件部分与不是 UTF-8 的部分取 sha256）；JSON（含 `*+json`）拍平，键是**完整路径**（`a.b.0.c`，上游只用叶子名会撞键；空对象 / 空数组记成 `{}` / `[]`），逐行 JSON 逐行拍平；`text/plain` 或没有类型的先试 JSON、再试 `k=v&…` 表单形状（与 `redact_post_body` 认类型的方式一致）；解析不了的 body 记作 `__wb_post_data=<sha256>`；body 参数与 query 参数合在一起排序——**排序是我们自己写的**（上游 `postquery.py` 不排序，排序来自 AGPL 的 `surt`）。规范化后的 body 串超过 4096 字符时换成它的 sha256（上游截断到 4096 字符，大 GraphQL 请求会撞键）；适用规则 `parse_body_paths` 声明的字段先换成它的子字段、`ignore_body_paths` 再去掉，然后按剩下的参数判断长度、计算 sha256，所以被忽略的字段与子字段既不会把 body 推过上限，也不进摘要。所有带 body 的方法都参与（上游只取 POST / PUT）。
- **GraphQL**（JSON body 里有看起来是 GraphQL 文档的 `query`（以 `{`、`query`、`mutation`、`subscription` 或 `fragment` 开头，所以 `{"query": "red shoes"}` 这样的搜索 API 不算），或有 `extensions.persistedQuery.sha256Hash`；GET 的 `query` / `variables` / `operationName` / `extensions` 参数同理）：键是 `operationName` + 空白归一后的 query 的 sha256（只有 persisted query 的 hash 时用它自己的 sha256）+ 拍平后去掉易变路径（站点规则 `ignore_body_paths`，在单个操作之内匹配 `variables.cursor` 这样的路径，所以同一条规则对单个请求和批量请求都有效）的 `variables`；批量请求（顶层数组）逐项算、按顺序编号（`0.operationName`…）连起来。

**三级匹配**（`matcher.py`）：

- **L0 精确**：method、完整 URL（不含 fragment）、body 字节（脱敏后）都相同；multipart 比较时去掉 boundary（参照 Playwright `harBackend.ts` 的精确匹配，Apache-2.0）。
- **L1 规范化**：上面的规范键相同。
- **允许范围与强度顺序**：`matcher.levels` 是本次运行允许集合，不是顺序旋钮；总按 `exact`、`normalized`、`fuzzy` 从强到弱尝试。规则可进一步收窄，不能扩张；规则请求了运行禁止的级别时配置检查具名报告规则与运行配置。普通请求与 Range 共用边界。
- **L2 模糊**：同 host、端口、path、method，按规范参数打分 `2M/T`，达到 `fuzzy_threshold` 才命中。默认不用于文档与非 GET；规则可显式允许这两类，但仍须运行允许 `fuzzy`。模糊匹配不是业务等价证明。
- **响应序列**：共享已用集合，选取和消耗在一个同步决策内完成。首次有候选的最强允许级别决定序列；该序列耗尽后按适用规则明确声明的 `exhaustion: repeat_last | miss` 处理，不悄悄改用较弱级别的另一个资源。`repeat_last` 重复该序列最后一条**可用**的响应（有 body、可以送出；按录制顺序，本 run 亲和优先）并记 `exhausted`；序列里一条可用的都没有时是 miss，原因是被重复的那条（录制顺序最后一条）自己的 `body_unavailable`（或 `too_large`），日志带它的记录、源结局与 `repeated: true`（下面"回放日志"），读者分得出第一次落到这条记录与之后的重复。`miss` 记 `sequence_exhausted`，不同于从未有候选。没有策略声明的配置拒绝；框架没有统一隐式值，用户可声明宽范围策略并用细规则覆盖。
- **没有正文的记录照样占位置**（用户 2026-10-20 的裁决）：游标按录制顺序走到一条 `body_unavailable` 的记录，就消耗它，保持与源请求次序对齐；不把它从序列里删掉、让后面的回答提前，也不因为整个序列都没有正文就落到较弱的级别去送别的资源。这一位置怎么应答见下面一条。
- **run 亲和**：合并过的归档（合并工具在 M33 之后）里候选来自不同 run 时，优先本 episode 第一次命中的那个 run，再按录制顺序回退到别的 run（"Right HTML, Wrong JSON"）。v1 的归档只有一个 run，亲和由两 run 的合成索引单测钉住。
- **请求头只打破平局**：键内由录制顺序决定；只有其余都相同的候选（得分相同的不同 L2 URL、合并归档里时间相同的记录）才按匹配的请求头数量排，比的是 Fetch 看得见的那组头（S2a：`Accept`、`Referer`、`User-Agent`、页面自设的头在 Request / Response 两个阶段一样），去掉被剥离的头与站点规则的 `ignore_headers`；再平局时录制早的优先，然后 URL 字典序小的优先（WPR 的做法）。
- **miss 时给出最近的 3 个候选**：同 host 的记录按（同 method、同 path、L2 得分）排，取前 3 个（`record_id`、method、URL、得分、是否用过）；host 从没出现过时为空。
- 带 `Range` 的请求依允许级别匹配资源后从相应区间图取，不消耗普通响应游标。`exact` 只接受同完整 URL 的区间；允许 `normalized` 时才可拼规范键相同的资源。未录到区间记 `range_uncovered`，只有被运行级别禁止的候选时记 `match_level_disallowed`，都不伪装 `no_match`。连续区间、后缀区间和多段 Range 的既有覆盖合同保留。
- 匹配到、却送不出 body 的记录（`too_large`、`body_unavailable`）也照样消耗游标，结果带上记录与原因。`too_large` 由 miss 策略应答。`body_unavailable` 按适用规则显式声明的 `unavailable: miss | serve_identical` 应答：
  - `miss`：交给 miss 策略，日志带源结局。
  - `serve_identical`：下面四条都成立时，用同一序列里的一条可用记录**代答**；否则同 `miss`。
    1. 源结局不是 `network_failed`。源页面看到的是真实网络失败的，回放也必须失败：这一条写死在代码里，不是规则或运行的开关。
    2. 同一序列（同一级别、同一规则选出的那组候选，含已经用过的和录制得更晚的）里至少有一条可用记录。
    3. 序列里**所有**可用记录状态相同、正文相同（索引记下的 `payload_sha256` 相同）。没有正文摘要的可用记录（重定向）证明不了正文相同，不代答。正文在序列里变过的（状态接口）一律不代答。
    4. 被消耗的那条若录下了状态码（响应头到了、正文没存下：`page_cancelled_during_body`、`recorder_timeout`、`memory_cache` 等），它必须等于可用记录的那个状态：源服务器对这次请求答过的状态（例如 500 或 404）不能换成别的回答（用户 2026-10-20 的裁决）。没有状态码的（响应之前就被取消、到结束还在途）不受这一条限制。
  - 页面自己取消的（`page_cancelled_*`、`in_flight_at_run_end`）与 recorder 没存下的（`memory_cache`、`recorder_timeout`）都在代答范围内，`unknown` 也在（用户 2026-10-20 的裁决：只有 `network_failed` 不代答），日志与度量按源结局分开计数。对页面取消的请求代答是有意的、记在日志里的偏离：源页面没收到这次成功，回放页面收到了（下面"回放的残余"）。
  - 代答记录可以是录制得更晚的（先取消、后来又加载成功的缩略图）。在可用记录里选：本 run 亲和优先，然后与这次请求共享的请求头最多的（同一 URL 的跨站请求（带 `Origin`）与普通请求，录下的响应头可能不同，例如跨站读取要的 `Access-Control-Allow-Origin`），然后录制顺序离被消耗的那条最近的，同样近取录制早的。送出它录下的状态、响应头与正文，应答的形状与普通命中相同（下面"服务"）。
  - 被消耗的那条照样记为已用；代答记录**不**记为已用，它在自己的位置上照常送出，序列不前移。
  - 代答记录在服务时读不出或超过 `max_fulfill_body_bytes`，与普通命中一样记 `archive_unreadable` / `too_large` miss，日志带被消耗的记录与源结局。
- 源中声明有 body、但服务时读取失败或内容校验失败，另记 `archive_unreadable`；它是环境无法提供源响应的事实，按离线 miss 应答并保留记录身份，不经本次记录器的故障通道处理。源索引或 WARC 在加载时损坏仍具名拒绝启动。

**请求规则**（`site_rules.py`；`matcher.site_rules` 目录）：规则是冻结、校验的实验输入。作用域可声明 host、URL/path、HTTP method 与网络端口；明确整数优先级决定多条匹配规则中的唯一赢家，同优先级相交不得靠文件名或目录遍历顺序决定，冲突具名拒绝。候选的录制请求与实时请求必须选择同一规则身份，不能把不同规则删除参数后偶然相同的规范键拼成同一响应序列；Range 与 fuzzy 同样遵守。细规则完整声明其内容处理、耗尽策略（`exhaustion`）与无正文策略（`unavailable: miss | serve_identical`，上面"匹配"；与 `exhaustion` 一样必填、框架不给默认值，没写的规则在解析时具名拒绝），不隐式叠加另一个范围的内容假设；宽规则可显式覆盖其余请求。规则保留 `ignore_query`、`ignore_body_paths`、`ignore_headers`、`synthesize_204` 和 `response_echo` 的既有能力，并能保留明确字段、限制匹配级别以及允许文档/非 GET 的 fuzzy。响应 echo 仍只替换已有 JSON 位置，正文变化移除其摘要头。同一个规则集还有一节页面地址规则（`page_urls`，`page_urls.py`）与一节媒体区域规则（`media_areas`，`media_areas.py`）：与请求规则一起读入、冻结、计入摘要与快照，但匹配从不读它们，只有恢复校验读（下面"恢复：重定位与校验"）。

**字段里的字段**（`parse_query`、`parse_body_paths`，2026-10-23）：有的请求把几个字段放进一个字段的值里。腾讯视频播放器的 `proxyhttp` 把视频号、清晰度、加密 key、客户端时间与 flow id 都写进 JSON body 里的一个 URL 编码串 `vinfoparam`；爱奇艺 `dash` 的 query 字段 `bop` 是一个 JSON 文档。整个字段忽略会连视频号一起丢掉，所以规则可以声明一个 query 参数（`parse_query`）或一个拍平后的 body 路径（`parse_body_paths`，GraphQL 的 `variables` 路径同样在单个操作之内）的值是 `urlencoded`（`a=1&b=2`，按 query 串解码）或 `json`（对象或数组）。每条声明是 `{field, format}`，`field` 是确切的名字，不是 glob。规范化时这个字段换成它的子字段，名字是 `<字段>.<子字段>`（`vinfoparam.cKey`；JSON 按完整路径，就像它写在原处：`bop.dfp`），然后与其他参数一起排序；`ignore_*` / `keep_*` 用这些名字指子字段，其余子字段留在键里。
- 两边一致：解析在 `canonical.py` 里，录制请求加载时与线上请求暂停时用同一个函数、同一条选中的规则；body 的子字段在 4096 字符上限与摘要之前展开和忽略（上面"规范化"）。exact 级照旧比原字节；normalized 级不再区分子字段的顺序与编码（`+` 与 `%20`）。
- 形状严格：`urlencoded` 要求每个非空的 `&` 段都是名字非空的 `name=value`，`json` 要求对象或数组。不是声明形状的值（空串、单个 token、JSON 字符串或数字）保持整个字段、原来的名字，不会和解析出来的键混在一起。只解析一层：已解析字段的子字段不再解析。
- 加载时具名拒绝（文件与规则 id）：未知格式或多余的键、空名字、同一字段声明两次、一个声明落在另一个已解析字段之内、被同一规则整个忽略的字段（解析不起作用）。
- 声明是规则正文的一部分，进规则快照与 `site_rules_sha256`。近邻 miss 报告两边都按回放记下的规则规范化，所以声明过的字段按子字段名列出不同。
- 不解析 protobuf：没有 schema 时，长度分隔的字段是字符串、字节还是嵌套消息分不清，而且 protobuf 通常是整个 body，不是 body 里的一个字段（腾讯视频 `GetTabRemoteConfig` 的 protobuf 变体仍按整个 body 匹配，见 `configs/replay_rules/qq.com.yaml`）。

参数忽略须有作用域证据：统计 endpoint 的随机字段不授权同域业务接口忽略同名字段。发布规则按已有证据收窄；证据不足的全域规则删除，不用命中率替代误匹配反例验收。

**规则冻结与快照**：准备回放时每份规则正文只读一次，摘要从同一份字节计算；解析后的冻结集合服务整个 run。来源目录后续修改或删除不影响该 run。规则快照包含文件身份、原始正文、逐文件摘要、集合摘要与来源，连同实际 matcher 配置写入 `network_source` 指纹，随现有 `ArchiveFile` / WAL run 记录版本提交；browser 不创建第二套磁盘目录。历史 run 经 `ArchiveReader.archive_run_file` / `read_extra` 独立读回快照、核对摘要并重建规则，不访问原目录。保存失败按记录故障处理，不改变浏览器或模型的执行事实。

#### 服务（Request 阶段，在 recorder 连接上）

- **回放服务**（`server.py`，`ReplayServer`，由 `ArchiveNetworkSource.serve(recorded, ctx)` 造出，纯构造）是一个 recorder 贡献者：在每个 page / iframe / shared worker / service worker target 上贡献 `{urlPattern: "*", requestStage: "Request"}`（合进那一次 `Fetch.enable`），在每个准备的 target 上 `Network.enable` + `Network.setCacheDisabled(true)`，frame 上再加 `Network.setBypassServiceWorker(true)` 与拒绝 `ServiceWorkerContainer.prototype.register` 的注入脚本（`replay/shims.py` 的 `SERVICE_WORKER_BLOCK`，`Page.enable` 之后、`runImmediately`；它只替换 `register`，注入几次结果都一样，pinned 的 shim 也拒绝，两份叠加无害），这些全部在 `runIfWaitingForDebugger` 之前（模式 B）。它是 Request 阶段暂停的主人，`serves_requests` 为真；recorder 在 `open()` 时看一次参与者里有没有服务（与 Fetch pattern 一样，打开时定下），之后按它决定兜底是 fail 还是 continue。`ctx` 是 `server.py` 的 `ReplayContext`（recorder 句柄、观测 cycle、时钟），capability 在建 recorder 连接时组装。
- 每个 `Fetch.requestPaused` 在 recorder 的回调里派生一个受跟踪的 task（从不进 scope 队列，M29 的规则），回放服务在 `fulfill_timeout_s` 之内在**暂停它的那个 session** 上应答（dedicated worker 的请求暂停在父页面 session 上；OOPIF 的文档暂停在父页面 session、子资源在 iframe session），选择与消耗游标在同一段同步代码里完成；没应答的由 recorder 在 `finally` 里 `Fetch.failRequest`（上面"事件"）。**`archive` 来源的任何决定都不会把请求 continue 到线上**：这是这个来源自己的规则（上面"回放：网络来源与 miss 策略"），由测试钉住。
- 状态都按 recorder 的 session 记：target detach 时 recorder 的簿记连同它的 session 一起清掉，回放服务丢掉那个 session 的缓存；控制连接上的 `close_tab` 与 `_rollback_tab_attach` 因此不需要额外的回放清理。teardown 先关 recorder（还扣着的请求一律 fail），再关服务：停止应答、丢掉状态；最后一次收集之后才做的决定不再写出（收尾那次收集在 teardown 之前）。游标是整个 run 的，不按 tab。
- 服务自己的代码出错（匹配、应答、策略抛异常）：这个请求由 recorder 兜底 fail，`ReplayLog.fail` 记下原因，下一次收集（`drain()`）抛错：日志不完整时不假装完整，与归档会话 `_errors` 的规则相同。发送失败（Chrome 拒绝那条 fulfill，或在 `fulfill_timeout_s` 里没答）不算服务的错，recorder 兜底 fail；但归档的应答没到页面，所以它不是命中：本来要送归档响应的决定（`hit` / `exhausted` / `substituted` / `synthetic`）改记成 miss，原因 `fulfil_failed`，`fulfil_error` 是 Chrome 的报错，被拒的那条记录照样点名（`record_id`，代答的另有 `served_record_id`）。它的类别与是否发散由 miss 策略对这个 miss 的判断给出（主 frame 文档 → `diverged`），应答是 recorder 的 fail（`answer: failed`，没有 `status`），不再发策略的应答。用尽之后的重复（`exhausted`）被拒时带 `repeated: true`。miss 策略本来就决定的应答被拒（404、204、文档的 fail）仍是原来的 miss，`answer: failed`。2026-10-22 修复之前这些都记成 `answer: failed` 的命中、不标发散，恢复校验、`document_misses` 与度量报告都看不见（2026-10-21 的数据里 66 个分叉是 chrome-error 页、回放日志里没有一个 miss）。
- **按记录的形态应答**（`responder.py`，纯函数，每种形态一个单测）：
  - 录下的 3xx 连同 `Location` 原样送出，Chrome 自己跟随。Playwright `harBackend` 在查找时就沿重定向链走到底、送出最终响应，我们不那样做（Fetch 拦截下送原始 3xx 更忠实）。
  - 不在归档里的 CORS 预检（`OPTIONS` 且带 `Access-Control-Request-Method`；不按 resourceType 判断，它是 `XHR`）→ 合成 204：`Access-Control-Allow-Origin` = 请求的 `Origin`（从不是 `*`）、`Access-Control-Allow-Credentials: true`、`Access-Control-Allow-Methods` / `-Headers` 回显请求的方法与头，加 `Vary: Origin`。**预检永远不走 404**：预检失败，真正的请求就不会发出。
  - `Range: bytes=N-` / `bytes=N-M` → 用区间图合成 206（`Content-Range: bytes N-E/total`、`Accept-Ranges: bytes`），长度受 `max_fulfill_body_bytes` 限制（Chrome 会接着要后面的区间）；**从不用 200 回答 Range 请求**（S2a：200 能播放但 seek 失效）；区间没被覆盖 → miss（`range_uncovered`）。
  - **原因短语**（`responder.reason_phrase`）：每个 fulfil 都带 `responsePhrase`。录下的短语是可打印 ASCII 时原样送出；没有（HTTP/2 没有原因短语，h2 的响应录下的都是空串）或带别的字符时，送这个状态码的标准短语（`http.HTTPStatus`），没有标准短语的码送 `UNKNOWN_STATUS_PHRASE`（"Unknown Status"）。实测（Chrome 155）：空短语不论什么码都被拒（`Invalid http status code or phrase`，200、404 也一样）；不带短语时，Chrome 有标准短语的码（418、425、429、451、103、226 等）可以，没有的（432、299、599、999）被拒；带了短语的 432 / 999 都能送，非 ASCII 的短语 Chrome 也收，但不保证状态行在各处读成同样的字符，所以不原样送。动因：携程的文档在 h2 上回 432、没有短语，修复之前回放不带短语，Chrome 拒绝 fulfil，页面是 chrome-error。
  - 去掉 `Content-Encoding` / `Transfer-Encoding`，按送出的 body 重算 `Content-Length`，去掉 HTTP/2 伪头（`:status`…）。这是给页面 JS 看的头部卫生，`fulfillRequest` 本身原样送字节、不看这几个头。多条 `Set-Cookie` 分成多个头条目送出。
  - 送出的 body 只有两处可能与录下的不同：站点规则的 `response_echo`，与 JSONP 应答开头的回调名（下面"JSONP 回调名"）。body 变了就去掉为录下的字节作保的摘要头（`Content-MD5`、`Digest`、`Content-Digest`、`Repr-Digest`，`responder.BODY_DIGEST_HEADERS`）：Chrome 一个都不校验，但页面 JS 读到的摘要不能对不上它拿到的字节。`ETag` 是校验器不是摘要，回放关了缓存（`Network.setCacheDisabled`），没有谁拿它重新验证，所以照送。超过 `max_fulfill_body_bytes` 的判断用的是实际送出的字节。
  - 日期：`shift_dates: none`（默认）原样送出录下的 `Date` / `Expires` / `Last-Modified` 与 cookie 过期时间；`all` 把四者按同一个 Δ = now − 记录时间（录下的 `Date`，没有就用 WARC-Date）一起平移。**从不只平移一部分**：Chrome 会按送出的 `Date` 重算 cookie 过期（`Expires + (now − Date)`），只移 cookie 会移两次。没有 `Date` 的响应补一个 `Date` = 记录时间 + Δ，让 Chrome 的换算有锚点。`base_time` 控制页内 JS 时钟，`shift_dates` 控制响应头，两者是独立实验变量：`from_archive/none`、`run_start/all`、`run_start/none`、`from_archive/all` 全部合法；只拒绝缺归档或源时钟等真实前提不足。偏移逐响应按服务时刻计算，不是完整时间轴重建；HTML/JSON 正文、定时器、`performance.now` 与浏览器内部时钟不随之整体平移。
  - body 超过 `max_fulfill_body_bytes` → 不 fulfill（发给 Chrome 的一条消息超过 100 MiB 会带走整条 recorder 连接），按 miss 处理（原因 `too_large`）。
- **miss（`strict`）**：类别按 method、头与 frame 判断，文档优先：
  - 文档请求（`resourceType: Document`；SW 拦住之后导航都以 `Document` 暂停在页面 session 上）：暂停它的是 `page` target 且 `frameId` 等于 target id 的是**主 frame**，→ `Fetch.failRequest{errorReason: "Failed"}` 并在日志里标 `diverged`；否则是**子 frame**，→ `failRequest` + miss 记录。用 `Failed` 是因为 404 或 `BlockedByClient` 会掐断 Chrome 的 http 回退（S2a）。
  - 良性请求（`resourceType` 是 `Ping`（beacon、`<a ping>`）或 `CSPViolationReport`、`Content-Type` 是 CSP / Reporting 报告（`application/csp-report`、`application/reports+json`、`application/expect-ct-report+json`）、host 落在分析类黑名单——内置一份（`strict.py` 的 `BENIGN_HOSTS`，按后缀、在标签边界上匹配）、`extra_benign_hosts` 追加——或命中站点规则的 `synthesize_204`）→ 204，同样带 `x-replay-miss: 1`（回放编出来、不是录下来的应答都带这个头，页面与读日志的人都分得出来）。
  - 其余子资源 → 404，带 `x-replay-miss: 1`。
  - 归档里有记录但送不出 body 的（`too_large`、`body_unavailable`、`range_uncovered`）按同一张表应答，原因照记；`body_unavailable` 的除外情形是规则声明了 `serve_identical` 且条件成立、已经代答（上面"匹配"），那时不经 miss 策略。没有可代答的记录时照旧 404，日志带源结局。
  - 归档的应答被 Chrome 拒绝（`fulfil_failed`，上面"服务"）：策略照这张表给出类别与是否发散（主 frame 文档发散），应答是 recorder 已经做的 fail，不再发策略的应答。
- **WebSocket 与绕过 Fetch 的请求**：握手从不经过 Fetch，v1 一律记为 miss：回放服务声明 `Network.webSocketCreated`，记 `reason: websocket`；隔离下握手本身失败。`Network.requestWillBeSent` 看到、却从没在 Fetch 上暂停过（按 `networkId` 对）、随后 `Network.loadingFailed` 的 http(s) 请求（嵌套的 dedicated worker 脚本就是这样）记 `reason: unservable`，`answer: none`；随后是 `Network.loadingFinished` 的也记，那是一个绕过 Fetch、真的从别处拿到了响应的请求（隔离下黑洞没有例外，不应出现；`none_acknowledged` 下是线上），日志里必须看得见。这两类不经 miss 策略（没有暂停可以应答），`request_class` 为空。事件经 recorder 的分发器准入（只留 `requestId`、URL、method、type、frameId，不带头与 body）进 archive scope；暂停先到还是 `requestWillBeSent` 先被处理都一样（服务按 `networkId` 对账）。v2 的 WebSocket 回放用页面内 shim（参照 Playwright `webSocketMock.ts`）。

#### JSONP 回调名（`jsonp.py`）

JSONP 接口按请求里的回调名（`?callback=NAME`）回一段调用 `NAME(数据)` 的脚本。名字是页面自己起的：jQuery 起成 `jQuery<随机数字>_<请求计数>`，别的脚本带一段随机数。pinned 固定了随机数种子，却不固定页面上几个脚本谁先取随机数、谁先发请求（一个请求失败、页面走进错误处理，后面的取数就全部错位），所以回放时页面要的名字常常与录制时不同。只有 endpoint 规则显式忽略回调参数时，不同名字的请求才可能规范命中；命中后若仍送调用录制时名字的脚本，页面定义的是另一个函数，数据悄悄丢掉（2026-10-14 的 12 站实测：东方财富与界面新闻的"热门搜索"、21 经济网的搜索结果）；名字带随机部分的（豆瓣广告的 `erebor_<32 位十六进制>`、乐视登录接口的 `vjs_<时间><两位随机数>`）连键都对不上。用户 2026-10-14 的裁决：回放送出这种应答时，把开头的函数名换成这次请求的回调名，每次改写都记进回放日志，度量里看得见。

**改写的条件**（`jsonp.rewrite_callback`，纯函数，由 `responder.serve_record` 在 `response_echo` 之后调用；全部满足才改，否则字节原样送出）：

0. **是页面会运行或读取的脚本**（`may_be_script`）：请求的 `resourceType` 是 `Script`、`XHR` 或 `Fetch`（脚本标签，或某个库取回文本再执行：jQuery 对同源 JSONP 就用 XHR），文档、图片、样式表、字体、媒体请求一律不改；录下的应答的媒体类型是 JavaScript 或 JSON 类型、`text/plain`、`text/html`、`application/octet-stream`，或者没有类型：12 个站点的 JSONP 应答用过的就是这些（`application/json` 与 `text/html` 都有）。一张宽 40 像素的 GIF 开头正好读作 `GIF89a(`，它不是调用。
1. **应答以一次调用开头**（`leading_call`），形状是下面之一，各处的 `NAME` 是同一个名字：`NAME(`；`typeof NAME === 'function' && NAME(`（`==` 与双引号也认，Express 的 `res.jsonp` 就写成这样）；`NAME && NAME(`；以上任一个包在 `try {` 里（`try{NAME(…)}catch(e){}`）。调用之前与各记号之间只允许空白、注释（`/* … */`、`// …`）、第一个记号之前的 `;`，以及最开头的 UTF-8 BOM。`NAME` 是 ASCII 字母、数字、`_`、`$` 组成、不以数字开头的 JavaScript 标识符，或几个这样的标识符用点连起来（Angular 的 `angular.callbacks._0`）。左括号之后的内容一概不读。扫描是手写的，线性的，不用会回溯的正则（连续的 `/**/` 会让回溯正则指数爆炸）。
2. **录下的请求里恰好一个查询参数的值是 `NAME`**（`callback_parameter`）：服务器回显的就是它。参数名不限（12 个站点里有 `callback`、`cb`、`jsonp`、`dcb`、`j`、`method`）。两个参数都是这个值、或这个参数出现两次，就说不准服务器读的是哪个，不改。
3. **这次请求恰好带一次这个参数，值是同样规则下的名字，且与 `NAME` 不同**。不是名字的值（`cb);alert(1)//`）从不写进脚本。

不改的情形因此包括：应答是 JSON、HTML，或开头不是这种调用；服务器写死的名字（乐视广告的 `LETV.Plugin.AD.resoJsonp(`、东方财富 `version.jsonp` 不管参数写什么都调用 `callback(`：没有参数的值是它，条件 2 不成立）；回调名在路径或请求体里。12 个录制的 4295 条记录里，开头是调用的应答 247 条，满足条件 0–2 的 212 条（`Script` 203、`XHR` 9）；其余 35 条没有哪个参数的值是被调用的名字：写死的回调名，以 `$(`、`if(` 开头的普通脚本，还有两张开头恰好像调用的图片。

**字节**：名字是 ASCII，改写只替换名字所在的那几段字节，其余原样拷贝，所以不需要知道字符集（GBK 的应答还是 GBK）。名字长短不同，`Content-Length` 按送出的字节重算（上面"按记录的形态应答"），为录下的字节作保的摘要头去掉。子资源完整性（SRI）：页面不可能为一个回调名每次都变的脚本事先写好 `integrity`；回调名固定的脚本名字两边相同，不改写。

**记录**：命中（`hit` / `exhausted`）的日志条目多一个 `callback_rewrite`：`parameter`（哪个参数）、`recorded`（录下的名字）、`requested`（这次请求要的名字），没改写时不写。度量按站点数改写了多少个应答（§4.16 "站点"的 `callback_rewrites`）。折叠时 `requested` 与 URL 一样算每次不同的部分，所以每次换新名字的 JSONP 轮询仍按同一个请求折叠，`summary` 带着最后一次的改写、`count` 计入其余的。

**站点规则因此可以忽略回调参数**：名字带随机部分的参数忽略之后，请求命中录下的应答，名字由改写对上；应答不用这个参数的，忽略它送出的本来就是同样的字节（上面"站点规则"）。

#### 回放日志（`replay.jsonl.gz`）

回放服务把每个决定写进自己的日志（`replay_log.py`：`ReplayLog`），`browser.replay_log` 的 `ReplayLogSession` 在每次收集时把它换出来、写成本步的 `env/steps/NNN/browser.replay_log/replay.jsonl.gz`：gzip 压缩的逐行 JSON（`archive/encoding.py::gzip_jsonl`，`mtime=0`，同样的条目得到同样的字节；`media_type` 是 `application/gzip`），与录制旁边 WARC 的 `exchanges.warc.gz` 一样压缩存。原因：日志一行接一行重复同样的 URL、规范键与候选，压缩后约为原来的十分之一（2026-10-23 实测：318 个回放与分叉 run 的 3583 个步文件共 185.5 MB，逐个文件 gzip 后 19.4 MB；最大的 6 个 run 从 11.0 MB 到 1.0 MB；最大的一个步文件 1 MB，压缩 13 ms）。读侧经 `RunTrace.archive_lines` 解压（名字以 `.gz` 结尾的逐行 JSON，§5.5），所以下面的读法不变。压缩之前写的、不压缩的 `replay.jsonl` 不再读：读侧遇到这个归档器下别的名字的文件就抛 `ValueError`（点名文件，让人重跑回放），不把它当成空日志，也不另设兼容路径。**收集取走它的那一步是批次归属，不是动作因果**（capability 不知道步号，与网络证据同一个模型；episode 就是这个 run）。每行是一个 `ReplayLogEntry`（pydantic，extra-forbid）：

- `kind`：`hit` / `exhausted`（重复了用尽序列的最后一条可用响应）/ `substituted`（被消耗的记录没有正文，按规则的 `serve_identical` 用同一序列一条正文相同的可用记录代答，上面"匹配"）/ `synthetic`（合成的预检）/ `miss` / `summary`（被限流折叠的那些）；`captured_at`、`cycle`；请求事实 `method`、`url`、`resource_type`、`frame_id`、`frame`（文档：`main` / `sub`）、`target_id`、`session_id`。
- 命中：`rule_id` 与 `exhaustion_policy`、`unavailable_policy`（规则的无正文策略）、`record_id` 与它的 `run_id`、`level`（`exact` / `normalized` / `fuzzy`）、`fuzzy_score`、`range`；送出的 JSONP 应答改写了回调名时还有 `callback_rewrite`（`parameter`、`recorded`、`requested`，上面"JSONP 回调名"）。
- 代答（`substituted`）：`record_id` 是被消耗的那条（没有正文的），`served_record_id` 是实际送出的那条，`source_outcome` 是被消耗那条的源结局（上面"加载"）；其余字段与命中相同，`status` 是送出的状态。
- miss：`request_class`（`document` / `benign` / `subresource`；`websocket` / `unservable` 不经策略，为空）、`reason`（`no_match` / `sequence_exhausted` / `match_level_disallowed` / `too_large` / `body_unavailable` / `archive_unreadable` / `range_uncovered` / `websocket` / `unservable` / `fulfil_failed`）、`answer`（`fulfilled` / `failed` / `none`）与 `status`、`match_key`（规范键：URL 与 body 合在一起，超过 512 字符的写成 `sha256:<hex>`）与 `body_key`（body 的规范参数，原样写下、不截短：规范化本身已经封顶，规范参数超过 4096 字符（`MAX_BODY_KEY_CHARS`）或 body 解析不了时只剩一个摘要参数，匹配器手里也只有这个；近邻 miss 的挖掘按它逐字段比较，用户 2026-10-23 的裁决。实测（冻结后台标签页那批录制上重跑的 6 个分叉，腾讯视频、同花顺、爱奇艺）：603 条决定的 body 键超过 512 字符，平均 1767 字符，每个 run 的回放日志多 ≈ 170 KB（日志 +16%，run 目录 +1%）；其中不同的键 397 个，每个不同的键内容寻址存一份只省 ≈ 21 KB / run，还要每个引用它的步各一条 extra 索引（≈ 460 B）、每个键一个小文件，所以内联）、`candidates`（最近的 3 个）、`diverged`（主 frame 文档 miss，含归档应答被拒的主 frame 文档）；`fulfil_failed` 的另带 `fulfil_error`（Chrome 对那条 fulfil 的报错，或超时）与被拒的记录；匹配到了、却送不出 body 的（`too_large`、`body_unavailable`）也带 `record_id` / `run_id`，`body_unavailable` 的另带 `source_outcome`（被消耗或被重复的那条的源结局）；`range_uncovered` 的另带 `requested_range`：页面这次请求的 `Range` 头原文（2026-10-20 裁决：否则一次缺口与它引起的几百次重试在日志里分不开，是否反复请求同一个起点也说不清）。`repeated: true`（用户 2026-10-20 的裁决）标出这个 miss 来自 `repeat_last` 在序列用尽之后的重复：被重复的那条没有可送出的 body（序列里没有可用记录），或者送出时读不出、超过上限；第一次消耗这条记录的 miss 不带它。重复送出成功的是 `exhausted`，本来就分得开。每个决定（命中也一样）都记 `answer` 与 `status`。
- **完整或摘要**：`log_mode: full` 保留每次事件；显式 `summary` 时同一次收集里相同含义的条目超过 `log_repeat_limit` 才折叠。摘要带 `folded_kind`、`count`、`first_captured_at`、`last_captured_at`，标明逐次时间不可恢复。不得跨 kind、原因、应答、状态、请求类别、frame、发散、记录身份（被消耗的与送出的都算）、源结局、是否重复（`repeated`）或适用规则合并；计数按代表次数而非文件行数。批次归属是收集时的步骤，不声称该步骤动作造成这些异步事件。


读侧：`RunTrace.archive_lines(path, model)`（§5.5，通用的逐行 JSON 访问器，解析或校验失败抛点名文件与行号的 `TraceSchemaError`）；browser 一侧的 `replay_log(reader, step) -> list[ReplayLogEntry]`、`iter_replay_log(reader)`（含 trailing）、`diverged_steps(reader)` 建在 `ArchiveReader` 上，按写它的归档器（`browser.replay_log`）找文件，别的归档器下同名的文件不是回放日志。**"这一步发散了"在读时推出**：这一步的回放日志里有主 frame 文档 miss；M30 不改 `StepRecord`（M31 的 `restoration` 另算）。回放日志导出成 HAR 是可选项，推迟到有按 HAR 打分的评测器时。

#### 一致性检查与回放指纹

`check_setup` 里（`prepare()` 时，setup 在 `backend.open` 之前再查一遍；§4.9 `SetupCheck`），先调用来源的公开 `check_config(reader)` 验证规则 schema、运行级别冲突及源已提交请求的规则覆盖与范围冲突（`live` 无需检查）；此时只检查，实际 run 的冻结输入仍在 `open_replay` 读取。`cooperation_problems`（配置能否协作）之后，打开源 run、只读它的环境指纹（`archive_index.py::load_fingerprint`：`env/run.browser.json`、录制的 T0、`browser.network` 提交了几个 `index.jsonl`；指纹读取本身不读索引行、不读 WARC；规则检查另经 `iter_index_lines` 读取已提交请求的 method / URL，不打开响应正文，返回的 `RecordedArchive` 索引为空、没有 store，什么也匹配不上、什么也送不出），再把 `replay/consistency.py` 的 `consistency_problems(...)` 并进同一个 `BrowserCooperationError`。整个归档（`load_archive`：索引与每条 WARC 记录的校验）由 setup 在检查通过之后加载，用于服务；索引行损坏在 prepare 的规则检查时拒绝；WARC 记录损坏（`ReplayArchiveError`）在 setup、启动 Chrome 之前失败。检查失败时既不打开 trace 也从不启动 Chrome，每条点名两边的值：

- 源 run 没有 `env/run.browser.json`，或没有录 `browser.network`；源 run 自己就是回放（指纹的 `network_source.source_id` 是 `archive`，它绑定的是 `browser.replay_log`、没有 WARC，应当回放原始录制）。
- 档位不同；档位参数不同（`base_time` 除外，`from_archive` 正是回放要改的那一个）；`shim_sha256` 不同（pinned）；viewport 不同；`background_tabs` 不同（离开焦点的 tab 录制时运行、回放时冻结，或反过来；源指纹没有这一项也拒绝：那是加这一项之前录的，要重录）。
- 启动开关摘要不同：本 run 的摘要由 provider 的 `planned_launch_flags(档位开关 + 隔离开关, 档位的启动环境)` 在启动之前算出（`TZ` 也在里面），两边都去掉网络路由开关（§4.9.x），所以隔离开关不进比较；隔离由上面的协作检查断言（`required` 而 provider 支持，或明写 `none_acknowledged`）。两边有一边没记摘要时跳过并记一条说明。
- 浏览器的默认时区与语言不同（源指纹的 `browser_defaults`：没有 override 够得着的 frame 读到的值，录制启动之后实测的；用户 2026-10-14 的裁决：严格，没有开关）。启动之前，provider 从本机的 OS 读出它将启动的浏览器会落回什么（`LaunchArgsProvider.planned_browser_defaults(launch_env)`，`backends/browser_defaults.py`），读得出的每一项都比较：时区按 ICU 的规则，启动设的 `TZ`，没有就是继承的 `TZ`，再没有就是 `/etc/localtime` 链到的时区；语言只在 macOS 上读得出，用 CoreFoundation 对同样的输入做 Chrome 做的那次选择（Chrome 包的本地化、Chrome 自己的偏好域看到的 `AppleLanguages`，`_` 换成 `-`、`en` 读作 `en-US`）。源指纹没有 `browser_defaults` 也拒绝。
- `base_time: from_archive` 的 T0 取自源指纹的 `replayability.t0_ms`，在 `plan()` 之前解析。
- pinned 的源指纹没有 `replayability.client_hints`（在 override 带上浏览器自己的 client hints 之前录的：那时页面读到的 brands 是空的）：这个回放的页面会读到与录制不同的 client hints，拒绝，要重录（与 `browser_defaults` 同一个做法）。

浏览器起来之后、recorder 准备第一个 target 之前，setup 读这个浏览器自己的 client hints、做出计划，pin 了标识（`user_agent` 非空）时把计划的 override 带的 client hints 与源指纹的 `replayability.client_hints` 逐项比较，不同就**拒绝**（`client_hint_problems`，点名两边的 brands、平台与移动端；用户 2026-10-14 的裁决：与标识一样进比较）。标识本身作为档位参数在启动之前就比过了；同一个标识背后浏览器给出什么，启动之后才知道（例如录制用 Google Chrome、回放用同版本的 Chromium，brands 不同）。没 pin 标识时（`user_agent: null`），client hints 与浏览器自己的 UA 一样跟着浏览器走，不同只警告（下面）。

同样在这一步，setup 实测这个浏览器的默认时区与语言（上面"setup 与 teardown"），与源指纹的逐项比较，不同就**拒绝**（`browser_default_problems`，同样点名两边的值）：OS 在启动之前说不出的（macOS 以外的语言）在这里定下；实测与启动前读到的不一致时另打一条 WARNING（这台机器上启动前的那次比较不可信）；实测失败也拒绝。为什么选"启动前读 OS、启动后再量"（2026-10-14 实测，本机 zh-CN / Asia/Shanghai，Chrome 155）：环境变量不可信（本机 `LANG=en_US.UTF-8`，Chrome 落回 `zh-CN`），`AppleLanguages`（`zh-Hans-CN`）也不是 Chrome 的值；CoreFoundation 对 Chrome 包的选择（`zh_CN` → `zh-CN`）与实测一致，`/etc/localtime`（`Asia/Shanghai`）与 `TZ`（`Asia/Tokyo`、`UTC`、`:Asia/Tokyo`）也与实测一致，所以拒绝在 `prepare()` 就能出、不打开 trace（用户要求的优先级）；但别的语言列表在本机没法验证（改用户的全局偏好不可接受），macOS 以外的平台语言读不出，所以实测是记进指纹的值，也是启动后的那道比较；`requires_chrome` 的金丝雀（`test_browser_new_tab_live.py::test_the_os_facts_are_what_chrome_falls_back_to`）钉住"启动前读到的就是 Chrome 落回的"。

同样在浏览器起来之后，**只警告、不报错**的四项，记进回放指纹的 `warnings` 并打 WARNING 日志：Chrome 版本（`Browser.getVersion` 的 product / revision）不同；档位没有固定 `user_agent` 时浏览器自己的 UA 不同、override 带的浏览器自己的 client hints 不同（两者都带着版本号）；源指纹的脱敏策略（`redaction.policy`）与当前 `BROWSER_REDACTION_POLICY` 不同（规则变了，带凭证字段的 POST 可能对不上键）。`none` 档位录下的 DPR 是测出来的宿主机事实，不比较（写明）；它的时区与语言就是浏览器的默认值，按上面一条比较。

**回放指纹**：回放 run 自己的 `env/run.browser.json` 多一个 `network_source` 块：`source_id: archive`、`source_run_id`、`source_fingerprint_sha256`（源指纹那份已提交记录的 sha256）、`site_rules_sha256`、`site_rules_snapshot`（冻结正文、逐文件摘要、来源）、`matcher`（levels、阈值、规则目录）、`miss_policy_id`、`shift_dates`、`isolation` 与 `warnings`。块的一半是配置实例自报的（`NetworkSourceProtocol.fingerprint()`：`live` 是 `{"source_id": "live"}`，`archive` 是它的旋钮），一半是这次 run 的事实（`ReplayServer.fingerprint()` 加上源 run 与两个摘要、`warnings`）；capability 有服务时交服务的块，否则交来源的块，`BrowserArchiveRun.collect(network_fingerprint=...)` 写进指纹。`warnings` 里还有一致性检查没能比较的说明（`consistency_notes`：一边没有启动开关摘要）。

一致性一侧的函数都在 `replay/consistency.py`：`consistency_problems`（`prepare()` 与启动前，拒绝）、`browser_default_problems` 与 `client_hint_problems`（启动后、准备任何 target 之前，拒绝）、`consistency_notes`（启动前，说明）、`version_warnings`（启动后，警告）、`replay_t0`（`from_archive` 时 T0 取源指纹的，否则取本 run 的开始）。capability 只组装与转交；本 run 交给 provider 的开关（档位的，加上隔离的）、启动环境（档位的）与 provider 据此会记下的开关表、会落回的默认值，由 `cooperation.py` 的 `launch_args(cap)` / `launch_env(cap)` / `planned_launch_flags(cap, ...)` / `planned_browser_defaults(cap, ...)` 算出，同一份开关与环境既用于启动，也用于启动前的比较。

#### 回放实测（S2a，Chrome 153；探针在本地的 `tmp/flywheel/spikes/s2a/`）

| 事实 | 规则 |
|---|---|
| `MAP * ~NOTFOUND` 下，文档、css、img、script、XHR、fetch、beacon、iframe（OOPIF 的文档在父页面 session、子资源在 iframe session）、弹窗（第一个文档请求在弹窗自己的 session）都在 DNS 解析**之前**于 Request 阶段暂停并能 fulfill；https 不经 TLS 就拿到真实的 https origin 与 secure context，`Secure` cookie 写得进去，送出的 HSTS 被忽略 | 在 Request 阶段直接 fulfill；不用代理、没有 CA |
| 只有 Fetch 不是隔离：每个 https 导航约 6 次 TLS 握手、`<link rel=preconnect>` 2 次，WebSocket 从不暂停；加上隔离开关之后 0 个连接 | 隔离是必需的；零外联的验收数 fixture 服务器的 TCP accept，不数 HTTP 请求——WebSocket 服务器也数 accept、不数握手（M30 实测，Chrome 155：不带隔离的回放里，页面连 `127.0.0.1` 的 WebSocket 连上之后不发请求行就关掉） |
| macOS 上 Chrome 用系统代理、忽略 `http_proxy`；有代理时 resolver 规则只管代理主机，localhost 代理绕过黑洞 | 隔离开关含 `--no-proxy-server`；拒绝 `proxy_server` 与 `extra_args` 里的路由开关 |
| `MAP *` 也黑洞 IP 字面量、`*.localhost`、`localhost.`；写了 `EXCLUDE localhost` 时只有精确的 `localhost` 放行，不写时它也断（Chrome 155 复测）；IP 字面量与 `localhost` 的 URL 仍在 Request 阶段暂停、能从归档送出 | 隔离开关不留 `EXCLUDE`（上面"黑洞没有例外"）。fixture 页面用非 localhost 的主机名；回放去掉了 fixture 的名字映射，映射的名字有没有隔离都连不到 fixture，计数在那些页面上动不了；所以零外联的验收另有两页：`egress` 的页面本身、它的脚本、对另一台服务器的 preconnect 与 WebSocket 全走 IP 字面量，`localhost` 页同样的四条路全走精确的 `localhost`（录制的规则多一条 `EXCLUDE localhost, EXCLUDE 127.0.0.1`，录制时连得上）——回放里只有黑洞切得断这些路，回放若放行一个请求也会连到 fixture。对照组是同一回放写 `isolation: none_acknowledged`：accept 计数会动，HTTP 请求数不动 |
| Chrome 153 默认先把可注册域名的 http 导航升级成 https 试探；用 404 或 `BlockedByClient` 回答会掐断 http 回退 | pinned 录制与回放都关 `HttpsUpgrades`；文档 miss 一律 `failRequest(Failed)` |
| dedicated worker 的流量暂停在父页面 session；shared worker 的脚本在页面 session、它的 fetch 在自己的 session；SW 的流量在 SW session，SW 代理的导航以 `XHR` 出现；嵌套 dedicated worker 的脚本两个阶段都不经过 Fetch | 在暂停它的 session 上应答；回放时拦住 SW；嵌套 worker 脚本是已知限制，看得见的记 `unservable` |
| CORS 预检以 `OPTIONS`（`resourceType: XHR`）暂停；合成的 204 回显 Origin / 方法 / 头可用，404 让真正的请求永远不发 | 预检按方法与头识别，没归档就合成 204 |
| Chrome 读入的一条 CDP 消息 ≤ 100 MiB，超过就断掉整条 recorder socket（1006）；它发出的回复可到 256 MiB（上面"消息上限"），所以这条只约束回放 | fulfill 的 body 上限 `max_fulfill_body_bytes`（默认 64 MiB），超过按 `too_large` miss；录制端存下 64 MiB 以内的每个 body（`browser.network` 的上限，下面"体积分档"），两边一致；loopback blob 服务器（`continueRequest{url}`）要 Local Network Access 授权，还要隔离为它放行一个专用的本机名字（黑洞不再有例外），推迟 |
| 视频先要 `bytes=0-`；206 + `Content-Range`（含分块）可用，200 能播但 seek 失效 | Range 一律 206 |
| Chrome 按送出的 `Date` 重算 cookie 过期；没有 `Date` 时用真实时钟；JS 时钟固定在录制时刻时页面 JS 设的绝对过期 cookie 会被丢掉；没有 `Last-Modified` 时 `document.lastModified` 是真实时钟 | `shift_dates: none \| all`，从不只移一部分；缺 `Date` 时补一个 |
| `fulfillRequest` 原样送字节，不处理 `Content-Encoding`，也不按 `Content-Length` / `Transfer-Encoding` 分帧 | 归档存解码后的 body；去 CE / TE、重算 CL 只是给 JS 看的头部卫生 |

#### 恢复：重定位与校验（M31）

`BrowserCapability` 实现 §4.9 的 `RestorationSupport`，两个方法都只委托给 `core/capabilities/browser/restoration.py` 里的纯函数（不在 `capability.py` 里加模块级私有工具）。它们只读传进来的两个观测里本 capability 的 fragment（`browser.dom` 的自包含 `selector_map`、`browser.browser_meta`、`browser.screenshot`，M27 的 `DOMObs.from_trace` 把录下的那一份还原成 `DOMState` / `DOMElementNode`），外加回放服务自己的发散计数；不 import `core.trace`，不知道步号。

**重定位**（`retarget`）：

| 录下的动作 | 方式 |
|---|---|
| 带元素 index 的动作：`Params` 上带 `AddressesElement("browser.dom")` 的参数给了值的那些（`core/actions/params.py::element_params`，读本 capability 持有的动作实例的 `Params`，恢复不另列名单，也不查注册表）。今天是 `browser.click`、`browser.type`、`browser.select_dropdown`，以及带了 `index` 的 `browser.scroll` / `browser.extract_content` | 几何再签名（`method="geometry_signature"`），见下 |
| `browser.switch_tab`（`target_id` 是每个浏览器新生成的 CDP targetId） | 按 tab 顺序与 URL（`method="tab_order_url"`）：在录下观测的 `browser_meta.tabs`（按 tab 打开的先后排，上面"页面新开的标签页"）里找到这个 `target_id` 的位置，恢复出的 `tabs` 同一位置的 tab URL 相同就换成它的 id；`target_id` 为空（"最近打开的 tab"）原样 |
| 坐标动作、导航（`navigate` / `go_back` / `go_forward`）、`send_keys`、`wait`、`done`，以及不带 `index` 的 `scroll` / `extract_content` | 恒等（`method="identity"`）：pinned 回放下布局确定，截图哈希兜底 |

几何再签名：(1) 取录下观测 `selector_map[index]` 的条目：它的 `bounds`（页面坐标的 border box，已计入 iframe 偏移）、`tag_name`、`attributes` 与子树文本；(2) 在恢复出的 `selector_map` 里找 `bounds` 的四个量都在 `retarget_tolerance_px`（`BrowserCapability` 的旋钮，默认 2.0 CSS px）之内的候选；(3) 用签名确认：`tag_name`，`id` / `name` / `aria-label` / `role`（属性，或 AX role）各自相等，子树文本规范化（空白折叠）后的 sha256 相等；(4) 恰好一个候选时把这个参数（`params["index"]`）改写成它的 handle（可以在别的 OOPIF 命名空间里），其余参数不动；(5) 没有候选是 `failure="not_found"`，多于一个是 `failure="ambiguous"`，`detail` 写候选数。录下的条目缺 `bounds`、本 run 没有激活 `browser.dom`（或这一次的 `browser.dom` 采集失败），或者录下的 `selector_map` 里根本没有这个 index（源 run 的这个动作当时就找不到目标），都是 `not_found`（`detail` 说明原因）。子树文本只读到条目根以下 64 层（`TEXT_DEPTH_LIMIT`）：trace 的编码大约在第 100 层截断条目的子树，活的节点没有截断，两边读到同样的层数，同一个元素的签名才相同；实测的页面最深约 40 层。

**截图的页面出处（用户 2026-10-14 裁决）。** `ScreenshotObs.source_url` 保存截图观测同一次采集已有的文档 URL：复用截图之后用于 cycle 一致性检查的 `DocumentStamp`，不额外截图、不额外发一次 URL 查询。字段随截图 sidecar 写入、由 fragment 自己读回；模型输入仍只使用截图，不把这个字段渲染进视觉 agent 的 prompt。文档身份读取失败或旧证据没有 URL 时为 `None`，明确表示未测到，不猜地址。恢复测量优先使用截图出处，缺失时可读同 cycle 的 `browser_meta.url`；两边都有非空地址才比较，任一边缺失时 `url_match` 为 `None`，不是相等。报告必须单列 URL 证据覆盖率，不把缺失读成 URL 校验通过。

**校验**（`verify_restoration`）交回一份 `RestorationEvidence`：

- `url_match`：两边的截图出处 URL（或同 cycle 的 `browser_meta.url`）逐字相等，只有回放规则集的页面地址规则声明的查询参数不比（下面"页面写进自己地址的参数"）；任一边没有地址时为 `None`；`url_ignored_query` / `url_rule_ids` 记下比较时去掉的参数与去掉它们的规则，`recorded_url` / `restored_url` 照样是原样地址；
- `signature_match` / `signature_missing` / `signature_extra`：两边 `selector_map` 每个条目的签名（同上：tag、四个标识属性、文本哈希；不含 index 与 `bounds`）组成的多重集是否相等，以及录下有而恢复出没有、恢复出多出来的个数（没有 `browser.dom` 时为 `None`）；
- `screenshot_distance`：两张截图的 64 位 dHash（Pillow：转灰度、Lanczos 缩到 9×8、相邻像素比较）的汉明距离（任一边没有截图字节、或字节解不开时为 `None`，后者记 WARNING）；媒体区域规则声明的页面上是两张图都遮掉媒体框之后的距离（下面"页面上的媒体区域"）；
- `document_misses`：自上一次 `verify_restoration` 以来回放服务记下的主 frame 文档 miss 数（`ReplayLog` 的单调计数 `diverged_total`，带 `diverged` 的条目（折叠进 `summary` 的也算，归档应答被 Chrome 拒绝的主 frame 文档 `fulfil_failed` 也算）每条加一；回放日志照常被 `browser.replay_log` 逐步取走，不影响这个计数）。capability 记着上一次读到的总数，建回放服务时与 teardown 时归零。`network_source` 是 `live` 时为 `None`；
- `media`（2026-10-20 裁决）：两边截图的媒体状态（§4.4 "截图的媒体状态"）逐元素对应后的比较，加上这次观测之前做过的媒体对齐（下面"媒体对齐"）。任一边截图没有媒体状态，或一边两次读数都不知道时，`comparisons` 为 `None`；两边截图都没有时整个 `media` 为 `None`；
- `media_areas`（2026-10-23 裁决）：只在媒体区域规则声明的页面上有值，记下哪些规则、遮掉了哪些框、遮掉的占比、整图与遮掉之后的距离（下面"页面上的媒体区域"）。

**页面写进自己地址的参数**（2026-10-23，`replay/page_urls.py`）。有的页面在载入之后用 `history.replaceState` 或客户端路由把一个每次都变的参数写进自己的地址，没有任何请求带它：哔哩哔哩搜索页的 `vt`（值读来是页面时钟毫秒数的后八位），同花顺选股结果页的 `sign`（提问时的页面时钟毫秒数）。`pinned` 的 `clock: shared`（上面"`replayability`：回放档位"）让它们在每次回放里都不同，于是同一个页面也以 `url_mismatch` 不通过。这类站点知识与请求规则一样是按站点的实验假设，所以放在同一个回放规则集里（`matcher.site_rules` 目录，规则文件的 `page_urls` 一节；发布的在 `configs/replay_rules/page_urls.yaml`），随规则集一起冻结、计入 `site_rules_sha256` 与 `site_rules_snapshot`，分叉 run 自己的回放指纹就记着用的是哪一版。一条规则是 `{id, scope: {host, path}, ignore_query}`：`host` 必填（没有对所有站点生效的规则），与请求规则一样是 fnmatch、不隐含子域名；`path` 省略时是这个 host 的所有路径；`ignore_query` 至少一个参数名（fnmatch）。`id` 与请求规则共用一个身份空间。比较时，作用域同时匹配两边地址的每条规则都生效（页面地址规则只列要去掉的参数，没有优先级，也没有别的行为可合并）；两边查询串按 `&` 切开，解码后的参数名被规则列出的那一段去掉，不论值是什么、只在一边出现还是两边都有；其余部分逐字节比较：scheme、host 与端口、路径、剩下的各段（顺序与写法都算）和 fragment。没有规则生效、或没有去掉任何参数时就是原来的逐字比较；地址解析不了时也是。只列证据表明由页面写进自己地址、不选内容的参数；关键词、页码、排序、id 与筛选从不列。`live` 网络来源没有回放规则集，地址仍逐字比较。请求匹配从不读这一节：页面把 `vt` 写进地址之后再发出的文档请求，照样按请求规则匹配。查看器的恢复差异（`inspection/restoration.py::restoration_gap`）把"去掉参数之后才相等"的地址照样列为测得的差异，写出去掉的参数名。

**页面上的媒体区域**（2026-10-23 用户裁决，`replay/media_areas.py`）。视频站的播放状态在录制与离线回放之间很难做到一致：流的密钥加密、自动播放的预览在别的时刻开始、播放器离线加载不出来（腾讯视频的播放器显示网络错误）、两个叠放的 `<video>` 互换位置。于是这些站点几乎每个分叉都以 `media_mismatch` 和（或）集中在播放器上的 `screenshot_distance` 不通过，即使页面的其余部分相同（实测 `lookahead_15task_freeze_20261023`：腾讯视频 70 个检查点一个都没有恢复）。用户的裁决：录制照常（视频照样播放，数据真实），恢复校验在声明了的页面上不管视频区域。声明是按站点的实验假设，与页面地址规则一样放在回放规则集里（规则文件的 `media_areas` 一节；发布的在 `configs/replay_rules/media_areas.yaml`），随规则集一起冻结、计入 `site_rules_sha256` 与 `site_rules_snapshot`。一条规则是 `{id, scope: {host, path}}`：`host` 必填、fnmatch、不隐含子域名，`path` 省略时是这个 host 的所有路径（与页面地址规则同一个 `PageScope`）；`id` 与请求规则、页面地址规则共用一个身份空间。作用域同时匹配两边页面地址（截图出处 URL，没有时用同 cycle 的 `browser_meta.url`；任一边没有地址时不生效）的规则生效，这时：

1. 媒体状态照常测、照常记在 `media` 里，但恢复器不判它：比较、读不到的 frame 与对齐结果都不构成 `media_mismatch`（§5.3）。媒体对齐（`media: align`）照常做。
2. 两边截图媒体状态测到的框——每个 `<video>` / `<audio>` 元素的位置框，以及读数没有应答的 frame 的位置框（里面播什么不知道；腾讯视频的播放器就在影子树里的 iframe 里，测不到元素）——取并集，裁到视口之内，按各自截图的视口与图像尺寸换算成像素（覆盖框碰到的每个像素），在两张图上都涂成黑色，再算 dHash 距离。画布自己画的播放器在媒体事实里没有框，遮不到（媒体事实不认识画布）。一边截图没有媒体状态（旧证据）或两次读数都不知道时只遮另一边的框，`detail` 写明。
3. 遮掉的占比超过 `MAX_MASKED_SHARE`（0.75）时不遮：dHash 是 9×8 的灰度平均、64 位，遮掉四分之三之后只剩约 16 位读页面，即使其余部分完全不同也只翻转约一半，刚过默认阈值 5，更小的差别根本不会失败——遮掉之后的比较会凭构造通过。这时 `screenshot_distance` 用整图的距离（与没声明的页面一样），`mask_applied` 为 false，`detail` 说为什么，从不是一张近乎空白的图上的通过（实测：腾讯视频首页与电视剧频道页有一个覆盖视口 100% 与 87% 的读不到的 frame，频道页离线加载失败，遮掉之后会通过）。
4. 证据 `media_areas` 记下规则、遮掉的每个框（哪一边、媒体元素还是读不到的 frame、名字与位置）、遮掉的占比、整图与遮掉之后的距离和 `mask_applied`，trace 里看得见校验忽略了什么。

其余照常比较：URL、元素签名、文档 miss、动作成败，以及框之外的像素——页面没加载完、滚动位置不同、结果列表不同照样不通过。没声明的站点、`live` 网络来源（没有回放规则集）完全不变。只声明证据表明检查失败在媒体状态或集中在播放器区域的视频页与搜索页，从不声明非视频站。查看器的恢复差异照样列出整图距离与没判的媒体差异（注明是哪条规则）；度量链的原路径保真另计这类步数（§4.16 `media_area_steps`）。

**媒体元素的对应**（`media.py::match_media`）：录下的每个元素在恢复出的页面里找候选——同一 frame、视口位置框四个量都在 `retarget_tolerance_px` 之内、来源种类相同、两边都是 `url` 种类时地址逐字相等、总时长（都已知时）相差不超过 1 ms（同一个文件的总时长相同，作为辅助核对）；`blob:` 来源的地址与总时长都不比：地址每次加载都变，总时长是页面脚本设的、随数据到达而变（2026-10-21 实测，小红书的播放器在最后一段数据加入之前报 30.93 s、之后报 31.04 s）。恰好一个候选、且它在该 frame 里的文档顺序与录下的相同，是 `matched`；没有候选是 `missing`；多个候选，或唯一的候选文档顺序不同（页面插入或删掉了别的媒体元素，顺序与位置说法不一），是 `ambiguous`。恢复出的页面里没被任何录下元素对应上的元素是 `extra`。有歧义就不对齐。比较用两边都测到的那次读数，优先截图之前那次。

**恢复器的判定**：`media_mismatch`（§5.3）排在截图距离之前。`matched` 的元素两边的位置都已知、差超过 `media_time_tolerance_s`（默认 1 s），或两边的暂停状态都已知、不同，是不通过；`missing`、`extra`（隐藏的、视口外的元素也算，2026-10-21 确认）、`ambiguous`、任一边是直播（`live`），是不通过；任一边有读数没有应答的 frame（`unreachable_frames`，里面有没有媒体不知道），这一步不可恢复，是不通过（2026-10-21 裁决）；这次观测之前的对齐里有 `failed` 或 `not_restorable`，是不通过。值为 `None` 的量、`comparisons` 为 `None`（这时读不到的 frame 也不参与）、对齐结果 `unknown`，都不参与判定。媒体区域规则声明的页面上（证据带 `media_areas`）上面这些都不判，`screenshot_distance` 是遮掉媒体框之后的距离（上面"页面上的媒体区域"）。截图距离的阈值不变：媒体对上了，截图照样要比。截图距离分不出暂停与播放，所以暂停状态只靠媒体状态比。

**媒体对齐**（`media: align`，§4.1；探针在本地的 `tmp/media_playback_20261020/probe/`）：恢复器在每一个被恢复的观测之前调 `align_next_observation`（§4.9 `RestorationAlignment`），capability 记下预约；那次观测的采集在等完页面落定、确认焦点之后、发出采集事件之前执行对齐，所以截图前的媒体读数与像素都是对齐之后的。步骤：

1. 在 `af-media` 里读一次恢复出的页面的媒体状态（上限 `media_state_timeout_s`），按上面的规则与录下的截图媒体状态对应。录下的观测没有媒体状态（旧证据）时什么也不做，对齐记录为空列表、`detail` 说明。
2. 每个 `matched` 元素以录下的截图之前那次读数（没有就用之后那次）为目标：精确的 `current_time` 与 `paused`。已经在同一位置（差不超过 1 ms）、同一暂停状态的是 `already_aligned`，不动。录下的位置或状态不知道的是 `unknown`，不动。
3. 先对齐录下是暂停的元素：`pause()`，设 `currentTime` 为目标，等到 `seeked` 已经发生、`seeking` 为 false、`readyState ≥ 2`，上限 `media_seek_timeout_s`（默认 2 s），并且跳转完成时停在目标位置上（差不超过 1 ms）。只看 `currentTime` 不算成功：跳到录下字节之外时 `currentTime` 立刻报目标值，`seeking` 却一直为 true、画面不变（实测）；反过来跳转完成了也不一定停在目标上：页面脚本喂数据的播放器在数据还没加完时，跳到 31.04 s 的请求完成在 26.66 s（2026-10-21 实测），这算跳转失败。
4. 再对齐录下是在播放的元素：同样暂停、跳转、等跳转完成，然后 `play()`，等到 `playing`（同一上限），随即采集——不再落定一次，否则在播的视频又会走掉最多一个落定上限。这次采集的截图（连同它前后两次媒体读数）排在其余 fragment 之前（`BrowserStateRequestEvent.screenshot_first`，2026-10-21 裁决）：一个 tab 的 fragment 依次采集、DOM 在前，在播的视频会走掉整个 DOM 采集的时间（实测 5,000 个元素 0.6 s、30,000 个 3.5 s，超出 1 s 容差；截图在前时 0.007–0.017 s）。只有对齐让录下在播的元素在目标处播放着（`aligned` / `already_aligned`）的那次采集这样排，其余采集照旧 DOM 在前。播放器在脚本跳转、播放之后可能露出控制条（小红书的 xgplayer 实测如此），对齐记录照实写 `seek_and_play`。
5. 录下时已经播完的元素（截图前那次读数 `ended` 为 true，2026-10-21 裁决）不直接跳到结尾：直接跳到结尾时，小红书的播放器停在一帧旧画面上、也不出结束画面（截图距离 6–7，实测），而自己播完的视频与源一致。所以先暂停、跳到录下的结尾之前 `media_end_lead_s`（默认 0.5 s），跳转同样要 `seeked`、不在跳转、`readyState ≥ 2` 并停在这个位置上；然后播放，直到 `ended` 事件发生、元素处于结束状态（同一上限 `media_seek_timeout_s`），随即采集。对齐记录写 `seek_and_play_to_end`。已经自然播完、停在同一位置的元素是 `already_aligned`；靠跳转停在结尾、没有结束状态的不算。
6. 跳转或播放没在上限内完成、跳转停在了目标之外、或没在上限内播到结束，是 `failed`：立刻把元素跳回对齐之前的位置、恢复对齐之前的播放或暂停状态（同一上限；这也让播放器停止对未录字节的重试风暴），`rolled_back` 记回滚是否完成。这一步不能恢复：判定里的 `media_mismatch`。
7. `missing`、`ambiguous` 的录下元素与直播是 `not_restorable`，不动它们。`extra` 元素不动（录下的没有它的状态），比较里照样不通过。

每一个动作——哪个元素、从哪里到哪里、做了什么、跳转用了多久、结果、回滚——都进对齐记录，由下一次 `verify_restoration` 交回，写进那一步 `RestorationCheck.evidence` 的 `media.alignment`，从不悄悄发生。回放日志里 `range_uncovered` 的次数从不决定对齐成败：播放器暂停时也会向后预取，越过录下的范围产生大量 miss，而对齐本身完全成功（实测：跳到 130 秒成功，随后 5 秒里 69 次 miss）。对齐只看元素自己的状态。

阈值不在这里：`screenshot_max_distance`、`media_time_tolerance_s` 与 `strict | warn` 是恢复器的旋钮（§4.1）。

焦点的移动不用单独恢复：前缀回放重新执行录下的动作、照常观测，跟随新 tab 与离开关掉的 tab 按与录制时同一条规则发生（上面"页面新开的标签页"），校验比较的就是移动之后采集到的那个 tab；没有重现的移动是 `url_mismatch` 或 `screenshot_distance`。

校验比较的就是录下的与恢复出的两个观测本身，也就是 agent 看到的那两个：两边都在采集之前等过页面落定（上面"观测之前等页面落定"，录制与回放用同一个等待），没有只为校验而另拍的截图或另读的 DOM。`media: align` 时恢复出的观测是在媒体对齐之后采集的：这正是检查点上 agent 看到的那一个，对齐做了什么记在证据里。

#### 已知限制

- 嵌套的 dedicated worker 脚本绕过 Fetch（见上）。
- 归档开着时，内联读的 body（不超过 `max_inline_body_bytes`、没声明长度的，以及 64 MiB 以内的文档）会被扣到下载完（最多 `body_read_timeout_s`），页面拿不到渐进的 HTML；最终 DOM 不变，依赖加载时序的页面行为可能不同（固定 fixture 上验证观测逐字节相同）。更大的 body 边下边收，不扣（上面"体积分档"）。
- `pinned` 不改 `performance.now` 与定时器；没有 `Last-Modified` 头时，`document.lastModified` 是真实时钟（S2a）；不带参数的 `Intl.DateTimeFormat().format()` 也读真实时钟，不经过 shim 的 `Date`（M29 实测）。
- 时区与语言的 override 在一个渲染进程里只有一个所有者，最先设它的 frame（上面的事实表）。同进程的另一个 frame（保留 opener 的同站弹窗、同一个站点的第二个跨源 iframe）再设语言，Chrome 回 "already in effect"，记为该 target 的命令失败（`RecorderConnection.failures`）；再设时区返回成功、不成为所有者。所有者在的时候这些 frame 读到的都是 pin 的值；所有者一离开（iframe 被移除、opener 跳到别的站点），它们在 0.2–0.3 s 内落回浏览器的默认值，保留 opener 的跨站弹窗从第一个文档起就读默认语言。原先这里写"值已由第一个 session 设好、不致命"，只在所有者一直在的时候成立（2026-10-14 实测）。默认时区由启动时的 `TZ` 定成 pin 的时区（`launch_env()`），所以时区一直是 pin 的；默认语言是宿主机的（macOS 上定不下来），所以这些 frame 读到宿主机的语言：录制与回放在同一台机器、或默认语言相同的机器上读到的相同，而 setup 把默认语言记进指纹（`browser_defaults`），回放拒绝默认语言不同的宿主机。剩下的不确定只有落回发生在一次观测之前还是之后（所有者离开的时刻）。
- 打不到 opener 的 tab 的第一个请求带着 Chrome 自己的 `Sec-CH-UA`（上面 override 表的 UA 一行）。标识是 Chrome 自己的形式时（`user_agent: null` 或普通 Chrome 标识），override 带的正是 Chrome 自己的 client hints，这个请求与之后的请求、与页面里的 `navigator.userAgentData` 一致，不再是限制（2026-10-14 实测：这样的 tab 的两个文档请求带着同一个 `Sec-CH-UA`，`test_browser_client_hints_live.py`）。别的形式的标识（自定义字符串、Edge、Firefox）照旧：只有这个请求带 `Sec-CH-UA`，录制与回放相同，回放时这个请求不出浏览器。用户 2026-10-14 的第一个裁决是写明、不改代码；同日的第二个裁决（标识与 client hints 必须一致）走了当时没走的那条路，带上与 Chrome 自己相同的 `userAgentMetadata`。
- client hints 的高熵值（架构、位数、完整版本列表、平台版本、形态）是空的：写了 `user_agent` 的 pinned 给 Chrome 加 `--user-agent` 启动，Chrome 这时自己就把它们报成空的，override 带的是它自己的值（实测，Chrome 155）。真实的 Chrome 会给出真值，所以调用 `getHighEntropyValues` 或经 `Accept-CH` 要这些头的站点看得出差别；低熵值（brands、平台、移动端）一致。要补，得在别处测出真值（不带 `--user-agent` 的另一次启动）或自己合成，两条都还没有走。`user_agent: null` 时 Chrome 不加 `--user-agent`，高熵值是真实的。
- 普通 Chrome 标识的版本必须是本机 Chrome 的大版本：Chrome 自动更新之后，写着旧版本的配置在 setup 被拒（启动之后才知道浏览器的版本；`prepare()` 不报），改 `user_agent` 里的版本号就行。
- 长连接的响应（SSE、长轮询）在 Response 阶段被扣到 `body_read_timeout_s`，这段时间交换是 `pending`，之后记 `incomplete` 并放行（run 在那之前结束的，最后一行是 `pending`，回放读作 `in_flight_at_run_end`）。放行之后页面也收不到事件：读还在跑，Chrome 要等整个 body 下完才交给页面（上面"读答回之前不放行"），所以录制开着时这类响应在页面一侧不工作，原先写的"之后放行"并没有让页面拿到流。
- 下载比 `body_read_timeout_s` 还久、之后又失败的压缩 body，页面照样拿到成功的空响应（放行时读还在跑，上面"读答回之前不放行"）；预算之内的下载失败都按网络错误交给页面。`test_browser_archive_live.py::test_a_response_released_while_its_read_runs_hides_the_cut_from_the_page` 钉住这件 Chrome 的事实：Chrome 改了，这条残余就没了。
- 没有 `Content-Length` 的 body 不论多大都内联读（分档只看声明的长度），解码后超过 64 MiB 的读完记 `too_large`。
- `serialized_dom` 里的元素标记是 Chrome 的 backend node id，两次独立启动同一页面时会因加载时序不同而不同，归档**关着**两边也一样（实测：fixture 的进程外 iframe 元素一半启动是 `[13]`、一半是 `[14]`；开着归档时固定是 `[14]`）。所以"开关归档观测相同"的验收按出现顺序重编号这些标记后比较，其余每个字节（结构、文本、属性、截图）都必须相同。
- **回放的残余**（M30）：
  - 隔离原先留着的 `EXCLUDE localhost` 口子已经关上（2026-10-13，上面"黑洞没有例外"）：隔离回放里绕过 Fetch 的流量连哪儿都连不上，`localhost` 也一样。
  - WebSocket 不回放（v1 记为 miss）；嵌套 dedicated worker 的脚本送不出（看得见的记 `unservable`）；超过 `max_fulfill_body_bytes` 的 body 不送（loopback blob 服务器推迟）。
  - JS 时钟固定在录制时刻（`base_time: from_archive`）时，页面 JS 设的绝对过期 cookie 可能被 Chrome 丢掉；没有 `Last-Modified` 的文档，`document.lastModified` 读真实时钟。
  - 回放 `none` 档位的录制：DPR 不比较（时区与语言是浏览器的默认值，按"一致性检查"比较）；SW 录制时放行、回放时被拦住；只有 pinned 关掉了 `HttpsUpgrades`，所以升级试探可能显示为主 frame 文档 miss。
  - L2 模糊命中可能送错资源（`fuzzy` 与得分照记）；显式 `repeat_last` 才产生复用的 `exhausted`（重复的是最后一条可用的响应）；`miss` 策略产生 `sequence_exhausted`，日志里各自可辨。
  - 代答是有意的偏离（用户 2026-10-20 的裁决）：源里被页面取消或到结束都没回答的请求，回放页面却收到了一次成功，字节是服务器对同一请求给过的；页面为什么在源里取消（例如图片地址 2 ms 后改成了别的），网络证据看不到，回放页面可能因此走上源里没走的路。只在规则声明 `serve_identical`、同一序列所有可用正文相同、被消耗那条录下的状态（如有）与之相同、源里不是真实网络失败时发生，日志 `substituted` 带两条记录与源结局，度量按源结局单列。
  - 没有可代答记录的无正文记录（整个序列都没有正文，或可用正文不同，或被消耗那条录下的状态与可用记录不同，或源里是真实网络失败）照旧按 miss 策略应答（子资源 404），不是源里服务器说过的话，也不是源页面看到的事；日志带源结局。"暂不回答、等页面自己取消"最忠实，但结果受页面时序影响，是另一个待定的设计；它的前提证据（`pending` 行的开始时间、`exchange` / `body` 行的结束时间）已经开始记录（上面 `browser.network`）。
  - JSONP 回调名只在上面"JSONP 回调名"列出的形状里改写：回调名在路径或请求体里的、包装不在这几种形状里的（例如 `window.NAME(`、`if (…) NAME(`），照录下的字节送出，页面照旧拿不到数据，而日志里是普通的命中。规则若显式忽略回调参数，一个 JSONP 请求可能在该规则范围内命中同一接口不带回调参数的 JSON 记录（或反过来），这种应答没有可改的名字，必须作为规则反例检查；jQuery 名字本身没有全局忽略特权。
  - 批次归属是"收集取走它的那一步"，不声称其中每条请求由该步动作引起：页面加载完之后才发出的异步请求（`fetch`、XHR、迟到的 beacon）可能落在下一步的 `replay.jsonl.gz` 里，与录制时 `index.jsonl` 的归因是同一个模型（fixture 实测：金丝雀页面在第 0 步导航，它的 `fetch` 在第 1 步的日志里）。
  - 回放日志导出成 HAR 推迟（还没有按 HAR 打分的评测器）。
  - 第一次 live run（10 个真实 `online_mind2web` 任务上的度量）是度量链只跑录制、原路径回放与报告的那一种（§4.16）；它的链规格与命令在本地的 `tmp/flywheel/M33_live_plan.md`，等用户授权（路由、N、token 上限），没有跑过。M30 写的 `tmp/flywheel/s2b/` 脚本已被它取代。
- **改动之前录下的浏览器快捷键步**（2026-10-24，§4.13 "浏览器自己的快捷键"）：这次改动之前录下的 run 里，后退、前进、重新加载、关闭与切换 tab 的快捷键只发给了页面，记为什么也没发生。分叉或原路径回放重放这些步时命令真的执行，下一个被恢复的观测与录下的不同（`url_mismatch`、`screenshot_distance`），`strict` 下判为发散。不进回放指纹，不为旧录制设兼容路径：要从这些步之后分叉，用改动之后重新录的 run。信号最弱的是重新加载同一个页面：URL 不变，页面状态（滚动位置、填了一半的表单）可能变也可能不变，这一步可能照样通过。
- **改动之前录下的输入失败步**（2026-10-22，§4.5“动作效果”）：输入前判断焦点之前录下的 run 里，焦点不在文本框上的 `type` / `type_coordinate` 当时报告成功（文字没有去处）。分叉或原路径回放重放这些步时动作如实失败，恢复校验按“动作成败与录下的不同”记 `action_failed`，`strict` 下这一步判为发散。不为旧录制设兼容路径：要从这些步之后分叉，用改动之后重新录的 run。
- **页面落定的残余**（上面"观测之前等页面落定"）：视口里已经有内容时，窗口里什么都没发生、只有一个比窗口长的请求在途（点击之后一次慢的接口调用，页面又没有加载提示）不等它，下一步才看到结果（视口空着时等它，那是第 (3) 条），Chrome 不为同一文档里的变化报网络空闲，而在控制连接上跟踪每个请求会把所有网络事件压到 agent 的连接上（recorder 自己有连接正是为此）；CSS 过渡与动画、canvas / WebGL、进程外 iframe 里的变化不经过主文档的 DOM，不算变化；永远不静的页面每一步都付上限（实测：滚动之后 6/38 的站点等满 5 s）。内容这一条只认文字与图形元素：CSS 背景图、伪元素里的文字（`::before` 的 `content`、图标字体）、闭合 shadow root 里的东西它看不见，只由这些组成的页面每次观测都等满上限，视口真的空着的页面也一样；反过来，盖住内容的不透明遮罩、还在等网页字体而暂时不画的文字、还没画东西的 iframe 与 canvas，它都当作有内容。在播的视频就是这样的页面：小红书任务 30 里视频播放的每一步都等满 5 s，暂停的步约 0.5 s 落定；落定记录不点名是媒体在播，读者可以对照同一截图的媒体状态。
- **媒体恢复的边界**（2026-10-20 裁决，上面"恢复：重定位与校验"）：直播（总时长无限）、跳进录下字节之外的位置（跳转不完成，回滚、记 `failed`）、对应有歧义或缺失的元素都不恢复，判为 `media_mismatch`，从不判为通过。跨站 iframe 里的媒体经那个 frame 自己的 CDP 会话读与对齐（2026-10-21 裁决）；只有自己的读数没有应答的 frame（例如它的页面脚本占满主线程）记进 `unreachable_frames`，这一步判为不可恢复（`media_mismatch`）；影子树里的 iframe 算不出自己的路径，总是这样（2026-10-21 裁决：接受，见 §4.4 "截图的媒体状态"）。画布自己画的播放器测不到，只有截图距离把关。离线回放只能送源页下载过的字节：检查点之后视频播过源页下载的范围就会卡住，播放器以每秒十几次的速度重试同一个区间（日志里是 `range_uncovered`，带 `requested_range`）。码率自适应的播放器在本机高速送出时可能换到归档里没有的清晰度。源页在模型思考时继续播放、播完自动跳到下一条，回放里对齐救不回这次导航，表现为 URL 不同。脚本跳转、播放之后播放器可能露出控制条，源截图里没有它；感知哈希看不出来（2026-10-21 裁决：接受，对齐记录里写明）。媒体元数据之前录的源不能做媒体校验（接受）。这些做不到一致的视频站，用户 2026-10-23 裁决：在回放规则集声明的视频页与搜索页上不判媒体、截图比较遮掉媒体框（上面"页面上的媒体区域"）；画布自己画的播放器没有框，遮不到。有界面浏览器里源视频自己停住，用户判断多半是网络停顿看起来像暂停，或可见浏览器上的人工键鼠输入，不再追查。
- 本节的 Chrome 行为都是 153 的实测，归档器的部分与新 tab 的准备、时区与语言的所有者是 155 的实测（本机 Chrome 在 M29 期间升到了 155，153 时写下的金丝雀全部照样通过）；`requires_chrome` 的回归测试是版本金丝雀（新 tab 的在 `test_browser_new_tab_live.py`）。

### 4.15 训练导出（M32）

导出是离线读 `RunTrace` 的消费者，地位与 evaluator 相同：不 setup 任何环境、不调学生或 teacher 的模型、不写源 trace，产物是一个数据集目录。唯一的模型调用是 `wait_actions: drop_when_capped` 时导出器自己配的改写模型，只用来改写 thought，结果存进内容寻址的缓存（见下面"等待动作"）；`keep`（默认）时导出不调任何 LLM。**学生就是一个普通的 af agent**：样本由学生 agent 自己的 `prepare` / `build_react_prompt` / memory 策略渲染、目标轮由它的 `format_react_output` 写出（UI-TARS：一行，调它在 `prepare` 里按学生的动作空间建的无状态编解码，§4.1"UI-TARS：胶水 agent 与无状态编解码"），所以训练格式就是服务格式，训练完的学生接回 af、用同一份运行 YAML 评测。新的学生格式 = 新的 Agent 类（v1 的学生是 `ui_tars` + `coordinate_space: normalized_1000`，`configs/flywheel/ui_tars_student.yaml`）。

**学生一侧是暂定的，导出冻结（用户 2026-10-13 的裁决）。** 这一期交付的是 teacher 轨迹：录制、回放、分叉。学生一侧（消息怎么组装、"学生看截图、输出坐标"）挪到下一期再定。导出包（`spectragent/export/`、`spectragent export`、`configs/flywheel/export_sft.yaml`、`configs/flywheel/ui_tars_student.yaml`）冻结：不加功能，不做新的设计决定；只为横切的改动（改名、manifest 与台账的变化、null 的规则）跟着调整，让它的测试保持全绿。本节写的是冻结时的形状。**例外（用户 2026-10-25 的裁决）**：导出可以后处理 teacher 在没落定的采集上做的等待动作（下面"等待动作"）；除此之外照旧冻结。

#### 配置：运行根的两个可选层

导出不另立根（§4.2.1 实测）：`ExporterRegistrar`（`exporter_id`，`spectragent/export/_registrar.py`）与 `ActionTranslatorRegistrar`（`translator_id`，`spectragent/export/translate/_registrar.py`）进 `collect_registrars()`，是运行根的两个可选顶层层。导出用的 YAML 有一个 `exporter:` 块（`configs/flywheel/export_sft.yaml`），可选地再有运行根自己的 `eval_loader:` / `task_loader:` 块，去污染与泄漏扫描经它们读基准数据（见"基准数据"）：

```yaml
exporter:
  - exporter_id: openai_messages
    sample: per_step                 # v1 唯一值
    image_store: files               # v1 唯一值：图片按 sha256 存成文件
    sources_root: traces             # sources 的根（null：工作目录下的 traces）
    sources: ["recorded/*"]          # 相对根的 glob，按路径排序（默认：["*"]）
    filters:
      agent_outcome: any             # done | max_steps | error | timeout | vetoed（读 result.json）
      require_eval: null             # 可选：{evaluator_id, label: 1, slug: null}，只留判分通过的 run
      exclude_restoration_diverged: true
      max_document_misses: 0         # unlimited：不按回放日志过滤
      exclude_sites: []              # 主机名，后缀匹配（M33 的报告喂它）
    split: {by: site, validation_fraction: 0.1, salt: af-split-1}   # by: site | task
    decontamination: {against: [], match: either}                  # 见"切分与去污染"；[]（默认）：不去污染
    translator:
      translator_id: index_to_coordinate
      occlusion: check               # none：不查遮挡
      occlusion_unknown: raise       # untranslatable：没证据的步按 untranslatable 处理
      untranslatable: drop_step      # truncate | keep_as_text | fail
    student_run_config: configs/flywheel/ui_tars_student.yaml
    thought_source: teacher          # none；relabel 是以后的扩展点
    thought_index_refs: drop         # keep
    wait_actions: keep               # drop_when_capped：去掉 teacher 在被上限截断的采集上做的等待（见"等待动作"）
    # drop_when_capped 且 thought_source: teacher 时这两个必填，其余时候写了就拒绝：
    # thought_rewrite_llms: [{model_type: openai, model: gpt-4.1-mini, provider: {name: direct}}]
    # thought_rewrite_cache: datasets/thought_rewrites   # 改写的内容寻址缓存，相对工作目录
    length: {max_tokens: 65536}      # image_tokens: qwen_vl（默认）| fixed（配 fixed_image_tokens）；null：不检查长度
    output_dir: datasets/sft_v1
# 可选：基准数据从这里读（旋钮俱全）；没配的 benchmark 用它注册的加载器的默认值
# eval_loader:
#   - {benchmark_name: online_mind2web, subset: All, data_dir: ~/agent-data/browser/online_mind2web}
# task_loader:
#   - {benchmark_name: online_mind2web, subset: All, data_dir: ~/agent-data/browser/online_mind2web}
```

`length` 与学生要一致：学生每轮都留截图（`memory: simple`），样本长度随段增长：发布的学生 30 步的整段在 1280×720 上光图像就约 36k token（`qwen_vl`），所以发布的预算是 65536；训练器的上下文更短时要换步数更少的学生，否则导出按名字失败（`test_validate.py` 用发布的两份配置渲染一整段核对）。

- **`ExporterBase`**（不是 dataclass，手写关键字 `__init__`，与 `trace_branches` 同一写法）：上面每个任务字段都是 `__init__` 的 `Annotated` 参数：`sources_root: str | None`、`sources: list[str] | None`、`filters: ExportFilters | None`、`split: SplitSettings | None`、`decontamination: Decontamination | None`、`student_run_config: str`（必填）、`thought_source: Literal["teacher", "none"]`、`thought_index_refs: Literal["drop", "keep"]`、`wait_actions: Literal["keep", "drop_when_capped"]`（默认 `keep`，类型 `settings.py::WaitActions`）、`thought_rewrite_cache: str | None`、`length: LengthBudget | None`、`output_dir: str`（必填），以及 v1 所有导出器共有的 `sample: Literal["per_step"]`、`image_store: Literal["files"]`。`sources` 使用不可变元组默认值，生成配置直接显示真实的数组默认值。嵌套旋钮是 pydantic dataclass，各在 `spectragent/export/settings.py`（`ExportFilters`、`SplitSettings`、`Decontamination`、`BenchmarkRef`、`LengthBudget`）。conscribe 2.0 直接导入这些类型，已注解的受体不再生成遮蔽模块的存根；`require_eval` 复用 `core/data_loader/task/require_eval.py::RequireEval`（M31 的同一个类型、同一个语义）。`translator` 是单实例受体：`__wiring__ = {"translator": "action_translator"}`、类级注解、Python 默认 `default_translator()`（`identity`）。`thought_rewrite_llms` 是第二个受体：`__wiring__` 接 `llm` 注册表，`__init__` 的参数 `Annotated[list[BaseChatModel] | None, Field(max_length=1)]`，与 `prompt_contribution` 的 `helper_llms` 同一形状（至多一个的列表：单实例受体不能省略），只服务等待动作的 thought 改写；它与 `thought_rewrite_cache` 在 `wait_actions: drop_when_capped` 且 `thought_source: teacher` 时必填，其余时候写了就拒绝，构造时检查（组装即失败，`settings.py::thought_rewrite_problem` 一处规则，`ExportRunner` 对注入后的值再查一遍）。自己没有旋钮的导出器不写 `__init__`（conscribe 读到的就是基类的签名）；有自己旋钮的导出器要写出完整签名。`af-regenerate-and-verify` 的检查 1 证明任务字段与 `translator: Union[...]` 都在生成的每键配置上（`tests/test_export/test_registrar.py`）。
- **`Exporter` Protocol**（`_registrar.py`）：`exporter_id`、只读的 `translator`、上面的任务字段、`root()`（sources 根的绝对路径）与 `format(rendered: RenderedSample, images: ImageStore) -> ExportRecord`。`ImageStore.put(data, media_type) -> <数据集里的相对路径>`、`ExportRecord{sample_id, record, images}` 也在 `_registrar.py`，`RenderedSample` 在 `render.py`（它不 import `_registrar`）。Protocol 里的 `translator` 写成只读 property：生成的存根把受体类型写成 `ActionTranslatorBase`，可变属性在 Protocol 里是不变型的。
- **翻译器旋钮**：`ActionTranslatorBase.untranslatable: Literal["drop_step", "truncate", "keep_as_text", "fail"] = "drop_step"`；`index_to_coordinate` 另有 `occlusion: Literal["none", "check"] = "check"`、`occlusion_unknown: Literal["raise", "untranslatable"] = "raise"`。翻译器没有受体，旋钮是手写 `__init__` 的关键字参数。
- **CLI**：`spectragent export --config <yaml>`（`spectragent/export/cli.py`，`pyproject` 的 `[project.scripts]`）：用 `ConfigComposer(registrars=collect_registrars())` 组装，`ExportRunner.from_run(run, ...)` 取恰好一个 `exporter` 实例（否则 `ExportConfigError`）与文档里的加载器（`BenchmarkLoaders.from_run`），跑它，打印每个切分的样本数与按原因的丢弃数；失败非零退出。`/datasets/` 在 `.gitignore` 里。

#### 流水线（`ExportRunner`，`spectragent/export/runner.py`）

不注册（与 `EvaluationRunner` 对应），同步执行（渲染阶段在一次 `asyncio.run` 里），没有环境；除 `wait_actions: drop_when_capped` 的改写模型之外没有 LLM：

1. **源**：`sources_root` 下按 `sources` glob 匹配的 run 目录（`core/trace/sources.py::match_runs`，从 `trace_branches` 挪过来的那一份，加载器改用它），按相对路径排序；只经 `RunTrace` 读已提交的部分。源级检查与筛选（见下）。
2. **canonical 化**：每个可导出的步 → 一个 `CanonicalStepSample`（`schemas/sample.py`）。`wait_actions: drop_when_capped` 时随即去掉截断采集上的等待动作（见"等待动作"）：只剩等待的步不再是样本，其余受影响的步只少了等待。
3. **翻译**：`exporter.translator.translate(sample, trace)`：与学生无关的动作空间换算。
4. **学生渲染**：（`drop_when_capped` 时先按轨迹改写受影响的 thought、从历史里去掉消失的步，见"等待动作"）组装学生、只装配；按段重建上下文；目标轮来自 `format_react_output`；逐轮 round-trip。
5. **格式化**：`exporter.format(rendered, images)` → 每个样本一条记录。
6. **校验**：图片个数、坐标范围、泄漏扫描、凭证扫描、长度。
7. **切分、重复标记、数据集 manifest**，然后落盘。

整个产物先写进 `<output_dir>.partial/`，所有校验通过才原子改名成 `output_dir`；任何一步失败（含校验）都删掉 `.partial`、不留半个数据集（fail closed）。导出出零个样本是 `ExportEmptyError`，列出每个筛选与丢弃原因的计数。

#### 源级检查与筛选

- **不可导出的源**（跳过，原因记进 manifest 的 `sources.skipped`）：读不回来（`TraceSchemaError`；原因只写相对 run 目录的文件与错误类，如 `not a readable trace: manifest.json does not read back (TraceSchemaError)`；读取器的消息带绝对路径、可能引用读不出的字节，只进日志，manifest 因此与 sources 根在哪无关；比当前格式旧的 trace 也是这样读不回来，§5.5）、trace 从没关闭（manifest 还是 `writing`：trace 自己那本账的硬条件，同 `trace_branches`，§4.10）、`TraceConfig.secrets_recorded`（带原样凭证的 trace 不进数据集）、没有记录协商（`TraceConfig.negotiation is None`，手工组装的 run）、与另一个源共用 `run_id`（共用者全部跳过，同 `trace_branches`）。
- **完整到哪一步是硬条件**：已关闭的源只导出它完整到的那些步（`complete_through` 之前、含它；`canonicalise` 只取这些），所以一个记录出洞的 run 导出洞之前的步，尾部失败的 run 一步不少（用户 2026-10-13 的裁决，与分叉同一条规则）。
- **筛选**（`filters`，原因记进 `sources.filtered`）：`agent_outcome`（agent 怎么结束，读源 run 自己的 `result.json`，默认 `any`；没有 `result.json` 的源过不了设了值的筛选）；`require_eval`（可选，默认 `null`、发布的配置也是 `null`：不按判定筛，teacher 失败的轨迹一样导出，每个样本照样带 `labels.step_status` 与判定标签，训练时可以再筛；设了时是 `RequireEval.satisfied_by(RunTrace.evals())`，**分叉 run 用它的源 run 的判定**，因为分叉 run 在 v1 没有评测加载器；源 run 按 `TraceConfig.branch.source_run_id` 在本次匹配到的源里找，找不到就筛掉，原因 `branch source not among the sources`）；`exclude_restoration_diverged`（默认 `true`：任何一条 `StepRecord.restoration` 没通过（`verdict` 不是 `ok`）的 run 整个排除：`warn` 下发散后照常走完的分叉，它的策略步决策时的状态不是源 run 的前缀，上下文因此是错的，这是只有这个筛选挡得住的一类；`diverged` 是 `strict` 中止的分叉，它在策略步之前就以 `error` 结束，本来就没有可导出的步）；`max_document_misses`（回放日志里的主 frame 文档 miss 总数超过它就排除，计数规则与 `ReplayLog.diverged_total` 相同，折叠进 `summary` 的也算；读侧是 browser 的 `replay/replay_log.py::document_miss_total(reader)`；没有回放日志的 run 是 0）；`exclude_sites`（站点键命中列表：主机名相等或以 `.` + 条目结尾）。分叉 run 的源 run 不在本次可读的源里时，不论有没有 `require_eval` 都筛掉（`branch source '<id>' not among the sources`）：它的上下文就是源 run 的前缀。读筛选要用的文件读不回来（`TraceSchemaError`）算跳过，不算筛掉（原因的写法同上）。
- **筛掉的步**（`filters.exclude_unloaded_steps`，默认 `true`、发布的 `export_sft.yaml` 显式写出；用户 2026-10-25 的裁决，§4.4 “初次加载是否完成”）：一个要发射样本的步（`emit`：分叉源前缀之外、有样本的那些），录下的观测里 `capture_state` 的 `settle_state.loaded` 是 `false`——在初次加载完成之前采集，浏览器上就是采集时主文档还没到 `load`——就不产生样本。它照样留在之后样本的上下文里，原样：渲染时与分叉源前缀的步一样，观测轮、目标轮照常写进学生的 memory，只是不发射，段不断（历史不因这项改动改写）。每个被筛掉的步逐个记进数据集 manifest 的 `steps.filtered`，键 `<源路径>#s<t>`（与 `trace_branches` 的 `loader.skipped` 同一写法），值是原因（附录下的等待结果与时长、点名 `exclude_unloaded_steps`），与 `sources.filtered` 一样带原因；`ExportEmptyError` 的计数也算上它们。只是 DOM 没静下来的步照常导出；没有证据的步（没协商 `capture_state`、这一步没录、采集失败、等待关着）照常导出。判定经 `core/trace/sources.py::unloaded_capture_reason`，与 `trace_branches` 的 `exclude_unloaded_checkpoints` 同一条规则；读录下的 fragment 失败（`TraceSchemaError`）是这个源跳过。`false` 时不读。

#### 可导出的步与 `CanonicalStepSample`

**可导出的步**：`origin == "policy"`、`error_phase` 为空、至少执行了一个动作（`zip(actions, action_results)` 的那些；解析失败的步与零动作的步在 `StepRecord` 上分不开，M31 的未决问题，两者都不导出）。分叉 run 只有它的策略步可导出（检查点步 t 与其后的继续步）；恢复步、终止步从不是样本。不可导出的步不产生样本，并在轨迹里结束当前段（"学生渲染"）。

`CanonicalStepSample`（`spectragent/schemas/sample.py`，领域中立：不出现 URL、DOM、bbox 这类字段名，`test_shared_module_is_domain_neutral.py` 覆盖到它；不 import `spectragent.core`）：

- **身份与来源**：`sample_id`（`<run_id>/s<step>`）、`run_id`、`source_trace`（相对 `sources_root` 的 POSIX 路径）、`step`、`benchmark` / `subset` / `task_id`、teacher 的 `agent_id` / `loop_id` / `llm_model`、`config_hash`。
- **任务**：`task`（学生拿到的指令，`TraceConfig.task`）。参考答案与评测提示**不在样本里**：泄漏扫描按需从源 benchmark 的评测加载器取，从不进样本、从不渲染。
- **观测引用**：`observations: list[FragmentRef]`（每个录下的 env 来源 fragment：全限定键、manifest 里的文件路径、产物的 sha256、`failed`）；`image: ImageRef | None`（满足 `ScreenshotGeometry` 的那个 fragment：sha256、媒体类型、`image_*`、`viewport_*`）。
- **历史引用**：`history_steps: list[int]`（同一轨迹里在它之前的可导出步；分叉 run 的是源 run 的 0..t-1 加分叉自己在它之前的策略步）。
- **目标**：`teacher_actions`（执行过的动作，后面接着 Loop 因最后一个执行的动作失败而跳过的动作（`StepRecord.skipped_actions`，§4.1"一步里有动作失败，这一步就停下"），全限定名，照录下的样子：参数在执行之前已按该动作的 `Params` 校验过，§4.1“参数不合 `Params` 的动作是解析错误”，所以没有要丢掉的未声明参数。跳过的动作在目标里，因为它们与执行过的一样是 teacher 的决定：只写执行过的那部分，目标就成了 teacher 从没给过的一条更短的回复，与它的 thought 也对不上；学生在同一状态下给出这条回复，同样会停在那个动作上。`done` 之后的动作不是谁据以行动的决定，照旧不在目标里。`export/canonical.py::target_actions`）；`thought: ThoughtRecord(text, source_field) | None`（`thought_source: teacher` 时经 teacher 的 `parse_react_thought`，§4.1）；翻译之后的 `actions`、`untranslatable: Untranslatable(reason, detail) | None` 与 `translation: list[TranslationRecord(translator_id, action_index, payload)]`（翻译器的来源记录，内容由翻译器定，对样本是不透明的 JSON）。
- **标签**：`labels.step_status`（`ok`：每个执行的动作都成功；否则 `action_failed`，照样导出、标出；结束动作的成功是它执行了，agent 自报没有完成的结束步也是 `ok`，§4.1"agent 的任务结论"）、`labels.skipped_actions`（目标末尾有几个动作 teacher 的 Loop 没执行；大于 0 时 `step_status` 一定是 `action_failed`；默认 0，所以以前导出的样本照常可读，数据集格式版本不变）、`labels.loss_weight`（目标轮 1.0）、`labels.verdicts`（该 run 的每份 `graded` 判定：`verdict_slug`、`evaluator_id`、`predicted_label`，分叉 run 用源 run 的）。
- **脱敏台账**：`redactions`，源 `TraceConfig.task_setup` 台账的条目照抄（策略版本、位置、字段、规则），让读者知道源 run 的哪些输入被换过。
- **分叉元数据**：`branch: BranchSampleMeta | None`，含 `branch_group_id`（`<source_run_id>/s<t>`）、`source_run_id`、`checkpoint_step`、`branch_index`、终止观测的引用（`terminal_step`、`terminal_image_sha256`，没有终止步时为 `None`）、`duplicate_of`。
- **等待动作的来源**：`wait_removal: WaitRemoval | None`（`wait_actions: drop_when_capped` 改过的样本才有，见"等待动作"）：`removed_actions`（本步去掉的等待）、`removed_steps_before`（它前面消失的只剩等待的步的动作），条目都是 `RemovedAction{run_id, step, action_index, action}`；`thought: ThoughtRewrite | None`（原文 `original`、改写后的 `rewritten`、`outcome: rewritten | reviewed_unchanged`、`synthetic`（只有 `rewritten` 为真）、`model: RewriteModel{model_type, provider, model}`、`prompt_version`、`prompt_digest`、`cache_key`）。这时 `teacher_actions` 是去掉等待之后的目标，`thought` 是改写后的（改写成空时为 `None`），`history_steps` 里没有消失的步。

`UntranslatableReason = Literal["no_counterpart", "target_offscreen", "exceeds_action_budget", "occluded", "occlusion_unknown", "unrepresentable"]` 也在 `schemas/sample.py`。

#### 翻译器（`ActionTranslatorRegistrar`）

```python
class ActionTranslator(Protocol):
    translator_id: str
    untranslatable: Literal["drop_step", "truncate", "keep_as_text", "fail"]
    def translate(self, sample: CanonicalStepSample, trace: RunTrace) -> Translation: ...
    # Translation{actions: list[Action] | None, untranslatable: Untranslatable | None, records: list[TranslationRecord]}
```

翻译只做与学生无关的换算；"学生写不写得出来"在渲染时由学生自己判定（下面），两处的不可译原因走同一个 `untranslatable` 策略（翻译器只报告原因，策略由渲染执行）。翻译后的每个动作都过一遍它的 `Params.model_validate`（只留设了的参数；过不了是 `InvalidTranslationError`，表示翻译器或数据有缺陷，不算不可译）；没注册的动作名是 `no_counterpart`；一步里任一动作不可译，整步不可译。

- **`identity`**：原样交出 `teacher_actions`。UI-TARS 录下的 run 本来就是坐标动作。
- **`index_to_coordinate`**：带元素 index 的动作（参数上带 `AddressesElement("browser.dom")` 且这一次给了值的那些，`core/actions/params.py::element_params`）换成坐标动作：`browser.click` → `browser.click_coordinate`、`browser.type` → `browser.type_coordinate`（`text`、`clear` 照搬），其余带了 index 的（`select_dropdown`、带 `index` 的 `scroll` / `extract_content`）是 `no_counterpart`；不带 index 的动作原样。index 在录下的 `selector_map` 里找不到（源 run 的这个动作当时就找不到目标）也是 `no_counterpart`。换算的 browser 知识在 `core/capabilities/browser/index_points.py`（纯函数，读交进来的 fragment 与文档，不 import `core.trace`），翻译器只组织调用：
  - **几何**：录下的条目（M27 起自包含）的 `bounds` 是顶层文档的页面坐标（CSS px，已计入 iframe 偏移，与 DPR 无关）；减去 `DOMState.scroll_info` 的滚动得到视口坐标；与视口求交；条目在重链接的 `element_tree.dom_tree` 里找得到时，再与每一层 frame 宿主的 `bounds` 求交（被深度截断而找不到时只裁视口，来源记录里写 `frame_clip: "unknown"`）；交集为空是 `target_offscreen`（live 的 index 点击会先滚动到可见，录下的截图上表达不了）；否则取可见部分的中心，四舍五入成整数 CSS px（坐标动作的参数是 `int`）。变换过的 iframe 只按平移处理（已知限制）。
  - **遮挡**（`occlusion: check`，只对换算出的点）：证据按优先级：(a) 这一步的 `selector_map` 条目带 `hit_test`（`skyvern_hit_test` 录的）：中心命中用中心，否则用 `visible_point`，两者都没有就是 `occluded`；(b) 这一步的 `browser.dom_raw` 文档（`RunTrace.archive_files(step, "browser.dom_raw")`，`archive/dom_raw.py::read_dom_document` 解开；按文档戳挑与本步同一个文档的那份）里的 DOMSnapshot：在目标所在的主文档里，包含该点（快照 `bounds` 是设备像素，除以文档里记的 DPR）且 paint order 最大的布局节点必须是目标或它的后代（`pointer-events: none` 与 `visibility: hidden` 的节点不算，点击穿过它们；并列的 paint order 算目标的），v1 只判主文档里的目标，frame 里的目标这一条证据不适用；(c) `browser_use` 清洗过、trace 里带 `paint_order` 的 `dom_tree`：同一个查询，只在目标所在文档的、留下来的节点里做（BrowserUse cleaner 已经把整个被盖住的元素去掉了，所以这一条只抓得到部分遮挡）。(b)(c) 判定被盖住时，在可见部分里按 5×5 内点网格（内缩 2 px、行优先）找第一个不被盖住的点；找不到是 `occluded`。三条证据都没有：`occlusion_unknown: raise`（默认）抛 `OcclusionEvidenceMissingError`，点名样本与 index，并提示"绑定 `browser.dom_raw` 或用 cleaner `skyvern_hit_test` 录制"；`untranslatable` 时按不可译原因 `occlusion_unknown` 处理。`occlusion: none` 不查（原名 `off`，用户 2026-10-13 的裁决改名）。
  - **来源记录**：元素引用（`element_index`、`backend_node_id`、`target_id`）、页面 bbox、视口里的可见矩形、点的来源（`center` / `hit_test` / `grid`）、遮挡判定（`clear` / `occluded` / `unknown`）与所用证据（`hit_test` / `dom_raw` / `paint_order` / `none`）。

#### 学生渲染（`spectragent/export/render.py`）

- **组装**：`ConfigComposer` 组装 `student_run_config`（一份普通运行 YAML，不带 hook）；它必须恰好有一个 agent，`AgentRuntime.for_agent(run, <它>).prepare()`，用同一套协商、带 `fragments=` 收窄的 descriptors、绑定表与 weave（§4.2）；学生的 Loop 必须是 `ReactLoop`（含子类），学生配置不得带 hook（学生只装配、从不运行），否则 `ExportConfigError`。然后查导出用的可选 slot（§4.1）：学生没有 `format_react_output`、或 `thought_source: teacher` 而某个源的 teacher 类没有 `parse_react_thought`，抛 `IncompatibleError(..., needed_for="needed for training export ...")`。种子消息只经 `runtime.seed_messages(task)`。从不调 `execute()`、`setup_environment()` 或 LLM。
- **片段与采集检查**：学生协商出的 env 来源 fragment 必须 ⊆ 源 run 录下的（`TraceConfig.negotiation.env_sourced_fragments`），否则 `IncompatibleFragmentError`（`provider=` 措辞参数写成源 run，点名缺的 fragment，比如学生要 DOM 而源 run 是纯视觉，或学生协商 `capture_state` 而源 run 没录；从原始归档重渲染是以后的事）。所以 `show_capture_state`（§4.4）这类改变协商的开关属于导出身份。每个取样的步把录下的 env 来源 fragment 交给学生环境的 `compare_capture`（§4.3、§4.9）：有 `mismatches` 抛 `StudentCaptureMismatchError(source, mismatches)`，`unchecked` 记进 manifest。
- **一个轨迹、若干段**：轨迹是一个源 run（分叉 run：源 run 的 0..t-1 加分叉自己的策略步）。段从一个可渲染的步开始：学生的 memory（学生自己的策略实例）清空、写入 `seed_messages(task)`。段内每一步 k：观测 = 学生协商出的 `NegotiatedObservation`，env 来源 fragment 取 `RunTrace.observation_fragment(k, key)`，loop 来源 fragment 由 `loop_fragment_builders(...)`（§4.1）建：`loop.step_info` 的 `step` 是 k+1，`max_steps` / `today` 取源 run 第 k 步录下的 `loop.step_info`（学生 Loop 的 `today_override` 优先；源 run 没录 step_info 时 `today` 取源 manifest 的 `created_at` 的 UTC 日期、`max_steps` 取学生 Loop 的，manifest 记下用了哪个来源），`loop.history` 是这一段里之前的步（源 `StepRecord` 的副本，`actions` 换成翻译后的，跳过的动作照旧在最后、`skipped_actions` 不变，所以学生下一步同样被告知哪几个没执行；`keep_as_text` 的步换成它记成文本的那些动作；`frame` 换成学生在那一步的坐标系），`loop.action_result` 取这一段里的上一步（段首没有上一步）；env 来源 fragment 经 `core/trace/reconstruction.py::recorded_observation` 读（与 `BranchLoop` 的 `SourceRun.observation` 同一个读法），录下的 step_info 经 `recorded_step_info` 读、按 `_loop_fragments.py::pinned_step_info` 钉住（与 `BranchLoop` 同一条规则）；学生在第 k 步的坐标系由框架建：学生声明的网格铺在录下的截图上（`core/loops/coordinate_frames.py::step_frame`，与实时 run 同一个函数；teacher 记下的 `StepRecord.frame` 是 teacher 的网格，不用），放进 `ReactPromptContext.frame`；`await build_react_prompt(obs, context=...)` 写进 memory；k 是样本时此刻的 `memory.get_context_messages()` 就是它的上下文；然后框架把 k 的翻译后动作的点放到这个坐标系上（`actions_in_model_frame`），`format_react_output(<放好的动作>, thought=<k 的 thought>)` 写出目标轮，立即 `parse_react_output` 那段文本并由框架换回 capability 的单位（`actions_in_capability_units`，round-trip；Skyvern 的动作历史窗口不靠这一步：它从 `loop.history` 读实际执行的动作、结果与效果行），文本经 `ReactLoop.remember_reply(memory, text, [])` 写进 memory。学生不做任何坐标换算。UI-TARS 学生的这两个 slot 都是它的编解码（`UITarsCodec.write` / `read_actions`）：编解码在每段开头的 `seed_messages` → `prepare` 里按学生的动作空间建一次，只读交给它的动作与文本，所以写目标轮与读回不依赖学生渲染过哪些步、以什么顺序；导出经 slot 调它，不 import UI-TARS 的模块（导出对任何学生都成立）。较早的 assistant 轮 weight 0，目标轮 weight 1。
- **缺学生要的 fragment 的步**：某一步没录下学生要的 env 来源 fragment，或在学生必需（`required_fragments`）的 fragment 上录下的是采集失败（teacher 那边它是可选的）：这一步不渲染、结束当前段，manifest 记 `steps.skipped.fragment_unavailable`。
- **学生写不写得出来**：动作名经 `resolve_action_name(name, <学生守卫过的动作名>, <学生的 action_bindings>)` 解析不出来 → `no_counterpart`；一步的动作数超过学生 Loop 的 `max_actions_per_step` → `exceeds_action_budget`；框架把点放到学生坐标系上时放不下（那一步没有可测尺寸的截图、或点在截图之外），或 `format_react_output` 抛 `UnrepresentableActionError` → `unrepresentable`。
- **thought**：`thought_source: none` 时目标轮与历史轮都没有 thought。`thought_index_refs: drop`（默认）且学生协商出的 fragment 不含源 run 的元素地址参数所在的 fragment（`element_params` 那些参数的 `AddressesElement` 键，browser 上就是 `browser.dom`）时，含 `[123]` 这类 index 记号的 thought 被丢掉（样本保留、只是没有 thought），manifest 计数。
- **不可译的步**（翻译器给的或渲染时判的）按翻译器的 `untranslatable` 处理。`drop_step`：这一步不是样本，当前段结束，下一个可渲染的步开新段（重新播种的上下文，同一个任务）；`truncate`：这条轨迹在这一步结束；`keep_as_text`：这一步不是样本，但它的观测轮照常进上下文，assistant 轮写成 `[not expressible: <reason>] <动作的 JSON 列表>`（键排序），每个动作的点先由框架放到学生在第 k 步的坐标系上（`fact_in_model_frame`），再经学生的可选 slot `format_action_fact(action)`（§4.1）写，与下一步反馈里的写法相同；学生没有这个 slot 时是动作自己的 JSON。段不断；`fail`：抛 `UntranslatableActionError(sample_id, reason, detail)`。每个原因的丢弃数进 manifest（M33 的输入）。不可导出的步（上面）总是结束当前段。
- **消失的步**（`wait_actions: drop_when_capped` 去掉了它全部的动作，见"等待动作"）不渲染、不结束段，也不计入任何丢弃或跳过；它之后的步在学生看到的编号上前移。
- **identity 导出的期望**：对 UI-TARS 录下的 run 用 `identity` 与同一份学生配置导出，第 k 个样本的上下文与源 run 第 k 步第 0 次调用的 `messages` 在 trace 的记录形态下（`core/trace/reconstruction.py::recorded_form`）逐条相同，assistant 轮按解析结果相同；预期的差异只有：源 run 的解析重问轮与截断提示（学生上下文里从不写它们）、跳过或丢弃的步造成的分段、assistant 轮的书写形式（学生的规范写法 vs 模型原文），以及 `wait_actions: drop_when_capped` 去掉的等待、消失的步、前移的步号与改写的 thought。

#### round-trip（`parse(format(a)) == a` 做不到，按下面的定义）

实测：`normalized_1000` 在 1280 px 宽的视口上有 280/1280（22%）个 x 值回不到自己（误差至多 1 px）；agent 解析出的是相对名，trace 里是全限定名；有些参数没有写法。对一个目标（翻译后、全限定名）动作 a，round-trip 成立当且仅当：

1. **名字**：`resolve_action_name(parse(format(a)).name, <学生守卫过的动作名>, <学生的绑定表>) == a.name`；
2. **不动点**：`format(parse(format(a))) == format(a)`（文本相等，thought 在内）；
3. **点**：每个点参数 `|parse(format(a)) − a| ≤ 学生网格的 quantum`（`PointGrid.quantum`，见下；网格是学生在这一步的坐标系 `step_frame(<学生的动作空间>, <这一步的观测>).grid`，没有定坐标系或没有网格时 quantum 为 0）。这里的 `format` 与 `parse` 都含框架的换算：`format(a)` 是 `format_react_output(actions_in_model_frame(a))`，`parse(t)` 是 `actions_in_capability_units(parse_react_output(validated_reply(t)))`（有输出模型的学生，文本先由框架按它的输出模型校验，§4.1"结构化回复不合输出模型，由框架措辞"；文本协议的学生原样读），与服务时一样。哪些参数是点由动作的 `PointAxis` 标记认（§4.5“参数的含义”）：学生 descriptor 的 `point_params`（`coordinate_x/y`、`start_x/y`、`end_x/y`）；
4. **其余参数**：a 设了非 `None` 值的每个参数都原样回来；a 没设的参数由学生自己填。写不出来的参数在 `format` 里就抛 `UnrepresentableActionError`，从不静默丢掉（UI-TARS：`type` 的 `clear`、`scroll` 的 `amount` / `index`、`wait` 的 `seconds`、`done` 的 `success`、含空白的 `send_keys` 组合键）。`done` 的 `success` 是 agent 对任务的结论（§4.1"agent 的任务结论"，参数带 `TaskClaim` 标记）：`finished()` 读回时没有结论。没有结论的结束步照常写成 `finished(...)`、原样回来。**说完成了的结论（`true`）学生格式写不出时不算这一条**（用户 2026-10-23 的裁决，在导出层处理）：结论是 agent 的陈述，不是对环境的指令，去掉它不改变目标执行出的动作，而成功的结束步是学生学会何时结束的唯一样本；所以导出把这个结论去掉、写成学生的普通结束（UI-TARS 的 `finished(content=...)`），round-trip 比较的是去掉结论之后的动作，样本在 `samples.jsonl` 的 `render.claim_not_written: true` 注明 teacher 说了完成、学生格式写不出（teacher 的结论仍在样本的 `teacher_actions` 里）。判断只看拒绝说明里点名的参数：全部是那个动作的 `TaskClaim` 参数、而且都是 `true`，才这样写；**说没完成的结论（`false`）照旧 `unrepresentable`**，原因不变（放弃的结束不当作正常结束来教），任何别的写不出的参数也照旧。BrowserUse、Skyvern 这类 teacher 录下的结束步通常带着结论：`true` 的成为样本，`false` 的按翻译器的 `untranslatable` 处理（默认 `drop_step`）。

**训练标签是量化后的点**（目标轮文本里写的网格点；它在服务时执行成 `parse(format(a))` 的那个视口点）。每个点的量化误差（像素，按值分桶）进 manifest。`PointGrid.to_grid(vx, vy) -> tuple[int, int]`（`schemas/observation/geometry.py`：`round(v × grid / viewport)`，夹到 `[0, width] × [0, height]`）是 `to_viewport` 的逆；`PointGrid.quantum: int` 是一次 `to_grid` → `to_viewport` 在任一轴上可能带来的最大视口误差：网格不比视口粗（`viewport / grid ≤ 1`）时为 0，否则 `floor(viewport / grid / 2 + 1/2)`（1280 px 上的 `normalized_1000` 是 1；整数 DPR 与 1.5 的 `screenshot_pixels`、1000 px 视口是 0）。round-trip 对上下文里每一个 `format_react_output` 的结果都检查（目标轮与历史轮），失败是 `RoundTripError`，表示格式器有缺陷，不算不可译。

#### 格式化：`openai_messages`

- 每个样本一条记录（`<split>.jsonl`）：`{"id": <sample_id>, "messages": [...]}`；消息经 `core/llm/serializers/openai_format.py::OpenAIMessageSerializer` 序列化，assistant 消息带 `weight`（较早的轮 0，目标轮 1）。图片部分的 data URL 换成相对路径 `images/<sha256><后缀>`，同一张图在整个数据集里只存一份（`image_store: files`）。OpenAI 的自助微调在收缩，这只是交换格式；以后的 `llamafactory`（`<image>` 个数 = 图片个数）、`ms_swift`（`loss_scale`、`rejected_messages` 做 DPO）、`trl` 是新的导出器。
- 同序的 `<split>.samples.jsonl`：每行 `{"sample": <CanonicalStepSample>, "render": {split, segment, context_steps, step_info, target, target_actions, quantization_px, thought, thought_dropped, claim_not_written, images}}`（`claim_not_written`：teacher 的结束说了完成、学生格式写不出，见 round-trip 第 4 条）。`target_actions` 是目标轮文本执行出的动作（量化后的点：训练标签），`duplicate_of` 在样本的 `branch` 里；这个文件是与格式无关的那一半。
- **数据集 manifest**（`manifest.json`）：写与读经同一个类型化模型 `spectragent/schemas/dataset.py::DatasetManifest`（导出器写、度量报告读；改了名或类型的键在读的时候点名失败，不会读成 0）。数据集格式版本 `af-sft-4`（2：去污染只认明确的列表、泄漏扫描只数比过的样本；3：样本的 `teacher_actions` 照录下的样子，不再有 `dropped_params`；4：`steps.filtered` 逐步列出筛掉的步与原因，等待动作的台账 `wait_actions`，样本可带 `wait_removal`）、导出器与翻译器的配置（导出器的含 `wait_actions`）、学生配置的摘要（学生 `top_level_cfg` 的规范 JSON 的 sha256）与学生的 agent / loop / `coordinate_space`、每个源（相对路径、`run_id`、`config_hash`、样本数）、`sources.skipped` / `sources.filtered`（带原因）、按原因的丢弃与跳过、逐步列出的筛掉的步（带原因）、量化误差分布、thought 的来源字段计数与丢弃数、`step_info` 的来源计数、采集检查的 `unchecked`、切分（每个切分的样本数、每个切分键分到哪里）、去污染与泄漏扫描的结果、重复数。键名：`format`、`exporter`、`translator`、`student{config_sha256, agent_id, loop_id, coordinate_space}`、`sources{exported, skipped, filtered}`、`steps{untranslatable, skipped, filtered}`、`samples`、`quantization_px`、`thoughts{source, fields, dropped_index_refs}`、`step_info`、`capture{unchecked}`、`split{by, validation_fraction, salt, samples, keys}`、`decontamination{decontaminated, against, match, excluded}`、`leak_scan{compared, no_eval_loader, no_reference_answer}`、`duplicates`、`wait_actions{policy, removed_steps, removed_actions, rewritten, reviewed_unchanged, rewriter}`（见"等待动作"；`keep` 时两个列表为空、计数为 0、`rewriter` 为 `null`）。**不含任何墙钟字段、绝对路径或主机信息**：路径类的旋钮（`sources_root`、`student_run_config`、`output_dir`、`thought_rewrite_cache`）不进 manifest，学生配置只记摘要，跳过原因不带读取器消息（见"源级检查与筛选"）。
- **环境证据从不导出**：`env/` 只为遮挡证据与文档 miss 计数被读；输出目录里没有任何文件来自 `env/`（测试按内容哈希核对）。

#### 校验（`spectragent/export/validate.py`）

round-trip 在渲染里做（它要学生在这一步的坐标系），其余的在格式化之后、落盘之前，任何一条失败都让整个导出失败：

- **图片个数**：每条记录的图片部分数 = 渲染出的上下文里的图片数，每个引用的文件都在 `images/` 里。
- **坐标**：每个点参数在本步截图的视口内（`[0, viewport)`），学生网格点在网格内，执行点在图像内。
- **泄漏扫描**：源 benchmark（分叉 run 用源 benchmark）的评测加载器给出这个任务的 `expected_answer`：来自导出文档为它配的 `eval_loader`（按文档顺序，第一个有这个任务的），没配时来自它注册的评测加载器按源 run 的 `subset` 默认构造的实例（"基准数据"）；规范化（casefold、空白折叠）后长度 ≥ 8 时，它不得出现在导出流水线自己放进 prompt 的文本里（种子 system 消息与任务文本），否则 `ReferenceLeakError` 点名样本与字段。观测本身（页面上本来就有答案）是世界，不算泄漏。manifest 只把真的比过的样本记成 `leak_scan.compared`（"没比"不能读成"查过"）；没比的按原因分开记：既没配、也没注册评测加载器的 benchmark 记 `leak_scan.no_eval_loader`，参考里没有答案、或答案不到 8 个字符的记 `leak_scan.no_reference_answer`（online_mind2web 的 300 条记录都没有 `expected_answer`，所以它的样本全在这里）；加载器读不出来、或没有这个任务的参考，是错误。
- **凭证扫描**：`core/trace/secret_patterns.py::SECRET_PATTERNS`（从 `test_secret_guard.py` 挪过来，secret guard 测试与这里共用一份）扫描要写出的每一个文本字节（记录、样本、manifest），命中即 `CredentialLeakError`（点名文件、样本与模式标签），fail closed。它认的是凭证的形状，不是 PII（残余见"隐私"）。
- **长度**：`length` 给了时用可插拔的 `LengthEstimator` Protocol 估算每条记录的长度，v1 的 `ApproxTokenEstimator`：文本字符数 / `chars_per_token`（默认 4），每张图按 `image_tokens` 取 `qwen_vl`（`ceil(w/28) × ceil(h/28) + 2`，按图像像素）或 `fixed`（`fixed_image_tokens`）；不依赖 tokenizer、不联网。超过 `max_tokens` 抛 `SampleTooLongError` 点名样本，从不截断。新的估算器（真 tokenizer）有第二个实现时再成注册表。

#### 切分与去污染

- **按站点或任务切分，从不按步**：`split.by: site` 的键是源任务 `start_url` 的主机名（`TraceConfig.task_setup.context["start_url"]`，M28 记下的、值级脱敏过的 URL，主机名不受脱敏影响），`task` 的键是 `<benchmark>/<subset>/<task_id>`；分叉 run 用它的源任务（`TraceConfig.branch` 里的 `source_*`），所以同一个 `branch_group_id`、同一个源任务的样本总在同一个切分里。切分 = `sha256(salt + "\0" + key)` 的前 8 字节作为 [0, 1) 的数小于 `validation_fraction` 进 `validation`，否则 `train`。算不出键的 run（没有 `start_url`）跳过，原因 `no split key`。
- **去污染**：`decontamination.against` 列出学生将来评测用的 benchmark（`BenchmarkRef{benchmark, subset}`），只认这份明确的列表，从不从评测器或判定推断（用户 2026-10-13 的裁决）。默认 `[]`：不去污染，CLI 记一条 INFO，manifest 记 `decontamination.decontaminated: false`；`null` 不是列表，组装时就报错。`match`：`task_id`（样本的源任务 id 在该 benchmark 评测加载器的任务里）、`site_instruction`（源任务的 start_url 主机 + 规范化指令与该 benchmark 任务加载器里某个任务相同；任务变体的分叉比对变体文本与源任务文本两份指令，§4.10）、`either`（默认）。命中的源整个排除，原因 `decontaminated`。加载器读不出来、没注册也没配是 `ExportConfigError`（去污染从不静默地什么都没查）。
- **基准数据**（去污染与泄漏扫描共用，`spectragent/export/benchmarks.py::BenchmarkLoaders`）：导出文档自己的 `eval_loader:` / `task_loader:` 块（运行根的层，组装时旋钮俱全，如 `data_dir`、`subset`），按 `benchmark_name` 对上 benchmark、按文档顺序；文档没给某个 benchmark 配加载器时，才用它注册的加载器按默认值构造（`spectragent bench` 解析评测加载器的同一条规则：配置优先，其次注册表）；配了的 benchmark 只经配的那些读，从不在配好的实例旁边再默认构造一个，那一个会无视文档配的旋钮（af-add-a-receptor §0 的那类缺陷）。去污染的引用写了 `subset` 时只读该 subset 的已配加载器（配了这个 benchmark 却没有这个 subset 是 `ExportConfigError`，两处设置不一致不猜），`subset: null` 读这个 benchmark 的全部已配加载器（没配时是注册加载器的默认 subset）。所以为泄漏扫描另配了训练 subset 的评测加载器时，`against` 里要写明评测的 subset。泄漏扫描在源 benchmark 的全部已配评测加载器里找这个任务。列表的后果写明：把录制所用的 benchmark 本身列进 `against`，会把这些源 run 全排除（导出因此 `ExportEmptyError`）；要训练就列一个不相交的评测集，或者不列。

#### 分叉样本（默认：中立记录）

每个分叉 run 导出它的策略步，上下文的历史来自它的源 run（按 `source_run_id` 在本次的源里找到，同样的翻译、同样的段规则），第 t 步的观测是分叉 run 自己录下的（经恢复校验的 live obs_t）。元数据带 `branch_group_id`、终止观测的引用。同一个 `branch_group_id` 里检查点步的目标动作（翻译后的名字与参数，不比 thought）完全相同的样本，排序在后的标 `duplicate_of = <排在前的 sample_id>`，只标出，不删。怎么把分叉结果变成训练信号（judge 选正样本、DPO 对、world model）不在导出器里，v1 只交中立记录。

#### 隐私

- `env/` 从不导出；样本里没有原始 DOM，学生没协商 DOM 时 DOM 文本也进不了 prompt。
- `secrets_recorded` 的源不导出；凭证扫描 fail closed。
- **残余，写明**：凭证扫描认形状：teacher 键入一个不像凭证的密码（`type` 动作的 `text`）会原样成为训练目标；录制时键入动作的结果记下了 `typed_into_sensitive_field`（§4.14 隐私），导出怎么用它随学生侧一起推迟。BrowserUse 观测里的密码框已按敏感规则打码（§4.12）。v1 的学生是纯视觉的 UI-TARS。

#### 确定性

同样的输入导出两次，输出目录逐字节相同（整棵树的哈希）：源按相对路径排序、步按序号、图片按 sha256 命名、JSON 键排序且分隔符固定、`today` 取自 trace、没有墙钟字段、路径都是相对的、切分是盐 + 键的哈希。测试用不同的文件系统创建顺序建两遍源。`wait_actions: drop_when_capped` 的改写来自模型，模型调用本身不确定：确定性靠改写缓存（"等待动作"）：数据集只取缓存里的改写，所以同样的输入、配置与缓存导出两次逐字节相同；第一次导出填满缓存之后，再导出一次不调模型、整棵树的哈希与第一次相同（命中与未命中的计数因此不进数据集，见下）。

#### 等待动作（`wait_actions`，用户 2026-10-25 的裁决）

慢网络上录下的 teacher 轨迹里有一类等待：每次采集之前的落定等待被上限（`settle_max_wait_s`，5 s，§4.14 "观测之前等页面落定"）截断，teacher 看到的是还没加载完的页面，于是先等一等。15 个真实录制里，409 个策略步有 109 步含等待动作，其中 48 步的采集被上限截断。学生在正常网络上服务，不该学这种等待。用户的裁决：训练导出可以后处理它们，去掉 teacher 在截断的采集上做的等待动作，把受影响的 thought 改写成页面正常加载时的样子。录下的 trace 不变（导出从不写源 trace）；只改导出的数据，每个去掉的步与动作、每个改写的 thought 都有来源记录，改写的 thought 标为合成。代码在 `spectragent/export/waits.py`。

- **开关**：导出器的 `wait_actions: keep | drop_when_capped`（`export/settings.py::WaitActions`），与 `thought_index_refs` 一样是整形样本的选项，不是源的筛选。默认 `keep`：与此前完全相同，不读 `capture_state`、不调模型。发布的 `configs/flywheel/export_sft.yaml` 用 `keep`，所以脚本化的度量演练（`tests/_helpers/flywheel_rehearsal.py`）不需要 API key。
- **哪一步的采集算截断**：策略步 k 的观测里有平台中立的采集状态（注册类是 `CaptureStateFragment` 的子类的 fragment，§4.4 "跨平台采集状态"；browser 上是 `browser.capture_state`），它的 `settle_state.outcome == "capped"`（`pending` 是什么都算）。fragment 键的前缀是采集它的 capability。没有采集状态的步（没协商、不在录制范围里、这一项采集失败、等待关掉了）原样保留：没有证据就不改。
- **哪些动作算等待**：由动作自己声明（与 §4.5 "参数的含义" 同一条原则：含义只在声明处写一次）：`ActionBase.only_waits: ClassVar[bool]`，只让时间过去、不碰环境的动作为 `True`（`browser.wait`），其余为 `False`。导出按录下的全限定名（守卫限定过的名字）在 `ActionRegistrar` 里找到动作类读它（`core/actions/_registrar.py::is_wait_action`），从不按名字或后缀认。去掉的只是与截断的采集状态同一个 capability（键的前缀）的等待动作。
- **去掉什么**：可导出的策略步 k 的采集被截断时，去掉 k 里每一个**执行了而且成功**的等待动作，连同它的结果。
  - **失败的等待留下。** 它的失败是 teacher 的错（例如秒数超过 `max_seconds`），记录用它说明后面的动作为什么没执行（`skipped_actions`，§4.1 "一步里有动作失败，这一步就停下"）。"等待后面跟着跳过的动作"一定是这种情形，因为跳过的动作只跟在失败的动作后面：这一步就原样保留，`skipped_actions` 不变，学生学到的仍是那条停在失败处的回复。
  - **跳过的尾部里的等待留下。** 它没执行；跳过的尾部是 teacher 的回复在失败之后的部分，导出照录（"可导出的步与 `CanonicalStepSample`" 的 `teacher_actions`）。
  - 所以去掉的动作从不是最后那个失败的结果，也不在跳过的尾部里：步的副本上 `actions` 与 `action_results` 一起去掉那几项，`StepRecord` 的不变式（跳过的动作恰好跟在失败的结果后面）照样成立，`labels.step_status` 与 `labels.skipped_actions` 不变。
- **只剩等待的步消失。** k 的目标动作（执行的加跳过的）全被去掉时，k 从导出的轨迹里消失：不产生样本，也不出现在之后样本的历史里，它的观测轮与回复轮都不进上下文，`history_steps` 里也没有它，段不因它结束（与丢弃的步不同）。它之后的步在学生看到的编号上前移：`loop.step_info` 的 `step`、`ReactPromptContext.step` 与 `policy_step`、学生历史里那一步副本的 `step` 都减去这条轨迹在它之前消失的步数，上下文读起来就像这一步从没发生过；样本的 `sample_id`、`step`、`render.context_steps` 与 `history_steps` 仍是源步号（来源）。k 还有别的动作时，k 留下，只少了等待。
- **改写 thought。** 受影响的 thought 有两种：(i) 去掉了等待、留下了别的动作的步的 thought；(ii) 一个或多个消失的步之后，同一条轨迹里紧接着的那个留下的步的 thought（中间隔着不可导出的步时不算：段在那里已经断了；分叉 run 的检查点步紧接在源 run 的第 t-1 步之后）。一个 thought 两种都占时只改写一次，两种去掉的动作一起交给模型。改写由导出器在 YAML 里配的辅助 chat 模型做：`thought_rewrite_llms`（经 `__wiring__` 接 `llm` 注册表；与 `prompt_contribution` 的 `helper_llms` 同一形状，至多一个），它与 `thought_rewrite_cache` 在 `wait_actions: drop_when_capped` 且 `thought_source: teacher` 时必填，没写就在组装时拒绝；`keep` 或 `thought_source: none`（没有 thought 可改）时写了也拒绝，配置里不留一个从不调用的模型。提示词是代码里的版本化文本（`REWRITE_PROMPT_VERSION` 与它的内容摘要 `REWRITE_PROMPT_DIGEST`）：只去掉提到等待、或页面还在加载、还没加载出来的话，其余逐字保留，不改写、不总结、不添加；没有这样的话就原样返回。模型只看到 thought 与去掉的动作（名字与参数），从不读写 trace。回答原样返回的记为 `reviewed_unchanged`（不是合成）；不同的记为 `rewritten`、`synthetic: true`；改成空的，样本没有 thought（与丢掉 index 记号同样的写法），仍记为 `rewritten`。改写在学生渲染之前按轨迹做，`thought_index_refs: drop` 之后照常检查改写后的文字。
- **来源。** 每个受影响的样本带 `wait_removal`（"可导出的步与 `CanonicalStepSample`"）：本步去掉的动作、它前面消失的步的动作（源 run、步、动作序号与动作本身）与 thought 的改写（原文、改写后的文字、`outcome`、`synthetic`、模型身份 `model_type` / `provider` / `model`、提示版本与摘要、缓存键）。数据集 manifest 的 `wait_actions` 汇总：`policy`；`removed_steps`（消失的步：源的相对路径、`run_id`、`step`、去掉的动作序号）；`removed_actions`（留下的步里去掉的动作：源、`run_id`、`step`、`action_index`）；`rewritten` 与 `reviewed_unchanged`（样本数）；`rewriter`（模型身份、提示版本与摘要；没有改写时为 `null`）。去掉的步与动作按 run 只记一次（一个源 run 的前缀可能出现在好几个分叉的轨迹里），改写只数作为样本导出的步。
- **确定性与缓存。** 模型调用不确定，导出又必须确定，所以改写存进内容寻址的缓存，位置是 YAML 字段 `thought_rewrite_cache`（相对工作目录，`~` 展开；路径旋钮不进 manifest）。键是 {原 thought、本步去掉的动作、前面消失的步的动作（名字与参数）、模型身份、提示摘要} 的规范 JSON 的 sha256；文件是 `<缓存>/<键>.json`，写到临时文件再原子改名，里面记下键的每一项输入与改写结果。读到的条目与请求的输入逐项核对，读不回来或对不上是 `ThoughtRewriteCacheError`（点名文件；删掉它重新导出），从不将就。命中的不调模型；没命中的调一次、立即写进缓存，所以导出后面的步骤失败也不白调。一次导出里同一个键只调一次。**命中与未命中不进数据集**：它们是这一次调用的事实，与墙钟同类；写进 manifest 会让第一次导出（未命中）与第二次（命中）的数据集不同，度量链的导出确定性（§4.16，两次导出比 `tree_digest`）就永远通不过。它们在 `ExportReport.thought_rewrites`（`cache_hits`、`model_calls`）里，CLI 打印。数据集的内容与改写来自缓存还是新调用无关。
- **学生一侧。** 去掉等待之后的目标照常翻译、照常由 `format_react_output` 写、照常 round-trip 与校验；`keep_as_text` 的文本轮写的是去掉之后的动作（`format_action_fact`），学生历史里那一步的副本动作与结果一一对应，所以下一步的执行反馈与目标一致。**录下的观测不改**：学生协商了 `capture_state` 时（`show_capture_state`，§4.4），去掉了等待的那一步的观测轮照样写着截断（`Settle: capped ... still pending: load`），学生学到的是在这样的采集上照常行动；导出只去掉动作、改写 thought，从不改写观测，不想让学生看到这一项就用 `show_capture_state: false` 的学生（它同时属于导出身份）。
- **模型的生命周期。** 导出拥有这个模型：渲染阶段结束时（成功、失败或取消）`close_models` 关掉它，清理的问题只进日志。

#### 代码位置

`spectragent/export/`：`_registrar.py`（`Exporter`、`ExporterRegistrar`、`ExporterBase`）、`settings.py`、`openai_messages.py`、`translate/`（`_registrar.py`、`identity.py`、`index_to_coordinate.py`）、`canonical.py`、`waits.py`（等待动作：截断的判定、去掉等待、thought 改写与缓存）、`filters.py`、`split.py`、`benchmarks.py`、`render.py`、`validate.py`、`runner.py`、`cli.py`、`errors.py`；`schemas/sample.py`；`schemas/dataset.py`（`DatasetManifest`，与度量共用）；`schemas/observation/geometry.py`（`to_grid`、`quantum`、`PointFrame`）；`core/loops/coordinate_frames.py`（`step_frame`、`actions_in_model_frame`、`actions_in_capability_units`、`fact_in_model_frame`）；`core/loops/errors.py`（`UnrepresentableActionError`）；`core/loops/_loop_fragments.py`（`loop_fragment_builders`、`pinned_step_info`）；`core/loops/restore/source.py`（`recorded_observation`、`recorded_step_info`：与分叉共用的读法）；`core/actions/params.py`（`point_params`）；`core/actions/_registrar.py`（`is_wait_action`）；`schemas/observation/capture_state.py`（`CaptureStateFragment`）；`core/runtime.py`（`seed_messages`）；`core/trace/sources.py`、`core/trace/secret_patterns.py`；`core/llm/lifecycle.py`（`close_models`）；browser：`index_points.py`、`capture_check.py`、`dom/cleaning/skyvern_hit_test.py`、`replay/replay_log.py::document_miss_total`。

### 4.16 度量闸门（M33）

度量是离线读已提交数据的消费者，地位与 evaluator、训练导出相同：只经 `RunTrace` 的访问器读 trace（只读已提交的部分），只读数据集目录里的文件；不 setup 任何环境、不调任何 LLM、不碰任何运行中的状态，产物是一份报告。它算出数据线的每一项度量，交给 M33 决定的每个问题各成一节，每一节以一条带证据的建议收尾。包是 `spectragent/flywheel/`，与 `evaluator/`、`export/` 同级：`metrics.py`（度量）、`report.py`（报告模型、建议规则、Markdown）、`chain.py`（链）、`cli.py`（`spectragent flywheel`）。**不是注册表**：v1 只有一份报告；browser 证据的格式知识在 browser 的包里，度量只组织调用（与 `index_points.py` 同一先例）。第二条数据线（Docker、Android）出现时，"每个 capability 贡献哪些度量"再成为一根轴（§6 与 AGENTS.md §2.2 同时加行）。

#### 链（`spectragent flywheel run --spec <chain.yaml>`）

链是现有入口的顺序调用，不是新的运行路径：每一段在进程内调用该 CLI 的 `main(argv)`（`spectragent.core.benchmark_runner.main`、`spectragent.export.cli.main`），最后算报告。链规格是一份普通 YAML（pydantic `ChainSpec`，`extra="forbid"`）。它不是 conscribe 的运行根，所以不放在 `configs/*.yaml` / `configs/flywheel/*.yaml` 这一层（那里每个文件都要能组装并 prepare，`test_shipped_config_integration.py`）：

```yaml
name: first-live
root: traces/chains/first-live       # 一切产物都在它下面（traces/ 被 git 忽略）
evidence: live                       # live | rehearsal：报告里每条建议的证据基础
tasks: {benchmark: online_mind2web, subset: All, task_ids: [...], limit: null}   # 交给 record / control 段的 CLI 参数
token_limit: 2000000                 # 所有运行段合计的 token 上限；默认 unlimited（不限）
record:    {config: <录制 YAML>, timeout: 600, evaluate: false}   # evaluate: false → --no-eval
control:   {config: <同档位、archive: [] 的录制 YAML>}              # 可选：归档开销的对照组
same_path: {config: configs/flywheel/same_path_ui_tars_archive.yaml}
branch:    {config: <分叉 YAML>}
export:    {config: <导出 YAML>, repeat: 2}                           # 2：导出确定性；1：不测
report:    {planted: [{label: teacher api key, env: OPENAI_API_KEY}]}   # 演练写 value；live 只写环境变量名
```

- **布局**（链的约定，报告只认它，`metrics.ChainLayout`）：`<root>/traces/{record,control,same_path,branch}/`、`<root>/datasets/{first,second}/`、`<root>/report/{report.json,report.md}`、`<root>/configs/<段>.yaml`（每段实际用的配置；导出两份：`export_first.yaml` / `export_second.yaml`）、`<root>/evals/<段>/`（`evaluate: true` 的段的 `--evals-dir`；判定也照常写进每个 trace 的 `evals/`）、`<root>/chain.json`（每段的 argv、退出码、产生的 run 目录与起止时刻；`token_limit` 是数字时，还有每个运行段新 run 的 token 用量、链根下全部 run 的用量与 token 上限的停止记录，见下面"token 上限"；`--stages` 再跑时与已有的段合并；只是来历记录，报告不依赖它）。
- **路径从哪里算**：规格里的相对路径（`root`、每段的 `config`）相对工作目录，与每个 af CLI 读路径的方式一样（阶段 YAML 自己的相对路径，例如 `matcher.site_rules: configs/replay_rules`，也相对工作目录）；`root` 解析成绝对路径后，链写进阶段 YAML 的路径都是绝对的。
- **链只改路径**：每段的 YAML 原样读入，链只写这几个字段再落到 `<root>/configs/<段>.yaml`：唯一的 `trace_writer` 的 `trace_dir`（`<root>/traces/<段>`）；`same_path` / `branch` 唯一的 `trace_branches` 加载器的 `data_dir`（`<root>/traces/record`）；导出段唯一的导出器的 `sources_root`（`<root>/traces`）、`sources`（`["record/*", "branch/*"]`）与 `output_dir`（`<root>/datasets/first`，第二次 `second`）。缺这些块（或多于一个）在任何一段开跑之前报错、点名段与缺的块。其余一切（agent、档位、归档器、选步、分支数、`verify`、评测器）都写在段的 YAML 里；对照组的 YAML 自己写 `archive: []`。
- **段的成败**：`spectragent bench` 退出码 0 或 1（个别任务的基础设施失败是要度量的数据）且这一段写出了至少一个新的 run 目录，算跑完；退出码 4（这一段的 sweep 达到了链交给它的 token 额度）且写出了新 run，这一段算跑完、链在这里停下（下面"token 上限"）；别的退出码（2：它的 pre-flight 拒绝了这一段的配置，一个任务也没跑；3：一个 guard 否决了这一段，被否决那一段的 run 不喂给下一段；§4.2）、`SystemExit`、异常、一个新 run 目录都没有，是这一段失败：链停下（`ChainStageError` 点名这一段），之前的产物留着。导出段退出码非 0 就失败（导出什么也没写）；报告段抛出的异常（缺陷、报告目录写不了；坏 run 从不让它抛，见下）同样是这一段失败（`ChainStageError` 点名 `report`，`chain.json` 记下）。选了导出段而 `<root>/datasets/<first|second>` 已经存在，是在任何一段开跑之前的 `ChainConfigError`（导出从不覆盖，要重导先删掉；不让前面花钱的段为一次必然被拒的导出白跑）。`--stages` 只跑列出的段（按规格的顺序；未知的段名、规格里没配的段在任何一段开跑之前报 `ChainConfigError`），已经跑完的段可以不重跑；重跑一个运行段会在已有的 run 旁边再加 run（记 WARNING）。
- **规格**：`ChainSpec{name, root, evidence, tasks{benchmark, subset, task_ids, limit}, token_limit, record, control?, same_path?, branch?, export?, report{planted}}`，每段 `StageSpec{config, timeout, evaluate}`（`same_path` / `branch` 写 `evaluate: true` 被拒：`trace_branches` 没有评测加载器），导出段 `ExportStageSpec{config, repeat: 1|2}`，植入值 `PlantedSecret{label, value | env}`（恰好一个），`token_limit` 是至少为 1 的整数或 `unlimited`（默认）。CLI：`spectragent flywheel run --spec X [--stages a,b]`、`report --spec X [--out D]`、`report --root R --evidence live|rehearsal [--name N] [--out D]`（没有规格就没有植入值，只扫凭证形状，也不知道 token 上限）；退出码 0 完成、1 某段失败（`report` 算不出报告——发布的数据集读不回来——或写不了——`--out` 建不了、写不进——也是 1，报错点名是哪一个，不抛 traceback）、2 规格或参数不对、4 链在 token 上限处停下（`run`；选了报告段时报告照样算出，与 `spectragent bench` 的 4 同一个意思）。测试的接缝是 `run_chain(spec, bench=..., export=...)`（替换两个进程内的 `main`）。
- **token 上限**（用户 2026-10-14 的裁决）：`token_limit` 是链的所有运行段合计能用的 token，数的是 trace 里记下的用量——每个 run 的每次调用记录里 provider 报的数（`core/trace/usage.py::recorded_usage`：每个已提交步的调用、再是 trailing 调用；成功、失败与取消都读 `RecordedCallMetrics.usage`，失败的调用照样计费；一次调用的 token 数与步记录同一条规则 `TokenUsage.from_llm_usage`：provider 的 `total_tokens`，没给总数时 prompt 加 completion）。从独立 `call_metrics` 账读取，不需要还原完整提示或图片；记录计划没选 metrics 时用量为未测量。provider 没报用量的调用（被取消的、回复之前就失败的、从不报用量的 provider 的）计入调用数、不加 token，按 `traces/<角色>/<run>: step S call K (状态)` 点名，从不当作 0；调用记录读不回来的 run 不计，点名原因。
  - 有上限时，链开跑之前先数一遍链根下已有的每个 run（四个角色，`metrics.role_token_use`），所以分段跑（`--stages`）时前几次花掉的照样算，重跑一段加的 run 也算。每个运行段把剩下的额度（上限减已用）交给 `spectragent bench --token-limit`（§4.2 "sweep 的 token 上限"：每个任务跑完就加一次，达到额度时那个任务照常跑完、之后的任务不开始、退出码 4）；段跑完后链数这一段新写的 run（`chain.json` 的 `stages.<段>.token_use{runs, tokens, calls, calls_without_usage, skipped}`），加进总数（这一段失败了也加，来历照记）。
  - 这一段的 bench 以退出码 4 结束，或总数达到上限（大于或等于）：链停下，后面的运行段与导出段都不开始；报告段（选了的话）照样用已有的数据算；`chain.json` 写 `stopped_by_token_limit{stage, tokens_used, token_limit, stages_not_started, calls_without_usage}`（顶层另有 `token_limit` 与当时的 `tokens_used`）；`spectragent flywheel run` 打印停在哪里、退出码 4。开跑之前已有的 run 就已经达到上限（例如上一次在上限处停下，规格里的上限没有调高）：选中的运行段与导出段一个也不开始（`stage: null`），报告照跑；只选了报告段时没有要停的段，不算停下。要接着跑，先调高 `token_limit`，再用 `--stages` 只跑没开始的段。
  - `unlimited`：链不数用量，不给 bench 传 `--token-limit`，各段的 argv 与 `chain.json` 的各段记录和以前一样（顶层只多一个 `token_limit: unlimited`）。
  - 报告自己从 trace 数（不读 `chain.json`）：`token_use{limit, tokens, limit_reached, reason, roles}`，`roles` 每个角色一个 `TokenUse`；上限来自规格（链的报告段与 `report --spec`），`report --root` 没有规格时 `limit` 与 `limit_reached` 是 `null`，`reason` 写明。`limit_reached` 为真时 `report.md` 开头写明链因 token 上限停下：上限一到就不再开始任何段，下面的报告只覆盖已有的 run 与数据集。
  - 真跑的规格写具体的数（2026-10-14 的 12 站 live run：`token_limit: 2000000`）；演练的规格也写一个数（远高于脚本化模型的用量：每次调用报 15 token），让真实的 `spectragent bench` 走一遍有上限的路径。
- **原路径回放是一个分叉段**：`configs/flywheel/same_path_ui_tars_archive.yaml`：`branch` loop，选步 `last`（每个源 run 最后一个可分叉的策略步，§4.10），`continue_steps: 0`，`verify: warn`（发散了照样走完，每一步都有校验结果），`branches_per_step: 2`（回放确定性的重复），`agent_outcome: any`，`on_unbranchable: skip`。它取代 M30 `tests/_helpers/replay_driver.py`"把录下的模型输出喂回同一个 agent"作为度量手段：恢复器本来就逐步重放录下的动作、逐步校验观测（§4.14 "恢复"），只有最后一步要 teacher 再决定一次（每个分支一次 LLM 调用）。第一次 live run（真实任务上只跑录制、原路径回放与报告）就是这条链只选这几段的跑法。
- `spectragent bench` 不读 `.env`，链也不读：跑之前在 shell 里导出密钥（`set -a; . ./.env; set +a`）。
- **链的导出 YAML** 用 `filters.require_eval: null`（链默认不评测，没有判定可筛）与 `decontamination: {against: []}`（源 run 本来就是被测 benchmark 的任务）：这两个现在就是导出的默认值，也是发布的 `export_sft.yaml` 的写法；`max_document_misses` 与 `exclude_restoration_diverged` 照常，被它们筛掉的分叉正是要计数的产出损失。

#### 度量（`metrics.py`）

每个数都带分母（`Ratio{numerator, denominator, value, reason}`）；分母为 0 时值是 `null` 加原因，从不是 0："没测到"不能变成"测到了 0"（`evaluator/summary.py` 的同一条规则）；一组空值的 `percentile` 直接报错，不返回 0。读不回来的 trace 跳过并按角色记原因（`core/trace/sources.py::unreadable_reason`，导出用的同一个函数：只写相对 run 目录的文件名与错误类），不让一个坏 trace 拖垮整份报告；某个 run 的证据读不回来（环境证据的坏行、WARC 记录对不上，提交了的截图缺失或为空，回放日志读不回来）只在读它的那几项里跳过并记原因（该项的 `skipped`，键是 run 相对角色目录的路径，跨角色的项用 `traces/<角色>/<run>`：例如截图读不回来的 run 只离开原路径保真的严格版与回放确定性，恢复器的判定照常计入），`report.md` 也逐条列出。录制的 `result.json` 读不回来（解析不了，或读不了：权限、I/O）记作 `(unreadable result)`，与从没写过的 `(no result)` 分开，不让报告崩。每项建议所依据的度量另记它的数据来自几个不同的站点（`sites`：任务 `start_url` 的主机，与下面"站点"一节同一个键；只数真正贡献了数据的 run，跳过的不算），建议的广度规则（下面"建议槽"）读它。

**空分叉**：分叉 run 从检查点起一个动作也没执行（`metrics.empty_branch`）——teacher 的模型调用失败（`model_call_failed`）、它的回复没有动作（`no_action`：解析失败与解析出零个动作在记录里一模一样：没有动作、没有 `error_phase`）、或者 teacher 决定之前 run 就结束了（`ended_before_decision`：恢复把它结束了，或检查点步的观测、prompt 失败）——说明不了 teacher 的选择会走到哪里。它不进分叉结果覆盖的占比与 miss 计数、不进按站点的分叉占比与记录覆盖决定的输入，按原因单独计数（`empty`）；重复分叉率不比较检查点步没有动作的分叉（`not_compared`：导出只给样本标 `duplicate_of`，没有动作的步不是样本）。用户 2026-10-13 的裁决：否则 teacher 路由越不稳，"`strict` 够用"的占比反而越高。

| 度量 | 读什么 | 定义 |
|---|---|---|
| 归档开销 | `record` 与 `control` 的 `StepRecord`；`record` 的 env/ extra 索引与 `index.jsonl` | **延迟**：策略步，沿用该报告从第 1 步起的比较窗口（第 0 步不再含 setup；setup 只计入 Runtime 的 execution_ms），每步环境侧执行时间 = `duration_ms − (llm_latency_ms 或 0)`，不把单独记录的 `recording_ms` 当成环境执行耗时（`duration_ms` 为 `None` 的步不计）；两组各自的 p50 / p95（最近秩的 `percentile`），开销 = 开 − 关。两组的档位（`TraceConfig.resolved` 里 capability 的 `replayability`）不同、或任一组为空时为 `null`。live 的两组走的不是同一条路径，比的是分布。**字节**：每个录制 run 的 env/ 去重前后（`RunTrace.committed_files` 里带 `archive` 的条目）——去重后 = 每个不同路径各计一次的 `size`；去重前 = 每一步（与收尾）的 extra 索引列出的每个 env 文件各计一次（内容寻址的文件被每个引用它的步各计一次），再加 revisit 记录替掉的完整记录长度（browser 的 `revisit_saving`）；按步数平均。按实际索引计账：`before == after + saved_by_revisit + saved_by_content_addressing`。revisit 只引用同一收集批次；很小的 body 可能因 revisit 头更大而产生负的净节省，照实报告，不截为零。**观测前的落定等待**（`settle_wait`，每个跑过的角色一条，按阶段顺序）：环境侧时间里有多少是观测之前等页面落定（§4.14），从每一步每个 browser fragment 都带的 `settle` 记录读（`settle.py::recorded_settle`：读该步记录了的第一个 browser fragment，`browser_meta`、截图、DOM 依次）；恢复步与终止观测也算，它们也是采集。每个角色给 p50 / p95（同一个 `percentile`）、上限截断的占比（分母是带记录的观测，没有记录时为 `null` 并写明原因）、读不到页面状态的次数、站点数与读不回来的 run。只是陈列，不参与规则 |
| body 完整率 | `record` 的 `index.jsonl`（`iter_index_lines`） | 每个 `record_id` 的最终状态（最后一行；`streamed_later` 看有没有它的 `kind: body` 行）：有 body（`ok`、流完 body 的 `streamed_later`）、不需要 body（`redirect`、`websocket`）、明确没有 body（`too_large`、`incomplete`；最后一行是 `pending` 的按 `incomplete` 读，§4.14）。完整率 = 有 body 或状态明确的记录 / 全部记录；另报有 body 率（分母去掉不需要 body 的）与按状态的分布（browser 的 `body_completeness`）。"服务器发了、归档里没有"只有 fixture 的访问日志能判：那是演练测试的断言，不是报告的度量 |
| 原路径回放保真（按站点） | `same_path` 的 `StepRecord.restoration`；源 run（`TraceConfig.branch.source_trace`，相对 `<root>/traces/record`）的截图 | 被校验的每一步（恢复步与检查点步）：恢复器判定 `ok` 的占比（URL、元素签名多重集、截图 dHash ≤ 阈值、无文档 miss，§4.14 "恢复"）；严格版：URL 相等（读恢复器的同一份 URL 证据 `url_match`，所以页面地址规则同样生效）且截图 sha256 与源 run 同一步相同的占比；每个 run 第一个不通过的步与原因；全部通过的 run 占比；`errored_runs`：没有干净结束的 run 数（两本账任一：trace 不是 `complete`，或 agent 以 error / timeout / vetoed 结束、没有读得回的 `result.json`）；媒体（2026-10-20 裁决）：真正比较了媒体状态的步占被校验步的比例（`media_coverage`，`comparisons` 不为 `None` 才算）、原因里有 `media_mismatch` 的步数（`media_mismatches`）与对齐结果按 `outcome` 的计数（`media_alignment`），总体与按站点都给；媒体区域规则声明的页面上被校验的步数（`media_area_steps`，2026-10-23：这些步不判媒体、截图距离遮掉了媒体框，判定依赖声明，重新归类不是保真提高） |
| 恢复校验率 | `branch` 的 `StepRecord.restoration` | 通过的校验 / 全部校验；不通过的按 `RestorationCheck.reasons` 计（一条多个原因各计一次）；重定位失败按 `not_found` / `ambiguous` 与 `RetargetMethod` 计 |
| 分叉结果覆盖 | `branch` 的回放日志（`iter_decisions`）、`TraceConfig.branch.checkpoint_step`、各步执行的动作 | 在动了手的分叉里（空分叉单独计数，见上），检查点步及之后（策略步、终止步、收尾）一个主 frame 文档 miss（回放自己的 `diverged`）都没有的分叉占比；同一范围里其余的 miss（子 frame 文档、beacon、子资源；折叠的按它代表的条数计）按动作类型（miss 所在步执行的动作名；终止步记为 `terminal`、收尾的记为 `trailing`——页面加载完之后才发出的请求落在取走它的那次收集，§4.14 "步归因"）、`resourceType`、host、原因与请求类别分布。检查点之前的 miss 属于恢复，由恢复校验率的 `document_miss` 体现，不算这一项 |
| 回放确定性 | `same_path` 与 `branch` 的终止步、检查点步的截图 | 同一输入 = 同一源 run、同一检查点、策略步执行的动作逐个相同（名字与参数的规范 JSON）；组内两两比较终止截图（`origin == "terminal"`）的 sha256，报可比的对数与相同的对数（没有终止步的 run 不比、计数）；另报同一检查点所有分支的检查点步截图（恢复出的状态，与 teacher 无关）是否相同 |
| 导出确定性 | `datasets/first`、`datasets/second` | 两棵目录树的摘要（相对路径与每个文件的 sha256，按路径排序；`core/trace/digest.py::tree_digest`）相同；`repeat: 1` 时为 `null` |
| 隐私 | 只看两道门（威胁模型"只有我"，§5.6；用户 2026-10-13 裁决）：**发给模型的**——每个角色每个读得回的 run 录下的每一次请求（`RunTrace.llm_messages` 与 `trailing_calls`，`core/trace/usage.py::recorded_calls`，token 上限数用量读的是同一份），搜它的 messages 里的每一段文字（`metrics.request_texts`：内联图片的 base64 `data:` URL 不是文字，不搜，base64 的字母表里也拼不出任何凭证形状；按 URL 发的图片照搜）；**导出的数据集**——`<root>/datasets` 下的每个文件（gzip 解开，`core/trace/secret_patterns.py::iter_readable`；导出的产物就是它的文件）。`env/` 与 trace 的其余文件（步记录、观测、配置）留在录制它的机器上，不搜：站点页面里自带的公开 key（例如 Google Maps 的浏览器 key，在 WARC 与回放日志里）因此不算泄漏 | 按门分开计数（`model_requests` / `datasets`，各一个 `PrivacyGate{items, planted, shapes, shape_items}`）：每个植入的凭证出现的次数（必须是 0）与含有它的请求数 / 文件数；`SECRET_PATTERNS` 每个形状的命中数与含有它的请求数 / 文件数；另记读到的 run 数与站点数（建议的站点数）与 `skipped`（读不回来的 trace、某次录下的请求读不回来的 run：整个 run 不计，`traces/<角色>/<run>` 加原因）。植入值来自链规格：演练写字面量，live 只写环境变量名、值从进程环境读；报告只写标签与次数，从不写值。"没有泄漏"要每个植入值都搜过：有一个没搜（环境变量没设、`report --root` 没有规格也就没有植入值），建议是 `insufficient`、没有结论；搜到的植入值或凭证形状照样是"泄漏"的结论，并点名是哪一道门；有 run 没读到时结论后面写明几个 |

**检查证据与精确复现分开。** 原路径报告同时给出：已有恢复判据通过的步数、全部检查通过的 run 数、截图字节完全相等的比例、URL 已知且相等并且截图字节完全相等的比例，以及 URL / 元素签名 / 媒体状态的证据覆盖率。后一个精确比例仅比较两边截图都可读、且有 URL 比较证据的步；无 URL 证据的步不进它的分母，分母为零是未测到。图像相同但 URL 未知可以计入纯截图相同率，不能计入 URL 与截图都相同。原路径的边界仍是最后可分叉检查点，不能把该 run 的 `done` 写成源末动作后的业务成功。

`runs_reaching_checkpoint` 必须具有分叉检查点的来源信息，且第 0 步至检查点步每一步都有恢复检查；`runs_passing` 还要求这些检查全部通过。中途观察失败、只留下全部通过的短前缀，不能算完整路径通过，报告列出首个缺检查的步。到检查点之后的模型失败另计 `errored_runs`，不抹掉已经证明的状态恢复；任务正确性仍独立判断。

**动态区域首轮只诊断（用户 2026-10-14 裁决）。** 离线读取已有录制与重复回放的对应截图，显示原图、差异与候选变化区域，区分录回差异和重复回放之间的变化。保留原图及全图判据，不额外驱动观测，不改 `RestorationCheck.verdict`、strict 分叉放行或站点排除；有变化的区域不自动认定为与任务无关，不制作可直接用于放行的遮罩。任务关键内容的保护范围和遮罩验收留待后续设计。2026-10-23 的媒体区域（§4.14 "页面上的媒体区域"）是另一回事：它是按站点声明、随回放规则集冻结的实验输入，遮的是媒体事实测到的框，不是这里找出的变化区域；诊断照旧只给整图 dHash、不出遮罩，嵌入的录制时校验（`recorded_check`）带着它自己的 `media_areas` 证据。

`spectragent flywheel diagnose --recordings DIR --replays DIR --out NEW_DIR` 是这项诊断的产品入口（`flywheel/diagnostics.py`）。两侧目录经现有 `open_role` / `RunTrace` 读取，回放通过 `TraceConfig.branch.source_trace` 对应录制，只比较检查点及以前的源策略步，不把分叉后的新动作或终止观测冒充原路径。它不启动浏览器或模型，不读取原始 `env/`，不改源 trace；输出目录必须尚不存在且不能放进输入目录。输出 `diagnostics.json`（格式 `af-replay-diagnostics-1`）、可直接离线打开的 `index.html` 与生成器命名的 `assets/`：原截图字节按 SHA 去重保留、PNG 预览、录回像素差图和区域框、同源同步骤重复回放的候选区域。HTML 中的任务、路径、URL 和错误文本全部转义，不加载外部资源。

诊断方法明确写入 JSON 和 HTML：RGB 每像素最大通道差大于 `pixel_threshold`（CLI 默认 16，范围 0–255）才算变化；按 `grid_size`（默认 32，正整数）分格，格内变化像素比例至少 `cell_fraction`（默认 0.01，范围大于 0 至 1）即标记；用四邻接合并成区域框。尺寸不等不缩放比较，记为不可比较；原始全图 SHA、dHash（与恢复测量共用算法）与录制时已有的 URL 比较证据保留，缺图、坏图、缺步、来源无法匹配、trace 读不回等均逐项列原因。相同源步骤至少有两个尺寸一致、可读的回放截图，才可由其两两差异的并集得到“重复回放候选变化区域”；只有一次回放明确写证据不足。候选可能是业务文字、错误提示或配置差异，不等于动态装饰、不等于可忽略；输出不含可用于 strict 放行的遮罩，也不输出遮盖后提高的保真率。相同输入和参数重复诊断产物逐字节相同，诊断的各类可比较数与不可比较数在摘要中分开。

#### 各节与建议（`report.py`）

- **站点**：每个站点（任务 `start_url` 的主机，`core/trace/sources.py::task_identity(config).site`，与导出 `split.by: site` 和 `exclude_sites` 用的是同一个键；分叉 run 用它的源任务）的任务数、录制的两本账（`record_status`：trace 的状态 `complete` / `incomplete` / `writing`；`record_outcome`：agent 的 `agent_done`，没写 `result.json` 是 `(no result)`，读不回来是 `(unreadable result)`）、原路径保真、恢复校验率、分叉覆盖（空分叉不计）、miss 数、回放改写了 JSONP 回调名的应答数（`callback_rewrites`：原路径回放与分叉合计，从回放日志的 `callback_rewrite` 数，折叠的条目按它代表的条数计，§4.14 "JSONP 回调名"）、回放用正文相同的记录代答的次数（`substitutions`：按源结局分开计数，页面取消的与 recorder 没存下的各自可见；原路径回放与分叉合计，从回放日志的 `substituted` 条目数，折叠的按条数计；不算作命中，§4.14 "匹配"）与序列用尽之后 `repeat_last` 重复送出的次数（`reuses`：回放日志的 `exhausted` 条目数，同样合计、折叠的按条数计，也不算作命中；用户 2026-10-20 的裁决：与代答次数并列）；回放日志读不回来的 run 不进 miss 数、改写数、代答数、复用数与分叉占比，记在 `sections.sites.skipped`。**剔除名单**：被拦截（下一条）、或原路径保真低于 `SITE_MIN_FIDELITY`（0.8）、或录制的环境在第 0 步就失败（观测或动作失败，即循环以 `env_status: failed` 结束的那两个阶段；或一步都没提交）的站点；模型调用失败（`error_phase: llm`：429、超时、被拒的 schema）是路由的失败，从不剔除站点，按站点单独计数（`record_model_failures`）；输出成 `ExportFilters.exclude_sites` 的形状（主机名，后缀匹配）并附 YAML 片段。链不自动套用（导出在报告之前）：人把它放进导出配置再导一次。
- **anti-bot / Cloudflare**：录制里文档请求（`resourceType: Document`，主 frame 与子 frame 都算，验证码 iframe 也是拦截）的应答带拦截特征（browser 的 `challenge_documents`：状态 403 / 429 / 503，且有 `cf-mitigated: challenge` 头、或 `server: cloudflare` 加 `cf-ray`）的站点、run 与步；建议：剔除。只认应答头，不带这些头的拦截页面认不出来（写明的残余）。
- **重复分叉率**：同一 `branch_group_id` 里检查点步执行的动作（规范 JSON）与排在前面的分支相同的分叉，占参与比较的分叉的比例（与导出的 `duplicate_of` 同一规则：检查点步没有动作的分叉不比较，计在 `not_compared`——三次失败的 teacher 调用不是两个重复），并对照数据集 manifest 的 `duplicates`。teacher 的采样够不够分散就看它；它高时再考虑每个分支不同的提示（§4.1 "teacher 采样"）。
- **POST 分叉的 miss**：分叉 run 检查点及之后方法为 POST 的 miss（文档、子资源分开），有 POST miss 的分叉占比。
- **存储增长与压缩评估的输入**：按角色、按类别的字节（`observations:<fragment>` 的 json、`artifacts:<fragment>`（截图）、`messages`（调用记录与 output schema）、`messages:images`（prompt 里内联的图片，每个 run 每张只存一份，§5.3）、`steps`（步记录与 extra 索引）、`env:<归档器>`、`env:blobs`（内容寻址）、`env:run`（capability 的 run 记录）、`run`（manifest / config / result）），每个 run、每步的平均；文件是 `RunTrace.committed_files` 列出的那些（run 自己的三个文件、manifest 与 extra 索引列出的；不按目录树走，路径的形状与"留在 run 目录里"由 RunTrace 检查，§5.5），每个路径计一次，字节经 `read_committed` 读，类别按条目的 `kind` 与 `archive` 定（`metrics.storage_category`）；还没压缩的字节用 gzip（level 6）实测压缩比；按文件开头的字节认出已经压缩的（gzip、PNG、JPEG、GIF、WebP：截图、WARC、`dom_raw`），只报大小；数据集目录按文件树的字节。
- **保留期与容量**：每个角色每个 run 的平均字节、数据集每个样本的字节；每个任务的字节 = 一次录制 + 其余每个角色"每次录制有几个 run × 每个 run 的字节" + 数据集每个任务的字节；按 `RETENTION_PROJECTION_TASKS`（10、20、300：第一次 live run、完整的 live 链、整个 `All` 子集）投影。
- **导出产出**：每个切分的样本数、按原因的丢弃与跳过（数据集 manifest，经导出写它用的同一个 `DatasetManifest` 读），`labels.step_status == action_failed` 的样本占比（导出该留还是该丢它们；每行 `<split>.samples.jsonl` 的样本经导出 dump 它用的同一个 `CanonicalStepSample` 读，`metrics.dataset_samples`）。读不回来的 manifest 或样本行是点名文件（与行号）的 `DatasetFormatError`，不读成 0。
- **待决的决定**：`miss_policy`（`strict` 够不够用；"miss 时转去真实网络"不是 miss 策略，而是一个新的网络来源，§4.14）看分叉覆盖、子资源 miss 的分布与 POST miss（`STRICT_MIN_IN_ARCHIVE_SHARE` 0.9：动了手的分叉里零文档 miss 的占比不低于它就留 `strict`，空分叉不进分母）；`recording_coverage`（录制时扩大覆盖：预取或探索）看动了手的分叉在检查点之后的文档 miss 里 GET 到未录 URL（`no_match`）的占比（`EXPAND_COVERAGE_MIN_UNRECORDED_SHARE` 0.5；没有文档 miss 时结论是"不扩大"）；`site_rules`（给哪些端点写站点规则：只提给人审，从不自动生成）看所有回放 run（`same_path` 与 `branch`）的近邻 miss（browser 的 `near_misses` / `site_rule_candidates`：最佳候选（匹配器排第一的那个）同 host、同 path、同方法，而查询参数或 body 字段不同的 `no_match`；按站点（回放 run 的任务站点）与端点（host、方法、路径）汇总（计数、排除与排序三条是用户 2026-10-23 的裁决），**计的是回放 run，不是请求**：一个 run 对一个端点、一个字段、一种一起变化的字段组合各只计一次，每秒都发的遥测因此不再靠重复压过只发一次的内容请求，请求数 `requests` 只作参考；查询参数与 body 字段（拍平后的路径，即 `ignore_body_paths` 写的名字）分开计，并列出每一种一起变化的字段组合（`field_sets`：总是一起出现的字段是一条规则要一起忽略的）；**不喂页面的请求按录制本身判断、不按域名表，单独计数**（`not_feeding_page`，按原因给端点数与请求数）：回放用的规则已经对它合成 204（`rule_answers_204`），Chrome 把它标成 `Ping`（`navigator.sendBeacon` 与 `<a ping>`，Chrome 没有单独的 beacon 类型）或 `Image`（`ping_or_image`），或者录下的应答是 204、正文为空（`recorded_empty`），依次判断，取第一条；**排序看它与恢复失败的关联**：每个有恢复校验的步（每个 run 的每一步只计一次）按这一步自己的校验过没过分成 `failed_check_steps` 与 `passed_check_steps`，`failure_association` 是失败占比的 95% Wilson 区间下界（没有校验过的步为 0），站点内按它从高到低，再按 `failed_restoration_runs`（恢复没过、且在第一个没过的步或之前就有这个近邻 miss 的 run 数），再按 run 数，同分按 host、路径、方法；每步都发的请求在恢复通过的步上也 miss，所以排在只在恢复失败的步上 miss 的请求之后。只看"第一个失败步或之前"区分不了：实测 2026-10-23 那批 304 个分叉里 279 个恢复没过，几乎都在第 0、1 步（截图距离、媒体状态），落地页的遥测在每个 run 都排第一，B 站的 `wbi/search/type`（搜索在后面几步）一个也算不上，按步的关联把它排到 B 站第一（58 个有近邻 miss 的校验步里 57 个失败），所以关联按步算，run 级的计数只作次序；两边都按回放自己记下的规则（`recorded_site_rules`）规范化——源 run 用这份规则载入（`load_archive`），活请求的查询用这份规则为它选出的那条，body 用回放日志里的 body 键（服务时已按同一条规则去掉忽略的路径）——所以规则已经忽略的字段不算不同；一边的 body 只剩摘要（规范化本身的 4096 字符上限，或 body 解析不了：匹配器手里也只有摘要；回放日志的 body 键 2026-10-23 起原样保存，不再截短）时只能说 body 不同、说不出哪些字段，记作 `opaque_body`；源 run 不在链里、规则快照或源归档读不回来的回放连同原因记在 `skipped`；每个站点排在最前的 `SITE_RULE_MAX_PROPOSALS_PER_SITE` 3 个端点建议给它写一条限定在这个端点的规则（`ignore_query` / `ignore_body_paths`，取最多 run 一起变化的那组字段，判断里只写前 `SITE_RULE_PROPOSAL_MAX_FIELDS` 4 个、每个截到 `SITE_RULE_PROPOSAL_FIELD_MAX_CHARS` 40 字符：拍平的 body 路径可以有几千字符）供人审，代替原来全局的次数门槛（2 次时 220 个端点里 215 个达标，判断约 19000 字符）：每条建议都要人去读这个端点的应答，确认这些字段不选内容，所以每站一小批；完整的排名留在 `decision_inputs`。只是提示，不证明这些字段不选内容）；`retention` 看上一条的容量数（与 `recommendations.sections.retention` 是同一条建议）。这几个决定的输入（分叉的文档 miss、近邻 miss）单列在报告的 `decision_inputs` 里。

**建议槽**：每个度量、每一节、每个决定一个 `Recommendation{status: supported | partial | insufficient, verdict, rule, evidence: list[EvidenceRef{pointer, value, n}], basis, sites, status_reason}`。`basis` 是链规格的 `evidence`；`sites` 是证据来自的不同站点数。没有数据是 `insufficient`、`verdict` 为空，从不给默认结论；否则只有 `live` 的证据、且来自至少 `SUPPORTED_MIN_SITES`（10）个站点才是 `supported`，不到 10 个站点最多是 `partial`（"insufficient sample"），`rehearsal` 的证据最多是 `partial`（"fixture 只校准度量本身，live run 之后再定"）；低于 `supported` 的原因写在 `status_reason` 里（`supported` 时为 `null`），`report.md` 每条建议下照写。用户 2026-10-13 的裁决：2–3 个任务的 live run 是冒烟测试，不拿来定分界线与决定。`rule` 写出所用的规则与阈值，`evidence` 引用报告里的度量（JSON 指针）与它们的分母。

**阈值**：具名常量，名字是"它约束什么 + `_MIN_` / `_MAX_` + 量"：`SUPPORTED_MIN_SITES` 10、`SITE_MIN_FIDELITY` 0.8、`REPLAY_MIN_FIDELITY` 0.9、`RESTORATION_MIN_RATE` 0.9、`STRICT_MIN_IN_ARCHIVE_SHARE` 0.9、`EXPAND_COVERAGE_MIN_UNRECORDED_SHARE` 0.5、`SITE_RULE_MAX_PROPOSALS_PER_SITE` 3（与它一起的 `SITE_RULE_PROPOSAL_MAX_FIELDS` 4、`SITE_RULE_PROPOSAL_FIELD_MAX_CHARS` 40 只管判断写多长）、`CAPTURE_MIN_BODY_SHARE` 0.95、`REPLAY_MIN_IDENTICAL_SHARE` 1.0、`BRANCH_MAX_DUPLICATE_SHARE` 0.5、`COMPRESS_MAX_GZIP_RATIO` 0.5、`ACTION_FAILED_MAX_KEPT_SHARE` 0.2（`RETENTION_PROJECTION_TASKS` 是投影用的任务数，不是阈值）。它们是 live 之前定的起点，留在 `report.py` 里，直到一次覆盖足够多站点的 live run 校准它们（用户 2026-10-13 的裁决）。每步的归档开销上限不在这里：`ARCHIVE_MAX_P50_OVERHEAD_MS`（300 ms）只定义一次，在 `core/archive/budget.py`，报告的归档开销规则与 browser 的开销基准（§4.14）都读它。

#### 报告

- `report.json`：格式 `af-flywheel-report-12`（pydantic `FlywheelReport{format, chain{name, evidence}, token_use, inputs, metrics, sections, decision_inputs, recommendations{metrics, sections}, decisions{miss_policy, recording_coverage, site_rules, retention}}`；`-2` 起：建议带 `sites` 与 `status_reason`，决定改用语义名，度量带 `sites`、空分叉与 `skipped`；`-3` 起：隐私一项只看两道门，按门分开计数，带 `runs`、`sites` 与 `skipped`；`-4` 起：归档开销带每个角色观测前的落定等待 `settle_wait`；`-5` 起：每个角色记下的 token 用量与链的 token 上限 `token_use`，见上面"token 上限"；`-6` 起：每个站点回放改写了 JSONP 回调名的应答数 `sections.sites.by_site.<站点>.callback_rewrites`；`-7` 起分别给出纯截图字节一致、已测 URL 与截图一致、URL / 元素签名的证据覆盖；`-8` 起：每个站点按源结局的代答次数 `sections.sites.by_site.<站点>.substitutions` 与重复送出的次数 `reuses`；`-9` 起：原路径保真（总体与按站点）带媒体状态真正比较了的步的覆盖 `media_coverage`、原因里有 `media_mismatch` 的步数 `media_mismatches` 与对齐结果按 `outcome` 的计数 `media_alignment`，2026-10-21 裁决；`-10` 起：`decision_inputs.site_rule_candidates` 按端点给出近邻 miss（代替按 host 的 `by_host` / `params`），body 字段不同的 miss 也计入；`-11` 起：按站点与端点、计回放 run（`endpoints[{site, host, method, path, runs, failed_restoration_runs, failed_check_steps, passed_check_steps, failure_association, requests, query, body, opaque_body, field_sets[{query, body, opaque_body, runs}]}]`），另有 `failed_restoration_runs` 与不喂页面的请求 `not_feeding_page{<原因>: {endpoints, requests}}`；`-12` 起：原路径保真（总体与按站点）带媒体区域规则声明的页面上被校验的步数 `media_area_steps`（2026-10-23）；只读当前格式，旧格式的报告重新生成），键排序、缩进 2，**没有墙钟字段、绝对路径或主机信息**（run 写成相对链根的 `traces/<角色>/<run>`），同样的输入算两次逐字节相同；`inputs` 按角色记 run 数、跳过的 trace 与原因、以模型调用失败结束的 run（`model_failures`：路由的失败，不是站点的证据；它们照样计入各项度量，live 计划写了怎么处理），以及 trace 里实际运行的组件（agent、loop、恢复器、cleaner、serializer、backend、网络来源、模型路由；由 `metrics.components(trace)` 读脱敏后的 `TraceConfig.resolved`，没标 trace-safe 的自由字符串读作 `<omitted>`：验证要点名所用的配置），数据集记在不在与格式。每条证据的 `pointer` 都能在 `report.json` 里解析到它的 `value`（测试钉住）。
- `report.md`：同一个模型渲染，标题是 `Token use` / `Metrics` / `Sections` / `Pending decisions`（链在 token 上限处停下时，`Token use` 一节开头写明，见上面"token 上限"），每项度量、每一节、每个决定各占一节，各项跳过的 run 与原因逐条列出，每节末尾是建议、证据、站点数、基础，以及低于 `supported` 的原因。报告里的名字、标题与规则文字不用规划编号（AGENTS.md §3.6；`test_report_schema.py` 钉住）。
- `spectragent flywheel report --spec <chain.yaml>`（或 `--root <dir> --evidence live|rehearsal`）只读、可重复；`run` 的最后一段就是它。

#### 共用而不是复制

- `restoration_rate` / `RestorationRate`、`components` 从 `tests/_helpers/branch_driver.py` 挪进 `flywheel/metrics.py`（M31 的 live 测试改从这里 import，`branch_driver` 里不留同名的函数）；
- 去重的账法（`DedupBytes`）与 `percentile` 从 `tests/_helpers/archive_bench.py` 挪进 `flywheel/metrics.py`，revisit 的那一半是 browser 知识（`archive_stats.revisit_saving`）；M29 的基准与它的 schema 测试改用这两处；
- `tests/_helpers/secret_scan.py` 的 gzip 感知读法挪进 `core/trace/secret_patterns.py`（`readable_text(path)` / `iter_readable(root)`，只 import 标准库），secret guard 与 M29 的隐私测试改从这里 import，测试帮手删掉；
- `TaskIdentity` / `task_identity` 从 `export/filters.py` 挪进 `core/trace/sources.py`（只读 `TraceConfig`；导出与度量共用同一个"站点"与"任务"的定义），导出改从这里 import；读不回来的 run 的措辞 `unreadable_reason` 一起挪过去（报告与数据集 manifest 写同一句）。
- 数据集 manifest 的形状是 `schemas/dataset.py::DatasetManifest` 一处：导出器经它写，度量经它读（`metrics.read_dataset_manifest`），没有哪一边用 `.get(...)` 猜键。
- 目录树摘要是 `core/trace/digest.py::tree_digest` 一处：度量的导出确定性、导出测试的两次导出比较、离线回放与 `RunTrace` 读源 run 时"一个字节也没动"的测试（`metadata=True` 连权限、大小与修改时间一起比）都经它；它只 import 标准库，core 一侧的测试不必 import `spectragent.flywheel`。
- 一个 run 录下了哪些调用、它们报了多少 token，是 `core/trace/usage.py` 一处（`recorded_calls`：每个已提交步的调用、再是 trailing 调用；`recorded_usage`：它们的 token 合计与没报用量的调用，一次调用的数与步记录同一条规则 `TokenUsage.from_llm_usage`）：`spectragent bench` 与链的 token 上限、报告的 `token_use`、隐私一项读的请求都经它（原来的 `metrics.recorded_requests` 删掉）；它只经 `RunTrace` 读，不 import `spectragent.flywheel`。
- 每步的归档开销上限 `ARCHIVE_MAX_P50_OVERHEAD_MS` 在 `core/archive/budget.py` 一处：报告的规则与 browser 的开销基准都读它（基准仍可用 `SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS` 在吵的机器上临时放宽）。
- 去重的账法是 `DedupBytes.account(listed, distinct, saved_by_revisit)` 一处：trace 一侧（`metrics.archive_bytes(run)`：`RunTrace.committed_files` 里每个提交列出的每个归档文件各计一次 = 去重前，每个不同路径一次 = 去重后）与 M29 基准的内存适配（`archive_bench.dedup_bytes(files)`）都经它；`percentile` 保持 M29 的最近秩定义，空输入改成报错。

#### browser 一侧的读法

browser 证据的格式知识留在 browser 的包里：纯函数，只经 `ArchiveReader`（§4.14 "回放的数据通路"）或它读出来的行读，不 import `core.trace` 与 `spectragent.flywheel`，与 `replay_log.py` 是同一条边界（`test_archive_boundaries.py` 的第 5 条规则）。`replay/archive_index.py::iter_index_lines(reader)`（`(step, 行)`：每一步、再是收尾的每一行 `ExchangeLine | PendingLine | BodyLine`；与 `load_archive` 共用同一个按提交读行的函数）；`replay/replay_log.py::iter_decisions(reader)`（`(step, 决定, 条数)`：写全的条数为 1，`summary` 读成它折叠的那种、条数为 `count`；`document_miss_total` 改成经它计数，折叠的规则只有一份）；新的 `core/capabilities/browser/archive_stats.py`：`body_completeness(lines)`（每条记录的最终状态读它的最后一行，`body` 行在内，与回放加载同一读法）、`revisit_saving(lines)`（revisit 指向的完整记录长度减自身长度，可以为负；指向缺失的记录报错，同 M29）、`challenge_documents(reader)`（应答头在 WARC 记录里，所以经 `load_archive` 读，带出所在的步）、`near_misses(decisions, recorded)`（一个回放的近邻 miss；`recorded` 是它的源归档，用回放记下的规则载入；候选指向源里没有的记录时报 `ReplayArchiveError`：这个回放不是从这份归档服务的）（每个近邻 miss 带取走它的步与不喂页面的原因）、`site_rule_candidates(runs)`（`runs` 是每个回放 run 的 `ReplayNearMisses{run, site, misses, checks}`，`checks` 是每个有恢复校验的步过没过；按站点与端点汇总、计 run，站点内按 `failure_association` 排，结果确定）、`not_feeding_page_counts(runs)` 与 `failure_association(failed, passed)`（Wilson 下界）。

#### 演练（fixture，零花费）

`tests/_helpers/flywheel_rehearsal.py`：一条命令 `uv run python -m tests._helpers.flywheel_rehearsal --out <dir>`（`run_rehearsal`；除了链根，还写一份 `rehearsal.json`：各段、各模型的路由、外联审计），e2e 测试（`tests/test_flywheel/test_rehearsal_live.py`）在测试进程里调同一个 `run_rehearsal`，读它跑出的东西（链根与 `Rehearsal.summary()`）。它有意放在全量测试的进程里，并让登录任务紧挨在漂移任务之前录：provider 把客户端留给垃圾回收的问题（§4.7）修好之前，正是这里一次模型调用的 TCP connect 永远完不成。演练起 M29 的 fixture 站点与每个运行段一个脚本化的模型（UI-TARS 的坐标回复，按任务文本里的标记与步号（请求里 assistant 轮的个数）路由），把发布的 UI-TARS 录制 / 原路径回放 / 分叉 / 导出配置改成 fixture 的那份（模型地址与键、fixture 的路由开关、任务的 `data_dir`、仓库文件的绝对路径；与 M30–M32 的驱动同一种改法），加上对照组（录制配置 + `archive: []`），写出链规格（`evidence: rehearsal`，植入值写字面量，`token_limit` 写一个远高于脚本化模型用量的数：每次调用报 15 token），在进程内跑整条链。四个任务分在三个站点上（fixture 的 `FLYWHEEL_SCENARIOS`，每个站点一个映射主机，控件都绝对定位）：`shop.example.test` 上确定性的四步流程（输入框、GET 表单提交、商品链接、读价格；商品页的脚本请求 `recommend.json?session=<timeOrigin>`，每次回放都是一个子资源近邻 miss）与一个登录页（运行时拼出凭证，经 `Authorization` 头、urlencoded POST 的 `password`、密码框的值发出，值从不出现在任何应答的字节里；不经 `document.cookie`：`storage.json` 原样保存 cookie 的值（§5.6），经 cookie 的凭证按设计会出现在 `env/` 里）；`drift.example.test` 上点击后同步跳到 `next.html?visit=<timeOrigin>`（pinned 不固定 time origin，回放必然是文档 miss，而且停在 Chrome 的错误页上；错误页写着那个 URL，所以这一组的回放截图两次不同）；`blocked.example.test` 的 403 带 Cloudflare 的三个头。分叉段在商品流程的结果页（第 2 步）分三叉：一个与录制做同样的事（覆盖）、一个提交结果页上录制没用过的 POST "quick add" 表单（必然 miss）、一个与第一个重复。导出 YAML 加上指向 fixture 任务数据的 `eval_loader` / `task_loader`（泄漏扫描经它们读参考答案）。e2e 测试断言每个度量的值与它的解释，每个运行段交给 bench 的剩余额度与它的 run 记下的 token（每段脚本化模型应答的请求数乘 15），报告算两次逐字节相同，以及演练期间没有非 loopback 的外联：Python 侧审计每一次 `socket.connect` 的对端；Chrome 侧录制用 hermetic 的解析规则、回放用隔离，归档里出现的主机只有 fixture 的。整条链在本机约 40 s。

#### 已知限制

- live 的对照组与录制不走同一条路径，归档开销比的是分布；对照组要再花一次录制的 token（live 计划里是可选项）。
- 原路径回放每个分支有一次 teacher 调用（最后一步），不是零花费。
- 拦截只按应答头识别；隐私只认植入的值与凭证的形状（同 §4.15 "隐私"的残余），而且只看两道门：`env/` 与 trace 的其余文件不搜，一个只落在那里的秘密这一项看不到（它们不离开这台机器，§5.6）。
- 度量只认 browser 的证据格式；第二条数据线见本节开头。
- token 上限只数 trace 记下的用量：provider 没报用量的调用只计数、点名，不加 token，所以从不报用量的路由会让上限形同虚设（这时每次调用都被点名）；评测器的 judge 调用不在 trace 里，不计入；一个任务跑完才检查，所以实际用量最多超出上限一个任务的用量。
- 一次回放停在错误页上时，Chrome 的错误页写着没送到的 URL：URL 每次不同的发散站点，回放确定性按截图比必然"不同"，报告照实计数（演练里的发散站点就是这样）。
- 存储的字节是 trace 落盘的样子。UI-TARS 的 memory 保留全部截图，每次模型调用的 prompt 都带着之前的每一张；trace 里每张图只存一份（`messages:images`，§5.3 "调用记录里的图片只存一份"），调用记录只写引用，所以 `messages` 随不同截图的张数线性增长，不再随步数平方增长（发给模型的 token 仍然是平方增长的，那是 agent 的选择）。这是保留期与压缩评估的主要输入，演练的 fixture 页面太小，看不出真实比例。

#### 代码位置

`spectragent/flywheel/`：`metrics.py`、`report.py`、`chain.py`、`cli.py`（`pyproject` 的 `[project.scripts]`：`spectragent flywheel`）；`core/data_loader/task/step_selector/last.py`；`core/trace/digest.py`（`tree_digest`）、`core/trace/usage.py`（`recorded_calls`、`recorded_usage`）、`core/archive/budget.py`（`ARCHIVE_MAX_P50_OVERHEAD_MS`）、`core/trace/sources.py`（`task_identity`、`unreadable_reason`）、`core/trace/secret_patterns.py`（`readable_text`、`iter_readable`）、`core/trace/reader.py`（`RunTrace.committed_files` / `read_committed`：存储与归档字节的账只读它们，§5.5）；browser：`archive_stats.py`、`replay/archive_index.py::iter_index_lines`、`replay/replay_log.py::iter_decisions`；发布的配置 `configs/flywheel/same_path_ui_tars_archive.yaml`；测试 `tests/test_flywheel/`（`tests/_helpers/flywheel_traces.py` 用真实的 WAL 写出答案已知的链根）；演练 `tests/_helpers/flywheel_rehearsal.py`；live 计划与它的配置在本地的 `tmp/flywheel/M33_live_plan.md`、`tmp/flywheel/m33_live/`。

---

### 4.17 实验命令、终端监控与本地结果查看器

用户 2026-10-15 确认的使用方式是本机或 SSH 启动实验、终端看进度、浏览器复盘。公开操作入口收敛为 `spectragent`：`config check|explain|generate|template`、`components list`、`run`、`bench`、`eval`、`export`、`flywheel`、`runs list|show|compare|compare-groups`、`tui` 与 `view`。YAML/conscribe 仍是实验配置的唯一完整表达；执行 CLI 参数选择任务与展示方式，模板命令的选择参数只产出普通 YAML，不另造运行配置路径。原有运行、评测、导出和飞轮的执行函数继续是唯一业务路径，旧的独立 console scripts 删除，不保留兼容别名。构建工具仍为 `spectragent-build`。

**配置解释。** `check` 和 `explain` 只 compose/prepare，不 setup、不调用模型。显示实例图、生效 action/fragment/archive 协商、AOP 拦截与生成配置新鲜度；配置按 trace 同一规则脱敏，校验错误只显示字段位置与问题类型，不回显秘密值。实际 Chrome 平台、外部服务可用性等只能 setup 后检查的事实不声称已通过。运行的环境变量规则保持现状：run/eval 读 `.env`，bench/flywheel 要求 shell 导出。

**配置来源有三条独立证据。** `explain --json` 的 `provenance.inputs` 记录验证模型的字段输入来源 `explicit | default`；显式写成默认值、`null` 或 `[]` 仍是 explicit，省略的默认子块不会因工厂给子字段传参而冒充用户输入。`provenance.declarations` 由 Conscribe 2.1 的公开 `registrar.get_config_provenance(key)` 提供：字段的声明类、构造接收者、继承覆盖记录、受体与 wiring 各自的声明者、完整 MRO、实际构造参数链及截断原因、schema/flattened 提取来源分别表达。它是注册类的声明视图（`declared_fields_before_schema_composition`）；注册表添加的判别器与组合包装不伪造 Python 参数声明，实例的层/实现身份与全部验证字段的输入来源另行记录。AF 不复制 Conscribe 的 MRO、注解或 wiring 解析，也不读其私有缓存。声明来源与输入来源不是互斥标签：显式值可以填到继承自基类的参数。

`ComposedRun.composition` 是 Composer 在真实注入决定处产生的不可变 `CompositionEvidence`；没有配置值或运行时对象引用。`components` 记录配置路径、层、实现和显式/序号实例身份；`wiring` 区分 `inline`、`pool`、`owner_default`、`empty` 与 `declaration_only`，并记录实际 init/setattr/retained 路径及目标。主人自己构造的默认对象标为 `owner_default`、`path=None`、`instance_id=None`，不能伪造 YAML 块或 Composer 身份。省略与显式空不合并，纯协商声明不画成实例注入边。CLI 展示这些事实，不通过扫描实例私有字段重建图；输出的 `resolved` 仍是按 trace 规则脱敏的已验证配置，不声称是任意运行状态的快照。

配置工具共用 `core/configuration_catalog.py` 的不可变 `ConfigurationCatalog`（公开模型/实现引用、只读层与键映射、依赖顺序）；`ConfigComposer.catalog` 指向它实际使用的那次构建。模板编写与解释共享此配置空间，不共享运行来源记录。组合失败保持原有异常及异常链；CLI 另标明 YAML、schema、validation、composition、selection、prepare 或 provenance 阶段，并从公开异常合同读取字段位置、缺失集合和原因。构造异常正文不回显配置值，安全诊断由抛错位置提供；不另造一套 prepare/协商规则，不把未完成的图显示成成功结果。

**带注释配置模板。** `config template` 从组件选择或 `--from CONFIG` 的选型骨架产生普通 YAML，默认展开所选组件、受体和 Browser 默认动作的全部配置字段；未选的可选组件不展开。字段说明、约束和默认值来自实时 Conscribe schema；声明为 `declare_only` 或没有接收槽的协商项不写成配置块，已填写的非法协商项仍由 Composer 拒绝。模板消费公开 `ConfigurationCatalog` 和 `conscribe.introspect`，不读取 Composer 或 Conscribe 的私有解析状态。通用 schema 元数据属于 Conscribe，浏览器选型、记录通道和动作默认集合的含义属于 AF；不为模板新增 registrar、兼容性 Protocol 或第二个 Composer。

无骨架的新模板明确选择 BrowserUse、ReAct、`openai.direct`、simple memory、browser environment、local Chrome、instant mouse/typing、BrowserUse cleaner、text serializer、`replayability: none`、`network_source: live`；trace writer 只打开 metrics，并显式 `archive: []`。这些是模板选型默认，注释与 schema 字段默认区分，不是注册表首项；`--model` 必填，不猜模型型号。`--agent`、`--loop`、`--llm`、`--environment`、`--backend`、`--model` 和 `--replayability` 可替换相应选择；多实例骨架若无法唯一定位则要求直接编辑 YAML，不任取一个。变更实现时清除旧实现的标量旋钮，保留同名受体及实例 id；选择同一实现保留原值。重复 `--record metrics|errors|trajectory|observations` 显式选择这四条记录通道，`all` / `none` 是四通道的全开 / 全关，不能与其它通道并列；不增设 tokens 或 cost 开关。`--replayability` 只选 none/pinned，既不改网络来源也不改归档；archive 网络与归档器可从已有回放骨架保留。

Browser 的 `actions: null`（含省略）在没有匹配的顶层 action 实例时展开其声明的默认动作及参数，`actions: []` 和显式子集原义不变；有匹配的顶层 action 实例则保留受体的 null/adoption 语义并完整展开池中实例，不复制成第二套动作。trace writer 的默认 `RecordingPlan` 也显式展开。模板前后均经同一 Composer 校验；生成不启动环境、不调用模型、不打开任务的回放证据，任务相关 prepare 继续交给 `config check` / `run --dry-run`。缺少必填字段时点名路径，失败不写输出。默认 stdout 仅为 YAML，`--json` 包含 YAML 字符串；`--output PATH` 只创建新文件，拒绝覆盖已有文件。`--from` 保留用户明确填写的值，不修改源文件；模板是可执行配置，不是 explain 的脱敏报告。

**一个读侧，多个界面。** `spectragent/inspection/` 提供结构化查询 Protocol，CLI、Textual 和 FastAPI/React 本地查看器共用它。运行目录发现可以列目录；一个 run 的全部记录与附件只经 `RunTrace`，不绕过 manifest。每次刷新建立新的 reader 快照，不复用缓存的 manifest。路径必须在用户指定根目录之内，损坏或不支持的 trace 在列表中显示原因，不静默消失。网页对 trace 只读，唯一可写的是下文显式给出的标注目录；默认监听回环地址，远端使用 SSH 转发。前端资源随 Python 包分发，不依赖运行时 CDN 或 Node。

`view` 是前台进程，`serve` 显式持有监听 socket，启动或关闭异常也在作用域退出时关闭；先成功绑定再报告 URL 或安排打开网页。Ctrl-C/SIGTERM 沿 Uvicorn 的信号处理关闭，未完成请求最多有 2 秒 graceful-shutdown 宽限。此预算限制请求等待，不声称能中断 OS 中阻塞的文件 I/O；查看器不写 trace。已脱离终端的旧 viewer 不会收到另一个终端的 Ctrl-C，不能在启动时擅自杀掉占用端口的进程。

**目录、配置来源与单次运行。** `view`、历史 `tui` 和 `runs list` 使用同一选择规则：`--root` 是目录搜索边界，默认 `traces`，也可以是单个 run；`--config PATH` 按启动配置文件的来源筛选这个根内的全部历史版本（用户 2026-10-15 确认），不解释为当前 YAML 内容相同，也不通过读取 YAML 的输出路径偷偷扩大搜索范围。路径展开 `~` 并规范化为绝对路径；不同目录中的同名文件不同，移动后的文件视为新的来源。配置文件已经删掉也能按原路径查询。`--config-hash` 另用于精确选择已记录的有效配置摘要，两者可以取交集。`--run KEY` 是根内相对路径，用于初始聚焦；列表筛选不缩小根内源运行导航的范围，但初始 run 必须满足显式筛选。没有显式网页路由时采用初始聚焦，已保存的网页深链接优先。

**来源证据。** `TraceConfig.config_source` 是可选的 `ConfigSource{path, sha256}`：path 是配置文件规范化后的绝对路径，sha256 是启动时读取的源 YAML 字节摘要。CLI 读取一次字节，同时用于解析和计算版本；经 `AgentRuntime.execute(config_source=...)`、lifecycle 传给 TraceWriter，不从脱敏配置反推。它是来源元数据，不进入既有 `configuration_digest`，也不记录 YAML 内容、凭据或环境变量。批量任务清单记录同一来源。飞轮先读取原阶段配置，再改写运行路径；调用现有 CLI 时显式传递原来源及其已捕获版本，因此筛选原模板能找到它启动的运行。未记录来源的 trace 显示来源未知，不迁移、不猜测、不混入指定来源的结果。

**四种状态。** agent 结束原因、环境状态、记录完整性和评测判定分开；`writing` 不是进程存活证明，离线列表只显示记录状态。任务正确率采用有效判定作分母并展示覆盖率，评测失败与未评测不算任务判错。一个 run 的多个 evaluator 分列，不能任取一个当总判定。策略步、恢复步和终止观测分开，预算只数策略步。用量从所有已提交及 trailing 调用的 `recorded_usage` 读取，缺失用量明确列出；没有价格估算，没有伪造 TTFT。

**实时监控。** 先前 §4.6 的非阻塞 after/WebSocket 占位由本节细化：注册的 `live_monitor` 采用 blocking around，仅更新 run 所有的内存状态，`proceed()` 的返回、异常及取消原样传播；不引入 Loop emitter，不驱动环境另采观测。终端渲染独立调度，故慢界面不进入 agent 方法调用。CLI 在 prepare 前绑定本次的监控状态并按环境是否存在选择 env 拦截，没有全局 sink。方法完成不等于 WAL 提交：已提交步骤从 RunTrace 读，瞬时阶段明确属于当前进程。关闭拥有执行任务的 TUI 必须请求取消并等待 runtime 收尾；历史查看没有暂停、原地恢复或远程接管的暗含语义。

**批量任务清单。** CLI 在 trace 根的 `.sessions/` 下原子写入本次任务选择、逐任务进度、结果引用和停止原因；不保存凭据、原始 YAML 或环境副本。它描述命令选择了哪些任务、哪些尚未启动，不替代 RunTrace 中的 agent/eval 事实，也不是进程心跳。取消、协议否决及 token 限额停止必须保留尚未启动任务；token 阈值仍在任务结束后检查。

**查看内容。** 运行列表、步骤时间线、截图、任意 observation fragment、每步全部模型调用及 trailing 调用、请求 schema、动作与错误、评测依据、源/分叉关系和配置比较；不同 evaluator 不混算。已有独立评测、飞轮、诊断和数据集报告直接读取各自产物，保留原来的分母和不可测原因。浏览历史与主动网络回放是两种操作；查看器不启动浏览器环境、不重新判分。

**人工标注与审阅（2026-10-21）。** 用户要求查看器不只供人查看数据，也供人标注与审阅数据。`spectragent view --annotations DIR [--annotator NAME]` 打开写入；省略 `--annotations` 时查看器与以前完全一样只读，所有写接口拒绝。查看器只写这个显式给出的目录，从不写 trace（目录位于某个 run trace 目录之内时启动即拒绝；判断依据是该目录的 manifest 能否作为 trace manifest 读出，而不是任意名为 manifest.json 的文件），也不改动队列文件。布局固定：`DIR/queues/<queue_id>.json`（`review-queue-1`）由产生待审数据的工具写入，查看器只读；`DIR/labels/<queue_id>.jsonl` 与 `DIR/labels/_adhoc.jsonl`（`review-label-1`，一行一个 JSON）由查看器追加。存储、校验与判定在 `spectragent/annotation/`，与网页无关，工具和测试可直接使用。

队列每次请求重新读取并校验：条目 id 唯一，条目内图片角色唯一，建议所用的选项、区域类别与图片都存在于规格和条目中，队列 id 与文件名一致且不以 `_` 开头。建议的 `answers` 为空对象时整个队列视为损坏（它会让任何标签都被判为 accepted；没有建议答案时应省略该字段）；框与其它数值必须是有限数，NaN 和无穷在读取时拒绝、写出时不产生。损坏的队列在列表中带原因显示，不消失。标签只追加：对标签文件加排他锁，整行写入、flush、fsync，历史行永不改写；当前标签是同一 `(item_id, annotator)` 在文件中的最后一行，按文件顺序而不按各自时钟，不同标注者互不覆盖，也不合成多数票。每行记录的是标注者实际看到的队列版本：网页随每次保存（以及条目图片请求）带上它加载的队列文件字节 sha256，存储层与当前文件不一致时拒绝并请标注者重新加载，而不是把标签记到他没见过的版本上；队列之后被工具重写时，旧标签照常显示并标明针对早先版本，网页里未保存的草稿也只属于它所在的版本，版本变化时丢弃并告知。不可读的标签行逐行报告，不影响其它行；文件末尾没有换行的最后一行视为仍在写入，跳过而不报错。条目已从队列删除的标签按条目计数（不按历史行数）。

队列对每个条目问一个问题（`choices` 与 `multi`）或几个互相独立的问题（`questions`，每个 `{key, text, choices, applies_to}`），二者只能选一；条目可用自己的 `questions` 列出所问的 key，否则按各问题的 `applies_to`（`null` 为全部条目）。标签按队列的 label 规格校验：单问题时 `multi` 决定填 `choice` 还是 `choices`，值必须在 `choices` 中，单选规格要求选择；多问题时标签行写 `answers`（问题 key → 选项值）而不写 `choice`/`choices`，条目所问的每个问题都必须回答（跳过和驳回除外，此时不带答案）；区域类别必须在 `regions.kinds` 中，`regions: null` 的队列不接受区域；框 `[x0, y0, x1, y1]` 以所引用图片的原始像素计，面积非零且在图片之内，尺寸由服务器从图片本身读取；`note: false` 不接受备注。每个选项另有框规则 `LabelChoice.regions: optional | required | forbidden`（默认 `optional`；不画框的队列只能是 `optional`）：标签选中的选项（单选、多选或各题答案）里有 `required` 就至少要一个框，有 `forbidden` 就不能有框，两者同时选中则不能保存；跳过和驳回不带答案，不受规则约束。建议同样受规则约束，但只在它给出了 `regions` 时检查（没给框的建议不算“空框”）。规则在存储层校验，网页只用同一条规则提前说明为什么不能保存。`verdict` 由存储层计算，网页不能指定：没有建议、或建议没有给出 `choice`/`choices`/`answers`/`regions` 中任何一项、或标注被跳过时为 `null`；标注者显式驳回建议且不给替代答案时为 `rejected`；否则只比较建议实际给出的答案字段（多问题时比较建议 `answers` 里的每一题，区域按 (图片, 类别, 框) 的多重集合精确比较，备注与建议的 `reasons` 不参与），相同为 `accepted`，不同为 `corrected`。

条目图片按 `source.kind` 解析，且只服务队列条目引用的图片，不提供任意文件接口：`observation` 走既有截图读取（Browser state 原图）；`archive` 只经 `RunTrace.archive_files(step, archiver)` 按 `name` 找到已提交文件（`step: null` 表示 trailing 证据，经 `iter_archive`），声明的 media type 与文件头都必须是 PNG/JPEG/GIF/WEBP。run 键与查看器的其它路径一样必须在根目录之内，并在输入时规范化为仓库的唯一写法（`./run`、`run/.`、`run/` 都是 `run`），同一个 run 只有一个身份。随手标注可附在任何 run 的任一步（已提交的步不会改变，因此随手标注不需要版本；只写备注时不读取截图）：备注和画在该步 Browser state 截图上的框（类别固定为 needed information / problem / other），写入 `_adhoc.jsonl`，带 `run`、`step`、`queue_id: "_adhoc"`，无 `item_id`，当前值按 `(run, step, annotator)`。

**动作标记（2026-10-22）。** 用户要求截图上画出动作的鼠标位置：点击的点、拖动的起点到终点、输入的点和文字、滚动的位置和方向，否则“拖动只能靠猜”。唯一来源是 `spectragent/inspection/marks.py`：它经 `RunTrace` 读取一步已记录的动作与结果，把每个动作变成一个标记（编号即它在该步动作列表中的序号，从 1 起）和一行中文描述；查看器的运行页（`GET /api/marks?key&index`，该步动作画在该步截图上）与审阅页（`GET /api/review/marks?queue_id&item_id&role&queue_sha256`，只服务队列条目引用的图片）都只转发它的结果，lookahead 的队列构建也用它写条目里的动作文字。动作的中文措辞也只有这一份：`ACTION_VERBS`（按全限定名，每个浏览器动作一个动词）既是每行描述的开头，也是查看器步骤列表的标签，运行接口（`GET /api/run`）以 `action_verbs`（本 run 各步执行过的动作名 → 动词）转发；网页不另存措辞表，表里没有的动作照录下的名字显示。动作参数与测得效果是 CSS viewport 像素（§4.1），标记按所画图片自己的几何换算为图片像素：观测用截图 fragment 记录的图片尺寸与视口（`point_grid("screenshot_pixels", …)`，即 §4.1 的同一条换算），`browser.frames` 画面帧用帧自身的 PNG 尺寸与提交它的那一步观测的视口；任一尺寸未知时只列出动作、不画，并说明原因，绝不假设设备缩放为 1。点位优先取动作效果测得的点（实际派发输入的位置），否则取参数：哪些参数是点、哪个参数指名元素，读录下动作注册的 `Params` 上的标记（§4.5“参数的含义”；拖动按在第一个点、松开在第二个点），动作名按全限定名分派，fragment 按全限定键读，不按名字的最后一段；页面滚动在鼠标所在处派发，因此画在该步观测记录的鼠标位置，并随同一步里此前的指针动作移动，未知就是未知；敏感字段的输入文字不显示。没有位置的动作（按键、等待、完成等）只进图例。Loop 因同一步更早的动作失败而没执行的动作（`StepRecord.skipped_actions`，§4.1）照样列出，描述后面写“（没有执行：第 N 个动作失败）”，不画在图上，也不移动推算的指针；运行页的步详情在动作数量超限的提示旁另有一行，说第几个动作失败、最后几个动作没有执行。浏览器能力目前没有双击、右键和悬停动作，因此没有对应标记；拖动的移动轨迹没有被记录，只画起点到终点的箭头。

哪张图画谁的动作：`QueueImage.marks_from` 为 `null`（省略）时跟随图片来源——观测画该步在这一屏上决定的动作，before-action 画面帧只画它之前的那个动作（帧按 `env.act` 调用编号，与 `StepRecord.actions` 按名字顺序对齐，跳过被动作守卫拒绝、从未到达环境的动作），其它图片不画；`[]` 不画；列表 `[{run, step, label}]` 按顺序画每个指定步骤的全部动作，每项一种颜色，图例用 `label`，坐标一律换算到这张图的像素。同一位置附近的多个点在屏幕上错开显示，标记可以整体隐藏。

**给外包标注者用的界面（2026-10-22）。** 用户要把标注交给其他人做，他们“不知道三个审阅里要标什么、每个选项是什么意思”。查看器的全部界面文字（运行页、审阅页、快捷键表、按钮、提示、空状态，以及网页原样显示的标注错误）是简体中文；代码标识符、存储值（如 `still_loading`）、API 参数与运行记录本身保持英文，不引入 i18n 框架。审阅页能自我说明：`LabelChoice.help`、`Question.help`、`RegionKind.help` 写在选项旁；`ReviewQueue.guide`（`[{title, text}]`，`- ` 开头的行是列表项）连同说明、每个选项的含义与框规则组成“标注说明”面板，首次打开队列时自动展开，按 I 或按钮随时打开；`ItemField.technical: true` 的字段收进默认折叠的“技术细节”。`ReviewQueue.suggestion_display` 的 `title` 是建议的标题（如“模型的判断（仅供参考）”），`shown: on_request` 时建议默认隐藏、不在选项上做记号，标注者按 V 或按钮查看后才能接受，避免先入为主；`always` 为默认。标题之外，模型给出的英文理由原样显示并标明是原文。

标注者身份由启动参数决定（默认操作系统用户名），网页请求不能改写。所有 `/api` 请求（读与写）的 Host 必须是 IP 字面量或 `localhost`，防止 DNS rebinding；写请求另须是 `application/json` 且 `Origin` 与 Host 同源，防止跨站提交。`--annotations` 只能与回环地址的 `--host` 同用，否则启动即拒绝并点名两个参数；远端标注经 SSH 转发。网页里按住的按键不重复执行决定；Enter 在任何审阅控件上都是保存；接受、编辑、跳过、驳回若会丢掉未保存的答案，需在 1.5 秒内再按一次。网页的审阅模式与运行浏览并列：队列列表与进度、逐条审阅（多图并排、缩放与对比、按图画框/移动/删除、动作标记及其开关（M）、选项数字键（多问题时每题一行，数字键作用于当前题）、选项说明与框规则提示、标注说明面板（I）、备注、建议及其来源和每题理由（按需显示时按 V 查看）、一键接受、驳回、跳过、前后翻页、未标/我已标/与建议不一致筛选、跳转到 run 与步骤），步骤视图内可随手标注。

**回放与恢复显示。** 只由已录事实标记网络来源与分叉关系，不从目录名推测 same-path 或 fork 业务角色。分叉的来源链接指向源 run 的检查点；源缺失、ID 歧义或根外来源明确显示，查看器不越界打开。恢复步的普通 actions/action_results 是本次恢复中实际返回结果的配对；所有执行尝试、异常和重定位保存在 `restoration.actions`。旧回复与源动作只在 `source_history` 中作为来源材料展示，不能冒充本次新思考；顶层无返回结果的失败尝试也必须从完整恢复证据中显示。实时 TUI 显示配置的 max_steps 上限与 branch continue_steps，不能把 max_steps 当作分叉实际策略预算。浏览器回放决定通过既有 `iter_decisions` 读取和按权重计数，包含 trailing 的决定；未绑定日志、日志为空与已提交日志不可读三种情况分开，未知不能画成零 miss。

**历史与环境的差异视图（2026-10-16）。** 分叉页面与 TUI 显式展示已记录的 `history_source`、恢复器 `verify`、checkpoint 和 continuation 预算。源历史与实际恢复观测并列，动作重定位、返回结果、异常、URL、DOM signature、截图 dHash 和 document misses 使用既有恢复证据；容差内通过与存在可测差异同时显示，零 dHash 不宣称像素相同，未测不补零。TUI 与网页查看器（用户 2026-10-24 裁定）：一个未测项若在某 run 的每个已检查步骤（有恢复差异记录的步骤：恢复步及带恢复校验的步）上都未测（例如 DOM signature 未测），只在 run 级说明一次，仍显示为未知、绝不变成 0；步骤行与步骤列表只计其余、在各步之间不同的未测项，每步详情与 JSON 保留完整的逐步记录，并标明哪一项是全 run 都未测的。这一 run 级事实由读侧唯一计算：`inspection/restoration.py` 的 `unmeasured_everywhere`，经 `run` 详情的 `unmeasured_everywhere`（`facts` 与 `checked_steps`）提供给网页与历史 TUI；实时监控对目前已观测到的步骤调用同一函数。只在恢复步与显式 checkpoint 校验处建立源步骤对应，后续 policy 步不凭相同步号伪造配对。源不可读不遮蔽本次可读证据。截图可切换 Browser state 原图与 Agent observation：后者经 fragment 的公开 `ModelImage` 合同按已记录元数据重建 cursor，默认显示模型视图，标明光标关闭或位置未知；精确提交的模型输入仍看 recorded calls，不把重建视图宣称为已提交输入。TUI 详情与日志支持选择和复制；在文本区 Ctrl-C 复制，活动区 Ctrl-C 与 q 保留取消并等待收尾；日志选择期间冻结文本，防止刷新破坏选区。

**终端界面的读法（2026-10-24）。** 两个 Textual 界面都由纵向排列、占满宽度的面板组成；面板标题带跳转键（历史查看：1 runs、2 steps、3 details；实时监控：1 tasks、2 observed steps、3 details、4 log），只有拥有焦点的面板边框高亮。详情先列已记录事实的摘要，下方保留完整 JSON（按当前主题语法高亮，只依赖 `tree-sitter` 与 `tree-sitter-json`，不引入 Textual 的整套语法扩展；高亮只改样式，选择与复制得到的仍是记录原文）；摘要只是同一份记录的读法，不另造事实，也不替代 JSON。复制键：文本区内 Ctrl-C 复制所选（无选区时复制全部）；F8 在任一面板复制整个详情 JSON，F9 复制摘要的纯文本；两者都不取消运行，并显示在 footer 与 ? 说明中。步骤表把恢复器的容差判定（Check）与测得差异和未测项（Measured）分列，各自计数；恢复步先列本次实际尝试及其返回结果，再以 `source:` 列出来源动作，窄单元格截断时保住本次所做的事。终止观测、checkpoint（◆）与 policy 步各有标记。一步在失败动作处停下时（`StepRecord.skipped_actions`），步骤行与摘要把其后的动作列为 not executed 并写明原因（action N failed，编号与智能体告诉模型的一致），从不显示成失败的结果；只有那个失败动作标为 failed。历史查看在步骤表中移动光标即显示该步，Enter 进入详情，Esc 逐级返回（放大、详情、步骤、列表），`[`/`]` 在任一面板前后逐步，z 放大当前面板，? 显示当前面板的按键与图例；从分叉按 s 打开源 run 时，源 run 的 checkpoint 步同样标出，并注明来自哪个分叉。run 列表按已录 branch provenance 把分叉折叠在其源 run 之下：读侧 `list_runs` 为每个分叉行给出在整个根内解析的 `source_key` 与 `source_resolution`（found / missing / ambiguous，与 run 详情同一规则，不因列表筛选而缩小，也不参与搜索匹配）；只有源被唯一解析且在当前列表中时才嵌套，否则分叉留在顶层并写明源缺失、ID 歧义或不在当前列表中，绝不按目录名归组。分组默认折叠，空格切换，→ 展开，← 折叠或回到源；有筛选时含匹配项的组全部展开（可再折叠），源不匹配的分叉单独列出并注明，清除筛选后恢复用户原来的折叠。实时监控的步骤表在光标位于最后一行时跟随最新观测步，详情随之更新；用户上移后停在原处，回到最后一行再恢复跟随。q 的标签是 Close TUI；运行仍在进行时，状态卡写明关闭会取消本次运行并等待收尾，取消中则写明正在等待收尾。颜色取自当前主题并总与文字或符号同时出现，浅色主题、ANSI 主题与 `NO_COLOR` 下含义不变；终端变窄时表格先隐藏次要列（不可读 run 的原因留在不会隐藏的首列），隐藏的值仍在详情中。以上布局与按键不改变上文的复制、取消、收尾与日志冻结规则。

### 4.18 独立 replay 审查

`ReplayReviewerRegistrar`（`reviewer_id`，配置层 `replay_reviewer`）属于独立
`spectragent/replay_review/`，首个实现 `jev` 通过 `decision_model` 单实例受体调用
Decision 模型。`BaseReplayReviewer.review(case) -> ReviewAssessment` 只判断给定任务、
下一批动作和对齐观测的相关差异。它不返回 `EvalResult`，不把模型故障当任务失败，
不进入 Runtime/Restorer，不修改 matcher、规则、源 trace 或源用量。

`spectragent replay-review --config <普通组合 YAML> --pairs <输入 JSON> --out <新目录>`
由 `ReplayReviewRunner` 驱动。输入列明 source/replay trace、各自 step 和源运行配置。
自动对齐只接受有明确 branch/source 对应关系的恢复步骤；显式输入也验证该关系。
所有证据从 committed reader 取得。审查域层选择任务和动作，浏览器证据读取/重清洗
位于 browser 包，通过 `ArchiveReader` 读取 raw DOM；cleaner、budget、serializer
从所给源配置的同一个已组合 capability 取得，不另外默认构造策略。保存源配置中
这些策略与轨迹记录配置的比对、证据文件摘要、capture 时间/cycle、裁剪说明、链接、
几何、原始 screenshot 来源和原有 restoration/miss 事实。缺少来源、未采集内容、损坏、
对齐失败分别报告，不从空提取推断等价。图片不混入 Agent 的光标展示变换。

原生答案、执行状态、分诊路线及其理由分别保存。没有原生的总体语义判断：路线是政策
从原生答案和机器事实导出的值，不是模型的结论。不自动调用强模型。标签、预期答案、
开发/留出划分只在验收侧，永不进入模型请求。

**问题集、政策和证据选项都是必填 YAML 字段**（用户 2026-10-20 决定），代码不留默认值，
也不保留旧版本的并行路径。三者各有内容摘要：对规范化 JSON（键排序、紧凑分隔、UTF-8）
取 SHA-256；问题按发送给模型的逻辑形式计算（省略的字段与显式 null 不同）。版本标签
给人看，摘要才是依据。job 快照记录三者的全文与摘要；每条 assessment 记录问题集版本
与摘要、完整政策与摘要、证据选项与摘要、实际使用的阈值及其来源和机器事实，
`route_result(result, settings, facts)` 因此可以离线重算。出厂形状：

```yaml
replay_reviewer:
  - reviewer_id: jev
    decision_model: {...}             # §4.7
    evidence:                         # 请求带什么：每侧观测预算、截图、隐去的文本
      done_answer: withhold           # keep | withhold
      max_text_chars: 6000
      max_links: 20
      include_screenshots: true
    questions:
      version: replay-relevance-2
      items:                          # 问题 ID → §4.7 的 Decision 问题
        targets_equivalent: {type: noul, instructions: {...}}
        information_equivalent: {type: noul, instructions: {...}}
        evidence_sufficient: {type: noul, instructions: {...}}
    policy:
      version: replay-triage-2
      information_question: information_equivalent
      thresholds:                     # 全局默认
        accept_information_min: 0.45
        investigate_information_max: 0.25
        fit:                          # 拟合来源
          dataset: <所用标注数据的说明>
          dataset_sha256: <64 位十六进制>
          fitted_on: 2026-10-20
          policy_version: replay-triage-2
          questions_sha256: <拟合所用问题集的摘要>
          evidence: {...}             # 拟合所用的证据选项全文，含预算
          evidence_sha256: <这些证据选项的摘要>
      overrides: []                   # 可为空，不可省略；元素形状见下
```

覆盖元素是 `{scope: site, domain: <注册域>, ...}` 或
`{scope: task, benchmark: ..., subset: ..., task_id: ..., ...}`，其余字段与 `thresholds`
相同（两个阈值和 `fit`）。

**问题集 `replay-relevance-2`。** 三个独立的原生 Noul，不求平均：下一批动作的目标是否
相同（`targets_equivalent`）、任务所需信息是否相同（`information_equivalent`）、证据是否
足以比较（`evidence_sufficient`）。总体三选一 Choice 删除。只有信息问题参与路由，另两问
只记录：目标问题在已有样本中缺少目标型反例，证据问题在现有裁剪下几乎总是低分。信息问题
点名需要账号时的登录状态、错误或登录提示、未渲染的图片、视频画面与播放位置，并写明
next_actions 里的答案文字不是信息存在的证据。这些点名参考过已知错误，在新任务、新站点
的留出验证之前视为未验证。问题集是通用的 ID → 问题映射，研究者可以改写措辞；改写后应
换版本标签，摘要无论如何都会区分内容。构造时检查政策的 `information_question` 存在于
问题集且是 Noul，否则组合失败。

**证据选项。** 决定请求内容的设置都在 `evidence` 块里，全部必填：每侧观测的文本字符
上限 `max_text_chars`、链接与几何条目上限 `max_links`、是否附原始截图
`include_screenshots`，以及 `done_answer`。出厂预算为每侧 6000 字符、20 条，即阈值拟合
所用的预算；更大的预算在 DMX 路线上多数页面超出输入上限（用户 2026-10-20 决定）。
`done_answer: withhold` 从请求中去掉源 `done` 动作的答案文本：动作参数中的
`extracted_content`，以及恢复对应关系里 done 动作参数和其结果中的同一文本；动作类型保留。
`keep` 原样发送。出厂值为 withhold：审查判断的是观测是否相同，不应参考被测 Agent 自己的
结论。回放服务事实不进请求，确定性规则放在模型之外。恢复对应关系里每条实际结果的动作效果
（`effect`，§4.5）也不进请求，一律去掉（用户 2026-10-20 决定）：请求因此与阈值拟合时的证据
逐字节相同（拟合所用的录制早于动作效果）。要让审查模型看到效果，须新增一个显式证据选项，
它进入证据选项摘要，原有拟合随之失效、需要重新拟合。

**政策 `replay-triage-2`。** 路由是纯函数，输入为信息问题的原生 Noul、选中的阈值和机器
事实。判定按以下顺序：
- `investigate_difference`：回放有视口内图片未被服务（见下），或信息值 ≤
  `investigate_information_max`。确定性门槛先于阈值：视口内缺图时信息值再高也进入调查。
- `candidate_acceptable`：信息值 ≥ `accept_information_min`，恢复检查结论为 `ok`
  （`warn` 与 `diverged` 都算恢复警告），并且已测得没有视口内图片未被服务。
- `further_review`：其余情况，包括介于两阈值之间、恢复警告但信息值高、图片事实无法测得、
  证据缺失、服务故障和答案合同错误。

视口内缺图不依赖模型：模型调用失败、答案不合合同或因缺截图而未调用时，只要该事实为真，
路线仍是 `investigate_difference`，执行状态照实记为失败（用户 2026-10-20 决定）。图片事实
无法测得时阻止候选接受（同日确认）。reasons 写明决定路线的条件。这三条门槛由政策版本固定，改门槛须换版本；YAML 只给出路由
问题、阈值和覆盖。candidate_acceptable 只决定人工复核的先后（最后复核），从不跳过复核，
也不在运行时自动放行或改变恢复结论。

**阈值是活参数。** 阈值是暂定的分诊参数，随标注数据增加持续重拟合，不是校准承诺；
应在不同运行上继续拟合和检查，使误放率可以测量。政策有一个全局默认，另有可选覆盖，
范围是站点或任务。站点写明注册域 `domain`（如 `bilibili.com`），参考观测 URL 的主机名
等于该域或以 `.` 加该域结尾时匹配；不使用公共后缀表。多个站点覆盖同时匹配时最长的域
胜出，同一个域写两次在组合时拒绝。任务按源 trace 的 `benchmark`、`subset`、`task_id`
三者精确匹配。任务覆盖优先于站点覆盖，二者都优先于默认（用户 2026-10-20 决定）。
两个阈值都在 [0, 1] 内，调查上限必须严格小于接受下限。

每组阈值（默认和每个覆盖）都带拟合来源 `fit`：数据集说明、数据集摘要、拟合日期、拟合时
的政策版本、所用问题集的摘要、所用证据选项的全文（含预算）及其摘要；摘要须与记录的全文
一致。阈值与措辞和证据绑定：当前问题集或证据选项的摘要与某组阈值的 `fit` 不同时，组合
直接失败，错误同时写出拟合时与当前的两组摘要；修正方法是重新拟合，或为当前问题与证据
写一条新的拟合记录。

**视口内图片未被服务**（机器事实 `viewport_image_not_served`）有两个来源，读法都在 browser 包
`review_evidence.py`，都只看被审查回放步骤自己的那次采集（用户 2026-10-20 决定）：

1. **replay log + raw DOM。** 只取发生时间不晚于回放截图的决定。更早的采集不计入：
   单页应用在许多步骤里保持同一个文档，早先没服务的图片之后可能已被服务或已离开页面。
   这一来源为真的条件：这次采集中某个 `resourceType: Image` 请求被回放判为 miss（strict 下答
   404 或失败），其 URL 被主文档某元素的属性值包含，或等于该元素的 `currentSourceURL`，
   并且该元素的布局框与视口相交。回放没有绑定 `browser.replay_log`，或 raw DOM 采集没有
   布局快照时，这一来源无法测得。
2. **回放步骤的 `capture_state`**（§4.4 的“可见资源加载失败”）。它与回放截图同属一次采集，
   是页面自己看到的结果：浏览器本轮数主文档中与视口相交、可见、已加载结束却没有可显示内容的
   `<img>`，不论原因是回放答了 miss、所给内容解不出图片还是别的。**只数回放新出现的失败**
   （用户 2026-10-20 决定）：回放步骤的失败图片标识出现在源步骤自己的失败图片标识里，就是源页面
   本来就坏的图，不计入；回放新出现的至少一张时为真，没有时为假。标识只有前 8 个（§4.4），
   回放里超出部分没有标识可比，按新出现计入。源步骤没有可用读数（源 run 没有协商这个 fragment、
   该步没有记下它、该次采集失败、或为 `unknown` / `unavailable`）时退回保守做法：回放的失败
   全部计入，并记下用了退回（`capture_state_baseline: none`，证据里写明源读数为何不可用）；
   比较了源步骤时记 `source_step`。回放 run 没有协商这个 fragment、该步没有记下它、该次采集
   失败，或这项事实为 `unknown` / `unavailable` 时，这一来源无法测得（基准也为空）。

合并：任一来源为真，事实为真；没有来源为真而至少一个来源测得为假，事实为假；两个来源都无法
测得时事实无法测得（`None`），不当作没有缺图。机器事实同时保存合并值和两个来源各自的读数
`viewport_image_sources`（真、假或无法测得，以及来源二的比较基准），因此能看出事实由哪个来源
给出、两个来源是否一致；证据里保留各来源的明细和无法测得的原因，来源二的明细含回放自己的读数、
新出现的标识，并附源步骤自己的 `capture_state` 读数（比较的基准）。两个来源都不覆盖样式表里的 CSS
背景图和子框架文档中的图片，结果中写明。审查域只消费合并后的事实；路由门槛和阈值不随来源变化。

独立 job 有自己的 `job_id`、不可覆盖的新目录、配置/问题/policy 快照、证据来源及
调用索引。模型调用经 AOP 的 `ainvoke` 边界，复用公共 PendingLLMCall、编码和图片
哈希合同；owner 为 replay_review、step 为 null，源 step 仅在证据引用中。
内容文件先持久写入，再原子安装审查 manifest；reader 只认索引内文件并校验摘要与
路径。每项有执行与记录两种状态，失败/取消调用只结算一次，写入故障不覆盖模型
返回或原错误。job 最终有界关闭自己持有的模型。

首版验收使用固定 Jev 的 DMX 路线做文本、图片/Box、真实网页的离线审查；官方和
OpenRouter 各有 HTTP 契约测试，有凭证才补真实调用。以三个独立源任务的 Luna
record/replay（max_actions_per_step=4）加合成语义反例、缺证据、重复和选项顺序测试
报告有效覆盖、接受子集错误/误放、分歧、翻转、延迟和超时。样本不足或错误仍单独
列明，不据此宣布动态 replay 已验收。动态参与另行设计。

## 5. RunTrace 记录与持久化体系

### 5.1 设计原则

**记录是运行时的一等公民**，但执行进展与持久化成功独立。每步先封存自己的事实，再由有界后台任务提交；manifest 已提交的记录具有 WAL 保证。进程崩溃时仍在队列中的段可能缺失，记录健康和提交索引如实表达缺口，不声称执行到第几步就必然保存到第几步。

**不使用 SQLite** — agent 场景的数据结构是异构的（不同 fragment 类型字段完全不同），schema 会随框架演进频繁变化，这与关系数据库的核心假设（同构行、稳定 schema）冲突。数据落盘速度不是运行瓶颈（LLM 推理每步需要数秒，文件写入只需毫秒），SQLite 的查询优势可通过后置索引实现。

**环境证据（environment evidence）是第二类记录。** trace 记录 agent 行为（2026-09-16 的裁决："agent 换一个选择，这个值会不会变？"会变的才记）。环境在 run 中的状态与资源——响应体、存储快照、原始 DOM 采集、环境指纹——通过同一个检验：agent 换一个选择，它们就会不同，而且它们是那次决策所处的世界，不是框架执行决策的细节。按用户裁决 D2（2026-10-09），它作为单独的一类进 trace：

- **显式开启**：只有绑定了非空 `archive`（§4.5）的 run 才有，默认没有；
- **单独放在 `env/` 子树**（§5.3），读侧有自己的访问器（§5.5）；
- **单独标记隐私**：`TraceConfig.env_evidence_privacy`（绑定了归档器才写；绑定了哪些看 `negotiation.active_archivers`），加上 capability 自己的 run 记录（§5.6）。

2026-09-16 那条裁决的另一半照旧成立：不记 CDP 事件流，不记输入 tick。归档存的是资源和状态，不是把它们送过来的协议事件序列，也不是框架执行一次点击的轨迹。

### 5.2 写入协议

每步的内容是一个不可变封存段：`TraceWriter` 在交接时快照选择的事实，后台持久化任务把该段交给 `WALWriter.commit_step`。每步只提交一次；失败段按原身份有界重试，后续段可以先提交。WAL 按固定顺序写完，再原子更新 manifest。

```
commit_step(step, record, *, fragments, llm_calls, call_metrics, extra_files) 的顺序:
  0. 校验（写任何字节之前）：路径规则见下；内容寻址文件的摘要与路径一致
  1. 内容物：fragment JSON 与 artifact、每次 LLM 调用的记录、本 run 还没提交过的 output schema
     与 prompt 图片、extra 文件 —— 逐个 tmp → fsync(file) → os.replace；不做逐文件的目录 fsync，
     只记下写入触及的目录和新建的目录
  2. steps/NNN.json（StepRecord）；有 extra 文件时再写 steps/NNN.extra.json（ExtraFileIndex），
     同样只做数据 fsync
  3. 每个触及的不同目录 fsync 一次；路径上每个还没确认落盘的目录，其父目录也各 fsync 一次
  4. 原子重写 manifest.json（tmp → fsync → os.replace → fsync run 目录）← commit point

crash recovery:
  - 读 manifest → 知道哪些 step、哪些 trailing 条目已提交
  - manifest 没有列出的文件 → 未提交，对所有访问器都不可见（§5.5），安全忽略
  - 0–4 任何一处在 manifest rename 之前失败（包括目录 fsync 的真实 I/O 错误）→ 异常上抛，
    manifest 保持原样（磁盘上和 WALWriter 内存里都不变；写下的文件是孤立文件，同一次提交可以重试）。
    manifest rename 之后的目录 fsync 失败则先核对盘上 manifest；若已安装则同步内存、已提交路径和去重状态。
    核对失败时下一次写先 reconcile，不能以旧内存覆盖新索引
```

**"还没确认落盘"按 WAL 自己的记录算，不按"这次调用新建了谁"算。** 目录的名字是它父目录里的一个目录项，只有父目录 fsync 之后才落盘。WALWriter 记着哪些目录的名字已经由 `init_run` 或一次成功的提交确认落盘；一次提交写到的每个文件，其路径上不在这个集合里的目录，父目录都要 fsync。只看"这次新建了哪些目录"会漏掉重试：失败的那次已经把 `env/`、`env/steps/000/` 建出来了，重试时它们"已存在"，父目录就没人 fsync，manifest 却把它们下面的文件列成已提交，掉电后这些文件可能整个不见。失败封存段重试时仍使用原步骤身份和同一批目录；不能改用 `commit_trailing` 掩盖步骤缺口。

`WALWriter`（`core/trace/wal.py`）是纯 I/O 引擎，不认识 Hook/AOP。它的写接口只有下面这些；逐文件写入的 `write_observation` / `write_messages` / `write_step_record` 和 `commit_unfinished_messages` 已删除，不保留并存的旧路径：

```python
class WALWriter:
    def __init__(self, base_dir: Path, run_id: str) -> None: ...

    def init_run(self, config: TraceConfig, *, config_hash: str | None = None) -> None:
        """write_durable config.json（按需建 run 目录和 traces/），建 steps/ observations/ messages/ evals/，
        把这些目录项一批 fsync：run 目录，加上为它新建的每个目录的父目录（traces/；这个 run
        新建了 traces/ 时再加 traces/ 的父目录），然后才 atomic_write 初始 manifest
        （format_version=7，last_step_completed=-1，complete_through=-1，status=writing）。
        先有 config.json 才有 manifest：run 存在了，它的配置就一定在。之后的提交只 fsync run 目录
        之内的目录，run 目录自己的名字只在这里落盘。manifest 写成之前任何一步失败，都不留下 run。"""

    def commit_step(
        self,
        step: int,
        record: StepRecord,
        *,
        fragments: Sequence[EncodedFragment] = (),
        llm_calls: Sequence[EncodedLLMCall] = (),
        call_metrics: Iterable[TraceFile] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """按上面的 0–4 提交一步；返回即已落盘。manifest 的 complete_through 随之更新：
        从第 0 步起所选内容连续完整的最后一步；未恢复的步骤通道错误也会截断前缀。"""

    def commit_trailing(
        self,
        *,
        llm_calls: Sequence[EncodedLLMCall] = (),
        call_metrics: Iterable[TraceFile] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """同一协议，但不写 StepRecord、不推进 last_step_completed：
        manifest.trailing 追加一条 TrailingEntry。两样都为空时什么也不做。"""

    def write_result(self, result: AgentResult) -> None: ...   # atomic_write result.json
    def finalize(self, status: TraceStatus, *,
                 incomplete_reason: str | None = None, expected_steps: int | None = None,
                 recording_problems: Sequence[RecordingProblem] | None = None) -> None: ...   # 关闭 trace：complete，或 incomplete 加原因
```

参数类型来自 `core/trace/encoding.py`。它是落盘形状的唯一规则，`TraceWriter`、`RunTrace` 和测试 fixture 共用同一份（文件名与记录类型见 §5.3）：

```python
@dataclass(frozen=True)
class TraceFile:
    rel_path: str                    # run 目录下的 POSIX 相对路径
    data: bytes
    content_addressed: bool = False  # True：路径由 sha256(data) 决定，run 内去重
    meta: Mapping[str, Any] = field(default_factory=dict)   # 写进 ExtraFileRecord.meta

@dataclass(frozen=True)
class EncodedFragment:               # encode_observation(step, observation) -> list[EncodedFragment]
    key: str                         # 全限定 fragment key
    file: TraceFile                  # observations/NNN_<key>.json
    artifact: TraceFile | None       # observations/NNN_<key><suffix>
    failed: bool                     # True：file 里是 FragmentFailure

@dataclass(frozen=True)
class EncodedLLMCall:                # encode_llm_call(step, call, pending) -> EncodedLLMCall
    file: TraceFile                  # messages/NNN_call_KK.json
    output_format: TraceFile | None = None  # messages/output_formats/<sha256>.json（内容寻址）
    images: tuple[TraceFile, ...] = ()      # messages/images/<sha256><后缀>（内容寻址）：prompt 里内联的每张不同的图片
```

`encode_llm_call` 的 `pending`（`PendingLLMCall`）是 TraceWriter 在 `around_ainvoke` 里登记的那次调用：登记那一刻取下的 prompt 的 JSON 快照（之后 memory 或 serializer 再改 message 对象，记录不变）、按类缓存、已编码的请求 schema（`EncodedOutputFormat`：`OutputFormatRef` 加它指向的内容寻址文件）、开始时间，以及结局——由 `complete` / `fail` / `cancel` 中先到的那一个定下，同样在发生时取快照（§5.4）。快照由 `snapshot(value)` 取：pydantic model、dataclass、映射和序列按结构转换，bytes 记成 base64，其余 JSON 装不下的东西变成它的 `str()`，自身永不抛错。同一模块还有：

- `encode_llm_calls(step, pending, *, first_call=0)`：按开始顺序编号，从 `first_call` 起；TraceWriter 用它让一步的调用接在这一步已提交的 trailing 调用之后；
- `store_prompt_images(messages) -> StoredPrompt`：`encode_llm_call` 用它把 prompt 里内联的每张图片换成对存储文件的引用（规则见 §5.3 "调用记录里的图片只存一份"），得到换好的 messages、每个引用在 messages 里的位置（记录的 `image_refs`）和要提交的图片文件（`EncodedLLMCall.images`，同一次调用里重复的图片只列一次）。它从不抛错：走不完的 prompt 原样记录，图片留在原处，两种写法都无损。读侧的逆运算是 `restore_prompt_images(record, text_of)`（按 `image_refs` 把每个位置上的引用换回 `text_of(ref)` 给出的原文，就地修改，并去掉 `image_refs`；位置或引用不合法抛 `ValueError`）和 `prompt_image_text(ref, data)`（由存储文件的字节还原原文；该是 UTF-8 文本却不是时抛 `ValueError`）；
- `encode_output_format(output_format) -> EncodedOutputFormat`：回复模型的 `model_json_schema()`，规范化 JSON 加 sha256；不是 pydantic model 类就抛 `TypeError`；
- `encode_fragment(step, value)`：单个 slot，`encode_observation` 逐个调用它；
- 读侧用的 `decode_fragment(key, cls, payload, artifact)`：先核对 payload 的 `fragment_id`，再调 `from_trace`；
- `encode_archive_file(file, *, step=None, trailing=None, run_version=None) -> TraceFile`：把一个 `ArchiveFile` 编码成 extra 文件，按它的文档化字段读（鸭子类型，与 `TraceNegotiation.from_results` 同样的理由：`core/trace` 不 import `core/archive` 的类型）。路径：`scope == "run"` → 必须给 `run_version`，`archive_run_path(capability_id, run_version)`；`content_addressed` → `archive_blob_path(sha256(data))`；其余 → `step` 与 `trailing` 恰好给一个，`archive_step_path(step, archiver_id, name)` / `archive_trailing_path(trailing, archiver_id, name)`；不满足抛 `ValueError`。同样抛 `ValueError` 的还有：`step` 与 `trailing` 同时给出（一个文件只属于一次提交）、不是 run 记录却给了 `run_version`、内容寻址的 run 记录、未知的 `scope`。因为是鸭子类型，`ArchiveFile` 自己会拒绝的这里再查一遍。`meta` = `{ARCHIVE_META_KEY: ArchiveMeta(...).model_dump(mode="json")}`（`ARCHIVE_META_KEY = "archive"`），`content_addressed` 照抄。

fixture 与 `RunTrace` 共用这些函数。

**路径校验**（第 0 步；任何一条不满足，都在写任何字节之前抛 `ValueError` 并点名路径）：

- POSIX 相对路径：不以 `/` 开头，不含 `\`，没有空段、`.` 段或 `..` 段（`encoding.check_relative_path`，读侧用同一条规则，§5.5）。每个通道都查，`encoding.py` 命名的文件也不例外：fragment 文件按 key 命名，而 `FragmentFailure.fragment_id` 是自由文本；下面"待在自己的目录里"那条只是前缀判断，`observations/000_x/../../../escaped.json` 照样通过，output schema 的父目录判断经 `PurePosixPath` 规范化，`.` 段和空段会被吃掉；
- extra 文件不得落在 WAL 自有的名字上：`manifest.json`、`config.json`、`result.json`，以及 `steps/`、`observations/`、`messages/`、`evals/` 之下的任何路径。这些名字由 WAL 和 `encoding.py` 自己决定。按第一段判断，所以 WAL 的文件名也不能被当成目录：`result.json/evidence.txt` 会建出一个叫 `result.json` 的目录，run 收尾时写 `result.json` 就会失败；
- 一个路径要么是文件、要么是目录，不能两者都是：`env/a` 和 `env/a/b` 不能同时出现，不论在同一次提交里还是跨提交（不区分大小写），WAL 自己的目录（`steps/`、`observations/`、`messages/`、`messages/output_formats/`、`messages/images/`、`evals/`）也不能被文件占用。不在校验里拦住，它会在写到一半时变成 `NotADirectoryError`；
- 已提交的文件不可变：非内容寻址文件不得与本 run 已提交的路径同名；
- 内容寻址文件的路径必须包含 `sha256(data)` 的完整十六进制摘要，允许按 `<前两位>/<其余>` 分片（如 M28 的 `env/blobs/sha256/ab/cdef…`）；
- `encoding.py` 命名的文件待在自己的目录里：fragment 文件和 artifact 在 `observations/` 下，调用记录在 `messages/` 下（不在 `messages/output_formats/`、`messages/images/` 里），output schema 必须内容寻址、直接位于 `messages/output_formats/` 下，prompt 图片必须内容寻址、直接位于 `messages/images/` 下；
- 同一次提交里同一路径只能出现一次（内容寻址文件除外，只写一次），同一步里同一个 fragment key 只能出现一次；`record.step` 必须等于 `step`，一个 step 只能提交一次；
- 以上比较一律按 `encoding.fold_path` 的键进行：macOS 默认的 APFS 既不区分大小写、也不区分 Unicode 规范化形式，`env/A.txt` 和 `env/a.txt` 是同一个文件，`café.json` 的预组合（NFC）与分解（NFD）两种写法也是同一个文件，trace 拷到这样的文件系统上也不能互相覆盖。键是 Unicode 的规范无大小写匹配（canonical caseless matching，Unicode §3.13 D145）：`NFD(casefold(NFD(path)))`。只做 `casefold` 漏掉规范化变体；先 NFC 再 `casefold` 仍会漏掉大小写折叠之后才分解出来的那些对（`Ś` 与 `ſ` + 组合尖音符）。

**内容寻址文件在 run 内去重。** WAL 在内存里记着本 run 已提交的内容寻址路径，只在 manifest 提交成功之后更新。再遇到同一路径就跳过写入：extra 文件照常列进本次提交的索引（同一个响应体会被很多步引用）；output schema 只在 `manifest.output_formats` 里出现一次，由引用它的每条调用记录指向（同一个回复模型会被几十次调用使用）；prompt 图片同样只在 `manifest.prompt_images` 里出现一次，由引用它的每条调用记录指向（保留全部截图的 agent，之后每一步的调用都带着同一张截图）。去重跳过的只是写入，摘要校验照做：同一路径换了字节再提交会被拒绝，否则索引会记下磁盘上那个文件并不具有的 size 和 sha256。

**为什么每个文件的数据 fsync 都保留，只合并目录 fsync。** WAL 的成功返回承诺这一提交已经落盘，Agent 的继续不以此为条件。只 rename、不 fsync 数据，掉电后 manifest 可能列着一个空文件，提交点本身就在说谎。数据 fsync 之前还要先把字节 flush 出 Python 的缓冲：`os.fdopen` 最多缓冲 `st_blksize`（APFS、ext4 上是 4 KiB），漏了 flush 或者把 fsync 挪到写之前，所有比它小的文件（step 记录、前几步的 manifest、调用记录、verdict）fsync 时都还是空的，字节要到 `close()` 才进内核。fsync 的次数和顺序都不变，只有 fsync 那一刻的文件大小看得出来，测试就按这个检查。目录 fsync 让 `os.replace` 和 `mkdir` 产生的目录项落盘；一个目录 fsync 一次，就覆盖本批在它下面产生的全部目录项，所以按目录合并不损失任何保证。省下的是逐文件的目录 fsync，M28 那种每步上百个证据文件时尤其明显。macOS（APFS）上 1000 个 1 KiB 文件一次提交约 0.2 s；macOS 的 `os.fsync` 不刷设备缓存，fsync 的真实代价在 Linux ext4/xfs 上。**TODO（Linux）**：这些数只在 macOS 的 APFS 上校准过，ext4/xfs 还没测过。1000 个文件一次提交的墙钟上限（默认 3 s）只是 `test_wal_batch_commit.py` 基准测试里的断言，运行时不强制，`SPECTRAGENT_WAL_COMMIT_BUDGET_S` 可覆盖；Linux 上的提交只会更慢，不会因此失败。

**目录 fsync 的 errno 策略**（`core/trace/fsio.py` 的 `fsync_dir`）：

- `os.name == "nt"`：跳过（Windows 打不开目录）；
- 目录 fd 上的 `os.fsync` 抛 `EINVAL` / `ENOTSUP` / `EOPNOTSUPP`：容忍，那是文件系统不支持目录 fsync；
- 其余一律原样上抛，包括 `os.open` 自己的失败（`EMFILE`、`EACCES`、`ENOENT`）和 `EIO`、`ENOSPC`。数据 fsync 和 `os.replace` 出错时本来就是这个策略：一个声称已落盘、其实没有把握的 manifest，比一次中断的 run 更糟。WAL 原样报告这类错误；记录器隔离它并核对 manifest，不改变执行结果。

macOS 上提交点的 `fcntl(F_FULLFSYNC)` 不启用。本机实测每次约 5.2–5.4 ms（`os.fsync` 约 0.06–0.14 ms）；测试套件约 1600 次 manifest 提交，会多出约 8.5 s，约占一次 85 s 全量运行的 10%。它也只在 macOS 上生效，而评测主要跑在 Linux 上。

`core/trace/fsio.py` 提供三个原语，WAL 和 evaluator 共用：

```python
def write_durable(path: Path, data: bytes) -> set[Path]:
    """同目录临时文件（os.open(..., 0o666)）→ write、flush → fsync(file) → os.replace；不 fsync 目录。
    按需建父目录。任何一步失败都删掉临时文件，path 上原来的文件保持完整。
    权限与普通写入相同：新文件是 0o666 经 umask 屏蔽，替换已有文件时沿用它的权限位。
    返回之后需要 fsync 的目录：path 所在目录，加上为它新建的每个目录的父目录。"""

def fsync_dir(path: Path) -> None:
    """按上面的 errno 策略 fsync 一个目录。以 O_DIRECTORY 打开：传进来的是文件时
    抛 NotADirectoryError，而不是默默 fsync 那个文件、把该落盘的目录项留在缓存里。"""

def atomic_write(path: Path, data: bytes) -> None:
    """单文件持久写：write_durable，再对返回的每个目录 fsync_dir（深的先）。
    manifest、result.json 和 evals/ 都走它；它是唯一公开的原子写 helper。
    config.json 不走它：init_run 用 write_durable 写下它，建好子目录，再一批 fsync run 目录和
    为它新建的每个目录的父目录。"""
```

**文件权限就是普通写入会给的权限。** 临时文件用 `os.open(..., 0o666)` 创建，由内核按 umask 屏蔽；替换已有文件时，写入第一个字节之前先把临时文件改成原文件的权限位，所以它从来不比被替换的文件更宽。不用 `tempfile.mkstemp`：它固定 `0o600`，适合临时数据，不适合马上要 rename 到位的文件：用了它，每个 trace 文件和每个 verdict 都只有属主可读，另一个用户（队友、看板、CI 上传）读共享的 `--evals-dir` 拿到 `PermissionError`，重新打分还会把运维特意设的权限改回 `0o600`。读 umask 也不行：`os.umask` 只能"设置并返回旧值"，会和其他线程创建文件互相竞争。trace 是否私有，由 umask 或目录权限决定，和进程写的其他文件一样；需要私有的 trace（`record_secrets` 打开时）放在私有目录里。

**不属于任何已提交 step 的文件走 `commit_trailing`。** 它落盘 Loop 没来得及交出的那一步的 LLM 调用（超时、外部取消、崩溃），以及 M28 在 run 结束时冲刷的环境证据。它走同一套校验、同一种批量 fsync、同一个提交点，但不写 StepRecord，也不推进 `last_step_completed`：声称一个完成的 step，会在 manifest 里放进一个没有动作的幽灵步。`finalize(status, incomplete_reason=...)` 关闭 trace：`complete`，或 `incomplete` 加上没记下来的是什么（`writing` 不是终态，`incomplete` 没有原因、`complete` 带着原因都被拒绝）；停在 `writing` 的 trace 没有任何下游会用（evaluator 没有结果可读，分叉和导出没有可用的步）。

**环境证据走同一个提交。** 归档文件是普通的 extra 文件，WAL 不认识它们（接口仍然只有 `TraceFile`）：

- 一步的 `after_record_step` 在 manifest 提交之前拉取的批次，随**这一步**的 `commit_step(..., extra_files=...)` 落盘，列在 `steps/NNN.extra.json`；内容寻址的文件由 WAL 在 run 内去重，每个引用它的步的索引都列它。拉取失败、批次被拒或带缺口时这一步照样提交，没收到的那部分证据记成这一步的记录问题（§5.4），WAL 照常写它带着的那些文件。
- 已交出步骤的失败段始终按原 step 提交，不能重新编码成 trailing。只有未交出步骤的调用与收尾的环境批次分别使用 `commit_trailing`。每个 trailing 段携带稳定 `segment_id`；补写先核对该身份，不能把后来复用同一位置的别的段误认为本段已提交。
- `complete_through` 是连续完整前缀，`expected_steps` 保存执行交出范围，`missing_steps` 是该范围内实际未提交的步。`steps` 保留洞后的已提交项；数量取 `len(steps)`，不能取最大索引加一。补齐全部缺口可以恢复完整，历史 `recording_problems` 保留。
- 段已安装但提交点目录 fsync 报错时，WAL 核对实际 manifest 并同步引用索引。无法核对时停止覆盖，后续写先 `reconcile()`。run 归档版本也随实际提交核对，不能用旧版本表覆盖后来的证据；browser WARC 的 revisit 只引用同一采集批次中自包含的 response。

### 5.3 目录结构

```
traces/
  ├── suite_index.json                  # 可选：后置生成的聚合索引
  │
  └── {run_id}/
      ├── manifest.json                 # 提交点（每次提交原子重写），format_version: 7；trace 自己那本账
      ├── config.json                   # TraceConfig 快照（init_run 写一次）
      ├── result.json                   # AgentResult（run 结束时写一次）；agent 那本账
      │
      ├── steps/
      │   ├── 000.json                  # StepRecord
      │   ├── 001.json
      │   ├── 001.extra.json            # 可选：第 1 步的 extra 文件索引（ExtraFileIndex）
      │   ├── trailing_00.extra.json    # 可选：第 0 次 commit_trailing 的 extra 文件索引
      │   └── ...
      │
      ├── observations/                 # Observation fragments（每步追加）
      │   ├── 000_browser.dom.json            # 文件名 = 全限定 fragment key
      │   ├── 000_browser.screenshot.json     # fragment JSON（不含 trace_json_exclude() 的字段）
      │   ├── 000_browser.screenshot.png      # artifact：trace_binary() 的字节，后缀取自字节头（jpeg 则是 .jpg）
      │   ├── 000_browser.browser_meta.json   # 采集失败的 optional fragment：FragmentFailure，索引里 failed: true
      │   ├── 000_loop.history.json           # loop 来源的 fragment 同样落盘
      │   ├── 001_docker.terminal.json        # SWE 场景
      │   └── ...
      │
      ├── messages/                     # 每次 LLM 调用一个文件（RecordedLLMCall）
      │   ├── 000_call_00.json
      │   ├── 000_call_01.json          # 同一步的第二次调用：parse 重试、校验失败后重问……
      │   ├── 002_call_00.json          # trailing：第 2 步没有提交，列在 manifest.trailing
      │   ├── ...
      │   ├── output_formats/
      │   │   └── <sha256>.json         # 请求的 output schema，run 内按内容寻址、只写一次
      │   └── images/
      │       ├── <sha256>.png          # prompt 里内联的图片（解码后的字节，后缀取自字节头），run 内只写一次
      │       └── <sha256>.txt          # base64 不是标准写法的 data: URL，原文照存
      │
      ├── env/                                  # 环境证据（经 extra_files 提交）；archive 为空的 run 没有它
      │   ├── run.<capability_id>.json          # capability 的 run 记录（环境指纹等，JSON）：第 0 版
      │   ├── run.<capability_id>.<k>.json      # 内容变化后再发出的第 k 版（k ≥ 1；已提交路径不可变）
      │   ├── blobs/sha256/<ab>/<rest>          # content_addressed 的归档文件，run 内去重
      │   ├── steps/NNN/<archiver_id>/<name>    # 第 NNN 步的提交取走的证据
      │   └── trailing/KK/<archiver_id>/<name>  # 第 KK 次 commit_trailing 的证据（收尾、提交失败遗留）
      │
      └── evals/                        # 评测结果（后置、原子写，可多次运行不同 judge）
          ├── exact_match_v1.json
          ├── llm_judge_gpt4o_v1.json
          └── llm_judge_claude_v2.json  # 新 judge 无需重跑 agent
```

**文件名规则**（`encoding.py` 是唯一出处）：

| 文件 | 路径 | 内容 |
|---|---|---|
| fragment JSON | `observations/{step:03d}_{key}.json` | `fragment.model_dump(mode="json", exclude=fragment.trace_json_exclude())`；采集失败时是 `FragmentFailure.model_dump(mode="json")` |
| fragment artifact | `observations/{step:03d}_{key}{suffix}` | `trace_binary()` 返回的字节，`suffix` 来自字节头 |
| LLM 调用 | `messages/{step:03d}_call_{call:02d}.json` | `RecordedLLMCall`；`call` 是本步内按开始顺序的序号，从 0 起；prompt 里内联的每张图片在原处换成 `PromptImageRef`，`image_refs` 列出它们的位置（下面"调用记录里的图片只存一份"） |
| output schema | `messages/output_formats/{sha256}.json` | 请求的 JSON schema，序列化为规范化 JSON（键排序、紧凑分隔符、UTF-8）；`sha256` 是这些字节的摘要 |
| prompt 图片 | `messages/images/{sha256}{suffix}` | `prompt_image_path(sha256, suffix)`；解码后的图片字节，`suffix` 来自字节头（`.png`、`.jpg`、`.gif`、`.webp`，认不出是 `.bin`）；或 `data:` URL 的原文（UTF-8），`suffix` 是 `.txt`；`sha256` 是文件字节的摘要，run 内只写一次 |
| step record | `steps/{step:03d}.json` | `StepRecord` |
| extra 文件索引 | `steps/{step:03d}.extra.json`、`steps/trailing_{n:02d}.extra.json` | `ExtraFileIndex`；`n` 是该条目在 `manifest.trailing` 里的位置 |
| 归档文件（步） | `env/steps/{step:03d}/{archiver_id}/{name}` | `archive_step_path(step, archiver_id, name)`；`ArchiveFile.data` 原样 |
| 归档文件（trailing） | `env/trailing/{n:02d}/{archiver_id}/{name}` | `archive_trailing_path(n, archiver_id, name)`；`n` 同上，是该条目在 `manifest.trailing` 里的位置 |
| 内容寻址的归档文件 | `env/blobs/sha256/{sha256[:2]}/{sha256[2:]}` | `archive_blob_path(sha256)`；run 内只写一次，`sha256` 是 `data` 的摘要 |
| run 记录 | `env/run.{capability_id}.json`（第 0 版）、`env/run.{capability_id}.{k}.json`（第 k 版，k ≥ 1） | `archive_run_path(capability_id, version)`；JSON |

归档路径的根是 `ENV_DIR = "env"`，四个函数都在 `encoding.py`。每个归档文件在它所在索引里的 `ExtraFileRecord.meta["archive"]` 是一份 `ArchiveMeta`（下面的记录类型）：读侧靠它知道一个 extra 文件是归档文件、属于哪个归档器、第几版 run 记录。

**manifest（format_version 7）**，以一个在第 2 步超时的 run 为例：agent 超时了（`result.json` 的 `agent_done: timeout`），trace 却是完整的——它记下了这个 run 做过的一切，被切断那一步的调用作为 trailing 落了盘：

```json
{
  "format_version": 7,
  "run_id": "f2bbae22",
  "last_step_completed": 1,
  "status": "complete",
  "incomplete_reason": null,
  "complete_through": 1,
  "created_at": "2026-10-10T08:00:00Z",
  "updated_at": "2026-10-10T08:05:00Z",
  "config_hash": "…",
  "steps": [
    {
      "step": 0,
      "step_file": "steps/000.json",
      "fragments": {
        "browser.dom":          {"file": "observations/000_browser.dom.json", "artifact": null, "failed": false},
        "browser.screenshot":   {"file": "observations/000_browser.screenshot.json",
                                 "artifact": "observations/000_browser.screenshot.png", "failed": false},
        "browser.browser_meta": {"file": "observations/000_browser.browser_meta.json", "artifact": null, "failed": true},
        "loop.history":         {"file": "observations/000_loop.history.json", "artifact": null, "failed": false}
      },
      "llm_calls": ["messages/000_call_00.json", "messages/000_call_01.json"],
      "extra_index": null
    },
    {
      "step": 1,
      "step_file": "steps/001.json",
      "fragments": {"…": "…"},
      "llm_calls": ["messages/001_call_00.json"],
      "extra_index": "steps/001.extra.json"
    }
  ],
  "trailing": [
    {"llm_calls": ["messages/002_call_00.json"], "extra_index": null}
  ],
  "output_formats": ["3b6f…"],
  "prompt_images": ["messages/images/9f2c….png", "messages/images/d41a….png"]
}
```

同一个 run 在第 2 步采集环境证据失败时，Agent 仍可执行后续步骤并最终 done；第 2 步照样带着 StepRecord、观测与调用提交，只是没有它没收到的那部分证据（某一个归档器失败时，其余归档器的证据照样在），manifest 记录 `missing_steps: []`、`complete_through: 1`、一条第 2 步的记录问题（通道是丢了证据的归档器，或 `archive`），保留第 3 步之后实际提交的内容。`missing_steps` 只列真正没提交的步（写入失败、缓冲满）。result.json 里的真实执行结局不因记录错误改变，metrics.recording_health 独立说明记录健康。

**两本账（用户 2026-10-13 的裁决）。** `result.json` 只说 agent 怎么结束：done / max_steps / error / timeout / vetoed（`AgentDoneStatus`）。manifest 只说 trace 记全没有：

| `status` | 含义 | 谁写 |
|---|---|---|
| `writing` | trace 未关闭；可能仍在运行或进程未完成收尾，已提交索引仍可供查看 | `init_run` |
| `complete` | 已关闭，在声明记录范围内完整；已恢复的历史故障仍可见 | `finalize` |
| `incomplete` | 已关闭，但有东西没记下来：某一步选择的内容或它的某一路归档证据（run 可继续）、收尾那次收集（含超出 `closeout_collection_timeout`）、收尾补写的某一样欠账、`result.json`；`incomplete_reason` 写明是什么（每一项 `<通道>: <错误类>: <原因>`，几样按发生顺序用 `; ` 连起来，第一个洞在前），`complete_through` 说完整到第几步 | `finalize` |

`complete_through` 表示声明记录范围内连续完整到哪一步；-1 表示没有完整前缀。已提交步缺少某个所选通道也会截断前缀，不只检查 step 文件是否存在。`missing_steps` 列实际尚未提交的步，后续提交仍保留；成功补齐缺口后前缀可以前进。无步骤归属的 trailing 故障不抹掉已有完整前缀。记录健康与 WAL 共用 `recorded_prefix` 规则，查看器和指标可读取缺口后的已提交内容并显示覆盖不足，分叉和导出则要求声明范围与所用前缀完整。当前只读取 format 6，旧数据和 fixtures 重建，不保留兼容路径。

**记录类型**（`core/trace/models.py`，`TRACE_FORMAT_VERSION = 5`）：

```python
class FragmentFiles(BaseModel):
    file: str                          # observations/NNN_<key>.json
    artifact: str | None = None        # observations/NNN_<key><suffix>
    failed: bool = False               # True：file 里是 FragmentFailure，不是 fragment

class StepManifestEntry(BaseModel):
    step: int
    step_file: str                                          # steps/NNN.json
    fragments: dict[str, FragmentFiles] = Field(default_factory=dict)  # 全限定 key → 文件
    llm_calls: list[str] = Field(default_factory=list)      # 完整调用内容，按开始顺序
    call_metrics: list[str] = Field(default_factory=list)    # 不需要还原 prompt 的轻量调用事实
    extra_index: str | None = None                          # steps/NNN.extra.json；无 extra 文件为 None

class TrailingEntry(BaseModel):                             # 每次 commit_trailing 追加一条
    llm_calls: list[str] = Field(default_factory=list)
    call_metrics: list[str] = Field(default_factory=list)
    extra_index: str | None = None                          # steps/trailing_KK.extra.json

class TraceStatus(StrEnum):            # trace 自己那本账，不是 agent 的
    WRITING = "writing"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"

class TraceManifest(BaseModel):
    format_version: int = 1            # 缺字段仍可识别为旧数据；init_run 写 7，RunTrace 只接受 7
    run_id: str
    last_step_completed: int = -1            # 最大已提交步号，不是计数
    expected_steps: int = 0
    missing_steps: list[int] = Field(default_factory=list)
    recording_problems: list[RecordingProblem] = Field(default_factory=list)
    status: TraceStatus = TraceStatus.WRITING
    incomplete_reason: str | None = None     # 恰在 incomplete 时有值
    complete_through: int = -1               # 完整到第几步；commit_step 维护
    created_at: UTCDatetime
    updated_at: UTCDatetime
    config_hash: str = ""
    steps: list[StepManifestEntry] = Field(default_factory=list)
    trailing: list[TrailingEntry] = Field(default_factory=list)
    output_formats: list[str] = Field(default_factory=list)  # 已提交的 output schema 的 sha256
    prompt_images: list[str] = Field(default_factory=list)   # 已提交的 prompt 图片：messages/images/<sha256><后缀>，每张一次，按提交顺序

class ExtraFileRecord(BaseModel):      # extra 文件索引里的一条
    path: str                          # run 目录下的相对路径
    size: int
    sha256: str
    content_addressed: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)      # 生产者自带的元数据；归档文件：meta["archive"] = ArchiveMeta

class ExtraFileIndex(BaseModel):       # steps/NNN.extra.json、steps/trailing_KK.extra.json
    files: list[ExtraFileRecord]

class ArchiveMeta(BaseModel):          # ExtraFileRecord.meta["archive"]：一个归档文件的来源
    archiver_id: str                   # 全限定 archiver key；run 记录也可以是裸 capability id
    name: str                          # 归档器自定的文件名（单个路径段）
    media_type: str
    scope: Literal["step", "run"]
    captured_at: float                 # 墙钟 Unix 时间，与 StepRecord.timestamp 可比
    cycle: int | None = None
    ids: dict[str, str] = Field(default_factory=dict)       # capability 作用域的标识（session / frame / loader …）
    run_version: int | None = None     # 仅 run 记录：第几版，从 0 起（校验：run 记录必有且 ≥ 0，步级文件必无）

class ArchiveRecord(BaseModel):        # 读侧：一条 ExtraFileRecord 与它的 ArchiveMeta 摊平
    path: str
    size: int
    sha256: str
    content_addressed: bool
    archiver_id: str
    name: str
    media_type: str
    scope: Literal["step", "run"]
    captured_at: float
    cycle: int | None = None
    ids: dict[str, str] = Field(default_factory=dict)
    run_version: int | None = None
    @property
    def capability_id(self) -> str: ...   # archiver_id 的第一段

class OutputFormatRef(BaseModel):
    name: str                          # 回复模型的类名
    sha256: str                        # → messages/output_formats/<sha256>.json
    source: Literal["requested"] = "requested"   # 请求的 schema，不是 provider 实际发出的那份

class RecordedCompletion(BaseModel):
    completion: Any                    # 文本回复是 str；结构化回复是它的 JSON 形式，不还原成回复模型
    thinking: str | None = None        # 响应带回的模型推理（§4.7）；没有就是 None
    redacted_thinking: str | None = None   # 只以不透明形式返回的推理（Anthropic 的 data、Bedrock 的 base64……）
    usage: ModelInvokeUsage | None = None
    stop_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

class CallFailure(BaseModel):
    error: str                         # 异常类名
    detail: str                        # str(exc)
    raw_output: str | None = None      # 校验器看到的原文（StructuredOutputValidationError）
    partial_output: str | None = None  # 被截断的回复已经生成的部分（ModelOutputTruncatedError）
    stop_reason: str | None = None
    usage: ModelInvokeUsage | None = None

class RecordedChatCall(BaseModel):     # messages/NNN_call_KK.json
    kind: Literal["chat"]              # required; no old-format default
    owner: Literal["run", "replay_review"]
    owner_id: str
    step: int                          # 调用时 Loop 所在的 step；trailing 调用 = 没提交的那一步
    call: int                          # 本步内按开始顺序的序号，从 0 起
    status: Literal["completed", "failed", "cancelled"]
    started_at: float                  # 墙钟 Unix 时间，与 StepRecord.timestamp、captured_at 可比
    latency_ms: int                    # 从开始到返回 / 抛出 / 取消
    messages: list[RecordedMessage]    # 发给模型的完整 prompt；= Annotated[BaseMessage, Field(discriminator="role")]
    output_format: OutputFormatRef | None = None   # None = 非结构化调用
    completion: RecordedCompletion | None = None   # 仅 completed
    failure: CallFailure | None = None             # failed、cancelled

class PromptImageRef(BaseModel):       # 盘上的调用记录里，一张内联图片所在的位置放的是它
    model_config = ConfigDict(extra="forbid")
    stored_image: str                  # messages/images/<sha256><后缀>，必须列在 manifest.prompt_images 里
    prefix: str | None                 # 字符串：data: URL 到逗号为止（"data:image/png;base64,"），文件是解码后的字节，
                                       #   原文 = prefix + 字节的标准 base64；None：文件就是原文（UTF-8）
```

盘上的调用记录比 `RecordedLLMCall` 多一样东西：prompt 里带了内联图片时，`messages` 后面有 `image_refs`，列出每个 `PromptImageRef` 在 `messages` 里的位置（从 `messages` 起的键与下标，如 `[1, "content", 1, "image_url", "url"]`）；没有内联图片的调用不写这个键（有值才写）。`RunTrace` 按它把原文放回去之后才按 `RecordedLLMCall` 校验，所以读回来的永远是完整的 prompt；绕过 `RunTrace` 直接按 `RecordedLLMCall` 校验盘上的记录会失败（`url` 的位置是一个对象，不是字符串），不会悄悄拿到一个缺了图片的 prompt。

**当前调用格式为 v6。** `RecordedLLMCall` 是以必填 `kind` 判别的
`RecordedChatCall | RecordedDecisionCall`，共享 owner/owner_id、call_id、caller/purpose、
model、时间、状态与 failure。Chat 保留 messages/output_format/completion；Decision
保存完整 request（state/questions/assets）与原生 result。run 的 step 为整数，独立
审查的 step 为 null，不能把证据来源伪装为执行身份。kind 与 purpose 独立，恢复和
导出明确拒绝 Decision 主决策，跳过 helper；不把 helper 重新调用或交给 Agent parser。
metrics-only 不复制 request/图片；Decision assets 的 data_base64 同样内容寻址，
引用位置从 request 根开始，所有其他字符串不被当作图片。读取恢复原文并校验哈希。
旧 v5 reader 不在生产中兼容；升级前在当前工具下冻结历史输入和摘要，旧轨迹不回写。
AgentResult 结构未变，仍为 3.0。

**调用记录里的图片只存一份（用户 2026-10-14 的裁决）。** 调用记录存的是整份 prompt，而 memory 保留全部截图的 agent（UI-TARS）每次调用都带着之前的每一张：第 k 次调用把前面 k 张截图再存一遍。实测一个 12 步的 UI-TARS 录制：`messages/` 里存了 78 份截图（32.9 MB），不同的只有 10 张（3.9 MB，按 base64 计），同一个 run 的 `observations/` 是 3.5 MB；三个站点的冒烟链共 305 MB，其中 127 MB 是调用记录。按下面的规则重新编码同一批记录：这个录制的 `messages/` 从 33.8 MB 降到 3.1 MB（调用记录 0.1 MB，10 张图片解码后 3.0 MB），整条链的调用记录与图片从 129.7 MB 降到 46.6 MB，报告按每个任务各段合计的字节从 102 MB 降到 74 MB；57 次调用读回来与原记录逐字节相同。现在：

- **什么算内联图片**：`messages` 的 JSON 里，任何 `image_url` 键下的对象的 `url`，只要它是 base64 的 `data:` URL（`data:<任意参数>;base64,<载荷>`，不区分大小写）。这就是 af 的消息模型放图片的地方（`ContentPartImageParam.image_url.url`，user 与 tool 消息都一样）。按 URL 发的图片、文字里出现的 `data:` 字样、`image_url` 之外的任何字符串都原样留在记录里。
- **存什么**：载荷解码之后，再按标准 base64 编码回去恰好是原来的载荷、而且解码出的字节不为空，就存解码后的字节（`messages/images/<sha256><后缀>`，后缀取自字节头，认不出是 `.bin`），引用的 `prefix` 记下逗号之前（含逗号）的原文；否则（换行、缺填充、URL 安全字母表、尾部多余的位、非 ASCII 字符）存原文本身（`messages/images/<sha256>.txt`，UTF-8），`prefix` 为 `None`。原文连 UTF-8 都编码不了（孤立的代理字符）时图片留在原处。哪一种都是无损的：读回来的原文与写进去的逐字符相同。`sha256` 是文件字节的摘要：截图以 PNG 发出时，它与该步截图 artifact 的摘要相同。
- **只存一次**：图片文件是内容寻址的，与 output schema 同一个机制（§5.2）：随第一次带着它的提交落盘，列进 `manifest.prompt_images`，之后的提交只校验、不再写。图片文件与调用记录同属一次提交的内容物，在提交点之前带着各自的数据 fsync 落盘；提交点之前崩溃，写下的图片与调用记录一样是孤立文件，对每个访问器都不可见（§5.5）。
- **写侧从不因此失败**：`store_prompt_images` 只认上面那一种形状，走不完的 prompt（嵌套深到递归走不下去之类）原样记录，图片留在原处。它是编码层的事：TraceWriter 和 Loop 都不知道它。
- **读侧**：`RunTrace` 每读一条调用记录，就按 `image_refs` 读回每张图片、核对摘要、把原文放回原处（`restore_prompt_images`），然后才校验；每次读都核对，不缓存。引用的图片不在 `manifest.prompt_images` 里（从没提交过）、位置不对或引用不合法，`TraceSchemaError` 点名调用记录；图片文件缺失、字节与名字里的摘要不符、该是 UTF-8 的原文不是，点名图片文件。
- **代价**：读一条调用记录要多读它引用的图片文件（与原先从调用记录里解析同样多的 base64 相比不更慢；冒烟链 57 次调用经 `RunTrace` 读回共约 0.2 s）；第 k 次调用的编码要解码并哈希 k 张图片（12 步共 78 张、约 30 MB 的 base64，实测约 0.1 s）。图片只在一个 run 之内去重：分叉与原路径回放的 run 各自存一份它重建的前缀里的截图，调用记录里的图片也不去引用同一个 run 已经存下的观测截图（字节相同、摘要相同，但那是另一种文件）。

**fragment 索引进 manifest，extra 文件索引不进。** 每步的 fragment 和 LLM 调用只有几个，放进 manifest，读者一次 eager 读就知道每步记录了什么。extra 文件（M28 的环境证据）每步可能上百个；manifest 每次提交都整份重写，每个 `RunTrace` 又都 eager 读它，把这些文件内联进去会让 manifest 随 run 长度二次增长。所以每步一个 `steps/NNN.extra.json`，manifest 只记它的路径（`extra_index`）。

**失败的 fragment 显式标记。** `FragmentFailure` 的 JSON 没有判别字段，而 `ScreenshotObs` / `DOMObs` / `BrowserMetaObs` 的字段都有默认值，同一份 JSON 能被它们静默接受（已验证）。按 key 类型化读回时，一次失败的采集就会变成一个空 fragment，正是 `failure.py` 要拒绝的伪造。所以失败落在同一个文件名下，fragment 索引带 `failed: true`；读侧见到它就返回 `FragmentFailure`，从不尝试按 fragment 类解析。

**fragment JSON 就是 fragment 自己的 JSON。** `file` 的内容恰好是 `model_dump(mode="json", exclude=trace_json_exclude())`，不再往里塞 `artifact_file`：二进制文件只由索引的 `artifact` 指向，WAL 的布局信息不混进 fragment 的数据。读侧用 `from_trace(payload, artifact)` 还原（§5.5）。

**每次 LLM 调用一个文件，不论结局。** 一步可能有多次调用（ReactLoop 的 parse 重试、结构化输出校验失败后重问），它们按开始顺序编号、全部提交。`completed` 记 `RecordedCompletion`，`failed` / `cancelled` 记 `CallFailure`（字段语义见 §5.4）。写侧把登记时拿到的 prompt 直接序列化成 JSON（内联图片换成引用，见上面"调用记录里的图片只存一份"），不做 model 校验，hook 永远不会因为 trace 的类型问题让 run 失败；读侧把图片放回去之后按 `RecordedLLMCall` 校验，不合法就是 `TraceSchemaError`。Loop 没来得及交出的那一步，其调用沿用执行时分配的稳定步号（绝不从最后提交索引反推），由 `commit_trailing` 提交、列在 `manifest.trailing`。驱动冲刷之后若继续跑并提交了这一步，它的调用编号接在已提交的 trailing 调用之后，已提交的文件不会被覆盖。

**调用记录带着模型的推理，前提是响应带回了它。** `RecordedCompletion.thinking` / `redacted_thinking` 就是 provider 填进 `ChatInvokeCompletion` 的那两个字段（来源与合成规则见 §4.7），写侧照录，读侧原样读回；响应没带推理的调用两者都是 `null`。它只是记录：请求从不为了拿到推理而改动，推理也从不回喂给模型，写进记忆、带进下一轮提示的只有 `completion`（§4.7）。失败的调用记 `CallFailure`，没有这两个字段，它的推理不记。

**output schema 记的是请求的那份。** `OutputFormatRef.source = "requested"`：文件内容是回复模型的 `model_json_schema()`，不是 provider 编译后真正发出的 schema（strict 与否、Gemini 的清理、union 分支的展开，各家不同；schema 写进提示词的路径不发 `json_schema`）。它不能当作"schema 被遵守了"的证据。记录 wire 级 schema 是后续工作。

**不是 fragment 的 observation 不落盘。** 旧版把它整个写成 `NNN_observation.json` 兜底，没有任何访问器能读回；生产路径总是 `NegotiatedObservation`。这条兜底已删除，那一步的 fragment 索引为空。

**`env/` 是环境证据的子树。** 它建在 M27 的通用 extra 文件机制上（任意不撞 WAL 自有名字的相对路径、每步一个索引、内容寻址文件 run 内去重、`commit_trailing` 收尾），WAL 没有为它加任何东西。布局就是上面那棵树，语义如下：

- **只在开启时存在。** 没有绑定归档器的 run 不会有任何组件往 `env/` 写文件，目录根本不出现；绑定了但从没收集到文件的 run 也没有它。
- **步级文件归取走它的那一步。** `env/steps/NNN/` 下是第 NNN 步的提交取走的证据，路径段是全限定的 `archiver_id`（provenance）和归档器自定的 `name`。这也是 capability 侧事件的步归因：例如回放 run 的 `env/steps/NNN/browser.replay_log/replay.jsonl.gz` 是第 NNN 步收集到的回放决定（M30，§4.14），capability 自己从不知道步号。
- **内容寻址的文件只存一份。** 同一个响应体被很多步引用：文件在 `env/blobs/sha256/` 下只写一次，每个引用它的步的索引各列一条。
- **trailing 证据不冒充任何一步。** 收尾时的尾部、以及一次提交失败遗留的批次，落在 `env/trailing/KK/`，`KK` 与 `steps/trailing_KK.extra.json` 是同一个位置编号。
- **run 记录按版本提交。** capability 的 run 级证据（环境指纹等）属于 capability，不属于某个归档器。已提交的路径不可变，所以内容变化后的再次发出写成新的一版：第一版是 `env/run.<capability_id>.json`，之后依次是 `.1.json`、`.2.json`……；与最新一版字节相同的再发被丢掉。版本号记在索引的 `ArchiveMeta.run_version` 里，所以 trailing 提交与步提交交错也不会把顺序弄乱。run 记录随紧接着的那次提交落盘，读侧取已提交的最高版本（§5.5）。版本保持采集顺序，不跟提交顺序走：提交失败遗留的批次在收尾才提交，晚于之后采集的批次；它的 run 记录若比该 capability 已提交的最新一版采集得早，就不再提交（记一条日志），否则这份过时的指纹会成为最高版本，回放与分叉会照它重建环境（§5.4 "run 记录版本"）。
- **崩溃窗口照旧。** manifest 没有列出的 `env/` 文件（崩溃在 rename 之前、或手工放进去的）对每个访问器都不可见，和其他 extra 文件一样。

**`TraceConfig` / `TraceNegotiation` 的归档字段**（`core/trace/models.py`；都是带默认值的新增字段，不升 `format_version`）：

```python
class TraceNegotiation(BaseModel):
    ...                                                     # fragment / action 字段不变
    active_archivers: list[str] = Field(default_factory=list)      # 本 run 绑定的归档器，全限定、有序
    archive_bindings: dict[str, str] = Field(default_factory=dict) # archiver key → capability_id，按键有序

class TaskSetupRecord(BaseModel):      # 交给 setup_environment 的环境 setup context，脱敏后
    context: dict[str, Any]            # encoding.snapshot(context) 再 redact_value；快照不是 JSON 对象时记成 {"value": <snapshot>}
    redactions: RedactionLedger        # core/archive/privacy.py；record_secrets 打开时为空台账

class TraceConfig(BaseModel):
    ...                                                     # identity / resolved / negotiation / secrets_recorded 不变
    extra: dict[str, Any] = Field(default_factory=dict, exclude_if=<空>)          # 调用方给的任务上下文
    env_evidence_privacy: list[str] | None = Field(None, exclude_if=<None>)    # 所绑定归档器 privacy_markers 的有序并集；没绑定归档器时为 None
    task_setup: TaskSetupRecord | None = Field(None, exclude_if=<None>)        # None：execute() 没有收到 env_context
```

**按任务、按功能的字段，有值才写（用户 2026-10-13 的裁决）。** `branch` 只在分叉 run 里写，`task_setup` 只在任务带着 setup context 起的时候写，`env_evidence_privacy` 只在绑定了归档器时写（绑定了、但归档器什么标记都没声明时写 `[]`），`extra` 只在调用方给了时写。普通 run 的 `config.json` 里因此没有 `"branch": null` 这类占位。读侧不用改：缺的键读回来就是字段的默认值，也不升 `format_version`。原先的 `env_evidence_recorded` 删掉了：它与 `negotiation.active_archivers` 是否为空重复，也没有读者。

`TraceNegotiation.from_results(observation=None, action=None, archive=None)` 多收一个归档结果，三个都为 `None` 时才返回 `None`；绑定表原样记录（值都是 registry key，不是用户起的实例 id），排序的理由与另外两条轴相同。`task_setup` 回答的是"这个任务的环境是怎么起的"：分叉任务（M31）要靠它在没有源 benchmark 数据的情况下重建环境，所以从 M28 起就记录；它是按任务变化的数据，脱敏规则见 §5.6；其中相对的 `replay_archive` 在记录之前已经解析成绝对路径（§4.10）。`env_evidence_privacy` 是 provenance，和 `secrets_recorded` 一样回答"这个目录能不能直接分享"。

**摘要（`config_hash`）不为归档开任何特例。** 归档设置经两条现成的路进入 `configuration_digest`：`negotiation`（`active_archivers` / `archive_bindings`）和 `resolved`（M29 起归档器的旋钮在 trace hook 的配置里）。`task_setup` 按任务变化，`env_evidence_privacy` 是 provenance，都不进摘要，所以同一次 sweep 里只差 `start_url` 的两个任务共享一个 `config_hash`。代价写明：`TraceNegotiation` 多了两个字段，M28 之后写的 trace 与同一配置在 M28 之前写的 trace 摘要不同；读者比较的是同一个框架版本写下的 manifest。

**`evals/` 也是原子写。** `EvaluationRunner` 写 `evals/<verdict_slug>.json`，以及可选的平铺副本和 summary，都走 `fsio.atomic_write`：半写的 verdict 文件会被下一个读者当成损坏的评测。权限与普通写入相同（§5.2），共享的 `--evals-dir` 对其他用户照常可读，重新打分也不改动已有文件的权限。evals 是后置产物，不进 manifest。

**步的来源与恢复校验（M31）**（`spectragent/schemas/step_record.py`、`spectragent/schemas/restoration.py`）。`StepRecord` 多两个带默认值的字段，按 §4.4 的演进规则属于 BACKWARD ✓：

```python
class StepRecord(BaseModel):
    ...                                                       # 原有字段不变
    origin: Literal["policy", "restored", "terminal"] = "policy"
    restoration: RestorationCheck | None = None

RestorationReason = Literal["document_miss", "url_mismatch", "retarget_not_found",
                            "retarget_ambiguous", "action_failed", "signature_mismatch",
                            "media_mismatch", "screenshot_distance"]   # 也是判定 reason 的先后顺序

class RestoredAction(BaseModel):          # 恢复时一个录下的动作的去向
    recorded: Action                      # 源 StepRecord 里的那个（`guarded_act` 留下的名字，§4.1）
    executed: Action | None               # 实际交给 act_fn 的；strict 下重定位失败时为 None（没执行）
    method: RetargetMethod                # identity | geometry_signature | tab_order_url
    failure: Literal["not_found", "ambiguous"] | None = None
    detail: str = ""
    recorded_success: bool | None         # 源结果的 success；源没有结果时 None
    live_success: bool | None = None
    live_error: str | None = None

class RestorationCheck(BaseModel):
    step: int
    verdict: Literal["ok", "warn", "diverged"]    # warn / diverged：verify 是 warn / strict 时的失败
    reason: RestorationReason | None = None       # reasons 的第一个
    reasons: list[RestorationReason] = []         # 每个不通过的检查，按上面的顺序
    evidence: list[RestorationEvidence] = []      # 每个实现了 RestorationSupport 的 capability 一份（§4.9）
    actions: list[RestoredAction] = []            # 这一步录下的动作；第 t 步（策略步的起点）为空
```

- `origin`：`policy` 是 Loop 自己的 agent 决定的一步；`restored` 是分叉 run 恢复出来的前缀步（实际 retarget 后动作及结果，旧回复只在 `source_history` 来源材料中，§4.1 `BranchLoop`）；`terminal` 是终止观测（§4.1，没有动作、没有 LLM 调用）。`AgentMetrics.steps` 与 `max_steps` 只数 `policy`。
- `restoration`：恢复出来的第 k 步带它自己的校验；分叉的策略步（第 t 步）带 obs_t 的校验，因为这一步是在那个被校验过的状态上做的决定；`strict` 下发散的那一条（`origin="restored"`、`error_phase="restore"`）带不通过的那次校验。原因的判定：capability 证据里 `document_misses > 0` → `document_miss`；`url_match is False` → `url_mismatch`；重定位 `not_found` / `ambiguous` → `retarget_*`；动作成败与录下的不同、或执行抛错 → `action_failed`；`signature_match is False` → `signature_mismatch`；媒体证据不通过（`media.comparisons` 里有 `missing` / `extra` / `ambiguous` 或直播，`matched` 元素的位置差超过 `media_time_tolerance_s` 或暂停状态不同，比较成立时任一边有读不到的 frame，或 `media.alignment` 里有 `failed` / `not_restorable`；§4.14 "恢复：重定位与校验"，2026-10-20 裁决；证据带 `media_areas` 时不判，§4.14 "页面上的媒体区域"，2026-10-23 裁决）→ `media_mismatch`；`screenshot_distance > screenshot_max_distance` → `screenshot_distance`（媒体区域规则声明的页面上是遮掉媒体框之后的距离）。值为 `None` 的量不参与判定。
- 当前 `AgentResult.schema_version` 为 `5.0`（4.0：`agent_reported_success` 取自结束动作结果的 `reported_success`，§4.1"agent 的任务结论"；5.0：删掉 `action_history`，它是 trace 出现之前留下的、把每个动作拼成 Python repr 字符串的副本，动作只在步记录里，评测读 `TraceView.action_history`），trace `format_version` 为 `7`（7：每一步记下它的坐标系 `StepRecord.frame`，run 配置里的动作空间带 `coordinate_space`，§4.1"坐标系归框架"；6 的步没有说它的回复在哪个网格上写点，读取时拒绝）；执行耗时、未知用量、记录范围与稀疏提交采用同一当前合同。旧数据可重建，不为保留旧 trace 维持格式或别名。
- 这两个字段都不渲染进 prompt：`StepRecord` 是 `HistoryObs` 的载荷，agent 只渲染动作、结果与回复（`test_the_stamp_does_not_reach_the_prompt` 同样钉住 `origin` / `restoration`）。

**步的坐标系（2026-10-23）**：`StepRecord.frame: PointFrame | None`（`schemas/observation/geometry.py`），JSON 为 `{"space": ..., "grid": {"space", "width", "height", "viewport_width", "viewport_height"} | null}`：这一步写点的网格铺在它给模型看的截图上（§4.1"坐标系归框架"）。Loop 在 Agent 决定的步上写它（解析失败的步也写），restored 历史下的恢复步写本次恢复观测的坐标系；`None` 表示 run 的点就是 capability 的单位，或这一步没有给模型看过点（终止观测、prompt 之前就失败的步、source 历史下的恢复步）。它随 `HistoryObs` 到 Agent 手里，与 `origin` / `restoration` 一样不是渲染的内容，而是渲染这一步的点要用的坐标系。trace 读者靠它把 `llm_output` 里的点与 `actions` 的点对上，不用知道 agent 的配置。

**步里没执行的动作（用户 2026-10-24 的裁决）**：`StepRecord.skipped_actions: int = 0`，`actions` 末尾有几个动作 Loop 因为它们之前的那个动作失败而没执行（§4.1"一步里有动作失败，这一步就停下"）：它们没有结果，`action_results` 与前 `len(actions) - skipped_actions` 个动作配对，最后一个结果是失败的。带默认值的字段，trace 格式不升，缺少它就是 0：这次改动之前的 Loop 在失败之后照样执行后面的动作，所以那时录下的每一步确实什么都没跳过。恢复步只在恢复重现了源的停下时带它（§4.1 `BranchLoop`"restored 历史"）。它随 `HistoryObs` 到 Agent 手里，Agent 据它写"没执行"那一句。

**`TraceConfig.branch`：分叉来源（M31）**。按 §5.4 "记录一个新的 run 级事实" 的步骤：

```python
class TraceConfig(BaseModel):
    ...
    branch: BranchProvenance | None = Field(None, exclude_if=<None>)   # None（不写）：不是分叉 run
```

- 由 `TaskContext.branch` 而来，`AgentRuntime.execute(branch=...)` 按名字传给 `on_run_start`，`TraceWriter` 按名字接收、原样记录，不推导（§4.2）。
- **不进 `configuration_digest`**：它是按任务变化的事实（任务 id 本身就是 `<source_run_id>/s<t>/b<i>`），同一次分叉 sweep 的所有任务共享一个 `config_hash`；sweep 级的旋钮（`continue_steps`、`history_source`、提示贡献配置、teacher 的温度、恢复器的 `verify`）照常经 `resolved` 改变它。字段名不用 "provenance"：那个词已经是 `secrets_recorded` / `env_evidence_privacy` 那一层的名字。
- `source_trace` 是源 run 目录相对 sources 根的路径，加上 `source_run_id`，下游（M32 的样本渲染）才找得到源 trace 去拼第 t 步之前的历史；源 benchmark / subset / task_id 让分叉结果能连回源任务的参考答案。
- 分叉按统一 `RecordingPlan` 选择落盘范围。选择观测时，恢复步保存本次恢复出的 live 观测，终止步保存最后那次观测。轻量计划不强制保存观测；需要后续分叉或导出的消费者检查所需证据，不能把未选择内容当作空观测。

### 5.4 统一记录组件

`TraceWriter`（`hook_id: trace_writer`）通过 AOP 收集执行事实、计算指标，并按 `RecordingPlan` 选择持久化范围。`metrics`、`errors`、`trajectory`、`observations` 独立选择；环境证据仍由 `archive` 受体的归档器列表选择。默认四项开启，默认不选择环境归档。轻量与完整描述持久化内容，不是内存模式。计划保存在 `TraceConfig.recording`；只选指标时不复制或编码 prompt、reply、DOM 或截图。消费者显式检查所需范围，未选择内容不是损坏，也不能当空数据。

调用在发起前有稳定 `call_id`，同时关联 step/call/caller/purpose/model。`RunCallContext` 由 Runtime 每次执行创建，拥有自己的上下文变量；提示贡献用其公开 `scope()` 暂时切换来源，主决策 purpose 为 `agent_decision`，辅助提示为 `prompt_helper`。不存在进程全局调用上下文。`RecordedCallMetrics` 是不含 prompt 的调用事实，完整轨迹和轻量指标共用，读指标不需要恢复图片。主决策与辅助调用都计入实验总量，并可分别聚合；恢复前缀不新造模型调用。

步骤执行从公开 `execute_step` 开始，到 `record_step` 交接之前结束，涵盖观察、提示、模型、解析和动作。记录器记录本步额外采集、编码、提交的 `recording_ms`，不记入下一步。唯一的例外在动作里面：`browser.frames` 在每次 `env.act` 碰页面之前拍一帧，这段等待发生在动作之内，算进 agent 的超时、`duration_ms`、`execution_ms` 和那次动作的 `ActionSpan`，不进 `recording_ms`。它有界（每个动作最多 `max_action_wait_s`，上限 5 s，再加 0.5 s；一个 tab 连续超时两次之后不再等），每条 `before_action` 帧记录的 `released_at - requested_at` 就是录制给这次动作加的时间，消费者据此从动作时长里减掉它（§4.14 `browser.frames`）。提交完成后的计量通过追加指标记录关联原 step，不修改已提交步骤。Runtime 唯一测量 `execution_ms`；任务总耗时仍由任务驱动的 `TaskOutcome.elapsed_ms` 表达。

**每个动作的起止时间（`StepRecord.action_spans`）。** 一步里每次 `env.act` 调用一条 `ActionSpan`，按调用顺序，恢复步也算：

```python
class ActionSpan(BaseModel):
    action: str        # env.act 收到的动作名，全限定（guarded_act 已解析过，如 "browser.click"）
    started_at: float  # 墙钟 time.time()，env.act 即将执行之前
    ended_at: float    # 墙钟，env.act 返回、抛出或被取消时
    outcome: Literal["returned", "raised", "cancelled"]

class StepRecord(BaseModel):
    ...
    action_spans: list[ActionSpan] | None = None
```

- 由 `TraceWriter` 对 `env.act` 的 around 通知测得，像模型调用一样归到 `execute_step` 打开的那一步（分叉检查点与随后同编号的策略步合在一起），从不由 Loop 填写。普通异常（含 `RunAbort`）记 `raised`，取消及其他 `BaseException` 记 `cancelled`；返回值、异常与取消都原样传播，记录故障只丢这一条 span 并进记录健康。挂在 `env.act` 上的守卫（before 通知）在这次调用之内执行，它的否决因此是一条 `raised` 的 span，底层动作并没有执行；`guarded_act` 拒绝的协商外动作不调用 `env.act`，没有 span。
- 与 `duration_ms` 同一规则：`metrics` 关闭、或这一步没经过计时边界时为 `None`；只选 `metrics`、不选 `trajectory` 时照样保留。旧 trace 没有这个键，读回来是 `None`，不升 `format_version`。`[]` 是测过而这一步没调用 `env.act`（终止观测、解析失败的步、无环境的 run）。抛出或取消的调用有 span 却没有 `action_results` 条目，所以列表可以比 `action_results` 长；交接时仍在执行的调用没有终点，不写。
- 用墙钟是为了与环境证据对时：网络归档每条记录的 `started_at`（Chrome wallTime）、每份 `ArchiveProvenance.captured_at`。离线工具据此把一步里多个动作之间的网络往来和归档证据归到具体动作；collection 批次仍只说明证据归哪一步（§4.6），span 也只给时间，不断言因果。
- 不渲染进 prompt：`StepRecord` 是 `HistoryObs` 的载荷，agent 只渲染动作、结果与回复（`test_action_spans_do_not_reach_the_prompt`）。
- 无环境的 run 没有 `env.act` 可拦：`TraceWriter` 经 `EnvironmentInterceptor` 得知后不声明这一拦截（§4.6），run 照常装配，各步记 `[]`。

交接先封存，后台持久化后提交。输入快照在交接时完成；重试只保留同一封存段，不能改名 trailing。记录组件隔离初始化、采集、编码、提交和收尾的异常，真实执行异常原样传播。默认缓冲 64 个段、最多重试 2 次、重试间隔 0.05 秒；这些是记录配置，绝非停机策略。满缓冲丢失的段有健康诊断，后续段继续尝试；恢复等待只在后台和收尾发生。

**收尾的两段等待各自计时。** 收尾先把未交出步骤的调用排进队列，再做最后一次归档收集，最多 `closeout_collection_timeout`（默认 10 秒，`CLOSEOUT_COLLECTION_TIMEOUT_S`：capability 自己给收集设了界，browser 光等事件通道就可能 5 秒，这只是兜底，留够时间让慢的 capability 报出自己的失败并交出健康归档器的证据）；超时、失败或缺口都只是收尾的一条归档问题，超时的 `detail` 写明预算。之后后台写入另有一整段 `close_timeout`（默认 5 秒），从收集结束时起算，把队列写完、写 `result.json` 并关闭 manifest。所以环境卡死只能让 trace 以 `incomplete` 关闭，不能让它停在 `writing`；两段若共用一个预算，卡住的收集会把它用光，写入只剩零头，`result.json` 写不成、manifest 停在 `writing`。只有写入本身超出自己的预算（一次卡在内核里的写）才停止后续写入、保持 manifest 原样：一个可能还会落盘的写之后不能再接一次关闭。每条记录问题的日志都写出通道与 `detail`（`Recording <phase> failed at step <step> (<channel>): <error>: <detail>`），`incomplete_reason` 的每一项也是 `<channel>: <error>: <detail>`。

归档仍从原始 environment.collect_archive 拉取；它决定批次所属步骤，每条证据自己的 captured_at/cycle/ids 保留，不推断动作因果。**一次收集失败只丢它没收到的证据，不丢这一步。** 拉取在封存之后，撤销不了封存：收集抛错、被记录器按契约拒绝（`ArchiveContractError`）或被取消时，这一步照样带着 StepRecord、观测与调用提交，只是没有归档文件，原因记成这一步的一条记录问题（通道 `archive`，阶段 `collect`；取消时先把封存段排进队列再让取消继续）。批次里的每条 `ArchiveGap` 记成这一步的一条记录问题，通道就是缺口的 `archiver_id`（`browser.dom_raw`，或整个 capability 的 id），批次里其余的证据照常随这一步提交。这一步因此留在 `steps` 里，`missing_steps` 不列它，`complete_through` 截在它之前（未恢复的、有步号的问题截断前缀，§5.3）。收尾那次收集的缺口同样记成问题，步号为空，不抹掉已有的完整前缀。归档 run 版本按采集顺序保留，去重只信任提交索引；跨批次引用不得指向未提交对象。归档摘要日志显式标记折叠、代表次数、首末时间和事件类别。

逻辑请求保存框架交给 provider 的消息和 requested output schema，不冒充 provider 转换后的 wire 报文。记录失败保存取得的异常类型、原因、partial/raw output 和 usage，不伪造 completion。

### 5.5 RunTrace 的 lazy loading

30MB 的 trace 不一次性读入内存。不同 judge 按需加载不同部分；只有 manifest 是 eager 读的（每步一条索引，几百字节）：

```python
class RunTrace:
    def __init__(self, trace_dir: Path) -> None: ...        # 只读 manifest.json；不是 format 6 就抛 TraceSchemaError

    @property
    def trace_dir(self) -> Path: ...
    @property
    def manifest(self) -> TraceManifest: ...
    @property
    def run_id(self) -> str: ...
    @property
    def step_count(self) -> int: ...                        # len(manifest.steps)，含洞后的已提交步，无 I/O

    # ── 身份、配置、结果、步记录 ─────────────────────────────────
    @cached_property
    def config(self) -> TraceConfig: ...                    # config.json
    @cached_property
    def result(self) -> AgentResult | None: ...             # result.json；None = 文件不存在
    @cached_property
    def step_records(self) -> list[StepRecord]: ...         # 每个已提交 step，manifest 顺序
    def step(self, idx: int) -> StepRecord | None: ...      # None = 该步没有提交

    # ── 观测、调用、extra 文件 ───────────────────────────────────
    def observation_fragment(self, step: int, key: str) -> FragmentBase | FragmentFailure | None: ...
    def screenshot(self, step: int) -> bytes | None: ...
    @cached_property
    def screenshots(self) -> list[Path]: ...
    def llm_messages(self, step: int) -> list[RecordedLLMCall]: ...
    @cached_property
    def trailing_calls(self) -> list[RecordedLLMCall]: ...
    def extra_files(self, step: int) -> list[ExtraFileRecord]: ...
    @cached_property
    def trailing_extra_files(self) -> list[ExtraFileRecord]: ...
    def read_extra(self, path: str) -> bytes: ...
    def output_schema(self, sha256: str) -> dict[str, Any]: ...

    # ── 环境证据（env/，§5.3）：同样只经 extra 索引解析 ─────────────
    def archive_files(self, step: int, archiver_id: str) -> list[ArchiveRecord]: ...
    def iter_archive(self, archiver_id: str, *,
                     include_trailing: bool = False) -> Iterator[tuple[int | None, ArchiveRecord]]: ...
    def blob(self, sha256: str) -> bytes: ...
    def archive_run_file(self, capability_id: str) -> ArchiveRecord | None: ...
    def archive_lines(self, path: str, model: type[M]) -> list[M]: ...   # M30：逐行 JSON 的归档文件，读成 model

    # ── 一个 run 由哪些文件组成（M33：存储与归档字节的账）──────────
    @cached_property
    def committed_files(self) -> list[CommittedFile]: ...   # 每被列出一次一条，按提交顺序
    def read_committed(self, path: str) -> bytes: ...       # 经该文件自己那个访问器的检查

    # ── 后置产物（不在 manifest 里）──────────────────────────────
    def evals(self) -> dict[str, EvalResult]: ...           # M31：evals/<verdict_slug>.json，按 slug 排序；不缓存
```

`exact_match` judge 只读 `result.json`（几 KB），`visual_judge` 才会加载所有 screenshot。

**每个读取都经由 manifest。** manifest 是提交点（§5.2），所以也是读侧唯一的目录：每个访问器都从 manifest（及其引用的 extra 索引）解析路径，不按文件名探测，不 glob。提交点之前写下的文件，不论是崩溃窗口里的观测、调用记录、截图、extra 文件，还是手工放进目录的孤立文件，对每个访问器都不可见。旧的 `get_observation`、`get_messages`、`unfinished_message_files` 和截图 glob 已删除，没有并存的旧访问器。

**trace 只读它自己目录里的文件。** manifest 和 extra 索引也是数据，而 trace 是拿来分享的（附在 issue 上、随论文上传）。一个伪造或损坏的 manifest 可以把 `/home/me/.ssh/id_rsa` 或 `../../secret.png` 列成第 0 步的截图：不加检查，`screenshots` 会把这个路径交给图像 judge，judge 把文件 base64 进请求发出去；解析失败的 JSON 还会把文件开头几个字节带进报错和日志。所以 manifest 或索引列出的每个路径，打开之前都先检查：形状符合写侧同一条规则（`encoding.check_relative_path`：POSIX 相对路径、无空段、`.`、`..`），并且解析符号链接之后仍在 run 目录之内（`(trace_dir / rel).resolve()` 落在 `trace_dir.resolve()` 之下）。`manifest.json`、`config.json`、`result.json` 这三个固定名字同样要在 run 目录之内。违反时抛 `TraceSchemaError`，点名列出它的那个文件（manifest 或 extra 索引；固定名字则是它自己）。索引里的 `ExtraFileRecord.path` 在读索引时就检查，因为 `extra_files` 会把这些路径交给调用方。run 目录之内的符号链接照常可读。

记录计划未选择的通道由 `RecordingScopeError` 明确拒绝，不能返回空内容冒充发生过但为空。`require_recording(*channels, consumer=...)` 是分叉、导出、回放与需要轨迹的评测者的前置检查；这些消费者还要检查实际归档和连续完整前缀。

`call_metrics(step)` / `trailing_call_metrics` 只读取独立调用事实，`recorded_usage` 统一使用它们，返回未测量状态、已知小计、provider 缺失调用和磁盘覆盖；损坏的 prompt 图片不阻止读取完整的轻量用量账。`recording_timing(step)` 通过提交后的 extra 指标记录读本步记录开销，不重写原 StepRecord。

各访问器的语义：

- `step(idx)` / `step_records`：只看 manifest 列出的 step。列了但文件缺失抛 `TraceSchemaError`，不再静默跳过或返回 `None`。
- `observation_fragment(step, key)`：`key` 是全限定 fragment key（`browser.dom`）。该步没提交，或它的 fragment 索引里没有这个 key（本 run 没激活、这一步没记录）→ `None`。索引 `failed: true` → 按 `FragmentFailure` 读回并返回（它记录的 `fragment_id` 也必须等于 `key`）。否则用 `ObservationRegistrar.get(key)` 找到类，要求 payload 的 `fragment_id == key`，返回 `cls.from_trace(payload, artifact)`；`from_trace` 抛的 `ValueError`（以及嵌套深到解码器走不下去的 `RecursionError`，远超写侧的深度）转成点名文件和类的 `TraceSchemaError`。
- `screenshot(step)`：该步本地名为 `screenshot` 的 fragment（`browser.screenshot`、`desktop.screenshot`……，协商保证一步至多一个）的 artifact 字节。该步没提交、没有截图 fragment、或截图采集失败 → `None`；失败原因用 `observation_fragment` 读。
- `screenshots`：按 step 顺序，每个已提交 step 的截图 artifact 的路径，由 manifest 索引得出；采集失败、没有 artifact 的 step 跳过。索引列了的 artifact 缺失或为空 → `TraceSchemaError`。evaluator 继续拿路径（`online_mind2web`、`lexbench_browser` 按后缀判媒体类型）。写侧保证不会提交空 artifact：CDP 回复里没有图像数据时，`ScreenshotWatchdog` 抛错（resolver 规则"抛错，不伪造"），这一步按 optional / required 记成 `FragmentFailure` 或终止 run；仍然带着 `b""` 的 `ScreenshotObs` 不写 artifact（`trace_binary()` 返回 `None`），读回时 `image_bytes=None`。过去空截图被写成 0 字节的 `.png`，读侧把它当损坏，一帧就让整个 run 的 `screenshots` 抛错。
- `llm_messages(step)`：该步**每一次** LLM 调用，按开始顺序：parse 重试、带 `raw_output` 的校验失败、带 `partial_output` 的截断、只有 prompt 的取消。该步没提交 → `[]`；它的调用如果有，在 `trailing_calls` 里。prompt 里内联的图片已经放回原处（§5.3 "调用记录里的图片只存一份"）：`messages` 与写进去时逐字节相同，每张图片读的时候都核对摘要。
- `trailing_calls`：Loop 没来得及交出的那一步的调用（`manifest.trailing`），先按提交顺序、再按开始顺序；图片同样放回原处。
- `extra_files(step)` / `trailing_extra_files`：已提交的 extra 文件记录，来自 `steps/NNN.extra.json` / `steps/trailing_KK.extra.json`；没有 → `[]`。
- `read_extra(path)`：只读已提交的 extra 文件。`path` 不在任何已提交的索引里 → `KeyError`（没提交和没写过，对读者是同一个事实）；读出的字节与记录的 `size` / `sha256` 不符 → `TraceSchemaError`。
- `output_schema(sha256)`：`OutputFormatRef.sha256` 指向的那份请求的 JSON schema。不在 `manifest.output_formats` 里 → `KeyError`；字节的摘要不符 → `TraceSchemaError`。
- `archive_files(step, archiver_id)`：第 `step` 步的提交取走的、属于 `archiver_id` 的步级归档文件（`scope == "step"`，含内容寻址的那些），按索引顺序，每条是 `ArchiveRecord`。该步没有提交、或它的索引里没有这个归档器 → `[]`。
- `iter_archive(archiver_id, *, include_trailing=False)`：按 step 顺序走每个已提交 step 的 `(step, record)`；`include_trailing=True` 时再按 `manifest.trailing` 顺序给出 trailing 批次里的 `(None, record)`。只含步级文件；格式检查在调用时立即做，不等到第一次迭代。
- `blob(sha256)`：一个已提交的内容寻址归档文件的字节，即 `read_extra(archive_blob_path(sha256))`：没有任何已提交的索引把它列为归档文件（`meta` 带 `"archive"`）→ `KeyError`（别的生产者放在同一地址上、不带归档元数据的 extra 文件不是归档文件）；字节与索引不符 → `TraceSchemaError`。
- `archive_run_file(capability_id)`：该 capability 已提交的 run 记录里 `run_version` 最高的那一版，跨步索引和 trailing 索引找；没有 → `None`。字节用 `read_extra(record.path)` 读。
- `archive_lines(path, model)`（M30）：一个已提交的、逐行 JSON 的归档文件（`index.jsonl`、`replay.jsonl.gz`……），每个非空行用 `model.model_validate` 读回，按文件顺序返回；名字以 `.gz` 结尾的是 gzip 压缩的逐行 JSON，先解压再读（`reader.py::GZIP_SUFFIX`），解压不了 → `TraceSchemaError`；字节经 `read_extra` 读（同样的 size / sha256 校验，没提交 → `KeyError`），一行不是 JSON 或校验不过 → `TraceSchemaError`，点名文件与行号。它是通用的：行的类型由调用方给（browser 的 `ReplayLogEntry` 与网络索引行的模型都在 browser 包里，trace 层不认识它们），"读回来的必须是写进去的那个类型"对 capability 的证据也成立。`RunTrace` 由此结构化地满足 `core/archive` 的 `ArchiveReader`（§4.14 "回放的数据通路"）。
- `evals()`（M31）：这个 run 的 `evals/` 目录里每一份判定，`verdict_slug`（文件名去掉 `.json`）→ `EvalResult`，按 slug 排序；目录按排序后的顺序遍历，所以几份都坏时点名的是哪一份不取决于文件系统列目录的顺序；只看 `*.json`（原子写中断留下的 `tmp….tmp` 不算）。evals 是后置产物（`EvaluationRunner` 在 run 之后原子写入，§5.3），不在 manifest 里，所以这是唯一按目录列文件的访问器：没有 `evals/` → `{}`；目录与每个文件都必须在 run 目录之内（解析符号链接之后），否则 `TraceSchemaError`；一份不是 JSON 或不能按 `EvalResult` 校验的文件 → 点名它的 `TraceSchemaError`。不缓存：同一个 `RunTrace` 在评测写入之后再读，看得见新的判定。`trace_branches` 的 `require_eval` 经它筛选源 run（§4.10），从不读 `result.json`。
- `committed_files`（M33）：这个 run 提交了的每个文件，每被列出一次算一条（`CommittedFile{path, kind, step, fragment, extra, archive}`），按提交顺序：run 自己的三个文件（`manifest.json`、`config.json`，写了的话还有 `result.json`）；按 manifest 顺序每一步的步记录、每个 fragment 的文件（或记在它位置上的失败）与 artifact、每次 LLM 调用、extra 索引与它列出的每个文件；每个 trailing 提交的调用、索引与文件；然后是请求的回复 schema；最后是 prompt 图片（`manifest.prompt_images` 的顺序）。`kind` 说文件是什么（`manifest` / `config` / `result` / `step` / `fragment` / `artifact` / `llm_call` / `extra_index` / `output_schema` / `prompt_image` / `extra`），`step` 是列出它的那次提交（run 自己的文件、trailing 提交、回复 schema 与 prompt 图片为 `None`），extra 文件带它的索引条目，是归档文件的再带 `ArchiveRecord`（与 `iter_archive` 同一个读法）。被几次提交引用的内容寻址文件，每次提交各列一次；怎么计数是调用方的规则（度量的存储每个路径计一次；归档字节的"去重前"每次列出计一次，§4.16）。每个路径列出时就过 `_listed` 的同一套检查（写侧的形状、解析符号链接之后仍在 run 目录之内，违反时点名列出它的文件），manifest 与已提交的索引没列的文件不会出现。消费者因此不必自己走 manifest 的字段、不必按名字探测 `result.json`、也不必再写一遍包含检查。
- `read_committed(path)`（M33）：`committed_files` 列出的一个文件的字节，经它自己那个访问器的检查：extra 文件就是 `read_extra`（size / sha256）；回复 schema 与 prompt 图片的字节要与它的地址相符；artifact 不能为空；调用记录是盘上的样子（图片是引用）；每个文件都要在磁盘上、在 run 目录之内。没被列出 → `KeyError`。
- 任何 `ArchiveRecord` 的字节都用 `read_extra(record.path)` 读，同一套 size / sha256 校验。这四个访问器全部只从每步和 trailing 的 extra 索引解析（索引里 `meta` 带 `"archive"` 键的记录才是归档文件），不探测路径、不 glob，所以孤立的 `env/` 文件对它们不可见。

**读回来的必须是写进去的那个类型。** Writer 拿到的是 `StepRecord` / `AgentResult`，reader 就必须还原成同一个 model，而不是 `dict[str, Any]` —— 否则字段名只能靠翻 JSON 文件去猜，猜错（`rec["step_index"]`，实际是 `rec.step`）能通过全部静态检查、到运行时才炸。这也是 Evaluator 唯一的输入契约。

fragment 也一样。`FragmentBase.from_trace(payload: dict[str, Any], artifact: bytes | None) -> Self`（classmethod）是 `trace_binary()` / `trace_json_exclude()` 的成文逆运算。默认实现处理没有二进制载荷的 fragment：要求 `artifact is None`（不是 `None` 就说明 fragment 漏了覆盖，直接报错），返回 `cls.model_validate(payload)`。覆盖了 `trace_binary()` 的 fragment 必须同时覆盖 `from_trace`，把 artifact 放回被排除的字段：`ScreenshotObs.from_trace` 把字节放回 `image_bytes`。`DOMObs.from_trace` 把 `element_tree` 还原成 `DOMState`（`serialized_dom`、`scroll_info`、`dom_tree`；不单独落盘的 `DOMState.selector_map` 指向同一份 `selector_map`），把 `selector_map` 还原成 `dict[int, DOMElementNode]`，每个条目带 `tag_name`、`attributes`、`bounds` 和子树里的文本。编码里仍然有损的部分（深度截断留下的 `__truncated__`、树内的 `__cycle__` 指针、不追踪的 `parent` 与私有字段）写在 `DOMObs` 的 docstring 里，`from_trace` 不伪造被截掉的节点，两种标记读回时直接丢掉。`parent` 不落盘，但可以由结构推出：`from_trace` 在每棵解码出的子树内按 children / shadow root / frame 文档重建它，所以 `frame_owners()` 在树内可用；条目根节点之上的部分不在条目里，条目根的 `parent` 为 `None`。一个按 `ObservationRegistrar` 全部注册类参数化的往返测试守住这条契约：encode 再 decode，JSON 形式不变。

写侧的配套修正：`DOMObs` 序列化 `selector_map` 时，每个条目用自己的 visited 集合。过去所有条目共享一个，嵌套在前一个已索引元素之下的元素被写成 `{"__cycle__": …}` 桩，丢了自己的 `bounds`（M18 的 BrowserUse trace 里 59/60、70/71、106/107 个条目是桩）。条目带完整子树，不做浅条目：BrowserUse 清洗出的元素节点不带 `text_content`，文本在后代 `#text` 节点里。代价随已索引元素的嵌套深度增长：把 M18 的 45 个 `dom.json`（BrowserUse 与 Skyvern-JS 清洗，38 个非空）按新编码重写，带缩进的文件中位数增大 14%、最多 65%（紧凑 JSON 中位数 31%、最多 98%），7 个文件超过 30%；Skyvern-JS 那几步 34.6 MB 的页面变成约 50 MB。这是下限：条目是从受深度限制的树里重建的。M33 的存储评估从这组数开始。M31 的元素重定位和 M32 的 index → 坐标换算直接读 `selector_map[index]`，不再各写一个解码器。

`DOMObs` 的解码器按 dataclass 字段的注解建解码计划（与编码器读同一份声明），认得标量、`Any`、dataclass、`X | None`、标量联合、`Literal`、`list`、`tuple`、`set` / `frozenset` 和 `dict`。其余泛型（如 `Sequence[str]`）在建计划时抛点名注解的 `TypeError`：那是编程错误，第一个读回 DOM 的测试就会暴露，而不是无限递归成一个 `RecursionError`。

**三种“读不到”必须互相可区分**：

| 情况 | 表现 | 含义 |
|---|---|---|
| 不存在：`result.json` 缺失；或所问的 step、fragment、调用、extra 文件、归档文件不在 manifest 里 | 返回 `None`（`result`、`step`、`observation_fragment`、`screenshot`、`archive_run_file`）或 `[]`（`llm_messages`、`extra_files`、`archive_files`）；`read_extra` / `output_schema` / `blob` / `read_committed` 抛 `KeyError` | 没有发生：run 死在写 `result.json` 之前，或者那一步没有提交 |
| 存在但不可读 | 抛 `TraceSchemaError`，点名文件（以及 model、key 或版本） | trace 本身不可读，重跑评测也没用，只能重跑任务 |
| 评测器自己失败 | `EvaluationError` | 与 trace 无关（judge 429、prompt 缺失等） |

`TraceSchemaError` 的全部来源：

| 来源 | 说明 |
|---|---|
| JSON 损坏、非 UTF-8、空文件 | manifest、config、result、step、fragment、调用记录、extra 索引、output schema 一视同仁；嵌套深到连解释器都解析不了的 JSON 同样算（远超写侧能写出的深度） |
| schema 不匹配 | 包括 `AgentResult.schema_version` 不是当前版本 |
| manifest 列了、磁盘上没有 | 提交过的文件丢了是损坏，不是"没跑完"；`manifest.json`、`config.json` 缺失同理 |
| 路径出了 run 目录 | manifest 或 extra 索引列出的路径不符合写侧的路径规则（绝对路径、`..` 段等），或解析符号链接之后不在 run 目录里；三个固定名字的文件是指向目录外的链接。点名列出它的文件，被指向的文件不会被打开 |
| 字节与索引不符 | 空的 artifact；extra 文件或 output schema 的 size / sha256 对不上；同一个 extra 路径在两个索引里的 size / sha256 不一致 |
| 调用记录引用的图片对不上 | 引用的图片不在 `manifest.prompt_images` 里（没提交过）、`image_refs` 列的位置上不是一个合法的 `PromptImageRef`：点名调用记录；图片文件缺失、字节与名字里的摘要不符、该是 UTF-8 的原文不是：点名图片文件 |
| 归档元数据不合法 | extra 索引里一条记录的 `meta["archive"]` 不能按 `ArchiveMeta` 校验通过；点名那个索引文件 |
| 记录与位置不符 | step 文件里的 `step`、调用记录里的 `step` 与 manifest 提交它的那一步不符；一步里有两个截图 fragment（协商只允许一个） |
| fragment key 不认识 | 懒发现之后注册表里仍然没有这个 key；或 payload 的 `fragment_id` 与 key 不符 |
| `format_version` 不支持 | 不是 7（更旧、更新、或不是整数）：构造 `RunTrace` 时就抛，在校验 manifest 的其余字段之前 |

把前两者都塌缩成 `None`，会让一批旧 schema 的 trace 静默地表现为“agent 没跑完”。`AgentResult.schema_version` 只接受当前版本：旧版本字段名对不上，接受它只会换来一条指着某个被改名字段的 pydantic 报错，而不是“这个 trace 太旧”。

**一个归一化入口。** 一个 run 必有的 `manifest.json`、`config.json`，以及 manifest 列出的每个 JSON 文件（step、fragment、调用记录、extra 索引、output schema），都经 `_read_bytes` 读字节、经同一个 `_parse` 解析和校验：model 用 `_read_model(path, model)` 读，fragment 的 JSON 先读成 dict 交给 `from_trace`。解析是严格 UTF-8 解码加标准库 `json.loads`，校验是 pydantic 的 Python 模式（`model_validate` / `validate_python`）。不用 pydantic 的 `validate_json`：它的 JSON 解析器拒绝超过 200 层的嵌套，而写侧用的 pydantic-core 序列化器能写到约 254 层，DOM 编码器的深度预算本身就到约 204 层。一个嵌套一百来层的页面（正是深度截断要照顾的页面）、一个深层的结构化回复或工具结果，都能顺利提交，却永远读不回来，还被报成"Invalid JSON"。`json.loads` 只受解释器递归上限约束，远高于写侧能写出的深度；真超过了，同样转成点名文件的 `TraceSchemaError`。非 UTF-8、JSON 损坏、空文件（`UnicodeDecodeError` / `JSONDecodeError`，都是 `ValueError`）和 schema 不匹配（`ValidationError`）都由 `_parse` 转成点名文件和 model 的 `TraceSchemaError`（`path` 属性就是那个文件；format_version、fragment key 和一步两个截图 fragment 的情形是 `manifest.json`）；`_read_bytes` 把 manifest 列了而磁盘上没有的文件同样转成 `TraceSchemaError`；其余 `OSError`（权限、I/O）原样上抛，那不是 trace 本身的问题。`result.json` 是唯一的例外，因为没有谁列出它：它自己读字节，不存在就返回 `None`（唯一返回 `None` 的缺文件情形），解析和校验失败同样转成点名文件的 `TraceSchemaError`；版本不符时，报错按 `result.json` 自己声明的版本说"这个 trace 早于当前 AgentResult schema"。

**落盘文件名带 provenance。** Observation 文件名用的是**全限定 fragment key**（`000_browser.dom.json`），不是协商后的 field name（`000_dom.json`）。后者是"本次装配里哪个 capability 赢了协商"的产物：同一个 `000_screenshot.png` 在 browser run 和 desktop run 里含义不同，而读 trace 的人无从分辨——"Record Once, Evaluate Many" 的前提是 trace 自带足够信息，evaluator 不需要回头猜配置。

同一份 provenance 在数据里再写一遍：`fragment_id` 是 `FragmentBase` 上的**真实字段**（默认值来自 `__registry_key__`），不是 `@property`——property 不进 `model_dump()`，于是每个 fragment 落盘时都是匿名的。

**截图原图与模型呈现分别记录。** `browser.screenshot` 的二进制 artifact 是 clean capture，sidecar 的 `cursor` 是可重建模型视图的坐标与样式元数据。`ModelImage.model_image_bytes()` 产生给模型看的图；LLM 调用的内容寻址图片保存这份实际输入。reader/writer 不画光标，恢复与评测不把光标差异当成页面变化。

**二进制 fragment 的后缀来自字节本身，不来自配置。** 截图的编码是 capability 配置（`screenshot_format: png | jpeg`，默认 png 与旧行为逐字节相同），但 `ScreenshotObs.trace_binary()` 是从图像头读出后缀的——一个"声明的格式"字段能和字节不一致（provider 没听配置、fragment 从磁盘文件重建），而不一致会在几层之外变成 vision provider 的 400。同理，agent 拼 `data:` URL 的 media type、artifact 落盘时的后缀、evaluator 按后缀判类型，读的都是同一个来源（`spectragent/schemas/observation/image.py`）。

**trace 格式有版本，只读当前这一版。** `TRACE_FORMAT_VERSION = 5`。本版引入声明记录范围、独立轻量调用账、稀疏步骤缺口和历史记录诊断；仍保留每 run 内容寻址的 prompt 图片。`AgentResult.schema_version = "5.0"`，总执行时间是 `metrics.execution_ms`，未测量的 usage/call count 为 None。读者拒绝旧格式和旧结果 schema，不保留迁移转发或对测试数据的兼容分支。

**离线读者找得到 fragment 类。** 评测进程未必 import 过定义 fragment 的模块（browser 的 fragment 是被各自的 watchdog import 时才注册的）。`ObservationRegistrar.get(key)` 落空时，reader 懒执行一次 `discover("spectragent.schemas")` 和 `discover("spectragent.core.capabilities")` 再查；仍然落空就抛 `TraceSchemaError`，点名 key，并说明定义它的模块必须在读侧可导入。

### 5.6 环境证据的隐私

环境证据的隐私方案（用户裁决 D4，2026-10-09：剥离凭证、表单值脱敏、归档打标、导出前审查）在这里落成框架与 capability 的分工。**框架从不读、也从不改证据的字节**：采集和脱敏是 capability 的事（§4.9），框架不声称替任何证据脱敏。框架这一侧只做三件事：记录本 run 声明了什么；提供共享的规则与台账格式，让每个 capability 用同一套；在产物层面用 secret guard 检查。

**威胁模型"只有我"（用户 2026-10-13 裁决）。** trace 与 `env/` 只在录制它的那台机器、那个账户里。框架守两道门：发给 LLM 服务商的提示词里不带秘密（敏感控件的值不进观测，也不进 `extract_content` 的动作结果，§4.12）；导出数据集时把住（`env/` 永不导出，凭证扫描 fail closed，§4.15）。度量报告的隐私一项也只看这两道门：录下的每一次模型请求与导出的数据集，不搜 `env/` 与 trace 的其余文件（§4.16）。由此：`Set-Cookie` 原样保留（回放的 cookie jar 用它）；`storage.json` 存原值；trace 文件的权限跟 umask 走（`0o666 & ~umask`，§5.2），框架不管可见性；`task_setup` 的残余只记 TODO（下面"残余"）；键入动作在敲字时记下文字是否进了敏感控件（`typed_into_sensitive_field`），导出怎么用它随学生侧一起推迟；键入的文字本身仍原样留在动作参数与 LLM 记录里。

| 对象 | 默认处理 | 归属 |
|---|---|---|
| 本 run 有没有环境证据、声明了哪些隐私标记 | 有没有：`TraceConfig.negotiation.active_archivers` 非空；`env_evidence_privacy` = 所绑定归档器 `privacy_markers` 的有序并集，绑定了归档器才写进 `config.json`。它只陈述归档器的声明，不证明字节里没有秘密 | 框架（M28） |
| 共享规则与脱敏台账 | `core/archive/privacy.py`：`PRIVACY_POLICY`（规则版本，`"af-privacy-1"`）、字段名模式 `password\|passwd\|pwd\|otp\|token\|secret\|card\|cvv`（子串匹配，有意宽）、凭证参数名模式（`key` / `apikey` / `sig` / `signature` / `auth` / `credential` 作尾段）、URL 规则、`RedactionLedger`、标记常量 `MARKER_REDACTED` / `MARKER_SESSION_STATE`。规则一改，`PRIVACY_POLICY` 必须升版本（测试用规则源码的摘要钉住：两个名字模式、占位符、URL 模式和每个规则函数的源码，URL 规则在内） | 框架（M28）；M29、M30 复用 |
| 组装后的配置（`TraceConfig.resolved`） | 字段级、默认不记（`core/trace/redaction.py`）：闭集值（`Literal`、`Enum`、`bool`、`None`）与数字（`int`、`float` 及只由它们和闭集组成的 union，用户 2026-10-13 的裁决）原样记录；自由字符串只有字段标了 `trace_safe` 才原样记录，否则写 `"<omitted>"`；列表等容器里的值同样写 `"<omitted>"`。数字原样记录，读者才看得到 teacher 的采样（温度、`max_actions_per_step`），分叉才能在装配期比较 agent 与 memory 的数字旋钮（§4.1）。`config_hash` 取自脱敏之前的配置（§5.3），所以没记下来的字符串照样区分两次 run。`record_secrets` 打开时全部原样记录 | 框架 |
| 环境 setup context（`TraceConfig.task_setup`） | 值级脱敏：凭证形状的映射键，其值（整棵子树）换成 `<redacted>`；URL（以 `scheme://` 开头的字符串）的 userinfo 整段换掉；名字是凭证形状的 query / fragment 参数，值换掉；其余字节原样。`null` 与空参数值没有可换的东西，原样保留、不记台账（`null` 要与"设置过"区分开）。台账一起记录。`record_secrets` 打开时与 `resolved` 一样原样记录，台账为空 | 框架（M28） |
| secret guard | 扫描整个 trace 目录（含 `env/`），gzip 文件（魔数 `1f 8b` 或 `.gz` 后缀）先解压再扫；正向对照：假归档器把凭证写进 `env/`（明文与 `.gz` 各一份），必须被发现。凭证形状的正则 `SECRET_PATTERNS` 在 `core/trace/secret_patterns.py`（M32 从测试模块挪进生产代码），guard 测试与训练导出的凭证扫描 import 同一份 | 框架（M28；M32 挪位置） |
| 归档本身 | 永不导出；导出物只含截图、动作、thought 与引用（§4.15） | 导出（M32 遵守） |
| 训练导出的产物（`output_dir`） | `secrets_recorded` 的源不导出；写出的每个文本字节过 `SECRET_PATTERNS`，命中则整个导出失败、不留输出（fail closed）；源 `task_setup` 的台账抄进样本的 `redactions`；残余：形状之外的秘密（键入的密码），见 §4.15 "隐私" | 导出（M32） |
| 请求头 `Cookie` / `Authorization` / `Proxy-Authorization`（以及 `browser.network` 的 `strip_request_headers` 追加的，大小写不敏感） | 采集时在每一个来源剥离（含 ExtraInfo 与 `associatedCookies`），从不落盘；台账只记名字 | browser（M29，§4.14） |
| 响应头 `Set-Cookie` | 保留（回放的 cookie jar 需要）；`browser.network` 声明 `contains_session_state`，capability 的 run 记录（指纹）同样标出 | browser（M29） |
| 会话态（`logged_in`） | 定义为"浏览器不是从新建的 profile 启动的"（provider 的 `persistent_profile()` 点名旋钮，只看配置；这是 `NETWORK_CAPTURE` 的承诺之一，答不出的 provider 被拒）；此时每个绑定的归档器都要写 `allow_session_state: true`，否则报错点名 provider 旋钮与归档器；`pinned` 一律拒绝非新建的 profile | browser（M29） |
| cookie 与 local / session storage 的值（`browser.storage`） | 原样保存（用户 2026-10-13 裁决）；会话 token 就在里面，所以 `browser.storage` 声明 `contains_session_state`；`allow_session_state` 只表示"接受非新建的 profile" | browser（M29；2026-10-13 改存原值） |
| DOM 原始采集里的输入值 | 敏感控件（`dom/sensitivity.py`：type、autocomplete、OTP 提示，与 Skyvern 页内规则对齐）的 value 属性、UA shadow 里的文本、`DOMSnapshot` 的 inputValue / textValue、AX value 在落盘之前替换（`input_value`） | browser（M29） |
| DOM 观测里的输入值（提示词与 `browser.dom` fragment） | 同一条规则：BrowserUse 清洗器建树时把敏感控件的 value 属性、它下面的文本、内部节点的无障碍名字换成 `<redacted>`（§4.12）；Skyvern 页内脚本本来就不放进树 | browser（2026-10-13） |
| `browser.extract_content` 读一个元素返回的 HTML（动作结果，进提示词与 `steps/NNN.json`） | 同一条规则、同样的位置：敏感控件及它里面的 value 属性、文本与注释在序列化的 HTML 上换成 `&lt;redacted&gt;`，其余字节不动（§4.12）；不给编号时的页面文本不含字段值，只列 `<select>` 的选项文字（残余） | browser（2026-10-13） |
| 键入的目标 | 键入动作在敲字之前按同一条规则判定焦点所在的元素，结果记进 `ActionResult.typed_into_sensitive_field`（`true` / `false` / 看不见时 `null`），进 `steps/NNN.json`，agent 不渲染它；键入的文字原样留在动作参数里 | browser（2026-10-13）；导出的用法随学生侧推迟 |
| POST body | 按字段名模式脱敏，写入台账；`redact_post_body` 从 browser 的 `archive/redaction.py` 导出，回放时先用同一个函数脱敏再算匹配键 | browser（M29）；M30 复用 |
| 台账的规则名 | `RedactionRule` 增加 `request_header` / `input_value`；browser 的规则版本 `BROWSER_REDACTION_POLICY = "<PRIVACY_POLICY>+browser-2"` 写进 browser 台账的 `policy`，规则源码按摘要钉住（`browser-2` 删掉了 cookie 值的哈希与 `value_hash`）。词表的增删不改框架的规则函数，`PRIVACY_POLICY` 不升版本 | 框架的台账词表（`core/archive/privacy.py`，M29 扩充）；browser 规则（M29） |

**脱敏写成 `<redacted>`，不是 `"<omitted>"`。** 后者是配置脱敏（`core/trace/redaction.py`，deny by default）的"设置了、没记录"；前者是"值被这条规则换掉了"，两者回答不同的问题。`<redacted>` 也不会被 secret guard 的凭证形状误认成凭证。

**台账**（`RedactionLedger`）：`policy`（写下它时的 `PRIVACY_POLICY`）加一串 `RedactionEntry(location, field, rule)`：`location` 是 JSON pointer（`/start_url`），`field` 是被换掉的键或参数名，`rule` 是 `field_name` / `url_userinfo` / `url_query` / `url_fragment` 之一，按遍历顺序。它让读者知道哪里被换过，而不是猜一个空值是不是本来就空。

**残余，写明。** `task_setup` 是没有字段声明的按任务数据，`core/trace/redaction.py` 那种"没标记就不记"在这里会把每个值都吞掉、让分叉任务（M31）无法重建环境，所以它走值级规则。规则只认得凭证形状的名字和 URL 里的凭证位置：一个藏在规则不认识的名字下面的秘密（`{"note": "the password is hunter2"}`，或名字叫 `pass` 的键）会原样记录；嵌在非凭证 query 参数里的 URL 也一样——没有百分号编码时的 userinfo，百分号编码时里面的 `token=`。这些只记 TODO、不改代码（用户 2026-10-13 裁决，威胁模型"只有我"）。还有一条具体的：`BrowserCapability.setup` 接受 `cdp_url`，调用方若经 `env_context` 传一个 devtools URL（`ws://…/devtools/browser/<id>`），它会原样进 `task_setup`。发布的入口都不这么传（`TaskSetup.to_context()` 没有这个键，`spectragent run` 只传 `start_url`），要覆盖它得新增规则并升 `PRIVACY_POLICY`。`TraceConfig.extra` 仍然不脱敏（调用方自己挑的字段）。v1 的数据来源选的是无需登录的公开站点；登录态采集是单独的决定。

---

## 6. conscribe 注册表总览

| 注册表 | 判别方式 | 说明 | 状态 |
|--------|---------|------|------|
| `AgentRegistrar` | `agent_id` (flat, hierarchical key `family.member`) | Agent 实现（领域知识），通过 `__wiring__` 限定可配置的 Loop / Capability 范围；实际兼容性在装配期校验。`key_separator="."`：家族根是抽象类（`browser_use`），不注册；成员注册为 `browser_use.dom`，各写完整 `__wiring__`，prompt 读家族根的目录（§4.1 "Agent 家族"）；不带点的键（`ui_tars`）自成一族 | ✅；发布的两族：`browser_use.dom` / `browser_use.dom_vision`（§4.1 "BrowserUse 家族"）与 `skyvern.dom` / `skyvern.dom_vision`（§4.1 "Skyvern 家族"） |
| `AgentLoopRegistrar` | `loop_id` (flat) | Loop 实现（执行策略）；通过 `__required_slots__` 声明所需 agent 方法，通过 `__loop_inputs__` 声明每任务的输入（M31） | ✅ ReactLoop；M31：`branch`（`BranchLoop`，ReactLoop 子类，§4.1）；CodeAct/FunctionCalling/PlanExecute 待 P2/P3 |
| `LLMRegistrar` | `model_type.provider` (nested) | Chat provider 实现，保持 Chat 受体隔离 | ✅ |
| `ReplayReviewerRegistrar` | `reviewer_id` (flat) | `replay_reviewer`：独立 replay 审查 job 的策略，`jev` 持 Decision 受体，§4.18 | ✅ |
| `DecisionModelRegistrar` | `model_type.provider` (nested) | `core/llm/decision_registrar.py`，Decision provider：`typesafe.direct` / `typesafe.openrouter`，配置层 `decision_model` | ✅ |
| `EnvironmentRegistrar` | `env_type` (flat) | 环境实现，通过 `__wiring__["capabilities"]` 注入 Capabilities，按前缀路由 action | ✅（仅 BrowserEnvironment；CompositeEnvironment 待） |
| `CapabilityRegistrar` | `capability_id` (flat) | 能力 = ActionGroup + Resolver 合体；持 backend wired field | ✅（仅 BrowserCapability；docker / tool_sandbox / desktop_vm / android / gpu_profiling 待） |
| `BrowserProviderRegistrar` | `provider_id` (flat) | Browser capability 的 backend：`local_chrome` / `lexmount` / `skyvern` / `browser_use` / `airtop` / `agentbay` / `hyperbrowser`。文件位于 `core/capabilities/browser/backends/` | ✅ |
| `DockerProviderRegistrar` | `provider_id` (flat) | Docker capability 的 backend（`local_docker` / `ssh_docker` ...） | P2 占位待实现 |
| `MemoryRegistrar` | `memory_id` (flat) | 记忆策略 | ✅（3 种；协议兼容性在 `prepare()` 校验，SummaryMemory 待实现） |
| `RestorerRegistrar` | `restorer_id` (flat) | 检查点恢复策略：把环境恢复到源 run 第 t 步之前的状态（`prefix_replay`；快照恢复 `capability_checkpoint` 是已知的下一个），`core/loops/restore/`；`BranchLoop.__wiring__["restorer"]` 受体，进 `collect_registrars()`（§4.1） | M31 |
| `HookRegistrar` | `hook_id` (flat) | Lifecycle hooks (AOP) | ✅（trace_writer / live_monitor） |
| `EvaluatorRegistrar` | `evaluator_id` (flat) | 评测策略 | 🚧 partial |
| `ExporterRegistrar` | `exporter_id` (flat) | 训练导出的格式（v1 `openai_messages`；以后 `llamafactory` / `ms_swift` / `trl`），`spectragent/export/_registrar.py`；`ExporterBase` 持导出任务字段与 `translator` 受体，运行根的可选顶层层（与 `evaluator` 同一先例），进 `collect_registrars()`（§4.15） | M32 |
| `ActionTranslatorRegistrar` | `translator_id` (flat) | 训练导出时把 teacher 的动作空间换成学生的（`identity` / `index_to_coordinate`），`spectragent/export/translate/_registrar.py`；`ExporterBase.__wiring__["translator"]` 受体，进 `collect_registrars()`（§4.15） | M32 |
| `TaskLoaderRegistrar` | `benchmark_name` (flat) | 任务数据加载 | ✅ |
| `StepSelectorRegistrar` | `selector_id` (flat) | `trace_branches` 加载器在源 run 的已提交策略步里选分叉点的策略：`explicit` / `every_k` / `all` / `last`（M33：每个源 run 最后一个候选，原路径回放段用，§4.16），`core/data_loader/task/step_selector/`；`TraceBranchesTaskLoader.__wiring__["step_selector"]` 受体，进 `collect_registrars()`（§4.10） | M31；`last` M33 |
| `EvalLoaderRegistrar` | `benchmark_name` (flat) | 评测数据加载 | ✅ |
| `ActionRegistrar` | `action_id` (flat, dotted key `source.id`) | 每个 Action 独立注册；键含 source 前缀避免跨 capability 撞名（`browser.click` vs `desktop.click`） | ✅ |
| `ObservationRegistrar` | `fragment_id` (flat, dotted key `source.id`) | Observation fragment；键含 source 前缀（`browser.dom`、`docker.terminal`、`loop.action_result`）；agent / loop 的 `__wiring__` 写 capability-relative 短名（`dom`），协商期绑定到本次 environment 的 provider | ✅ |
| `ArchiverRegistrar` | `archiver_id` (flat, dotted key `source.id`) | 环境证据归档器；键含提供它的 capability 前缀（`browser.network`）。capability 的 `__wiring__["archiver"]` 声明 provides，trace hook 实例的 `archive` 列表声明 requires（全限定）；`negotiate_archive` 在 `prepare()` 校验，并在 `prepare()` 末尾绑定（§4.5） | ✅ M28 框架机制；M29：`browser.network` / `browser.storage` / `browser.dom_raw`，只作标签的截图 `browser.frames`，回放的 `browser.replay_log`（§4.14），进 `collect_registrars()`，经 `trace_writer.archive` 由 YAML 选择 |
| `ReplayabilityRegistrar` | `replayability_id` (flat) | Browser 的回放档位（`none` / `pinned`），`core/capabilities/browser/replay/`；`BrowserCapability.__wiring__["replayability"]` 受体，策略即开关，YAML 每个 capability 块必填（§4.14） | ✅（browser 内部，M29） |
| `NetworkSourceRegistrar` | `source_id` (flat) | Browser 的网络来源（`live` 默认 / `archive` 从已录制 run 的归档离线回放），`core/capabilities/browser/replay/network_source.py`；`BrowserCapability.__wiring__["network_source"]` 受体，策略即开关——隔离由来源推出，provider 上没有并列的布尔；YAML 每个 capability 块必填（§4.14 "回放"） | ✅ M30（browser 内部） |
| `MissPolicyRegistrar` | `miss_policy_id` (flat) | 回放 miss 的应答策略（v1 只有 `strict`），`core/capabilities/browser/replay/miss_policy.py`；`ArchiveNetworkSource.__wiring__["miss_policy"]` 受体（嵌套在 `network_source` 里），`archive` 块里必填（§4.14 "回放"） | ✅ M30（browser 内部） |
| `NegotiationResultRegistrar` | `result_type` (flat) | 协商结果类型；内置 `observation` / `action` / `archive` 自动注册，自定义协商协议注册自己的结果类型并作为 `target_class` 传给 `negotiate()` | ✅ |
| `DOMCleanerRegistrar` | `cleaner_type` (flat) | DOM 清洗策略：`browser_use` / `skyvern` / `skyvern_hit_test`（M32：skyvern 加页内命中测试，§4.12） | ✅（browser 内部） |
| `DOMSerializerRegistrar` | `serializer_type` (flat) | DOM 序列化策略 | ✅（browser 内部） |
| `MouseTrajectoryRegistrar` | `trajectory_type` (flat) | 光标轨迹策略（`instant` / `windmouse` / `bezier` / `learned`）；同时是鼠标侧唯一的拟人化开关，见 §4.13 | ✅（browser 内部） |
| `TypingRhythmRegistrar` | `rhythm_type` (flat) | 键入节奏策略（`instant` / `human`）；键盘侧的同一形状，独立于轨迹轴，见 §4.13 | ✅（browser 内部） |

> **Provider 目录归属**：每个 capability 的 backend provider 文件物理上属于 `spectragent/core/capabilities/{cap}/backends/`，对应的 `*ProviderRegistrar` 也在该目录的 `_registrar.py` 里。Browser 已落地；Docker / Desktop / Android 等 P2/P3 时按同样模式增设。

> **Dotted-key flat vs nested discriminator（2026-04 订正）**：Action / Observation 原计划上 nested discriminator，实测 conscribe 在“所有 leaf 同一 source”时会命中 Pydantic 的单字段 discriminator 冲突（见 `conscribe.config.composed._build_subset_union`）。改用 *dotted-key flat*：discriminator 仍是单字段 `action_id` / `fragment_id`，但键内嵌 source 前缀（`browser.click`、`loop.action_result`），同时避免 registry 撞名和 conscribe 的 Pydantic 冲突。语义与 nested 等价，wiring 字符串、前缀路由、config 生成都不需要特殊处理。Agent 的家族键同理：`agent_id: browser_use.dom` 是单个判别值，不是嵌套块（§4.1 "Agent 家族"）。

---

## 7. 设计模式总结

| 模式 | 应用场景 |
|------|---------|
| **Protocol (结构化类型)** | 所有公共契约（`BaseAgent`、`AgentLoop`、`BaseCapability` 等） |
| **Slot 校验** | Loop 兼容性：每个 Loop 用 `__required_slots__: set[str]` 声明它需要的 agent 方法名；`weave()` 用 `callable(getattr(agent, slot))` 校验。避免 `*Compatible(Protocol)` 在 Loop 子类化时爆炸 |
| **Monkey-patch / Loop-on-agent** | `loop.weave(agent)` 把 `loop._execute(agent, task)` 装成 `agent.run(task)`，Loop 物理上"安装"到 agent 上 |
| **Strategy** | Loop 策略、DOM cleaners/serializers、mouse trajectory / typing rhythm、Memory、Evaluator — 通过注册表 key 切换 |
| **策略即开关** | 拟人化没有与策略并列的布尔：`humanized` 是策略自身的属性，只有 `instant` 为 `False`（§4.13）。同一语义两个声明点必然产生无人报错的矛盾组合 |
| **Registry (自动注册)** | conscribe 注册表，metaclass 驱动导入时注册 |
| **`__wiring__` 跨注册表依赖** | 限定可配置的注册键及子配置，并为 Env↔Capability、Capability↔Backend 等受体提供注入声明；Observation 协商读取其中的需求声明 |
| **IoC / setattr-injected wired receptor** | `agent.llm`、`env.capabilities`、`capability.backend` 都是 conscribe `WiredField` descriptor + ConfigComposer setattr 注入 |
| **AOP / Lifecycle Hooks** | TraceWriter — 通过 Weaver + `__intercepts__` + AOPProxy 透明织入 |
| **Resolver / IoC** | Capability 内部按需解析 observation fragment（Watchdog 即 Resolver） |
| **Avro-style Negotiation** | Observation 三方协商（Capability provides / Agent requires / Loop requires） |
| **Observer / Event Bus** | Watchdog 系统 + Lifecycle Hooks |
| **Factory** | `ConfigComposer` 按判别键查注册表 → 实例化 → setattr 注入 |
| **Adapter** | Message serializers (7 种 provider 格式) |
| **Composite + Prefix Router** | Environment 组合 capabilities，前缀路由 action（`browser.click`） |
| **Profile / Preset** | BrowserProfileConfig 命名组合 |
| **Discriminated Union** | nested discriminator 生成 tagged union 配置类 |
| **WAL (Write-Ahead Log)** | RunTrace 每步落盘，manifest 原子更新 |
| **Lazy Loading** | RunTrace 按需加载 fragment / screenshot / messages |
| **guarded_act()** | AgentRuntime 生成 action 守卫函数，保证 prompt 与实际能力一致 |

---

## 8. 完整配置示例

YAML 形态与 `spectragent/generated/_composed.py` 的 discriminated-union schema 一一对应。`backend` 是 capability 的执行层配置，由 `provider_id` 区分 + provider 自身字段平铺。

**WebArena / browser-use 配置（local Chrome）**：

```yaml
# 通过 YAML 驱动全部组件选择和组装

agent:
  - agent_id: browser_use.dom_vision
    # browser_use.dom | browser_use.dom_vision —— 决定什么进模型上下文、以及有哪些点击动词。
    # max_steps / max_actions_per_step 归 loop，agent 不持有第二份。
    flash_mode: false

agent_loop:
  - loop_id: react
    max_steps: 100
    max_actions_per_step: 1

llm:
  - model_type: openai
    provider:
      name: direct
      model: gpt-4o

memory:
  - memory_id: sliding_window
    window_size: 20

capability:
  - capability_id: browser
    profile: standard
    viewport_width: 1280
    viewport_height: 720
    # observation: / archiver: 是 provides 声明（没有 slot）：不在这里填值，
    # 填了会被 Composer 以 UnresolvedWiringError 拒绝（§4.2.1）
    backend:
      provider_id: local_chrome     # 切 backend 只改这一行
      headless: true                # provider 自身的 conscribe 字段

environment:
  - env_type: browser

hook:
  - hook_id: trace_writer
    trace_dir: ./traces

# evaluator:                        # 待 M16 完整实现
#   - evaluator_id: llm_judge
#     judge_model: claude-sonnet-4-6
```

**切到 cloud backend（skyvern）**——只动 `backend` 块：

```yaml
capability:
  - capability_id: browser
    backend:
      provider_id: skyvern
      api_key: ${SKYVERN_API_KEY}
      # ... skyvern 自身的 conscribe 字段
```

**录制 run（M29，§4.14；发布的完整配置在 `configs/flywheel/record_*.yaml`）**。环境的档位挂在 capability 上，记什么挂在 trace hook 上：

```yaml
capability:
  - capability_id: browser
    replayability: {replayability_id: pinned, base_time: run_start, random_seed: 1,
                    timezone: UTC, locale: en-US, device_scale_factor: 1.0}
    network_source: {source_id: live}      # 录制：网络就是网络
    backend: {provider_id: local_chrome}   # 录制只支持声明了 NETWORK_CAPTURE 的 provider

hook:
  - hook_id: trace_writer
    trace_dir: ./traces
    archive:                               # 归档的唯一开关；省略、null、[] = 不归档；顶层 archiver: 段报错（§4.2.1）
      - archiver_id: browser.network
        max_inline_body_bytes: 5000000
      - archiver_id: browser.storage
      - archiver_id: browser.dom_raw
```

评测配置不写 `archive`，归档关闭（顶层 `archiver:` 段会被拒绝）；但每个手写的 capability 块都写 `replayability: {replayability_id: none}` 与 `network_source: {source_id: live}`（单实例受体必填，与 `mouse_trajectory`、`typing_rhythm` 一样）。

**回放 run（M30，§4.14 "回放"；发布的完整配置在 `configs/flywheel/replay_*.yaml`）**。它与录制配置只差网络来源、T0 与 fixture 用的路由开关；源 run 目录是每个任务不同的输入，经任务上下文的 `replay_archive` 送到 `prepare()` 的检查与 setup（`spectragent run --replay-archive DIR`；不给它，`prepare()` 就拒绝这份配置），不写在配置里：

```yaml
capability:
  - capability_id: browser
    replayability: {replayability_id: pinned, base_time: from_archive, random_seed: 1,
                    timezone: UTC, locale: en-US, device_scale_factor: 1.0}
    network_source:
      source_id: archive
      isolation: required                   # 只有声明了 NETWORK_ISOLATION 的 provider 支持
      matcher: {levels: [exact, normalized], site_rules: configs/replay_rules}
      miss_policy: {miss_policy_id: strict}
    backend: {provider_id: local_chrome}   # 不写 proxy_server、不在 extra_args 里设路由开关

hook:
  - hook_id: trace_writer
    trace_dir: ./traces
    archive:
      - archiver_id: browser.replay_log     # 回放必须绑定：它记下回放日志（replay.jsonl.gz）；browser.network 在回放里被拒
```

**SWE-bench 配置示例（P2 待实现）**：

```yaml
agent:
  - agent_id: swe_agent
agent_loop:
  - loop_id: codeact          # 同一个 agent，换 loop
capability:
  - capability_id: docker
    backend:
      provider_id: local_docker
environment:
  - env_type: docker_terminal
```

**GAIA 复合环境配置示例（P3 待实现）**：

```yaml
agent:
  - agent_id: gaia_agent
agent_loop:
  - loop_id: plan_execute
capability:
  - capability_id: browser
    backend: { provider_id: local_chrome }
  - capability_id: tool_sandbox
environment:
  - env_type: composite
# agent 看到: browser.click, browser.navigate, ..., tool_sandbox.call_tool, ...
```

---

## 9. 扩展点清单

| 扩展点 | 方法 | 示例 |
|--------|------|------|
| **新增 Agent Loop（全新策略）** | 继承 `AgentLoopBase`，设 `__registry_key__` + `__required_slots__`，实现 `_execute()` | Tree-of-Thought loop |
| **新增 Agent Loop（已有策略的微调）** | 继承现有 Loop 类，slot 集合做并集 | `ReactWithThoughtLoop(ReactLoop): __required_slots__ = ReactLoop.__required_slots__ \| {"parse_thought"}` |
| **新增 Agent** | 继承 `AgentBase`，实现 Loop 要求的方法 slots，设 `__wiring__` | `SWEAgent` 同时满足 React 和 CodeAct 的 slots |
| **新增 Agent 家族成员** | 继承家族的抽象根，键为 `<根>.<成员>`（由类名推出或写全），写出自己完整的 `__wiring__` 条目；prompt 放在家族根的目录里（§4.1 "Agent 家族"） | `browser_use.dom`、`browser_use.dom_vision` |
| **新增 Environment** | 继承 `EnvironmentBase`，`__wiring__["capabilities"] = ("capability", [...])` 注入 capability 集合 | `CompositeEnvironment`（多 capability 前缀路由） |
| **新增 Hook** | 继承 `HookBase`，设 `__intercepts__` 与 `{advice}_{method}` handler | 自定义日志、告警、live_monitor |
| **新增 Evaluator** | 继承 `EvaluatorBase`，实现 `evaluate()` | LLM-as-judge / 测试套件 |
| **新增 Observation Fragment** | 继承 `ObservationFragment`，设 `__registry_key__`（dotted）+ `__source__` | `docker.filesystem`, `gpu.metrics` |
| **新增 LLM Provider** | 继承 `OpenAICompatBase` / `AnthropicCompatBase` | 私有部署 LLM |
| **新增 Capability** | 继承 `CapabilityBase`，实现 action / resolver 接口；`__wiring__["backend"] = "<provider_registry>"` | `DatabaseCapability` (NL2SQL bench) |
| **新增 Capability Backend** | 在 `core/capabilities/{cap}/backends/` 下加 provider 类，继承 `<Cap>ProviderBase` | 自建浏览器集群、私有 GPU 集群、SSH Docker |
| **新增 Memory** | 继承 `MemoryBase` | RAG-based memory |
| **新增 Benchmark Loader** | 继承 `TaskLoaderBase` / `EvalLoaderBase` | SWE-bench, WebArena |
| **新增 DOM Cleaner/Serializer** | 继承对应 Base | Markdown 序列化 |
| **新增 Archiver（环境证据归档器）** | 继承所属 capability 的 archiver 基类（框架基类 `ArchiverBase`；browser 是 `BrowserArchiverBase`：声明 `needs`、实现 `open(ctx)` 返回运行期会话，§4.14），`__registry_key__ = "<capability>.<id>"`，旋钮写成 `Annotated` 构造参数（模块里不用 `from __future__ import annotations`），按需声明 `privacy_markers`；把键加进该 capability 的 `__wiring__["archiver"]`；capability 实现 `bind_archivers` / `collect_archive`（§4.9 归档契约）；把 capability 接入 `tests/_helpers/archive_contract.py` | `browser.network`、`browser.storage`、`browser.dom_raw`（M29）、`browser.replay_log`；以后的 `browser.rrweb`、`docker.fs_diff`、`android.ui_dump` |
| **新增回放档位** | 继承 `ReplayabilityBase`，实现 `ReplayabilityProtocol` 全部成员（`replayable`、`launch_args`、`launch_env`、`device_scale_factor`、`plan`），旋钮写成 `__init__` 关键字参数；启动开关与启动环境只从自己的设置生成，经 `LAUNCH_ARGS` 进浏览器（§4.14） | 以后的 `realistic` |
| **新增网络来源 / miss 策略** | 继承 `NetworkSourceBase` / `MissPolicyBase`，设 `__registry_key__`，实现 Protocol 全部成员；网络来源是不是在拦截（`intercepts`）、要不要隔离是策略自己的属性，旁边不加布尔；带受体的策略不是 dataclass、手写 `__init__`（§4.14 "回放"） | 以后的 `har`（从 HAR 回放）、`lenient`（miss 时放宽到 L2 再试） |
| **新增恢复器（检查点恢复策略）** | 继承 `RestorerBase`（`core/loops/restore/`），设 `__registry_key__`，实现 `restore(ctx) -> AsyncGenerator[RestoredStep, None]`（写成 `async def` + `yield`）：只恢复环境、不碰 memory，k < t 每步交回一条，最后一条是经校验的第 t 步观测；每个恢复出的观测恰好调一次 `env.verify_restoration`（capability 的计数类证据是自上一次调用以来的增量，§4.9）；`strict` 下第一个不通过的校验（观测、重定位或动作成败）就抛 `RestorationDivergedError`，不再执行后面的动作；不吞 `RunAbort`（`test_abort_handler_hygiene.py` 递归扫描 `core/loops/`） | 以后的 `capability_checkpoint`（Docker snapshot、Android 模拟器快照，经新增的 `RestorableCapability` 协议） |
| **新增分叉选步策略** | 继承 `StepSelectorBase`（`core/data_loader/task/step_selector/`），实现 `select(candidates) -> list[int]` | 按评测器给的关键帧选步 |
| **新增 Loop 输入** | 在 Loop 的 `__loop_inputs__` 里声明 `LoopInput(type, required)`；由任务加载器在 `TaskContext.loop` 里给出（§4.1、§4.10） | `checkpoint`（`BranchLoop`） |
| **新增提示贡献** | 配置 `prompt_contribution` Hook 的稳定 Python 函数引用及可选 wired helper；函数按公开 context 返回贡献，Agent 决定 placement（§4.1） | 首策略步一次或每策略步一次；历史重建只读原贡献 |
| **新增学生格式（训练导出）** | 一个新的 Agent 类（或已有 agent）实现 ReAct 的可选 slot `format_react_output(actions, *, thought=None)`：`parse_react_output` 的逆，写不出来的动作或参数抛 `UnrepresentableActionError`，满足 §4.15 的 round-trip；`tests/test_agents/test_format_react_output_round_trip.py` 自动覆盖每个定义了它的已注册 agent；文本协议放进 agent 自己的无状态编解码模块，slot 一行调它（`agents/ui_tars/codec.py` 的先例，§4.1）；学生的运行 YAML 就是服务时的那份 | v1 `ui_tars`；以后 Qwen3-VL 原生工具调用格式、OpenCUA |
| **新增 teacher 的 thought 读法** | teacher agent 实现可选 slot `parse_react_thought(llm_output) -> (text, source_field) \| None`（`@staticmethod`，只读回复，§4.1） | `browser_use` / `ui_tars` / `skyvern`（M32） |
| **新增导出格式** | 继承 `ExporterBase`（`spectragent/export/`），设 `__registry_key__`，实现 `format(rendered, images)`；只有自己旋钮的导出器写出完整的 `__init__` 签名（任务字段在基类签名里，conscribe 读子类的 `__init__`） | `llamafactory`、`ms_swift`（`rejected_messages` 做 DPO）、`trl` |
| **新增动作翻译器** | 继承 `ActionTranslatorBase`（`spectragent/export/translate/`），实现 `translate(sample, trace) -> Translation`：只做与学生无关的换算，capability 专属的知识放在 capability 的包里由它调用；输出过 `Params.model_validate` | 以后的 `bbox_to_point`、desktop 的 a11y 节点 → 坐标 |
| **新增度量 / 报告节（M33）** | 在 `spectragent/flywheel/metrics.py` 加一个只读 `RunTrace`（与数据集目录）的计算、在 `report.py` 加它的节模型与建议规则（阈值写成具名常量、进 `rule`）；分母为 0 时是 `null` 加原因；capability 证据的格式知识放进该 capability 的包（纯函数，只经 `ArchiveReader` 读），度量只组织调用（§4.16） | 每条数据线的 miss 成本；第二条数据线出现时每个 capability 的度量贡献成为一根轴 |
| **新增 thought 来源（`relabel`）** | 今天是 `thought_source: Literal["teacher", "none"]`；`relabel`（固定动作、采样 K 个 thought、保留一致的）有了实现时成为新注册表（§6 与 AGENTS.md §2.2 同时加行） | OpenCUA、arXiv 2504.10127 的做法 |
| **新增 Mouse Trajectory / Typing Rhythm 策略** | 继承 `MouseTrajectoryBase` / `TypingRhythmBase`，设 `__registry_key__`，实现 Protocol 全部成员（含 `click_hold_ms` / `aim_offset_px`），knobs 写成 `__init__` 关键字参数 | 新的运动模型；已训练模型的新导出格式属于 `learned` 的 backend，不是新策略（§4.13） |
| **新增注册表（全新变化轴）** | `create_registrar(...)` + `Base(metaclass=...Meta)`；**同一次改动**里把行写进 §6 与 AGENTS.md §2.2（`tests/test_docs/test_registrar_coverage.py` 会核对，判别字段必须与 `discriminator_field` 逐字一致）；若这根轴要由 YAML 选择，还须在 `core/registries.py::collect_registrars` 注册，否则字段退化成 `Literal[<keys>]`，策略的旋钮全部无法从 YAML 到达（AGENTS.md §3.4）。注册表还是空的时候不能成为 Composer 层（`composer.py` 对没有实现的已收集层直接抛错，`generate_all` 和 Composer 都拒绝空层，必须先注册实现）：先创建、先写文档，在注册第一个实现的那次改动里再收集 | 滚轮节奏、触屏手势。反例：`NegotiationResultRegistrar` 不由 YAML 选择，因此正确地不在 Composer 里。`ArchiverRegistrar`：M28 创建，M29 随首批归档器收集 |
| **配置字段扩展** | 给注册类添加 `Annotated[type, Field(...)]` | 自动出现在生成的 `_composed.py` config schema 中 |

---

## 10. 依赖关系图

```
                    conscribe (auto-register + config typing + __wiring__)
                         │
                         ▼
              ┌─── All Registrars ─────────────────┐
              │                                     │
    ┌─────────┼──────────────┬──────────────────────┼──────────────┐
    │         │              │                      │              │
    ▼         ▼              ▼                      ▼              ▼
  Agent    AgentLoop     LLM Provider         Capabilities      Data Loader
  (agent_id) (loop_id)  (model_type.         (capability_id)   (benchmark)
    │  __wiring__  │      provider)         ┌──────┼──────┐        │
    │←────────────→│         │              ▼      ▼      ▼        │
    │              │         ▼           Docker Browser  GPU...    │
    │              │    Message Ser.    (各自: actions + resolvers  │
    │              │         │            + backend wired field)   │
    │              │         │                    │                │
    │              │         │                    ▼                │
    │              │         │       BrowserProviderRegistrar      │
    │              │         │       (core/capabilities/browser/   │
    │              │         │         backends/local_chrome,...) │
    ├── Memory     │         │                    │                │
    │              │         │                    ▼                │
    │              │         │            Environment              │
    │              │         │       (__wiring__["capabilities"]   │
    │              │         │        注入 caps; act 按前缀路由)     │
    │              │         │                    │                │
    └──────┬───────┘         │                    │                │
           ▼                 ▼                    ▼                │
        ConfigComposer (validate → leaves-first → setattr inject)  │
           ▼                                                       │
        ComposedRun                                                │
           ▼                                                       │
      AgentRuntime.from_composed(run)                              │
       ├── prepare(): slot 校验 → negotiate obs/action             │
       │              → negotiate memory/archive                   │
       │              → Weaver 织入 → loop.weave(agent)             │
       │              → patch agent.run = closure(loop._execute)   │
       │              → 拦截审计 → env.bind_archivers（有消费者时） │
       ├── execute(task): on_run_start → asyncio.wait_for(         │
       │                  agent.run(task), timeout=...)            │
       │                  → on_run_end                              │
       │                                                           │
       │ AOP 拦截：llm.ainvoke → TraceWriter 登记 LLM 调用             │
       │                         同一本调用账累计 token       │
       │           loop.record_step → TraceWriter 拉取原始           │
       │                              env.collect_archive()        │
       │                              → commit_step(extra_files)   │
       │                              → manifest 原子提交          │
       │                                                           │
       └── 输出 AgentResult                                          │
                │                                                  │
                ▼                                                  ▼
           AgentResult ──→ RunTrace (full persistence)     TaskDefinition
                               │   env/：archive_files /    EvalReference
                               │   iter_archive / blob /
                               │   archive_run_file
                               ▼
                          Evaluator ──→ EvalResult ──→ EvalSummary
```

**归档相关的 import 方向**（`tests/test_core/test_archive/test_archive_boundaries.py` 用 AST 守着：每一条 import 都算，函数里的、`if TYPE_CHECKING:` 下的也算，因为只为类型的依赖仍是设计上的依赖；名字 `TraceWriter` 从哪个模块 import、或经属性访问都算；包 `__init__` 顺带 import 了什么不在这几条的视野里，它们管的是每个模块自己点了哪些名字）：

- `core/runtime.py`、`core/negotiation/*`、`core/environments/*` 不 import `core.hooks.trace_writer`，也不 import 任何定义具体归档器的模块：runtime 只认 `ArchiveConsumer`，environment 只认 `ArchivingCapability`；
- `core/archive/*` 不 import `core.trace`、`core.hooks`、`core.negotiation`、`core.environments`、`core.capabilities`。**唯一的例外**是 `core/archive/source.py`（M30）：回放的打开器 `open_archive` 用 `core.trace.reader.RunTrace` 实现 `ArchiveReader`，只 import 这一个模块；包的 `__init__` 不 import `source`，所以 `import spectragent.core.archive` 永远不会带进 trace 层（`core/trace` → `core.archive.privacy` 这个方向因此不成环），测试按模块名钉住这一条例外；
- `core/trace/*` 从 `core.archive` 只 import `core.archive.privacy`（`TaskSetupRecord` 里的台账类型）；归档文件以鸭子类型跨进 trace 层，WAL 的接口仍然只有 `TraceFile`；
- browser 的归档器、回放档位与离线回放（`core/capabilities/browser/archive/`、`replay/`、`recorder.py`）只从框架 import `core.archive`（类型、`ArchiverBase`、`privacy`、回放读侧的 `ArchiveReader` / `open_archive`），不 import `core.trace` / `core.hooks`，因为 capability 不知道 WAL；`core.hooks.trace_writer` 也不 import 任何具体归档器，归档器配置经注册表进入它的生成配置；
- 新的归档与协商模型里没有 `price` / `cost` / `usd` / `dollar` 字段：只记 token，不记钱（2026-09-19）。

**分叉相关的 import 方向**（M31）：`core/loops/branch.py` 与 `core/loops/restore/*` 经 `core.trace.reader.RunTrace` 读源 trace（`SourceRun` 是唯一的入口，只读已提交的部分），反方向不成立：`core/trace/*` 不 import `core.loops`；capability 一侧的 `RestorationSupport` 实现（browser 的 `restoration.py`）只读传进来的观测，不 import `core.trace` / `core.loops`；`core/environments/*` 只认 `RestorationSupport` Protocol 与 `schemas/restoration.py` 的类型。

**导出相关的 import 方向**（M32，`tests/test_export/test_export_boundaries.py` 用 AST 守着）：`spectragent/` 下 `export/` 之外没有任何模块 import `spectragent.export`（`core/registries.py` 的收集、以及它生成的 `generated/_composed.py` 引用导出旋钮类型除外）；`export/*` 经 `core.trace.reader.RunTrace` 读源 trace，经注册表与 Protocol 拿到导出器与翻译器（`runner.py` 不 import 具体的导出器或翻译器类）；`schemas/sample.py` 不 import `spectragent.core`；browser 一侧的导出知识（`index_points.py`、`capture_check.py`、`dom/cleaning/skyvern_hit_test.py`）只读交进来的 fragment、文档与配置，不 import `core.trace` / `spectragent.export`；新模型里没有 `price` / `cost` / `usd` / `dollar` 字段。导出的叶子规则有且只有一个例外：度量闸门的链（`spectragent/flywheel/chain.py`）把 `spectragent export` 当作一段来调，只 import `spectragent.export.cli`（测试按模块名钉住）。

**度量相关的 import 方向**（M33，`tests/test_flywheel/test_flywheel_boundaries.py` 用 AST 守着）：`spectragent/` 下 `flywheel/` 之外没有任何模块 import `spectragent.flywheel`；`flywheel/*` 经 `core.trace.reader.RunTrace` 读 trace、经 `core/trace/sources.py` 找 run 与任务身份、经 `core/trace/usage.py` 读每个 run 的调用与用量，从 `spectragent.export` 只有 `chain.py` 的那一个例外（`metrics.py` / `report.py` 读数据集只读文件，不 import 导出）；browser 一侧的度量知识（`archive_stats.py`、`replay_log.iter_decisions`、`archive_index.iter_index_lines`）只经 `ArchiveReader` 读，不 import `core.trace` / `spectragent.flywheel`；报告与链的模型里没有 `price` / `cost` / `usd` / `dollar` 字段。

外部核心依赖：
- `conscribe` — 注册 + 配置生成
- `bubus` — 事件总线 (watchdog 事件分发)
- `agent-cdp` — Browser Capability 内部中间层 (BaseEvent, WatchdogBase, ScopeGroup, EventScope)
- `pydantic` v2 — 数据契约
- `websockets`（≥ 15，`proxy` 参数）— CDP WebSocket 连接（Browser Capability 内部）
- `warcio`（Apache-2.0，唯一的依赖是 `six`）— `browser.network` 写 WARC（M29）。许可证陷阱：`py-wacz` → `cdxj-indexer` → `surt`（AGPL），这三个永远不进依赖，锁文件测试守着；WACZ 打包与 SURT/CDXJ 需要时自己实现

---

## 11. Capability 内部中间层示例：Browser (agent-cdp)

每个 capability 各自管理自己与 backend 之间的中间层协议。以 Browser Capability 为例，内部通过 `agent-cdp` 实现事件驱动架构：

```
Browser Capability
├── agent-cdp 中间层
│   ├── 控制连接 CDPClient (WebSocket → Chrome/Chromium；agent 的 tab、观测、动作、viewport)
│   ├── recorder 连接 CDPClient（只在绑定了归档器或 replayability ≠ none 时；§4.14）
│   │   ├── browser 级 auto-attach + waitForDebuggerOnStart，逐 target 流水线准备，finally 里 resume
│   │   ├── 暂停类事件（Fetch.requestPaused、等待中的 attach）→ 派生的 task，finally 里 continue / resume
│   │   ├── 贡献者声明的元数据事件 → 本地分发器（逐贡献者准入，带 session id；字节进侧存储）→ archive EventScope
│   │   └── 回放服务（network_source: archive，M30）：Request 阶段 Fetch 的主人，匹配归档 → fulfill / failRequest，
│   │       在暂停它的 session 上应答；未应答的一律 failRequest，从不 continue；决定 → 回放日志
│   ├── ScopeGroup
│   │   ├── browser EventScope → CrashWatchdog
│   │   ├── archive EventScope（browser 级，随 recorder 连接建与关；只在绑定了归档器时）
│   │   │   ├── ← ArchiveWatchdog（shared，provides ∅，按绑定安装）→ 已绑定归档器的运行期会话
│   │   │   └── ← tab 观测 scope 的 BrowserStateRequestEvent（target_scope；纯视觉 run 的 dom_raw 自采）
│   │   └── 每个已接管 tab（同一 tab/CDP session）
│   │       ├── 观测 EventScope → DOM / Action / Screenshot / Meta 等 Watchdogs
│   │       └── JS 执行 EventScope ← 跨 scope QUEUED 连接
│   │                              ← 共享 JavaScriptExecutionWatchdog 的 handler
│   ├── DOM Pipeline (策略选择 capture → clean → budget → serialize)
│   │    └── raw_capture_sink → 本 cycle 缓存 → 提交的 cycle → browser.dom_raw（§4.12）
│   ├── 回放 replay/（ReplayabilityRegistrar：none / pinned；NetworkSourceRegistrar：live / archive；
│   │    MissPolicyRegistrar：strict；canonical / matcher / site_rules / page_urls / media_areas / archive_index / responder / server，§4.14）
│   ├── 归档器 archive/（browser.network / browser.storage / browser.dom_raw / browser.frames / browser.replay_log + 环境指纹，§4.14）
│   └── Input 拟人化层 (input/, §4.13)
│        ├── MouseTrajectoryRegistrar  → HumanMouse    → Input.dispatchMouseEvent
│        ├── TypingRhythmRegistrar     → HumanKeyboard → Input.dispatchKeyEvent
│        └── play_on_deadline —— 两条链共用的唯一时钟（绝对截止时间）
│
└── Backend (core/capabilities/browser/backends/, IoC-injected via __wiring__["backend"])
    ├── local_chrome, lexmount, skyvern, browser_use,
    │   agentbay, airtop, hyperbrowser
    ├── 每个 backend 提供 async open() → ProviderSession.cdp_url
    │   capability.setup() 调 backend.open() 拿到 cdp_url 后启 agent-cdp
    └── __capabilities__ 声明支持什么：NETWORK_CAPTURE / LAUNCH_ARGS / NETWORK_ISOLATION 只有 local_chrome（§4.9.x）
```

两个 per-tab scope 是 Skyvern JS 提取的编排选择，不是 agent-cdp 的基数约束：
scope 与 watchdog/handler 为 N:M。当前 DOMWatchdog 因持有该 tab 的 selector map 和 frame
发现状态而按 tab 建实例；JS 执行 watchdog 一个实例服务多个 tab，只维护从 CDP
attach/detach 事件得出的 tab→iframe session 归属表，以拒绝跨 tab 的 JS 请求，不持有
任何 tab 的 DOM 提取缓存或 cleaner 配置。
同一 scope 的 QUEUED handler 逐个等待，观测 scope 通过 `target_scope` 投递到另一个
JS scope 后可等待其结果而不自等待。`_attach_tab()` 成组创建并绑定两个 scope；
`switch_tab()` 与跟随新 tab（§4.14 "页面新开的标签页"）的懒附加走同一路径，`close_tab()`、
`close_focused_tab()`（浏览器的关闭 tab 快捷键，§4.13）和 teardown 成组释放（页面自己关掉的 tab 先放开回调与 watchdog，两个 scope 留到 teardown）。

archive scope 不是第三个 per-tab scope，而是 browser 级的一个：recorder 连接在控制连接接管
一个 tab 之前（弹窗）就收到它的流量，per-tab 的 scope 那时还不存在。它与 recorder 连接同生
同灭（setup 在第一个 tab 之前建，teardown 时一起关），tab 仍然成组创建、释放那两个 scope，
`bind_tab_scopes(observation_scope, js_scope, *, session_id)` 的签名不变；需要与 cycle 同步
采集的共享 watchdog（ArchiveWatchdog）在这个钩子里把 tab 的观测 scope 跨 scope 连到 archive
scope（§4.14）。

`BrowserProfileConfig` 是命名预设（`standard` / `skyvern` / `vision_only` / `lightweight`），但只承载两类事实：**安装哪些 watchdog**（`watchdogs_exclude`，反向声明——注册表里没被排除的都装，名字在 `prepare()`（`check_setup`）与 setup 时对着注册表校验，写错是硬报错而不是静默 no-op），和**允许花多少次 CDP 往返**（`js_click_listener_detection`、`cross_site_iframe_capture`），外加 SecurityWatchdog 的域名白 / 黑名单。唯一的例外是**按绑定安装**的 watchdog：`BrowserWatchdog.should_install(ctx)`（类方法，默认 True）在建 watchdog 的地方与 profile 一起求值，ArchiveWatchdog 只在绑定了归档器、recorder 连接开着时安装（§4.14）。记不记录是每次 run 的选择（D3），不是 profile 的属性；profile 排除了 `archive` 而 run 又绑定了归档器，`prepare()` 报错（`check_setup`，随后 `bind_archivers` 绑定前再查一遍；setup 再查一遍）。

**策略一律不在 profile 上**：`dom_cleaner`、`dom_serializer`、`mouse_trajectory`、`typing_rhythm`、`replayability`、`network_source` 全部是 `BrowserCapability` 上的 `__wiring__` 受体（§4.12 / §4.13 / §4.14）。profile 曾经用字符串名持有 cleaner 与 serializer，后果是消费者拿着名字去注册表里 `get(name)()` 现造一个默认实例，把 Composer 已经按 YAML 构造好的那个丢掉——策略的 knobs 生成在 config 里却无人可达。profile 也不支持 override 或增量修改：一个预设要么被整体选中，要么另起一个。区分规则是「谁为它买单」——改变一次 capture 花多少次 CDP 往返的，属于 profile；改变产出的 observation 让 prompt 花多少 token 的（`screenshot_quality`、`dom_max_elements`），是 capability 自己的 `Annotated` 标量。

其他 capability 的中间层各不相同：Docker 用 Docker Engine API（`core/capabilities/docker/backends/` P2 占位），Android 用 ADB + gRPC，Desktop VM 用 VNC + AT-SPI，GPU Profiling 用 Unix socket IPC → bare-metal harness。这些都是 capability 的实现细节，framework 不感知。

---

## 12. 实施优先级

| 优先级 | 工作项 |
|--------|-------|
| **P0** | AgentRegistrar (flat) + AgentLoopRegistrar (flat) + Loop Protocol 体系 |
| **P0** | AgentRuntime 提取（协商 + AOP 织入 + timeout + guarded_act） |
| **P0** | conscribe `__wiring__` Mode 2 三元素 tuple（`optional_keys`）+ `WiringSpec` 扩展 |
| **P0** | ObservationRegistrar + Fragment 注册 + `__source__` + 三方协商 + Resolver 模式 |
| **P0** | HookRegistrar + AOP Weaver（Intercept + AOPProxy）+ TraceWriter hook（全量落盘 + WAL commit） |
| **P1** | CapabilityRegistrar 扩展：BaseCapability = action 执行 + observation resolver |
| **P1** | EnvironmentRegistrar + `__wiring__` 注入 capabilities + 前缀路由 |
| **P1** | `action_filter` config 收窄 + guarded_act 实现 |
| **P1** | TaskLoader / EvalLoader 去掉 Browser 前缀，泛化接口 |
| **P1** | EvaluatorRegistrar + RunTrace lazy loader |
| **P2** | DockerCapability 实现（actions + terminal/filesystem resolver） |
| **P2** | CodeActLoop 实现 + DockerCapability 集成 |
| **P2** | 数据飞轮（browser）：离线回放（M30）；状态分叉——`BranchLoop` + `RestorerRegistrar`（`prefix_replay`）+ `RestorationSupport`（browser 重定位与校验）+ `trace_branches` 加载器与 `StepSelectorRegistrar` + `terminal_observation`（M31）；训练导出——`ExporterRegistrar`（`openai_messages`）+ `ActionTranslatorRegistrar`（`identity` / `index_to_coordinate`）+ `CanonicalStepSample` + 学生渲染（ReAct 可选 slot `format_react_output` / `parse_react_thought`）+ 校验器 + `spectragent export` + `skyvern_hit_test` cleaner（M32）；度量闸门——`spectragent/flywheel/`（度量、报告、链）+ `spectragent flywheel` + 选步 `last` 与原路径回放配置 + 表单提交的观测修复（M33） |
| **P3** | FunctionCallingLoop + ToolSandboxCapability |
| **P3** | PlanExecuteLoop |
| **P3** | GPUProfilingCapability（Docker + bare-metal harness split 架构） |
| **P3** | DesktopVMCapability / AndroidCapability |

---

## 13. 已决定的设计问题

### 13.1 LLM 所有权（已决定）

**LLM 作为简单 IoC**：Agent 通过 `__wiring__` auto-discovery 引用全部 LLM（`"llm": "llm"`），用户在 YAML 配置即可。AgentRuntime 从 config 创建 LLM 实例，注入 Loop.run()。每个 agent 可独立配置不同 LLM（实验场景）。真有 agent 对 LLM 有限制再通过 `__wiring__` 收窄。

### 13.2 Memory 协商（已决定）

Agent 通过 `__wiring__` 选择并注入 Memory 策略，配置校验覆盖可选实现及其子配置。Loop 声明需要的消息协议（`user_assistant` / `tool_call` / `planning` 等），Memory 声明支持的协议；`AgentRuntime.prepare()` 检查 `Loop requires ⊆ Memory supports`，不把组件行为兼容性委托给配置生成。详见 §4.8。

### 13.3 Observation 二次协商（已决定）

**两层检查，职责不同**。配置校验保证 wiring 引用的 fragment 键和子配置有效；选定 Agent、Loop、Environment 后，`AgentRuntime.prepare()` 执行三方协商，检查 required 是否由本次环境提供，并在失败时点名请求方和缺失 fragment。配置有效不代表组件组合在行为上兼容。

### 13.4 Observation 完整序列化（已决定）

观测是否保存由 RecordingPlan.observations 明确选择。选择时序列化实际 NegotiatedObservation，包括恢复前缀与终止观测；未选择时不进行额外复制和编码。分叉、回放、导出与评测在使用前检查自身所需记录范围和完整前缀。

### 13.5 ToolBench 动态 tool 定义来源（已决定）

Tool 定义来自 **TaskLoader.get_task_context()**，每个 task 提供不同的 tools。流程：TaskLoader 提供 tools → AgentRuntime 传给 ToolSandboxCapability.setup(task_tools=...) → Capability.get_action_descriptors() 动态生成 → FunctionCallingLoop 转换为 LLM tools 参数。每换一个 task 重新 setup + 重新生成 descriptors。

### 13.6 conscribe 三元素 tuple（已确认）

conscribe 0.6.1 将支持 `__wiring__` Mode 2 三元素 tuple（`required + optional`），`WiringSpec` 扩展 `optional_keys` 字段。

---

## 14. 待决问题

### 14.1 Memory 消息协议的具体设计

当前协议键已有 `user_assistant`、`tool_call`、`planning`、`code_execution`，Loop 需求和 Memory 支持范围按 §4.8 在 `prepare()` 比较。新 Loop 引入 `planning` 或 `code_execution` 时，仍需定义这些协议对应的消息类型，以及 Memory 如何保存、裁剪并回放它们；这属于组件契约设计，不是把协议键写进 Memory 的 `__wiring__`。

### 14.2 Hierarchical / Multi-agent 编排

DD 1.4 提到走独立的 A2A 通讯协议设计，不属于 agent loop 范畴。具体设计待定。

### 14.3 LLM 客户端的生命周期（已决定）

已按 §4.7 实施执行内惰性复用与显式收尾。生命周期归一次任务、独立评测批次或显式 Python
`model_session`，不属于 AgentLoop 策略，也不通过 YAML wire id 建立共享池。云浏览器 lite SDK 的
生命周期审计仍延期；不因模型客户端改动扩大到浏览器后端。
