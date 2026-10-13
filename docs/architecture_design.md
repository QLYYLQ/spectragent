# All4One Agent Framework — Architecture Design Document

> **Current package name**: `browseruse_bench` (v0.2.0)
> **Target vision**: All4One Agent Framework — 通过统一的抽象和 config-driven 机制，支持所有场景的 benchmark 与 agent 评测（browser-use、tool-use、SWE agent 等）。

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

### 1.2 类比：Spring-like Config-Driven

框架的组件发现和组装机制与 Spring 类似：

| Spring 概念 | All4One 对应 |
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
│   summary)     │   P3 待实现       │                              │
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
AgentRegistrar = create_registrar("agent", BaseAgent, discriminator_field="agent_id")
AgentLoopRegistrar = create_registrar("agent_loop", AgentLoop, discriminator_field="loop_id")
```

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

    def _validate_slots(self, agent: Any) -> None:
        """callable(getattr(agent, slot)) — 缺 slot 即 raise IncompatibleError"""

    def check_loop_inputs(self, inputs: Mapping[str, Any]) -> None:
        """键与类型对照 __loop_inputs__ — 多的、缺的、类型不对的一次报全：IncompatibleLoopInputError"""

    def weave(self, agent: Any, **deps: Any) -> Any:
        self._validate_slots(agent)
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
                          "format_react_output", "parse_react_thought"}   # 后两个：训练导出（M32，见下）

    async def _execute(self, agent, task, *, ctx, **_):
        # deps 每次调用经 ctx 传入，不存在 self 上
        run = LoopRun.from_ctx(agent, task, ctx, ...)   # 一个 run 的依赖与账本（core/loops/run.py）
        for step_idx in range(self.max_steps):
            result = await self._run_step(run, step_idx)   # observe → build_prompt → LLM → parse → act
            if result is not None:
                return result
        ...
```

#### 可选 slot：Loop 声明，谁用谁校验（M32）

`__optional_slots__` 列的是"有就用"的 agent 方法，用的人不一定是 Loop 自己。`prepare()` 的 slot 校验只查 `__required_slots__`；可选 slot 由**用它的那一方**在用之前校验：

| 可选 slot | 谁调用 | 何时校验 |
|---|---|---|
| `get_react_output_model()` | `ReactLoop` 自己（每次 LLM 调用的 `output_format`） | 不校验：没有就是文本调用 |
| `prepare(task, action_descriptors, max_actions_per_step=...)` | `AgentRuntime`（种子消息，§4.2 `seed_messages`） | 不校验：没有就没有种子消息 |
| `format_react_output(actions, *, thought=None) -> str` | 训练导出器（学生一侧，§4.15） | 只在导出组装学生时 |
| `parse_react_thought(llm_output) -> tuple[str, str] \| None` | 训练导出器（teacher 一侧，`thought_source: teacher`） | 只在导出读 teacher 的 thought 时 |

后两个都是 ReAct 的 slot，所以写在 `ReactLoop.__optional_slots__` 里（`BranchLoop` 继承）；不另立 `__inverse_slots__` 之类的新机制，也没有导出专用的错误类型。

- **`format_react_output`** 是 `parse_react_output` 的逆：把一步的（已翻译、全限定名的）动作和可选的 thought 写成本 agent 的回复文本，正是这个 agent 在服务时会被要求写出的那种。它读本步的 agent 状态，所以调用顺序固定：`prepare` → 本步的 `build_react_prompt(obs)` → `format_react_output` → `parse_react_output`（UI-TARS 的点要用本步截图的网格换算）。确定性：同样的输入同样的文本。动作名或某个参数这个格式写不出来时抛 `UnrepresentableActionError(action_name, params, detail)`（`core/loops/errors.py`，`ParseError` 的对偶），绝不静默丢掉；导出器把它当成 `untranslatable` 的原因 `unrepresentable`（§4.15）。round-trip 的定义（§4.15 "round-trip"）对每个实现都成立，`tests/test_agents/test_format_react_output_round_trip.py` 对每个定义了它的已注册 agent 类逐一检查。v1 只有 `ui_tars` 实现它。
- **`parse_react_thought`** 从一次录下的回复里读出 thought 及其来源字段，读不到返回 `None`。它是纯函数（只读传进来的回复，不读实例状态，所以写成 `@staticmethod`），导出器按源 trace 的 `agent_id` 从 `AgentRegistrar` 取 teacher 的类直接调用，不构造实例。`browser_use`：`thinking` > `next_goal` > `memory`（第一个非空的，记下是哪个字段；flash 模板只教 `memory`）；`ui_tars`：原生的 `Thought:`（到第一个 Action 行为止）；`skyvern`：每个动作的 `reasoning` 按顺序以换行连接。
- **校验**：导出器组装学生之后先照常 `loop._validate_slots(agent)`（`prepare()` 已做），再查 `callable(getattr(agent, "format_react_output", None))`；`thought_source: teacher` 时对 teacher 的类查 `parse_react_thought`。缺了抛已有的 `IncompatibleError(agent_name, loop_id, missing_slots, needed_for=...)`。`needed_for` 是 M32 加的措辞参数：默认（运行装配）写 "required by loop 'react'"，导出写 "needed for training export (optional slots of loop 'react')"，不让一个可选 slot 的缺失读起来像 Loop 的必需 slot。

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

    # 共享领域知识（一份代码）
    domain: str = "software_engineering"

    # 满足 ReactLoop.__required_slots__ 的方法
    def build_react_prompt(self, obs): ...
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
            memory.add_messages(agent.build_react_prompt(obs))
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
| `llm.ainvoke` | after | 每次 LLM 成功返回后 | MetricsCollector 累加 token |
| `llm.ainvoke` | around | 每次 LLM 调用：返回、抛异常或被取消 | TraceWriter 在 `proceed()` 之前登记这次调用，按结局记为 completed / failed / cancelled，并测量等待时间；MetricsCollector 补记计费了的失败回复（校验失败、截断） |
| `loop.record_step` | after | Loop 交出完整 StepRecord 后 | TraceWriter 一次 `commit_step` 提交本步观测、全部 LLM 调用记录和 step record，manifest 原子重写即提交点 |
| `env.act` | after | 每次 action 执行后 | 可选：action 级别追踪 |
| `agent.build_react_prompt` | around | prompt 构建时 | 可选：prompt 审计（同步方法，`proceed()` 直接返回值而非 awaitable） |

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

#### 一个 run 与一步：`LoopRun`、`ReactLoop._run_step`（M31）

`BranchLoop` 的策略步与继续步就是普通的 ReAct 步，所以 `ReactLoop._execute` 拆成三样（行为不变，原有的 `test_react_*.py` 一字不改地通过）：

- **`LoopRun`**（`core/loops/run.py`，与 Loop 无关的一个 run 的状态）：从 `ctx` 取出的依赖（env、llm、memory、act_fn、negotiated_obs、task_id、step_recorder、model_id）与每个 Loop 都记的账：开始时刻、`step_records`、`action_history`、`llm_calls`、上一步的记录、目前最好的答案、连续解析失败的步数，以及 token 账本 `usage`。账本对象本身仍是 `react.py` 的 `_UsageTally`；它和 `_merge_token_usage`、`_parse_feedback`、`_build_result` 等现有私有工具的归属另有交接记录（`HANDOFF_261010_react_loop_layering.md`），M31 不搬它们，也不再往 `react.py` 加模块级私有工具。
- **`ReactLoop._run_step(run, step_idx, *, observation=None, restoration=None)`**：一个 ReAct 步（observe → 填 loop 来源 fragment → `build_react_prompt` → LLM 与步内重问 → 解析 → 截断 → act → 交出 StepRecord）。返回终止的 `AgentResult`，或 `None` 表示继续；`RunAbort` 照原样上抛。给了 `observation` 就不再 observe（分叉的策略步用恢复器已经校验过的那次观测，再 observe 会开一个新 cycle）；给了 `restoration`，这一步写出的每条记录都带上它（§5.3）。
- **ReAct 自己的 memory 协议是 `ReactLoop` 的方法**，在线的步与 `BranchLoop` 的重放只用这一份：`_cap_actions(actions, step_idx)`（按 `max_actions_per_step` 截断，返回保留的动作和被丢弃的动作名）、`_remember_rejected(memory, output_text, exc)`（被拒的回复作为 assistant 轮，再加 `_parse_feedback` 的 user 轮）、`_remember_reply(memory, output_text, dropped)`（解析成功的回复作为 assistant 轮，有动作被丢弃时再加截断提示）。训练导出的学生渲染（§4.15）写较早的 assistant 轮也走 `_remember_reply`（`dropped` 恒为空），是这份协议的第三个使用者，不是第二份拷贝。
- **loop 来源 fragment 的 builder 是显式入参的纯函数**（M32）：`core/loops/_loop_fragments.py::loop_fragment_builders(*, task, step, max_steps, today, history, prev_step)`，返回四个 `loop.*` 的 builder 交给 `populate_loop_fragments`。`ReactLoop._loop_fragment_builders(run, step_idx)` 只是把本 run 的值（`self.max_steps`、`self._today_iso()`）交给它；`BranchLoop` 交源 run 第 t 步录下的 `max_steps` / `today`（`today_override` 仍优先），不再整个覆写 step_info 的 builder；导出器交源 trace 录下的（§4.15 "学生渲染"）。三处只有一份构造逻辑，它本来就该在与 Loop 无关的 `_loop_fragments.py` 里（`HANDOFF_261010_react_loop_layering.md` 3.6 的 B 类）；`today_override` / `max_steps` 两个旋钮不动。

**步数只数策略步。** `AgentMetrics.steps` 数 `origin == "policy"` 的记录（§5.3），`max_steps` 也只数策略步；恢复出来的步与终止观测不占步数，也不进 `AgentResult.action_history`。

#### 终止观测：`terminal_observation`（M31）

`ReactLoop.terminal_observation: bool = False`：run 结束之后再看一次世界，记成一条 `origin="terminal"` 的 StepRecord（不调 LLM、没有动作，步号接在最后一条记录之后）。这个开关是通用的：`online_mind2web` 按截图打分，最后一个动作之后的那张截图它同样用得上。

- **何时**：done、`max_steps` 用尽、连续解析失败到上限，以及 `BranchLoop` 的步数预算用尽。**从不**在基础设施失败（`_abort`）、动作失败、恢复发散和任何 `RunAbort` 路径上。
- **结果在前**：终止的 `AgentResult` 在决定结局时就建好，终止观测不改它的 `agent_done`、`env_status`、`answer` 与 `end_to_end_ms`（后者不含这次观测）；唯一的例外见下一条：交出终止记录失败时，失败的结局在 `error` 后附上原因。
- **不致命**：observe 抛普通异常时记一条带 `error_phase="observe"` 的终止记录，run 的结局不变。guard 在这次 observe 上否决：先把这条终止记录作为被否决的一步交出，再上抛否决（否决优先于已经定下的结局，§4.2）。交出终止记录失败不顶替结局（与"记录失败不顶替 run 已经定下的结局"同一条规则，§4.6）：失败的结局在 `error` 后面附上原因，成功的结局只记日志，什么也不抛；trace 记为 `incomplete`（完整到最后一个策略步，原因写明，§4.6 "尾部失败不抛错"）；交出被取消切断时取消带着结局离开（`carry_outcome`）。
- **分层**：observe 并建出终止记录是 `AgentLoopBase._observe_terminal(env, negotiated_obs, *, step)`（任何 Loop 可用，自己不交出记录）；交出与结局优先级用的是 `ReactLoop` 现有的交出方法。终止观测只有 env 来源的 fragment：没有 agent 看它，所以 loop 来源的 fragment 不填。

#### `BranchLoop`：从检查点分叉（M31）

分叉 = 把环境恢复到源 run 第 t 步之前的状态 + 一个策略步 + 可配置的继续步数。恢复的方式是策略（Loop 的 `restorer` 受体，§4.1 下文与 §6），元素重定位与恢复校验是 capability 的知识（§4.9 `RestorationSupport`），teacher 是被测对象（wired 的 LLM，采样参数在 LLM 配置里，可以与源 run 不同；第 t 步可以多给一条固定指令，见下文"teacher 采样"）。

```python
@dataclass
class BranchLoop(_BranchLoopReceptors, ReactLoop):      # restorer 只在普通 mixin 上注解（AgentReceptors 写法）
    __registry_key__ = "branch"
    __wiring__ = {"restorer": ("restorer", ["prefix_replay"])}
    __loop_inputs__ = {"checkpoint": LoopInput(Checkpoint)}
    continue_steps: int = 0              # ge=0；策略步之后再走几个 ReAct 步
    terminal_observation: bool = True    # 覆盖 ReactLoop 的默认
    branch_instruction: Literal["none", "prefer_different_action"] = "none"   # 第 t 步的固定指令（闭集）
```

**装配期（`weave`，在 `prepare()` 里、任何 setup 之前）**：打开检查点的源 trace，对照当前装配，不符就抛 `IncompatibleCheckpointError`，点名源 run 以及两边的值：

- 源 trace 已关闭（manifest 不是 `writing`）、且完整到第 t 步（`t ≤ complete_through`，§5.3）：trace 自己那本账是硬条件，不论检查点是谁选的（`trace_branches` 从不给出这样的步，§4.10）；第 t 步已提交且 `origin == "policy"`、它的观测已记录（`error_phase != "observe"`）、0..t 每一步都已提交；（比当前格式旧的 trace 根本读不出来，`SourceRun.open` 照"没有可读的 trace"拒绝，§5.5）源 run 的 `loop_id` 是 `react`（重放用的是 ReAct 的 memory 协议）；源 run 自己不是分叉 run（`TraceConfig.branch` 为空：分叉 run 的前缀没有 LLM 调用可重放，要分叉就从它的源 run 分叉）。
- 同一个 agent（默认 teacher 必须是源 run 的 agent 类）：`agent_id` 相同；源 `resolved` 里这个 agent 条目上记下来的每个标量旋钮（闭集值与数字原样记录，§5.6：`observation_mode`、`coordinate_space`、`prompt_template`、Skyvern 的 `action_history_window`……；`"<omitted>"` 的不比）与当前 agent 实例的同名属性相同；memory 的 `memory_id` 与它记下来的标量旋钮（`max_messages`、`window_size`……）相同；loop 那一块只比挑出来的一项：`agent_loop.max_actions_per_step` 与本分叉 Loop 的相同（它写在种子 prompt 里，也决定重放丢掉几个动作；两边 loop 的其余设置本来就不同：`loop_id`、`max_steps`、`terminal_observation`）；`TraceConfig.negotiation` 的观测与动作两部分（激活与必需的 fragment、激活的动作、两张绑定表）与本次协商相同；归档那两个字段不比，分叉记录什么证据是它自己的选择（裁决 D3）。
- 自由字符串的旋钮（没标 trace-safe 的，如 BrowserUse 设过的 `extend_system_message`）在源 trace 里是 `"<omitted>"`（§5.6），装配期比不了；数字原样记录之前写下的源 trace 里 `max_actions_per_step` 也是 `"<omitted>"`。它们由运行期的种子与重放逐条校验兜住（下一条），在任何动作执行之前。llm 那一块从来不比：teacher 的模型与采样可以与源 run 不同。

**运行期（`_execute`）**：

1. **种子消息**：runtime 已写入的 `agent.prepare` 种子消息（三个 browser agent 都只有一条 system 消息），必须与源 run 第 0 步第 0 次调用的 prompt 开头逐条相同，否则 `IncompatibleCheckpointError`（system prompt 里有任务、动作表与 `max_actions_per_step`）。比较的是 trace 记录的形状（`core/loops/restore/source.py::recorded_form`：活的消息走记录器同一条路，先做 JSON 快照，再按记录的消息类型读回），所以两边逐条相同当且仅当记录器会为两边写下同样的内容。
2. **重建上下文（Option A）**：上下文从**源 trace** 重建，不从恢复出的观测重建，因为恢复出的页面里元素 index 是新的 backendNodeId、tab id 是新的 CDP targetId、截图是重拍的，从它们重建的 prompt 不可能与源 run 一致。对每个 k < t：读回录下的观测 obs_k（env 与 loop 来源的 fragment 都读，经 `from_trace` 还原成类型，组装成本次协商的 `NegotiatedObservation`），调 `build_react_prompt`，写进 memory；再按开始顺序重放第 k 步的**每一次**录下的 LLM 调用：取出 live 时交给解析器的那个回复（文本调用是录下的文本；结构化调用用 agent 的 `get_react_output_model()` 把录下的 JSON 还原成实例；被 schema 拒绝的调用是 `failure.raw_output`），经 `parse_react_output` 解析：`ParseError` 走 `_remember_rejected`，成功走 `_cap_actions` + `_remember_reply`。重放自己校验自己：每次调用之前，memory 的上下文必须与这次录下的调用的 `messages` 逐条相同；第 k 步最后一次调用之后，解析出的动作（按 `guarded_act` 的那条规则命名，即 `core/action_guard.py` 的 `resolve_action_name`，用本 run 守卫的动作名集合与绑定表：守卫解析得了的补全前缀，它拒绝的保持 agent 写的名字，源记录里留下的正是守卫给的名字；本 run 的守卫能代表源 run 的，因为 weave 已证明两边协商相同、种子检查已证明带着动作表的种子 prompt 相同）、被丢弃的个数、写进 memory 的回复文本必须等于源 StepRecord 的 `actions`、`truncated_actions`、`llm_output`，解析失败的步必须在源 run 里也是解析失败的步。任何一条不符都是 `IncompatibleCheckpointError`（这个 agent 复现不了这个检查点），发生在恢复环境之前。重放经过 agent 的 slot，所以 Skyvern 的 `_action_history`、browser_use / UI-TARS 的坐标网格这类 agent 自己的状态一并重建；连续解析失败的步数也由重放得出。
3. **恢复环境**：`async for restored in self.restorer.restore(RestorationContext(...))`：每个 k < t 交回一条 `RestoredStep(step=k, check, observation)`，Loop 把它记成 `origin="restored"` 的 StepRecord：内容是源 run 第 k 步记录的副本（动作、结果、回复、观测摘要照抄，所以策略步看到的 `loop.history` / `loop.action_result` 与源 run 第 t 步看到的相同），`restoration` 是这一步的校验结果，`token_usage` 置空，`duration_ms` / `llm_latency_ms` 由 TraceWriter 照常盖章，`timestamp` 是现在；交给 `record_step` 的是**恢复出的** live 观测（只有 env 来源的 fragment）。最后一条 `step == t`：经校验的 live obs_t 与它的校验结果。
4. **策略步**：先把配置的 `branch_instruction` 写进 memory（`none` 什么都不写，见下文"teacher 采样"），然后 `_run_step(run, t, observation=obs_t, restoration=<obs_t 的校验结果>)`，即一个 ReAct 步。`loop.step_info` 的 `max_steps` 与 `today` 取自源 run 在第 t 步录下的 `loop.step_info`（设了 `today_override` 时 today 用它），所以 agent 看到的"第几步 / 共几步"与源 run 相同。
5. **继续步**：再走至多 `continue_steps` 个 ReAct 步（`continue_steps: 0` 就是单步分叉，单步由配置值决定，没有写死在逻辑里）。策略步总数不超过 `max_steps`；预算用尽是 `agent_done=max_steps`。
6. **终止观测**（默认开）。

**失败语义**：重放与恢复里 agent 与环境的方法都是织过的，guard 照样能否决：否决发生在重放或恢复时，记一条 `origin="restored"`、`error_phase` 为 `"replay"` / `"restore"` 的记录，然后上抛。恢复器在 `verify: strict` 下发现发散，抛 `RestorationDivergedError(check, observation)`：Loop 记一条 `origin="restored"`、`error_phase="restore"`、`restoration=<发散的那次校验>` 的记录，以 `agent_done=error`、`env_status=failed`、`error` 点名 `RestorationDivergedError` 结束（不做终止观测）；恢复时环境的普通异常同样以 `error_phase="restore"` 结束。`verify: warn` 只把失败写进那一步的 `restoration`，照常继续。

**隔离**：N 个分叉就是 N 个任务，每个任务新组一套 stack、新起一个浏览器（pinned 档位每 run 新建 profile），各自恢复，第 t_1 个分叉做过的事影响不到第 t_2 个。这是 Zero Global State 的直接结果。

**teacher 采样（用户 2026-10-13 的裁决）**：同一个检查点的 `branches_per_step` 个分叉输入完全相同（同一个源、同一个检查点、同一份任务文本与环境 context；分支序号只进 trace 的 `TraceConfig.branch`，Loop 与模型都看不到），所以它们之间的差别只来自 teacher 的采样。发布的分叉配置因此把 teacher 的温度提到 0.8（录制用的是 openai 的默认 0.2），每步分 3 个（`configs/flywheel/branch_ui_tars_archive.yaml`）；原路径回放那份不变（录制时的模型、默认温度）。温度这类数字原样记进 `config.json`（§5.6）。

- **`branch_instruction`**（闭集，默认 `none`）：第 t 步给 teacher 的一条固定指令。闭集值原样记进 `config.json`（§5.6），指令的原文出现在第 t 步录下的那次模型调用里（TraceWriter 记的就是交给模型的上下文）。`prefer_different_action` 引用源 run 在第 t 步的回复（`StepRecord.llm_output`：agent 自己的写法、自己的坐标网格），请 teacher 在另一个动作同样推进任务时选那个动作。不引用录下的 `Action`：它的参数是 capability 的单位（UI-TARS 把 0–1000 网格上的点换成 CSS px），模型从不写那种单位。文本还写明录下回复里的元素编号属于源 run 的页面（DOM 类 agent 的编号在恢复出的页面上是新的），而且不说"同一个页面"：`verify: warn` 下恢复出的页面可以不同。选项与文本一一对应：`branch.py::BRANCH_INSTRUCTIONS` 是 `none` 之外每个选项的文本，测试把它与字段的 `Literal` 绑在一起。
- **何时、何处**：前缀重建并逐条校验、环境恢复之后，写进 memory 一次（`BranchLoop._add_branch_instruction`：一条以 `[SYSTEM]` 开头的 user 消息，与截断提示同一种写法），位置在策略步的观测之前，因为文本说的是"接下来给出的页面"。继续步把它当历史看见，不再写第二次。第 t 步的上下文不与任何东西比较，所以它不碰种子与重放的校验，也不需要放松它们。源 run 在第 t 步没有录下动作（回复没解析成，或那次模型调用失败）时没有可引用的动作：不写，记一条 INFO。
- **隐私**：引用的是源 run 的模型自己写的回复；重建的前缀本来就原样带着更早的每一条回复，所以它不把环境观测或别的新来源放进提示词（§5.6 两道门的第一道）。
- **不做的**：不规定 teacher 必须与源不同（原路径回放段要用录制时的模型量回放确定性，§4.16）。每个分支一份不同的提示要把分支序号作为 Loop 输入传进来，等 live 数据显示重复的分叉多（度量报告的"重复分叉率"，§4.16）再做。自由文本的指令旋钮不做：它在 `config.json` 里只会是 `"<omitted>"`。
- **残余，写明**：teacher 在指令下写的 thought 可能提到这条指令或源 run 的回复，`thought_source: teacher` 的导出原样带上它（导出冻结，学生侧怎么用随下一期再定，§4.15）。高温下有的模型会写错坐标网格（运行手册"分叉"一节）。

**恢复器**（`RestorerRegistrar`，`restorer_id`，`core/loops/restore/`）：

```python
class Restorer(Protocol):
    restorer_id: str
    def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]: ...   # 异步生成器：Loop 拿到第 t 步（或交出恢复步失败）时要能关掉它

@dataclass(frozen=True)
class RestorationContext:            # 窄的：没有 LLM、没有 memory、没有 recorder
    env: Any                         # 织过的 environment：observe / retarget / verify_restoration
    act_fn: Callable[[Action], Awaitable[ActionResult]]   # guarded_act
    negotiated_obs: Any
    source: SourceRun                # 源 trace 的类型化读取：record(k)、observation(k)、calls(k)、step_info(k)
    checkpoint_step: int             # t
```

恢复器只恢复**环境**，从不碰 memory，所以以后的 `capability_checkpoint`（Docker 的 snapshot/restore、Android 模拟器快照，经新增的 `RestorableCapability` 协议接入）是同一个接口下的另一个策略。逐条交回而不是最后一次性返回，是为了每恢复一步就提交一步：恢复到一半超时或崩溃，trace 里也已经有前面的步。

`prefix_replay`（v1）对每个 k < t：`env.observe(negotiated_obs)` → `env.verify_restoration(recorded_obs_k, live_obs_k)`（`strict` 下这一步已经不通过就不再执行动作）→ 对第 k 步录下的、源 run 执行过的每个动作（名字是 `guarded_act` 留下的：全限定，被它拒绝的是 agent 写的原名）：`env.retarget(action, recorded_obs_k, live_obs_k)` → `act_fn(重定位后的动作)` → 比较动作成败与录下的结果（`strict` 下一个动作的成败与录下的不同、或执行抛错，就是发散：这一步余下的动作不再执行）；最后再 observe 一次并校验 obs_t。旋钮：`verify: strict | warn`（默认 `strict`）、`screenshot_max_distance`（截图感知哈希的汉明距离上限，默认 5，64 位 dHash）、`pace: none | recorded_llm_latency`（后者在执行第 k 步动作之前等待第 k 步录下的模型等待时间，让异步加载有与源 run 相同的时间落定；默认 `none`）。每一步的观测次数与源 run 相同（一步一个 cycle，§4.4）。校验结果的原因按固定顺序判定，见 §5.3 `RestorationCheck`；重定位失败在 `warn` 下照录下的动作原样执行，在 `strict` 下就是发散。

### 4.2 AgentRuntime（通用编排）

**设计决策**：从原 `BaseReactAgent.run()` 提取与 loop 范式无关的逻辑（约 90%）为独立 AgentRuntime。AgentRuntime 负责协商、AOP 织入、Loop weave、超时、错误处理，Loop 只负责编排。**API 是两段式**：`prepare()` 装配并返回 wrapped agent；`execute(task)` 加 lifecycle/timeout 包装 `agent.run(task)`。所有 deps 来自 `ComposedRun`（见 §4.2.1）。

```python
class AgentRuntime:
    """与 loop 范式无关的通用编排。每次 prepare/execute 是独立 run 上下文。"""

    def __init__(self, run: ComposedRun) -> None:
        self._run = run
        self._hooks: list[RuntimeHook] = list(run.hooks)

    @classmethod
    def from_composed(cls, run: ComposedRun) -> "AgentRuntime":
        return cls(run)

    # ── prepare(): 装配阶段 ─────────────────────────────────────────
    def prepare(self, *, task_id="unknown", action_filter=None,
                loop_inputs=None,                  # 没有 **kwargs：Loop 的每任务输入走有类型的 loop_inputs
                env_context=None) -> Any:          # 本任务的环境 setup context（TaskContext.env），给 3d 的检查
        run = self._run
        agent, loop, env, llm, memory = (
            run.agent, run.loop, run.env, run.llm, run.memory
        )

        # 1. Slot 校验：loop.__required_slots__ ⊆ agent 的 callable 方法
        loop._validate_slots(agent)
        # 1b. Loop 输入校验（M31，§4.1）：多的 / 缺的 / 类型不对 → IncompatibleLoopInputError
        loop.check_loop_inputs(loop_inputs or {})
        # 1c. Loop 要环境而这个 run 没有（§4.1 "Loop 的两条类级声明"）→ MissingEnvironmentError；
        #     不要环境的 Loop：缺席的环境什么都不提供，下面的协商照常进行
        if env is None and type(loop).__requires_env__:
            raise MissingEnvironmentError(loop.loop_id)

        # 2. Observation 三方协商（见 §4.4）：universe = 环境提供的 + 本 Loop 的 __loop_fragments__
        negotiated_obs = negotiate_observation(agent, loop, env)

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

        # 3d. setup 会拒绝的，现在就拒绝（§4.3 "setup 之前的检查"）：任务的 setup context（相对的
        #     replay_archive 先解析成绝对路径）、观测协商结果、将要绑定的归档器；只校验，不获取任何资源
        context = resolve_env_context_paths(env_context)
        if env is not None:
            env.check_setup(context, negotiation=negotiated_obs, archivers=archivers)
        elif context:
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
        active_descriptors, act_fn = build_guarded_act(
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
        # 相对的 replay_archive 按启动目录解析成绝对路径（§4.10）：prepare() 查的、trace 记的、setup 打开的是同一个目录
        env_context = resolve_env_context_paths(env_context)

        prepared_agent = self.prepare(task_id=task_id, action_filter=action_filter,
                                      loop_inputs=loop_inputs, env_context=env_context)
        memory.clear(); self._recorder.clear()                 # 一个 ComposedRun 可以跑多个任务
        memory.add_messages(self.seed_messages(task))          # M32：种子消息是公开方法，见下

        # 在 env setup 之前打开 trace：setup 失败也要落进 trace
        await self._dispatch_lifecycle("on_run_start",
            agent=self._run.agent, env=self._run.env,          # env：原始环境，不是 AOP 代理
            task=task, config=config or {}, ...,
            archive_negotiation=self._negotiated_archive,      # 没有归档消费者时为 None
            env_evidence_privacy=self._env_evidence_privacy,   # 有序、去重的隐私标记；没绑定归档器时为 None
            task_setup=env_context,                            # 交给 setup_environment 的那份；没有时为 None
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
                result = ...   # agent_done=ERROR；RecordingFailedError 时 error = "recording failed at step t: …"
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

**种子消息是 runtime 的公开方法（M32）。** `seed_messages(task) -> list[BaseMessage]`：在 `prepare()` 之后调用，调 prepared agent 的可选 slot `prepare(task=, action_descriptors=<本次收窄、守卫过的 descriptors>, **<Loop 的执行上限>)`（`_loop_policy_for` 只把 agent 签名里有的参数交过去），agent 没有 `prepare` 时返回 `[]`；不写 memory、不碰环境。`execute()` 清空 memory 后把它的结果写进去，行为不变，只是从内联代码抽成了方法。抽出来是因为有第二个调用方：训练导出的学生渲染（§4.15）需要与 live run **完全同一套**的装配（同样的协商、同样带 `fragments=` 收窄的 descriptors、同样的绑定表、同样的种子），而它只调 `prepare()` 与 `seed_messages()`，从不调 `execute()`、`setup_environment()`，也不调 LLM。`prepare()` 本来就不 setup 环境、不碰 LLM（`spectragent-run --dry-run` 靠的也是这一点）；导出侧的哨兵测试（LLM 与 capability 的 setup 一调就抛）把它钉成契约。学生的装配不带任务上下文（`env_context=None`），setup 之前的检查照样运行：学生的运行配置（"把 `llm` 指向训练好的模型，跑同一个文件来评测"）必须是跑得起来的配置。

**setup 会拒绝的，`prepare()` 就拒绝（用户 2026-10-13 的裁决：两个开关的组合由 `prepare()` 校验，sweep 开跑前先过一次 `prepare()`）。** 能否运行由配置与任务数据决定的问题，都不留到 setup：

- 第 3d 步（上面）把本任务的环境 setup context、观测协商结果与将要绑定的归档器交给 `Environment.check_setup`（§4.3 "setup 之前的检查"）：谁都不接受的 setup 键在这里报 `ValueError`；实现了 `SetupCheck` 的 capability 检查自己的那一份（browser：§4.14 "协作检查一览"的全部行，含回放与源 run 的一致性检查，读的只是源 run 的环境指纹；profile 的 watchdog 解析不了的已协商 fragment）。只校验：不 setup、不绑定、不收窄、不连浏览器；
- 所以这些错误和其余装配错误一样，在 `on_run_start` 打开 trace 之前、任何东西启动之前从 `prepare()`（`execute()` 里也一样，不看 `raise_on_error`）出去，`spectragent-run --dry-run` 也查得出来（`--start-url` / `--replay-archive` 是它的任务上下文）；
- setup 照样再查一遍（capability 的 `setup()` 调同一个 `check_setup`，带着那时已绑定的归档器）：直接调 setup 的调用方、`prepare()` 之后才注入控制客户端的测试，仍在启动任何东西之前被拒绝；
- 需要读盘的只有回放的一致性检查：`load_fingerprint` 只读源 run 的环境指纹（`env/run.browser.json`）并数 `index.jsonl` 文件，从不读索引行或 WARC；setup 才把整个归档加载进来用于服务（§4.14 "一致性检查与回放指纹"）。

**sweep 先过一次 `prepare()`。** `BenchmarkRunner.run`（`core/benchmark_runner.py`）在跑任何任务之前，为第一个选中的任务组装栈并调一次 `prepare(task_id=, loop_inputs=, env_context=)`（`BenchmarkRunner.preflight`）：组装失败、读不到任务上下文、`prepare()` 拒绝，都抛 `PreflightError`（点名任务，原错误作为 `__cause__`、原样写进消息，所以报错仍点名双方），一个任务也不跑、一个 trace 也不打开；`spectragent-bench` 把它印到 stderr，退出码 2（`PREFLIGHT_EXIT_CODE`；0 与 1 是跑过了的 sweep），度量链（§4.16）因此在这一段停下。通过之后第一个任务就跑在 pre-flight 组装的那个栈上，所以每个任务仍然恰好组装一次栈。之后的任务各自 `execute()` 时照样 `prepare()`，只有某个任务自己的数据有问题时才在那个任务上报（记在它的 `TaskOutcome.error`，不打开 trace）。

**sweep 的汇总：三本账、基础设施失败、否决（用户 2026-10-13 的裁决）。** 每个任务的 `TaskOutcome` 带三本账：agent 怎么结束（`result.json` 的 `agent_done`：done / max_steps / error / timeout / vetoed）、环境撑没撑住（`env_status`）、trace 记全没有（run 结束之后读回它的 manifest：`complete` / `incomplete` 加原因 / `writing`，读不回来是 `unreadable`）。`format_table` 先印这三本账的计数，再逐个任务印一行。**基础设施失败**（`infrastructure_error`，计入 `failed`，退出码 1）都看得见：环境坏了（`env_status=failed`）；模型在 provider 自己的重试之后仍不可用（run 以 `error` 结束在一次模型调用上：最后一个策略步 `error_phase == "llm"`，它失败的那次调用记录不是截断——截断是本 run 自己的 token 上限，不算不可用；读的是 trace 里记下的调用，不解析 `error` 文本）；trace 不完整、没关闭或读不回来。agent 在健康的环境里用完了步数、并且全部记下了，是一次完整的尝试（`ok`）。**否决**不是基础设施失败：被否决的任务照样是一个 outcome（`vetoed`，结果从它的 `result.json` 读回），表里有它那一行和一行 `vetoed: ...`，sweep 在它之后停下，`spectragent-bench` 退出码 3（`VETO_EXIT_CODE`），度量链因此在这一段停下（§4.16），被否决那一段的 run 不会被当作正常输入交给下一段。

**归档：先校验，最后绑定。** 归档轴（§4.5）的协商与绑定分在 `prepare()` 的两头：

- `negotiate_archive` 紧跟 `negotiate_memory`，在任何织入之前运行，所以绑定错误（`IncompatibleArchiveError`）和另外几条协商一样在装配期暴露。`spectragent-run --dry-run` 也调 `prepare()`，干跑就能发现；
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

**AOP 织入流程**：Weaver 读取所有 hook 的 `__intercepts__` 声明（如 `Intercept("ainvoke", role="llm", advice="after")`），为 agent/env/llm 和 StepRecorder 生成 `AOPProxy` 包装。Loop 调用 `llm.ainvoke()` 时，代理触发 TraceWriter 的 `around` handler（调用前登记、结束时记下结局）与 MetricsCollector；Loop 调用 `recorder.record_step()` 时，代理触发 TraceWriter 一次提交完整 step。Loop 代码零 hook 感知。

**Lifecycle vs AOP 两层 hook 接口**：
- **Lifecycle 回调**（`on_run_start/end/error`）：由 AgentRuntime 在结构边界直接调用
- **方法拦截**（`__intercepts__` + `before_/after_/around_`）：由 AOP Weaver 透明织入

### 4.2.1 ConfigComposer（IoC / AOP 胶水层）

**设计决策**：conscribe 提供"YAML → typed Pydantic config"的 schema 层；`AgentRuntime` 消费 wired 实例图。两者之间需要一层 IoC 胶水，把 conscribe `build_composed_config(inline_wiring=True)` 输出的 discriminated-union model 实例化成"已连接好"的对象图——这层就是 `ConfigComposer`。它是文档此前没有显式描述、但代码里必备的层（参见 §3 架构图）。

```python
@dataclass(frozen=True)
class ComposedRun:
    """Fully assembled, setattr-injected object graph ready for AgentRuntime."""
    by_layer: dict[str, dict[str, Any]]    # {layer: {registry_key: instance}}
    top_level_cfg: BaseModel | None = None

    @property
    def agent(self) -> Any: ...     # single("agent")
    @property
    def loop(self) -> Any: ...      # 优先 agent.agent_loop，回退到 single("agent_loop")
    @property
    def env(self) -> Any | None: ...
    @property
    def llm(self) -> Any: ...
    @property
    def memory(self) -> Any: ...
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
        self._refuse_inline_only_sections(cfg)                 #    只能内联的层不许有顶层段（下面）

        by_layer = {layer: {} for layer in self._registrars}
        for layer in self._composed.dependency_order:           # 4) leaves-first 实例化
            for entry in (getattr(cfg, layer, None) or []):
                self._instantiate(entry, by_layer, ...)
        return ComposedRun(by_layer=by_layer, top_level_cfg=cfg)
```

**Wired receptor 的判定与注入**（`_resolve_wired`）：
- 若类的 `__wiring__` 有该 field 的声明，且 field 同时出现在 `init_params` 或 `class_hints`（`get_type_hints(cls)`）→ 视为 receptor
- `dict[str, X]` 注解 → multi-instance receptor，注入 `{registry_key: instance, ...}`
- `list[X]` 注解 → multi-instance receptor，注入 `[instance, ...]`
- 单实例注解 → single receptor，注入唯一 instance
- list / dict / single 的判定读的是**类级注解**（`get_type_hints(cls)`），不是 `__init__` 签名：只在 `__init__` 参数里出现、类上没有注解的受体会走单实例分支。所以 list 受体必须在 MRO 里有一条类级注解（`archive: list[ArchiverBase]`；类体要把这个名字另作他用时放在 mixin 上，见 `_BrowserReceptors` / `AgentReceptors`）
- 不在 init_params 也不在 class_hints → 纯协商声明（没有 slot，如 agent / loop 的 `observation:`、`action:`，capability 的 `observation:` 与 `archiver:` provides 声明）：conscribe 把它生成成可选字段，Pydantic 照样校验它的 `Literal[...]`；**但 YAML 一旦填了值，Composer 抛 `UnresolvedWiringError`**（"negotiation-only entry"）：没有任何地方接收这个值，这条报错就是为了不让它被静默丢掉
- 可选的 list / dict 受体在 YAML 里**省略**（或写 `null`）是"没设"，不等于空：Composer 在所有实例建完之后的第二遍（`_resolve_deferred`，deferred resolution）从所连注册表那一层的**全部**实例里取（wiring 条目列了 key 时只取被允许的那些）；这一层一个可取的实例也没有时，受体保持主人自己的默认值（`capability.actions` 省略 = wiring 提供的全部动作），只有主人自己不给默认值时（`environment.capabilities`）才设成空容器。`[]` 永远是空：`capability.actions: []` 一个动作都不建（用户 2026-10-13 的裁决）。`environment.capabilities` 与 `capability.actions` 的池子采纳不变（`tests/test_core/test_composer/test_inline_only.py` 钉住这条规则）
- **只能内联的受体**（`__inline_only__`，主人类上的声明）：主人把一个 list / dict 受体列进 `__inline_only__`，它就只从自己的配置块取值：省略或 `null` 是主人的默认值，从不从池子里取（池子里还有别的块内联建出来的实例，也不取）；它所连的那一层的顶层段在组装时被拒绝（`InlineOnlyLayerError`），报错点名实例该写在哪里（由 wiring 算出，形如 `trace_writer.archive`）。Composer 构造时检查声明：只能点名本类的 list / dict 受体；同一层不能一边只能内联、一边被别的受体从池子里取。唯一的用例是 `TraceWriter.archive`：**归档只有一个开关**（用户 2026-10-13 的裁决），省略、`null`、`[]` 都是不归档，顶层 `archiver:` 段报错并提示写进 `trace_writer.archive`（`tests/test_core/test_composer/test_archive_yaml_surface.py`）。conscribe 给每个层都生成顶层段、而且没有选项去掉，所以这一段由 af 的组装器拒绝；"只能内联的层"已写进给 conscribe 的反馈，上游支持后这段检查改成生成配置时的声明
- **配置面的拼写规则**（用户 2026-10-13 的裁决；注册名、选项值与旋钮都遵守）：
  - "不启用"拼作 `none`（`replayability: none`、导出的 `occlusion: none`、`shift_dates: none`、`thought_source: none`）；默认值本身做一件正常事的保留描述性名字（`live`、`instant`、`identity`）；`isolation: none_acknowledged` 是书面承认会漏流量，保留。注册名与选项值都不许是 YAML 1.1 读成布尔或 null 的词（on / off / yes / no / true / false / null / ~，任何大小写）：所有入口都用 PyYAML 的 `safe_load`，它按 YAML 1.1 读，裸写的这些词到校验时已经是 `False` 或 `None`。`tests/test_core/test_composer/test_yaml_safe_values.py` 扫描每个注册表的全部键，以及运行配置、链规格、站点规则三份 schema 里的全部选项值
  - `null` 只表示"这个可选项没设"（可选的子块与覆盖值：`require_eval`、`user_agent`、`length`……），默认为 null 的旋钮都在说明里写明没设时是什么（`None (the default): ...`）。数值上限不用 null：不限写 `unlimited`，默认值写出真实的值（`dom_max_elements`、`dom_max_serialized_chars` 默认 `unlimited`，导出的 `max_document_misses` 默认 `0`）；共用的拼写与读法在 `schemas/_types.py`（`UNLIMITED`、`Unlimited`、`limit_value()`，代码内部仍用 `None` 表示不限）。类型写成 `Annotated[int, Field(ge=1)] | Unlimited`：界写在 `int` 那一支上（写在外层 pydantic 会拿它去比 `"unlimited"`）。conscribe 1.3 生成 `_composed.py` 时会丢掉这个嵌套的界，但 Composer 校验用的是活的模型，界照样生效。"按 provider 自己的策略"这类默认写成显式的词：`reasoning_effort: auto`
  - `[]` 永远是空（上面省略受体的那一条规则）。真实的列表默认值要等 conscribe 支持后才能直接显示在生成的配置里（反馈报告）；在那之前，这类字段（`sources`、`require_archive`）在说明里写明默认值，带注释的示例 YAML 也解释它

**注入方式**：init param → 进 `__init__` 的 kwargs；class_hints-only → 通过 `setattr(instance, name, value)` 注入（conscribe `WiredField` descriptor 接受这次写入并把值放到 instance dict，遮蔽自身）。

**与 AgentRuntime 的边界**：`AgentRuntime` 只接收 `ComposedRun`，**不**直接看 conscribe schema、**不**自己解析 YAML。`ConfigComposer` 是唯一懂 conscribe 内部约定（receptor 检测、dict/list 形态、wiring 解析）的层。增删 wiring grammar、调整 discover 路径、加新 layer 都只动 Composer。

**只有一个根，每一层都是可选的（M32 实测）。** 生成的 `ComposedConfig` 每个收集的层都是 `list[<layer union>] = []`，没有必需层：只写 `evaluator:` 的 YAML 能组装；只写一个顶层层、且该层带单实例受体（`task_loader: [{benchmark_name: trace_branches, step_selector: {...}}]`）也能组装，受体照样是类型化的嵌套联合，拼错的键被拒（`tmp/flywheel/m32_design/probe_root.py`）。所以训练导出不另立根：`exporter` 与 `action_translator` 是这个根的两个可选顶层层（与 `evaluator` 同一先例，§4.15），一份只有 `exporter:` 的 YAML 照样由 `ConfigComposer(registrars=_collect_registrars())` 组装；学生的运行配置是另一份普通的运行 YAML，由导出器再组装一次。

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
        ...  # 全部跑完才裁决：异常，或批次里有 capability_id 不是自己的文件，都算该 capability 失败
             # 有失败 → ArchiveCollectionError（点名每个失败的 capability），否则按 capability 顺序合并
        return ArchiveBatch.merge(batches)
```

**归档的路由与收集**（`core/environments/_registrar.py`，错误类在 `core/environments/errors.py`）：

- `provided_archivers`：实现了 `ArchivingCapability` 的 capability 各自 `provided_archivers` 的并集；其余 capability 什么也不贡献。
- `bind_archivers(archivers)`：按每个键的第一段经 `_by_capability_id()` 找 capability（与 `act()` 同一套路由）。下面任何一种情况都记一条问题，全部收集完一次抛 `UnroutableArchiverError(problems, capability_ids)`（`problems`：键 → 原因，按键排序写进消息；`capability_ids`：本环境的 capability id），抛错时没有任何 capability 被调用：前缀对应不到 capability；对应的 capability 不实现 `ArchivingCapability`；键不在该 capability 的 `provided_archivers` 里。校验通过后，**每个** `ArchivingCapability` 都收到自己的那一组，分不到的收到空列表，这会清掉它之前的绑定；收到非空组的 capability 按 capability 顺序记进 `_archiving`。绑定不获取任何资源（它可能发生在干跑里、setup 之前）。
- `collect_archive()`：什么都没绑定时直接返回 `ArchiveBatch()`，一个 capability 也不碰。否则每个绑定了的 capability 一个任务**并发**收集；每个任务都跑完才裁决结果，不留孤儿任务。一个 capability 抛 `Exception`、交回的不是 `ArchiveBatch`，或交回的文件里有 `capability_id` 不是它自己的（后两条记成点名该 capability 的 `ArchiveContractError`，最后一条还点名那些 `archiver_id`），都算它失败；有任何失败就抛 `ArchiveCollectionError(env_type, failures)`：`failures` 是 capability id → 异常，消息按 capability 顺序点名每一个，`__cause__` 是按 capability 顺序的第一个失败；健康 capability 交回的批次随之丢弃（run 反正要失败，§4.6）。没有失败时按 capability 顺序把各批次合并成一个 `ArchiveBatch`。`CancelledError` 原样传播，包括某个 capability 自己抛出它的情形：`gather` 这时会让兄弟任务接着跑，所以先取消还没结束的任务、等它们收场，再原样重抛，同样不留孤儿任务。
- 收集不带任何参数：environment 从不把步号（也不把 cycle）交给 capability。"第 t 步的提交取走的就归第 t 步"是记录器那一侧的规则（§4.6）。

**检查点恢复的路由（M31）**（`core/environments/_registrar.py`）：恢复器（§4.1 `BranchLoop`）经 environment 调用两个只读的方法，capability 侧的协议是 §4.9 的 `RestorationSupport`：

- `retarget(action, recorded, restored) -> Retarget`：与 `act()` 同一套前缀路由（`action.name` 的第一段经 `_by_capability_id()` 找 capability）。路由到的 capability 实现了 `RestorationSupport` 就交给它；没实现、前缀不带点、或前缀找不到 capability 时返回恒等的 `Retarget(action=<原动作的副本>, method="identity")`。重放要复现源 run 的执行，连同它被 `guarded_act` 拒绝的那些动作：原样交给 `act_fn`，由它再拒绝一次，成败比较照样成立。`retarget` 从不执行动作，也不改环境。
- `verify_restoration(recorded, restored) -> list[RestorationEvidence]`：按 capability 顺序扇出到每个实现了 `RestorationSupport` 的 capability，各交回一份证据（带自己的 `capability_id`）；没有任何实现者时是空列表，即"没有可校验的"。`recorded` 与 `restored` 都是本次协商的整个 `NegotiatedObservation`，capability 按自己 fragment 的本地名读它要的那几个（协商保证本地名不撞）。
- 两者都只量、不判：阈值与 `strict | warn` 是恢复器的策略（§4.1），capability 只报告测到了什么，没测的字段为 `None`。environment 不缓存、不记录任何东西。

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
class BrowserUseAgent(AgentBase):
    __wiring__ = {
        "agent_loop": ("agent_loop", ["react"]),
        # 类声明是**跨模式的超集**：所有模式都要的进 required，
        # 因模式而异的进 optional，由 observation_mode 提升或排除（见下）。
        "observation": ("observation",
                        ["screenshot", "task"],
                        ["dom", "browser_meta", "step_info", "history"]),
        "action": ("action",
                   ["navigate", "done"],
                   ["click", "type", "click_coordinate", "type_coordinate",
                    "drag", "scroll", "send_keys", "extract_content",
                    "go_back", "go_forward", "switch_tab", "wait"]),
    }

class GAIAAgent(AgentBase):
    __wiring__ = {
        "agent_loop": ("agent_loop", ["react", "plan_execute"]),
        "observation": ("observation", ["dom", "screenshot", "tool_result"], []),
    }
```

注意两处：(1) 没有 `capability` 条目——写它只会把生成的 config 收窄到某个 capability，运行时没人读；capability 归属由协商在 action / observation 两个轴上回答。(2) `dom` 在类上是 **optional**，DOM 模式再把它提升为 required；这样"跑 DOM 却配了 vision_only profile"仍然在协商期大声失败，而不是静默降级成视觉模式。

运行前 AgentRuntime 自动验证兼容性：Agent 需要的 fragment / action ⊆ Environment 提供的集合。

### 4.4 Observation Fragment 体系 + 协商

**设计决策**：Observation 不再是万能袋，而是 Fragment 注册表 + `__wiring__` 三方协商 + Resolver 模式。Fragment 是语义不可分的一组字段，独立注册在 conscribe；Env(Capability)/Agent/Loop 通过 `__wiring__` 声明 provides/requires；config 时 Avro 式协商 → 动态生成 Observation model → Capability 的 Resolver 按需计算。

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

边界由框架强制，不靠约定：`demand_of()` 把任何覆盖的返回值再过一遍 `bounded_demand()`。这是整条协商路径上唯一新增的自由度，它不该是靠"每个实现自觉调用"来守的那个。`browser_use` 自己也调一次 `bounded_demand`（幂等），因为两次检查回答的是不同的问题：`_ModeDemand.apply` 怪的是模式表，`bounded_demand` 怪的是实例。

边界之所以是硬的，有两个独立理由：

1. conscribe 把类 `__wiring__` 编译成生成 config 里的 `Literal`。声明空间之外的 key 是任何 YAML 都写不出来的 key，用它跑出来的 run 无法从自己的 config 复现。
2. 类级 required 是 agent 说"没有它我干不了活"。实例把它丢掉，就是把一次响亮的协商失败换成一次静默降级——正是本设计反复删除的失败模式。

违反任一条抛 `DemandOutsideWiringError`。这是 agent 类的编程错误，不是用户的配置错误：它在 `prepare()` 阶段、环境启动之前就炸。

**失败要点名是谁在要。** 实例能收窄之后，"谁 required 了 `dom`"就不再是类的属性了：同一个 `browser_use`，两个模式要它、一个模式排除它。所以 `demand_of` 之外还有一个 `demand_label(owner)`，它只做一件事——给诊断取名字。规则是：`__repr__` 以 registry key 开头，就当作实例对自己的紧凑描述整段采用（`browser_use(observation_mode='dom')`）；否则退回 registry key。这条规则把 dataclass 自动生成的 `__repr__`（开头是**类名**，而且会把 LLM 客户端整个 dump 进错误信息）和 `object` 的 `<... at 0x…>` 一起挡在外面。

标签只走到错误信息里：`_observation_rules` / `_action_rules` 多收两个 `*_source` 形参，它们不进 `resolve_demand`，改不了协商结果。`IncompatibleFragmentError` / `IncompatibleActionError` 于是能写出

```
Missing: ['dom']. Requested by: dom ← browser_use(observation_mode='dom').
Environment provides: ['browser.browser_meta', 'browser.screenshot'].
```

—— `observation_mode` 和 `profile` 指向相反的两处修改，没有这一行，用户只能猜改哪一个。

**第一个使用者：`browser_use.observation_mode`**（`dom` / `dom_vision` / `vision`）。三个模式共用一个 `agent_id`、一棵 prompt 树，只在协商需求和 surface 片段上分叉：

| 模式 | 进入模型上下文 | observation 调整 | action 调整 | 点击动词 |
|---|---|---|---|---|
| `dom` | DOM 文本 | 提升 `dom` | 提升 `click`/`type`；排除 `click_coordinate`/`type_coordinate`/`drag` | `click` |
| `dom_vision` | DOM 文本 + 截图 | 提升 `dom` | 提升 `click`/`type`；排除 `type_coordinate`/`drag` | `click` 与 `click_coordinate`，互相独立 |
| `vision` | 截图 + URL/标题/tab 文本 | 排除 `dom` | 提升 `click_coordinate`/`type_coordinate`；排除 `click`/`type`/`extract_content` | 仅 `click_coordinate` |

模式表（`_MODE_DEMAND`）里的 `promote` / `exclude` 只能点名类 wiring 的 **optional** 键，`_ModeDemand.apply` 当场校验。两种失败不对称，这是为什么校验不能只交给 `bounded_demand`：写错的 `promote` 会把 required 集合撑出声明空间，下游必然抓到；写错的 `exclude` 只是减掉一个本来就不存在的键——拼错的、或者类里 required 因而不许 mode 碰的——得到的两个集合完全合法，只是悄悄地不对。模式表是三个 surface 的唯一定义，它里面的笔误必须是错误，不能是耸肩。

模式是**显式配置**，不从环境推导。"环境没有 dom，那这次大概是视觉模式"这种推导，会让一个打错的 capability profile 把 DOM 实验静默降级成视觉实验——分不清是消融还是笔误的消融不算消融。

`screenshot` 在**所有**模式下都是 required：它不只是模型输入，`online_mind2web` / `lexbench_browser` 是从 trace 里的截图打分的。模式决定**模型看到什么**，不决定**run 记录什么**。

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
| 注册单元 | 每个 fragment 一个类 (`DOMObs` / `ScreenshotObs` / ...) | 每个 action 一个类 (`ClickAction` / `ScrollAction` / ...) | 每个归档器一个类（M29：`browser.network` / `browser.storage` / `browser.dom_raw`；回放日志 `browser.replay_log`，§4.14） |
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

**唯一的不对称**：requires 来自 trace hook **实例**的配置，而且永远全限定。记录什么是每次 run 的选择（用户裁决 D3，2026-10-09），它既不是某个 agent 类的领域知识（§4.4："模式决定模型看到什么，不决定 run 记录什么"），也不是记录器类的固有属性。所以没有类级 `__wiring__` 需求，也就没有 relative key 要绑定：`ArchiveConsumer.archive` 里每个归档器的 `archiver_id` 本身就点名了 provider。

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
    index: Annotated[int, RequiresFragment("browser.dom")]
```

一处声明，两级生效：

- **required** 参数依赖 F ⇒ 整个 action 依赖 F（`browser.click` / `browser.type` / `browser.select_dropdown` 依赖 `browser.dom`）；
- **optional** 参数依赖 F ⇒ F 未激活时**把这个参数从 descriptor 里删掉**（视觉 run 里 `browser.scroll` 还在，它的 `index` 不在）。

删除发生在 descriptor 的 `**context` 通道上：`AgentRuntime.prepare` Step 4 调用 `env.get_action_space(active_actions=..., fragments=negotiated_obs.active_fragments)`，环境把它转发给每个 capability、再转发给每个 `action.descriptor(**context)`——与 §4.9 里 `task_tools` 走的是同一条通道。协商规则不变；不读 `fragments` 的 capability 行为与从前完全一致。

守门是静态扫描：`tests/test_core/test_actions/test_action_params_schema.py` 对每个 agent × 每个模式检查"激活动作推导出的依赖 ⊆ 同一模式 observation 的 required"。运行时一致性检查推迟到第二个会用到 DOM 的 capability 出现时再加。

**YAML 旋钮进 description，不进 `default`**：`clear_by_default` 这类当前值被插进参数的 description（prompt 和 schema 两边都看得到），但 descriptor 的 schema 里**不写 `default`**——默认值归 capability，在 `execute()` 里应用，写一个 schema default 等于声明一个"省略该参数时会得到的值"，而那个值不是它。

作用域要说清楚：这是 **descriptor 那一层**的保证。agent 把 descriptor 编译成 per-run 输出模型时，每个 optional 参数会重新带上 `default: null`——strict dialect 把所有属性变成 required，`null` 于是成了模型"弃权"的唯一写法。这个 null 到不了 `execute()`：agent 在构造 Action 前 `model_dump(exclude_none=True)` 把它抹掉，所以"省略"和"写 null"到达 capability 时完全一样——这才是 `clear: null` 覆盖 `clear_by_default` 被堵住的地方。

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
            RequiresFragment("browser.dom"),
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
agent: browser_use
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
active_descriptors, act_fn = build_guarded_act(
    wrapped_env.act, active_descriptors, action_filter=None,
)
# active_descriptors 用于 prompt 构建，act_fn 是 guarded 的执行闭包
```

`build_guarded_act` 的闭包在每次调用时用 `resolve_action_name` 给 `action.name` 定名：在 active set 里就是它自己；短名经绑定表解析到仍在 active set 里的全限定名，就地改写；其余直接返回 `ActionResult(success=False, error=...)`，名字保持 agent 写的样子。

**结果：prompt 说有什么 action，实际就能用什么 action。配置错误在 `AgentRuntime._negotiate_actions` 阶段就会 raise `IncompatibleActionError`，而不是等到 LLM 第一次输出之后才暴露。**

> **Historical note**：`action_filter` 作为顶层 YAML 键曾经是这里的主 ablation 机制。在对称协商方案下，ablation 的首选路径是 agent wiring 的 `required/optional` 二分 + capability 的 `actions` 列表收窄；`action_filter` 仅作为可选的额外 override 保留，不是主设计。

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

**设计决策**：TraceWriter、MetricsCollector、LiveMonitor 等横切关注点通过 AOP Weaver 透明织入。Loop 不持有任何 hook/emitter 引用，调用 `env.observe()`、`llm.ainvoke()` 或 `recorder.record_step()` 时，代理按已声明的拦截点触发对应 handler。hook 走 conscribe 注册表，通过 YAML 配置启用和参数化。

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

**目标方法抛异常时，`after` 不触发。** 通常这是对的：没返回的调用没什么可记。但有**一个** LLM 错误是可恢复的——结构化回复不符合 schema（`StructuredOutputValidationError`），Loop 会把模型自己的文本喂回去继续跑。这一次调用如果没人记，trace 里就会出现一个调用记录干脆缺失的 step，而且缺的正是研究者最想看的那些 step（模型答错的那些），metrics 的 token 数也会比 result 自己算的少一次调用。所以 `metrics` 加一条 `around_ainvoke`：`try: await proceed() except (StructuredOutputValidationError, ModelOutputTruncatedError): 记下来再 raise`——只补记 provider 计费了的失败回复。成功路径仍然走 `after`（`around` 的最内层就是 before/after 链），两者不会重复记账。

`trace_writer` 走得更远：它**只**用 `around_ainvoke` 记录 LLM 调用，没有 `after_ainvoke`。在 `proceed()` **之前**登记这次调用（messages、`kwargs["output_format"]`、开始时间）；返回记 `completed`，抛 `Exception` 记 `failed`，抛 `BaseException`（`CancelledError` 等）记 `cancelled`；每条路径都原样重抛。理由是取消：`CancelledError` 是 `BaseException`，`after` 永远看不到被取消的调用，在 `except Exception` 里才登记的调用会连 prompt 一起丢，而超时和外部取消正是最需要知道"模型当时被问了什么"的时刻。只用一个 advice 也省掉了在 `after` 与 `around` 之间关联同一次调用：Loop 一旦并发发起调用，这种关联就会出错。记录的字段见 §5.3、§5.4。

这里没有引入第四种 advice。"哪些失败的调用该进 trace"是个值得单独决定的问题，而 `around` 已经能表达这一个确定的答案。

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

代价是校验也变懒了，所以装配期补了两道检查，都在 `AgentRuntime.prepare()` 里：`weaver.raise_for_unclaimed_roles()`（wrap 之后）抓「声明了没人 wrap 的 role」，`weaver.audit_intercepts()`（**`loop.weave()` 之后**，这是答案第一次可知的时刻）抓「role 被 wrap 了但拦截仍然永远不会触发」——本次运行没有该对象（env-less run 里的 `env` 拦截）、目标没有该属性（`env.obsserve` 拼写错误）、advice 与方法形状不匹配。两者都 raise：拦到空处的 hook 与没配的 hook 等价，配置错误应当在装配期暴露，而不是等到第一次 LLM 调用。

`AOPProxy` 的透明度边界也是显式的：CPython 的特殊方法查找走 `type(obj)`，不经过 `__getattr__`，所以代理只对**具名属性**透明。`__bool__` / `__eq__` / `__hash__` 转发给 target（它们过去会静默给出错误答案）；`__enter__` / `__exit__` / `__aenter__` / `__aexit__` / `__iter__` / `__aiter__` 在 target 实现时转发、否则抛 `UnsupportedProxyProtocolError`（`TypeError` 子类，消息里点名 target）。这之外的 dunder 一律不声明——声明 `__call__` 会让 `callable(proxy)` 对任何 target 都返回 True，用一个安静的谎话换掉一次响亮的失败。

**`blocking` 的唯一语义 = 并发**：`blocking=True` 内联 await handler，被拦截的方法要等它做完才返回；`blocking=False` 把 handler 交给后台任务，调用立刻继续。错误隔离是**推论**而非第二重语义——非阻塞 handler 抛错时它所修饰的调用早已返回，没有可传播的对象，因此只能记日志。三种形状不成立，装配期以 `UnsupportedAdviceError` 拒绝：同步目标上的 `blocking=False`（没有可延后的调用）、`around` 上的 `blocking=False`（handler 本身就是那次调用，返回值经由它的 `proceed()`）、handler 与目标的 sync/async 形状不匹配。

推论要写清楚：**`blocking=False` 的 handler 进不了 trace**。它在 WAL commit 点之后才结束，算出来的东西落不到任何产物上。所以 `trace_writer` / `metrics` 必须是 blocking，而 `blocking=False` 只适合下游无人读取的旁路（即 `live_monitor`）。这些后台任务由 `BackgroundTasks` 持有强引用，并在 `AgentRuntime._finish()` 里 env 拆除之前 drain（默认 5s，超时则取消并 WARNING 点名 hook）——「fire and forget」不等于「fire and never run」：裸 `asyncio.create_task()` 丢掉句柄时，事件循环只持弱引用，handler 可能被 GC 掉，也可能因为没人等而根本没执行。

**已知 Hook 实现**：

| hook_id | 状态 | blocking | `__intercepts__` | 职责 |
|---------|------|----------|-------------------|------|
| `trace_writer` | ✅ | True | `around_ainvoke(llm)`, `after_record_step(loop)` | 在 `around` 中按开始顺序登记每次 LLM 调用（completed / failed / cancelled）→ Loop 交出 StepRecord 时一次 `commit_step` 提交当步数据到 WAL；run 结束时未交出的调用经 `commit_trailing` 落盘。`archive` 归档器列表（M28：普通构造参数；M29 起是 YAML 受体）：非空时它是本 run 的归档消费者，在 `after_record_step` 与收尾时拉取原始 env 的 `collect_archive()`，证据进 `env/`（§5.2–§5.4） |
| `metrics` | ✅ | True | `after_ainvoke(llm)`, `around_ainvoke(llm)` | 从 ChatInvokeCompletion 提取 token usage，内存累加 |
| `live_monitor` | **P3 未实现** | False | `after_observe(env)`, `after_ainvoke(llm)` | WebSocket 推送实时状态。设计保留位，目前框架内未注册；按本表的 schema 实现一个 `HookBase` 子类即可纳入。 |

**挂受体的 hook 手写 `__init__`。** conscribe 的元类在类创建时读 `inspect.signature(cls.__init__)` 判断一个 `__wiring__` 条目是不是构造参数，这一步早于 `@dataclass` 合成 `__init__`；dataclass 字段形式的受体因此看起来"没被接收"，conscribe 用 `WiredField` 盖掉字段默认值（§5.4、`af-add-a-receptor` guide §6）。所以挂 `__wiring__` 受体的 hook 不是 `@dataclass`：手写 `__init__`，旋钮写成具名的 `Annotated` 参数，受体另有一条类级注解（list 受体写 `list[...]`，§4.2.1）。不挂受体的 hook（`metrics`）可以继续是 dataclass。`HookBase` 本身是 `@dataclass(eq=False)`：hook 是有状态的对象，按身份比较。否则手写 `__init__`、不再是 dataclass 的子类（`TraceWriter`）会继承一个没有字段的 `__eq__`，任意两个实例都"相等"。需要区分 hook 的代码（归档协商比较消费者）一律用 `is`。

**与 emitter.dispatch() 的权衡**：

AOP 方案让 Loop 代码完全干净 — 没有 `emitter` 参数，没有 `dispatch` 调用，甚至不知道 hook 的存在。代价是只能拦截实际存在的方法调用。对于不映射到方法调用的语义事件（如 PlanExecuteLoop 的「规划完成」），解决方案是将该语义建模为 Agent Protocol 方法（如 `agent.parse_plan()` → `after_parse_plan` hook），AOP 自然覆盖。

#### 记录器从环境拉取证据（先例）

环境证据（§5.1）是生产代码里第一个"hook 通过 Environment Protocol 方法拉数据"的地方。它不是 emitter 旁路，规则是：

1. **只在原始环境上拉。** 记录器从 `on_run_start(env=...)` 拿到的是没有被 AOP 包装的环境，拉取就调它的 `collect_archive()`，从不经过代理，所以没有 hook 会拦截这次拉取，也不会递归。
2. **只在自己的提交点拉。** `after_record_step` 里、manifest 提交之前一次；收尾时（`on_run_end` / `on_run_error`）至多再一次。
3. **从不驱动环境。** 不调 `act` / `observe`，不调任何会改变环境的方法；`collect_archive` 只交出 capability 已经采到的东西。
4. **environment 永远拿不到步号。** "第 t 步的提交取走的东西就归第 t 步"：第 t 步动作引发的网络往来、第 t 步开头观测时的原始 DOM 与存储快照，都在第 t 步的 `record_step` 之后被取走，所以归第 t 步。cycle 也不等于步号，capability 只给每条证据带上自己的 `captured_at` / `cycle` / id。

runtime 通过 `ArchiveConsumer` Protocol（`core/archive/protocols.py`）发现记录器：它只有一个属性 `archive: list[ArchiverBase]`，与 YAML 受体同名（M29）。`archive` 非空的 hook 就是本 run 的归档消费者，v1 至多一个（§4.5）；它必须是 blocking 的，因为 `blocking=False` 的 handler 进不了 trace（见上）：`negotiate_archive` 把不是 blocking 的消费者作为一条问题报出（§4.5 的表），所以它在 `prepare()` 就被拒绝，不会在提交点之后悄悄丢掉证据。

**失败语义：两本账，出洞就停，不改判已经定下的结局（用户 2026-10-13 的裁决）。** `result.json` 记 agent 怎么结束（done / max_steps / error / timeout / vetoed），manifest 记 trace 记全没有（`writing` / `complete` / `incomplete` 加原因与"完整到第几步"，§5.3）；一个超时的 run 可以有完整的 trace，一个 done 的 run 也可以有不完整的。归档是显式要求的，静默不完整的归档会毒化回放和分叉，所以记录失败仍然 fail closed：

- **出洞：某一步记不下来。** 这一步的 `collect_archive` 抛错、交回的批次违反契约（`ArchiveContractError`），或这一步的提交本身失败（I/O）→ 这一步**不提交**，它登记的 LLM 调用在收尾时作为 trailing 调用落盘，trace 记为 `incomplete`、完整到第 t-1 步（`complete_through`，WAL 维护，§5.2），`incomplete_reason` 写 `recording failed at step t: <原错误>`；之后不再收集，也不再带着证据提交任何一步：Loop 若吞掉了那个错误还要继续，下一次提交照样失败，点名先出洞的那一步。run 在这一步停下：`TraceWriter` 抛 `RecordingFailedError`（`core/step_recorder.py`，原错误是 `__cause__`），run 的结局是 `agent_done=error`、`error` 为 `recording failed at step t: <原错误>`（`execute` 照 `raise_on_error` 抛出或返回）——除非这一步的结局已经定了：
- **已经定下的结局不改判。** agent 就在这一步说了 done（这一步的记录里有 `is_done` 的动作结果）：`TraceWriter` 不抛错，只记日志，run 照 agent 的决定以 done 结束、答案保留；抛错只会把 done 改判成失败、清空答案，而 run 本来就在这里结束。trace 照样是 `incomplete`，完整到第 t-1 步。run 正因失败而结束的那一步（否决、基础设施失败、动作失败、最后一次容许的解析错误）由 Loop 保住结局，见下面"记录失败不顶替"那一条。在 `max_steps` 用尽的最后一步出洞不算已定：Loop 要到这一步记下之后才决定以 `max_steps` 结束，所以那是 `recording failed at step t`。
- **尾部失败不抛错。** run 结束之后的尾部——收尾那次收集、收尾要补写的欠账（trailing 提交）、终止观测那一步（§4.1）——失败时，run 的结局不变，trace 记为 `incomplete`（原因写明），不抛错。收尾时 `TraceWriter` 一律不抛错，因为这时 run 的结局已经定了，就在它要写的 `result` 里：收尾的收集失败或被取消、某一样 trailing 提交失败（那一样就此丢掉、不重试，§5.2）、`result.json` 写不进去，都只进 `incomplete_reason` 并记日志；连终态都写不进去（磁盘一直满着）时 manifest 停在 `writing`，记一条 ERROR。唯一上抛的是收尾收集被取消时的那个取消，在终态写完之后（§4.2 "取消永远优先"）。
- 收尾时至多再收集一次，而且只在之前没有出过洞时收集。收尾也只做一次：`on_run_end` 收过尾之后，紧跟着的 `on_run_error`（`AgentRuntime` 在有错误时总会派发它）什么也不重做；只派发 `on_run_error` 的驱动由它按同样的规则收尾，没有 `result`，所以没有 `result.json`、trace 是 `incomplete`。
- **记录失败不顶替 run 已经定下的结局。** 某一步的收集失败（或任何让 `record_step` 抛错的记录失败）恰好落在 run 正因失败而结束的那一步（guard 的否决正在上抛，或 Loop 正要把基础设施失败、动作失败或最后一次容许的解析错误变成终止结果）时，Loop 不让这个记录失败从 Loop 里取代那个结局：否决照样上抛（记录失败作为 note 附在 `RunAbort` 上，`execute` 因此照样越过 `raise_on_error=False` 把它交给 sweep，sweep 照样停下；记录失败若一直持续到收尾（磁盘一直满着），`TraceWriter` 的收尾不抛错，写不进去的只进 trace 的 `incomplete_reason`，别的 hook 收尾失败才是否决上的又一条 note，§4.2 "否决总是上抛"），终止结果的 `error` 仍然点名根因，后面附上"这一步也没能记录"及其原因（`result.json` 于是说清楚 trace 为什么少了这一步）。fail closed 不受影响：这一步照样不提交，它的调用照样作为 trailing 落盘，trace 照样记为 `incomplete`、完整到前一步（磁盘写得进去的话；写不进去的那一样就丢了，不重试，§5.2），之后不再收集。只有在 run 本来要继续的那一步，记录失败才是让 run 失败的那个错误（上面的第一条）。记录被取消切断（这一步的收集太慢，run 自己的超时恰好到期）也顶替不了那个结局：取消照样原样离开 Loop，但带着结局（否决本身，或已建好的终止结果，`loops.errors.carry_outcome`）；是 run 自己的超时时，`execute` 照这个结局收尾，外部取消照旧优先（§4.2 "run 自己的超时顶替不了已经定下的结局"）。被否决的那一步，Loop 在交出它之前先记下否决的日志，所以交接不论怎样结束，日志里都有 guard 说过停。（`ReactLoop._surrender_failing`、`_surrender_vetoed`、`_end_failing`；新 Loop 的义务见 `af-add-a-loop` guide §3。）
- 取消不是采集失败：某一步的记录被取消切断时，这一步不提交（交接之后才被取消的那一批证据随之丢失；已提交的步各自带着自己的证据，不受影响），收尾照常收集一次；这一步的记录本已存在、却没能写下，所以 trace 是 `incomplete`（`recording step t was cut off by a cancellation`），完整到第 t-1 步。取消落在一步还没结束的时候（比如 LLM 调用当中）不是洞：那一步本来就没有记录，它的调用作为 trailing 落盘，trace 可以是 `complete`。交接之前就被取消的证据，按交接契约（§4.9 "交出并忘掉"）仍在 capability 里，由收尾那次收集取走，作为 trailing 证据落盘。
- "降级继续"（不致命的洞照写这一步、标"证据缺失"，run 继续）暂不做，等 live 数据说明它常见时再定：它还要重置网络证据全 run 共用的去重索引（`warc.py`），否则洞之后的重复响应会指向丢掉的那条记录。

### 4.7 LLM 抽象

```
BaseChatModel (Protocol)
    ├── model: str
    ├── model_type: str         # "openai", "anthropic", "google", ...
    ├── provider: str           # "direct", "azure", "aws", ...
    ├── name: str
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

**provider 建的客户端由 provider 关，不留给垃圾回收。**OpenAI 系（`OpenAICompatBase`
及其子类）、Anthropic 系（`AnthropicCompatBase`）与 `ChatGoogle` 的每个请求经 `get_client()`
新建一个 SDK 客户端（连同它的连接池），并在这个请求返回之前关掉它，成功、失败、被取消都一样
（`_request_client()`；`ChatGroq` 的 SDK 不是 openai 的，用它自己的 `async with`）。留给回收器的后果比泄漏更糟：
openai / anthropic / groq / google-genai 的异步客户端在未关闭就被回收时，`__del__` 会在仍在运行的事件
循环上排一个 `aclose()`。这类客户端多半挂在引用环上（它的资源对象引用回它，或持有它的 run 对象图成环），
由循环回收器收；同一轮回收已经把它的 socket 关掉，号码随即被下一个新建的 socket 拿到，迟到的 `aclose()`
关传输时却按旧号码把事件循环选择器里**新 socket 的**注册删掉；删掉的若是下一次模型调用的 connect 回调，
这次调用就永远等不到连接完成，卡到超时为止，超时设多长都一样。调用方注入的 `http_client` 归注入方，
provider 不关它。代价是调用之间不复用连接：OpenAI 系与 Anthropic 系原本就每次调用新建客户端，`ChatGoogle`
原先按实例缓存一个；按 run 复用一个客户端需要 LLM 的生命周期（§14.3）。`ChatOllama`（SDK 没有关闭接口）
与 `ChatMistral`（按实例缓存）也还没关自己的客户端；它们是纯 httpx，没有那种 `__del__`，只泄漏到回收为止，
不会删别人的注册。

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

**归档契约**（`core/archive/` 定义类型，capability 实现行为；`tests/_helpers/archive_contract.py` 对每个提供归档器的 capability 检查能从外面检查的那些条：替换与清空绑定、不带参数、交出并忘掉、未绑定不采、只交已绑定归档器的证据、每批至多一份 run 记录、来源信封、采不到就抛错，以及这些证据经 TraceWriter 提交、读回、崩溃窗口与收尾的行为，和另一个归档 capability 同处一个环境时 run 记录仍按 capability 计数、编号与去重。"不无限阻塞"、"对生产者原子交接"与"在途条目恰好交出一次"取决于 capability 的内部时序，与 capability 无关的 harness 造不出来，由各 capability 自己的测试钉住，browser 的在 M29，用 fake CDP）：

- `bind_archivers(archivers)` **替换**整个绑定（空列表 = 解除），不获取任何资源；runtime 只在 setup 之前绑定，capability 在 `setup()` 里读绑定，决定建不建自己的采集通道。收到的是 trace hook 持有的归档器实例，旋钮就在实例上。
- `collect_archive()` 不带任何参数（没有步号，没有 cycle），交出自上次收集以来采到的全部证据并忘掉它们，批次从此归调用方。不无限阻塞；在没有 setup、或 setup 半途失败的 capability 上调用也安全（没采到东西就返回空批次）；采不到时**抛错**，不返回一个假装完整的空批次。
- 它可能在共享环境里另一个 agent 正在动作时被调用，所以"交出并清空"对 capability 自己的生产者必须是原子的（例如在一条同步语句里把缓冲区换出去）。
- 收集边界上还没完成的条目（还在读的响应体）留到之后的批次，恰好交出一次，不重复也不丢；收尾那次收集是最后一次：capability 要么在有限时间内把它们收完，要么把它们标成未完成交出（怎么标是 capability 自己的事，M29 的 `body_status`）。
- 每个 `ArchiveFile` 自带来源信封 `meta: ArchiveProvenance`：`captured_at`（墙钟 Unix 时间）、适用时的 `cycle`、capability 作用域的 `ids`（session / frame / loader id 等）。文件的 `capability_id`（`archiver_id` 的第一段）必须是这个 capability 自己——environment 把别的前缀当作该 capability 的收集失败；步级文件必须属于一个已绑定的归档器键。
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
class ArchiveBatch:               # 一次收集取走的全部文件
    files: tuple[ArchiveFile, ...] = ()   # 传入 list 时转成 tuple
    @property
    def is_empty(self) -> bool: ...
    @classmethod
    def merge(cls, batches: Iterable["ArchiveBatch"]) -> "ArchiveBatch": ...   # 保持顺序

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

- 类型在 `spectragent/schemas/restoration.py`：`Retarget{action: Action | None, method: "identity" | "geometry_signature" | "tab_order_url", failure: "not_found" | "ambiguous" | None, detail}`（`action is None` 当且仅当 `failure` 不为空）；`RestorationEvidence{capability_id, url_match, recorded_url, restored_url, signature_match, signature_missing, signature_extra, screenshot_distance, document_misses}`，每个量都可以是 `None`（这个 capability 不测它，或这一次测不了，例如本 run 没有激活截图）。
- 为什么需要：browser 的元素 index 就是 Chrome 的 `backendNodeId`（`dom/models.py` 的 `element_handle`），新启动的浏览器里同一个元素的 index 不同，录下的 `click(index)` 不能原样重放；`switch_tab` 的 `target_id` 是每个浏览器新生成的 CDP targetId。坐标动作与导航动作不需要（pinned 回放下布局确定，截图哈希兜底）。
- 不进 `BaseCapability`，`CapabilityBase` 也不给默认实现：默认（恒等、不校验）在 environment 那一侧（§4.3 "检查点恢复的路由"），不实现的 capability 什么都不用写。
- capability 不知道 WAL、不知道步号：它拿到的是两个观测对象，读的是 fragment（browser 读 `browser.dom` 的自包含 `selector_map`、`browser.browser_meta`、`browser.screenshot`），不 import `core.trace`（§10）。唯一不从两个观测里读的是观测装不下的计数（browser 回放服务的主 frame 文档 miss）：capability 记着上一次读到的总数，报自上一次 `verify_restoration` 以来的增量，所以这个调用不是幂等的，恢复器对每个恢复出的观测恰好调用一次、按顺序（`prefix_replay` 正是如此）。Browser 的实现见 §4.14 "恢复：重定位与校验"。

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
- Browser 的实现（`BrowserCapability.check_setup`）：profile 的 watchdog 解析不了的已协商 fragment（profile 当场重新解析），§4.14 "协作检查一览"的全部行（`cdp_url` 取路由来的 context，没有再取旋钮），回放时源 run 与本配置的一致性（只读源 run 的环境指纹）；一次报全，合成一个 `BrowserCooperationError`。`replay_archive` 指向的不是已提交的 run 目录时抛 `ArchiveOpenError`。
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

BrowserCapability.setup(*, negotiation, cdp_url, start_url, replay_archive, login_*/risk_* …)
├── network_source 拦截（archive）时：先经 core/archive 的 open_archive(replay_archive) 打开源 run
│     （ArchiveReader，只读已提交的部分），加载它的指纹与网络索引（§4.14 "回放"）
├── 协作检查 + 回放一致性检查（§4.14）：需要 recorder 连接的配置与 provider / cdp_url / 注入客户端 /
│     provider 旋钮是否相容、隔离、归档指纹与本 run 配置是否一致；不相容就在启动任何浏览器之前
│     抛错，一次报全、点名双方
├── cdp_url 优先级:
│     1. setup 的 cdp_url 参数         (任务上下文按名字路由来的 per-run override)
│     2. self.cdp_url                  (conscribe field)
│     3. self.backend.open()           ← 取 ProviderSession.cdp_url
│        （provider 声明了 LAUNCH_ARGS 时是 open(launch_args=档位的开关 + 需要时的隔离开关)）
├── 持有 self._session = ProviderSession(...)  以便 teardown 释放
├── 控制连接 CDPClient(cdp_url).start()（loopback 端点 proxy=None、compression=None：cdp_client.transport_options）
├── 需要时开 recorder 连接 + browser 级 archive scope + 回放服务（§4.14），在第一个 tab 之前
└── 创建 watchdogs / 初始 tab / start_url / 等等

BrowserCapability.teardown()
├── recorder 显式清理（§4.14：关 auto-attach → 放行扣着的请求 → Fetch.disable → detach → 关连接）
├── close watchdogs / scopes / 控制 CDPClient
└── if self._session is not None: await self.backend.close(self._session)
```

**`__wiring__["backend"]` 字段类型**：Mode 1 字符串（`"browser_provider"`），表示"任意已注册到这个 registry 的 provider"。如果某个 capability 想限制 backend 子集，可改 Mode 2/3 的 tuple 形式（如 `("browser_provider", ["local_chrome", "skyvern"])`）——conscribe 把 backend 字段约束为对应的 discriminated union 子集。

**Backend 文件归属**：所有 browser provider 文件物理上属于 `spectragent/core/capabilities/browser/backends/`，与 `BrowserCapability` 同目录；`BrowserProviderRegistrar` 也在 `backends/_registrar.py`。这让"backend 是 capability 的从属配置"在文件结构上一目了然。其他 capability 的 backend 同理：`spectragent/core/capabilities/docker/backends/`、`spectragent/core/capabilities/desktop_vm/backends/` 等。

**Provider 能力声明（`__capabilities__`）**：每个 provider 类用 `__capabilities__: ClassVar[set[str]]` 静态声明自己支持什么，常量在 `backends/capabilities.py`。这是 provider 对"支持什么"的**静态**声明，与"这次 run 打不打开"无关；后者永远由 capability 上的策略决定（`replayability`、`network_source`），provider 上没有与策略并列的开关。capability 只读这个集合决定能不能把某件事交给它，从不按 `provider_id` 分支、也不探测 provider 的字段；provider 一侧的事实（profile 是否新建、哪些旋钮与开关矛盾）由常量承诺的 Protocol 方法自己报。M29 新增两个、M30 新增一个，v1 只有 `local_chrome` 声明：

| 常量 | 承诺 | 谁要求它 |
|---|---|---|
| `NETWORK_CAPTURE` | 浏览器进程由 capability 经这个 provider 启动并在 teardown 杀掉；CDP 端点接受第二个 browser 级客户端（`waitForDebuggerOnStart` 的 auto-attach、Network、Fetch）；provider 实现 `NetworkCaptureProvider`：`persistent_profile() -> str \| None`（profile 不是每 run 新建时，点名是哪个旋钮——只有配置能让 profile 不是新建的，从不靠环境变量，`config.json` 才看得出来；它决定 §5.6 的 `logged_in` 与指纹的 `fresh_profile`；声明了 `NETWORK_CAPTURE` 却答不出的 provider 被协作检查拒绝，从不当作新建的） | 任一 browser 归档器被绑定；`replayability` 不是 `none`；`network_source: archive`——也就是一切需要 recorder 连接的配置（§4.14） |
| `LAUNCH_ARGS` | `open(*, launch_args: Sequence[str] = ())` 接受本次 session 额外的 Chrome 开关；provider 实现 `LaunchArgsProvider`：`launch_arg_conflicts(launch_args) -> list[str]`（自己的哪些旋钮与这些开关矛盾，每条点名旋钮），以及 `planned_launch_flags(launch_args) -> list[str]`（M30：`open(launch_args=...)` 将会写进 `metadata["launch_flags"]` 的那张开关表，启动之前就能算——回放的一致性检查据此在启动浏览器之前比较开关摘要） | `replayability: pinned`（它的启动开关只能从这条路进浏览器）；`NETWORK_ISOLATION`（隔离开关也走这条路） |
| `NETWORK_ISOLATION` | provider 启动的浏览器能被切断网络：capability 经 `open(launch_args=...)` 交给它 `chrome_switches.NETWORK_ISOLATION_ARGS`（`--host-resolver-rules=MAP * ~NOTFOUND` 与 `--no-proxy-server` 两个 argv 元素，不带 shell 引号；没有任何例外，`localhost` 也断，§4.14 "回放"）；provider 实现 `NetworkIsolatingProvider`：`isolation_conflicts() -> list[str]`（自己哪些旋钮会让隔离失效，每条点名旋钮：`local_chrome` 是 `proxy_server`、`proxy_bypass_list`、`extra_args` 里任何一个网络路由开关（与某个隔离开关一字不差的重复除外：它什么都不改）（`chrome_switches.NETWORK_ROUTING_SWITCHES`：`--host-resolver-rules`、`--proxy-server`、`--no-proxy-server`、`--proxy-pac-url`、`--proxy-auto-detect`、`--proxy-bypass-list`）、不是 `about:blank` 的 `startup_url`——它在任何 recorder 连上之前就从命令行加载）。声明它的 provider 也声明 `LAUNCH_ARGS` | `network_source: archive` 且 `isolation: required`（默认；§4.14 "回放"） |

- `backends/base.py` 的约定本来就允许 `open()` 接收 per-session 的显式关键字参数；`launch_args` 是其中一个。capability 只对声明了 `LAUNCH_ARGS` 的 provider 传它；`none` 档位什么都不传，云 provider 的 `open()` 调用与以前完全一样。
- `local_chrome` 把 `launch_args` 接在 `extra_args` 之后，然后把同一个开关的多次出现合并成一个：所有 `--js-flags` 合成一个（V8 flags 以空格拼接），所有 `--disable-features` / `--enable-features` 各合成一个（逗号拼接、去重）。Chrome 对这几个开关只认最后一次出现，原先 `CHROME_DEFAULT_ARGS` 与 `disable_security` 各带一个 `--disable-features`，后者静默吃掉了前者。合并写在 `backends/chrome_switches.py`（`merge_chrome_switches`），归 Chrome 命令行这一层，不是某个 provider 的私有函数。
- `ProviderSession.metadata` 记录确切的命令行（`command_line`）与去掉易变值的开关表（`launch_flags`：不含可执行文件与启动 URL，`--user-data-dir` 的路径与 `--remote-debugging-port` 的端口换成占位符）。环境指纹只记后者的摘要，而且去掉网络路由开关之后才算（M30：录制时 fixture 的 `MAP` 规则与回放时的隔离开关都不进摘要，隔离单独记，§4.14），命令行本身从不进 trace（`proxy_server` 可能带着凭证）。
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
                                  "browser.replay_log"]),
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
| `start_url` | browser | 任务的落地页，agent 第一次观测之前打开（`TaskSetup.to_context()`；`spectragent-run --start-url`） |
| `login_required`、`login_type`、`credentials_ref`、`risk_control`、`risk_control_types` | browser | `BrowserSetup` 的其余词表；接受、记 WARNING、还不处理 |
| `cdp_url` | browser | 显式 CDP 端点（per-run override；与需要 recorder 连接的配置互斥，§4.14） |
| `replay_archive` | browser（M30） | 回放的**源 run 目录**（一个 trace 目录，含 `manifest.json`）。`network_source` 拦截（`archive`）时必须给、不拦截（`live`）时不许给，都在 `prepare()` 报错（`check_setup`）、点名双方。capability 经 `core/archive` 的 `open_archive` 打开它，只读已提交的部分（§4.14 "回放"）；`spectragent-run --replay-archive DIR`。相对路径可以写，意思是相对启动目录（af 从不切换工作目录，所以就是当前目录；`~` 先展开）：`AgentRuntime.execute` 在交给 `prepare()` 检查、记进 `task_setup`、交给 setup 之前把它解析成绝对路径（`schemas/task.py::resolve_env_context_paths`；`prepare()` 自己也解析一遍，直接调它的调用方一样），对每个调用方都一样：CLI、任务加载器、自己组 `env_context` 的 Python 调用方；按 capability id 挂的那一块（`{"browser": {...}}`）里的也一样。trace 因此记下 setup 打开的那个目录（用户 2026-10-13 的裁决） |
| `task_tools` | tool_sandbox（设计中，M23） | 每个 task 的工具定义（§13.5） |

整个 context 按值级规则脱敏后记进 `TraceConfig.task_setup`（§5.6），所以 `replay_archive` 是路径字符串，不是打开的对象；分叉任务（M31）从源 trace 里重建环境靠的就是这份记录。上面的键都属于 `TaskContext.env`，路由不变（下一段）。

**`TaskContext`：一个任务交给三个不同的消费者的三样东西（M31）**（`spectragent/schemas/task.py`）：

```python
class TaskContext(BaseModel):
    env: dict[str, Any] = {}                # → execute(env_context=...)：上表的键，按 setup() 签名路由
    loop: dict[str, Any] = {}               # → execute(loop_inputs=...)：Loop 声明的输入（__loop_inputs__，§4.1）
    branch: BranchProvenance | None = None  # → execute(branch=...)：分叉来源，只进 trace（TraceConfig.branch，§5.3）
```

- `TaskLoaderBase.get_task_context(id)` 返回 `TaskContext(env=task.setup.to_context())`，任务不存在时返回空的 `TaskContext()`；`BaseTaskLoader` Protocol 的返回类型同步改成 `TaskContext`，不留返回 dict 的旧形状。
- `BenchmarkRunner.run_task` 把三部分分别交给 `AgentRuntime.execute`（§4.2）；开跑之前 `BenchmarkRunner.preflight` 用第一个任务的 loop 与 env 部分调一次 `prepare()`（§4.2 "sweep 先过一次 `prepare()`"）。`spectragent-run` 只组 env 部分（`--start-url`、`--replay-archive`），`--dry-run` 也把它交给 `prepare()`。
- loop 部分的词表：`checkpoint`（`Checkpoint{trace_dir: 源 run 目录的绝对路径, step: t}`）→ `BranchLoop`。交给不声明它的 Loop 在 `prepare()` 报 `IncompatibleLoopInputError`。
- branch 部分：`BranchProvenance{source_run_id, source_trace（源 run 目录相对 sources 根的 POSIX 路径）, source_benchmark, source_subset, source_task_id, checkpoint_step, branch_index, branch_group_id}`（`spectragent/schemas/restoration.py`），按任务变化，不进配置摘要。

**分叉任务：`trace_branches` 加载器**（`core/data_loader/task/trace_branches.py`，`benchmark_name: trace_branches`）把已录制的 run 展开成分叉任务。它读源 trace 只经 `RunTrace`，只读已提交的部分。

- **旋钮**：`data_dir`（sources 的根目录，默认 `traces`）；`sources: list[str]`（相对根的 glob，展开后按路径排序；必须相对根、不含 `..`，构造时检查；一个匹配到的路径不是目录或没有 `manifest.json` 就不是 run，忽略）；`step_selector` 受体（`StepSelectorRegistrar`，`selector_id`：`explicit{steps}` / `every_k{k, offset}` / `all` / `last`，`core/data_loader/task/step_selector/`）；`branches_per_step`（≥ 1）；`agent_outcome`（`any` 默认 / `done` / `max_steps` / `error` / `timeout` / `vetoed`：源 run 的 agent 怎么结束，读 `result.json` 的 `agent_done`，只在设了时读；没有 `result.json` 的源过不了设了值的筛选）；`require_eval`（`RequireEval{evaluator_id, label: 1, slug: null}`，pydantic dataclass，在 `core/data_loader/task/require_eval.py`：至少一份该评测器的 `verdict_source == "graded"` 的判定，其 `predicted_label == label`，给了 `slug` 时只看那一份；经 `RunTrace.evals()` 读，§5.5，不读 `result.json`）；`require_archive`（默认 `["browser.network"]`，`[]` 不要求任何归档：源 run 绑定了这些归档器、且它们的 capability 有已提交的 run 记录即环境指纹）；`on_unbranchable: raise | skip`（默认 `raise`）；`subset` 只是标签。`sources` 与 `require_archive` 在 YAML 里的默认写作 `null`，意思就是上面两个默认值：conscribe 读签名生成配置，签名里写不了非空的列表默认值（列表是共享的可变默认，`tuple[str, ...]` 被生成成 pyright 拒绝的 `tuple[str, Ellipsis]`）。
- **两本账（用户 2026-10-13 的裁决）**：trace 自己那本账是**硬条件**，不是筛选、关不掉：检查点永远是源 trace 已关闭、而且完整到的那一步。`writing` 的 trace（还在写，或进程死在收尾之前）没有可分叉的步（记在 `loader.skipped`，原因 `no branchable step: its trace was never closed ...`）；`incomplete` 的 trace 只有 `complete_through` 之前（含）的步是候选，所以一个在第 t 步出洞停下的 run 仍然贡献 0..t-1 的每个检查点，一个被限流打断的 30 步 run 前 25 个检查点照样能用，尾部失败（收尾收集、终止观测）的 run 一个检查点也不少。agent 那本账是**筛选**：`agent_outcome`，默认 `any`。
- **筛选与前提**：`agent_outcome` / `require_eval` 是筛选，不符的源跳过（记 INFO，也记在 `loader.skipped`，原因以 `filtered:` 开头；筛掉的源从不触发抛错）。前提不满足的源按 `on_unbranchable` 抛错或跳过，原因都记在 `loader.skipped`（源路径 → 原因）：读不回来的 trace（manifest、配置、步记录、`result.json`（`agent_outcome` 设了时）或 `evals/` 坏了；比当前格式旧的 trace 也在此列，`RunTrace` 只读 format 3，§5.5）；源 run 本身是分叉 run（`config.branch` 不为空）或回放 run（`task_setup` 里有 `replay_archive`）；`require_archive` 不满足；环境 setup context 拿不到（见下）；源 run 的 `run_id` 与另一个通过筛选的源重复（共享这个 `run_id` 的每个源都不可分叉，因为它们的任务 id 会撞）。`raise` 一次列出全部不可分叉的源（一个 `BenchmarkDataError`），这次扫描不发布任何任务；`skip` 记 WARNING。
- **环境 setup context**：取源 `TraceConfig.task_setup.context`（M28 起记录）；没有这份记录、或它的脱敏台账不空（值被换成了 `<redacted>`，照它重建的环境不是源环境）时，按源 run 的 `(benchmark, subset, task_id)` 用源 benchmark 的 TaskLoader 重新解析（一次扫描里每个 `(benchmark, subset)` 只建一个加载器）；都不行就是不可分叉。分叉任务的 env 部分 = 这份 context，加上 `replay_archive = 源 run 目录的绝对路径`。
- **选步**：候选是已提交的、`origin == "policy"` 的步 t，要求 0..t 每一步都已提交、trace 完整到第 t 步（`t ≤ complete_through`）、第 t 步的观测已记录（`error_phase != "observe"`）；`step_selector` 在候选里选，候选之外的步（如 `explicit` 写了超出某个源长度的步）跳过并记原因，键是 `<源路径>#s<t>`（没提交、前缀有缺口、超出 trace 完整到的那一步、是恢复或终止步、观测没记录）。选步策略是纯函数：`all` 返回全部候选；`every_k` 按**步号**数（`offset`、`offset + k`……里是候选的那些），不按候选里的位置，所以一个源缺了第 4 步时 `k: 2` 仍然选 0、2、6；`explicit` 原样返回写了的每一步（至少一步，构造时检查，因为生成的配置不带 `min_length`），由加载器判断哪些是候选；`last`（M33）只返回最后一个候选，每个源 run 恰好一步，长短不一的源也一样（`explicit` 写不出"每个源的最后一步"），度量闸门的原路径回放段用它（§4.16）。一个源一个候选都没有时记在 `loader.skipped`，不抛错。
- **展开**：每个选中的 t 展开 `branches_per_step` 个任务，任务 id `<source_run_id>/s<t>/b<i>`，`branch_group_id = <source_run_id>/s<t>`；`TaskDefinition.task_text` 是源 run 的任务文本，`env_type` 是源 run 的，`difficulty` / `domain` / `tags` 取自源 `TraceConfig.extra`（结果照样按它们切片），`setup` 在 env 类型有对应 setup 变体时照 env 部分写一份（只是描述；跑的始终是 `get_task_context` 的 env 部分）；`get_task_context` 给出 env（上面那份）、loop（`checkpoint`）与 branch（`BranchProvenance`），每次给一份副本。
- **惰性加载**：构造时不读盘，第一次 `task_ids()` / `__getitem__` / `get_task_context` 时才扫描源，所以组装配置（每个任务都会重组一次）不做 I/O。一个源都没匹配上时任务列表为空并记 WARNING，点名根目录与 glob。
- **受体写法**：`TaskLoaderBase` 在 `__post_init__` 里加载、而受体在构造之后才注入，所以这个加载器不是 dataclass：手写 `__init__`（接受继承来的 `subset` / `data_dir` 与上面的旋钮），`step_selector` 是类级注解的单实例受体加 Python 侧默认（`all`），与 `ArchiveNetworkSource.miss_policy` 同一写法。
- **v1 没有 `trace_branches` 的评测数据加载器**：`spectragent-bench` 跑分叉 sweep 时不解析评测加载器（没配置评测器、或给了 `--no-eval`，就从不查 `EvalLoaderRegistrar`）；配置了评测器而没有 `eval_loader:` 块时就在开跑前报错，点名这个 benchmark。这条规则对任何没注册评测加载器的 benchmark 都成立，不是 `trace_branches` 的特例（分叉结果怎么变成训练信号，M32 之前定）。

### 4.11 Evaluator 抽象

```python
EvaluatorRegistrar = create_registrar("evaluator", BaseEvaluator, discriminator_field="evaluator_id")

class BaseEvaluator(Protocol):
    @property
    def evaluator_id(self) -> str: ...
    async def evaluate(self, trace: RunTrace, reference: Any, **kwargs) -> EvalResult: ...
```

Evaluator 的输入是**完整的 RunTrace**（不是 AgentResult），因此可以访问任意深度的运行数据。

**现状：休眠，与数据飞轮解耦（用户 2026-10-13 的裁决）。** 评测器不删除，也不改进。飞轮的每一段（录制、回放、分叉、导出、度量）都不需要评测结果：分叉与导出里按判定筛源的过滤都是可选项，默认不读判定（`trace_branches` 的 `require_eval` 与导出器的 `filters.require_eval` 默认都是 `null`，发布的 `export_sft.yaml` 也写 `null`），度量报告从不读判定；去污染只认导出配置里明确列出的 benchmark（默认 `[]`：不去污染，数据集 manifest 记 `decontaminated: false`），不再从判定推断（§4.15 "切分与去污染"）；`execution_dag.md` 里评测器指向分叉与导出两个模块的边因此是可选边。剩下的一处依赖在数据一侧、不在判分器：导出的参考答案泄漏扫描读 benchmark 的评测数据加载器（§4.15 "校验"）。"Record Once, Evaluate Many" 不变：已提交的 trace 随时可以拿去评测。

| evaluator_id | 评测方式 | 适用 benchmark |
|-------------|---------|---------------|
| `exact_match` | 答案精确匹配 | GAIA, BrowseComp |
| `llm_judge` | LLM-as-judge 评分 | WebArena, Mind2Web |
| `test_suite` | 运行测试套件 | SWE-bench |
| `tool_trace` | 工具调用序列匹配 | ToolBench, BFCL |
| `visual_diff` | 截图对比 | OSWorld |
| `multi_criteria` | 多维度打分 | WebArena |

**评测器经 `TraceView` 读 trace，`TraceView` 认得步的来源（M31）**（`evaluator/trace_view.py`）。一个 trace 的步有三种来源（`StepRecord.origin`，§5.3）：

- `action_history` 只含 `origin == "policy"` 的步的动作，即这个 run 的 agent 自己做的决定；恢复出来的步（分叉 run 的前缀）是重放源 run 的动作，不混进来。
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

Serialization (可插拔策略)
    ├── text: 缩进文本 + [backend_node_id] 标记
    └── html: HTML 格式
```

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
逐字相等，并在真实 Chrome 里用同一张控件表比对两边的判定。Python 这一份有三处用：归档对原始采集的
脱敏（§4.14 隐私）；BrowserUse 清洗器建树时打码（下一段）；键入动作在敲字之前判定焦点所在的元素，
判定记进 `ActionResult.typed_into_sensitive_field`（§4.14 隐私、§5.6）。

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

#### 策略本身就是开关

`humanized` 是**策略的属性**，不是配置项：`MouseTrajectoryBase` / `TypingRhythmBase` 默认返回 `True`（会生成路径 / 会建模时序的策略，按构造就是拟人的），只有 `instant` 覆盖成 `False`。消费者读这一个布尔来决定路径之外的行为——冷启动位置、滚轮是否分段缓动、键入 / 选择前是否先把光标移过去，以及 `instant` 下整串一次 `insertText`。

**它旁边不允许再出现第二个开关**（defect F4）：`BrowserCapability` 上曾经有一个 `humanize_mouse: bool` 与 `mouse_trajectory` 受体并列。YAML 里同时写 `trajectory_type: windmouse` 和 `humanize_mouse: false`，配置校验全过，运行时静默走旧的直跳——用户配了一个策略，框架执行的是另一个。同一个语义有两个声明点，就必然存在互相矛盾的组合，而矛盾的那个组合没有任何地方会报错。所以规则是：**策略即开关**，`humanized` 只能从策略读，config 面上不得出现与之并列的布尔。`typing_rhythm` 从第一天起就是这个形状。

同理，两者都是 `BrowserCapability.__wiring__` 上的**受体**（wired receptor），不是 `Literal` 字符串：策略以**构造好的实例**注入，它自己的 knobs（windmouse 的 gravity / wind、bezier 的控制点、`human` rhythm 的 median_iki / hold 分布、learned 的 weights_dir / num_candidates，以及下面两节的 click-hold 与 aim 分布参数）因此全部经 conscribe 进入生成的 config，YAML 可达且被校验。曾经的形状是 `Literal` 加一个未类型化的 overrides dict，conscribe 把后者整个从 schema 里丢掉——knobs 存在，没有任何东西能设置它们。这与 `dom_cleaner` / `dom_serializer` 是同一类 defect（见 §4.12），`tests/test_core/test_capabilities/test_receptor_hygiene.py` 是结构性守卫。

但**受体身份是必要而非充分条件**：注册表还必须被 Composer 收集（`scripts/generate_all.py::_collect_registrars`），否则 conscribe 无从知道这个字段是一个判别联合，只能把它发成裸 `str`——knobs 依然不可达，而且更坏：一个拼错的 `trajectory_type` 不会被校验挡住，会静默退回 `instant`（非拟人）。`mouse_trajectory` 正是这样漏过一轮的，`_collect_registrars` 里现在留着记录此事的注释。新增任何一根策略轴都要同时做这两件事。

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

`dt_ms` / `gap_ms` / `hold_ms` 是**计划，不是 sleep 时长**。`input/_schedule.py::play_on_deadline` 是这一层唯一的调度器：它在发出任何东西之前就把全部间隔折算成**绝对 deadline**（slot *i* 到期于 `start + sum(dt[: i + 1])`），然后逐个 yield。调用方在两次 yield 之间 await 的东西（一次 CDP 往返，headless Chrome 实测 ~17 ms/`mouseMoved`、~12 ms/`mouseWheel`）因此花在它所属的 slot **里面**，而不是加在它**上面**。

先 sleep 间隔、再 await 派发，等于给每个 tick 加一次传输成本。实测后果：50 Hz 的 learned 路径被放成 32.6 Hz，windmouse（ease-out 尾巴每几毫秒就要一个 waypoint）被放慢到 **5.06 倍**，12 ms 的滚轮步被拉到 28 ms。改成绝对截止时间后 windmouse 1.10×、learned 1.01× @ 49.7 Hz（defect F14）。**这一层的时间就是它的全部意义**——learned 模型是按「在录制速率下匹配人类速度」挑出来的，滚动的步进节奏正是 anti-bot 系统读的东西——所以 wall time 必须跟着 schedule 走，不能跟着 transport 走。

跟不上的时候，规则是**丢点，不拖时间**：一个 slot 的整段时间在它能被发出之前就已经过去，就跳过它（掉帧，不是慢放），总时长与速度曲线保持诚实，只是采样变粗。**最后一个 slot 永远不会被跳过**——这正是端点不变量在调度器一侧的保证。「被跳过的 slot 意味着什么」属于调用方：`move_to` 干脆不发那个位置；`wheel` 把 delta 折进下一个真正发出的步（增量仍精确求和到 `(dx, dy)`）；`HumanKeyboard.type_text` 与 `HumanMouse.click` 则把积压的事件一起补发——按键可以迟到，但绝不能被丢（否则键就一直按着）。点击尤其如此：`click` 的 schedule 是 `[0.0, click_hold_ms()]` 配 `events = [pressed, released]`，补发的写法让「比 hold 还慢的传输」只会**缩短** hold，结构上不可能放出一个从未按下过的 release。

被 yield 而调用方选择不动作的 slot 依然花掉了它的时间——这就是「重复 waypoint」表达静止的方式：路径里一个与前点相同的坐标，调度器等它、`move_to` 不派发事件，于是 learned 模型里「点击前停在目标上」的那段留白按原样保留，而不被摊成 1 px 的微动。

#### `learned`：meta.json 对模型参数有最终解释权

`learned` 的 `weights_dir` 指向一个导出目录（`generator.npz` + `meta.json`）。**里面是哪个模型、以什么 tick 率、在多大的归一化窗口里训练的，一律从 `meta.json` 读，不从 YAML 读**（defect F7）：`format` / `kind` 决定走哪套 numpy 推理；`hz` 与训练窗口只要 meta 声明了就以 meta 为准，而 `mouse_trajectory` 块里写了不同的值会在**构造时**报错并同时指出双方，而不是静默地把模型按错误的速率或错误的尺度回放。理由与受体那条同源：配置不得与它所指的权重互相矛盾，而当两者必有一错时，权威属于产出权重的那一侧。

新训练出来的模型（比如一个 diffusion 导出）**不是一个新策略**，而是 `learned` 的一个 backend，按导出格式分派——这与 capability 的 backend 是同一个模式。真正的新策略是另一种运动模型：加一个类文件，加一个 wiring key，消费者零改动。

numpy 来自可选 extra `learned-mouse`，只在构造 `LearnedTrajectory` 时导入，绝不在模块导入时——所以没装 extra 的机器上注册与 config 生成照常工作。`input/_timing.py` 和 `input/_aim.py` 被 `learned` 导入，因此这两个模块必须保持 **numpy-free**。

### 4.14 回放与归档中间层（recorder 连接、`replayability`、browser 归档器）

Browser 实现了 §4.5 的第三条 provides 轴（`browser.network` / `browser.storage` / `browser.dom_raw`；回放 run 用 `browser.replay_log` 代替 `browser.network`），另有三根自己的环境策略轴：`replayability`（`none` / `pinned`，录制的可回放档位）、`network_source`（`live` / `archive`，网络从哪里来，M30）与挂在 `archive` 来源上的 `miss_policy`（`strict`，M30）。它们共用一条独立于 agent 的 CDP 连接，即 **recorder 连接**（也叫 early-attach 连接）。这一层的设计依据来自两轮实测（S1、S2a，Chrome 153.0.8010.53，headless，macOS；探针脚本在本地的 `tmp/flywheel/spikes/`）。前半部分是录制（M29），"回放"之后的几节是离线回放（M30）。下面引用的 Chrome 行为都会随版本变化，`requires_chrome` 的回归测试把它们钉成版本金丝雀。

```
BrowserCapability
├── 控制连接（agent 的）：tab attach、观测 scope、JS scope、动作、viewport/DPR
│     永不设置 waitForDebuggerOnStart: true
└── recorder 连接（RecorderConnection，browser/recorder.py；只在需要时打开）
      ├── browser 级 Target.setAutoAttach{autoAttach, waitForDebuggerOnStart, flatten}
      ├── 每个新 target：流水线准备（replayability → 回放服务 → 归档器 → Fetch / 递归 auto-attach），finally 里 resume
      ├── 暂停类事件 → 直接派生的 task（finally 里 continue / resume；archive 来源回放时未应答的请求一律 failRequest，从不 continue），从不进 scope 队列
      ├── 回放服务 ReplayServer（network_source: archive，M30）：Request 阶段的 Fetch 暂停的主人，
      │     匹配 → fulfill / failRequest，在暂停它的那个 session 上应答；决定写进回放日志 → browser.replay_log 的 replay.jsonl
      └── 贡献者声明的事件 → 本地分发器（逐贡献者准入：带 session id 的小事件，字节进它的侧存储）→ browser 级 archive EventScope
                                                       ← ArchiveWatchdog（shared，provides ∅）
                                                         → 已绑定归档器的运行期会话
```

#### 为什么是第二条连接

- **消息上限**：控制连接的 `max_frame_size` 是 100 MiB（af 自己的设置），超过就以 1009 关掉整条连接，所以一个大响应体或大上传（`Fetch.requestPaused` 把 POST body 带两份：`postData` 和 base64 的 `postDataEntries`）不能走控制连接，否则会带走 agent。Chrome 自己的上限分方向：它**读入**的一条 CDP 消息至多 100 MiB（DevTools HTTP server 的读缓冲，约合 75 MiB 的 base64 body），超过直接断开 socket，这只约束发给 Chrome 的消息，也就是回放的 `Fetch.fulfillRequest`（`max_fulfill_body_bytes`，下面"回放"）；它**发出**的一条消息至多 256 MiB（发送缓冲），超过的那条回复被悄悄丢掉、连接不断（2026-10-12 实测，Chrome 155：150 MiB 的 body 照常收到，200 MiB 的 body 没有回复）。所以 100 MiB 不限制录制：recorder 客户端不设帧上限，录制时哪些 body 存不下，由 `browser.network` 自己的分档决定（下面"体积分档"）。
- **队头阻塞**：大帧会堵住 agent 的命令回复。
- **暂停**：被 Fetch 扣着的请求一直卡住页面，直到有人应答；这条通道需要自己的事件处理，不能排在观测后面。
- **早接管**：只有 browser 级 auto-attach 加 `waitForDebuggerOnStart`，才能在弹窗、worker、OOPIF 的第一个请求和第一个脚本之前做好准备；控制连接要到 `switch_tab` 才接管弹窗。

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
| agent-cdp 0.4.0 的 `PausedTarget` 模式 A 在 af 的 flatten 客户端上静默 resume 错 session | 只用模式 B：自带的 resume 回调，按 session 路由 `Runtime.runIfWaitingForDebugger` |
| browser 级 auto-attach 也接上 Chrome 内部 target（`browser_ui`、`background_page`、扩展的 `service_worker`）；同一个 SW 会被接上两次（browser 级、注册它的页面之下），而 SW 主脚本的请求**只**在页面之下那个 session 上被拦截（M29 实测：那个 session 没开 Fetch 或被 detach，主脚本就哪儿也不暂停） | 只准备 `page` / `iframe` / `worker` / `shared_worker` / `service_worker` 且 URL 不是 `chrome:` / `chrome-extension:` / `devtools:` 的 target，其余只 resume 再 detach；同一 target 的第二个 session **照样准备**，不丢；于是它的请求可能每个 session 各暂停一次，由贡献者按 target 与请求去重记录（`RecorderConnection.sessions_for_target`） |
| 等待中的 SW session 上，`Network.enable`、`Runtime.evaluate` 要到 resume 之后才应答（`Fetch.enable` 立即应答） | resume 之前的命令一律流水线发出、一起等（逐条超时）；SW 上的 `Network.*` / `Runtime.*` 放到 resume 之后再等 |
| recorder session 上不先 `Page.enable`，`Page.addScriptToEvaluateOnNewDocument` 返回 ok 却从不执行 | frame 先 `Page.enable`，再注入 |
| dedicated worker 上没有 `Fetch.enable`；它的请求暂停在父页面 session 上 | 不给 dedicated worker 发 Fetch；永远在暂停请求的那个 session 上应答 |
| 第二个客户端的 `Emulation.setTimezoneOverride` 失败（"already in effect"） | 每个 override 只有一个所有者（下表） |
| 嵌套的 dedicated worker 脚本在 Request 与 Response 两个阶段都不经过 Fetch | 已知限制：既录不到也回放不了；从 Network 事件看得到的，记成未完成，不当作完整 |

#### 所有权与存活

- **只在自己的浏览器上**：需要 recorder 连接的配置要求 provider 声明 `NETWORK_CAPTURE`（§4.9.x）。显式 `cdp_url`、注入的控制客户端、六个云 lite provider，都在 `prepare()` 报错（`check_setup`；`bind_archivers` 绑定前再查与绑定有关的那部分，setup 启动前再查全部），点名双方，一次报全。云 provider 的录制可用性并入 lite SDK 审计，不在 v1。
- **存活是推送的**：`CDPClient` 在接收循环的 `finally` 里调用关闭回调（`add_close_callback`，回调拿到原因；`stop()` 主动关闭不算丢失）。recorder 连接意外断开时，capability 立刻标记自己不存活，用点名 recorder 的 `RecorderLostError` 让控制连接上还在等待的命令失败（`CDPClient.fail_pending`）；`begin_observation`、`execute_action`、`collect_archive` 入口检查并抛同一个错误；进行中的一次采集或一个动作结束时也再查一次——recorder 在途中丢了，这次采集不交出、这个动作不算成功。录制永远不会在没有记录的情况下继续；浏览器是 capability 自己的，teardown 照常杀掉它。
- **控制连接永不设置 `waitForDebuggerOnStart: true`**（它的 iframe auto-attach 一直是 `false`），所以 recorder 是唯一需要 resume 的一方。

#### setup 与 teardown

setup：协作检查（`check_setup`，`prepare()` 查过的同一个函数，再查一遍）→ 回放时加载整个源归档（`open_replay`）→ `backend.open(launch_args=...)`（只对声明了 `LAUNCH_ARGS` 的 provider）→ 控制连接 → 需要时建好 recorder 连接（还不 attach）：它的客户端、archive scope（`__archive__`，与 `__browser__` 平级，归 recorder 的生命周期）与 `ArchiveChannel`、`replayability` 的本 run 计划、已绑定归档器的运行期会话（`archive/run.py` 的 `BrowserArchiveRun`）→ global scope 与 watchdog（ArchiveWatchdog 按绑定安装）→ `RecorderConnection.open()`：设 browser 级 auto-attach，等已存在的 target 准备完（ready barrier）→ 初始 tab → `start_url`。watchdog 在 `open()` **之前**：ArchiveWatchdog 必须从 recorder 的第一个事件起就消费 archive scope，因为 `open()` 期间已存在 target 的 attach 事件若没人接，收集屏障会把它们算成丢失（M29 实测：fake CDP 下第一次收集就以"dropped 1 of 5"失败）。

teardown：标记不存活 → recorder 显式清理（`Target.setAutoAttach{autoAttach:false, waitForDebuggerOnStart:false}` → 继续所有还扣着的请求 → 每个 session `Fetch.disable` → `Target.detachFromTarget` → 停掉 recorder 客户端）→ 关闭归档会话与 archive scope → 原有的 bridge / watchdog / scope / 控制连接 → `backend.close()`。每一步都容错地往下走，第二次调用什么也不做。

#### 协作检查一览

"能配什么"由生成的配置约束，"能否协作"在这里查（2026-09-24 的裁决）。全部由 `browser/cooperation.py` 的一个函数算出、一次报全，每条点名双方。每一行都只取决于配置与任务数据，所以**全部在 `prepare()` 报**（用户 2026-10-13 的裁决）：`BrowserCapability.check_setup`（§4.9 `SetupCheck`，经 `Environment.check_setup`，§4.2 第 3d 步）带着本任务的 setup context（`cdp_url`、`replay_archive`）、观测协商结果与将要绑定的归档器算全部行，加上回放的一致性检查；错误在 `on_run_start` 打开 trace 之前出去，`spectragent-run --dry-run` 也查得出来。之后还有两次防御性复查，调的是同一个函数：`bind_archivers` 在绑定前查与绑定有关的行，setup 在启动浏览器之前把全部再查一遍（拦住绕过 `prepare()` 直接 setup 的调用方，与 `prepare()` 之后才注入控制客户端的测试）：

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
| 回放一致性：源 run 的环境指纹与本 run 的配置不一致（下面"一致性检查"；`prepare()` 只读源 run 的指纹） | setup，启动浏览器之前（之后才加载整个归档） |
| 已协商的 browser fragment + 当前 profile 装的 watchdog 解析不了它（profile 在构造之后被改过；当场重新解析） | setup |

#### 逐 target 的准备流水线

每个 `Target.attachedToTarget` 在派生的 task 里处理（它本身就是暂停类事件）：

1. 去重与过滤（上表）。
2. 收集本 target 的命令：`replayability` 的本 run 计划先给（frame：`Page.enable`、`Page.addScriptToEvaluateOnNewDocument(shim)`、时区 / 语言 / UA / 媒体 override、`Network.enable` 后的 `Network.setCacheDisabled` 与 `Network.setBypassServiceWorker`；worker：`Runtime.evaluate(shim)`、`Network.enable`、`Network.setUserAgentOverride`、`Network.setCacheDisabled`），然后是回放服务（M30，只在 `network_source: archive` 时存在：`Network.enable`、`Network.setCacheDisabled`，frame 上再加 `Network.setBypassServiceWorker` 与拒绝 SW 注册的注入脚本），再给每个已绑定归档器的会话（`browser.network`：`Network.enable{maxPostDataSize}`；`browser.replay_log` 什么都不发），最后是 recorder 自己的：所有贡献者的 Fetch pattern 合成**一次** `Fetch.enable`（只在有 Fetch 的 target 上；回放服务的 Request 阶段 pattern 也走这条路），递归的 `Target.setAutoAttach{autoAttach, waitForDebuggerOnStart, flatten}`。
3. 全部流水线发出，一起等（逐条超时），失败记日志、记在 target 上；`Runtime.runIfWaitingForDebugger` 用 `PausedTarget` 模式 B 放在 `finally` 里，任何一步抛错也照样 resume。

贡献者互不认识，recorder 也不按归档器 id 分支：它只问每个贡献者"这个 target 要发什么""你要哪些事件"。贡献者实现 recorder 的 `RecorderParticipant` 协议（`target_commands`、`fetch_patterns`、`event_methods`、`admit_event`、`on_event`、`pause_budget_s`、`on_paused`、`on_cycle`、`serves_requests`（M30），基类全部默认 no-op / `False`）：`replayability` 的本 run 计划是一个，回放服务是一个（M30），每个已绑定归档器的运行期会话各是一个。recorder 交给 archive scope 一侧的东西叫 `ArchiveChannel`（scope、贡献者、发出 / 处理计数与收集屏障），它只在绑定了归档器时存在。

#### 事件：暂停的进 task，元数据进 scope，字节进侧存储

- **暂停类事件**（`Fetch.requestPaused`、`waitingForDebugger` 的 `Target.attachedToTarget`）在 recorder 的回调里直接派生受跟踪的 task，**绝不进 scope 队列**：agent-cdp 的 scope 队列满 1024 时丢最新的事件，丢掉一个 `requestPaused`，页面就卡死。暂停的主人（Response 阶段是 `browser.network` 的会话）在超时之内读 body。超时是主人自报的预算（`pause_budget_s`，`browser.network` 是 `command_timeout_s + body_read_timeout_s`）再加 1 s；不报预算的主人用 recorder 的默认 15 s，旋钮因此不会被一个更短的全局上限悄悄截断；**continue 由 recorder 在 `finally` 里发**，不依赖主人记得。回放时规则相反（M30）：只要有一个贡献者声明 `serves_requests`（回放服务），recorder 对 Request 阶段任何没有被应答的暂停（主人超时、主人抛错、找不到主人、teardown 时还扣着的）一律发 `Fetch.failRequest{errorReason: "Failed"}`，**从不 continue**：今天唯一的回放服务属于 `archive` 网络来源，它不把请求放到线上（不同于 mitmproxy 的默认 `forward` 与 WARC-Bench 对 Sentry / Datadog 的 `route.continue()`）。这是 `archive` 来源自己的规则，不是框架的定律（下面"回放：网络来源与 miss 策略"）。主人自己发出 fulfill / fail 并成功之后才把 `PausedRequest.answered` 置位，所以发送失败的那个仍由 recorder 兜底失败。
- **元数据事件**：哪些 CDP 事件进 archive scope，由贡献者自己声明（`event_methods()`；`browser.network` 声明 `Network.*` 的请求 / 响应 / ExtraInfo / 结束 / 失败与 `webSocket*`），recorder 在 `open()` 时订阅当时贡献者声明的并集。和 Fetch pattern 一样，采什么在 recorder 打开时定下；它自己只认 target 的 attach / detach 簿记。recorder 模块里的本地分发器在客户端的接收循环里把每个原始事件交给声明了它的贡献者准入（`admit_event(method, params, session_id)`，同步）：贡献者返回要交给自己的小事件（带 session id），或 `None`；分发器在事件上记下它的 `consumer`，发进 archive scope。ArchiveWatchdog 在 scope 的循环里把准入的事件只交给准入它的那个贡献者（它可能指向那个贡献者的侧存储），target 簿记交给每个贡献者。准入时抛错与处理时抛错一样记进 `ArchiveChannel.errors`，下一次收集因此抛错。不用 `CDPEventBridge`：它的事件工厂拿不到 session id（让 agent-cdp 的 bridge 带上 session id 是以后的增强，M29 不给它打补丁）。
- **字节从不随事件走**（scope 的历史保留最近 1000 个事件）：响应体、POST body、WebSocket 帧的内容、原始 DOM 采集放在会话自己的侧存储里。流式 body 的块（`Network.dataReceived` 的 `data`）在准入时直接追加进它所属的流，准入什么也不交出，块从不进 scope；Chrome 对每个请求都发 `dataReceived`（没开流的不带 `data`），不属于任何流的在准入时丢掉。准入就是分界：`browser.network` 的准入（`archive/network_model.py` 的 `admit_network_event`）把 `requestWillBeSent` 的 `postData` / `postDataEntries` 与帧的 `payloadData` 留在会话的侧存储里（按准入事件的 `event_id`，事件处理时取回），归档从不保存的值根本不进 scope：剥离的请求头只留名字、值置空（名字照样进台账），ExtraInfo 只留 `requestId`、`headers`（与 `statusCode`），`associatedCookies` / `blockedCookies` / `exemptedCookies` 都不带，响应里重复的原始请求头（`requestHeaders` / `requestHeadersText`）去掉。原始事件本身不被修改（多个贡献者共用它）。
- **收集屏障**：`collect_archive` 先往 archive scope 发一个屏障事件并等它（`ArchiveChannel.barrier()`；应答它的排队处理器是 `ArchiveChannel` 自己挂的，所以屏障总排在之前的每个事件后面，不依赖谁在 scope 上监听）。队列是 FIFO，所以之前发出的元数据都已处理；分发器数着发出与处理的条数，对不上（队列丢过事件）就让这次收集抛错，归档不假装完整。

#### override 的所有者

| 设置 | 所有者 | 怎么设 |
|---|---|---|
| viewport + DPR | 控制连接 | `Emulation.setDeviceMetricsOverride`（`deviceScaleFactor` = `replayability.device_scale_factor`（属性）；`none` 是 0，保留平台 DPR） |
| 时区、语言 | recorder | `Emulation.setTimezoneOverride` / `setLocaleOverride`（每个 frame target，含 OOPIF） |
| UA、UA metadata、`Accept-Language` | recorder | frame：`Emulation.setUserAgentOverride`；worker：`Network.setUserAgentOverride`（worker 继承页面的 UA，但不继承 `Accept-Language`：不单独设，worker 里的 `navigator.language` 是宿主机的；macOS 上 `--lang` 对 worker 不起作用）。不带 `userAgentMetadata`，所以 `navigator.userAgentData.brands` 为空、不发 `Sec-CH-UA`——录制与回放一样 |
| 媒体特性（减少动画） | recorder | `Emulation.setEmulatedMedia` |
| 注入脚本（shim） | recorder | frame：`Page.enable` 之后 `Page.addScriptToEvaluateOnNewDocument{runImmediately: true}`（准备时已经在的文档，比如初始 tab 的 about:blank，也被 pin）；worker：resume 之前 `Runtime.evaluate` |
| 缓存、SW 旁路 | recorder | `Network.enable` 之后的 `Network.setCacheDisabled(true)`、`Network.setBypassServiceWorker(true)`（M29 实测：没有 `Network.enable` 的 session 上这两条返回成功却不生效）；pinned 档位发，回放服务不论档位也发（M30：回放时 SW 一律拦住，否则 SW 代理的导航以 `XHR` 暂停在 SW 的 session 上，主 frame 文档 miss 就分不出来）。不做每 run 一次的 `Network.clearBrowserCache`：pinned 的 profile 每 run 新建（协作检查拒绝别的），缓存本来就是空的，而清空一个空缓存实测每次 setup 要 3.9–9.3 s |
| V8 随机种子、`--lang`、`--force-device-scale-factor` 等启动开关 | `replayability.launch_args()` | 经 `LAUNCH_ARGS` 交给 provider（§4.9.x） |
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
    def plan(self, *, t0_ms: int, browser_user_agent: str) -> ReplayabilityPlan: ...
        # 本 run 的计划（也是一个 recorder 贡献者）：逐 target 的命令、指纹字段；配置实例不持有 run 状态
```

- **`none`**（默认）：什么都不做。
- **`pinned`**：旋钮 `base_time: Literal["run_start", "from_archive"]`、`random_seed: int`（≥ 1：V8 把 0 当作"用系统随机数"）、`timezone`（IANA，构造时校验）、`locale`（语言标签，构造时校验）、`device_scale_factor`、`user_agent: str | None`（None：沿用浏览器自己的 UA，只 pin `Accept-Language`）、`reduced_motion: bool`。第零条规则：录制与回放用完全相同的 shim、开关、种子、UA 与 viewport。`Accept-Language` 写成不带 q 值的列表（`en-US,en`），Chrome 自己加 q 值。
  - 启动开关只由它自己的设置生成：`--js-flags=--random-seed=<seed>`、`--lang=<locale>`、`--force-device-scale-factor=<dpr>`、`--disable-back-forward-cache`、`--disable-features=HttpsUpgrades`（Chrome 153 默认先把可注册域名的 http 导航升级成 https 试探；录制与回放都关），以及 `--disable-background-networking --disable-component-update --disable-field-trial-config`（它们在 `CHROME_DEFAULT_ARGS` 里，`ignore_default_args` 会把它们丢掉，pinned 重新加回）；写了 `user_agent` 时另加 `--user-agent=<ua>`（worker 与网络栈也看到同一个 UA，provider 自己的 `user_agent` 旋钮也因此能被报成冲突）。每个 run 必须是新建的 profile。
  - shim（`replay/shims.py`，Python 字符串常量，移植自 WPR `deterministic.js`，BSD-3，保留 notice）：无参 `Date` 与 `Date.now()` 从 T0 起、每 25 次构造前进 50 ms（**不冻结**，忙等能结束）；带种子的 `Math.random` 与 `crypto.getRandomValues`；`crypto.randomUUID` 是合法的 RFC 4122 v4；拒绝 `ServiceWorkerContainer.prototype.register`（配合 `setBypassServiceWorker`）；防重复注入。时区偏移交给 timezone override（不学 WPR 硬改 `getTimezoneOffset`）；`performance.now` 与定时器不改（WPR 的模型，残余的不确定性写明）。`shim_sha256` 是模板源码的摘要，进指纹。
  - `base_time: from_archive` 只与回放归档的网络来源一起有效：T0 取自源 run 指纹里的 `replayability.t0_ms`，setup 在 `plan()` 之前解析好，shim 因此在任何 target 准备之前就带着录制时的 T0（M30）；与 `network_source: live` 一起在 `prepare()` 报错（`check_setup`）。
  - 协作检查（`prepare()` 时，setup 再查一遍；点名双方，一次报全）：provider 没有声明 `LAUNCH_ARGS` 或 `NETWORK_CAPTURE`；显式 `cdp_url`；注入的控制客户端；provider 报告的旋钮冲突（`local_chrome`：`deterministic_rendering=true` 的 V8 种子与 pinned 的 `random_seed` 不同、`force_device_scale_factor` 与 pinned 的 DPR 不同、`user_agent` 与 pinned 的不同、`extra_args` 另设了 pinned 拥有的开关；每一行都是值相同就放行，`deterministic_rendering` 也一样（用户 2026-10-13 的裁决），它的 `--deterministic-mode` 与 `--font-render-hinting=none` 不是 pinned 拥有的开关）；profile 不是新建的（`persistent_profile()` 非空：`user_data_dir`、非默认的 `profile_directory`、`extra_args` 里的 `--user-data-dir`；profile 目录只由 YAML 决定，以前的环境变量 `CHROME_USER_DATA_DIR` 已删掉（同一次裁决），因为它让 run 是否从新建的 profile 启动在 YAML 和 `config.json` 里都看不出来）；`base_time: from_archive` 而网络来源是 `live`。
  - 已知取舍：pinned 会改变活站点的行为，本身也是 anti-bot 能抓到的特征（用户已接受）；评测 run 与 anti-bot run 保持 `none`。

#### Browser 归档器：配置实例与运行期会话

- **`BrowserArchiverBase(ArchiverBase)`**（`archive/base.py`）：配置实例只持有旋钮（YAML 构造、trace hook 持有、`bind_archivers` 交给 capability），从不持有 session 或 scope。它声明要哪些通道（`needs: ClassVar[frozenset[ArchiveNeed]]`，取值 `"fetch_response"`（线上流量：拦截的网络来源拒绝有它的归档器）/ `"raw_dom"` / `"observation_cycle"` / `"replay_log"`（M30：记下回放服务的决定，`browser.replay_log` 有它；回放的协作检查按它找记录回放日志的归档器，live 来源拒绝有它的归档器）），并用 `open(ctx) -> BrowserArchiveSession` 造出本 run 的运行期会话（纯构造，不发 CDP）。每个 browser 归档器都有 `allow_session_state: bool = False` 旋钮。
- **`BrowserArchiveContext`**（frozen，仿 `WatchdogContext`，capability 在 setup 时组装）：recorder 句柄（按 session 发命令、target ↔ session 映射、浏览器版本）、archive scope、原始采集仓库、tab 列表、当前 cycle、时钟、隐私事实（profile 是否新建）、回放服务的日志（`replay_log: ReplayLogFeed | None`，M30，只在回放时有：`drain()` 交出可直接写成 JSON 行的条目，归档一侧因此不 import 回放的类型）。
- **`BrowserArchiveSession`**（运行期会话的基类，是一个 `RecorderParticipant`，方法默认 no-op）：`target_commands(target)` / `fetch_patterns(target)`（准备流水线的贡献）、`on_event(event)`（archive scope 里的元数据，同步）、`on_paused(paused)`（暂停的主人才收到：Response 阶段是要 `fetch_response` 的会话）、`on_cycle(cycle)`（只给要 `observation_cycle` 的会话）、`collect() -> list[ArchiveFile]`、`aclose()`。
- capability 遍历已绑定的实例与它们的会话，**不按 `archiver_id` 分支，不查注册表**（receptor hygiene Rule 2）。通道在 setup 时按当时绑定的需求并集打开；setup 之后再绑定只改变谁收事件、谁被收集（会话按实例保留，新实例开新会话，解绑的会话随 run 关闭），不给已准备好的 target 补开通道（runtime 只在 setup 之前绑定）。setup 时没有绑定任何归档器、没有开 archive 通道的 capability，之后再绑定归档器在 `bind_archivers` 报错，因为那样的绑定什么也采不到。
- **运行期的归档侧**（`archive/run.py`，`BrowserArchiveRun`）：capability 在 setup 时建它、teardown 时关它；它持有 channel、本 run 的会话、`RawCaptureStore` 与指纹状态，`collect` 按下面的顺序收集。capability 只负责接线（建 scope、组装 context、调用它），不在自己的模块里写归档逻辑。
- **原始 DOM 的待定槽位**在 `_ObserveCycleCache.raw`（§4.12）：DOM resolver 的 sink 与 dom_raw 自采（经 context 的 `park_raw`）都放进当前 cycle 的槽位；每次尝试开始时清空（重发的那次替换上一次），`_populate_cycle` 成功时由 `BrowserArchiveRun.commit_raw` 补上 cycle、tab、viewport、cleaner 键后提交进 `RawCaptureStore`，失败的 cycle 什么也不留。sink 只在 setup 时打开了 `raw_dom` 通道时才装；装了之后，当前没有绑定要 `raw_dom` 的归档器时它什么也不放。
- **ArchiveWatchdog**（`watchdogs/archive_watchdog.py`，`level="shared"`，`provides=∅`，先例是 `JavaScriptExecutionWatchdog`）：**按绑定安装，不按 profile**。`BrowserWatchdog.should_install(ctx)` 是新的类方法（默认 True），capability 在建 watchdog 的地方求值；ArchiveWatchdog 只在 `ctx.archive` 存在（recorder 开着且绑定了归档器）时返回 True，从没有 recorder 的 context 也能惰性地建出来。它在 archive scope 上把准入的事件交给准入它的会话（target 簿记交给每个会话），并给每一个记一次"已处理"（屏障由 `ArchiveChannel` 自己应答）；某个会话处理事件时抛错，记进 `ArchiveChannel.errors`，不打断别的会话。有会话要 `observation_cycle` 时，它覆盖 `bind_tab_scopes(observation_scope, js_scope, *, session_id)`，把 tab 观测 scope 的 `BrowserStateRequestEvent` 跨 scope 连到 archive scope（`target_scope=`），`unbind_tab` 丢掉每 tab 的状态。tab 的 scope 仍然只有观测与 JS 两个，为每 tab 第三个 scope 设计的 `TabScopes` 组合对象不做：archive scope 是 browser 级的（弹窗的流量在控制连接接管它之前就到了），那个动机已经不存在，到下一个真正 per-tab 的 scope 再泛化 `bind_tab_scopes`。
- **`collect_archive()`**：没 setup 或什么都没绑定 → 空批次；否则入口查存活 → 屏障 → 上次收集以来有会话处理事件时抛过错（`ArchiveChannel.errors` 增加了）就抛错 → 按绑定顺序收集每个会话（每个会话在一条同步语句里换出自己的缓冲区）→ 指纹 run 记录（内容变了才再发）→ 批次。每一步都有界；采不到就抛错。
- **实测开销**（Chrome 155，fixture 站点除 `nested_worker` 外的 12 个场景、在同一个浏览器里各访问两次，`none` 档位，DOM agent，三个归档器全开；`test_browser_archive_live.py::test_overhead_benchmark` 写出 `ArchiveOverheadReport`）：每步（导航 + 观测 + 收集）p50 多 ≈ 10–25 ms、p95 多 ≈ 90–100 ms；上限 p50 300 ms（`core/archive/budget.py::ARCHIVE_MAX_P50_OVERHEAD_MS`，度量报告的归档开销规则用的是同一个常量，§4.16；基准可用 `SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS` 临时覆盖）。字节（这次 run 收集到的全部证据，含收尾那次收集，除以步数）：去重前每步 ≈ 1061 KB，去重后 ≈ 537 KB（2026-10-13 起两个 6 MB 的 body——`large` 场景的脚本与 XHR——都边下边收、存下；之前只补取脚本、XHR 记 `too_large`，那时是 537 / 275 KB）。去重前的算法：每份内容寻址文件都计入，每条 revisit 记录按它指向的那条完整 response 记录的存储长度计入（payload 与压缩相同，只差几个头部字节）。省下的字节几乎全来自内容寻址：两个 6 MB 的 body 两次访问各收一遍，各只存一份（每步 ≈ 524 KB）。revisit 每步只净省 ≈ 83 B：gzip 后一条 revisit 记录约 510–530 B，比几十字节的 JSON 或脚本的完整记录还大，这些 payload 写成 revisit 反而各多花 50–90 B；净省主要来自 Range 视频的 206 body 和较大的 HTML 页面。

#### `browser.network`

- **在 Fetch Response 阶段定 body 怎么取**：`requestPaused` 时（Chrome 扣着响应）按体积分档；内联读用 `Fetch.getResponseBody`，带超时（`body_read_timeout_s`，默认 10 s：Response 阶段的读取包含下载本身，不是 Lighthouse 那种读缓存的 1 秒；其余 recorder 命令用 `command_timeout_s`，默认 1 s），然后 recorder 在 `finally` 里 `Fetch.continueResponse`。不依赖 `Network.getResponseBody` 的缓冲区（桌面版总量 200 MB、单个 20 MB，先进先出淘汰，跨进程导航整体丢失）。
- **体积分档**（按声明的 `Content-Length`；64 MiB 以内的 body 不论类型都存下，用户 2026-10-13 的裁决）：
  - 不超过 `max_inline_body_bytes`（默认 5 MB），或者没声明长度：**内联读**，Chrome 扣着响应直到整个 body 下载完、交给 recorder，再放行。
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
  - 残余：页面不读的 body 流不完（`fetch()` 之后从不读 body，或停住的视频），它的记录一直 `pending`，run 结束时当作 `incomplete`；内联读时 Chrome 会自己把 body 读完。每次收集只等内联读（`drain_timeout_s`），从不等流：流什么时候完由页面的下载决定，可能永远不完。
- **元数据**：移植 Playwright `crNetworkManager.ts`（Apache-2.0，保留 notice）的事件模型：Fetch 与 Network 事件到达顺序不定，按 `networkId` 配对；重定向链每一跳一条记录（3xx 没有 body，`redirected_from` 串起来）；`*ExtraInfo` 配对（原始的 `Set-Cookie` 只在这里出现）；worker 的请求在 worker session 上 `requestWillBeSent`、在父页面 session 上暂停。
- **POST body**：`Network.enable{maxPostDataSize: 65536}`；来源依次是 `requestPaused` 的 `postDataEntries`（暂停走 task，从不进 scope），`requestWillBeSent` 的 `postDataEntries` / `postData`（在准入时就留进会话的侧存储，事件本身不带 body，见上面"字节从不随事件走"），最后是 `Network.getRequestPostData`。
- **WebSocket**：握手记成一条交换（`body_status: websocket`），帧写进本步的 `websocket.jsonl`（方向、opcode、时间、内容）。
- **304 永远不覆盖 200**：304 原样记录（空 body）；回放只把它当作带条件头（`If-None-Match` / `If-Modified-Since`）的请求的候选，从不拿它顶替 200（M30）。
- **Range**：206 按收到的样子记录：部分 body、`Content-Range` 原样保留、索引里解析出 `content_range`；超过内联上限的 206 与别的 body 一样边下边收。从不为 206 另取整个资源；回放用全部 206 记录拼出每个 URL 的字节区间，没覆盖的区间是 miss。
- **格式**：每步一个 WARC/1.1 `exchanges.warc.gz`（warcio，核心依赖，Apache-2.0；每条记录一个 gzip member）。request 与 response 记录以 `WARC-Concurrent-To` 关联；body 存解码后的字节，HTTP 头里去掉 `Content-Encoding` / `Transfer-Encoding`、按解码长度写 `Content-Length`（原值记在索引里）；同一 run 里重复的 payload 写成 `revisit` 记录（`identical-payload-digest`，指向第一次的 response 记录）。另配一个步索引 `index.jsonl`：
  - `kind: exchange`：每个交换恰好一次，在它定下来的那次收集里。字段：`record_id`、`response_record_id`、两条 WARC 记录的 `offset` / `length`、`redirected_from`、method、url、status、resourceType、frameId、loaderId、session / target、initiator、时间、**原始请求事实**（剥离后的请求头、POST body 的 sha256 与长度、content-type；脱敏后的 body 在 WARC 的 request 记录里）、`payload_sha256`、`revisit_of`、`content_range`、`body_status`（`ok` / `redirect` / `too_large` / `streamed_later` / `websocket` / `incomplete`）、脱敏台账。
  - **不写规范匹配键**：匹配键由 M30 在加载时用唯一的规范化实现从原始事实算出，规则变了也没有过期的键。
  - `kind: body`：一个 `streamed_later` 的 body 流完（它的请求结束时），指回 `record_id`：`ok` 带 `payload_sha256` / `payload_length`（body 是那个内容寻址的 blob），否则 `incomplete` / `too_large` 带 `note`。
  - `kind: pending`：这次收集时还没定下来的交换（请求已发出，响应或 body 还在途），只有状态、不带证据；之后它的 `exchange` 行恰好出现一次。`exchange` 行已经写过、body 还在流式接收的 `streamed_later` 记录也每次收集一条（`waiting_for: streamed_body`），直到它的 `kind: body` 行交出。一个 `record_id` 的最后一行是 `pending`，它就是 run 结束时还在途的那个，读者当作 `incomplete`（§4.9 "最后一次收集"）。每次收集先在 `drain_timeout_s` 之内等正在读的 body 与补取。
- **什么时候定下来**：终结事件（下一跳的 `redirectResponse`、`loadingFinished`、`loadingFailed`、WebSocket 握手应答）到了、body 读完；**或者** body 已在 Response 阶段读完：Chrome 扣着响应直到放行，读到的就是完整的 body，而页面从不读取 body 的 `fetch()` 根本不会有 `loadingFinished`（M29 实测，Chrome 155：GraphQL、弹窗、SW 的 fetch 原本都永远停在 `pending`）。两种等待只值一次收集：这种交换的 `responseReceived`，以及任何交换的原始响应头（`responseReceivedExtraInfo`，`Set-Cookie` 只在这里，Fetch 的 `responseHeaders` 与 `responseReceived` 都不带它，实测）。屏障保证收集之前发出的事件都已处理，所以一次收集的宽限足够；Chrome 不发的，到期照样写出（`raw_response_headers: false`）。从缓存来的响应不等 ExtraInfo（crbug 1340398）。目标 detach 时它还在加载的交换记为失败。
- **body 是解码后的**：`Fetch.getResponseBody` 返回解码后的字节（实测：`Content-Encoding: gzip`、`Content-Length: 50` 的响应读出 1600 字节明文）；索引的 `original_encoding` 记原来的 CE / TE / CL。Fetch 看到的请求头里有 `Cookie`（Chrome 155），与其他来源一样剥离。`Network.dataReceived` 的 `data` 同样是解码后的字节（gzip 实测）。没有 `networkId` 的暂停一律不记、原样放行，是防御性的后备（归档器自己不发任何请求）。
- **WARC 里只放已知的响应**：`ok`、`redirect`、`websocket`（101）有 response（或 revisit）记录；`too_large`、`streamed_later`、`incomplete` 只有 request 记录，状态与响应头写在索引行里，因为空的或不完整的 payload 会骗过不读索引的人。流式收到的 body 是内容寻址的 blob（`body-<sha>.bin`，原样不压缩：64 MiB 不可压缩的数据写进 gzip WARC 要约 1.8 s CPU，实测），`kind: body` 行在它的 `exchange` 行写出之后才交出。
- `require_replayable: bool = True`：与 `replayability: none` 一起绑定 → `prepare()` 报错（`check_setup`；`bind_archivers` 与 setup 再查一遍），点名归档器与档位。
- **只录线上流量**：`browser.network` 永远是"录流量"（用户 2026-10-13 的裁决），不随网络来源换意思。回放（`network_source: archive`）没有自己的线上流量，送出的每个字节都已经在源归档里，所以绑定它被协作检查拒绝（按 `fetch_response` 需求，`prepare()` 就报），报错点名回放要绑定的 `browser.replay_log`（下面）。
- 其他旋钮：`strip_request_headers`（大小写不敏感，追加在默认三个之后）、`max_post_data_size`、`allow_session_state`。原先补取大 body 的上限 `max_streamed_body_bytes` 与补取本身（`Network.loadNetworkResource` 再下载一遍，只对静态 GET）一起删掉：边下边收覆盖所有类型，64 MiB 以上一律 `too_large`。

#### `browser.storage`

在**收集时**读：`Storage.getCookies`，以及每个 tab 每个 origin 的 `DOMStorage.getDOMStorageItems`（local 与 session），走 recorder 连接。记在本步（动作之后的状态），有意不按每个观测 cycle 记：收集点本来就是步的边界，多一次 cycle 级采集只会把同样的状态再记一遍。`storage.json` **只在状态变了时**才写（采集时间不算）：某一步的状态是那一步及之前最近的一份；也因此一次什么都没发生的收集交出空批次（归档契约"交出并忘掉"）。localStorage 每个 origin 读一次（它在 origin 的 tab 之间共享），sessionStorage 按 tab 与 origin 读；frame tree 与 DOM storage 在 recorder 的 session 上不需要 `Page.enable` / `DOMStorage.enable`（实测）；读的时候已经关掉的 tab 记在 `unreadable_tabs` 里，不致命；读失败的 origin 记在 `unreadable_origins`（tab、origin、local / session、原因）；进程外的 iframe 是自己的 target，不在 tab 的 frame tree 里，它的 storage 又按顶层站点分区（这份文档还没有分区键），所以 v1 不读、记在 `unread_frames`。缺什么都写明，不悄悄缺失。cookie（名字、值、域、路径、过期、标志）与 local / session storage 的条目都**原样保存**（用户 2026-10-13 裁决，威胁模型"只有我"，§5.6）；会话 token 就在里面，所以 `browser.storage` 声明 `contains_session_state`。文件仍带 browser 台账，条目为空，只说明写它时的规则版本。`allow_session_state` 在它身上与在别的归档器上一样，只表示"接受非新建的 profile"。

#### `browser.dom_raw`

- DOM 参与了协商：持久化本 cycle 已经采到的原始采集（§4.12 的 sink，只要提交了的 cycle），不重新采；归档路径**不发任何 `DOM.*` 命令**（backendNodeId 是懒分配的，多一次采集可能扰动 `serialized_dom`）。
- 纯视觉（UI-TARS：没有协商 `browser.dom`）：ArchiveWatchdog 把观测 scope 的 cycle 事件跨 scope 交给会话，会话在 recorder 的 session 上自己采一次（与 BrowserUse 同一个采集函数：`DOM.getDocument` + `DOMSnapshot`（含 paint order）+ AX + DPR + 文档戳），结果同样只在 cycle 提交时才进仓库。代价是每步多一次完整的 DOM 采集，在 archive scope 里与截图并行，cycle 等它做完（M29 实测，fixture 站点：自采本身 p50 ≈ 6 ms、p95 ≈ 10 ms，在会话的 `on_cycle` 外面计时；大页面上会更多），上限 `self_capture_timeout_s`（默认 10 s），超时或失败让下一次收集抛错。自采不跟进进程外 iframe（`frames=None`）。归档一侧的文档戳不触发 cycle 重发，所以开不开归档，观测都一样。
- Skyvern 的 JS 路径归档 JS 的原始输出。
- 落盘：每个提交的 cycle 一份 gzip 的 JSON（键排序、集合转成有序列表，`mtime=0`），内容寻址，带 tab、loaderId、url、viewport、DPR、cleaner 键；cycle 与采集时间在来源信封（`ArchiveProvenance`）里、不在内容里，所以没变的文档跨步只存一次；重新导航之后 node id 都变了，同一个页面不会去重（实测）。脱敏在构造 `ArchiveFile` 之前完成（下面的隐私一节）。`archive/dom_raw.py` 的 `capture_from_document` 把归档内容还原成 `TargetCapture`，离线换 cleaner 用；JSON 的键都是字符串，它把 Skyvern 按整数 ref 建的两张表（`skyvern_nodes`、`skyvern_hit_tests`，`skyvern_capture.INTEGER_KEYED_MAPS`）还原成整数键，否则两个 Skyvern cleaner 按 ref 查节点全部落空（测试：归档往返之后，两个 cleaner 离线清洗出的序列化文本、选择器表的 backend id 与命中测试都与在线相同）。
- 用途：离线换 cleaner / serializer 重算观测（BrowserUse cleaner 在给定 capture、viewport、DPR、前一步 selector map 时是纯函数）；用 paint order 离线判断遮挡（M32）。

#### `browser.replay_log`

回放 run 的证据：回放服务的每个决定（下面"回放日志"）。原先由 `browser.network` 在回放时换模式来记，用户 2026-10-13 裁决拆成独立的归档器（`archive/replay_log.py`），每个归档器只有一个意思。

- `needs = {"replay_log"}`：协作检查按这个需求找它，`network_source: archive` 没有它就报错（miss 必须总有记录），`network_source: live` 绑定它也报错（live 来源什么也不回放）；两条都在 `prepare()` 报（`check_setup`），`bind_archivers` 与 setup 再查一遍（上面"协作检查一览"）。
- context 里带着回放服务的日志（`BrowserArchiveContext.replay_log`，只在回放时有），`open(ctx)` 交出 `ReplayLogSession`：不要 Fetch pattern、不订阅事件、不发命令；每次收集在一条同步调用里把这段时间的日志换出来，写成本步的 `replay.jsonl`（下面"回放日志"），这段时间没有决定就什么也不写。没有回放日志的 context 里 `open` 抛错（协作检查在那之前就拒绝了这种配置）。
- 不写 WARC / `index.jsonl` / `websocket.jsonl`：命中的记录用 `record_id` 指回源归档。
- 旋钮只有 `allow_session_state`（每个 browser 归档器都有）。日志的折叠（`log_repeat_limit`）是回放服务的，仍是 `archive` 来源的旋钮。
- 隐私标记是基类的 `MARKER_BROWSER_REDACTED`：日志里的 body 键由脱敏后的 body 算出，没有 cookie、没有 `Set-Cookie`，所以不声明 `contains_session_state`。

#### 环境指纹（`env/run.browser.json`）

capability 自己的 run 记录（`archiver_id="browser"`，`scope="run"`，`archive/fingerprint.py`），第一次收集时发出，内容变了才再发：Chrome 版本（`Browser.getVersion`）、UA 与 UA metadata、viewport、DPR、时区、语言（pinned 取参数；`none` 在第一个 tab 主 frame 的 isolated world 里经 recorder 的 `Runtime.evaluate` 实测：`Page.createIsolatedWorld`（`af-fingerprint`），页面脚本看不到这次读取、也改不了它读到的值（getter 陷阱、改写的 `JSON.stringify` 都不起作用，M29 实测）；每个文档（loaderId）只测一次，同一文档上的收集只多一条 `Page.getFrameTree`；残余：测量本身仍在页面的渲染进程里执行，主线程忙或对话框开着时这一次会等到 1 s 超时、换下一个 tab）、replayability 档位与参数（T0、种子……）、`shim_sha256`、启动开关摘要（`ProviderSession.metadata["launch_flags"]` 去掉网络路由开关（`chrome_switches.NETWORK_ROUTING_SWITCHES`）之后的 sha256；录制时 fixture 的 `MAP` 规则与回放时的隔离开关都不进摘要，两边才可比；`command_line` 从不进指纹）与 pinned 自己的开关表（单独存）、网络来源（M30：`network_source` 块，`live` 只有 `{"source_id": "live"}`，回放见下面"回放指纹"）、脱敏策略（`BROWSER_REDACTION_POLICY`，以及每个绑定归档器自报的 `redaction_facts()`：剥离的请求头）、隐私标记（`contains_session_state`、`fresh_profile`、`level`：有归档器保留会话态时是 `session_state`，否则 `redacted`）。`navigator.userAgentData` 只在 secure context 里存在（实测：tab 停在纯 http 的页面上时是 `null`），所以 UA metadata 记最近一次测到的，指纹不随页面来回翻。

#### 隐私（§5.6 的 browser 部分）

- 请求头 `Cookie` / `Authorization` / `Proxy-Authorization` 与 `strip_request_headers` 追加的那些，在**每一个**来源剥离，从不写盘：Fetch 的请求头、`requestWillBeSent`、`requestWillBeSentExtraInfo`（连同 `associatedCookies` 的值）、WebSocket 握手；台账记名字（`rule: request_header`）。
- 响应头 `Set-Cookie` 保留（回放的 cookie jar 需要）；`browser.network` 因此声明 `contains_session_state`，指纹同样标出。`responseReceivedExtraInfo` 里 `blockedCookies` / `exemptedCookies` 的 cookie 值不记。
- POST body：urlencoded / multipart / JSON 里名字命中共享字段名规则的值换成 `<redacted>`，写台账（`rule: field_name`）。`redact_post_body(body, content_type)` 从 `archive/redaction.py` 导出，M30 先用它脱敏线上请求再算匹配键，录制与回放用同一个函数。
- 原始 DOM：敏感控件（`dom/sensitivity.py`）的 `value` 属性（`DOM.getDocument` 与 Skyvern 的 `DOM.describeNode`）、它下面 user-agent shadow root 里的文本（`pierce: true` 会把输入值带出来）、`DOMSnapshot` 的 `inputValue` / `textValue`、AX 的 value，在构造 `ArchiveFile` 之前换掉并记台账（`rule: input_value`）。
- DOM 观测：BrowserUse 清洗器建树时按同一条规则把同样的位置换成 `<redacted>`（§4.12），提示词与 `browser.dom` fragment 都不带敏感值；Skyvern 的页内脚本本来就不带。
- `storage.json` 原样保存 cookie 与 local / session storage 的值（用户 2026-10-13 裁决，§5.6）；`browser.storage` 声明 `contains_session_state`，`allow_session_state` 只表示"接受非新建的 profile"。
- 两个新规则名（`request_header` / `input_value`）加进共享台账的 `RedactionRule`；browser 的规则版本是 `BROWSER_REDACTION_POLICY`（`<PRIVACY_POLICY>+browser-2`，写进每个 browser 台账的 `policy`），规则源码由测试按摘要钉住；与 `PRIVACY_POLICY` 一样，改规则就升版本。`browser-2`（2026-10-13）删掉了 cookie 值的哈希和它的规则名 `value_hash`。
- **键入标记**：`browser.type` / `browser.type_coordinate` 在敲字之前读焦点所在的元素（穿过开放的 shadow root 与同进程的 iframe），按同一条规则判定，动作结果的 `typed_into_sensitive_field` 记下判定：`true` / `false`；焦点在看不进去的 frame 里（进程外的 iframe、跨源）或读焦点失败时是 `null`（不知道，不是"不敏感"）；封闭的 shadow root 里看到的是宿主，判为 `false`。它进 `steps/NNN.json`；agent 只渲染动作结果的成败与错误，提示词逐字节不变（测试钉住）；导出怎么用它随学生侧一起推迟。键入的文字本身照旧原样留在动作参数与 LLM 记录里（§5.6）。
- **会话态（`logged_in` 的定义）**：没有任何地方声明 `logged_in`，所以定义为"浏览器不是从新建的 profile 启动的"，即 provider 的 `persistent_profile()`（`NETWORK_CAPTURE` 的承诺之一，§4.9.x）点名了一个旋钮（只看配置，从不看环境变量）。这时只要绑定了归档器，每个归档器都必须写 `allow_session_state: true`，否则报错，点名 provider 的旋钮与归档器（pinned 本来就拒绝非新建的 profile）。v1 的数据来源是无需登录的公开站点。
- 残余，写明：WebSocket 帧的内容原样记录（没有字段规则可用）；字段名规则只认凭证形状的名字；URL 原样记录（回放按 URL 匹配），query 里的凭证不脱敏；`browser.extract_content` 给了元素编号时返回 `DOM.getOuterHTML` 的原文，页面写进标记的敏感 `value` 会随动作结果进提示词（动作路径，未改）。
- 验证（`test_browser_archive_live.py`）：经 `Network.setExtraHTTPHeaders` 植入 `Authorization` 与 `X-Api-Key`（后者在 `strip_request_headers` 里）、经 `Network.setCookie` 植入 cookie、一个 urlencoded POST 的密码、脚本设置的密码输入值，fixture 收到了每一个；植入的 cookie 值只出现在 `storage.json`（原值），其余植入值在 trace 目录里（gzip 解开）一次也不出现，三种台账规则都出现；正向对照：WARC 里的标记请求能找到。DOM 观测路径另有一条（`test_privacy_guard_on_the_dom_path`，BrowserUse 清洗，绑定 `browser.dom_raw`，观测像运行期那样提交进 trace）：写进标记的、脚本设置的、`display:none` 的密码，在提示词与 trace 目录里一次也不出现；经 `browser.type` 敲进去的只出现在 `steps/000.json` 的动作参数里，它的结果带 `typed_into_sensitive_field: true`，敲进普通文本框的是 `false`；普通文本框的值照常出现在观测里（正向对照）。`Proxy-Authorization` 无法这样植入：Chrome 155 让每个带它的请求以 `net::ERR_INVALID_ARGUMENT` 失败；它的剥离由 fake CDP 测试覆盖（ExtraInfo 与 Fetch 两个来源）。

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
- **等待**：`_capture_once` 每次尝试之前 `wait_settled(_NAVIGATION_SETTLE_TIMEOUT_S)`（10 s，模块常量，与 `_OBSERVE_MAX_ATTEMPTS` 同一写法）；到上限仍在途就记一条警告、照常采集（下一条兜底）；上限按尝试算。没有在途导航时立即返回、不让出事件循环：没有导航的步与链接点击（fixture 上实测：链接的提交早于下一次观测）的时序不变；任何档位上观测时仍在途的跨文档导航都会被等：`none` 档位的表单提交同样等到提交（实测约 15 ms），真实站点上应答慢的链接也一样，看到的是新文档而不是旧的。
- 实测（裸 CDP，Chrome 155，带 pinned 的这个开关）各种交互在该 tab 的 session 上发什么：表单提交（GET / POST）、链接、`location.href` 是请求 → 跨文档的开始 → `frameNavigated` → `frameStoppedLoading`，点击最后一个回复比请求早约 1 ms 到；文本框里回车时请求早于按键的最后一个回复，动作返回时导航已经在途；204 与下载是请求 → 开始 → `frameStoppedLoading`，没有提交；锚点链接（含 `href="#"`）、`pushState` 没有请求，只有 `navigatedWithinDocument`；`mailto:` 是请求 → 开始 → `frameStoppedLoading`；连不上的主机提交的是出错页（`chrome-error://chromewebdata/`），之后 Chrome 自己重载一次（开始 → 停止加载）；`javascript:` 链接、被 `preventDefault()` 拦下的提交、`target=_blank`（链接或表单）、`<a download>` 在这个 session 上没有请求。
- **命令上限**：观测截图的 `Page.captureScreenshot` 有自己的上限（`screenshot_watchdog._SCREENSHOT_COMMAND_TIMEOUT_S`，10 s，远小于事件的 30 s；`browser.screenshot` 动作不受限）：Chrome 不回应的那一次算这次尝试缺 fragment，由原有的 `_OBSERVE_MAX_ATTEMPTS` 重发吸收，不再吃掉整个事件的超时，晚到的回复由客户端丢弃。它兜住跟踪覆盖不到的：检查之后才发起的导航（页面的定时器）。实测：把等待拿掉，表单提交之后那一步 10.1 s、第二次尝试成功（修之前 30 s 后以 `observe` 失败）。观测路径上其余要等 Chrome 回复的命令也都有上限（2026-10-13）：DOM watchdog 的整次采集（cleaner 选的全部命令，进程外 frame 在内）`dom_watchdog._DOM_CAPTURE_TIMEOUT_S` 10 s，超了这次尝试缺 DOM fragment、同样由重发吸收；小的读取——`browser_meta` 的 `Page.getNavigationHistory` 与 `Page.getFrameTree`、截图之后的 `Page.getFrameTree`——`watchdogs/base.py` 的 `OBSERVATION_READ_TIMEOUT_S` 2 s（`observation_read`），超了只少一个文档戳（或 `browser_meta` 的 URL 与标题），不让整个 fragment 失败。一次尝试最坏 10 + 10 + 3 × 2 = 26 s，仍在事件的 30 s 之内，重发还有余地（测试钉住这个不等式）。渲染进程不回应（对话框开着、页面脚本占满主线程、文档正在替换）时，一条命令因此最多花掉一次尝试，而不是整个事件。
- **回归**：`test_browser_form_submit_live.py`（真实 Chrome，fixture 的 `form_submit` 页，`replay_driver.record_config` 的形状：pinned + 三个归档器、BrowserUse `dom_vision`、按元素 id 点击的脚本化模型）：GET 与 POST 表单的点击提交、文本框里回车提交（`browser.send_keys`），提交之后那一步 5 s 之内完成（实测约 0.1 s）、URL 是结果页、loaderId 换了、DOM 里是回显的字段、没有 `observe` 错误；`none` 档位、不开归档的同样流程照旧通过。不用 Chrome 的部分在 `test_browser_navigation_settle.py`（含弹窗自己关掉时 feed 的解除）与 `test_browser_observation_bounds.py`（各条命令上限，以及卡住的 DOM 采集只花一次尝试）；`test_browser_popup_close_live.py` 在真实 Chrome 里让被接管的弹窗自己 `window.close()`，它的 feed 随 Chrome 的 detach 解除，打开它的 tab 照常跟踪。

#### 回放：网络来源与 miss 策略（M30）

两根 browser 内部的策略轴，都在 `core/capabilities/browser/replay/`，与 `replayability` 同一种受体形状（策略即开关，单实例受体在生成的 schema 里必填）：

- **`NetworkSourceRegistrar`**（`network_source`，判别字段 `source_id`；`network_source.py`：`NetworkSourceProtocol` / 注册表 / `NetworkSourceBase` / `default_network_source()`）。`BrowserCapability.__wiring__["network_source"] = ("network_source", ["archive", "live"])`，类级注解加 Python 默认（`live`）。**每一个手写的 capability 块**写 `network_source: {source_id: live}`。
  - `live`（`live.py`，默认）：什么都不拦截，没有旋钮；`intercepts` 为 `False`。
  - `archive`（`archive_source.py`，`ArchiveNetworkSource`）：从一个已录制 run 的归档回放网络。**它从不把请求放到线上**：这是 `archive` 来源自己的规则，不是框架的定律（用户 2026-10-13 的裁决）。要"miss 时转去真实网络"，那是一个新的网络来源（`NetworkSourceRegistrar` 的新键），不是 `archive` 的旋钮，也不是 miss 策略：miss 策略的决定只有 fulfill 与 fail 两种应答。**不是 dataclass**、手写 `__init__`：它带一个受体 `miss_policy`（`__wiring__ = {"miss_policy": ("miss_policy", ["strict"])}`，类级注解 + `default_miss_policy()`）。旋钮：`matcher: MatcherConfig`（嵌套的 pydantic 子配置：`levels: list[Literal["exact", "normalized", "fuzzy"]] = ["exact", "normalized"]`（至少一级、不重复）、`fuzzy_threshold: float = 0.5`（[0, 1]）、`site_rules: str | None = None`，即规则目录；写成 **pydantic dataclass** 而不是 `BaseModel`：conscribe 1.3 的代码生成把每个 `BaseModel` 都当成它自己生成的子模型、不写 import，生成的 `_composed.py` 会在导入时 `NameError`，而 dataclass 和其他类型一样被 import，YAML 一侧没有区别，拼错的键照样被拒）、`isolation: Literal["required", "none_acknowledged"] = "required"`、`shift_dates: Literal["none", "all"] = "none"`、`max_fulfill_body_bytes`（默认 64 MiB，校验上限 72 MiB：Chrome 读入的一条 CDP 消息 ≤ 100 MiB，base64 之后约 75 MiB 的 body）、`fulfill_timeout_s`（默认 10 s）、`log_repeat_limit`（默认 3）。
- **`MissPolicyRegistrar`**（`miss_policy`，判别字段 `miss_policy_id`；`miss_policy.py`）：v1 只有 `strict`（`strict.py`），这根轴存在是因为用户要求留口子。`decide(miss) -> MissDecision`：请求类别（`document` / `benign` / `subresource`）、怎么应答、是否标 `diverged`。
- 生成配置是三层嵌套的判别联合：capability → `network_source` → `miss_policy`（`archive` 块里必须写 `miss_policy: {miss_policy_id: strict}`）。`network_source` 与 `miss_policy` 都进 `_collect_registrars()`。

```yaml
network_source:
  source_id: archive
  isolation: required            # 默认；none_acknowledged = 明说不隔离
  shift_dates: none              # none（默认，配 base_time: from_archive）| all（配 base_time: run_start）
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

**加载（`archive_index.py`，setup 时、启动浏览器之前）**：`ArchiveNetworkSource.open_replay(reader) -> RecordedArchive`：源 run 的环境指纹（`archive_run_file("browser")`，连同它的 sha256）、`browser.network` 的每一份 `index.jsonl`（步与 trailing，`archive_lines`）与它们的 WARC、站点规则。索引只有原始请求事实（M29），**匹配键在加载时用唯一的规范化实现重算**，规则改了也没有过期的键。每条可服务的记录带 `run_id`（源 run），按录制顺序编号（请求开始的墙钟时间 `started_at`，同时间按索引行的顺序；没有开始时间的（只留下 `pending` 行的）排在最后）。同一次提交的 `index.jsonl` 与 `exchanges.warc.gz` 按 `iter_archive` 的迭代结构配对（同一次提交的文件连续给出、名字不重复），不看路径：

- `ok` / `redirect`：response（或 revisit）记录在同一步的 `exchanges.warc.gz` 里（索引给 offset / length）；revisit 的 payload 按 `revisit_of`（第一条 response 的 `WARC-Record-ID`，可能在更早一步的 WARC 里）取，HTTP 头仍用 revisit 自己的。WARC 里存的是解码后的 body，响应头已去掉 CE / TE、`Content-Length` 是解码长度。
- `streamed_later`：只有 request 记录，状态与响应头在索引行里；body 是之后某次收集的 `kind: body` 行指向的内容寻址 blob（`blob(payload_sha256)`）。没有 `ok` 的 body 行就按 `body_unavailable` 处理。
- `too_large`、`incomplete`、最后一行是 `pending` 的：有记录、没有 body，命中它们按 miss 处理（原因 `too_large` / `body_unavailable`）。`websocket` 的握手从不当作候选（WebSocket 不经 Fetch 暂停）。
- 206（`content_range`）：同一 URL 的全部 206 记录与 200 记录拼成字节区间图；304 只给带条件头的请求当候选。
- 索引行解析不了、指到的 WARC 记录对不上（类型或 `WARC-Record-ID`）→ `ReplayArchiveError`，点名源 run、文件与记录 id；setup 因此失败，浏览器没有启动。

#### 匹配（`canonical.py`、`matcher.py`、`site_rules.py`）

**规范化（`canonical.py`，唯一实现，加载与线上请求共用）**：

- **先脱敏**：线上请求的 body 先过 M29 的 `redact_post_body(body, content_type)`（`archive/redaction.py`，录制时存进 WARC 的就是它的输出），再算任何键。归档里只有 `<redacted>`，所以带密码或 token 的 POST 照样精确命中。
- **URL**：scheme 与 host 小写，去掉默认端口与 fragment，query 参数排序；去掉易变参数（事实取自 pywb `rules.yaml`、wabac、WARC-Bench 的列表，规则自己写，不移植它们的代码）：`utm_*`、`fbclid`、`_ga`；名字是 `_` / `cb` / `uncache` 或含 `cache`、值是数字（可带 `.`、`-`）的参数；值形如 `jsonp…` / `jQuery\d+_\d+` 的参数（jsonp 的 `callback`）；名字含 `bust` 或 `ts` 且值是时间戳的参数，即值是 `1` 再跟 12–15 位数字（pywb 的 `1[\d]{12,15}`：毫秒或微秒的 epoch；任意 13–16 位数字会把 `products=<EAN-13>` 这样的条码、编号也当成时间戳去掉，L1 就把不同的资源当成同一个）。WARC-Bench 的 `timestamp`、`_`、`r`、`width`、`height` 照它自己的做法只抹掉值、保留参数名。
- **body**（移植 cdxj-indexer `postquery.py`，Apache-2.0，保留 notice；不依赖 cdxj-indexer / surt）：加 `__wb_method=<METHOD>`；urlencoded 与 multipart 变成参数（multipart 不看 boundary，文件部分与不是 UTF-8 的部分取 sha256）；JSON（含 `*+json`）拍平，键是**完整路径**（`a.b.0.c`，上游只用叶子名会撞键；空对象 / 空数组记成 `{}` / `[]`），逐行 JSON 逐行拍平；`text/plain` 或没有类型的先试 JSON、再试 `k=v&…` 表单形状（与 `redact_post_body` 认类型的方式一致）；解析不了的 body 记作 `__wb_post_data=<sha256>`；body 参数与 query 参数合在一起排序——**排序是我们自己写的**（上游 `postquery.py` 不排序，排序来自 AGPL 的 `surt`）。规范化后的 body 串超过 4096 字符时换成它的 sha256（上游截断到 4096 字符，大 GraphQL 请求会撞键）。所有带 body 的方法都参与（上游只取 POST / PUT）。
- **GraphQL**（JSON body 里有看起来是 GraphQL 文档的 `query`（以 `{`、`query`、`mutation`、`subscription` 或 `fragment` 开头，所以 `{"query": "red shoes"}` 这样的搜索 API 不算），或有 `extensions.persistedQuery.sha256Hash`；GET 的 `query` / `variables` / `operationName` / `extensions` 参数同理）：键是 `operationName` + 空白归一后的 query 的 sha256（只有 persisted query 的 hash 时用它自己的 sha256）+ 拍平后去掉易变路径（站点规则 `ignore_body_paths`，在单个操作之内匹配 `variables.cursor` 这样的路径，所以同一条规则对单个请求和批量请求都有效）的 `variables`；批量请求（顶层数组）逐项算、按顺序编号（`0.operationName`…）连起来。

**三级匹配**（`matcher.py`）：

- **L0 精确**：method、完整 URL（不含 fragment）、body 字节（脱敏后）都相同；multipart 比较时去掉 boundary（参照 Playwright `harBackend.ts` 的精确匹配，Apache-2.0）。
- **L1 规范化**：上面的规范键相同。
- **L2 模糊**：同 host、同 path、同 method，打分 `2M/T`（M = 两边值相同的参数名数，T = 两边参数名总数，两边都没有参数时为 1.0；比的是规范化之后的 query 参数，带 body 的请求连同它规范化的 body 参数一起比，这只在站点规则对非 GET 打开 L2 时才会遇到），**得分 ≥ `fuzzy_threshold` 才算命中**；默认对文档请求与非 GET 请求关闭，站点规则 `fuzzy: on` 时对该站点全部打开。阈值与这两条限制是**我们的设计**：WPR 的模糊匹配没有阈值（同 path 得分最高的总会赢），也从不看 body。命中记 `level: fuzzy` 与得分。`levels` 里没有 `fuzzy` 时 L2 只对有 `fuzzy: on` 规则的站点生效。
- **每条记录只用一次**：每个 episode（一个回放 run）每个键一个游标，按录制顺序依次给出（mitmproxy 的 pop 语义、WPR 的 `serve-response-in-chronological-sequence`）；已用集合跨级共享。三级都找不到没用过的记录时，**重复最好那一级候选里录制顺序最后的一条，并记 `exhausted`**。这是我们的选择（mitmproxy 用尽即 miss，WPR 退回最佳头部匹配），轮询式页面因此不会在第 N+1 次突然失败；它可测，日志里看得见。
- **run 亲和**：合并过的归档（合并工具在 M33 之后）里候选来自不同 run 时，优先本 episode 第一次命中的那个 run，再按录制顺序回退到别的 run（"Right HTML, Wrong JSON"）。v1 的归档只有一个 run，亲和由两 run 的合成索引单测钉住。
- **请求头只打破平局**：键内由录制顺序决定；只有其余都相同的候选（得分相同的不同 L2 URL、合并归档里时间相同的记录）才按匹配的请求头数量排，比的是 Fetch 看得见的那组头（S2a：`Accept`、`Referer`、`User-Agent`、页面自设的头在 Request / Response 两个阶段一样），去掉被剥离的头与站点规则的 `ignore_headers`；再平局时录制早的优先，然后 URL 字典序小的优先（WPR 的做法）。
- **miss 时给出最近的 3 个候选**：同 host 的记录按（同 method、同 path、L2 得分）排，取前 3 个（`record_id`、method、URL、得分、是否用过）；host 从没出现过时为空。
- 带 `Range` 的请求从区间图取（下面"服务"），不消耗游标。区间图按规范键（GET）收录同一资源的全部 206 与 200 记录；请求的第一个字节被录到就算覆盖，从它起连续录到的字节一直给到请求的末尾（`bytes=N-` 时给到连续段的末尾）；`bytes=-K` 需要知道总长；多段的 `Range` 列表不送（`range_uncovered`）；这个资源从没录到过时是 `no_match`。
- 匹配到、却送不出 body 的记录（`too_large`、`body_unavailable`）也照样消耗游标：结果带上记录与原因，由 miss 策略应答。

**站点规则**（`site_rules.py`；`matcher.site_rules` 目录里的 `<domain>.yaml`，例 `configs/replay_rules/`）：请求 host 按**最长后缀**选一个文件（`a.b.example.com` 依次找 `a.b.example.com.yaml`、`b.example.com.yaml`、`example.com.yaml`……），不依赖 Public Suffix List；文件名写可注册域名照样能用。schema 是 extra-forbid 的 `SiteRule`：`ignore_query`（参数名或通配）、`ignore_body_paths`（完整路径通配）、`ignore_headers`、`fuzzy: on`（YAML 1.1 读成 `true`）、`synthesize_204`（URL 通配：miss 时当良性请求回 204，分析类请求）、`response_echo`（`[{url, request_path, response_path}]`：命中时把请求 JSON 里的 id 抄进响应 JSON 的对应路径，路径可带 `*` 走数组，即 WARC-Bench 处理 Salesforce Aura 的那种；表单 body 里值是 JSON 的参数也能走进去（`message.actions.*.id`）；请求里的值按顺序写进响应里已有的位置，只有一个值时写进每个位置；不新增键；响应不是 JSON、请求里没有值、或值本来就相同时，字节原样不动；`SiteRule.echo` 是纯函数，由应答一侧调用）。规则目录的摘要（每个文件的名字与 sha256，排序后再取 sha256）进回放指纹。

#### 服务（Request 阶段，在 recorder 连接上）

- **回放服务**（`server.py`，`ReplayServer`，由 `ArchiveNetworkSource.serve(recorded, ctx)` 造出，纯构造）是一个 recorder 贡献者：在每个 page / iframe / shared worker / service worker target 上贡献 `{urlPattern: "*", requestStage: "Request"}`（合进那一次 `Fetch.enable`），在每个准备的 target 上 `Network.enable` + `Network.setCacheDisabled(true)`，frame 上再加 `Network.setBypassServiceWorker(true)` 与拒绝 `ServiceWorkerContainer.prototype.register` 的注入脚本（`replay/shims.py` 的 `SERVICE_WORKER_BLOCK`，`Page.enable` 之后、`runImmediately`；它只替换 `register`，注入几次结果都一样，pinned 的 shim 也拒绝，两份叠加无害），这些全部在 `runIfWaitingForDebugger` 之前（模式 B）。它是 Request 阶段暂停的主人，`serves_requests` 为真；recorder 在 `open()` 时看一次参与者里有没有服务（与 Fetch pattern 一样，打开时定下），之后按它决定兜底是 fail 还是 continue。`ctx` 是 `server.py` 的 `ReplayContext`（recorder 句柄、观测 cycle、时钟），capability 在建 recorder 连接时组装。
- 每个 `Fetch.requestPaused` 在 recorder 的回调里派生一个受跟踪的 task（从不进 scope 队列，M29 的规则），回放服务在 `fulfill_timeout_s` 之内在**暂停它的那个 session** 上应答（dedicated worker 的请求暂停在父页面 session 上；OOPIF 的文档暂停在父页面 session、子资源在 iframe session），选择与消耗游标在同一段同步代码里完成；没应答的由 recorder 在 `finally` 里 `Fetch.failRequest`（上面"事件"）。**`archive` 来源的任何决定都不会把请求 continue 到线上**：这是这个来源自己的规则（上面"回放：网络来源与 miss 策略"），由测试钉住。
- 状态都按 recorder 的 session 记：target detach 时 recorder 的簿记连同它的 session 一起清掉，回放服务丢掉那个 session 的缓存；控制连接上的 `close_tab` 与 `_rollback_tab_attach` 因此不需要额外的回放清理。teardown 先关 recorder（还扣着的请求一律 fail），再关服务：停止应答、丢掉状态；最后一次收集之后才做的决定不再写出（收尾那次收集在 teardown 之前）。游标是整个 run 的，不按 tab。
- 服务自己的代码出错（匹配、应答、策略抛异常）：这个请求由 recorder 兜底 fail，`ReplayLog.fail` 记下原因，下一次收集（`drain()`）抛错：日志不完整时不假装完整，与归档会话 `_errors` 的规则相同。发送失败（Chrome 拒绝那条 fulfill）不算服务的错：日志照记，`answer: failed`，recorder 兜底 fail。
- **按记录的形态应答**（`responder.py`，纯函数，每种形态一个单测）：
  - 录下的 3xx 连同 `Location` 原样送出，Chrome 自己跟随。Playwright `harBackend` 在查找时就沿重定向链走到底、送出最终响应，我们不那样做（Fetch 拦截下送原始 3xx 更忠实）。
  - 不在归档里的 CORS 预检（`OPTIONS` 且带 `Access-Control-Request-Method`；不按 resourceType 判断，它是 `XHR`）→ 合成 204：`Access-Control-Allow-Origin` = 请求的 `Origin`（从不是 `*`）、`Access-Control-Allow-Credentials: true`、`Access-Control-Allow-Methods` / `-Headers` 回显请求的方法与头，加 `Vary: Origin`。**预检永远不走 404**：预检失败，真正的请求就不会发出。
  - `Range: bytes=N-` / `bytes=N-M` → 用区间图合成 206（`Content-Range: bytes N-E/total`、`Accept-Ranges: bytes`），长度受 `max_fulfill_body_bytes` 限制（Chrome 会接着要后面的区间）；**从不用 200 回答 Range 请求**（S2a：200 能播放但 seek 失效）；区间没被覆盖 → miss（`range_uncovered`）。
  - 去掉 `Content-Encoding` / `Transfer-Encoding`，按送出的 body 重算 `Content-Length`，去掉 HTTP/2 伪头（`:status`…）。这是给页面 JS 看的头部卫生，`fulfillRequest` 本身原样送字节、不看这几个头。多条 `Set-Cookie` 分成多个头条目送出。
  - 日期：`shift_dates: none`（默认）原样送出录下的 `Date` / `Expires` / `Last-Modified` 与 cookie 过期时间；`all` 把四者按同一个 Δ = now − 记录时间（录下的 `Date`，没有就用 WARC-Date）一起平移。**从不只平移一部分**：Chrome 会按送出的 `Date` 重算 cookie 过期（`Expires + (now − Date)`），只移 cookie 会移两次。没有 `Date` 的响应补一个 `Date` = 记录时间 + Δ，让 Chrome 的换算有锚点。`none` 配 `base_time: from_archive`，`all` 配 `base_time: run_start`；两者独立配置，不互相检查。
  - body 超过 `max_fulfill_body_bytes` → 不 fulfill（发给 Chrome 的一条消息超过 100 MiB 会带走整条 recorder 连接），按 miss 处理（原因 `too_large`）。
- **miss（`strict`）**：类别按 method、头与 frame 判断，文档优先：
  - 文档请求（`resourceType: Document`；SW 拦住之后导航都以 `Document` 暂停在页面 session 上）：暂停它的是 `page` target 且 `frameId` 等于 target id 的是**主 frame**，→ `Fetch.failRequest{errorReason: "Failed"}` 并在日志里标 `diverged`；否则是**子 frame**，→ `failRequest` + miss 记录。用 `Failed` 是因为 404 或 `BlockedByClient` 会掐断 Chrome 的 http 回退（S2a）。
  - 良性请求（`resourceType` 是 `Ping`（beacon、`<a ping>`）或 `CSPViolationReport`、`Content-Type` 是 CSP / Reporting 报告（`application/csp-report`、`application/reports+json`、`application/expect-ct-report+json`）、host 落在分析类黑名单——内置一份（`strict.py` 的 `BENIGN_HOSTS`，按后缀、在标签边界上匹配）、`extra_benign_hosts` 追加——或命中站点规则的 `synthesize_204`）→ 204，同样带 `x-replay-miss: 1`（回放编出来、不是录下来的应答都带这个头，页面与读日志的人都分得出来）。
  - 其余子资源 → 404，带 `x-replay-miss: 1`。
  - 归档里有记录但送不出 body 的（`too_large`、`body_unavailable`、`range_uncovered`）按同一张表应答，原因照记。
- **WebSocket 与绕过 Fetch 的请求**：握手从不经过 Fetch，v1 一律记为 miss：回放服务声明 `Network.webSocketCreated`，记 `reason: websocket`；隔离下握手本身失败。`Network.requestWillBeSent` 看到、却从没在 Fetch 上暂停过（按 `networkId` 对）、随后 `Network.loadingFailed` 的 http(s) 请求（嵌套的 dedicated worker 脚本就是这样）记 `reason: unservable`，`answer: none`；随后是 `Network.loadingFinished` 的也记，那是一个绕过 Fetch、真的从别处拿到了响应的请求（隔离下黑洞没有例外，不应出现；`none_acknowledged` 下是线上），日志里必须看得见。这两类不经 miss 策略（没有暂停可以应答），`request_class` 为空。事件经 recorder 的分发器准入（只留 `requestId`、URL、method、type、frameId，不带头与 body）进 archive scope；暂停先到还是 `requestWillBeSent` 先被处理都一样（服务按 `networkId` 对账）。v2 的 WebSocket 回放用页面内 shim（参照 Playwright `webSocketMock.ts`）。

#### 回放日志（`replay.jsonl`）

回放服务把每个决定写进自己的日志（`replay_log.py`：`ReplayLog`），`browser.replay_log` 的 `ReplayLogSession` 在每次收集时把它换出来、写成本步的 `env/steps/NNN/browser.replay_log/replay.jsonl`。**收集取走它的那一步就是归因的那一步**（capability 不知道步号，与网络证据同一个模型；episode 就是这个 run）。每行是一个 `ReplayLogEntry`（pydantic，extra-forbid）：

- `kind`：`hit` / `exhausted`（重复了用尽的最后一条）/ `synthetic`（合成的预检）/ `miss` / `summary`（被限流折叠的那些）；`captured_at`、`cycle`；请求事实 `method`、`url`、`resource_type`、`frame_id`、`frame`（文档：`main` / `sub`）、`target_id`、`session_id`。
- 命中：`record_id` 与它的 `run_id`、`level`（`exact` / `normalized` / `fuzzy`）、`fuzzy_score`、`range`。
- miss：`request_class`（`document` / `benign` / `subresource`；`websocket` / `unservable` 不经策略，为空）、`reason`（`no_match` / `too_large` / `body_unavailable` / `range_uncovered` / `websocket` / `unservable`）、`answer`（`fulfilled` / `failed` / `none`）与 `status`、`match_key` 与 `body_key`（规范键，超过 512 字符的写成 `sha256:<hex>`）、`candidates`（最近的 3 个）、`diverged`（主 frame 文档 miss）；匹配到了、却送不出 body 的（`too_large`、`body_unavailable`）也带 `record_id` / `run_id`。每个决定（命中也一样）都记 `answer` 与 `status`。
- **限流**：同一次收集里**完全相同**的条目超过 `log_repeat_limit` 之后只计数，收集时折成一条 `summary`（它是最后一条被折叠的条目，`folded_kind` 记它原来的 kind，`count` 是折叠掉的条数，不含照写的那 `log_repeat_limit` 条），因为归档页面的轮询可能每分钟上千次。完全相同指除了发生的时刻（`captured_at`、`cycle`）与列出的候选之外每个字段都相同：对同一个请求、在同一个 frame 里的同一个决定（kind、原因、应答、状态、请求类别、`frame`、`diverged`、record）；请求按规范键认（带缓存破坏参数的轮询是同一个请求反复问），没有规范键的（WebSocket、绕过 Fetch 的请求）按 URL。所以不同的 URL 从不折在一起，发散也从不被折进别的 miss。

读侧：`RunTrace.archive_lines(path, model)`（§5.5，通用的逐行 JSON 访问器，解析或校验失败抛点名文件与行号的 `TraceSchemaError`）；browser 一侧的 `replay_log(reader, step) -> list[ReplayLogEntry]`、`iter_replay_log(reader)`（含 trailing）、`diverged_steps(reader)` 建在 `ArchiveReader` 上，按写它的归档器（`browser.replay_log`）找文件，别的归档器下同名的文件不是回放日志。**"这一步发散了"在读时推出**：这一步的回放日志里有主 frame 文档 miss；M30 不改 `StepRecord`（M31 的 `restoration` 另算）。回放日志导出成 HAR 是可选项，推迟到有按 HAR 打分的评测器时。

#### 一致性检查与回放指纹

`check_setup` 里（`prepare()` 时，setup 在 `backend.open` 之前再查一遍；§4.9 `SetupCheck`），`cooperation_problems`（配置能否协作）之后，打开源 run、只读它的环境指纹（`archive_index.py::load_fingerprint`：`env/run.browser.json`、录制的 T0、`browser.network` 提交了几个 `index.jsonl`；不读索引行、不读 WARC，返回的 `RecordedArchive` 索引为空、没有 store，什么也匹配不上、什么也送不出），再把 `replay/consistency.py` 的 `consistency_problems(...)` 并进同一个 `BrowserCooperationError`。整个归档（`load_archive`：索引与每条 WARC 记录的校验）由 setup 在检查通过之后加载，用于服务；索引坏了（`ReplayArchiveError`）仍在 setup、启动 Chrome 之前失败。检查失败时既不打开 trace 也从不启动 Chrome，每条点名两边的值：

- 源 run 没有 `env/run.browser.json`，或没有录 `browser.network`；源 run 自己就是回放（指纹的 `network_source.source_id` 是 `archive`，它绑定的是 `browser.replay_log`、没有 WARC，应当回放原始录制）。
- 档位不同；档位参数不同（`base_time` 除外，`from_archive` 正是回放要改的那一个）；`shim_sha256` 不同（pinned）；viewport 不同。
- 启动开关摘要不同：本 run 的摘要由 provider 的 `planned_launch_flags(档位开关 + 隔离开关)` 在启动之前算出，两边都去掉网络路由开关（§4.9.x），所以隔离开关不进比较；隔离由上面的协作检查断言（`required` 而 provider 支持，或明写 `none_acknowledged`）。两边有一边没记摘要时跳过并记一条说明。
- `base_time: from_archive` 的 T0 取自源指纹的 `replayability.t0_ms`，在 `plan()` 之前解析。

浏览器起来之后（recorder 连上、准备第一个 target 之前），**只警告、不报错**的三项，记进回放指纹的 `warnings` 并打 WARNING 日志：Chrome 版本（`Browser.getVersion` 的 product / revision）不同；档位没有固定 `user_agent` 时浏览器自己的 UA 不同（它带着版本号）；源指纹的脱敏策略（`redaction.policy`）与当前 `BROWSER_REDACTION_POLICY` 不同（规则变了，带凭证字段的 POST 可能对不上键）。`none` 档位录下的时区、语言、DPR 是测出来的宿主机事实，不比较（写明）。

**回放指纹**：回放 run 自己的 `env/run.browser.json` 多一个 `network_source` 块：`source_id: archive`、`source_run_id`、`source_fingerprint_sha256`（源指纹那份已提交记录的 sha256）、`site_rules_sha256`、`matcher`（levels、阈值、规则目录）、`miss_policy_id`、`shift_dates`、`isolation` 与 `warnings`。块的一半是配置实例自报的（`NetworkSourceProtocol.fingerprint()`：`live` 是 `{"source_id": "live"}`，`archive` 是它的旋钮），一半是这次 run 的事实（`ReplayServer.fingerprint()` 加上源 run 与两个摘要、`warnings`）；capability 有服务时交服务的块，否则交来源的块，`BrowserArchiveRun.collect(network_fingerprint=...)` 写进指纹。`warnings` 里还有一致性检查没能比较的说明（`consistency_notes`：一边没有启动开关摘要）。

一致性一侧的函数都在 `replay/consistency.py`：`consistency_problems`（`prepare()` 与启动前，拒绝）、`consistency_notes`（启动前，说明）、`version_warnings`（启动后，警告）、`replay_t0`（`from_archive` 时 T0 取源指纹的，否则取本 run 的开始）。capability 只组装与转交；本 run 交给 provider 的开关（档位的，加上隔离的）与 provider 据此会记下的开关表，由 `cooperation.py` 的 `launch_args(cap)` / `planned_launch_flags(cap, ...)` 算出，同一份开关既用于启动，也用于启动前的比较。

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
| 带元素 index 的动作：`browser.click`、`browser.type`、`browser.select_dropdown`，以及带了 `index` 的 `browser.scroll` / `browser.extract_content` | 几何再签名（`method="geometry_signature"`），见下 |
| `browser.switch_tab`（`target_id` 是每个浏览器新生成的 CDP targetId） | 按 tab 顺序与 URL（`method="tab_order_url"`）：在录下观测的 `browser_meta.tabs` 里找到这个 `target_id` 的位置，恢复出的 `tabs` 同一位置的 tab URL 相同就换成它的 id；`target_id` 为空（"最近打开的 tab"）原样 |
| 坐标动作、导航（`navigate` / `go_back` / `go_forward`）、`send_keys`、`wait`、`done`，以及不带 `index` 的 `scroll` / `extract_content` | 恒等（`method="identity"`）：pinned 回放下布局确定，截图哈希兜底 |

几何再签名：(1) 取录下观测 `selector_map[index]` 的条目：它的 `bounds`（页面坐标的 border box，已计入 iframe 偏移）、`tag_name`、`attributes` 与子树文本；(2) 在恢复出的 `selector_map` 里找 `bounds` 的四个量都在 `retarget_tolerance_px`（`BrowserCapability` 的旋钮，默认 2.0 CSS px）之内的候选；(3) 用签名确认：`tag_name`，`id` / `name` / `aria-label` / `role`（属性，或 AX role）各自相等，子树文本规范化（空白折叠）后的 sha256 相等；(4) 恰好一个候选时把 `params["index"]` 改写成它的 handle（可以在别的 OOPIF 命名空间里），其余参数不动；(5) 没有候选是 `failure="not_found"`，多于一个是 `failure="ambiguous"`，`detail` 写候选数。录下的条目缺 `bounds`、本 run 没有激活 `browser.dom`（或这一次的 `browser.dom` 采集失败），或者录下的 `selector_map` 里根本没有这个 index（源 run 的这个动作当时就找不到目标），都是 `not_found`（`detail` 说明原因）。子树文本只读到条目根以下 64 层（`TEXT_DEPTH_LIMIT`）：trace 的编码大约在第 100 层截断条目的子树，活的节点没有截断，两边读到同样的层数，同一个元素的签名才相同；实测的页面最深约 40 层。

**校验**（`verify_restoration`）交回一份 `RestorationEvidence`：

- `url_match`：两边 `browser_meta.url` 逐字相等（任一边没有 `browser_meta` 时为 `None`）；
- `signature_match` / `signature_missing` / `signature_extra`：两边 `selector_map` 每个条目的签名（同上：tag、四个标识属性、文本哈希；不含 index 与 `bounds`）组成的多重集是否相等，以及录下有而恢复出没有、恢复出多出来的个数（没有 `browser.dom` 时为 `None`）；
- `screenshot_distance`：两张截图的 64 位 dHash（Pillow：转灰度、Lanczos 缩到 9×8、相邻像素比较）的汉明距离（任一边没有截图字节、或字节解不开时为 `None`，后者记 WARNING）；
- `document_misses`：自上一次 `verify_restoration` 以来回放服务记下的主 frame 文档 miss 数（`ReplayLog` 的单调计数 `diverged_total`，带 `diverged` 的条目（折叠进 `summary` 的也算）每条加一；回放日志照常被 `browser.replay_log` 逐步取走，不影响这个计数）。capability 记着上一次读到的总数，建回放服务时与 teardown 时归零。`network_source` 是 `live` 时为 `None`。

阈值不在这里：`screenshot_max_distance` 与 `strict | warn` 是恢复器的旋钮（§4.1）。

#### 已知限制

- 嵌套的 dedicated worker 脚本绕过 Fetch（见上）。
- 归档开着时，内联读的 body（不超过 `max_inline_body_bytes`、没声明长度的，以及 64 MiB 以内的文档）会被扣到下载完，页面拿不到渐进的 HTML；最终 DOM 不变，依赖加载时序的页面行为可能不同（固定 fixture 上验证观测逐字节相同）。更大的 body 边下边收，不扣（上面"体积分档"）。
- `pinned` 不改 `performance.now` 与定时器；没有 `Last-Modified` 头时，`document.lastModified` 是真实时钟（S2a）；不带参数的 `Intl.DateTimeFormat().format()` 也读真实时钟，不经过 shim 的 `Date`（M29 实测）。
- 时区与语言的 override 是渲染进程级的：同进程的第二个页面（`window.open` 的弹窗）上再设一次，Chrome 回 "already in effect"。值已由第一个 session 设好，记为该 target 的命令失败（`RecorderConnection.failures`），不致命。
- 长连接的响应（SSE、长轮询）在 Response 阶段被扣到 `body_read_timeout_s`，之后记为 `incomplete` 放行；没有 `Content-Length` 的 body 不论多大都内联读（分档只看声明的长度），解码后超过 64 MiB 的读完记 `too_large`。
- `serialized_dom` 里的元素标记是 Chrome 的 backend node id，两次独立启动同一页面时会因加载时序不同而不同，归档**关着**两边也一样（实测：fixture 的进程外 iframe 元素一半启动是 `[13]`、一半是 `[14]`；开着归档时固定是 `[14]`）。所以"开关归档观测相同"的验收按出现顺序重编号这些标记后比较，其余每个字节（结构、文本、属性、截图）都必须相同。
- **回放的残余**（M30）：
  - 隔离原先留着的 `EXCLUDE localhost` 口子已经关上（2026-10-13，上面"黑洞没有例外"）：隔离回放里绕过 Fetch 的流量连哪儿都连不上，`localhost` 也一样。
  - WebSocket 不回放（v1 记为 miss）；嵌套 dedicated worker 的脚本送不出（看得见的记 `unservable`）；超过 `max_fulfill_body_bytes` 的 body 不送（loopback blob 服务器推迟）。
  - JS 时钟固定在录制时刻（`base_time: from_archive`）时，页面 JS 设的绝对过期 cookie 可能被 Chrome 丢掉；没有 `Last-Modified` 的文档，`document.lastModified` 读真实时钟。
  - 回放 `none` 档位的录制：时区、语言、DPR 不比较；SW 录制时放行、回放时被拦住；只有 pinned 关掉了 `HttpsUpgrades`，所以升级试探可能显示为主 frame 文档 miss。
  - L2 模糊命中可能送错资源（`fuzzy` 与得分照记）；`exhausted` 重复最后一条是有意的，日志里看得见。
  - 步归因是"收集取走它的那一步"：页面加载完之后才发出的异步请求（`fetch`、XHR、迟到的 beacon）可能落在下一步的 `replay.jsonl` 里，与录制时 `index.jsonl` 的归因是同一个模型（fixture 实测：金丝雀页面在第 0 步导航，它的 `fetch` 在第 1 步的日志里）。
  - 回放日志导出成 HAR 推迟（还没有按 HAR 打分的评测器）。
  - 第一次 live run（10 个真实 `online_mind2web` 任务上的度量）是度量链只跑录制、原路径回放与报告的那一种（§4.16）；它的链规格与命令在本地的 `tmp/flywheel/M33_live_plan.md`，等用户授权（路由、N、token 上限），没有跑过。M30 写的 `tmp/flywheel/s2b/` 脚本已被它取代。
- 本节的 Chrome 行为都是 153 的实测，归档器的部分是 155 的实测（本机 Chrome 在 M29 期间升到了 155，153 时写下的金丝雀全部照样通过）；`requires_chrome` 的回归测试是版本金丝雀。

### 4.15 训练导出（M32）

导出是离线读 `RunTrace` 的消费者，地位与 evaluator 相同：不 setup 任何环境、不调任何 LLM、不写源 trace，产物是一个数据集目录。**学生就是一个普通的 af agent**：样本由学生 agent 自己的 `prepare` / `build_react_prompt` / memory 策略渲染、目标轮由它的 `format_react_output` 写出，所以训练格式就是服务格式，训练完的学生接回 af、用同一份运行 YAML 评测。新的学生格式 = 新的 Agent 类（v1 的学生是 `ui_tars` + `coordinate_space: normalized_1000`，`configs/flywheel/ui_tars_student.yaml`）。

**学生一侧是暂定的，导出冻结（用户 2026-10-13 的裁决）。** 这一期交付的是 teacher 轨迹：录制、回放、分叉。学生一侧（消息怎么组装、"学生看截图、输出坐标"）挪到下一期再定。导出包（`spectragent/export/`、`spectragent-export`、`configs/flywheel/export_sft.yaml`、`configs/flywheel/ui_tars_student.yaml`）冻结：不加功能，不做新的设计决定；只为横切的改动（改名、manifest 与台账的变化、null 的规则）跟着调整，让它的测试保持全绿。本节写的是冻结时的形状。

#### 配置：运行根的两个可选层

导出不另立根（§4.2.1 实测）：`ExporterRegistrar`（`exporter_id`，`spectragent/export/_registrar.py`）与 `ActionTranslatorRegistrar`（`translator_id`，`spectragent/export/translate/_registrar.py`）进 `_collect_registrars()`，是运行根的两个可选顶层层。导出用的 YAML 有一个 `exporter:` 块（`configs/flywheel/export_sft.yaml`），可选地再有运行根自己的 `eval_loader:` / `task_loader:` 块，去污染与泄漏扫描经它们读基准数据（见"基准数据"）：

```yaml
exporter:
  - exporter_id: openai_messages
    sample: per_step                 # v1 唯一值
    image_store: files               # v1 唯一值：图片按 sha256 存成文件
    sources_root: traces             # sources 的根（null：工作目录下的 traces）
    sources: ["recorded/*"]          # 相对根的 glob，按路径排序（null：["*"]）
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
    length: {max_tokens: 65536}      # image_tokens: qwen_vl（默认）| fixed（配 fixed_image_tokens）；null：不检查长度
    output_dir: datasets/sft_v1
# 可选：基准数据从这里读（旋钮俱全）；没配的 benchmark 用它注册的加载器的默认值
# eval_loader:
#   - {benchmark_name: online_mind2web, subset: All, data_dir: ~/agent-data/browser/online_mind2web}
# task_loader:
#   - {benchmark_name: online_mind2web, subset: All, data_dir: ~/agent-data/browser/online_mind2web}
```

`length` 与学生要一致：学生每轮都留截图（`memory: simple`），样本长度随段增长：发布的学生 30 步的整段在 1280×720 上光图像就约 36k token（`qwen_vl`），所以发布的预算是 65536；训练器的上下文更短时要换步数更少的学生，否则导出按名字失败（`test_validate.py` 用发布的两份配置渲染一整段核对）。

- **`ExporterBase`**（不是 dataclass，手写关键字 `__init__`，与 `trace_branches` 同一写法）：上面每个任务字段都是 `__init__` 的 `Annotated` 参数：`sources_root: str | None`、`sources: list[str] | None`、`filters: ExportFilters | None`、`split: SplitSettings | None`、`decontamination: Decontamination | None`、`student_run_config: str`（必填）、`thought_source: Literal["teacher", "none"]`、`thought_index_refs: Literal["drop", "keep"]`、`length: LengthBudget | None`、`output_dir: str`（必填），以及 v1 所有导出器共有的 `sample: Literal["per_step"]`、`image_store: Literal["files"]`。列表默认写 `None`、表示文档写明的默认值（conscribe 1.3：非空列表默认是共享可变默认、`tuple[str, ...]` 生成不了）。嵌套旋钮是 pydantic dataclass，各在 `spectragent/export/settings.py`（`ExportFilters`、`SplitSettings`、`Decontamination`、`BenchmarkRef`、`LengthBudget`）。带受体的类的存根把同模块兄弟类型写成 `Any`，`BaseModel` 会被代码生成当成它自己生成的子模型（af-add-a-receptor §7）；`require_eval` 复用 `core/data_loader/task/require_eval.py::RequireEval`（M31 的同一个类型、同一个语义）。`translator` 是单实例受体：`__wiring__ = {"translator": "action_translator"}`、类级注解、Python 默认 `default_translator()`（`identity`）。自己没有旋钮的导出器不写 `__init__`（conscribe 读到的就是基类的签名）；有自己旋钮的导出器要写出完整签名。`af-regenerate-and-verify` 的检查 1 证明任务字段与 `translator: Union[...]` 都在生成的每键配置上（`tests/test_export/test_registrar.py`）。
- **`Exporter` Protocol**（`_registrar.py`）：`exporter_id`、只读的 `translator`、上面的任务字段、`root()`（sources 根的绝对路径）与 `format(rendered: RenderedSample, images: ImageStore) -> ExportRecord`。`ImageStore.put(data, media_type) -> <数据集里的相对路径>`、`ExportRecord{sample_id, record, images}` 也在 `_registrar.py`，`RenderedSample` 在 `render.py`（它不 import `_registrar`）。Protocol 里的 `translator` 写成只读 property：生成的存根把受体类型写成 `ActionTranslatorBase`，可变属性在 Protocol 里是不变型的。
- **翻译器旋钮**：`ActionTranslatorBase.untranslatable: Literal["drop_step", "truncate", "keep_as_text", "fail"] = "drop_step"`；`index_to_coordinate` 另有 `occlusion: Literal["none", "check"] = "check"`、`occlusion_unknown: Literal["raise", "untranslatable"] = "raise"`。翻译器没有受体，旋钮是手写 `__init__` 的关键字参数。
- **CLI**：`spectragent-export --config <yaml>`（`spectragent/export/cli.py`，`pyproject` 的 `[project.scripts]`）：用 `ConfigComposer(registrars=_collect_registrars())` 组装，`ExportRunner.from_run(run, ...)` 取恰好一个 `exporter` 实例（否则 `ExportConfigError`）与文档里的加载器（`BenchmarkLoaders.from_run`），跑它，打印每个切分的样本数与按原因的丢弃数；失败非零退出。`/datasets/` 在 `.gitignore` 里。

#### 流水线（`ExportRunner`，`spectragent/export/runner.py`）

不注册（与 `EvaluationRunner` 对应），同步执行，没有 LLM，没有环境：

1. **源**：`sources_root` 下按 `sources` glob 匹配的 run 目录（`core/trace/sources.py::match_runs`，从 `trace_branches` 挪过来的那一份，加载器改用它），按相对路径排序；只经 `RunTrace` 读已提交的部分。源级检查与筛选（见下）。
2. **canonical 化**：每个可导出的步 → 一个 `CanonicalStepSample`（`schemas/sample.py`）。
3. **翻译**：`exporter.translator.translate(sample, trace)`：与学生无关的动作空间换算。
4. **学生渲染**：组装学生、只装配；按段重建上下文；目标轮来自 `format_react_output`；逐轮 round-trip。
5. **格式化**：`exporter.format(rendered, images)` → 每个样本一条记录。
6. **校验**：图片个数、坐标范围、泄漏扫描、凭证扫描、长度。
7. **切分、重复标记、数据集 manifest**，然后落盘。

整个产物先写进 `<output_dir>.partial/`，所有校验通过才原子改名成 `output_dir`；任何一步失败（含校验）都删掉 `.partial`、不留半个数据集（fail closed）。导出出零个样本是 `ExportEmptyError`，列出每个筛选与丢弃原因的计数。

#### 源级检查与筛选

- **不可导出的源**（跳过，原因记进 manifest 的 `sources.skipped`）：读不回来（`TraceSchemaError`；原因只写相对 run 目录的文件与错误类，如 `not a readable trace: manifest.json does not read back (TraceSchemaError)`；读取器的消息带绝对路径、可能引用读不出的字节，只进日志，manifest 因此与 sources 根在哪无关；比当前格式旧的 trace 也是这样读不回来，§5.5）、trace 从没关闭（manifest 还是 `writing`：trace 自己那本账的硬条件，同 `trace_branches`，§4.10）、`TraceConfig.secrets_recorded`（带原样凭证的 trace 不进数据集）、没有记录协商（`TraceConfig.negotiation is None`，手工组装的 run）、与另一个源共用 `run_id`（共用者全部跳过，同 `trace_branches`）。
- **完整到哪一步是硬条件**：已关闭的源只导出它完整到的那些步（`complete_through` 之前、含它；`canonicalise` 只取这些），所以一个出洞停下的 run 导出洞之前的步，尾部失败的 run 一步不少（用户 2026-10-13 的裁决，与分叉同一条规则）。
- **筛选**（`filters`，原因记进 `sources.filtered`）：`agent_outcome`（agent 怎么结束，读源 run 自己的 `result.json`，默认 `any`；没有 `result.json` 的源过不了设了值的筛选）；`require_eval`（可选，默认 `null`、发布的配置也是 `null`：不按判定筛，teacher 失败的轨迹一样导出，每个样本照样带 `labels.step_status` 与判定标签，训练时可以再筛；设了时是 `RequireEval.satisfied_by(RunTrace.evals())`，**分叉 run 用它的源 run 的判定**，因为分叉 run 在 v1 没有评测加载器；源 run 按 `TraceConfig.branch.source_run_id` 在本次匹配到的源里找，找不到就筛掉，原因 `branch source not among the sources`）；`exclude_restoration_diverged`（默认 `true`：任何一条 `StepRecord.restoration` 没通过（`verdict` 不是 `ok`）的 run 整个排除：`warn` 下发散后照常走完的分叉，它的策略步决策时的状态不是源 run 的前缀，上下文因此是错的，这是只有这个筛选挡得住的一类；`diverged` 是 `strict` 中止的分叉，它在策略步之前就以 `error` 结束，本来就没有可导出的步）；`max_document_misses`（回放日志里的主 frame 文档 miss 总数超过它就排除，计数规则与 `ReplayLog.diverged_total` 相同，折叠进 `summary` 的也算；读侧是 browser 的 `replay/replay_log.py::document_miss_total(reader)`；没有回放日志的 run 是 0）；`exclude_sites`（站点键命中列表：主机名相等或以 `.` + 条目结尾）。分叉 run 的源 run 不在本次可读的源里时，不论有没有 `require_eval` 都筛掉（`branch source '<id>' not among the sources`）：它的上下文就是源 run 的前缀。读筛选要用的文件读不回来（`TraceSchemaError`）算跳过，不算筛掉（原因的写法同上）。

#### 可导出的步与 `CanonicalStepSample`

**可导出的步**：`origin == "policy"`、`error_phase` 为空、至少执行了一个动作（`zip(actions, action_results)` 的那些；解析失败的步与零动作的步在 `StepRecord` 上分不开，M31 的未决问题，两者都不导出）。分叉 run 只有它的策略步可导出（检查点步 t 与其后的继续步）；恢复步、终止步从不是样本。不可导出的步不产生样本，并在轨迹里结束当前段（"学生渲染"）。

`CanonicalStepSample`（`spectragent/schemas/sample.py`，领域中立：不出现 URL、DOM、bbox 这类字段名，`test_shared_module_is_domain_neutral.py` 覆盖到它；不 import `spectragent.core`）：

- **身份与来源**：`sample_id`（`<run_id>/s<step>`）、`run_id`、`source_trace`（相对 `sources_root` 的 POSIX 路径）、`step`、`benchmark` / `subset` / `task_id`、teacher 的 `agent_id` / `loop_id` / `llm_model`、`config_hash`。
- **任务**：`task`（学生拿到的指令，`TraceConfig.task`）。参考答案与评测提示**不在样本里**：泄漏扫描按需从源 benchmark 的评测加载器取，从不进样本、从不渲染。
- **观测引用**：`observations: list[FragmentRef]`（每个录下的 env 来源 fragment：全限定键、manifest 里的文件路径、产物的 sha256、`failed`）；`image: ImageRef | None`（满足 `ScreenshotGeometry` 的那个 fragment：sha256、媒体类型、`image_*`、`viewport_*`）。
- **历史引用**：`history_steps: list[int]`（同一轨迹里在它之前的可导出步；分叉 run 的是源 run 的 0..t-1 加分叉自己在它之前的策略步）。
- **目标**：`teacher_actions`（执行过的动作，全限定名；参数投影到该动作 `Params` 声明的字段，未声明的键丢掉并在 `dropped_params` 里点名，例如 Skyvern 写进每个动作参数的 `reasoning`）；`thought: ThoughtRecord(text, source_field) | None`（`thought_source: teacher` 时经 teacher 的 `parse_react_thought`，§4.1）；翻译之后的 `actions`、`untranslatable: Untranslatable(reason, detail) | None` 与 `translation: list[TranslationRecord(translator_id, action_index, payload)]`（翻译器的来源记录，内容由翻译器定，对样本是不透明的 JSON）。
- **标签**：`labels.step_status`（`ok`：每个执行的动作都成功；否则 `action_failed`，照样导出、标出）、`labels.loss_weight`（目标轮 1.0）、`labels.verdicts`（该 run 的每份 `graded` 判定：`verdict_slug`、`evaluator_id`、`predicted_label`，分叉 run 用源 run 的）。
- **脱敏台账**：`redactions`，源 `TraceConfig.task_setup` 台账的条目照抄（策略版本、位置、字段、规则），让读者知道源 run 的哪些输入被换过。
- **分叉元数据**：`branch: BranchSampleMeta | None`，含 `branch_group_id`（`<source_run_id>/s<t>`）、`source_run_id`、`checkpoint_step`、`branch_index`、终止观测的引用（`terminal_step`、`terminal_image_sha256`，没有终止步时为 `None`）、`duplicate_of`。

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
- **`index_to_coordinate`**：带元素 index 的动作（参数上带 `RequiresFragment("browser.dom")` 且这一次给了值的那些，`core/actions/params.py::param_fragments`）换成坐标动作：`browser.click` → `browser.click_coordinate`、`browser.type` → `browser.type_coordinate`（`text`、`clear` 照搬），其余带了 index 的（`select_dropdown`、带 `index` 的 `scroll` / `extract_content`）是 `no_counterpart`；不带 index 的动作原样。index 在录下的 `selector_map` 里找不到（源 run 的这个动作当时就找不到目标）也是 `no_counterpart`。换算的 browser 知识在 `core/capabilities/browser/index_points.py`（纯函数，读交进来的 fragment 与文档，不 import `core.trace`），翻译器只组织调用：
  - **几何**：录下的条目（M27 起自包含）的 `bounds` 是顶层文档的页面坐标（CSS px，已计入 iframe 偏移，与 DPR 无关）；减去 `DOMState.scroll_info` 的滚动得到视口坐标；与视口求交；条目在重链接的 `element_tree.dom_tree` 里找得到时，再与每一层 frame 宿主的 `bounds` 求交（被深度截断而找不到时只裁视口，来源记录里写 `frame_clip: "unknown"`）；交集为空是 `target_offscreen`（live 的 index 点击会先滚动到可见，录下的截图上表达不了）；否则取可见部分的中心，四舍五入成整数 CSS px（坐标动作的参数是 `int`）。变换过的 iframe 只按平移处理（已知限制）。
  - **遮挡**（`occlusion: check`，只对换算出的点）：证据按优先级：(a) 这一步的 `selector_map` 条目带 `hit_test`（`skyvern_hit_test` 录的）：中心命中用中心，否则用 `visible_point`，两者都没有就是 `occluded`；(b) 这一步的 `browser.dom_raw` 文档（`RunTrace.archive_files(step, "browser.dom_raw")`，`archive/dom_raw.py::read_dom_document` 解开；按文档戳挑与本步同一个文档的那份）里的 DOMSnapshot：在目标所在的主文档里，包含该点（快照 `bounds` 是设备像素，除以文档里记的 DPR）且 paint order 最大的布局节点必须是目标或它的后代（`pointer-events: none` 与 `visibility: hidden` 的节点不算，点击穿过它们；并列的 paint order 算目标的），v1 只判主文档里的目标，frame 里的目标这一条证据不适用；(c) `browser_use` 清洗过、trace 里带 `paint_order` 的 `dom_tree`：同一个查询，只在目标所在文档的、留下来的节点里做（BrowserUse cleaner 已经把整个被盖住的元素去掉了，所以这一条只抓得到部分遮挡）。(b)(c) 判定被盖住时，在可见部分里按 5×5 内点网格（内缩 2 px、行优先）找第一个不被盖住的点；找不到是 `occluded`。三条证据都没有：`occlusion_unknown: raise`（默认）抛 `OcclusionEvidenceMissingError`，点名样本与 index，并提示"绑定 `browser.dom_raw` 或用 cleaner `skyvern_hit_test` 录制"；`untranslatable` 时按不可译原因 `occlusion_unknown` 处理。`occlusion: none` 不查（原名 `off`，用户 2026-10-13 的裁决改名）。
  - **来源记录**：元素引用（`element_index`、`backend_node_id`、`target_id`）、页面 bbox、视口里的可见矩形、点的来源（`center` / `hit_test` / `grid`）、遮挡判定（`clear` / `occluded` / `unknown`）与所用证据（`hit_test` / `dom_raw` / `paint_order` / `none`）。

#### 学生渲染（`spectragent/export/render.py`）

- **组装**：`ConfigComposer` 组装 `student_run_config`（一份普通运行 YAML，不带 hook）；它必须恰好有一个 agent，`AgentRuntime.for_agent(run, <它>).prepare()`，用同一套协商、带 `fragments=` 收窄的 descriptors、绑定表与 weave（§4.2）；学生的 Loop 必须是 `ReactLoop`（含子类），学生配置不得带 hook（学生只装配、从不运行），否则 `ExportConfigError`。然后查导出用的可选 slot（§4.1）：学生没有 `format_react_output`、或 `thought_source: teacher` 而某个源的 teacher 类没有 `parse_react_thought`，抛 `IncompatibleError(..., needed_for="needed for training export ...")`。种子消息只经 `runtime.seed_messages(task)`。从不调 `execute()`、`setup_environment()` 或 LLM。
- **片段与采集检查**：学生协商出的 env 来源 fragment 必须 ⊆ 源 run 录下的（`TraceConfig.negotiation.env_sourced_fragments`），否则 `IncompatibleFragmentError`（`provider=` 措辞参数写成源 run，点名缺的 fragment，比如学生要 DOM 而源 run 是纯视觉；从原始归档重渲染是以后的事）。每个取样的步把录下的 env 来源 fragment 交给学生环境的 `compare_capture`（§4.3、§4.9）：有 `mismatches` 抛 `StudentCaptureMismatchError(source, mismatches)`，`unchecked` 记进 manifest。
- **一个轨迹、若干段**：轨迹是一个源 run（分叉 run：源 run 的 0..t-1 加分叉自己的策略步）。段从一个可渲染的步开始：学生的 memory（学生自己的策略实例）清空、写入 `seed_messages(task)`。段内每一步 k：观测 = 学生协商出的 `NegotiatedObservation`，env 来源 fragment 取 `RunTrace.observation_fragment(k, key)`，loop 来源 fragment 由 `loop_fragment_builders(...)`（§4.1）建：`loop.step_info` 的 `step` 是 k+1，`max_steps` / `today` 取源 run 第 k 步录下的 `loop.step_info`（学生 Loop 的 `today_override` 优先；源 run 没录 step_info 时 `today` 取源 manifest 的 `created_at` 的 UTC 日期、`max_steps` 取学生 Loop 的，manifest 记下用了哪个来源），`loop.history` 是这一段里之前的步（源 `StepRecord` 的副本，`actions` 换成翻译后的；`keep_as_text` 的步换成它记成文本的那些动作），`loop.action_result` 取这一段里的上一步（段首没有上一步）；env 来源 fragment 经 `core/loops/restore/source.py::recorded_observation` 读（与 `BranchLoop` 的 `SourceRun.observation` 同一个读法），录下的 step_info 经 `recorded_step_info` 读、按 `_loop_fragments.py::pinned_step_info` 钉住（与 `BranchLoop` 同一条规则）；`build_react_prompt(obs)` 写进 memory；k 是样本时此刻的 `memory.get_context_messages()` 就是它的上下文；然后 `format_react_output(<k 的翻译后动作>, thought=<k 的 thought>)`、立即 `parse_react_output` 那段文本（round-trip，也让 Skyvern 的动作历史这类 agent 自己的状态与服务时一样前进），文本经 `ReactLoop._remember_reply(memory, text, [])` 写进 memory。较早的 assistant 轮 weight 0，目标轮 weight 1。
- **缺学生要的 fragment 的步**：某一步没录下学生要的 env 来源 fragment，或在学生必需（`required_fragments`）的 fragment 上录下的是采集失败（teacher 那边它是可选的）：这一步不渲染、结束当前段，manifest 记 `steps.skipped.fragment_unavailable`。
- **学生写不写得出来**：动作名经 `resolve_action_name(name, <学生守卫过的动作名>, <学生的 action_bindings>)` 解析不出来 → `no_counterpart`；一步的动作数超过学生 Loop 的 `max_actions_per_step` → `exceeds_action_budget`；`format_react_output` 抛 `UnrepresentableActionError` → `unrepresentable`。
- **thought**：`thought_source: none` 时目标轮与历史轮都没有 thought。`thought_index_refs: drop`（默认）且学生协商出的 fragment 不含源 run 的 index 参数所依赖的 fragment（`param_fragments` 给出的那些，browser 上就是 `browser.dom`）时，含 `[123]` 这类 index 记号的 thought 被丢掉（样本保留、只是没有 thought），manifest 计数。
- **不可译的步**（翻译器给的或渲染时判的）按翻译器的 `untranslatable` 处理。`drop_step`：这一步不是样本，当前段结束，下一个可渲染的步开新段（重新播种的上下文，同一个任务）；`truncate`：这条轨迹在这一步结束；`keep_as_text`：这一步不是样本，但它的观测轮照常进上下文，assistant 轮写成 `[not expressible: <reason>] <动作的 JSON>`（键排序），段不断；`fail`：抛 `UntranslatableActionError(sample_id, reason, detail)`。每个原因的丢弃数进 manifest（M33 的输入）。不可导出的步（上面）总是结束当前段。
- **identity 导出的期望**：对 UI-TARS 录下的 run 用 `identity` 与同一份学生配置导出，第 k 个样本的上下文与源 run 第 k 步第 0 次调用的 `messages` 在 trace 的记录形态下（`core/loops/restore/source.py::recorded_form`）逐条相同，assistant 轮按解析结果相同；预期的差异只有：源 run 的解析重问轮与截断提示（学生上下文里从不写它们）、跳过或丢弃的步造成的分段、以及 assistant 轮的书写形式（学生的规范写法 vs 模型原文）。

#### round-trip（`parse(format(a)) == a` 做不到，按下面的定义）

实测：`normalized_1000` 在 1280 px 宽的视口上有 280/1280（22%）个 x 值回不到自己（误差至多 1 px）；agent 解析出的是相对名，trace 里是全限定名；有些参数没有写法。对一个目标（翻译后、全限定名）动作 a，round-trip 成立当且仅当：

1. **名字**：`resolve_action_name(parse(format(a)).name, <学生守卫过的动作名>, <学生的绑定表>) == a.name`；
2. **不动点**：`format(parse(format(a))) == format(a)`（文本相等，thought 在内）；
3. **点**：每个点参数 `|parse(format(a)) − a| ≤ 学生网格的 quantum`（`PointGrid.quantum`，见下；网格是 `point_grid(<学生 agent 的 coordinate_space，没有时 screenshot_pixels>, sample.image)`）。哪些参数是点由 `core/actions/params.py::point_params` 认：动作 `Params` 里成对的整型 `<name>_x` / `<name>_y`（`coordinate_x/y`、`start_x/y`、`end_x/y`）；
4. **其余参数**：a 设了非 `None` 值的每个参数都原样回来；a 没设的参数由学生自己填（例如 UI-TARS 的 `finished` 总写 `success=True`，执行时与 `None` 等价）。写不出来的参数在 `format` 里就抛 `UnrepresentableActionError`，从不静默丢掉（UI-TARS：`type` 的 `clear`、`scroll` 的 `amount` / `index`、`wait` 的 `seconds`、`done` 的 `success=False`、含空白的 `send_keys` 组合键）。

**训练标签是量化后的点**（目标轮文本里写的网格点；它在服务时执行成 `parse(format(a))` 的那个视口点）。每个点的量化误差（像素，按值分桶）进 manifest。`PointGrid.to_grid(vx, vy) -> tuple[int, int]`（`schemas/observation/geometry.py`：`round(v × grid / viewport)`，夹到 `[0, width] × [0, height]`）是 `to_viewport` 的逆；`PointGrid.quantum: int` 是一次 `to_grid` → `to_viewport` 在任一轴上可能带来的最大视口误差：网格不比视口粗（`viewport / grid ≤ 1`）时为 0，否则 `floor(viewport / grid / 2 + 1/2)`（1280 px 上的 `normalized_1000` 是 1；整数 DPR 与 1.5 的 `screenshot_pixels`、1000 px 视口是 0）。round-trip 对上下文里每一个 `format_react_output` 的结果都检查（目标轮与历史轮），失败是 `RoundTripError`，表示格式器有缺陷，不算不可译。

#### 格式化：`openai_messages`

- 每个样本一条记录（`<split>.jsonl`）：`{"id": <sample_id>, "messages": [...]}`；消息经 `core/llm/serializers/openai_format.py::OpenAIMessageSerializer` 序列化，assistant 消息带 `weight`（较早的轮 0，目标轮 1）。图片部分的 data URL 换成相对路径 `images/<sha256><后缀>`，同一张图在整个数据集里只存一份（`image_store: files`）。OpenAI 的自助微调在收缩，这只是交换格式；以后的 `llamafactory`（`<image>` 个数 = 图片个数）、`ms_swift`（`loss_scale`、`rejected_messages` 做 DPO）、`trl` 是新的导出器。
- 同序的 `<split>.samples.jsonl`：每行 `{"sample": <CanonicalStepSample>, "render": {split, segment, context_steps, step_info, target, target_actions, quantization_px, thought, thought_dropped, images}}`。`target_actions` 是目标轮文本执行出的动作（量化后的点：训练标签），`duplicate_of` 在样本的 `branch` 里；这个文件是与格式无关的那一半。
- **数据集 manifest**（`manifest.json`）：写与读经同一个类型化模型 `spectragent/schemas/dataset.py::DatasetManifest`（导出器写、度量报告读；改了名或类型的键在读的时候点名失败，不会读成 0）。数据集格式版本 `af-sft-2`（2：去污染只认明确的列表、泄漏扫描只数比过的样本）、导出器与翻译器的配置、学生配置的摘要（学生 `top_level_cfg` 的规范 JSON 的 sha256）与学生的 agent / loop / `coordinate_space`、每个源（相对路径、`run_id`、`config_hash`、样本数）、`sources.skipped` / `sources.filtered`（带原因）、按原因的丢弃与跳过、量化误差分布、thought 的来源字段计数与丢弃数、`step_info` 的来源计数、采集检查的 `unchecked`、切分（每个切分的样本数、每个切分键分到哪里）、去污染与泄漏扫描的结果、重复数。键名：`format`、`exporter`、`translator`、`student{config_sha256, agent_id, loop_id, coordinate_space}`、`sources{exported, skipped, filtered}`、`steps{untranslatable, skipped}`、`samples`、`quantization_px`、`thoughts{source, fields, dropped_index_refs}`、`step_info`、`capture{unchecked}`、`split{by, validation_fraction, salt, samples, keys}`、`decontamination{decontaminated, against, match, excluded}`、`leak_scan{compared, no_eval_loader, no_reference_answer}`、`duplicates`。**不含任何墙钟字段、绝对路径或主机信息**：路径类的旋钮（`sources_root`、`student_run_config`、`output_dir`）不进 manifest，学生配置只记摘要，跳过原因不带读取器消息（见"源级检查与筛选"）。
- **环境证据从不导出**：`env/` 只为遮挡证据与文档 miss 计数被读；输出目录里没有任何文件来自 `env/`（测试按内容哈希核对）。

#### 校验（`spectragent/export/validate.py`）

round-trip 在渲染里做（它要 agent 本步的状态），其余的在格式化之后、落盘之前，任何一条失败都让整个导出失败：

- **图片个数**：每条记录的图片部分数 = 渲染出的上下文里的图片数，每个引用的文件都在 `images/` 里。
- **坐标**：每个点参数在本步截图的视口内（`[0, viewport)`），学生网格点在网格内，执行点在图像内。
- **泄漏扫描**：源 benchmark（分叉 run 用源 benchmark）的评测加载器给出这个任务的 `expected_answer`：来自导出文档为它配的 `eval_loader`（按文档顺序，第一个有这个任务的），没配时来自它注册的评测加载器按源 run 的 `subset` 默认构造的实例（"基准数据"）；规范化（casefold、空白折叠）后长度 ≥ 8 时，它不得出现在导出流水线自己放进 prompt 的文本里（种子 system 消息与任务文本），否则 `ReferenceLeakError` 点名样本与字段。观测本身（页面上本来就有答案）是世界，不算泄漏。manifest 只把真的比过的样本记成 `leak_scan.compared`（"没比"不能读成"查过"）；没比的按原因分开记：既没配、也没注册评测加载器的 benchmark 记 `leak_scan.no_eval_loader`，参考里没有答案、或答案不到 8 个字符的记 `leak_scan.no_reference_answer`（online_mind2web 的 300 条记录都没有 `expected_answer`，所以它的样本全在这里）；加载器读不出来、或没有这个任务的参考，是错误。
- **凭证扫描**：`core/trace/secret_patterns.py::SECRET_PATTERNS`（从 `test_secret_guard.py` 挪过来，secret guard 测试与这里共用一份）扫描要写出的每一个文本字节（记录、样本、manifest），命中即 `CredentialLeakError`（点名文件、样本与模式标签），fail closed。它认的是凭证的形状，不是 PII（残余见"隐私"）。
- **长度**：`length` 给了时用可插拔的 `LengthEstimator` Protocol 估算每条记录的长度，v1 的 `ApproxTokenEstimator`：文本字符数 / `chars_per_token`（默认 4），每张图按 `image_tokens` 取 `qwen_vl`（`ceil(w/28) × ceil(h/28) + 2`，按图像像素）或 `fixed`（`fixed_image_tokens`）；不依赖 tokenizer、不联网。超过 `max_tokens` 抛 `SampleTooLongError` 点名样本，从不截断。新的估算器（真 tokenizer）有第二个实现时再成注册表。

#### 切分与去污染

- **按站点或任务切分，从不按步**：`split.by: site` 的键是源任务 `start_url` 的主机名（`TraceConfig.task_setup.context["start_url"]`，M28 记下的、值级脱敏过的 URL，主机名不受脱敏影响），`task` 的键是 `<benchmark>/<subset>/<task_id>`；分叉 run 用它的源任务（`TraceConfig.branch` 里的 `source_*`），所以同一个 `branch_group_id`、同一个源任务的样本总在同一个切分里。切分 = `sha256(salt + "\0" + key)` 的前 8 字节作为 [0, 1) 的数小于 `validation_fraction` 进 `validation`，否则 `train`。算不出键的 run（没有 `start_url`）跳过，原因 `no split key`。
- **去污染**：`decontamination.against` 列出学生将来评测用的 benchmark（`BenchmarkRef{benchmark, subset}`），只认这份明确的列表，从不从评测器或判定推断（用户 2026-10-13 的裁决）。默认 `[]`：不去污染，CLI 记一条 INFO，manifest 记 `decontamination.decontaminated: false`；`null` 不是列表，组装时就报错。`match`：`task_id`（样本的源任务 id 在该 benchmark 评测加载器的任务里）、`site_instruction`（源任务的 start_url 主机 + 规范化指令与该 benchmark 任务加载器里某个任务相同）、`either`（默认）。命中的源整个排除，原因 `decontaminated`。加载器读不出来、没注册也没配是 `ExportConfigError`（去污染从不静默地什么都没查）。
- **基准数据**（去污染与泄漏扫描共用，`spectragent/export/benchmarks.py::BenchmarkLoaders`）：导出文档自己的 `eval_loader:` / `task_loader:` 块（运行根的层，组装时旋钮俱全，如 `data_dir`、`subset`），按 `benchmark_name` 对上 benchmark、按文档顺序；文档没给某个 benchmark 配加载器时，才用它注册的加载器按默认值构造（`spectragent-bench` 解析评测加载器的同一条规则：配置优先，其次注册表）；配了的 benchmark 只经配的那些读，从不在配好的实例旁边再默认构造一个，那一个会无视文档配的旋钮（af-add-a-receptor §0 的那类缺陷）。去污染的引用写了 `subset` 时只读该 subset 的已配加载器（配了这个 benchmark 却没有这个 subset 是 `ExportConfigError`，两处设置不一致不猜），`subset: null` 读这个 benchmark 的全部已配加载器（没配时是注册加载器的默认 subset）。所以为泄漏扫描另配了训练 subset 的评测加载器时，`against` 里要写明评测的 subset。泄漏扫描在源 benchmark 的全部已配评测加载器里找这个任务。列表的后果写明：把录制所用的 benchmark 本身列进 `against`，会把这些源 run 全排除（导出因此 `ExportEmptyError`）；要训练就列一个不相交的评测集，或者不列。

#### 分叉样本（默认：中立记录）

每个分叉 run 导出它的策略步，上下文的历史来自它的源 run（按 `source_run_id` 在本次的源里找到，同样的翻译、同样的段规则），第 t 步的观测是分叉 run 自己录下的（经恢复校验的 live obs_t）。元数据带 `branch_group_id`、终止观测的引用。同一个 `branch_group_id` 里检查点步的目标动作（翻译后的名字与参数，不比 thought）完全相同的样本，排序在后的标 `duplicate_of = <排在前的 sample_id>`，只标出，不删。怎么把分叉结果变成训练信号（judge 选正样本、DPO 对、world model）不在导出器里，v1 只交中立记录。

#### 隐私

- `env/` 从不导出；样本里没有原始 DOM，学生没协商 DOM 时 DOM 文本也进不了 prompt。
- `secrets_recorded` 的源不导出；凭证扫描 fail closed。
- **残余，写明**：凭证扫描认形状：teacher 键入一个不像凭证的密码（`type` 动作的 `text`）会原样成为训练目标；录制时键入动作的结果记下了 `typed_into_sensitive_field`（§4.14 隐私），导出怎么用它随学生侧一起推迟。BrowserUse 观测里的密码框已按敏感规则打码（§4.12）。v1 的学生是纯视觉的 UI-TARS。

#### 确定性

同样的输入导出两次，输出目录逐字节相同（整棵树的哈希）：源按相对路径排序、步按序号、图片按 sha256 命名、JSON 键排序且分隔符固定、`today` 取自 trace、没有墙钟字段、路径都是相对的、切分是盐 + 键的哈希。测试用不同的文件系统创建顺序建两遍源。

#### 代码位置

`spectragent/export/`：`_registrar.py`（`Exporter`、`ExporterRegistrar`、`ExporterBase`）、`settings.py`、`openai_messages.py`、`translate/`（`_registrar.py`、`identity.py`、`index_to_coordinate.py`）、`canonical.py`、`filters.py`、`split.py`、`benchmarks.py`、`render.py`、`validate.py`、`runner.py`、`cli.py`、`errors.py`；`schemas/sample.py`；`schemas/dataset.py`（`DatasetManifest`，与度量共用）；`schemas/observation/geometry.py`（`to_grid`、`quantum`）；`core/loops/errors.py`（`UnrepresentableActionError`）；`core/loops/_loop_fragments.py`（`loop_fragment_builders`、`pinned_step_info`）；`core/loops/restore/source.py`（`recorded_observation`、`recorded_step_info`：与分叉共用的读法）；`core/actions/params.py`（`point_params`）；`core/runtime.py`（`seed_messages`）；`core/trace/sources.py`、`core/trace/secret_patterns.py`；browser：`index_points.py`、`capture_check.py`、`dom/cleaning/skyvern_hit_test.py`、`replay/replay_log.py::document_miss_total`。

### 4.16 度量闸门（M33）

度量是离线读已提交数据的消费者，地位与 evaluator、训练导出相同：只经 `RunTrace` 的访问器读 trace（只读已提交的部分），只读数据集目录里的文件；不 setup 任何环境、不调任何 LLM、不碰任何运行中的状态，产物是一份报告。它算出数据线的每一项度量，交给 M33 决定的每个问题各成一节，每一节以一条带证据的建议收尾。包是 `spectragent/flywheel/`，与 `evaluator/`、`export/` 同级：`metrics.py`（度量）、`report.py`（报告模型、建议规则、Markdown）、`chain.py`（链）、`cli.py`（`spectragent-flywheel`）。**不是注册表**：v1 只有一份报告；browser 证据的格式知识在 browser 的包里，度量只组织调用（与 `index_points.py` 同一先例）。第二条数据线（Docker、Android）出现时，"每个 capability 贡献哪些度量"再成为一根轴（§6 与 AGENTS.md §2.2 同时加行）。

#### 链（`spectragent-flywheel run --spec <chain.yaml>`）

链是现有入口的顺序调用，不是新的运行路径：每一段在进程内调用该 CLI 的 `main(argv)`（`spectragent.core.benchmark_runner.main`、`spectragent.export.cli.main`），最后算报告。链规格是一份普通 YAML（pydantic `ChainSpec`，`extra="forbid"`）。它不是 conscribe 的运行根，所以不放在 `configs/*.yaml` / `configs/flywheel/*.yaml` 这一层（那里每个文件都要能组装并 prepare，`test_shipped_config_integration.py`）：

```yaml
name: first-live
root: traces/chains/first-live       # 一切产物都在它下面（traces/ 被 git 忽略）
evidence: live                       # live | rehearsal：报告里每条建议的证据基础
tasks: {benchmark: online_mind2web, subset: All, task_ids: [...], limit: null}   # 交给 record / control 段的 CLI 参数
record:    {config: <录制 YAML>, timeout: 600, evaluate: false}   # evaluate: false → --no-eval
control:   {config: <同档位、archive: [] 的录制 YAML>}              # 可选：归档开销的对照组
same_path: {config: configs/flywheel/same_path_ui_tars_archive.yaml}
branch:    {config: <分叉 YAML>}
export:    {config: <导出 YAML>, repeat: 2}                           # 2：导出确定性；1：不测
report:    {planted: [{label: teacher api key, env: OPENAI_API_KEY}]}   # 演练写 value；live 只写环境变量名
```

- **布局**（链的约定，报告只认它，`metrics.ChainLayout`）：`<root>/traces/{record,control,same_path,branch}/`、`<root>/datasets/{first,second}/`、`<root>/report/{report.json,report.md}`、`<root>/configs/<段>.yaml`（每段实际用的配置；导出两份：`export_first.yaml` / `export_second.yaml`）、`<root>/evals/<段>/`（`evaluate: true` 的段的 `--evals-dir`；判定也照常写进每个 trace 的 `evals/`）、`<root>/chain.json`（每段的 argv、退出码、产生的 run 目录与起止时刻；`--stages` 再跑时与已有的段合并；只是来历记录，报告不依赖它）。
- **路径从哪里算**：规格里的相对路径（`root`、每段的 `config`）相对工作目录，与每个 af CLI 读路径的方式一样（阶段 YAML 自己的相对路径，例如 `matcher.site_rules: configs/replay_rules`，也相对工作目录）；`root` 解析成绝对路径后，链写进阶段 YAML 的路径都是绝对的。
- **链只改路径**：每段的 YAML 原样读入，链只写这几个字段再落到 `<root>/configs/<段>.yaml`：唯一的 `trace_writer` 的 `trace_dir`（`<root>/traces/<段>`）；`same_path` / `branch` 唯一的 `trace_branches` 加载器的 `data_dir`（`<root>/traces/record`）；导出段唯一的导出器的 `sources_root`（`<root>/traces`）、`sources`（`["record/*", "branch/*"]`）与 `output_dir`（`<root>/datasets/first`，第二次 `second`）。缺这些块（或多于一个）在任何一段开跑之前报错、点名段与缺的块。其余一切（agent、档位、归档器、选步、分支数、`verify`、评测器）都写在段的 YAML 里；对照组的 YAML 自己写 `archive: []`。
- **段的成败**：`spectragent-bench` 退出码 0 或 1（个别任务的基础设施失败是要度量的数据）且这一段写出了至少一个新的 run 目录，算跑完；别的退出码（2：它的 pre-flight 拒绝了这一段的配置，一个任务也没跑；3：一个 guard 否决了这一段，被否决那一段的 run 不喂给下一段；§4.2）、`SystemExit`、异常、一个新 run 目录都没有，是这一段失败：链停下（`ChainStageError` 点名这一段），之前的产物留着。导出段退出码非 0 就失败（导出什么也没写）；报告段抛出的异常（缺陷、报告目录写不了；坏 run 从不让它抛，见下）同样是这一段失败（`ChainStageError` 点名 `report`，`chain.json` 记下）。选了导出段而 `<root>/datasets/<first|second>` 已经存在，是在任何一段开跑之前的 `ChainConfigError`（导出从不覆盖，要重导先删掉；不让前面花钱的段为一次必然被拒的导出白跑）。`--stages` 只跑列出的段（按规格的顺序；未知的段名、规格里没配的段在任何一段开跑之前报 `ChainConfigError`），已经跑完的段可以不重跑；重跑一个运行段会在已有的 run 旁边再加 run（记 WARNING）。
- **规格**：`ChainSpec{name, root, evidence, tasks{benchmark, subset, task_ids, limit}, record, control?, same_path?, branch?, export?, report{planted}}`，每段 `StageSpec{config, timeout, evaluate}`（`same_path` / `branch` 写 `evaluate: true` 被拒：`trace_branches` 没有评测加载器），导出段 `ExportStageSpec{config, repeat: 1|2}`，植入值 `PlantedSecret{label, value | env}`（恰好一个）。CLI：`spectragent-flywheel run --spec X [--stages a,b]`、`report --spec X [--out D]`、`report --root R --evidence live|rehearsal [--name N] [--out D]`（没有规格就没有植入值，只扫凭证形状）；退出码 0 完成、1 某段失败（`report` 算不出报告——发布的数据集读不回来——或写不了——`--out` 建不了、写不进——也是 1，报错点名是哪一个，不抛 traceback）、2 规格或参数不对。测试的接缝是 `run_chain(spec, bench=..., export=...)`（替换两个进程内的 `main`）。
- **原路径回放是一个分叉段**：`configs/flywheel/same_path_ui_tars_archive.yaml`：`branch` loop，选步 `last`（每个源 run 最后一个可分叉的策略步，§4.10），`continue_steps: 0`，`verify: warn`（发散了照样走完，每一步都有校验结果），`branches_per_step: 2`（回放确定性的重复），`agent_outcome: any`，`on_unbranchable: skip`。它取代 M30 `tests/_helpers/replay_driver.py`"把录下的模型输出喂回同一个 agent"作为度量手段：恢复器本来就逐步重放录下的动作、逐步校验观测（§4.14 "恢复"），只有最后一步要 teacher 再决定一次（每个分支一次 LLM 调用）。第一次 live run（真实任务上只跑录制、原路径回放与报告）就是这条链只选这几段的跑法。
- `spectragent-bench` 不读 `.env`，链也不读：跑之前在 shell 里导出密钥（`set -a; . ./.env; set +a`）。
- **链的导出 YAML** 用 `filters.require_eval: null`（链默认不评测，没有判定可筛）与 `decontamination: {against: []}`（源 run 本来就是被测 benchmark 的任务）：这两个现在就是导出的默认值，也是发布的 `export_sft.yaml` 的写法；`max_document_misses` 与 `exclude_restoration_diverged` 照常，被它们筛掉的分叉正是要计数的产出损失。

#### 度量（`metrics.py`）

每个数都带分母（`Ratio{numerator, denominator, value, reason}`）；分母为 0 时值是 `null` 加原因，从不是 0："没测到"不能变成"测到了 0"（`evaluator/summary.py` 的同一条规则）；一组空值的 `percentile` 直接报错，不返回 0。读不回来的 trace 跳过并按角色记原因（`core/trace/sources.py::unreadable_reason`，导出用的同一个函数：只写相对 run 目录的文件名与错误类），不让一个坏 trace 拖垮整份报告；某个 run 的证据读不回来（环境证据的坏行、WARC 记录对不上，提交了的截图缺失或为空，回放日志读不回来）只在读它的那几项里跳过并记原因（该项的 `skipped`，键是 run 相对角色目录的路径，跨角色的项用 `traces/<角色>/<run>`：例如截图读不回来的 run 只离开原路径保真的严格版与回放确定性，恢复器的判定照常计入），`report.md` 也逐条列出。录制的 `result.json` 读不回来（解析不了，或读不了：权限、I/O）记作 `(unreadable result)`，与从没写过的 `(no result)` 分开，不让报告崩。每项建议所依据的度量另记它的数据来自几个不同的站点（`sites`：任务 `start_url` 的主机，与下面"站点"一节同一个键；只数真正贡献了数据的 run，跳过的不算），建议的广度规则（下面"建议槽"）读它。

**空分叉**：分叉 run 从检查点起一个动作也没执行（`metrics.empty_branch`）——teacher 的模型调用失败（`model_call_failed`）、它的回复没有动作（`no_action`：解析失败与解析出零个动作在记录里一模一样：没有动作、没有 `error_phase`）、或者 teacher 决定之前 run 就结束了（`ended_before_decision`：恢复把它结束了，或检查点步的观测、prompt 失败）——说明不了 teacher 的选择会走到哪里。它不进分叉结果覆盖的占比与 miss 计数、不进按站点的分叉占比与记录覆盖决定的输入，按原因单独计数（`empty`）；重复分叉率不比较检查点步没有动作的分叉（`not_compared`：导出只给样本标 `duplicate_of`，没有动作的步不是样本）。用户 2026-10-13 的裁决：否则 teacher 路由越不稳，"`strict` 够用"的占比反而越高。

| 度量 | 读什么 | 定义 |
|---|---|---|
| 归档开销 | `record` 与 `control` 的 `StepRecord`；`record` 的 env/ extra 索引与 `index.jsonl` | **延迟**：策略步、从第 1 步起（第 0 步含环境 setup），每步环境侧时间 = `duration_ms − (llm_latency_ms 或 0)`（`duration_ms` 为 `None` 的步不计）；两组各自的 p50 / p95（最近秩的 `percentile`），开销 = 开 − 关。两组的档位（`TraceConfig.resolved` 里 capability 的 `replayability`）不同、或任一组为空时为 `null`。live 的两组走的不是同一条路径，比的是分布。**字节**：每个录制 run 的 env/ 去重前后（`RunTrace.committed_files` 里带 `archive` 的条目）——去重后 = 每个不同路径各计一次的 `size`；去重前 = 每一步（与收尾）的 extra 索引列出的每个 env 文件各计一次（内容寻址的文件被每个引用它的步各计一次），再加 revisit 记录替掉的完整记录长度（browser 的 `revisit_saving`）；按步数平均。与 M29 `ArchiveOverheadReport` 同一账法：`before == after + saved_by_revisit + saved_by_content_addressing` |
| body 完整率 | `record` 的 `index.jsonl`（`iter_index_lines`） | 每个 `record_id` 的最终状态（最后一行；`streamed_later` 看有没有它的 `kind: body` 行）：有 body（`ok`、流完 body 的 `streamed_later`）、不需要 body（`redirect`、`websocket`）、明确没有 body（`too_large`、`incomplete`；最后一行是 `pending` 的按 `incomplete` 读，§4.14）。完整率 = 有 body 或状态明确的记录 / 全部记录；另报有 body 率（分母去掉不需要 body 的）与按状态的分布（browser 的 `body_completeness`）。"服务器发了、归档里没有"只有 fixture 的访问日志能判：那是演练测试的断言，不是报告的度量 |
| 原路径回放保真（按站点） | `same_path` 的 `StepRecord.restoration`；源 run（`TraceConfig.branch.source_trace`，相对 `<root>/traces/record`）的截图 | 被校验的每一步（恢复步与检查点步）：恢复器判定 `ok` 的占比（URL、元素签名多重集、截图 dHash ≤ 阈值、无文档 miss，§4.14 "恢复"）；严格版：URL 相等且截图 sha256 与源 run 同一步相同的占比；每个 run 第一个不通过的步与原因；全部通过的 run 占比；`errored_runs`：没有干净结束的 run 数（两本账任一：trace 不是 `complete`，或 agent 以 error / timeout / vetoed 结束、没有读得回的 `result.json`） |
| 恢复校验率 | `branch` 的 `StepRecord.restoration` | 通过的校验 / 全部校验；不通过的按 `RestorationCheck.reasons` 计（一条多个原因各计一次）；重定位失败按 `not_found` / `ambiguous` 与 `RetargetMethod` 计 |
| 分叉结果覆盖 | `branch` 的回放日志（`iter_decisions`）、`TraceConfig.branch.checkpoint_step`、各步执行的动作 | 在动了手的分叉里（空分叉单独计数，见上），检查点步及之后（策略步、终止步、收尾）一个主 frame 文档 miss（回放自己的 `diverged`）都没有的分叉占比；同一范围里其余的 miss（子 frame 文档、beacon、子资源；折叠的按它代表的条数计）按动作类型（miss 所在步执行的动作名；终止步记为 `terminal`、收尾的记为 `trailing`——页面加载完之后才发出的请求落在取走它的那次收集，§4.14 "步归因"）、`resourceType`、host、原因与请求类别分布。检查点之前的 miss 属于恢复，由恢复校验率的 `document_miss` 体现，不算这一项 |
| 回放确定性 | `same_path` 与 `branch` 的终止步、检查点步的截图 | 同一输入 = 同一源 run、同一检查点、策略步执行的动作逐个相同（名字与参数的规范 JSON）；组内两两比较终止截图（`origin == "terminal"`）的 sha256，报可比的对数与相同的对数（没有终止步的 run 不比、计数）；另报同一检查点所有分支的检查点步截图（恢复出的状态，与 teacher 无关）是否相同 |
| 导出确定性 | `datasets/first`、`datasets/second` | 两棵目录树的摘要（相对路径与每个文件的 sha256，按路径排序；`core/trace/digest.py::tree_digest`）相同；`repeat: 1` 时为 `null` |
| 隐私 | `<root>/traces` 与 `<root>/datasets` 下的每个文件（gzip 解开，`core/trace/secret_patterns.py::readable_text`；按目录树走，不按 manifest——没被索引的文件泄漏了也是泄漏） | 每个植入的凭证出现的次数（必须是 0）与所在的文件数；`SECRET_PATTERNS` 每个形状的命中数。植入值来自链规格：演练写字面量，live 只写环境变量名、值从进程环境读；报告只写标签与次数，从不写值。"没有泄漏"要每个植入值都搜过：有一个没搜（环境变量没设、`report --root` 没有规格也就没有植入值），建议是 `insufficient`、没有结论；搜到的植入值或凭证形状照样是"泄漏"的结论。`env/` 也在扫描范围里：cookie 与 Web Storage 的值按 §5.6 原样存进 `storage.json`，经它们存放的植入值会被数到（演练因此不经 cookie 植入）；这一项要不要只看两道门（发给模型的请求、数据集），由用户定 |

#### 各节与建议（`report.py`）

- **站点**：每个站点（任务 `start_url` 的主机，`core/trace/sources.py::task_identity(config).site`，与导出 `split.by: site` 和 `exclude_sites` 用的是同一个键；分叉 run 用它的源任务）的任务数、录制的两本账（`record_status`：trace 的状态 `complete` / `incomplete` / `writing`；`record_outcome`：agent 的 `agent_done`，没写 `result.json` 是 `(no result)`，读不回来是 `(unreadable result)`）、原路径保真、恢复校验率、分叉覆盖（空分叉不计）、miss 数；回放日志读不回来的 run 不进 miss 数与分叉占比，记在 `sections.sites.skipped`。**剔除名单**：被拦截（下一条）、或原路径保真低于 `SITE_MIN_FIDELITY`（0.8）、或录制的环境在第 0 步就失败（观测或动作失败，即循环以 `env_status: failed` 结束的那两个阶段；或一步都没提交）的站点；模型调用失败（`error_phase: llm`：429、超时、被拒的 schema）是路由的失败，从不剔除站点，按站点单独计数（`record_model_failures`）；输出成 `ExportFilters.exclude_sites` 的形状（主机名，后缀匹配）并附 YAML 片段。链不自动套用（导出在报告之前）：人把它放进导出配置再导一次。
- **anti-bot / Cloudflare**：录制里文档请求（`resourceType: Document`，主 frame 与子 frame 都算，验证码 iframe 也是拦截）的应答带拦截特征（browser 的 `challenge_documents`：状态 403 / 429 / 503，且有 `cf-mitigated: challenge` 头、或 `server: cloudflare` 加 `cf-ray`）的站点、run 与步；建议：剔除。只认应答头，不带这些头的拦截页面认不出来（写明的残余）。
- **重复分叉率**：同一 `branch_group_id` 里检查点步执行的动作（规范 JSON）与排在前面的分支相同的分叉，占参与比较的分叉的比例（与导出的 `duplicate_of` 同一规则：检查点步没有动作的分叉不比较，计在 `not_compared`——三次失败的 teacher 调用不是两个重复），并对照数据集 manifest 的 `duplicates`。teacher 的采样够不够分散就看它；它高时再考虑每个分支不同的提示（§4.1 "teacher 采样"）。
- **POST 分叉的 miss**：分叉 run 检查点及之后方法为 POST 的 miss（文档、子资源分开），有 POST miss 的分叉占比。
- **存储增长与压缩评估的输入**：按角色、按类别的字节（`observations:<fragment>` 的 json、`artifacts:<fragment>`（截图）、`messages`、`steps`（步记录与 extra 索引）、`env:<归档器>`、`env:blobs`（内容寻址）、`env:run`（capability 的 run 记录）、`run`（manifest / config / result）），每个 run、每步的平均；文件是 `RunTrace.committed_files` 列出的那些（run 自己的三个文件、manifest 与 extra 索引列出的；不按目录树走，路径的形状与"留在 run 目录里"由 RunTrace 检查，§5.5），每个路径计一次，字节经 `read_committed` 读，类别按条目的 `kind` 与 `archive` 定（`metrics.storage_category`）；还没压缩的字节用 gzip（level 6）实测压缩比；按文件开头的字节认出已经压缩的（gzip、PNG、JPEG、GIF、WebP：截图、WARC、`dom_raw`），只报大小；数据集目录按文件树的字节。
- **保留期与容量**：每个角色每个 run 的平均字节、数据集每个样本的字节；每个任务的字节 = 一次录制 + 其余每个角色"每次录制有几个 run × 每个 run 的字节" + 数据集每个任务的字节；按 `RETENTION_PROJECTION_TASKS`（10、20、300：第一次 live run、完整的 live 链、整个 `All` 子集）投影。
- **导出产出**：每个切分的样本数、按原因的丢弃与跳过（数据集 manifest，经导出写它用的同一个 `DatasetManifest` 读），`labels.step_status == action_failed` 的样本占比（导出该留还是该丢它们；每行 `<split>.samples.jsonl` 的样本经导出 dump 它用的同一个 `CanonicalStepSample` 读，`metrics.dataset_samples`）。读不回来的 manifest 或样本行是点名文件（与行号）的 `DatasetFormatError`，不读成 0。
- **待决的决定**：`miss_policy`（`strict` 够不够用；"miss 时转去真实网络"不是 miss 策略，而是一个新的网络来源，§4.14）看分叉覆盖、子资源 miss 的分布与 POST miss（`STRICT_MIN_IN_ARCHIVE_SHARE` 0.9：动了手的分叉里零文档 miss 的占比不低于它就留 `strict`，空分叉不进分母）；`recording_coverage`（录制时扩大覆盖：预取或探索）看动了手的分叉在检查点之后的文档 miss 里 GET 到未录 URL（`no_match`）的占比（`EXPAND_COVERAGE_MIN_UNRECORDED_SHARE` 0.5；没有文档 miss 时结论是"不扩大"）；`site_rules`（要不要自动生成站点规则）看所有回放 run（`same_path` 与 `branch`）的近邻 miss（browser 的 `site_rule_candidates`：最佳候选（匹配器排第一的那个）同 host、同 path、同方法、规范化查询参数不同的 `no_match`，按 host 与不同的参数名计数；某个 host 达到 `SITE_RULE_MIN_NEAR_MISSES` 2 次就建议生成 `ignore_query` 规则供人审）；`retention` 看上一条的容量数（与 `recommendations.sections.retention` 是同一条建议）。这几个决定的输入（分叉的文档 miss、近邻 miss）单列在报告的 `decision_inputs` 里。

**建议槽**：每个度量、每一节、每个决定一个 `Recommendation{status: supported | partial | insufficient, verdict, rule, evidence: list[EvidenceRef{pointer, value, n}], basis, sites, status_reason}`。`basis` 是链规格的 `evidence`；`sites` 是证据来自的不同站点数。没有数据是 `insufficient`、`verdict` 为空，从不给默认结论；否则只有 `live` 的证据、且来自至少 `SUPPORTED_MIN_SITES`（10）个站点才是 `supported`，不到 10 个站点最多是 `partial`（"insufficient sample"），`rehearsal` 的证据最多是 `partial`（"fixture 只校准度量本身，live run 之后再定"）；低于 `supported` 的原因写在 `status_reason` 里（`supported` 时为 `null`），`report.md` 每条建议下照写。用户 2026-10-13 的裁决：2–3 个任务的 live run 是冒烟测试，不拿来定分界线与决定。`rule` 写出所用的规则与阈值，`evidence` 引用报告里的度量（JSON 指针）与它们的分母。

**阈值**：具名常量，名字是"它约束什么 + `_MIN_` / `_MAX_` + 量"：`SUPPORTED_MIN_SITES` 10、`SITE_MIN_FIDELITY` 0.8、`REPLAY_MIN_FIDELITY` 0.9、`RESTORATION_MIN_RATE` 0.9、`STRICT_MIN_IN_ARCHIVE_SHARE` 0.9、`EXPAND_COVERAGE_MIN_UNRECORDED_SHARE` 0.5、`SITE_RULE_MIN_NEAR_MISSES` 2、`CAPTURE_MIN_BODY_SHARE` 0.95、`REPLAY_MIN_IDENTICAL_SHARE` 1.0、`BRANCH_MAX_DUPLICATE_SHARE` 0.5、`COMPRESS_MAX_GZIP_RATIO` 0.5、`ACTION_FAILED_MAX_KEPT_SHARE` 0.2（`RETENTION_PROJECTION_TASKS` 是投影用的任务数，不是阈值）。它们是 live 之前定的起点，留在 `report.py` 里，直到一次覆盖足够多站点的 live run 校准它们（用户 2026-10-13 的裁决）。每步的归档开销上限不在这里：`ARCHIVE_MAX_P50_OVERHEAD_MS`（300 ms）只定义一次，在 `core/archive/budget.py`，报告的归档开销规则与 browser 的开销基准（§4.14）都读它。

#### 报告

- `report.json`：格式 `af-flywheel-report-2`（pydantic `FlywheelReport{format, chain{name, evidence}, inputs, metrics, sections, decision_inputs, recommendations{metrics, sections}, decisions{miss_policy, recording_coverage, site_rules, retention}}`；`-2` 起：建议带 `sites` 与 `status_reason`，决定改用语义名，度量带 `sites`、空分叉与 `skipped`），键排序、缩进 2，**没有墙钟字段、绝对路径或主机信息**（run 写成相对链根的 `traces/<角色>/<run>`），同样的输入算两次逐字节相同；`inputs` 按角色记 run 数、跳过的 trace 与原因、以模型调用失败结束的 run（`model_failures`：路由的失败，不是站点的证据；它们照样计入各项度量，live 计划写了怎么处理），以及 trace 里实际运行的组件（agent、loop、恢复器、cleaner、serializer、backend、网络来源、模型路由；由 `metrics.components(trace)` 读脱敏后的 `TraceConfig.resolved`，没标 trace-safe 的自由字符串读作 `<omitted>`：验证要点名所用的配置），数据集记在不在与格式。每条证据的 `pointer` 都能在 `report.json` 里解析到它的 `value`（测试钉住）。
- `report.md`：同一个模型渲染，标题是 `Metrics` / `Sections` / `Pending decisions`，每项度量、每一节、每个决定各占一节，各项跳过的 run 与原因逐条列出，每节末尾是建议、证据、站点数、基础，以及低于 `supported` 的原因。报告里的名字、标题与规则文字不用规划编号（AGENTS.md §3.6；`test_report_schema.py` 钉住）。
- `spectragent-flywheel report --spec <chain.yaml>`（或 `--root <dir> --evidence live|rehearsal`）只读、可重复；`run` 的最后一段就是它。

#### 共用而不是复制

- `restoration_rate` / `RestorationRate`、`components` 从 `tests/_helpers/branch_driver.py` 挪进 `flywheel/metrics.py`（M31 的 live 测试改从这里 import，`branch_driver` 里不留同名的函数）；
- 去重的账法（`DedupBytes`）与 `percentile` 从 `tests/_helpers/archive_bench.py` 挪进 `flywheel/metrics.py`，revisit 的那一半是 browser 知识（`archive_stats.revisit_saving`）；M29 的基准与它的 schema 测试改用这两处；
- `tests/_helpers/secret_scan.py` 的 gzip 感知读法挪进 `core/trace/secret_patterns.py`（`readable_text(path)` / `iter_readable(root)`，只 import 标准库），secret guard 与 M29 的隐私测试改从这里 import，测试帮手删掉；
- `TaskIdentity` / `task_identity` 从 `export/filters.py` 挪进 `core/trace/sources.py`（只读 `TraceConfig`；导出与度量共用同一个"站点"与"任务"的定义），导出改从这里 import；读不回来的 run 的措辞 `unreadable_reason` 一起挪过去（报告与数据集 manifest 写同一句）。
- 数据集 manifest 的形状是 `schemas/dataset.py::DatasetManifest` 一处：导出器经它写，度量经它读（`metrics.read_dataset_manifest`），没有哪一边用 `.get(...)` 猜键。
- 目录树摘要是 `core/trace/digest.py::tree_digest` 一处：度量的导出确定性、导出测试的两次导出比较、离线回放与 `RunTrace` 读源 run 时"一个字节也没动"的测试（`metadata=True` 连权限、大小与修改时间一起比）都经它；它只 import 标准库，core 一侧的测试不必 import `spectragent.flywheel`。
- 每步的归档开销上限 `ARCHIVE_MAX_P50_OVERHEAD_MS` 在 `core/archive/budget.py` 一处：报告的规则与 browser 的开销基准都读它（基准仍可用 `SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS` 在吵的机器上临时放宽）。
- 去重的账法是 `DedupBytes.account(listed, distinct, saved_by_revisit)` 一处：trace 一侧（`metrics.archive_bytes(run)`：`RunTrace.committed_files` 里每个提交列出的每个归档文件各计一次 = 去重前，每个不同路径一次 = 去重后）与 M29 基准的内存适配（`archive_bench.dedup_bytes(files)`）都经它；`percentile` 保持 M29 的最近秩定义，空输入改成报错。

#### browser 一侧的读法

browser 证据的格式知识留在 browser 的包里：纯函数，只经 `ArchiveReader`（§4.14 "回放的数据通路"）或它读出来的行读，不 import `core.trace` 与 `spectragent.flywheel`，与 `replay_log.py` 是同一条边界（`test_archive_boundaries.py` 的第 5 条规则）。`replay/archive_index.py::iter_index_lines(reader)`（`(step, 行)`：每一步、再是收尾的每一行 `ExchangeLine | PendingLine | BodyLine`；与 `load_archive` 共用同一个按提交读行的函数）；`replay/replay_log.py::iter_decisions(reader)`（`(step, 决定, 条数)`：写全的条数为 1，`summary` 读成它折叠的那种、条数为 `count`；`document_miss_total` 改成经它计数，折叠的规则只有一份）；新的 `core/capabilities/browser/archive_stats.py`：`body_completeness(lines)`（每条记录的最终状态读它的最后一行，`body` 行在内，与回放加载同一读法）、`revisit_saving(lines)`（revisit 指向的完整记录长度减自身长度，可以为负；指向缺失的记录报错，同 M29）、`challenge_documents(reader)`（应答头在 WARC 记录里，所以经 `load_archive` 读，带出所在的步）、`site_rule_candidates(decisions)`。

#### 演练（fixture，零花费）

`tests/_helpers/flywheel_rehearsal.py`：一条命令 `uv run python -m tests._helpers.flywheel_rehearsal --out <dir>`（`run_rehearsal`；除了链根，还写一份 `rehearsal.json`：各段、各模型的路由、外联审计），e2e 测试（`tests/test_flywheel/test_rehearsal_live.py`）在测试进程里调同一个 `run_rehearsal`，读它跑出的东西（链根与 `Rehearsal.summary()`）。它有意放在全量测试的进程里，并让登录任务紧挨在漂移任务之前录：provider 把客户端留给垃圾回收的问题（§4.7）修好之前，正是这里一次模型调用的 TCP connect 永远完不成。演练起 M29 的 fixture 站点与每个运行段一个脚本化的模型（UI-TARS 的坐标回复，按任务文本里的标记与步号（请求里 assistant 轮的个数）路由），把发布的 UI-TARS 录制 / 原路径回放 / 分叉 / 导出配置改成 fixture 的那份（模型地址与键、fixture 的路由开关、任务的 `data_dir`、仓库文件的绝对路径；与 M30–M32 的驱动同一种改法），加上对照组（录制配置 + `archive: []`），写出链规格（`evidence: rehearsal`，植入值写字面量），在进程内跑整条链。四个任务分在三个站点上（fixture 的 `FLYWHEEL_SCENARIOS`，每个站点一个映射主机，控件都绝对定位）：`shop.example.test` 上确定性的四步流程（输入框、GET 表单提交、商品链接、读价格；商品页的脚本请求 `recommend.json?session=<timeOrigin>`，每次回放都是一个子资源近邻 miss）与一个登录页（运行时拼出凭证，经 `Authorization` 头、urlencoded POST 的 `password`、密码框的值发出，值从不出现在任何应答的字节里；不经 `document.cookie`：`storage.json` 原样保存 cookie 的值（§5.6），经 cookie 的凭证按设计会出现在 `env/` 里）；`drift.example.test` 上点击后同步跳到 `next.html?visit=<timeOrigin>`（pinned 不固定 time origin，回放必然是文档 miss，而且停在 Chrome 的错误页上；错误页写着那个 URL，所以这一组的回放截图两次不同）；`blocked.example.test` 的 403 带 Cloudflare 的三个头。分叉段在商品流程的结果页（第 2 步）分三叉：一个与录制做同样的事（覆盖）、一个提交结果页上录制没用过的 POST "quick add" 表单（必然 miss）、一个与第一个重复。导出 YAML 加上指向 fixture 任务数据的 `eval_loader` / `task_loader`（泄漏扫描经它们读参考答案）。e2e 测试断言每个度量的值与它的解释，报告算两次逐字节相同，以及演练期间没有非 loopback 的外联：Python 侧审计每一次 `socket.connect` 的对端；Chrome 侧录制用 hermetic 的解析规则、回放用隔离，归档里出现的主机只有 fixture 的。整条链在本机约 40 s。

#### 已知限制

- live 的对照组与录制不走同一条路径，归档开销比的是分布；对照组要再花一次录制的 token（live 计划里是可选项）。
- 原路径回放每个分支有一次 teacher 调用（最后一步），不是零花费。
- 拦截只按应答头识别；隐私只认植入的值与凭证的形状（同 §4.15 "隐私"的残余）。
- 度量只认 browser 的证据格式；第二条数据线见本节开头。
- 一次回放停在错误页上时，Chrome 的错误页写着没送到的 URL：URL 每次不同的发散站点，回放确定性按截图比必然"不同"，报告照实计数（演练里的发散站点就是这样）。
- 存储的字节是 trace 落盘的样子：每次模型调用的消息文件内联了它之前的每一张截图（UI-TARS 的 memory 保留全部截图），所以 `messages` 随步数平方增长。这是保留期与压缩评估的主要输入，演练的 fixture 页面太小，看不出真实比例。

#### 代码位置

`spectragent/flywheel/`：`metrics.py`、`report.py`、`chain.py`、`cli.py`（`pyproject` 的 `[project.scripts]`：`spectragent-flywheel`）；`core/data_loader/task/step_selector/last.py`；`core/trace/digest.py`（`tree_digest`）、`core/archive/budget.py`（`ARCHIVE_MAX_P50_OVERHEAD_MS`）、`core/trace/sources.py`（`task_identity`、`unreadable_reason`）、`core/trace/secret_patterns.py`（`readable_text`、`iter_readable`）、`core/trace/reader.py`（`RunTrace.committed_files` / `read_committed`：存储与归档字节的账只读它们，§5.5）；browser：`archive_stats.py`、`replay/archive_index.py::iter_index_lines`、`replay/replay_log.py::iter_decisions`；发布的配置 `configs/flywheel/same_path_ui_tars_archive.yaml`；测试 `tests/test_flywheel/`（`tests/_helpers/flywheel_traces.py` 用真实的 WAL 写出答案已知的链根）；演练 `tests/_helpers/flywheel_rehearsal.py`；live 计划与它的配置在本地的 `tmp/flywheel/M33_live_plan.md`、`tmp/flywheel/m33_live/`。

---

## 5. RunTrace 全量落盘体系

### 5.1 设计原则

**Log 是运行时的一等公民**，与数据库 WAL 的设计思路相同：每步必须确认落盘后才继续下一步。如果 agent 在第 47 步 crash，前 46 步的数据不能丢。

**不使用 SQLite** — agent 场景的数据结构是异构的（不同 fragment 类型字段完全不同），schema 会随框架演进频繁变化，这与关系数据库的核心假设（同构行、稳定 schema）冲突。数据落盘速度不是运行瓶颈（LLM 推理每步需要数秒，文件写入只需毫秒），SQLite 的查询优势可通过后置索引实现。

**环境证据（environment evidence）是第二类记录。** trace 记录 agent 行为（2026-09-16 的裁决："agent 换一个选择，这个值会不会变？"会变的才记）。环境在 run 中的状态与资源——响应体、存储快照、原始 DOM 采集、环境指纹——通过同一个检验：agent 换一个选择，它们就会不同，而且它们是那次决策所处的世界，不是框架执行决策的细节。按用户裁决 D2（2026-10-09），它作为单独的一类进 trace：

- **显式开启**：只有绑定了非空 `archive`（§4.5）的 run 才有，默认没有；
- **单独放在 `env/` 子树**（§5.3），读侧有自己的访问器（§5.5）；
- **单独标记隐私**：`TraceConfig.env_evidence_privacy`（绑定了归档器才写；绑定了哪些看 `negotiation.active_archivers`），加上 capability 自己的 run 记录（§5.6）。

2026-09-16 那条裁决的另一半照旧成立：不记 CDP 事件流，不记输入 tick。归档存的是资源和状态，不是把它们送过来的协议事件序列，也不是框架执行一次点击的轨迹。

### 5.2 写入协议

每步只提交一次：`TraceWriter.after_record_step` 把这一步要落盘的全部东西一次交给 `WALWriter.commit_step`，WAL 按固定顺序写完，再原子更新 manifest。

```
commit_step(step, record, *, fragments, llm_calls, extra_files) 的顺序:
  0. 校验（写任何字节之前）：路径规则见下；内容寻址文件的摘要与路径一致
  1. 内容物：fragment JSON 与 artifact、每次 LLM 调用的记录、本 run 还没提交过的 output schema、
     extra 文件 —— 逐个 tmp → fsync(file) → os.replace；不做逐文件的目录 fsync，
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
    WALWriter 内存里的 manifest、已提交路径集合和已确认落盘的目录集合只在 manifest 写成功之后才更新
```

**"还没确认落盘"按 WAL 自己的记录算，不按"这次调用新建了谁"算。** 目录的名字是它父目录里的一个目录项，只有父目录 fsync 之后才落盘。WALWriter 记着哪些目录的名字已经由 `init_run` 或一次成功的提交确认落盘；一次提交写到的每个文件，其路径上不在这个集合里的目录，父目录都要 fsync。只看"这次新建了哪些目录"会漏掉重试：失败的那次已经把 `env/`、`env/steps/000/` 建出来了，重试时它们"已存在"，父目录就没人 fsync，manifest 却把它们下面的文件列成已提交，掉电后这些文件可能整个不见。M28 在一步提交失败之后用 `commit_trailing` 往同一批目录里冲刷证据，也是同一种情形。

`WALWriter`（`core/trace/wal.py`）是纯 I/O 引擎，不认识 Hook/AOP。它的写接口只有下面这些；逐文件写入的 `write_observation` / `write_messages` / `write_step_record` 和 `commit_unfinished_messages` 已删除，不保留并存的旧路径：

```python
class WALWriter:
    def __init__(self, base_dir: Path, run_id: str) -> None: ...

    def init_run(self, config: TraceConfig, *, config_hash: str | None = None) -> None:
        """write_durable config.json（按需建 run 目录和 traces/），建 steps/ observations/ messages/ evals/，
        把这些目录项一批 fsync：run 目录，加上为它新建的每个目录的父目录（traces/；这个 run
        新建了 traces/ 时再加 traces/ 的父目录），然后才 atomic_write 初始 manifest
        （format_version=3，last_step_completed=-1，complete_through=-1，status=writing）。
        先有 config.json 才有 manifest：run 存在了，它的配置就一定在。之后的提交只 fsync run 目录
        之内的目录，run 目录自己的名字只在这里落盘。manifest 写成之前任何一步失败，都不留下 run。"""

    def commit_step(
        self,
        step: int,
        record: StepRecord,
        *,
        fragments: Sequence[EncodedFragment] = (),
        llm_calls: Sequence[EncodedLLMCall] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """按上面的 0–4 提交一步；返回即已落盘。manifest 的 complete_through 随之更新：
        从第 0 步起不间断的已提交步的最后一步（记不下来的那一步从不提交，所以它停在洞前）。"""

    def commit_trailing(
        self,
        *,
        llm_calls: Sequence[EncodedLLMCall] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """同一协议，但不写 StepRecord、不推进 last_step_completed：
        manifest.trailing 追加一条 TrailingEntry。两样都为空时什么也不做。"""

    def write_result(self, result: AgentResult) -> None: ...   # atomic_write result.json
    def finalize(self, status: TraceStatus, *,
                 incomplete_reason: str | None = None) -> None: ...   # 关闭 trace：complete，或 incomplete 加原因
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
```

`encode_llm_call` 的 `pending`（`PendingLLMCall`）是 TraceWriter 在 `around_ainvoke` 里登记的那次调用：登记那一刻取下的 prompt 的 JSON 快照（之后 memory 或 serializer 再改 message 对象，记录不变）、按类缓存、已编码的请求 schema（`EncodedOutputFormat`：`OutputFormatRef` 加它指向的内容寻址文件）、开始时间，以及结局——由 `complete` / `fail` / `cancel` 中先到的那一个定下，同样在发生时取快照（§5.4）。快照由 `snapshot(value)` 取：pydantic model、dataclass、映射和序列按结构转换，bytes 记成 base64，其余 JSON 装不下的东西变成它的 `str()`，自身永不抛错。同一模块还有：

- `encode_llm_calls(step, pending, *, first_call=0)`：按开始顺序编号，从 `first_call` 起；TraceWriter 用它让一步的调用接在这一步已提交的 trailing 调用之后；
- `encode_output_format(output_format) -> EncodedOutputFormat`：回复模型的 `model_json_schema()`，规范化 JSON 加 sha256；不是 pydantic model 类就抛 `TypeError`；
- `encode_fragment(step, value)`：单个 slot，`encode_observation` 逐个调用它；
- 读侧用的 `decode_fragment(key, cls, payload, artifact)`：先核对 payload 的 `fragment_id`，再调 `from_trace`；
- `encode_archive_file(file, *, step=None, trailing=None, run_version=None) -> TraceFile`：把一个 `ArchiveFile` 编码成 extra 文件，按它的文档化字段读（鸭子类型，与 `TraceNegotiation.from_results` 同样的理由：`core/trace` 不 import `core/archive` 的类型）。路径：`scope == "run"` → 必须给 `run_version`，`archive_run_path(capability_id, run_version)`；`content_addressed` → `archive_blob_path(sha256(data))`；其余 → `step` 与 `trailing` 恰好给一个，`archive_step_path(step, archiver_id, name)` / `archive_trailing_path(trailing, archiver_id, name)`；不满足抛 `ValueError`。同样抛 `ValueError` 的还有：`step` 与 `trailing` 同时给出（一个文件只属于一次提交）、不是 run 记录却给了 `run_version`、内容寻址的 run 记录、未知的 `scope`。因为是鸭子类型，`ArchiveFile` 自己会拒绝的这里再查一遍。`meta` = `{ARCHIVE_META_KEY: ArchiveMeta(...).model_dump(mode="json")}`（`ARCHIVE_META_KEY = "archive"`），`content_addressed` 照抄。

fixture 与 `RunTrace` 共用这些函数。

**路径校验**（第 0 步；任何一条不满足，都在写任何字节之前抛 `ValueError` 并点名路径）：

- POSIX 相对路径：不以 `/` 开头，不含 `\`，没有空段、`.` 段或 `..` 段（`encoding.check_relative_path`，读侧用同一条规则，§5.5）。每个通道都查，`encoding.py` 命名的文件也不例外：fragment 文件按 key 命名，而 `FragmentFailure.fragment_id` 是自由文本；下面"待在自己的目录里"那条只是前缀判断，`observations/000_x/../../../escaped.json` 照样通过，output schema 的父目录判断经 `PurePosixPath` 规范化，`.` 段和空段会被吃掉；
- extra 文件不得落在 WAL 自有的名字上：`manifest.json`、`config.json`、`result.json`，以及 `steps/`、`observations/`、`messages/`、`evals/` 之下的任何路径。这些名字由 WAL 和 `encoding.py` 自己决定。按第一段判断，所以 WAL 的文件名也不能被当成目录：`result.json/evidence.txt` 会建出一个叫 `result.json` 的目录，run 收尾时写 `result.json` 就会失败；
- 一个路径要么是文件、要么是目录，不能两者都是：`env/a` 和 `env/a/b` 不能同时出现，不论在同一次提交里还是跨提交（不区分大小写），WAL 自己的目录（`steps/`、`observations/`、`messages/`、`messages/output_formats/`、`evals/`）也不能被文件占用。不在校验里拦住，它会在写到一半时变成 `NotADirectoryError`；
- 已提交的文件不可变：非内容寻址文件不得与本 run 已提交的路径同名；
- 内容寻址文件的路径必须包含 `sha256(data)` 的完整十六进制摘要，允许按 `<前两位>/<其余>` 分片（如 M28 的 `env/blobs/sha256/ab/cdef…`）；
- `encoding.py` 命名的文件待在自己的目录里：fragment 文件和 artifact 在 `observations/` 下，调用记录在 `messages/` 下（不在 `messages/output_formats/` 里），output schema 必须内容寻址、直接位于 `messages/output_formats/` 下；
- 同一次提交里同一路径只能出现一次（内容寻址文件除外，只写一次），同一步里同一个 fragment key 只能出现一次；`record.step` 必须等于 `step`，一个 step 只能提交一次；
- 以上比较一律按 `encoding.fold_path` 的键进行：macOS 默认的 APFS 既不区分大小写、也不区分 Unicode 规范化形式，`env/A.txt` 和 `env/a.txt` 是同一个文件，`café.json` 的预组合（NFC）与分解（NFD）两种写法也是同一个文件，trace 拷到这样的文件系统上也不能互相覆盖。键是 Unicode 的规范无大小写匹配（canonical caseless matching，Unicode §3.13 D145）：`NFD(casefold(NFD(path)))`。只做 `casefold` 漏掉规范化变体；先 NFC 再 `casefold` 仍会漏掉大小写折叠之后才分解出来的那些对（`Ś` 与 `ſ` + 组合尖音符）。

**内容寻址文件在 run 内去重。** WAL 在内存里记着本 run 已提交的内容寻址路径，只在 manifest 提交成功之后更新。再遇到同一路径就跳过写入：extra 文件照常列进本次提交的索引（同一个响应体会被很多步引用）；output schema 只在 `manifest.output_formats` 里出现一次，由引用它的每条调用记录指向（同一个回复模型会被几十次调用使用）。去重跳过的只是写入，摘要校验照做：同一路径换了字节再提交会被拒绝，否则索引会记下磁盘上那个文件并不具有的 size 和 sha256。

**为什么每个文件的数据 fsync 都保留，只合并目录 fsync。** §5.1 承诺每步确认落盘才继续。只 rename、不 fsync 数据，掉电后 manifest 可能列着一个空文件，提交点本身就在说谎。数据 fsync 之前还要先把字节 flush 出 Python 的缓冲：`os.fdopen` 最多缓冲 `st_blksize`（APFS、ext4 上是 4 KiB），漏了 flush 或者把 fsync 挪到写之前，所有比它小的文件（step 记录、前几步的 manifest、调用记录、verdict）fsync 时都还是空的，字节要到 `close()` 才进内核。fsync 的次数和顺序都不变，只有 fsync 那一刻的文件大小看得出来，测试就按这个检查。目录 fsync 让 `os.replace` 和 `mkdir` 产生的目录项落盘；一个目录 fsync 一次，就覆盖本批在它下面产生的全部目录项，所以按目录合并不损失任何保证。省下的是逐文件的目录 fsync，M28 那种每步上百个证据文件时尤其明显。macOS（APFS）上 1000 个 1 KiB 文件一次提交约 0.2 s；macOS 的 `os.fsync` 不刷设备缓存，fsync 的真实代价在 Linux ext4/xfs 上。**TODO（Linux）**：这些数只在 macOS 的 APFS 上校准过，ext4/xfs 还没测过。1000 个文件一次提交的墙钟上限（默认 3 s）只是 `test_wal_batch_commit.py` 基准测试里的断言，运行时不强制，`SPECTRAGENT_WAL_COMMIT_BUDGET_S` 可覆盖；Linux 上的提交只会更慢，不会因此失败。

**目录 fsync 的 errno 策略**（`core/trace/fsio.py` 的 `fsync_dir`）：

- `os.name == "nt"`：跳过（Windows 打不开目录）；
- 目录 fd 上的 `os.fsync` 抛 `EINVAL` / `ENOTSUP` / `EOPNOTSUPP`：容忍，那是文件系统不支持目录 fsync；
- 其余一律原样上抛，包括 `os.open` 自己的失败（`EMFILE`、`EACCES`、`ENOENT`）和 `EIO`、`ENOSPC`。数据 fsync 和 `os.replace` 出错时本来就是这个策略：一个声称已落盘、其实没有把握的 manifest，比一次中断的 run 更糟。TraceWriter 是 blocking hook，所以这类错误会结束 run。

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

- 一步的 `after_record_step` 在 manifest 提交之前拉取的批次，随**这一步**的 `commit_step(..., extra_files=...)` 落盘，列在 `steps/NNN.extra.json`；内容寻址的文件由 WAL 在 run 内去重，每个引用它的步的索引都列它。
- 收尾时每样欠 trace 的东西**各自一次** `commit_trailing`，按这个顺序：(1) 还登记着的 LLM 调用；(2) 提交失败的步遗留下来的每个批次（不止一个时，两步的批次可能带着同名的归档文件，不能挤进同一个 `env/trailing/KK/`；只有提交失败之后还继续跑的驱动会这样）；(3) 收尾时收集到的尾部。所以每一批证据都有自己的 `env/trailing/KK/`，调用那一次没有 extra 索引。没东西可提交的就不提交。
- 一次提交失败时，它带的归档文件留在 TraceWriter 里，收尾时作为上面的 (2) 提交，重新按 trailing 路径编码（失败那次写下的文件是孤立文件，对读侧不可见）。归档文件的路径段与要进索引的字符串在交给 WAL 之前就校验过（§4.9、§5.4），所以这样的批次不会因为同样的路径错误再失败一次。即便如此，收尾的某一次 trailing 提交仍然失败（磁盘满，或某个批次因为任何重试都修不好的原因写不进去），它也只赔上它自己带的东西：**那一样就此丢掉，不重试**（用户 2026-10-13 的裁决）。收尾只做一次：TraceWriter 收尾之后就清掉还欠着的东西，之后的 `on_run_error`（`AgentRuntime` 在有错误时紧跟 `on_run_end` 派发它）什么也不重做；磁盘刚拒绝过的写，几毫秒后再试多半还是失败，多一次重试只会让收尾更复杂。其余各样照常提交；全部试过之后，第一个错误写进 trace 的 `incomplete_reason`（trace 记为 `incomplete`）并记日志，不抛出：收尾时 run 的结局已经定了，记录失败不顶替它（§4.6）。调用单独先提交，正是为了这一点：写不进去的一批证据不会连累未完成那一步的 LLM 调用和尾部（"提交失败不丢调用"，§5.4）。

### 5.3 目录结构

```
traces/
  ├── suite_index.json                  # 可选：后置生成的聚合索引
  │
  └── {run_id}/
      ├── manifest.json                 # 提交点（每次提交原子重写），format_version: 3；trace 自己那本账
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
      │   └── output_formats/
      │       └── <sha256>.json         # 请求的 output schema，run 内按内容寻址、只写一次
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
| LLM 调用 | `messages/{step:03d}_call_{call:02d}.json` | `RecordedLLMCall`；`call` 是本步内按开始顺序的序号，从 0 起 |
| output schema | `messages/output_formats/{sha256}.json` | 请求的 JSON schema，序列化为规范化 JSON（键排序、紧凑分隔符、UTF-8）；`sha256` 是这些字节的摘要 |
| step record | `steps/{step:03d}.json` | `StepRecord` |
| extra 文件索引 | `steps/{step:03d}.extra.json`、`steps/trailing_{n:02d}.extra.json` | `ExtraFileIndex`；`n` 是该条目在 `manifest.trailing` 里的位置 |
| 归档文件（步） | `env/steps/{step:03d}/{archiver_id}/{name}` | `archive_step_path(step, archiver_id, name)`；`ArchiveFile.data` 原样 |
| 归档文件（trailing） | `env/trailing/{n:02d}/{archiver_id}/{name}` | `archive_trailing_path(n, archiver_id, name)`；`n` 同上，是该条目在 `manifest.trailing` 里的位置 |
| 内容寻址的归档文件 | `env/blobs/sha256/{sha256[:2]}/{sha256[2:]}` | `archive_blob_path(sha256)`；run 内只写一次，`sha256` 是 `data` 的摘要 |
| run 记录 | `env/run.{capability_id}.json`（第 0 版）、`env/run.{capability_id}.{k}.json`（第 k 版，k ≥ 1） | `archive_run_path(capability_id, version)`；JSON |

归档路径的根是 `ENV_DIR = "env"`，四个函数都在 `encoding.py`。每个归档文件在它所在索引里的 `ExtraFileRecord.meta["archive"]` 是一份 `ArchiveMeta`（下面的记录类型）：读侧靠它知道一个 extra 文件是归档文件、属于哪个归档器、第几版 run 记录。

**manifest（format_version 3）**，以一个在第 2 步超时的 run 为例：agent 超时了（`result.json` 的 `agent_done: timeout`），trace 却是完整的——它记下了这个 run 做过的一切，被切断那一步的调用作为 trailing 落了盘：

```json
{
  "format_version": 3,
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
  "output_formats": ["3b6f…"]
}
```

同一个 run 若在第 2 步收集环境证据失败（出洞，§4.6），manifest 是 `"status": "incomplete"`、`"incomplete_reason": "recording failed at step 2: ArchiveCollectionError: …"`、`"complete_through": 1`，`result.json` 是 `agent_done: error`、`error: "recording failed at step 2: …"`；若那一步正是 agent 说 done 的一步，`result.json` 仍是 done 带着答案，manifest 照样是 `incomplete`。

**两本账（用户 2026-10-13 的裁决）。** `result.json` 只说 agent 怎么结束：done / max_steps / error / timeout / vetoed（`AgentDoneStatus`）。manifest 只说 trace 记全没有：

| `status` | 含义 | 谁写 |
|---|---|---|
| `writing` | trace 还开着：run 还在跑，或进程死在收尾之前；什么都不作数 | `init_run` |
| `complete` | 已关闭，run 做过的一切都记下了 | `finalize`（收尾没有记录失败） |
| `incomplete` | 已关闭，但有东西没记下来：某一步（出洞，run 在那里停下）、收尾那次收集、收尾补写的某一样欠账、`result.json`；`incomplete_reason` 写明是什么（几样按发生顺序用 `; ` 连起来，第一个洞在前），`complete_through` 说完整到第几步 | `finalize` |

`complete_through` 是"完整到第几步"：0..`complete_through` 每一步都已提交、各自带着自己的证据；-1 表示一步也没有。它由 `commit_step` 维护（从第 0 步起不间断的已提交步的最后一步），记不下来的那一步从不提交，所以它停在第一个洞之前，之后就算还有步提交也不前进；没出过洞时它等于 `last_step_completed`，尾部失败不动它。`writing` 的 trace 里它也一直是当前值。读者各取所需：分叉与导出把"已关闭、且完整到所用的那一步"当硬条件（§4.10、§4.15），agent 怎么结束是另一个筛选；sweep 的汇总分三栏计数（§4.2）；度量按站点分别数两本账（§4.16）。格式 2 的 `error` 两种意思都有，读者分不出来，所以 format 3 不读任何更旧的 trace（§5.5），不做兼容 shim。

**记录类型**（`core/trace/models.py`，`TRACE_FORMAT_VERSION = 3`）：

```python
class FragmentFiles(BaseModel):
    file: str                          # observations/NNN_<key>.json
    artifact: str | None = None        # observations/NNN_<key><suffix>
    failed: bool = False               # True：file 里是 FragmentFailure，不是 fragment

class StepManifestEntry(BaseModel):
    step: int
    step_file: str                                          # steps/NNN.json
    fragments: dict[str, FragmentFiles] = Field(default_factory=dict)  # 全限定 key → 文件
    llm_calls: list[str] = Field(default_factory=list)      # 本步每次调用，按开始顺序
    extra_index: str | None = None                          # steps/NNN.extra.json；无 extra 文件为 None

class TrailingEntry(BaseModel):                             # 每次 commit_trailing 追加一条
    llm_calls: list[str] = Field(default_factory=list)
    extra_index: str | None = None                          # steps/trailing_KK.extra.json

class TraceStatus(StrEnum):            # trace 自己那本账，不是 agent 的
    WRITING = "writing"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"

class TraceManifest(BaseModel):
    format_version: int = 1            # 缺省 = M27 之前写的 trace；init_run 写 TRACE_FORMAT_VERSION；RunTrace 只读 3
    run_id: str
    last_step_completed: int = -1
    status: TraceStatus = TraceStatus.WRITING
    incomplete_reason: str | None = None     # 恰在 incomplete 时有值
    complete_through: int = -1               # 完整到第几步；commit_step 维护
    created_at: UTCDatetime
    updated_at: UTCDatetime
    config_hash: str = ""
    steps: list[StepManifestEntry] = Field(default_factory=list)
    trailing: list[TrailingEntry] = Field(default_factory=list)
    output_formats: list[str] = Field(default_factory=list)  # 已提交的 output schema 的 sha256

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
    thinking: str | None = None
    redacted_thinking: str | None = None
    usage: ChatInvokeUsage | None = None
    stop_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

class CallFailure(BaseModel):
    error: str                         # 异常类名
    detail: str                        # str(exc)
    raw_output: str | None = None      # 校验器看到的原文（StructuredOutputValidationError）
    partial_output: str | None = None  # 被截断的回复已经生成的部分（ModelOutputTruncatedError）
    stop_reason: str | None = None
    usage: ChatInvokeUsage | None = None

class RecordedLLMCall(BaseModel):      # messages/NNN_call_KK.json
    step: int                          # 调用时 Loop 所在的 step；trailing 调用 = 没提交的那一步
    call: int                          # 本步内按开始顺序的序号，从 0 起
    status: Literal["completed", "failed", "cancelled"]
    started_at: float                  # 墙钟 Unix 时间，与 StepRecord.timestamp、captured_at 可比
    latency_ms: int                    # 从开始到返回 / 抛出 / 取消
    messages: list[RecordedMessage]    # 发给模型的完整 prompt；= Annotated[BaseMessage, Field(discriminator="role")]
    output_format: OutputFormatRef | None = None   # None = 非结构化调用
    completion: RecordedCompletion | None = None   # 仅 completed
    failure: CallFailure | None = None             # failed、cancelled
```

**fragment 索引进 manifest，extra 文件索引不进。** 每步的 fragment 和 LLM 调用只有几个，放进 manifest，读者一次 eager 读就知道每步记录了什么。extra 文件（M28 的环境证据）每步可能上百个；manifest 每次提交都整份重写，每个 `RunTrace` 又都 eager 读它，把这些文件内联进去会让 manifest 随 run 长度二次增长。所以每步一个 `steps/NNN.extra.json`，manifest 只记它的路径（`extra_index`）。

**失败的 fragment 显式标记。** `FragmentFailure` 的 JSON 没有判别字段，而 `ScreenshotObs` / `DOMObs` / `BrowserMetaObs` 的字段都有默认值，同一份 JSON 能被它们静默接受（已验证）。按 key 类型化读回时，一次失败的采集就会变成一个空 fragment，正是 `failure.py` 要拒绝的伪造。所以失败落在同一个文件名下，fragment 索引带 `failed: true`；读侧见到它就返回 `FragmentFailure`，从不尝试按 fragment 类解析。

**fragment JSON 就是 fragment 自己的 JSON。** `file` 的内容恰好是 `model_dump(mode="json", exclude=trace_json_exclude())`，不再往里塞 `artifact_file`：二进制文件只由索引的 `artifact` 指向，WAL 的布局信息不混进 fragment 的数据。读侧用 `from_trace(payload, artifact)` 还原（§5.5）。

**每次 LLM 调用一个文件，不论结局。** 一步可能有多次调用（ReactLoop 的 parse 重试、结构化输出校验失败后重问），它们按开始顺序编号、全部提交。`completed` 记 `RecordedCompletion`，`failed` / `cancelled` 记 `CallFailure`（字段语义见 §5.4）。写侧把登记时拿到的 prompt 直接序列化成 JSON，不做 model 校验，hook 永远不会因为 trace 的类型问题让 run 失败；读侧按 `RecordedLLMCall` 校验，不合法就是 `TraceSchemaError`。Loop 没来得及交出的那一步，其调用沿用那一步的编号（`last_step_completed + 1`），由 `commit_trailing` 提交、列在 `manifest.trailing`。驱动冲刷之后若继续跑并提交了这一步，它的调用编号接在已提交的 trailing 调用之后，已提交的文件不会被覆盖。

**output schema 记的是请求的那份。** `OutputFormatRef.source = "requested"`：文件内容是回复模型的 `model_json_schema()`，不是 provider 编译后真正发出的 schema（strict 与否、Gemini 的清理、union 分支的展开，各家不同；`dont_force_structured_output` 时根本不发）。它不能当作"schema 被遵守了"的证据。记录 wire 级 schema 是后续工作。

**不是 fragment 的 observation 不落盘。** 旧版把它整个写成 `NNN_observation.json` 兜底，没有任何访问器能读回；生产路径总是 `NegotiatedObservation`。这条兜底已删除，那一步的 fragment 索引为空。

**`env/` 是环境证据的子树。** 它建在 M27 的通用 extra 文件机制上（任意不撞 WAL 自有名字的相对路径、每步一个索引、内容寻址文件 run 内去重、`commit_trailing` 收尾），WAL 没有为它加任何东西。布局就是上面那棵树，语义如下：

- **只在开启时存在。** 没有绑定归档器的 run 不会有任何组件往 `env/` 写文件，目录根本不出现；绑定了但从没收集到文件的 run 也没有它。
- **步级文件归取走它的那一步。** `env/steps/NNN/` 下是第 NNN 步的提交取走的证据，路径段是全限定的 `archiver_id`（provenance）和归档器自定的 `name`。这也是 capability 侧事件的步归因：例如回放 run 的 `env/steps/NNN/browser.replay_log/replay.jsonl` 是第 NNN 步收集到的回放决定（M30，§4.14），capability 自己从不知道步号。
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
                            "screenshot_distance"]                # 也是判定 reason 的先后顺序

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

- `origin`：`policy` 是 Loop 自己的 agent 决定的一步（包括所有旧 trace 的每一步：读回来就是 `policy`，这对它们是对的）；`restored` 是分叉 run 恢复出来的前缀步（内容抄自源 run 的那一步，§4.1 `BranchLoop`）；`terminal` 是终止观测（§4.1，没有动作、没有 LLM 调用）。`AgentMetrics.steps` 与 `max_steps` 只数 `policy`。
- `restoration`：恢复出来的第 k 步带它自己的校验；分叉的策略步（第 t 步）带 obs_t 的校验，因为这一步是在那个被校验过的状态上做的决定；`strict` 下发散的那一条（`origin="restored"`、`error_phase="restore"`）带不通过的那次校验。原因的判定：capability 证据里 `document_misses > 0` → `document_miss`；`url_match is False` → `url_mismatch`；重定位 `not_found` / `ambiguous` → `retarget_*`；动作成败与录下的不同、或执行抛错 → `action_failed`；`signature_match is False` → `signature_mismatch`；`screenshot_distance > screenshot_max_distance` → `screenshot_distance`。值为 `None` 的量不参与判定。
- `AgentResult.schema_version` **不升**（仍是 `2.1`）：`StepRecord` 不在 `result.json` 里，§5.5 的"只认当前版本"守的是 `AgentResult` 自己的形状；升版会让 M29 / M30 录下的、正要拿来分叉的每一个 trace 都读不出 `result`。`manifest.format_version` 也不升：只多了带默认值的字段。
- 这两个字段都不渲染进 prompt：`StepRecord` 是 `HistoryObs` 的载荷，agent 只渲染动作、结果与回复（`test_the_stamp_does_not_reach_the_prompt` 同样钉住 `origin` / `restoration`）。

**`TraceConfig.branch`：分叉来源（M31）**。按 §5.4 "记录一个新的 run 级事实" 的步骤：

```python
class TraceConfig(BaseModel):
    ...
    branch: BranchProvenance | None = Field(None, exclude_if=<None>)   # None（不写）：不是分叉 run
```

- 由 `TaskContext.branch` 而来，`AgentRuntime.execute(branch=...)` 按名字传给 `on_run_start`，`TraceWriter` 按名字接收、原样记录，不推导（§4.2）。
- **不进 `configuration_digest`**：它是按任务变化的事实（任务 id 本身就是 `<source_run_id>/s<t>/b<i>`），同一次分叉 sweep 的所有任务共享一个 `config_hash`；sweep 级的旋钮（`continue_steps`、`branch_instruction`、teacher 的温度、恢复器的 `verify`）照常经 `resolved` 改变它。字段名不用 "provenance"：那个词已经是 `secrets_recorded` / `env_evidence_privacy` 那一层的名字。
- `source_trace` 是源 run 目录相对 sources 根的路径，加上 `source_run_id`，下游（M32 的样本渲染）才找得到源 trace 去拼第 t 步之前的历史；源 benchmark / subset / task_id 让分叉结果能连回源任务的参考答案。
- 分叉 trace 的每一步都完整记录观测（§13.4 不变）：恢复步交给 `record_step` 的是恢复出的 live 观测，终止步是最后那次观测；不提供省掉恢复步观测的 `record_observations` 旋钮。

### 5.4 TraceWriter 作为 Hook 实现

TraceWriter 不是 AgentRuntime 的核心组件，而是一个 Hook，通过 AOP Weaver 透明织入 + YAML 配置参数化：

```python
class TraceWriter(HookBase):                   # 不是 @dataclass（§4.6 "挂受体的 hook 手写 __init__"）
    __registry_key__ = "trace_writer"
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="around"),
        Intercept("record_step", role="loop", advice="after"),
    ]

    priority = HookBase.PRIORITY_PERSIST       # 所有 enricher 改完 result 之后才序列化

    def __init__(
        self,
        trace_dir: Annotated[str, trace_safe("…", default="traces", description="…")] = "traces",
        record_secrets: Annotated[bool, Field(default=False, description="…")] = False,
        archive: list[ArchiverBase] | None = None,   # 普通参数，不是 Annotated：不进生成的配置
    ) -> None:
        super().__init__()
        self.trace_dir = trace_dir
        self.record_secrets = record_secrets
        self.archive: list[ArchiverBase] = list(archive or [])   # 非空 = 本 run 的归档消费者
        ...                                    # 其余都是每个 run 在 on_run_start 里重置的内部状态

    @property
    def blocking(self) -> bool:
        return True                            # 确认落盘后才继续

    async def on_run_start(self, *, env=None, run_id=None, resolved_config=None,
                           archive_negotiation=None, env_evidence_privacy=None,
                           task_setup=None, branch=None, **kwargs):
        if self.archive:
            ...  # 三种情况在建 trace 之前拒绝（RuntimeError，点名 AgentRuntime.prepare）：
                 # archive_negotiation 为 None；它的 active_archivers 与 archive 的键不一致；env 为 None
            self._env = env                    # 原始环境，不是 AOP 代理；收尾时释放
        self._wal = WALWriter(Path(self.trace_dir), run_id or str(uuid.uuid4())[:8])
        self._wal.init_run(trace_config, config_hash=...)   # 摘要取自未脱敏的配置（§5.3）
        # trace_config.negotiation = TraceNegotiation.from_results(obs, action, archive=archive_negotiation)
        # trace_config.env_evidence_privacy / task_setup / branch：按名字接收，不推导；没有值的不写进 config.json

    # ── AOP 拦截 handler ──────────────────────────────────
    async def around_ainvoke(self, *, proceed, args, kwargs, **ctx):
        """每次 LLM 调用唯一的记录点：先登记，再 proceed()。"""
        # 开始前登记，prompt 从此不会丢：prompt = args[0]（或 kwargs["messages"]），
        # 请求的 schema = kwargs["output_format"]（或第二个位置参数）
        call = self._register_call(args, kwargs)
        started = time.perf_counter()
        try:
            result = await proceed()
        except Exception as exc:
            call.fail(exc)                     # status="failed"，CallFailure；绝不伪造回复
            raise
        except BaseException as exc:
            call.cancel(exc)                   # status="cancelled"（CancelledError 等）
            raise
        finally:
            self._pending_llm_ms += (time.perf_counter() - started) * 1000
        call.complete(result)                  # status="completed"，RecordedCompletion
        return result

    async def after_record_step(self, result, *, args, kwargs, **ctx):
        """完整 step 唯一的提交点：Loop 交出 StepRecord 时，一次 commit_step。
        以「步完成」而非「LLM 返回」为边界，是因为一步可以有多次 LLM 调用。"""
        record = args[0] if args else kwargs.get("record")
        step = int(getattr(record, "step", self._steps_written))
        self._stamp_timings(record)
        pending = list(self._pending_calls)    # 本步全部调用，按开始顺序
        try:
            batch = await self._step_archive(step) if self.archive else None
            # ↑ 拉取 → 校验（违反契约：ArchiveContractError）；之前出过洞就直接抛错
            extra, versions = self._encode_archive(batch, step=step)
            self._wal.commit_step(             # 提交抛错：这批归档文件留给收尾的 trailing（自己一次）
                step, record,
                fragments=encode_observation(step, kwargs.get("observation")),
                llm_calls=encode_llm_calls(step, pending, first_call=self._calls_flushed),
                extra_files=extra,
            )
        except Exception as exc:               # 出洞：这一步不提交，它的调用留给收尾的 trailing
            self._hole = self._hole or f"recording failed at step {step}: ..."
            if _ended_in_success(record):      # agent 就在这一步说了 done：结局已定，不抛
                return
            raise RecordingFailedError(step, exc) from exc   # run 在这一步停下
        except BaseException:                  # 取消切断了这一步的记录：不是记录失败，但这一步丢了
            self._hole = self._hole or f"recording step {step} was cut off by a cancellation"
            raise
        self._run_versions = versions
        del self._pending_calls[: len(pending)]   # 提交成功之后才移出
        self._calls_flushed = 0

    async def on_run_end(self, *, result, env, **kwargs):
        if self._closed:
            return
        cancellation = await self._close_out(result)   # 从不抛错，只交回收尾收集的取消
        if cancellation is not None:
            raise cancellation                 # 取消永远优先：终态写完之后原样重抛

    async def on_run_error(self, *, error, partial_steps, env, **kwargs):
        # AgentRuntime 在 on_run_end 之后才派发它：已经收过尾，什么也不重做、不重试。
        # 只派发 on_run_error 的驱动：按同样的规则收尾，没有 result（所以 trace 是 incomplete）
        cancellation = None if self._closed else await self._close_out(None)
        if cancellation is not None:
            raise cancellation

    async def _close_out(self, result):
        self._closed = True                    # 收尾只做一次
        cancellation = None
        try:
            tail = await self._collect_tail()  # 至多一次；出过洞就不收集
        except Exception as exc:
            tail = None
            self._close_out_problems.append(f"the close-out evidence collection failed: {exc}")
        except BaseException as exc:           # 取消：先记下，收尾照常走完
            tail, cancellation = None, exc
            self._close_out_problems.append("the close-out evidence collection was cancelled")
        try:
            try:
                self._flush_trailing(tail)     # 调用、每个遗留批次、尾部 —— 各一次 commit_trailing
            except Exception as exc:           # 写不进去的那一样丢掉，不重试
                self._close_out_problems.append(f"the close-out could not commit ...: {exc}")
            if result is None:
                self._close_out_problems.append("... so result.json was not written")
            else:
                try:
                    self._wal.write_result(result)
                except Exception as exc:
                    self._close_out_problems.append(f"result.json could not be written: {exc}")
        finally:                               # 终态不论前面怎样都试着写
            reason = "; ".join(r for r in (self._hole, *self._close_out_problems) if r) or None
            try:
                self._wal.finalize(TraceStatus.INCOMPLETE if reason else TraceStatus.COMPLETE,
                                   incomplete_reason=reason)
            except Exception:
                logger.error(...)              # 连终态都写不进去：manifest 停在 writing
            finally:
                self._env = None               # 收尾之后不再持有环境
                self._pending_calls.clear()    # 欠着的东西就此丢掉
                self._pending_archive.clear()
        return cancellation
```

**归档的写侧规则**（`archive` 非空时才生效；为空时 TraceWriter 不缓存环境、不碰任何归档方法，行为与 M27 完全相同）：

- **`archive` 是 YAML 受体（M29）。** `TraceWriter.__wiring__ = {"archive": "archiver"}`；同一个构造参数是 `Annotated[list[ArchiverBase] | None, Field(...)]`；类级的 `archive: list[ArchiverBase]` 注解放在 mixin（`_TraceWriterReceptors`）上，因为 Composer 读类级注解决定按 list 注入（§4.2.1）；`archiver` 在 `_collect_registrars()` 里。生成的配置于是是 browser 归档器配置的判别联合列表：拼错的 `archiver_id` 校验失败，每个归档器的旋钮都从 YAML 可达。归档只有这一个开关（用户 2026-10-13 的裁决）：写了列表就用它；省略、`null`、`[]` 都是不归档；`TraceWriter.__inline_only__ = {"archive"}`，所以它从不从池子里取，YAML 顶层的 `archiver:` 段在组装时被拒绝（`InlineOnlyLayerError`，报错提示把归档器写进 `trace_writer.archive`，§4.2.1）。Composer 测试钉住这几种情形。capability 块里写 `archiver:`（那是 provides 声明，没有 slot）在组装时以 negotiation-only 的报错拒绝。历史：M28 时注册表还是空的，空注册表不能成为 Composer 层，所以 `archive` 先是普通构造参数、被 `__config_annotated_only__` 挡在生成的配置之外；给它一个没有 wiring 的 `Annotated` 类型会生成 `archive: Any`、把原始 dict 交给 hook，M28 因此没有那样做。
- **`on_run_start`**：在 `init_run` 之前核对（所以拒绝时不留下 trace 目录）：`archive_negotiation is None`、`set(archive_negotiation.active_archivers) != {a.archiver_id for a in self.archive}`、`env is None`，任何一条都抛 `RuntimeError`，说明归档器必须经 `AgentRuntime.prepare()` 协商并绑定。核对通过才缓存原始 `env`。
- **`after_record_step`** 的顺序：`_stamp_timings` → 如果之前已经有过归档失败（Loop 吞掉了第一次的错误又提交了一步），这一步就记不下来：`RuntimeError` 点名收集失败的那一步，`__cause__` 是记下的那个失败，不再收集（从那以后归档已经不完整）→ `batch = await env.collect_archive()`（抛 `Exception`：记为归档失败）→ 校验批次（违反时 `ArchiveContractError`，同样记为归档失败、整批丢弃，在写任何字节之前）→ 定 run 记录版本 → `encode_archive_file(..., step=step)` → `commit_step(..., extra_files=...)`。`commit_step` 自己抛错（I/O）时，这批文件留在 TraceWriter 里等收尾。这几处的错误都是这一步的洞（见下面"出洞"那一条）：包成 `RecordingFailedError` 上抛（原错误是 `__cause__`），这一步结局已定（done）时不抛；成功之后才更新 run 记录的版本表。
- **批次校验**（`ArchiveContractError`，步提交与收尾的批次一样查）：步级文件的 `archiver_id` 必须是本 run 绑定的键之一，内容寻址的文件（响应体）也一样：用户故意没绑定的归档器，它的证据不能借 `env/blobs/` 落盘；run 记录的 `capability_id` 必须是某个绑定归档器所属的 capability（`archive_bindings` 的值），它的 `archiver_id` 是绑定键或那个裸 capability id；每个 capability 至多一份 run 记录；`archiver_id` 与 `name` 各是文件系统接受的单段（§4.9 的段规则：至多 255 字节 UTF-8），`media_type` 与 `ids` 的键和值都能编码成 UTF-8；编码后的路径在批次内按 `encoding.fold_path` 的键互不相同，不区分大小写、也不区分 Unicode 规范化形式（内容寻址的同一路径可以重复，只写一次）；每条路径都过 `encoding.check_relative_path`。`ArchiveFile` 构造时已经拒绝了其中大半，这里再查一遍：被拒绝的批次从不成为欠 trace 的东西，而因为这类原因失败的提交，每次重试都会同样失败。
- **run 记录版本**：TraceWriter 给每次收集按发生顺序编号（步提交的收集与收尾那次都算），并记着每个 capability 已提交的最新一版（版本号、sha256、它来自第几次收集）。来自更早一次收集的 run 记录不再提交，记一条日志——这只发生在遗留批次上：它在收尾才提交，之后某次收集的 run 记录可能已经落盘（提交失败之后还继续跑的驱动）；与最新一版字节相同的再发直接丢掉，但它的收集仍算作最新的采集；否则取下一个版本号（第一版是 0）。版本表只在提交成功之后生效，收尾的每一次 trailing 提交之前重新计算。
- **收尾**：只做一次（`_closed`）。`_collect_tail()` 在 `archive` 非空、之前没有出过洞、也没有收尾收集过时才收集，并立刻标记"已收尾收集"。冲刷按 §5.2：调用、每个遗留批次、尾部各自一次 `commit_trailing`，trailing 位置编号是提交前 `len(manifest.trailing)`；某一次失败只赔上它自己那一样（丢掉、不重试，§5.2），其余照常提交。然后写 `result.json`（没有 result 就不写，trace 因此是 `incomplete`），最后不论前面怎样都试着写终态：没有任何记录失败是 `complete`，否则是 `incomplete`，`incomplete_reason` 依次写第一个洞（某一步没记下来；Loop 吞掉了那个错误、最后报告成功也一样）和收尾里每一样没记下来的东西（收尾的收集失败或被取消、某一样 trailing 提交失败、`result.json` 写不进去）。这些都不抛错，只记日志：收尾时 run 的结局已经定了（§4.6）。终态本身写不进去（磁盘一直满着）记一条 ERROR，manifest 停在 `writing`。唯一上抛的是收尾收集被取消时的那个取消，在终态写完之后（§4.2 "取消永远优先"）。收尾之后 TraceWriter 不再持有环境、清掉还欠着的东西；之后的 `on_run_error` 什么也不重做；之后再有提交（排空之后还继续跑的驱动）在有归档时失败：归档已经收尾，那一步不能带着证据提交。
- **出洞**：`after_record_step` 里，这一步的拉取、校验或提交失败（`Exception`）→ 记下第一个洞（`recording failed at step t: …`），这一步不提交；这一步的记录里有 `is_done` 的动作结果（agent 在这一步说了 done，结局已定）就只记日志、不抛，否则抛 `RecordingFailedError`（`core/step_recorder.py`，原错误是 `__cause__`），run 在这一步停下（§4.6）。取消（`BaseException`）切断这一步的记录时也记一个洞（`recording step t was cut off by a cancellation`），取消原样上抛。WAL 的 `complete_through` 因为这一步没提交而停在它前面。
- **耗时记在下一步上。** 拉取发生在 `_stamp_timings` 之后，和提交 I/O 一样算进下一步的 `duration_ms`；M29 的归档开销度量单独计时。

> **Commit 粒度说明**：`around_ainvoke` 只登记 LLM 调用，不写盘；一次 step 可能包含多次调用，而动作及其结果要到调用之后才产生。Loop 将含本步 actions、action results 和 usage 的完整 `StepRecord` 交给 `record_step()` 后，`after_record_step` 才把观测、本步全部调用记录和 step record 一次交给 `WALWriter.commit_step()`；manifest 的原子更新是持久化提交点。若运行在 `record_step()` 前结束（超时、外部取消、崩溃），已登记的调用（包括被取消、只有 prompt 的那一次）在 `on_run_end` / `on_run_error` 里经 `commit_trailing()` 落盘为 trailing calls，但不冒充一个完成的 step。

**失败记录只写拿到的东西。** `CallFailure.error` 是异常类名，`detail` 是 `str(exc)`；其余字段只在异常确实带着它们时出现：

- `raw_output`：provider 交回了文本，校验器没接受（`StructuredOutputValidationError`）；
- `partial_output`：回复被 token 上限截断时已经生成的部分。`ModelOutputTruncatedError(..., partial_output=...)` 在 openai_base、anthropic_base（含序列化后的 tool_use 输入）、google 的抛出点都传入。它刻意不叫 `raw_output`，那个名字的意思是"校验器看到的原文"；
- `stop_reason` / `usage`：失败的调用照样计费。

死掉的 socket 什么也没交回，记录就只有 `error` 和 `detail`。不造一个装着错误的 completion：读 trace 的人必须分得清模型写的回复和框架编的回复。被取消的调用 `status: cancelled`，`completion` 为空，`failure.error` 是取消异常的类名（`CancelledError`），prompt 完整保留。提交或收尾时仍在进行中的调用（目前的 Loop 不会出现，调用都在 Loop 内 await）同样记为 `cancelled`，但 `failure` 为空：trace 到此为止，hook 没见到任何异常，prompt 照样保留。

**提交失败不丢调用，收尾失败不丢终态。** `commit_step` 抛错（I/O 错误）时，这一步登记的调用仍留在 TraceWriter 里，run 结束时经 `commit_trailing` 落盘。它们自己一次提交，排在任何证据批次之前，所以一批写不进去的证据连累不到它们（§5.2）。`on_run_end` 里 trailing 提交本身失败时，`result.json` 和终态照样写，终态记为 `incomplete`，原因写明（欠 trace 的调用或证据没落盘，trace 不是这个 run 的完整记录），不抛错。没写进去的那一样丢掉，不重试（§5.2）。`result.json` 自己写不进去时，终态也照样写，记为 `incomplete`：没有 `result.json` 的 trace 不是这个 run 的完整记录，不能标成 `complete`；停在 `writing` 则更糟，没有任何下游会用它。

**output schema 每个类每个 run 只算一次。** `output_format` 来自 `kwargs["output_format"]`（ReactLoop 以 `llm.ainvoke(context, output_format=output_model)` 调用）。TraceWriter 按类缓存 `model_json_schema()`，编码成内容寻址的 `messages/output_formats/<sha256>.json`，随第一次引用它的提交落盘；之后的调用只带 `OutputFormatRef`。生成不了 schema 的回复模型记为没有 schema（`output_format: null`）并打 warning，不因为 trace 让调用失败：provider 用同一个 schema 构造请求，那个失败由 provider 自己报。

**等待时间仍然只在 `around` 里量。** `_pending_llm_ms` 在 `finally` 里累加，`_stamp_timings` 在提交点把 `duration_ms` / `llm_latency_ms` 写到 `StepRecord` 上；每条调用记录另带自己的 `latency_ms`。

### 5.5 RunTrace 的 lazy loading

30MB 的 trace 不一次性读入内存。不同 judge 按需加载不同部分；只有 manifest 是 eager 读的（每步一条索引，几百字节）：

```python
class RunTrace:
    def __init__(self, trace_dir: Path) -> None: ...        # 只读 manifest.json；不是 format 3 就抛 TraceSchemaError

    @property
    def trace_dir(self) -> Path: ...
    @property
    def manifest(self) -> TraceManifest: ...
    @property
    def run_id(self) -> str: ...
    @property
    def step_count(self) -> int: ...                        # last_step_completed + 1，无 I/O

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

各访问器的语义：

- `step(idx)` / `step_records`：只看 manifest 列出的 step。列了但文件缺失抛 `TraceSchemaError`，不再静默跳过或返回 `None`。
- `observation_fragment(step, key)`：`key` 是全限定 fragment key（`browser.dom`）。该步没提交，或它的 fragment 索引里没有这个 key（本 run 没激活、这一步没记录）→ `None`。索引 `failed: true` → 按 `FragmentFailure` 读回并返回（它记录的 `fragment_id` 也必须等于 `key`）。否则用 `ObservationRegistrar.get(key)` 找到类，要求 payload 的 `fragment_id == key`，返回 `cls.from_trace(payload, artifact)`；`from_trace` 抛的 `ValueError`（以及嵌套深到解码器走不下去的 `RecursionError`，远超写侧的深度）转成点名文件和类的 `TraceSchemaError`。
- `screenshot(step)`：该步本地名为 `screenshot` 的 fragment（`browser.screenshot`、`desktop.screenshot`……，协商保证一步至多一个）的 artifact 字节。该步没提交、没有截图 fragment、或截图采集失败 → `None`；失败原因用 `observation_fragment` 读。
- `screenshots`：按 step 顺序，每个已提交 step 的截图 artifact 的路径，由 manifest 索引得出；采集失败、没有 artifact 的 step 跳过。索引列了的 artifact 缺失或为空 → `TraceSchemaError`。evaluator 继续拿路径（`online_mind2web`、`lexbench_browser` 按后缀判媒体类型）。写侧保证不会提交空 artifact：CDP 回复里没有图像数据时，`ScreenshotWatchdog` 抛错（resolver 规则"抛错，不伪造"），这一步按 optional / required 记成 `FragmentFailure` 或终止 run；仍然带着 `b""` 的 `ScreenshotObs` 不写 artifact（`trace_binary()` 返回 `None`），读回时 `image_bytes=None`。过去空截图被写成 0 字节的 `.png`，读侧把它当损坏，一帧就让整个 run 的 `screenshots` 抛错。
- `llm_messages(step)`：该步**每一次** LLM 调用，按开始顺序：parse 重试、带 `raw_output` 的校验失败、带 `partial_output` 的截断、只有 prompt 的取消。该步没提交 → `[]`；它的调用如果有，在 `trailing_calls` 里。
- `trailing_calls`：Loop 没来得及交出的那一步的调用（`manifest.trailing`），先按提交顺序、再按开始顺序。
- `extra_files(step)` / `trailing_extra_files`：已提交的 extra 文件记录，来自 `steps/NNN.extra.json` / `steps/trailing_KK.extra.json`；没有 → `[]`。
- `read_extra(path)`：只读已提交的 extra 文件。`path` 不在任何已提交的索引里 → `KeyError`（没提交和没写过，对读者是同一个事实）；读出的字节与记录的 `size` / `sha256` 不符 → `TraceSchemaError`。
- `output_schema(sha256)`：`OutputFormatRef.sha256` 指向的那份请求的 JSON schema。不在 `manifest.output_formats` 里 → `KeyError`；字节的摘要不符 → `TraceSchemaError`。
- `archive_files(step, archiver_id)`：第 `step` 步的提交取走的、属于 `archiver_id` 的步级归档文件（`scope == "step"`，含内容寻址的那些），按索引顺序，每条是 `ArchiveRecord`。该步没有提交、或它的索引里没有这个归档器 → `[]`。
- `iter_archive(archiver_id, *, include_trailing=False)`：按 step 顺序走每个已提交 step 的 `(step, record)`；`include_trailing=True` 时再按 `manifest.trailing` 顺序给出 trailing 批次里的 `(None, record)`。只含步级文件；格式检查在调用时立即做，不等到第一次迭代。
- `blob(sha256)`：一个已提交的内容寻址归档文件的字节，即 `read_extra(archive_blob_path(sha256))`：没有任何已提交的索引把它列为归档文件（`meta` 带 `"archive"`）→ `KeyError`（别的生产者放在同一地址上、不带归档元数据的 extra 文件不是归档文件）；字节与索引不符 → `TraceSchemaError`。
- `archive_run_file(capability_id)`：该 capability 已提交的 run 记录里 `run_version` 最高的那一版，跨步索引和 trailing 索引找；没有 → `None`。字节用 `read_extra(record.path)` 读。
- `archive_lines(path, model)`（M30）：一个已提交的、逐行 JSON 的归档文件（`index.jsonl`、`replay.jsonl`……），每个非空行用 `model.model_validate` 读回，按文件顺序返回；字节经 `read_extra` 读（同样的 size / sha256 校验，没提交 → `KeyError`），一行不是 JSON 或校验不过 → `TraceSchemaError`，点名文件与行号。它是通用的：行的类型由调用方给（browser 的 `ReplayLogEntry` 与网络索引行的模型都在 browser 包里，trace 层不认识它们），"读回来的必须是写进去的那个类型"对 capability 的证据也成立。`RunTrace` 由此结构化地满足 `core/archive` 的 `ArchiveReader`（§4.14 "回放的数据通路"）。
- `evals()`（M31）：这个 run 的 `evals/` 目录里每一份判定，`verdict_slug`（文件名去掉 `.json`）→ `EvalResult`，按 slug 排序；目录按排序后的顺序遍历，所以几份都坏时点名的是哪一份不取决于文件系统列目录的顺序；只看 `*.json`（原子写中断留下的 `tmp….tmp` 不算）。evals 是后置产物（`EvaluationRunner` 在 run 之后原子写入，§5.3），不在 manifest 里，所以这是唯一按目录列文件的访问器：没有 `evals/` → `{}`；目录与每个文件都必须在 run 目录之内（解析符号链接之后），否则 `TraceSchemaError`；一份不是 JSON 或不能按 `EvalResult` 校验的文件 → 点名它的 `TraceSchemaError`。不缓存：同一个 `RunTrace` 在评测写入之后再读，看得见新的判定。`trace_branches` 的 `require_eval` 经它筛选源 run（§4.10），从不读 `result.json`。
- `committed_files`（M33）：这个 run 提交了的每个文件，每被列出一次算一条（`CommittedFile{path, kind, step, fragment, extra, archive}`），按提交顺序：run 自己的三个文件（`manifest.json`、`config.json`，写了的话还有 `result.json`）；按 manifest 顺序每一步的步记录、每个 fragment 的文件（或记在它位置上的失败）与 artifact、每次 LLM 调用、extra 索引与它列出的每个文件；每个 trailing 提交的调用、索引与文件；最后是请求的回复 schema。`kind` 说文件是什么（`manifest` / `config` / `result` / `step` / `fragment` / `artifact` / `llm_call` / `extra_index` / `output_schema` / `extra`），`step` 是列出它的那次提交（run 自己的文件、trailing 提交与回复 schema 为 `None`），extra 文件带它的索引条目，是归档文件的再带 `ArchiveRecord`（与 `iter_archive` 同一个读法）。被几次提交引用的内容寻址文件，每次提交各列一次；怎么计数是调用方的规则（度量的存储每个路径计一次；归档字节的"去重前"每次列出计一次，§4.16）。每个路径列出时就过 `_listed` 的同一套检查（写侧的形状、解析符号链接之后仍在 run 目录之内，违反时点名列出它的文件），manifest 与已提交的索引没列的文件不会出现。消费者因此不必自己走 manifest 的字段、不必按名字探测 `result.json`、也不必再写一遍包含检查。
- `read_committed(path)`（M33）：`committed_files` 列出的一个文件的字节，经它自己那个访问器的检查：extra 文件就是 `read_extra`（size / sha256）；回复 schema 的字节要与它的地址相符；artifact 不能为空；每个文件都要在磁盘上、在 run 目录之内。没被列出 → `KeyError`。
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
| 归档元数据不合法 | extra 索引里一条记录的 `meta["archive"]` 不能按 `ArchiveMeta` 校验通过；点名那个索引文件 |
| 记录与位置不符 | step 文件里的 `step`、调用记录里的 `step` 与 manifest 提交它的那一步不符；一步里有两个截图 fragment（协商只允许一个） |
| fragment key 不认识 | 懒发现之后注册表里仍然没有这个 key；或 payload 的 `fragment_id` 与 key 不符 |
| `format_version` 不支持 | 不是 3（更旧、更新、或不是整数）：构造 `RunTrace` 时就抛，在校验 manifest 的其余字段之前 |

把前两者都塌缩成 `None`，会让一批旧 schema 的 trace 静默地表现为“agent 没跑完”。`AgentResult.schema_version` 只接受当前版本：旧版本字段名对不上，接受它只会换来一条指着某个被改名字段的 pydantic 报错，而不是“这个 trace 太旧”。

**一个归一化入口。** 一个 run 必有的 `manifest.json`、`config.json`，以及 manifest 列出的每个 JSON 文件（step、fragment、调用记录、extra 索引、output schema），都经 `_read_bytes` 读字节、经同一个 `_parse` 解析和校验：model 用 `_read_model(path, model)` 读，fragment 的 JSON 先读成 dict 交给 `from_trace`。解析是严格 UTF-8 解码加标准库 `json.loads`，校验是 pydantic 的 Python 模式（`model_validate` / `validate_python`）。不用 pydantic 的 `validate_json`：它的 JSON 解析器拒绝超过 200 层的嵌套，而写侧用的 pydantic-core 序列化器能写到约 254 层，DOM 编码器的深度预算本身就到约 204 层。一个嵌套一百来层的页面（正是深度截断要照顾的页面）、一个深层的结构化回复或工具结果，都能顺利提交，却永远读不回来，还被报成"Invalid JSON"。`json.loads` 只受解释器递归上限约束，远高于写侧能写出的深度；真超过了，同样转成点名文件的 `TraceSchemaError`。非 UTF-8、JSON 损坏、空文件（`UnicodeDecodeError` / `JSONDecodeError`，都是 `ValueError`）和 schema 不匹配（`ValidationError`）都由 `_parse` 转成点名文件和 model 的 `TraceSchemaError`（`path` 属性就是那个文件；format_version、fragment key 和一步两个截图 fragment 的情形是 `manifest.json`）；`_read_bytes` 把 manifest 列了而磁盘上没有的文件同样转成 `TraceSchemaError`；其余 `OSError`（权限、I/O）原样上抛，那不是 trace 本身的问题。`result.json` 是唯一的例外，因为没有谁列出它：它自己读字节，不存在就返回 `None`（唯一返回 `None` 的缺文件情形），解析和校验失败同样转成点名文件的 `TraceSchemaError`；版本不符时，报错按 `result.json` 自己声明的版本说"这个 trace 早于当前 AgentResult schema"。

**落盘文件名带 provenance。** Observation 文件名用的是**全限定 fragment key**（`000_browser.dom.json`），不是协商后的 field name（`000_dom.json`）。后者是"本次装配里哪个 capability 赢了协商"的产物：同一个 `000_screenshot.png` 在 browser run 和 desktop run 里含义不同，而读 trace 的人无从分辨——"Record Once, Evaluate Many" 的前提是 trace 自带足够信息，evaluator 不需要回头猜配置。

同一份 provenance 在数据里再写一遍：`fragment_id` 是 `FragmentBase` 上的**真实字段**（默认值来自 `__registry_key__`），不是 `@property`——property 不进 `model_dump()`，于是每个 fragment 落盘时都是匿名的。

**二进制 fragment 的后缀来自字节本身，不来自配置。** 截图的编码是 capability 配置（`screenshot_format: png | jpeg`，默认 png 与旧行为逐字节相同），但 `ScreenshotObs.trace_binary()` 是从图像头读出后缀的——一个"声明的格式"字段能和字节不一致（provider 没听配置、fragment 从磁盘文件重建），而不一致会在几层之外变成 vision provider 的 400。同理，agent 拼 `data:` URL 的 media type、artifact 落盘时的后缀、evaluator 按后缀判类型，读的都是同一个来源（`spectragent/schemas/observation/image.py`）。

**trace 格式有版本，只读当前这一版。** `TraceManifest.format_version` 写 `3`（`TRACE_FORMAT_VERSION`）。1 是 M27 之前没有这个字段的 manifest（扁平的 fragment 列表、成对的 `NNN_input.json` / `NNN_output.json`、没有判别字段的失败 JSON）；2 是 M27 起每步的 fragment 索引、每次调用一个记录、extra 索引；3 在 2 之上把 manifest 的 `status` 收窄成 trace 自己那本账（`writing` / `complete` / `incomplete`，加 `incomplete_reason` 与 `complete_through`，§5.3），agent 怎么结束只看 `result.json`。格式 2 的 `error` 既可能是"agent 失败了"也可能是"trace 没记全"，任何读者都分不出来，所以 `RunTrace` 在构造时就拒绝不是 3 的 manifest（先读版本，再校验其余字段，报错点名 manifest 与版本），连旧 trace 的配置与结果也不读。不做兼容 shim（AGENTS.md §0.4）：旧 trace 只能重跑任务（用户 2026-10-13 的裁决：旧 trace 是没有价值的测试数据）。

**离线读者找得到 fragment 类。** 评测进程未必 import 过定义 fragment 的模块（browser 的 fragment 是被各自的 watchdog import 时才注册的）。`ObservationRegistrar.get(key)` 落空时，reader 懒执行一次 `discover("spectragent.schemas")` 和 `discover("spectragent.core.capabilities")` 再查；仍然落空就抛 `TraceSchemaError`，点名 key，并说明定义它的模块必须在读侧可导入。

### 5.6 环境证据的隐私

环境证据的隐私方案（用户裁决 D4，2026-10-09：剥离凭证、表单值脱敏、归档打标、导出前审查）在这里落成框架与 capability 的分工。**框架从不读、也从不改证据的字节**：采集和脱敏是 capability 的事（§4.9），框架不声称替任何证据脱敏。框架这一侧只做三件事：记录本 run 声明了什么；提供共享的规则与台账格式，让每个 capability 用同一套；在产物层面用 secret guard 检查。

**威胁模型"只有我"（用户 2026-10-13 裁决）。** trace 与 `env/` 只在录制它的那台机器、那个账户里。框架守两道门：发给 LLM 服务商的提示词里不带秘密（敏感控件的值不进观测，§4.12）；导出数据集时把住（`env/` 永不导出，凭证扫描 fail closed，§4.15）。由此：`Set-Cookie` 原样保留（回放的 cookie jar 用它）；`storage.json` 存原值；trace 文件的权限跟 umask 走（`0o666 & ~umask`，§5.2），框架不管可见性；`task_setup` 的残余只记 TODO（下面"残余"）；键入动作在敲字时记下文字是否进了敏感控件（`typed_into_sensitive_field`），导出怎么用它随学生侧一起推迟；键入的文字本身仍原样留在动作参数与 LLM 记录里。

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
| 键入的目标 | 键入动作在敲字之前按同一条规则判定焦点所在的元素，结果记进 `ActionResult.typed_into_sensitive_field`（`true` / `false` / 看不见时 `null`），进 `steps/NNN.json`，agent 不渲染它；键入的文字原样留在动作参数里 | browser（2026-10-13）；导出的用法随学生侧推迟 |
| POST body | 按字段名模式脱敏，写入台账；`redact_post_body` 从 browser 的 `archive/redaction.py` 导出，回放时先用同一个函数脱敏再算匹配键 | browser（M29）；M30 复用 |
| 台账的规则名 | `RedactionRule` 增加 `request_header` / `input_value`；browser 的规则版本 `BROWSER_REDACTION_POLICY = "<PRIVACY_POLICY>+browser-2"` 写进 browser 台账的 `policy`，规则源码按摘要钉住（`browser-2` 删掉了 cookie 值的哈希与 `value_hash`）。词表的增删不改框架的规则函数，`PRIVACY_POLICY` 不升版本 | 框架的台账词表（`core/archive/privacy.py`，M29 扩充）；browser 规则（M29） |

**脱敏写成 `<redacted>`，不是 `"<omitted>"`。** 后者是配置脱敏（`core/trace/redaction.py`，deny by default）的"设置了、没记录"；前者是"值被这条规则换掉了"，两者回答不同的问题。`<redacted>` 也不会被 secret guard 的凭证形状误认成凭证。

**台账**（`RedactionLedger`）：`policy`（写下它时的 `PRIVACY_POLICY`）加一串 `RedactionEntry(location, field, rule)`：`location` 是 JSON pointer（`/start_url`），`field` 是被换掉的键或参数名，`rule` 是 `field_name` / `url_userinfo` / `url_query` / `url_fragment` 之一，按遍历顺序。它让读者知道哪里被换过，而不是猜一个空值是不是本来就空。

**残余，写明。** `task_setup` 是没有字段声明的按任务数据，`core/trace/redaction.py` 那种"没标记就不记"在这里会把每个值都吞掉、让分叉任务（M31）无法重建环境，所以它走值级规则。规则只认得凭证形状的名字和 URL 里的凭证位置：一个藏在规则不认识的名字下面的秘密（`{"note": "the password is hunter2"}`，或名字叫 `pass` 的键）会原样记录；嵌在非凭证 query 参数里的 URL 也一样——没有百分号编码时的 userinfo，百分号编码时里面的 `token=`。这些只记 TODO、不改代码（用户 2026-10-13 裁决，威胁模型"只有我"）。还有一条具体的：`BrowserCapability.setup` 接受 `cdp_url`，调用方若经 `env_context` 传一个 devtools URL（`ws://…/devtools/browser/<id>`），它会原样进 `task_setup`。发布的入口都不这么传（`TaskSetup.to_context()` 没有这个键，`spectragent-run` 只传 `start_url`），要覆盖它得新增规则并升 `PRIVACY_POLICY`。`TraceConfig.extra` 仍然不脱敏（调用方自己挑的字段）。v1 的数据来源选的是无需登录的公开站点；登录态采集是单独的决定。

---

## 6. conscribe 注册表总览

| 注册表 | 判别方式 | 说明 | 状态 |
|--------|---------|------|------|
| `AgentRegistrar` | `agent_id` (flat) | Agent 实现（领域知识），通过 `__wiring__` 限定可配置的 Loop / Capability 范围；实际兼容性在装配期校验 | ✅ |
| `AgentLoopRegistrar` | `loop_id` (flat) | Loop 实现（执行策略）；通过 `__required_slots__` 声明所需 agent 方法，通过 `__loop_inputs__` 声明每任务的输入（M31） | ✅ ReactLoop；M31：`branch`（`BranchLoop`，ReactLoop 子类，§4.1）；CodeAct/FunctionCalling/PlanExecute 待 P2/P3 |
| `LLMRegistrar` | `model_type.provider` (nested) | LLM provider 实现 | ✅ |
| `EnvironmentRegistrar` | `env_type` (flat) | 环境实现，通过 `__wiring__["capabilities"]` 注入 Capabilities，按前缀路由 action | ✅（仅 BrowserEnvironment；CompositeEnvironment 待） |
| `CapabilityRegistrar` | `capability_id` (flat) | 能力 = ActionGroup + Resolver 合体；持 backend wired field | ✅（仅 BrowserCapability；docker / tool_sandbox / desktop_vm / android / gpu_profiling 待） |
| `BrowserProviderRegistrar` | `provider_id` (flat) | Browser capability 的 backend：`local_chrome` / `lexmount` / `skyvern` / `browser_use` / `airtop` / `agentbay` / `hyperbrowser`。文件位于 `core/capabilities/browser/backends/` | ✅ |
| `DockerProviderRegistrar` | `provider_id` (flat) | Docker capability 的 backend（`local_docker` / `ssh_docker` ...） | P2 占位待实现 |
| `MemoryRegistrar` | `memory_id` (flat) | 记忆策略 | ✅（3 种；协议兼容性在 `prepare()` 校验，SummaryMemory 待实现） |
| `RestorerRegistrar` | `restorer_id` (flat) | 检查点恢复策略：把环境恢复到源 run 第 t 步之前的状态（`prefix_replay`；快照恢复 `capability_checkpoint` 是已知的下一个），`core/loops/restore/`；`BranchLoop.__wiring__["restorer"]` 受体，进 `_collect_registrars()`（§4.1） | M31 |
| `HookRegistrar` | `hook_id` (flat) | Lifecycle hooks (AOP) | ✅（trace_writer / metrics；live_monitor 待 P3） |
| `EvaluatorRegistrar` | `evaluator_id` (flat) | 评测策略 | 🚧 partial |
| `ExporterRegistrar` | `exporter_id` (flat) | 训练导出的格式（v1 `openai_messages`；以后 `llamafactory` / `ms_swift` / `trl`），`spectragent/export/_registrar.py`；`ExporterBase` 持导出任务字段与 `translator` 受体，运行根的可选顶层层（与 `evaluator` 同一先例），进 `_collect_registrars()`（§4.15） | M32 |
| `ActionTranslatorRegistrar` | `translator_id` (flat) | 训练导出时把 teacher 的动作空间换成学生的（`identity` / `index_to_coordinate`），`spectragent/export/translate/_registrar.py`；`ExporterBase.__wiring__["translator"]` 受体，进 `_collect_registrars()`（§4.15） | M32 |
| `TaskLoaderRegistrar` | `benchmark_name` (flat) | 任务数据加载 | ✅ |
| `StepSelectorRegistrar` | `selector_id` (flat) | `trace_branches` 加载器在源 run 的已提交策略步里选分叉点的策略：`explicit` / `every_k` / `all` / `last`（M33：每个源 run 最后一个候选，原路径回放段用，§4.16），`core/data_loader/task/step_selector/`；`TraceBranchesTaskLoader.__wiring__["step_selector"]` 受体，进 `_collect_registrars()`（§4.10） | M31；`last` M33 |
| `EvalLoaderRegistrar` | `benchmark_name` (flat) | 评测数据加载 | ✅ |
| `ActionRegistrar` | `action_id` (flat, dotted key `source.id`) | 每个 Action 独立注册；键含 source 前缀避免跨 capability 撞名（`browser.click` vs `desktop.click`） | ✅ |
| `ObservationRegistrar` | `fragment_id` (flat, dotted key `source.id`) | Observation fragment；键含 source 前缀（`browser.dom`、`docker.terminal`、`loop.action_result`）；agent / loop 的 `__wiring__` 写 capability-relative 短名（`dom`），协商期绑定到本次 environment 的 provider | ✅ |
| `ArchiverRegistrar` | `archiver_id` (flat, dotted key `source.id`) | 环境证据归档器；键含提供它的 capability 前缀（`browser.network`）。capability 的 `__wiring__["archiver"]` 声明 provides，trace hook 实例的 `archive` 列表声明 requires（全限定）；`negotiate_archive` 在 `prepare()` 校验，并在 `prepare()` 末尾绑定（§4.5） | ✅ M28 框架机制；M29：`browser.network` / `browser.storage` / `browser.dom_raw`，回放的 `browser.replay_log`（§4.14），进 `_collect_registrars()`，经 `trace_writer.archive` 由 YAML 选择 |
| `ReplayabilityRegistrar` | `replayability_id` (flat) | Browser 的回放档位（`none` / `pinned`），`core/capabilities/browser/replay/`；`BrowserCapability.__wiring__["replayability"]` 受体，策略即开关，YAML 每个 capability 块必填（§4.14） | ✅（browser 内部，M29） |
| `NetworkSourceRegistrar` | `source_id` (flat) | Browser 的网络来源（`live` 默认 / `archive` 从已录制 run 的归档离线回放），`core/capabilities/browser/replay/network_source.py`；`BrowserCapability.__wiring__["network_source"]` 受体，策略即开关——隔离由来源推出，provider 上没有并列的布尔；YAML 每个 capability 块必填（§4.14 "回放"） | ✅ M30（browser 内部） |
| `MissPolicyRegistrar` | `miss_policy_id` (flat) | 回放 miss 的应答策略（v1 只有 `strict`），`core/capabilities/browser/replay/miss_policy.py`；`ArchiveNetworkSource.__wiring__["miss_policy"]` 受体（嵌套在 `network_source` 里），`archive` 块里必填（§4.14 "回放"） | ✅ M30（browser 内部） |
| `NegotiationResultRegistrar` | `result_type` (flat) | 协商结果类型；内置 `observation` / `action` / `archive` 自动注册，自定义协商协议注册自己的结果类型并作为 `target_class` 传给 `negotiate()` | ✅ |
| `DOMCleanerRegistrar` | `cleaner_type` (flat) | DOM 清洗策略：`browser_use` / `skyvern` / `skyvern_hit_test`（M32：skyvern 加页内命中测试，§4.12） | ✅（browser 内部） |
| `DOMSerializerRegistrar` | `serializer_type` (flat) | DOM 序列化策略 | ✅（browser 内部） |
| `MouseTrajectoryRegistrar` | `trajectory_type` (flat) | 光标轨迹策略（`instant` / `windmouse` / `bezier` / `learned`）；同时是鼠标侧唯一的拟人化开关，见 §4.13 | ✅（browser 内部） |
| `TypingRhythmRegistrar` | `rhythm_type` (flat) | 键入节奏策略（`instant` / `human`）；键盘侧的同一形状，独立于轨迹轴，见 §4.13 | ✅（browser 内部） |

> **Provider 目录归属**：每个 capability 的 backend provider 文件物理上属于 `spectragent/core/capabilities/{cap}/backends/`，对应的 `*ProviderRegistrar` 也在该目录的 `_registrar.py` 里。Browser 已落地；Docker / Desktop / Android 等 P2/P3 时按同样模式增设。

> **Dotted-key flat vs nested discriminator（2026-04 订正）**：Action / Observation 原计划上 nested discriminator，实测 conscribe 在“所有 leaf 同一 source”时会命中 Pydantic 的单字段 discriminator 冲突（见 `conscribe.config.composed._build_subset_union`）。改用 *dotted-key flat*：discriminator 仍是单字段 `action_id` / `fragment_id`，但键内嵌 source 前缀（`browser.click`、`loop.action_result`），同时避免 registry 撞名和 conscribe 的 Pydantic 冲突。语义与 nested 等价，wiring 字符串、前缀路由、config 生成都不需要特殊处理。

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
| **AOP / Lifecycle Hooks** | TraceWriter, MetricsCollector — 通过 Weaver + `__intercepts__` + AOPProxy 透明织入 |
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
  - agent_id: browser_use
    # dom | dom_vision | vision —— 决定什么进模型上下文、以及有哪些点击动词。
    # max_steps / max_actions_per_step 归 loop，agent 不持有第二份。
    observation_mode: dom_vision
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
  - hook_id: metrics

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

**回放 run（M30，§4.14 "回放"；发布的完整配置在 `configs/flywheel/replay_*.yaml`）**。它与录制配置只差网络来源、T0 与 fixture 用的路由开关；源 run 目录是每个任务不同的输入，经任务上下文的 `replay_archive` 送到 `prepare()` 的检查与 setup（`spectragent-run --replay-archive DIR`；不给它，`prepare()` 就拒绝这份配置），不写在配置里：

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
      - archiver_id: browser.replay_log     # 回放必须绑定：它记下回放日志（replay.jsonl）；browser.network 在回放里被拒
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
| **新增回放档位** | 继承 `ReplayabilityBase`，实现 `ReplayabilityProtocol` 全部成员（`replayable`、`launch_args`、`device_scale_factor`、`plan`），旋钮写成 `__init__` 关键字参数；启动开关只从自己的设置生成，经 `LAUNCH_ARGS` 进浏览器（§4.14） | 以后的 `realistic` |
| **新增网络来源 / miss 策略** | 继承 `NetworkSourceBase` / `MissPolicyBase`，设 `__registry_key__`，实现 Protocol 全部成员；网络来源是不是在拦截（`intercepts`）、要不要隔离是策略自己的属性，旁边不加布尔；带受体的策略不是 dataclass、手写 `__init__`（§4.14 "回放"） | 以后的 `har`（从 HAR 回放）、`lenient`（miss 时放宽到 L2 再试） |
| **新增恢复器（检查点恢复策略）** | 继承 `RestorerBase`（`core/loops/restore/`），设 `__registry_key__`，实现 `restore(ctx) -> AsyncGenerator[RestoredStep, None]`（写成 `async def` + `yield`）：只恢复环境、不碰 memory，k < t 每步交回一条，最后一条是经校验的第 t 步观测；每个恢复出的观测恰好调一次 `env.verify_restoration`（capability 的计数类证据是自上一次调用以来的增量，§4.9）；`strict` 下第一个不通过的校验（观测、重定位或动作成败）就抛 `RestorationDivergedError`，不再执行后面的动作；不吞 `RunAbort`（`test_abort_handler_hygiene.py` 递归扫描 `core/loops/`） | 以后的 `capability_checkpoint`（Docker snapshot、Android 模拟器快照，经新增的 `RestorableCapability` 协议） |
| **新增分叉选步策略** | 继承 `StepSelectorBase`（`core/data_loader/task/step_selector/`），实现 `select(candidates) -> list[int]` | 按评测器给的关键帧选步 |
| **新增 Loop 输入** | 在 Loop 的 `__loop_inputs__` 里声明 `LoopInput(type, required)`；由任务加载器在 `TaskContext.loop` 里给出（§4.1、§4.10） | `checkpoint`（`BranchLoop`） |
| **新增分叉指令** | 在 `BranchLoop.branch_instruction` 的 `Literal` 里加一个值，在 `branch.py::BRANCH_INSTRUCTIONS` 里加它的文本（可用 `{recorded_reply}`；测试要求两者一一对应）；文本只在第 t 步写一次，不进任何校验（§4.1 "teacher 采样"） | `prefer_different_action`；每个分支不同的提示要另加一个 Loop 输入（分支序号），不是新选项 |
| **新增学生格式（训练导出）** | 一个新的 Agent 类（或已有 agent）实现 ReAct 的可选 slot `format_react_output(actions, *, thought=None)`：`parse_react_output` 的逆，写不出来的动作或参数抛 `UnrepresentableActionError`，满足 §4.15 的 round-trip；`tests/test_agents/test_format_react_output_round_trip.py` 自动覆盖每个定义了它的已注册 agent；学生的运行 YAML 就是服务时的那份 | v1 `ui_tars`；以后 Qwen3-VL 原生工具调用格式、OpenCUA |
| **新增 teacher 的 thought 读法** | teacher agent 实现可选 slot `parse_react_thought(llm_output) -> (text, source_field) \| None`（`@staticmethod`，只读回复，§4.1） | `browser_use` / `ui_tars` / `skyvern`（M32） |
| **新增导出格式** | 继承 `ExporterBase`（`spectragent/export/`），设 `__registry_key__`，实现 `format(rendered, images)`；只有自己旋钮的导出器写出完整的 `__init__` 签名（任务字段在基类签名里，conscribe 读子类的 `__init__`） | `llamafactory`、`ms_swift`（`rejected_messages` 做 DPO）、`trl` |
| **新增动作翻译器** | 继承 `ActionTranslatorBase`（`spectragent/export/translate/`），实现 `translate(sample, trace) -> Translation`：只做与学生无关的换算，capability 专属的知识放在 capability 的包里由它调用；输出过 `Params.model_validate` | 以后的 `bbox_to_point`、desktop 的 a11y 节点 → 坐标 |
| **新增度量 / 报告节（M33）** | 在 `spectragent/flywheel/metrics.py` 加一个只读 `RunTrace`（与数据集目录）的计算、在 `report.py` 加它的节模型与建议规则（阈值写成具名常量、进 `rule`）；分母为 0 时是 `null` 加原因；capability 证据的格式知识放进该 capability 的包（纯函数，只经 `ArchiveReader` 读），度量只组织调用（§4.16） | 每条数据线的 miss 成本；第二条数据线出现时每个 capability 的度量贡献成为一根轴 |
| **新增 thought 来源（`relabel`）** | 今天是 `thought_source: Literal["teacher", "none"]`；`relabel`（固定动作、采样 K 个 thought、保留一致的）有了实现时成为新注册表（§6 与 AGENTS.md §2.2 同时加行） | OpenCUA、arXiv 2504.10127 的做法 |
| **新增 Mouse Trajectory / Typing Rhythm 策略** | 继承 `MouseTrajectoryBase` / `TypingRhythmBase`，设 `__registry_key__`，实现 Protocol 全部成员（含 `click_hold_ms` / `aim_offset_px`），knobs 写成 `__init__` 关键字参数 | 新的运动模型；已训练模型的新导出格式属于 `learned` 的 backend，不是新策略（§4.13） |
| **新增注册表（全新变化轴）** | `create_registrar(...)` + `Base(metaclass=...Meta)`；**同一次改动**里把行写进 §6 与 AGENTS.md §2.2（`tests/test_docs/test_registrar_coverage.py` 会核对，判别字段必须与 `discriminator_field` 逐字一致）；若这根轴要由 YAML 选择，还须在 `scripts/generate_all.py::_collect_registrars` 注册，否则字段退化成 `Literal[<keys>]`，策略的旋钮全部无法从 YAML 到达（AGENTS.md §3.4）。注册表还是空的时候不能成为 Composer 层（`composer.py` 对没有实现的已收集层直接抛错，`generate_all` 跳过它，fingerprint 测试随之失败）：先创建、先写文档，在注册第一个实现的那次改动里再收集 | 滚轮节奏、触屏手势。反例：`NegotiationResultRegistrar` 不由 YAML 选择，因此正确地不在 Composer 里。`ArchiverRegistrar`：M28 创建，M29 随首批归档器收集 |
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
       │                         MetricsCollector 累加 token       │
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

**导出相关的 import 方向**（M32，`tests/test_export/test_export_boundaries.py` 用 AST 守着）：`spectragent/` 下 `export/` 之外没有任何模块 import `spectragent.export`（`scripts/generate_all.py` 的收集、以及它生成的 `generated/_composed.py` 引用导出旋钮类型除外）；`export/*` 经 `core.trace.reader.RunTrace` 读源 trace，经注册表与 Protocol 拿到导出器与翻译器（`runner.py` 不 import 具体的导出器或翻译器类）；`schemas/sample.py` 不 import `spectragent.core`；browser 一侧的导出知识（`index_points.py`、`capture_check.py`、`dom/cleaning/skyvern_hit_test.py`）只读交进来的 fragment、文档与配置，不 import `core.trace` / `spectragent.export`；新模型里没有 `price` / `cost` / `usd` / `dollar` 字段。导出的叶子规则有且只有一个例外：度量闸门的链（`spectragent/flywheel/chain.py`）把 `spectragent-export` 当作一段来调，只 import `spectragent.export.cli`（测试按模块名钉住）。

**度量相关的 import 方向**（M33，`tests/test_flywheel/test_flywheel_boundaries.py` 用 AST 守着）：`spectragent/` 下 `flywheel/` 之外没有任何模块 import `spectragent.flywheel`；`flywheel/*` 经 `core.trace.reader.RunTrace` 读 trace、经 `core/trace/sources.py` 找 run 与任务身份，从 `spectragent.export` 只有 `chain.py` 的那一个例外（`metrics.py` / `report.py` 读数据集只读文件，不 import 导出）；browser 一侧的度量知识（`archive_stats.py`、`replay_log.iter_decisions`、`archive_index.iter_index_lines`）只经 `ArchiveReader` 读，不 import `core.trace` / `spectragent.flywheel`；报告与链的模型里没有 `price` / `cost` / `usd` / `dollar` 字段。

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
│   │    MissPolicyRegistrar：strict；canonical / matcher / site_rules / archive_index / responder / server，§4.14）
│   ├── 归档器 archive/（browser.network / browser.storage / browser.dom_raw / browser.replay_log + 环境指纹，§4.14）
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
`switch_tab()` 懒附加走同一路径，`close_tab()` 和 teardown 成组释放。

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
| **P2** | 数据飞轮（browser）：离线回放（M30）；状态分叉——`BranchLoop` + `RestorerRegistrar`（`prefix_replay`）+ `RestorationSupport`（browser 重定位与校验）+ `trace_branches` 加载器与 `StepSelectorRegistrar` + `terminal_observation`（M31）；训练导出——`ExporterRegistrar`（`openai_messages`）+ `ActionTranslatorRegistrar`（`identity` / `index_to_coordinate`）+ `CanonicalStepSample` + 学生渲染（ReAct 可选 slot `format_react_output` / `parse_react_thought`）+ 校验器 + `spectragent-export` + `skyvern_hit_test` cleaner（M32）；度量闸门——`spectragent/flywheel/`（度量、报告、链）+ `spectragent-flywheel` + 选步 `last` 与原路径回放配置 + 表单提交的观测修复（M33） |
| **P3** | FunctionCallingLoop + ToolSandboxCapability |
| **P3** | PlanExecuteLoop |
| **P3** | GPUProfilingCapability（Docker + bare-metal harness split 架构） |
| **P3** | DesktopVMCapability / AndroidCapability |
| **P3** | 包重命名 `browseruse_bench` → `all4one` |

---

## 13. 已决定的设计问题

### 13.1 LLM 所有权（已决定）

**LLM 作为简单 IoC**：Agent 通过 `__wiring__` auto-discovery 引用全部 LLM（`"llm": "llm"`），用户在 YAML 配置即可。AgentRuntime 从 config 创建 LLM 实例，注入 Loop.run()。每个 agent 可独立配置不同 LLM（实验场景）。真有 agent 对 LLM 有限制再通过 `__wiring__` 收窄。

### 13.2 Memory 协商（已决定）

Agent 通过 `__wiring__` 选择并注入 Memory 策略，配置校验覆盖可选实现及其子配置。Loop 声明需要的消息协议（`user_assistant` / `tool_call` / `planning` 等），Memory 声明支持的协议；`AgentRuntime.prepare()` 检查 `Loop requires ⊆ Memory supports`，不把组件行为兼容性委托给配置生成。详见 §4.8。

### 13.3 Observation 二次协商（已决定）

**两层检查，职责不同**。配置校验保证 wiring 引用的 fragment 键和子配置有效；选定 Agent、Loop、Environment 后，`AgentRuntime.prepare()` 执行三方协商，检查 required 是否由本次环境提供，并在失败时点名请求方和缺失 fragment。配置有效不代表组件组合在行为上兼容。

### 13.4 Observation 完整序列化（已决定）

完整序列化每步的 NegotiatedObservation。理由：存储不值钱，API 和时间值钱。完整 trajectory 数据可直接用于下游 RL 训练。TraceWriter Hook 序列化时利用 ObservationRegistrar 的 discriminated union 能力。分叉 run（M31）同样如此：恢复出来的前缀步与终止观测都完整记录（§5.3），不提供为省存储而跳过恢复步观测的旋钮：体积不是不记 agent 所处环境的理由。

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

### 14.3 LLM 客户端的生命周期

provider 为每个请求建一个 SDK 客户端、请求返回前关掉（§4.7），所以请求之间不复用 TCP/TLS 连接；
`ChatGoogle` 走 Vertex 且没给 `credentials` 时，每个新客户端还要重新加载一次 ADC、刷新一次 token。
按 run 持有一个客户端能省掉这些，但得有人在 run 结束时关它：例如 `BaseChatModel` 加 `aclose()`，由持有
LLM 实例的一方（组装出它的 run——agent 的 `llm`、动作的 LLM、评测器的 judge）在每条结束路径上调用。
这是协议改动，待定。在它之前，按实例缓存客户端而不关是不可取的：对带 `__del__` 的 SDK，那正是 §4.7 描述的连接卡死。
