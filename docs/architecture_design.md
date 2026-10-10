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
    ctx: dict[str, Any]   # weave 后绑定的 runtime deps

    def weave(self, agent: Any, **deps: Any) -> Any: ...
    async def _execute(self, agent: Any, task: str, **kwargs: Any) -> Any: ...


class AgentLoopBase(metaclass=AgentLoopRegistrar.Meta):
    """所有 Loop 实现的基类。"""

    __required_slots__: ClassVar[set[str]] = set()
    __optional_slots__: ClassVar[set[str]] = set()

    def _validate_slots(self, agent: Any) -> None:
        """callable(getattr(agent, slot)) — 缺 slot 即 raise IncompatibleError"""

    def weave(self, agent: Any, **deps: Any) -> Any:
        self._validate_slots(agent)
        self.ctx = deps
        loop = self
        async def run(task: str, **kw: Any):
            return await loop._execute(agent, task, **kw)
        agent.run = run                # instance-level monkey-patch
        return agent
```

具体 Loop 子类示例：

```python
class ReactLoop(AgentLoopBase):
    __registry_key__ = "react"
    __required_slots__ = {"build_react_prompt", "parse_react_output"}
    __optional_slots__ = {"get_react_output_model", "prepare"}

    async def _execute(self, agent, task, **_):
        # 通过 self.ctx 拿到 weave 时绑定的 deps
        env = self.ctx["env"]
        llm = self.ctx["llm"]
        memory = self.ctx["memory"]
        act_fn = self.ctx["act_fn"]
        # ... observe → build_prompt → LLM → parse → act 循环 ...
```

#### Loop 实现现状与规划

```
AgentLoop (Protocol)
│
├── ReactLoop (__registry_key__ = "react")  ← ✅ 已实现
│     ├── slots: {build_react_prompt, parse_react_output}
│     │           opt: {get_react_output_model, prepare}
│     └── 循环：observe → build_react_prompt → LLM → parse_react_output → act
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
    __optional_slots__ = {"get_react_output_model", "prepare"}

    async def _execute(self, agent, task, **_):
        # weave() 已经把所有 deps 绑到 self.ctx
        env = self.ctx["env"]
        llm = self.ctx["llm"]
        memory = self.ctx["memory"]
        act_fn = self.ctx["act_fn"]                # guarded_act，见 4.5 节
        negotiated_obs = self.ctx.get("negotiated_obs")
        active_descriptors = self.ctx.get("active_descriptors", [])
        session_id = self.ctx.get("session_id")

        # 可选 prepare slot — agent 自己 seed system prompt
        if callable(getattr(agent, "prepare", None)):
            agent.prepare(task=task, action_descriptors=active_descriptors)

        for step in range(self.max_steps):
            # AOP hooks 在方法调用时自动触发 — Loop 完全不知道有 hook 存在
            obs = await env.observe(negotiated_obs, session_id)
            messages = agent.build_react_prompt(obs)
            completion = await llm.ainvoke(messages)
            actions = agent.parse_react_output(completion.completion)
            for action in actions:
                result = await act_fn(action, session_id=session_id)
                if result.is_done:
                    return ...
```

**Loop 反向"安装"到 agent**：调用方拿到的不是 `loop.run(agent, ...)`，而是 `agent.run(task)`——`AgentLoopBase.weave(agent, **deps)` 在执行前把 `agent.run` monkey-patch 成一个调 `loop._execute(agent, task)` 的闭包，并把 deps 绑到 `loop.ctx`。这样从外部看 agent 是一个自包含的可执行单元（`await agent.run(task)`），而 Loop 只是它内部的"编排策略"——这与"Loop 本质就是在调度 agent 的 method"的语义最贴近。

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
    def prepare(self, *, session_id=None, task_id="unknown",
                action_filter=None, **kwargs) -> Any:
        run = self._run
        agent, loop, env, llm, memory = (
            run.agent, run.loop, run.env, run.llm, run.memory
        )

        # 1. Slot 校验：loop.__required_slots__ ⊆ agent 的 callable 方法
        loop._validate_slots(agent)

        # 2. Observation 三方协商（见 §4.4）
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
        self._env_evidence_privacy = sorted({m for a in archivers for m in a.privacy_markers})

        # 4. AOP 织入 — Weaver 包装四个角色：agent / env / llm，以及 StepRecorder（role="loop"）
        weaver = Weaver(self._hooks)
        wrapped_agent = weaver.wrap(agent, role="agent")
        wrapped_env = weaver.wrap(env, role="env")
        wrapped_llm = weaver.wrap(llm, role="llm")
        wrapped_recorder = weaver.wrap(self._recorder, role="loop")  # record_step → 一次提交完整 step
        weaver.raise_for_unclaimed_roles()

        # 5. 收窄 action space + guarded_act
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
            active_descriptors=active_descriptors,
            step_recorder=wrapped_recorder,
            session_id=session_id, task_id=task_id, **kwargs,
        )

        # 7. 拦截点审计 —— 必须在 weave 之后：agent.run 到这一步才存在
        weaver.audit_intercepts()

        # 8. 绑定归档器 —— prepare 的最后一步，作用在原始 env 上（不经 AOP 代理）
        if negotiated_archive is not None:
            env.bind_archivers(archivers)
        return wrapped_agent

    # ── execute(task): 一站式驱动 ───────────────────────────────────
    async def execute(self, task, *, timeout=300, config=None,
                      env_context=None, raise_on_error=True, **kwargs):
        prepared_agent = self.prepare(**kwargs)

        # 在 env setup 之前打开 trace：setup 失败也要落进 trace
        await self._dispatch_lifecycle("on_run_start",
            agent=self._run.agent, env=self._run.env,          # env：原始环境，不是 AOP 代理
            task=task, config=config or {}, ...,
            archive_negotiation=self._negotiated_archive,      # 没有归档消费者时为 None
            env_evidence_recorded=self._negotiated_archive is not None,
            env_evidence_privacy=self._env_evidence_privacy,   # 有序、去重的隐私标记
            task_setup=env_context)                            # 交给 setup_environment 的那份；没有时为 None

        self._finish_started = False
        try:
            try:
                await self.setup_environment(env_context)
            except Exception as exc:
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
                    result = ...   # agent_done=ERROR，error 点名否决并注明超时
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
                result = ...   # agent_done=ERROR
                if not isinstance(exc, RunAbort):
                    await self._finish(result, error=exc)
                    if raise_on_error:
                        raise
                    return result
                await self._close_out_vetoed(result, exc)   # guard 的否决：总是上抛，不看 raise_on_error
                raise
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

**为什么两段式**：`prepare()` 是纯装配（slot 校验 → 协商 → AOP 织入 → loop weave），返回值就是一个 self-contained 的 agent —— 测试和需要细粒度控制的调用方可以直接 `await prepared_agent.run(task)`，跳过 timeout/lifecycle 包装。`execute()` 只是 prepare + 围绕 `agent.run` 加 timeout 和 lifecycle hook。两段都是独立的、零全局状态的 run 上下文，并行多个 run 互不干扰。

**归档：先校验，最后绑定。** 归档轴（§4.5）的协商与绑定分在 `prepare()` 的两头：

- `negotiate_archive` 紧跟 `negotiate_memory`，在任何织入之前运行，所以绑定错误（`IncompatibleArchiveError`）和另外几条协商一样在装配期暴露——`spectragent-run --dry-run` 也调 `prepare()`，干跑就能发现；
- `env.bind_archivers(...)` 是 `prepare()` 的最后一条语句，在拦截点审计之后：capability 的状态一旦改变，后面就不再有会失败的装配检查；
- 它仍然在 `on_run_start` 和 `setup_environment` 之前，因为 capability 在 setup 时就要决定建不建自己的归档采集通道（browser 的 archive scope，M29）。`bind_archivers` 不获取任何资源：干跑也会走到这一步，而干跑从不 setup；
- 没有任何 hook 声明非空的 `archive` 时，runtime 对 environment 不做任何归档调用——没有这些方法的 environment（测试里的 stub）照常工作；
- runtime 只认 `ArchiveConsumer` Protocol（`core/archive/protocols.py`），从不认识 `TraceWriter`。

协商结果留在 runtime 上：`negotiated_archive` 与 `negotiated_observation` / `negotiated_actions` 并列，`prepare()` 之后可读，没有归档消费者时为 `None`。它连同 `env_evidence_recorded`、`env_evidence_privacy`、`task_setup` 一起按名字传给 `on_run_start`：runtime 是唯一同时握有这些事实的组件，由它解析，hook 只按名字接收、不自己推导（记录到哪些 `TraceConfig` 字段见 §5.3）。

**每条终止路径都派发 lifecycle，外部取消也一样。** `on_run_start` 之后，done / max_steps / timeout / 异常 / 外部取消都经 `_finish` 收尾：先以一个真实的 `AgentResult` 调 `on_run_end`，有异常再调 `on_run_error`，最后 drain 后台 handler；env teardown 留在 `finally` 里，永远在 `on_run_end` 之后（env-state 打分要活着的环境）。外部取消（`asyncio.CancelledError`：Ctrl-C、sweep 被取消）构造 `agent_done=ERROR`、`error="Execution cancelled"` 的结果，`_finish` 之后**原样重抛**，调用方的 `asyncio.timeout` / `TaskGroup` 语义不受影响；取消若在 `_finish` 已经开始之后才到，不再二次派发。缺了这一条，被取消的 run 没有 `result.json`，manifest 永远停在 `running`，进行中那次 LLM 调用的 prompt 也落不了盘（§5.4）。进程被直接 kill 不在此列：那要求 prompt 预写（write-ahead），目前不做。

**收尾只做一次，取消永远优先。** 被取消的 run 收尾时，blocking hook 自己失败了（`TraceWriter.on_run_end` 遇到磁盘写满），这个错误只记日志，重抛的仍是原来那个 `CancelledError`：否则 Ctrl-C 会变成一个普通的 `OSError`，sweep 的 `except Exception` 把它记成一次任务失败、接着跑下一个任务，调用方的 `asyncio.timeout` 也只会看到 `OSError` 而不是 `TimeoutError`。被否决的 run 同理（下一段）。其余路径（完成、超时、其他异常、setup 失败）上收尾失败，hook 的错误照原样上抛，不看 `raise_on_error`；run 不会被再收尾一次。成功路径的 `_finish` 因此放在 `try` 的 `else` 里：放在 `try` 之内时，hook 在 `on_run_end` 里抛的错会落进 `except Exception`，同一个 run 被当成失败再收尾一次，每个 hook 都收到两次 `on_run_end`。

**否决总是上抛，记录失败顶替不了它。** `execute` 只看从 Loop 里出来的是不是 `RunAbort`。Loop 重抛否决之前先把被否决的那一步交给 `record_step`；那次记录若失败（归档收集坏了、磁盘满），记录失败绝不能取代否决从 Loop 里出来——否则 `raise_on_error=False` 会把它当成普通失败吞成结果，sweep 接着跑下一个任务。Loop 把它作为 note 附在否决上照样重抛；基础设施失败的终止结果同理，`error` 仍然点名根因（§4.6 "记录失败不顶替 run 已经定下的结局"）。收尾失败同样顶替不了它：磁盘一直满着时，`TraceWriter` 收尾要把这一步还登记着的调用（归档时还有遗留批次和尾部）作为 trailing 提交，照样失败，`on_run_end` 抛出这个错误。`execute` 对否决和对取消一样处理（`_close_out_vetoed`）：`_finish` 抛出的 `Exception` 只记日志，并作为又一条 note 附在否决上，重抛的仍是 `RunAbort`，sweep 照样停下。取消落在否决的收尾里时，取消优先：`CancelledError` 是 `BaseException`，这一层不接它。

**run 自己的超时顶替不了已经定下的结局。** 优先级从高到低：外部取消 > 否决 > run 自己的超时、收尾失败、记录失败。Loop 在结局离开之前要先记录 run 结束在的那一步，这次交接可能要等——归档时 `TraceWriter` 正是在这里收集环境证据，收的恰好是 guard 拒绝的那个重页面。run 的超时（`wait_for`）若在这时到期，取消落在 Loop 里。Loop 不吞取消（它也可能是 Ctrl-C 或调用方自己的 `asyncio.timeout`），可是只原样重抛的话，`wait_for` 把它变成 `TimeoutError`，`execute` 返回一个普通的超时结果——不看 `raise_on_error`——sweep 记下一个超时任务、接着对 guard 刚拒绝的站点跑下一个，日志和 trace 里都找不到 guard 说过停。所以 Loop 重抛的是同一个取消，但让它带上结局（`core/loops/errors.py`：`carry_outcome` 把一个 `RunEnded` 放到取消的 `__context__` 链最前面；在 `except RunAbort` 里等待时，否决本来就在那条链上）。`execute` 只在 `except TimeoutError` 里读这条链（`carried_outcome`，经 `wait_for` 的 `TimeoutError.__cause__`，3.11 与 3.12 一样）：带着否决，就照否决收尾——`agent_done=ERROR`，`error` 点名否决并注明超时在记录被否决那一步时到期，超时作为 note 附在 `RunAbort` 上——然后重抛它，不看 `raise_on_error`；带着 Loop 已建好的终止结果（基础设施失败、动作失败、最后一次容许的解析错误），就照这个结果收尾并返回它，`error` 仍点名根因，后面注明记录被 run 的超时切断——而不是把死掉的浏览器记成"模型太慢"。只有切断的是一个还在进行的 run，结果才是 `agent_done=TIMEOUT`。外部取消不读这条链，照旧原样重抛：结局只留在它的 traceback 里。ReactLoop 的做法见 §4.6 "记录失败不顶替 run 已经定下的结局"，新 Loop 的义务见 `af-add-a-loop` guide §3.3。

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
- 不在 init_params 也不在 class_hints → 纯协商声明（没有 slot，如 agent / loop 的 `observation:`、`action:`，capability 的 `observation:` 与 `archiver:` provides 声明）：conscribe 把它生成成可选字段，Pydantic 照样校验它的 `Literal[...]`；**但 YAML 一旦填了值，Composer 抛 `UnresolvedWiringError`**（"negotiation-only entry"）——没有任何地方接收这个值，静默丢掉它正是这条报错要堵的
- 可选的 list / dict 受体在 YAML 里**省略**时不等于空：Composer 在所有实例建完之后的第二遍（`_resolve_deferred`，deferred resolution）把它绑定到所连注册表那一层的**全部**实例（wiring 条目列了 key 时只取被允许的那些）。后果之一落在 `trace_writer.archive` 上（M29 起 YAML 可达）：省略它、而配置里有顶层 `archiver:` 段时，归档会被打开；"不归档"是写 `archive: []`，或者根本没有顶层 `archiver:` 段。M29 用 Composer 测试钉住这三种情形

**注入方式**：init param → 进 `__init__` 的 kwargs；class_hints-only → 通过 `setattr(instance, name, value)` 注入（conscribe `WiredField` descriptor 接受这次写入并把值放到 instance dict，遮蔽自身）。

**与 AgentRuntime 的边界**：`AgentRuntime` 只接收 `ComposedRun`，**不**直接看 conscribe schema、**不**自己解析 YAML。`ConfigComposer` 是唯一懂 conscribe 内部约定（receptor 检测、dict/list 形态、wiring 解析）的层。增删 wiring grammar、调整 discover 路径、加新 layer 都只动 Composer。

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

    # 生命周期 — setup 的 context 按 capability 路由，不广播（capability 自己内部调 backend.open/close）；
    # teardown 在 setup 半途失败、或根本没有 setup 之后调用也必须安全
    async def setup(self, context: Mapping[str, Any] | None = None, *,
                    negotiation: Any = None) -> None: ...
    async def teardown(self) -> None: ...

    @property
    def is_alive(self) -> bool: ...
```

**三条 provides 轴在 Protocol 上一样声明。** `provided_actions` 过去只在 `EnvironmentBase` 上，`negotiate_action` 却一直从环境上读它：结构化实现 `Environment` 的人照着 Protocol 写，类型检查通过，到协商时才失败。M28 把它补进 Protocol，第三条轴从第一天起就三样一起声明（`provided_archivers`、`bind_archivers`、`collect_archive`），不再复制那个不对称。代价写明：`Environment` 是 `@runtime_checkable`，多了这几个成员，一个没有归档面的结构化环境（测试里的 `SurfacelessEnv`）`isinstance(env, Environment)` 就变成 `False`。代码里没有任何地方做这个判断——runtime 只在有归档消费者时才碰这三个成员——以后也不要靠它分辨环境，否则不归档的 run 对 env 零调用这条保证就破了。

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
- `bind_archivers(archivers)`：按每个键的第一段经 `_by_capability_id()` 找 capability（与 `act()` 同一套路由）。下面任何一种情况都记一条问题，全部收集完一次抛 `UnroutableArchiverError(problems, capability_ids)`（`problems`：键 → 原因，按键排序写进消息；`capability_ids`：本环境的 capability id），抛错时没有任何 capability 被调用：前缀对应不到 capability；对应的 capability 不实现 `ArchivingCapability`；键不在该 capability 的 `provided_archivers` 里。校验通过后，**每个** `ArchivingCapability` 都收到自己的那一组——分不到的收到空列表，这会清掉它之前的绑定；收到非空组的 capability 按 capability 顺序记进 `_archiving`。绑定不获取任何资源（它可能发生在干跑里、setup 之前）。
- `collect_archive()`：什么都没绑定时直接返回 `ArchiveBatch()`，一个 capability 也不碰。否则每个绑定了的 capability 一个任务**并发**收集；每个任务都跑完才裁决结果，不留孤儿任务。一个 capability 抛 `Exception`、交回的不是 `ArchiveBatch`，或交回的文件里有 `capability_id` 不是它自己的（后两条记成点名该 capability 的 `ArchiveContractError`，最后一条还点名那些 `archiver_id`），都算它失败；有任何失败就抛 `ArchiveCollectionError(env_type, failures)`——`failures` 是 capability id → 异常，消息按 capability 顺序点名每一个，`__cause__` 是按 capability 顺序的第一个失败；健康 capability 交回的批次随之丢弃（run 反正要失败，§4.6）。没有失败时按 capability 顺序把各批次合并成一个 `ArchiveBatch`。`CancelledError` 原样传播——包括某个 capability 自己抛出它的情形：`gather` 这时会让兄弟任务接着跑，所以先取消还没结束的任务、等它们收场，再原样重抛，同样不留孤儿任务。
- 收集不带任何参数：environment 从不把步号（也不把 cycle）交给 capability。"第 t 步的提交取走的就归第 t 步"是记录器那一侧的规则（§4.6）。

**Observation cycle 由 env 宣告，不由 capability 推断**：`Environment.observe` 持有单调递增的 cycle 计数器，每次 observe 自增一次，并在任何 fragment 解析之前调用每个参与 capability 的 `begin_observation(cycle)`。这正是 capability 敢批量抓取的前提——browser 一次 `BrowserStateRequestEvent` 同时拿到 DOM + screenshot + tab list，然后在本 cycle 内把每个 key 服务任意多次，因为下一个 cycle 会被**告知**而不是被**猜到**。此前这条规则只以"我是否已经服务过这个 key"的形式活在 `BrowserCapability` 内部：那是用推断代替调用方本来就握有的信息，并且对任何读同一 fragment 两次的调用方都是错的。同一步内重复读同一个 fragment 是**同一次** observation，不是下一次。

**全限定 dotted key**：每个 action 的 `__registry_key__` 是 `"<source>.<id>"`（如 `browser.click`），所以无论 single-cap 还是 composite env，`action.name` 都已经带前缀，`act()` 只做路由不做改写——单 cap 场景也透明工作。Composite 场景（如 GAIA）的 `browser.click` / `tool_sandbox.call_tool` 通过同一段路由分发到对应 capability。

**但"声明"不用全限定键（capability-relative wiring）**：注册 key、`ActionDescriptor.name`、`act()` 路由、trace 文件名全部保持全限定——上面那段路由一个字都不改。唯一让出前缀的是 **agent / loop 的 `__wiring__` 声明面**：写 `"click_coordinate"` 而不是 `"browser.click_coordinate"`。理由是同一个字符串原本被要求同时承担三件互不相干的工作——(a) 注册表全局唯一、(b) 运行时分发路由、(c) agent 声明自己需要什么。(a)(b) 确实需要 capability 前缀，(c) 不需要："我会按坐标点击"这句领域知识天生与 capability 无关。让 (c) 继承前缀的后果是每个 agent 被焊死在一个 capability 上——UI-TARS 是 computer-use 模型，自己的 config 提供 COMPUTER_USE / MOBILE_USE / GROUNDING 三种模式，却只能对着 `browser` 协商。

- **展开**：`ActionRegistrar` / `ObservationRegistrar` 用 `key_separator="."` 创建，conscribe（≥1.3.0）在 config 生成期把不带分隔符的 wiring key 展开成所有尾段匹配的全限定 key，于是生成的 `Literal[...]` 覆盖每个可能的 provider。conscribe 只做**加宽**，不做**裁决**——生成期它不可能知道某次装配持有哪些 capability。
- **裁决**：协商期收窄。`negotiate_action` / `negotiate_observation` 在声明空间里跑集合运算，再把每个声明 key 绑定到本次 environment 里唯一提供它的全限定 key。绑定表（short → 全限定）只在协商期算一次，挂在协商结果上（`action_bindings` / `fragment_bindings`）；下游一律读表，不得自己再推一遍。
- **消歧靠拒绝**：一个 relative key 若被本 environment 里两个 capability 同时提供（`browser.screenshot` vs `desktop.screenshot`），抛错并把两个候选全限定名都报出来。按 wiring 列表顺序挑一个、或让 agent 声明 capability 亲和度，都被明确否掉：前者是静默降级，后者把 F10 刚拆掉的耦合又装回去。
- **逃生舱**：在 wiring 里直接写全限定 key 即钉死 provider（含消歧场景），这也是上面那条报错给出的修法。
- **收窄发生在唯一一处**：`build_guarded_act`。它本来就包住了每一个 action，所以 agent 吐出的 relative name 在进 `env.act` 之前就已经变回全限定名；此后整条链路（路由 / descriptor / WAL）只见全限定键。

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
loop-sourced（如 action_result）              → 始终激活，Loop 自行填充
```

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
| 注册单元 | 每个 fragment 一个类 (`DOMObs` / `ScreenshotObs` / ...) | 每个 action 一个类 (`ClickAction` / `ScrollAction` / ...) | 每个归档器一个类（M29：`browser.network` / `browser.storage` / `browser.dom_raw`） |
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

**唯一的不对称**：requires 来自 trace hook **实例**的配置，而且永远全限定。记录什么是每次 run 的选择（用户裁决 D3，2026-10-09）——它既不是某个 agent 类的领域知识（§4.4："模式决定模型看到什么，不决定 run 记录什么"），也不是记录器类的固有属性。所以没有类级 `__wiring__` 需求，也就没有 relative key 要绑定：`ArchiveConsumer.archive` 里每个归档器的 `archiver_id` 本身就点名了 provider。

**协商规则**（`core/negotiation/archive.py`）。`negotiate_archive(consumers, env)` 接收的是 runtime 筛出来的、`archive` 非空的 `ArchiveConsumer` 对象，不是一个扁平的归档器列表——请求方是谁、同一个键是不是跨消费者重复，只有从消费者那里才看得出来。`consumers` 为空是编程错误（`ValueError`），`archive` 里的元素不满足 `Archiver` Protocol 同样是编程错误（`TypeError`，点名消费者与元素类型）。消费者按身份（`is`）比较，不按相等：同一个对象列两次算一个消费者，两个相等的不同对象仍是两个。环境一侧的事实是 `env.env_type`，以及 `env.capabilities` 里每个 capability 的 `(capability_id, provided_archivers)`——不实现 `ArchivingCapability` 的 capability 视为一个也不提供；报错里"环境提供了什么"是这些集合的并集（按构造等于 `env.provided_archivers`）。请求方标签用 `demand_label(consumer)`（`trace_writer`）。每个请求的键按第一段找 capability，该 capability 必须提供这个键。下面的问题**全部收集完一次报出**，每条一行，按字典序排序，合成一个 `IncompatibleArchiveError`：

| 问题 | 消息（逐字） |
|---|---|
| 前缀对应不到 capability | `{req} requests archiver {key!r}, but environment {env!r} has no capability {prefix!r} (its capabilities: {cap_ids}).` |
| capability 不提供这个键 | `{req} requests archiver {key!r}, but environment {env!r} provides archivers: {env_provided} (capability {cap!r} declares {cap_provided}).` —— `{cap_provided}` 为空时写 `none`，否则是有序列表 |
| 同一个键在一个列表里出现两次 | `{req} lists archiver {key!r} more than once; an archive list names each archiver at most once.` |
| 同一个键出现在两个及以上消费者里 | `archiver {key!r} is requested by more than one hook ({req_a}, {req_b}); a run binds each archiver once.` —— 标签按消费者顺序、逗号分隔 |
| 不止一个消费者声明了非空 `archive` | `more than one hook requests archivers ({req_1}: {keys_1}; {req_2}: {keys_2}); a run binds the archive of at most one hook.` —— 按消费者顺序、`; ` 分隔。v1 只绑定一个消费者：`collect_archive` 取走即遗忘，证据只能交给一个调用方 |
| 本 run 没有 environment | `{req} requests archivers {keys}, but this run has no environment.` |

`{env!r}` 是 `env_type`，所有列表都是有序的 `list` repr。`IncompatibleArchiveError` 带三个与 `IncompatibleFragmentError` 同构的属性：`missing`（前缀对应不到或不被提供的键；没有 environment 时是全部请求的键）、`provided`（环境提供的全部归档器；没有 environment 时为空）、`requested_by`（键 → 请求它的消费者标签，有序）；另有 `problems`：消息里那些行，排序并去重（两个标签相同的消费者可能产生一模一样的一句话，只报一次）。重复键与多消费者这两类问题只出现在消息里，不进 `missing`。最常见的一条长这样：

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

`build_guarded_act` 的闭包在每次调用时校验 `action.name` 是否在 active set 里，不在就直接返回 `ActionResult(success=False, error=...)`。

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
                           archive_negotiation, env_evidence_recorded, env_evidence_privacy,
                           task_setup, **kwargs) -> None: ...       # payload 会长，**kwargs 是签名的一部分
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

`trace_writer` 走得更远：它**只**用 `around_ainvoke` 记录 LLM 调用，没有 `after_ainvoke`。在 `proceed()` **之前**登记这次调用（messages、`kwargs["output_format"]`、开始时间）；返回记 `completed`，抛 `Exception` 记 `failed`，抛 `BaseException`（`CancelledError` 等）记 `cancelled`；每条路径都原样重抛。理由是取消：`CancelledError` 是 `BaseException`，`after` 永远看不到被取消的调用，在 `except Exception` 里才登记的调用会连 prompt 一起丢——而超时和外部取消正是最需要知道"模型当时被问了什么"的时刻。只用一个 advice 也省掉了在 `after` 与 `around` 之间关联同一次调用：Loop 一旦并发发起调用，这种关联就会出错。记录的字段见 §5.3、§5.4。

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

**挂受体的 hook 手写 `__init__`。** conscribe 的元类在类创建时读 `inspect.signature(cls.__init__)` 判断一个 `__wiring__` 条目是不是构造参数，这一步早于 `@dataclass` 合成 `__init__`；dataclass 字段形式的受体因此看起来"没被接收"，conscribe 用 `WiredField` 盖掉字段默认值（§5.4、`af-add-a-receptor` guide §6）。所以挂 `__wiring__` 受体的 hook 不是 `@dataclass`：手写 `__init__`，旋钮写成具名的 `Annotated` 参数，受体另有一条类级注解（list 受体写 `list[...]`，§4.2.1）。不挂受体的 hook（`metrics`）可以继续是 dataclass。`HookBase` 本身是 `@dataclass(eq=False)`：hook 是有状态的对象，按身份比较——否则手写 `__init__`、不再是 dataclass 的子类（`TraceWriter`）会继承一个没有字段的 `__eq__`，任意两个实例都"相等"。需要区分 hook 的代码（归档协商比较消费者）一律用 `is`。

**与 emitter.dispatch() 的权衡**：

AOP 方案让 Loop 代码完全干净 — 没有 `emitter` 参数，没有 `dispatch` 调用，甚至不知道 hook 的存在。代价是只能拦截实际存在的方法调用。对于不映射到方法调用的语义事件（如 PlanExecuteLoop 的「规划完成」），解决方案是将该语义建模为 Agent Protocol 方法（如 `agent.parse_plan()` → `after_parse_plan` hook），AOP 自然覆盖。

#### 记录器从环境拉取证据（先例）

环境证据（§5.1）是生产代码里第一个"hook 通过 Environment Protocol 方法拉数据"的地方。它不是 emitter 旁路，规则是：

1. **只在原始环境上拉。** 记录器从 `on_run_start(env=...)` 拿到的是没有被 AOP 包装的环境，拉取就调它的 `collect_archive()`——从不经过代理，所以没有 hook 会拦截这次拉取，也不会递归。
2. **只在自己的提交点拉。** `after_record_step` 里、manifest 提交之前一次；收尾时（`on_run_end` / `on_run_error`）至多再一次。
3. **从不驱动环境。** 不调 `act` / `observe`，不调任何会改变环境的方法；`collect_archive` 只交出 capability 已经采到的东西。
4. **environment 永远拿不到步号。** "第 t 步的提交取走的东西就归第 t 步"：第 t 步动作引发的网络往来、第 t 步开头观测时的原始 DOM 与存储快照，都在第 t 步的 `record_step` 之后被取走，所以归第 t 步。cycle 也不等于步号，capability 只给每条证据带上自己的 `captured_at` / `cycle` / id。

runtime 通过 `ArchiveConsumer` Protocol（`core/archive/protocols.py`）发现记录器：它只有一个属性 `archive: list[ArchiverBase]`，与 YAML 受体同名（M29）。`archive` 非空的 hook 就是本 run 的归档消费者，v1 至多一个（§4.5）；它必须是 blocking 的——`blocking=False` 的 handler 进不了 trace（见上）。

**失败语义：fail closed。** 归档是显式要求的，静默不完整的归档会毒化回放和分叉（"一个写不了 trace 的 TraceWriter 就是一个必须停下的 run"）。所以：

- 某一步的 `collect_archive` 抛错（或交回的批次违反契约，`ArchiveContractError`）→ 这一步**不提交**，它登记的 LLM 调用在收尾时作为 trailing 调用落盘，错误照原样上抛，run 失败，trace 以 `error` 收尾；之后不再收集——Loop 若吞掉了那个错误还要继续，下一次提交直接抛错，点名哪一步的收集失败了。
- 收尾时至多再收集一次，而且只在之前没有失败过时收集。
- 收尾那次收集失败：trace 记为 `error`；只有 run 本身没有失败时才把这个错误上抛（成功的 run 由 `execute` 抛出它），run 已经失败时只记日志，不掩盖 run 自己的错误。收尾那次收集被取消：调用照样冲刷、终态照样写（记为 `error`），然后原样重抛取消——冲刷或写终态在它底下又失败了（磁盘满），那个错误也只记日志，`on_run_end` 与 `on_run_error` 都一样（§4.2 "取消永远优先"）。
- **记录失败不顶替 run 已经定下的结局。** 某一步的收集失败（或任何让 `record_step` 抛错的记录失败）恰好落在 run 正因失败而结束的那一步——guard 的否决正在上抛，Loop 正要把基础设施失败、动作失败或最后一次容许的解析错误变成终止结果——时，Loop 不让这个记录失败从 Loop 里取代那个结局：否决照样上抛（记录失败作为 note 附在 `RunAbort` 上，`execute` 因此照样越过 `raise_on_error=False` 把它交给 sweep，sweep 照样停下；记录失败若一直持续到收尾——磁盘一直满着——收尾的失败同样只是否决上的又一条 note，§4.2 "否决总是上抛"），终止结果的 `error` 仍然点名根因，后面附上"这一步也没能记录"及其原因（`result.json` 于是说清楚 trace 为什么少了这一步）。fail closed 不受影响：这一步照样不提交，它的调用照样作为 trailing 落盘、trace 照样以 `error` 收尾（磁盘写得进去的话；写不进去的就继续欠着），之后不再收集。只有在 run 本来要继续或成功结束的那一步，记录失败才是让 run 失败的那个错误。记录被取消切断（这一步的收集太慢，run 自己的超时恰好到期）也顶替不了那个结局：取消照样原样离开 Loop，但带着结局——否决本身，或已建好的终止结果（`loops.errors.carry_outcome`）——是 run 自己的超时时，`execute` 照这个结局收尾，外部取消照旧优先（§4.2 "run 自己的超时顶替不了已经定下的结局"）。被否决的那一步，Loop 在交出它之前先记下否决的日志，所以交接不论怎样结束，日志里都有 guard 说过停。（`ReactLoop._surrender_failing`、`_surrender_vetoed`、`_end_failing`；新 Loop 的义务见 `af-add-a-loop` guide §3。）
- 取消不是采集失败：被取消的那一步不提交（交接之后才被取消的那一批证据随之丢失；已提交的步各自带着自己的证据，不受影响），收尾照常收集一次，run 以 `error` 收尾。交接之前就被取消的，按交接契约（§4.9 "交出并忘掉"）仍在 capability 里，由收尾那次收集取走，作为 trailing 证据落盘。

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
    async def setup(self, config: dict, **kwargs) -> None: ...   # 内部调 backend.open()
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
- `bind_archivers([])` 什么也不做；非空的绑定抛 `NotImplementedError`，点名类名和那些键——声明了 provides 的 capability 必须同时覆盖 `bind_archivers` 和 `collect_archive`；
- `collect_archive()` 返回空的 `ArchiveBatch()`。

**归档契约**（`core/archive/` 定义类型，capability 实现行为；`tests/_helpers/archive_contract.py` 对每个提供归档器的 capability 检查能从外面检查的那些条：替换与清空绑定、不带参数、交出并忘掉、未绑定不采、只交已绑定归档器的证据、每批至多一份 run 记录、来源信封、采不到就抛错，以及这些证据经 TraceWriter 提交、读回、崩溃窗口与收尾的行为，和另一个归档 capability 同处一个环境时 run 记录仍按 capability 计数、编号与去重。"不无限阻塞"、"对生产者原子交接"与"在途条目恰好交出一次"取决于 capability 的内部时序，与 capability 无关的 harness 造不出来，由各 capability 自己的测试钉住——browser 的在 M29，用 fake CDP）：

- `bind_archivers(archivers)` **替换**整个绑定（空列表 = 解除），不获取任何资源；runtime 只在 setup 之前绑定，capability 在 `setup()` 里读绑定，决定建不建自己的采集通道。收到的是 trace hook 持有的归档器实例，旋钮就在实例上。
- `collect_archive()` 不带任何参数（没有步号，没有 cycle），交出自上次收集以来采到的全部证据并忘掉它们——批次从此归调用方。不无限阻塞；在没有 setup、或 setup 半途失败的 capability 上调用也安全（没采到东西就返回空批次）；采不到时**抛错**，不返回一个假装完整的空批次。
- 它可能在共享环境里另一个 agent 正在动作时被调用，所以"交出并清空"对 capability 自己的生产者必须是原子的（例如在一条同步语句里把缓冲区换出去）。
- 收集边界上还没完成的条目（还在读的响应体）留到之后的批次，恰好交出一次，不重复也不丢；收尾那次收集是最后一次——capability 要么在有限时间内把它们收完，要么把它们标成未完成交出（怎么标是 capability 自己的事，M29 的 `body_status`）。
- 每个 `ArchiveFile` 自带来源信封 `meta: ArchiveProvenance`：`captured_at`（墙钟 Unix 时间）、适用时的 `cycle`、capability 作用域的 `ids`（session / frame / loader id 等）。文件的 `capability_id`（`archiver_id` 的第一段）必须是这个 capability 自己——environment 把别的前缀当作该 capability 的收集失败；步级文件必须属于一个已绑定的归档器键。
- 每个批次里每个 capability 至多一份 run 记录（`scope="run"`，JSON，不内容寻址；`archiver_id` 可以是裸的 capability id，也可以是它的一个已绑定归档器键）。内容变化才需要再发；与上一版字节相同的再发会被记录器丢掉（§5.4）。

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

`ArchiveFile.__post_init__` 在构造时校验，违反就抛 `ValueError` / `TypeError`：`archiver_id` 非空，不含 `/` 或 `\`，不是 `.` / `..`；`scope == "step"` 要求带点的 `<capability>.<id>` 键；`scope == "run"` 允许裸 capability id，要求 `content_addressed is False` 且 `media_type == "application/json"`；`name` 是单个路径段（非空，不含 `/` 或 `\`，不是 `.` / `..`）；`archiver_id` 与 `name` 都必须能编码成 UTF-8，且编码后不超过 255 字节（`MAX_SEGMENT_BYTES`，ext4 等文件系统的 `NAME_MAX`：trace 在按字符计长度的 APFS 上写成，拷到按字节计的文件系统上也得放得下）；`media_type` 是 `type/subtype`（`/` 两侧都非空）且能编码成 UTF-8；`data` 是 `bytes`；`meta` 是 `ArchiveProvenance`；`content_addressed` 是 `bool`；`scope` 只能是那两个字面量。`ArchiveProvenance` 同样在构造时校验：`captured_at` 是有限的数（不是 `bool`），`cycle` 是 `int` 或 `None`，`ids` 是 str → str 的映射、每个键和值都能编码成 UTF-8，复制一份后只读暴露。规则写在 `core/archive/types.py` 的 `segment_problem` / `text_problem` 里。所有这些字符串最后都进 trace 的 JSON 索引或路径：孤立的代理字符（宽松解码留下的 `"\udc80"`）没有 UTF-8 编码，超长的名字会让文件系统报 `ENAMETOOLONG`，两者都会让带着它的提交失败，而且每次重试都以同样方式失败——所以在 capability 那里就拒绝。`archiver_id` 与 `name` 都是单段，所以 `env/` 下不会出现"一个路径既是文件又是目录"，也就不会在 WAL 里半途失败；拼出来的完整路径仍由消费者按 `encoding.check_relative_path` 再查一遍，上面的段规则也再查一遍（frozen dataclass 仍可能被绕过构造校验伪造出来）。

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

BrowserCapability.setup(**kwargs)
├── cdp_url 优先级:
│     1. kwargs["cdp_url"]            (per-call override)
│     2. config["cdp_url"]            (legacy explicit)
│     3. self.cdp_url                  (conscribe field)
│     4. self.backend.open()           ← 取 ProviderSession.cdp_url
├── 持有 self._session = ProviderSession(...)  以便 teardown 释放
└── CDPClient(cdp_url).start() / 创建 watchdogs / 等等

BrowserCapability.teardown()
├── close watchdogs / CDPClient
└── if self._session is not None: await self.backend.close(self._session)
```

**`__wiring__["backend"]` 字段类型**：Mode 1 字符串（`"browser_provider"`），表示"任意已注册到这个 registry 的 provider"。如果某个 capability 想限制 backend 子集，可改 Mode 2/3 的 tuple 形式（如 `("browser_provider", ["local_chrome", "skyvern"])`）——conscribe 把 backend 字段约束为对应的 discriminated union 子集。

**Backend 文件归属**：所有 browser provider 文件物理上属于 `spectragent/core/capabilities/browser/backends/`，与 `BrowserCapability` 同目录；`BrowserProviderRegistrar` 也在 `backends/_registrar.py`。这让"backend 是 capability 的从属配置"在文件结构上一目了然。其他 capability 的 backend 同理：`spectragent/core/capabilities/docker/backends/`、`spectragent/core/capabilities/desktop_vm/backends/` 等。

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
        "archiver": ("archiver", ["browser.network", "browser.storage", "browser.dom_raw"]),
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

    async def setup(self, config: dict, **kwargs) -> None:
        # task_tools 来自 TaskLoader.get_task_context()，每个 task 不同
        task_tools = kwargs.get("task_tools", [])
        self._task_tools = {t["name"]: t for t in task_tools}

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

Capability 本身不需要知道日志、超时、评分的存在。环境证据是唯一的分工：**被拉取时产出**环境证据是 capability 的职责（只有它够得着自己的世界），**持久化**不是——capability 不知道 WAL，不知道步号，也不知道 `env/` 的布局。

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
    └── get_task_context(id) → dict     ← 新增：task 特定上下文（如 per-task tools）
```

**去掉 "Browser" 前缀**，支持所有环境类型的 benchmark。

### 4.11 Evaluator 抽象

```python
EvaluatorRegistrar = create_registrar("evaluator", BaseEvaluator, discriminator_field="evaluator_id")

class BaseEvaluator(Protocol):
    @property
    def evaluator_id(self) -> str: ...
    async def evaluate(self, trace: RunTrace, reference: Any, **kwargs) -> EvalResult: ...
```

Evaluator 的输入是**完整的 RunTrace**（不是 AgentResult），因此可以访问任意深度的运行数据。

| evaluator_id | 评测方式 | 适用 benchmark |
|-------------|---------|---------------|
| `exact_match` | 答案精确匹配 | GAIA, BrowseComp |
| `llm_judge` | LLM-as-judge 评分 | WebArena, Mind2Web |
| `test_suite` | 运行测试套件 | SWE-bench |
| `tool_trace` | 工具调用序列匹配 | ToolBench, BFCL |
| `visual_diff` | 截图对比 | OSWorld |
| `multi_criteria` | 多维度打分 | WebArena |

### 4.12 DOM Pipeline 抽象

```
DOMWatchdog (browser.dom 唯一 resolver)
    └── DOMCleanerProtocol.capture(tab-bound DOMCaptureSource)
          ├── browser_use: CDP Document + DOMSnapshot + AX 并行采集
          └── skyvern: 观测 scope → JS 执行 scope → 页内树遍历
                       → DOM.describeNode 关联 backendNodeId / session
    └── DOMCleanerProtocol.clean(capture) → DOMState
          ├── browser_use: simplify→optimize→paint-order→bounds→index
          └── skyvern: 属性裁剪→frame/shadow 组装→索引
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

---

## 5. RunTrace 全量落盘体系

### 5.1 设计原则

**Log 是运行时的一等公民**，与数据库 WAL 的设计思路相同：每步必须确认落盘后才继续下一步。如果 agent 在第 47 步 crash，前 46 步的数据不能丢。

**不使用 SQLite** — agent 场景的数据结构是异构的（不同 fragment 类型字段完全不同），schema 会随框架演进频繁变化，这与关系数据库的核心假设（同构行、稳定 schema）冲突。数据落盘速度不是运行瓶颈（LLM 推理每步需要数秒，文件写入只需毫秒），SQLite 的查询优势可通过后置索引实现。

**环境证据（environment evidence）是第二类记录。** trace 记录 agent 行为（2026-09-16 的裁决："agent 换一个选择，这个值会不会变？"会变的才记）。环境在 run 中的状态与资源——响应体、存储快照、原始 DOM 采集、环境指纹——通过同一个检验：agent 换一个选择，它们就会不同，而且它们是那次决策所处的世界，不是框架执行决策的细节。按用户裁决 D2（2026-10-09），它作为单独的一类进 trace：

- **显式开启**：只有绑定了非空 `archive`（§4.5）的 run 才有，默认没有；
- **单独放在 `env/` 子树**（§5.3），读侧有自己的访问器（§5.5）；
- **单独标记隐私**：`TraceConfig.env_evidence_recorded` / `env_evidence_privacy`，加上 capability 自己的 run 记录（§5.6）。

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
        （format_version=2，last_step_completed=-1，status=running）。
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
        """按上面的 0–4 提交一步；返回即已落盘。"""

    def commit_trailing(
        self,
        *,
        llm_calls: Sequence[EncodedLLMCall] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """同一协议，但不写 StepRecord、不推进 last_step_completed：
        manifest.trailing 追加一条 TrailingEntry。两样都为空时什么也不做。"""

    def write_result(self, result: AgentResult) -> None: ...   # atomic_write result.json
    def finalize(self, status: RunStatus) -> None: ...         # 写终态，原子重写 manifest
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
- `encode_archive_file(file, *, step=None, trailing=None, run_version=None) -> TraceFile`：把一个 `ArchiveFile` 编码成 extra 文件，按它的文档化字段读（鸭子类型，与 `TraceNegotiation.from_results` 同样的理由：`core/trace` 不 import `core/archive` 的类型）。路径：`scope == "run"` → 必须给 `run_version`，`archive_run_path(capability_id, run_version)`；`content_addressed` → `archive_blob_path(sha256(data))`；其余 → `step` 与 `trailing` 恰好给一个，`archive_step_path(step, archiver_id, name)` / `archive_trailing_path(trailing, archiver_id, name)`；不满足抛 `ValueError`。同样抛 `ValueError` 的还有：`step` 与 `trailing` 同时给出（一个文件只属于一次提交）、不是 run 记录却给了 `run_version`、内容寻址的 run 记录、未知的 `scope`——鸭子类型，所以 `ArchiveFile` 自己会拒绝的这里再查一遍。`meta` = `{ARCHIVE_META_KEY: ArchiveMeta(...).model_dump(mode="json")}`（`ARCHIVE_META_KEY = "archive"`），`content_addressed` 照抄。

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

**为什么每个文件的数据 fsync 都保留，只合并目录 fsync。** §5.1 承诺每步确认落盘才继续。只 rename、不 fsync 数据，掉电后 manifest 可能列着一个空文件，提交点本身就在说谎。数据 fsync 之前还要先把字节 flush 出 Python 的缓冲：`os.fdopen` 最多缓冲 `st_blksize`（APFS、ext4 上是 4 KiB），漏了 flush 或者把 fsync 挪到写之前，所有比它小的文件（step 记录、前几步的 manifest、调用记录、verdict）fsync 时都还是空的，字节要到 `close()` 才进内核。fsync 的次数和顺序都不变，只有 fsync 那一刻的文件大小看得出来，测试就按这个检查。目录 fsync 让 `os.replace` 和 `mkdir` 产生的目录项落盘；一个目录 fsync 一次，就覆盖本批在它下面产生的全部目录项，所以按目录合并不损失任何保证。省下的是逐文件的目录 fsync，M28 那种每步上百个证据文件时尤其明显。macOS（APFS）上 1000 个 1 KiB 文件一次提交约 0.2 s；macOS 的 `os.fsync` 不刷设备缓存，fsync 的真实代价在 Linux ext4/xfs 上。

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

**文件权限就是普通写入会给的权限。** 临时文件用 `os.open(..., 0o666)` 创建，由内核按 umask 屏蔽；替换已有文件时，写入第一个字节之前先把临时文件改成原文件的权限位，所以它从来不比被替换的文件更宽。不用 `tempfile.mkstemp`：它固定 `0o600`，适合临时数据，不适合马上要 rename 到位的文件——结果每个 trace 文件和每个 verdict 都只有属主可读，另一个用户（队友、看板、CI 上传）读共享的 `--evals-dir` 拿到 `PermissionError`，重新打分还会把运维特意设的权限改回 `0o600`。读 umask 也不行：`os.umask` 只能"设置并返回旧值"，会和其他线程创建文件互相竞争。trace 是否私有，由 umask 或目录权限决定，和进程写的其他文件一样；需要私有的 trace（`record_secrets` 打开时）放在私有目录里。

**不属于任何已提交 step 的文件走 `commit_trailing`。** 它落盘 Loop 没来得及交出的那一步的 LLM 调用（超时、外部取消、崩溃），以及 M28 在 run 结束时冲刷的环境证据。它走同一套校验、同一种批量 fsync、同一个提交点，但不写 StepRecord，也不推进 `last_step_completed`：声称一个完成的 step，会在 manifest 里放进一个没有动作的幽灵步。`finalize(status)` 则写终态；停在 `running` 的 trace 没有 evaluator 会碰。

**环境证据走同一个提交。** 归档文件是普通的 extra 文件，WAL 不认识它们（接口仍然只有 `TraceFile`）：

- 一步的 `after_record_step` 在 manifest 提交之前拉取的批次，随**这一步**的 `commit_step(..., extra_files=...)` 落盘，列在 `steps/NNN.extra.json`；内容寻址的文件由 WAL 在 run 内去重，每个引用它的步的索引都列它。
- 收尾时每样欠 trace 的东西**各自一次** `commit_trailing`，按这个顺序：(1) 还登记着的 LLM 调用；(2) 提交失败的步遗留下来的每个批次（不止一个时——只有提交失败之后还继续跑的驱动会这样——两步的批次可能带着同名的归档文件，不能挤进同一个 `env/trailing/KK/`）；(3) 收尾时收集到的尾部。所以每一批证据都有自己的 `env/trailing/KK/`，调用那一次没有 extra 索引。没东西可提交的就不提交。
- 一次提交失败时，它带的归档文件留在 TraceWriter 里，收尾时作为上面的 (2) 提交，重新按 trailing 路径编码（失败那次写下的文件是孤立文件，对读侧不可见）。归档文件的路径段与要进索引的字符串在交给 WAL 之前就校验过（§4.9、§5.4），所以这样的批次不会因为同样的路径错误再失败一次。即便如此，收尾的某一次 trailing 提交仍然失败——磁盘满，或某个批次因为任何重试都修不好的原因写不进去——它也只赔上它自己带的东西：那一样继续留在 TraceWriter 里，之后的冲刷（`on_run_end` 失败后驱动再派发的 `on_run_error`）再试一次，其余各样照常提交；全部试过之后抛出第一个错误（被取消或被否决的 run 上，`execute` 只把它记日志——否决上再附一条 note——重抛的仍是取消或否决，§4.2）。调用单独先提交，正是为了这一点：写不进去的一批证据不会连累未完成那一步的 LLM 调用和尾部（"提交失败不丢调用"，§5.4）。

### 5.3 目录结构

```
traces/
  ├── suite_index.json                  # 可选：后置生成的聚合索引
  │
  └── {run_id}/
      ├── manifest.json                 # 提交点（每次提交原子重写），format_version: 2
      ├── config.json                   # TraceConfig 快照（init_run 写一次）
      ├── result.json                   # AgentResult（run 结束时写一次）
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

**manifest（format_version 2）**，以一个在第 2 步超时的 run 为例：

```json
{
  "format_version": 2,
  "run_id": "f2bbae22",
  "last_step_completed": 1,
  "status": "error",
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

**记录类型**（`core/trace/models.py`，`TRACE_FORMAT_VERSION = 2`）：

```python
class FragmentFiles(BaseModel):
    file: str                          # observations/NNN_<key>.json
    artifact: str | None = None        # observations/NNN_<key><suffix>
    failed: bool = False               # True：file 里是 FragmentFailure，不是 fragment

class StepManifestEntry(BaseModel):
    model_config = ConfigDict(extra="allow")                # v1 的旧字段名照样能解析
    step: int
    step_file: str                                          # steps/NNN.json
    fragments: dict[str, FragmentFiles] = Field(default_factory=dict)  # 全限定 key → 文件
    llm_calls: list[str] = Field(default_factory=list)      # 本步每次调用，按开始顺序
    extra_index: str | None = None                          # steps/NNN.extra.json；无 extra 文件为 None

class TrailingEntry(BaseModel):                             # 每次 commit_trailing 追加一条
    llm_calls: list[str] = Field(default_factory=list)
    extra_index: str | None = None                          # steps/trailing_KK.extra.json

class TraceManifest(BaseModel):
    model_config = ConfigDict(extra="allow")   # v1 manifest 照样能解析，由 format_version 决定哪些访问器拒绝它
    format_version: int = 1            # 缺省 = M27 之前写的 trace；init_run 写 TRACE_FORMAT_VERSION
    run_id: str
    last_step_completed: int = -1
    status: RunStatus = RunStatus.RUNNING
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
- **步级文件归取走它的那一步。** `env/steps/NNN/` 下是第 NNN 步的提交取走的证据，路径段是全限定的 `archiver_id`（provenance）和归档器自定的 `name`。
- **内容寻址的文件只存一份。** 同一个响应体被很多步引用：文件在 `env/blobs/sha256/` 下只写一次，每个引用它的步的索引各列一条。
- **trailing 证据不冒充任何一步。** 收尾时的尾部、以及一次提交失败遗留的批次，落在 `env/trailing/KK/`，`KK` 与 `steps/trailing_KK.extra.json` 是同一个位置编号。
- **run 记录按版本提交。** capability 的 run 级证据（环境指纹等）属于 capability，不属于某个归档器。已提交的路径不可变，所以内容变化后的再次发出写成新的一版：第一版是 `env/run.<capability_id>.json`，之后依次是 `.1.json`、`.2.json`……；与最新一版字节相同的再发被丢掉。版本号记在索引的 `ArchiveMeta.run_version` 里，所以 trailing 提交与步提交交错也不会把顺序弄乱。run 记录随紧接着的那次提交落盘，读侧取已提交的最高版本（§5.5）。版本保持采集顺序，不跟提交顺序走：提交失败遗留的批次在收尾才提交，晚于之后采集的批次；它的 run 记录若比该 capability 已提交的最新一版采集得早，就不再提交（记一条日志）——否则这份过时的指纹会成为最高版本，回放与分叉会照它重建环境（§5.4 "run 记录版本"）。
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
    env_evidence_recorded: bool = False                     # 本 run 是否绑定了归档器（trace 可能带 env/ 证据）
    env_evidence_privacy: list[str] = Field(default_factory=list)  # 所绑定归档器 privacy_markers 的有序并集
    task_setup: TaskSetupRecord | None = None               # None：execute() 没有收到 env_context
```

`TraceNegotiation.from_results(observation=None, action=None, archive=None)` 多收一个归档结果，三个都为 `None` 时才返回 `None`；绑定表原样记录（值都是 registry key，不是用户起的实例 id），排序的理由与另外两条轴相同。`task_setup` 回答的是"这个任务的环境是怎么起的"：分叉任务（M31）要靠它在没有源 benchmark 数据的情况下重建环境，所以从 M28 起就记录；它是按任务变化的数据，脱敏规则见 §5.6。`env_evidence_*` 是 provenance，和 `secrets_recorded` 一样回答"这个目录能不能直接分享"。

**摘要（`config_hash`）不为归档开任何特例。** 归档设置经两条现成的路进入 `configuration_digest`：`negotiation`（`active_archivers` / `archive_bindings`）和 `resolved`（M29 起归档器的旋钮在 trace hook 的配置里）。`task_setup` 按任务变化，`env_evidence_*` 是 provenance，都不进摘要——同一次 sweep 里只差 `start_url` 的两个任务共享一个 `config_hash`。代价写明：`TraceNegotiation` 多了两个字段，M28 之后写的 trace 与同一配置在 M28 之前写的 trace 摘要不同；读者比较的是同一个框架版本写下的 manifest。

**`evals/` 也是原子写。** `EvaluationRunner` 写 `evals/<verdict_slug>.json`，以及可选的平铺副本和 summary，都走 `fsio.atomic_write`：半写的 verdict 文件会被下一个读者当成损坏的评测。权限与普通写入相同（§5.2），共享的 `--evals-dir` 对其他用户照常可读，重新打分也不改动已有文件的权限。evals 是后置产物，不进 manifest。

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
                           archive_negotiation=None, env_evidence_recorded=False,
                           env_evidence_privacy=(), task_setup=None, **kwargs):
        if self.archive:
            ...  # 三种情况在建 trace 之前拒绝（RuntimeError，点名 AgentRuntime.prepare）：
                 # archive_negotiation 为 None；它的 active_archivers 与 archive 的键不一致；env 为 None
            self._env = env                    # 原始环境，不是 AOP 代理；收尾时释放
        self._wal = WALWriter(Path(self.trace_dir), run_id or str(uuid.uuid4())[:8])
        self._wal.init_run(trace_config, config_hash=...)   # 摘要取自未脱敏的配置（§5.3）
        # trace_config.negotiation = TraceNegotiation.from_results(obs, action, archive=archive_negotiation)
        # trace_config.env_evidence_recorded / env_evidence_privacy / task_setup：按名字接收，不推导

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
        extra = await self._step_archive(step) if self.archive else []
        # ↑ 拉取 → 校验 → 定 run 记录版本 → encode_archive_file(..., step=step)。
        #   collect_archive() 抛错、或批次违反契约（ArchiveContractError）：记下失败、原样上抛，
        #   这一步不提交，它登记的调用留给收尾的 trailing
        pending = list(self._pending_calls)    # 本步全部调用，按开始顺序
        self._wal.commit_step(
            step, record,
            fragments=encode_observation(step, kwargs.get("observation")),
            llm_calls=encode_llm_calls(step, pending, first_call=self._calls_flushed),
            extra_files=extra,                 # 提交抛错：这批归档文件留给收尾的 trailing（自己一次）
        )
        del self._pending_calls[: len(pending)]   # 提交成功之后才移出
        self._calls_flushed = 0

    async def on_run_end(self, *, result, env, **kwargs):
        tail, archive_error = [], None
        try:
            tail = await self._collect_tail()  # 至多一次；之前失败过就不收集
        except BaseException as exc:           # 先记下：收尾必须照常走完
            archive_error = exc
        cancelled = archive_error is not None and not isinstance(archive_error, Exception)
        try:
            self._close_out(tail, result=result, incomplete=archive_error is not None)
        except Exception:
            if not cancelled:
                raise                          # 冲刷 / result.json 的失败照原样上抛（否决的 run 上 execute 只把它附成 note，§4.2）
            logger.warning(...)                # 取消永远优先：收尾在它底下又失败了，只记日志
        if archive_error is not None and (
            cancelled                                         # 取消：原样重抛
            or _status_for(result) is not RunStatus.ERROR     # 只在 run 本身没有失败时上抛
        ):
            raise archive_error

    def _close_out(self, tail, *, result, incomplete):
        flushed = False
        try:
            self._flush_trailing(tail)         # 调用、每个遗留批次、尾部 —— 各一次 commit_trailing
            flushed = True
        finally:                               # trailing 提交失败，终态也照样写
            status = _status_for(result)
            if incomplete or self._archive_failure is not None or not flushed:
                status = RunStatus.ERROR       # 归档不完整（收尾或某一步的收集失败过），或欠 trace 的没提交完
            try:
                if result is not None:
                    self._wal.write_result(result)
            except BaseException:
                status = RunStatus.ERROR       # result.json 写不进去：trace 不是这个 run 的完整记录
                raise
            finally:
                self._wal.finalize(status)
                self._env = None               # 收尾之后不再持有环境

    async def on_run_error(self, *, error, partial_steps, env, **kwargs):
        tail, cancellation = [], None
        try:
            tail = await self._collect_tail()  # on_run_end 已经收过就是 []；失败只记日志——run 本来就在失败
        except Exception:
            logger.warning(...)
        except BaseException as exc:           # 取消：收尾走完之后再原样重抛
            cancellation = exc
        try:
            try:
                self._flush_trailing(tail)     # 幂等：on_run_end 通常已经冲刷过
            finally:
                self._wal.finalize(RunStatus.ERROR)
                self._env = None
        except Exception:
            if cancellation is None:
                raise
            logger.warning(...)                # 同上：取消优先于它底下失败的收尾
        if cancellation is not None:
            raise cancellation
```

**归档的写侧规则**（`archive` 非空时才生效；为空时 TraceWriter 不缓存环境、不碰任何归档方法，行为与 M27 完全相同）：

- **`archive` 是普通构造参数。** 它不是 `Annotated`：`HookBase.__config_annotated_only__` 把它挡在生成的配置之外，所以在 M29 之前，YAML 里 `hook[].archive` 这个块会被 Pydantic 拒绝，而不是被静默丢掉。M29 加上 `__wiring__["archive"]`、同一参数上的 `Annotated` 类型、类级 `archive: list[ArchiverBase]` 受体注解（mixin），以及 `_collect_registrars()` 里的 `archiver`——空注册表不能成为 Composer 层（`composer.py` 对没有实现的已收集层直接抛错，`generate_all` 也跳过它），所以这些要和第一批具体归档器一起落地。M28 绝不能先给它一个没有 wiring 的 `Annotated` 类型：conscribe 会生成 `archive: Any`，把原始 dict 交给 hook。
- **`on_run_start`**：在 `init_run` 之前核对（所以拒绝时不留下 trace 目录）：`archive_negotiation is None`、`set(archive_negotiation.active_archivers) != {a.archiver_id for a in self.archive}`、`env is None`，任何一条都抛 `RuntimeError`，说明归档器必须经 `AgentRuntime.prepare()` 协商并绑定。核对通过才缓存原始 `env`。
- **`after_record_step`** 的顺序：`_stamp_timings` → 如果之前已经有过归档失败（Loop 吞掉了第一次的错误又提交了一步），直接抛 `RuntimeError`，点名收集失败的那一步，`__cause__` 是记下的那个失败，不再收集——从那以后归档已经不完整 → `batch = await env.collect_archive()`（抛 `Exception`：记为归档失败，原样上抛）→ 校验批次（违反时抛 `ArchiveContractError`，同样记为归档失败、整批丢弃，在写任何字节之前）→ 定 run 记录版本 → `encode_archive_file(..., step=step)` → `commit_step(..., extra_files=...)`。`commit_step` 自己抛错（I/O）时，这批文件留在 TraceWriter 里等收尾，错误照原样上抛；成功之后才更新 run 记录的版本表。
- **批次校验**（`ArchiveContractError`，步提交与收尾的批次一样查）：步级文件的 `archiver_id` 必须是本 run 绑定的键之一，内容寻址的文件（响应体）也一样——用户故意没绑定的归档器，它的证据不能借 `env/blobs/` 落盘；run 记录的 `capability_id` 必须是某个绑定归档器所属的 capability（`archive_bindings` 的值），它的 `archiver_id` 是绑定键或那个裸 capability id；每个 capability 至多一份 run 记录；`archiver_id` 与 `name` 各是文件系统接受的单段（§4.9 的段规则：至多 255 字节 UTF-8），`media_type` 与 `ids` 的键和值都能编码成 UTF-8；编码后的路径在批次内按 `encoding.fold_path` 的键互不相同——不区分大小写、也不区分 Unicode 规范化形式（内容寻址的同一路径可以重复，只写一次）；每条路径都过 `encoding.check_relative_path`。`ArchiveFile` 构造时已经拒绝了其中大半，这里再查一遍：被拒绝的批次从不成为欠 trace 的东西，而因为这类原因失败的提交，每次重试都会同样失败。
- **run 记录版本**：TraceWriter 给每次收集按发生顺序编号（步提交的收集与收尾那次都算），并记着每个 capability 已提交的最新一版（版本号、sha256、它来自第几次收集）。来自更早一次收集的 run 记录不再提交，记一条日志——这只发生在遗留批次上：它在收尾才提交，之后某次收集的 run 记录可能已经落盘（继续跑的驱动，或收尾写不进遗留批次、尾部却落了盘，之后 `on_run_error` 重试）；与最新一版字节相同的再发直接丢掉，但它的收集仍算作最新的采集；否则取下一个版本号（第一版是 0）。版本表只在提交成功之后生效，收尾的每一次 trailing 提交之前重新计算。
- **收尾**：`_collect_tail()` 在 `archive` 非空、之前没有归档失败、也没有收尾收集过时才收集，并立刻标记"已收尾收集"——所以 `on_run_end` 之后的 `on_run_error` 不会再收集一次，只派发 `on_run_error` 的驱动也照样收集一次。冲刷按 §5.2：调用、每个遗留批次、尾部各自一次 `commit_trailing`，trailing 位置编号是提交前 `len(manifest.trailing)`；某一次失败只让它自己那一样继续欠着，其余照常提交，全部试过之后抛第一个错误。收尾的收集没有正常完成（抛错或被取消），manifest 记为 `error`；抛错只在 run 本身没有失败时才从 `on_run_end` 上抛（`execute` 因此抛出它），`on_run_error` 里只记日志；取消在终态写完之后原样重抛，两个 hook 都一样——即使冲刷或写终态又在它底下失败（磁盘满），那个错误也只记日志，重抛的仍是取消（§4.2 "取消永远优先"）。某一步的收集失败过时，不论 result 怎么说（Loop 吞掉了那个错误、最后报告成功），manifest 同样记为 `error`：归档不完整，trace 就不是这个 run 的完整记录。收尾之后 TraceWriter 不再持有环境；之后再有提交（排空之后还继续跑的驱动）抛 `RuntimeError`——归档已经收尾，那一步不能带着证据提交。
- **耗时记在下一步上。** 拉取发生在 `_stamp_timings` 之后，和提交 I/O 一样算进下一步的 `duration_ms`；M29 的归档开销度量单独计时。

> **Commit 粒度说明**：`around_ainvoke` 只登记 LLM 调用，不写盘；一次 step 可能包含多次调用，而动作及其结果要到调用之后才产生。Loop 将含本步 actions、action results 和 usage 的完整 `StepRecord` 交给 `record_step()` 后，`after_record_step` 才把观测、本步全部调用记录和 step record 一次交给 `WALWriter.commit_step()`；manifest 的原子更新是持久化提交点。若运行在 `record_step()` 前结束（超时、外部取消、崩溃），已登记的调用（包括被取消、只有 prompt 的那一次）在 `on_run_end` / `on_run_error` 里经 `commit_trailing()` 落盘为 trailing calls，但不冒充一个完成的 step。

**失败记录只写拿到的东西。** `CallFailure.error` 是异常类名，`detail` 是 `str(exc)`；其余字段只在异常确实带着它们时出现：

- `raw_output`：provider 交回了文本，校验器没接受（`StructuredOutputValidationError`）；
- `partial_output`：回复被 token 上限截断时已经生成的部分。`ModelOutputTruncatedError(..., partial_output=...)` 在 openai_base、anthropic_base（含序列化后的 tool_use 输入）、google 的抛出点都传入。它刻意不叫 `raw_output`，那个名字的意思是"校验器看到的原文"；
- `stop_reason` / `usage`：失败的调用照样计费。

死掉的 socket 什么也没交回，记录就只有 `error` 和 `detail`。不造一个装着错误的 completion：读 trace 的人必须分得清模型写的回复和框架编的回复。被取消的调用 `status: cancelled`，`completion` 为空，`failure.error` 是取消异常的类名（`CancelledError`），prompt 完整保留。提交或收尾时仍在进行中的调用（目前的 Loop 不会出现，调用都在 Loop 内 await）同样记为 `cancelled`，但 `failure` 为空：trace 到此为止，hook 没见到任何异常，prompt 照样保留。

**提交失败不丢调用，收尾失败不丢终态。** `commit_step` 抛错（I/O 错误）时，这一步登记的调用仍留在 TraceWriter 里，run 结束时经 `commit_trailing` 落盘——它们自己一次提交，排在任何证据批次之前，所以一批写不进去的证据连累不到它们（§5.2）。`on_run_end` 里 trailing 提交本身失败时，`result.json` 和终态照样写，终态记为 `error`（欠 trace 的调用或证据没落盘，trace 不是这个 run 的完整记录——M28 之前这里沿用 result 的状态，成功的 run 因此可能带着丢失的尾部标成 `completed`），错误随后上抛。`result.json` 自己写不进去时，终态也照样写，但记为 `error`：没有 `result.json` 的 trace 不是这个 run 的干净记录，不能标成 `completed`；停在 `running` 则更糟，没有 evaluator 会碰它。

**output schema 每个类每个 run 只算一次。** `output_format` 来自 `kwargs["output_format"]`（ReactLoop 以 `llm.ainvoke(context, output_format=output_model)` 调用）。TraceWriter 按类缓存 `model_json_schema()`，编码成内容寻址的 `messages/output_formats/<sha256>.json`，随第一次引用它的提交落盘；之后的调用只带 `OutputFormatRef`。生成不了 schema 的回复模型记为没有 schema（`output_format: null`）并打 warning，不因为 trace 让调用失败：provider 用同一个 schema 构造请求，那个失败由 provider 自己报。

**等待时间仍然只在 `around` 里量。** `_pending_llm_ms` 在 `finally` 里累加，`_stamp_timings` 在提交点把 `duration_ms` / `llm_latency_ms` 写到 `StepRecord` 上；每条调用记录另带自己的 `latency_ms`。

### 5.5 RunTrace 的 lazy loading

30MB 的 trace 不一次性读入内存。不同 judge 按需加载不同部分；只有 manifest 是 eager 读的（每步一条索引，几百字节）：

```python
class RunTrace:
    def __init__(self, trace_dir: Path) -> None: ...        # 只读 manifest.json

    @property
    def trace_dir(self) -> Path: ...
    @property
    def manifest(self) -> TraceManifest: ...
    @property
    def run_id(self) -> str: ...
    @property
    def step_count(self) -> int: ...                        # last_step_completed + 1，无 I/O

    # ── 形状自 v1 未变：任何 format_version 都可读 ─────────────────
    @cached_property
    def config(self) -> TraceConfig: ...                    # config.json
    @cached_property
    def result(self) -> AgentResult | None: ...             # result.json；None = 文件不存在
    @cached_property
    def step_records(self) -> list[StepRecord]: ...         # 每个已提交 step，manifest 顺序
    def step(self, idx: int) -> StepRecord | None: ...      # None = 该步没有提交

    # ── v2 形状：format_version < 2 时抛 TraceSchemaError ────────────
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
- `blob(sha256)`：一个已提交的内容寻址归档文件的字节，即 `read_extra(archive_blob_path(sha256))`：没有任何已提交的索引把它列为归档文件（`meta` 带 `"archive"`）→ `KeyError`——别的生产者放在同一地址上、不带归档元数据的 extra 文件不是归档文件；字节与索引不符 → `TraceSchemaError`。
- `archive_run_file(capability_id)`：该 capability 已提交的 run 记录里 `run_version` 最高的那一版，跨步索引和 trailing 索引找；没有 → `None`。字节用 `read_extra(record.path)` 读。
- 任何 `ArchiveRecord` 的字节都用 `read_extra(record.path)` 读，同一套 size / sha256 校验。这四个访问器全部只从每步和 trailing 的 extra 索引解析（索引里 `meta` 带 `"archive"` 键的记录才是归档文件），不探测路径、不 glob，所以孤立的 `env/` 文件对它们不可见；它们和 `extra_files` 一样拒绝 format 1 的 trace。

**读回来的必须是写进去的那个类型。** Writer 拿到的是 `StepRecord` / `AgentResult`，reader 就必须还原成同一个 model，而不是 `dict[str, Any]` —— 否则字段名只能靠翻 JSON 文件去猜，猜错（`rec["step_index"]`，实际是 `rec.step`）能通过全部静态检查、到运行时才炸。这也是 Evaluator 唯一的输入契约。

fragment 也一样。`FragmentBase.from_trace(payload: dict[str, Any], artifact: bytes | None) -> Self`（classmethod）是 `trace_binary()` / `trace_json_exclude()` 的成文逆运算。默认实现处理没有二进制载荷的 fragment：要求 `artifact is None`（不是 `None` 就说明 fragment 漏了覆盖，直接报错），返回 `cls.model_validate(payload)`。覆盖了 `trace_binary()` 的 fragment 必须同时覆盖 `from_trace`，把 artifact 放回被排除的字段：`ScreenshotObs.from_trace` 把字节放回 `image_bytes`。`DOMObs.from_trace` 把 `element_tree` 还原成 `DOMState`（`serialized_dom`、`scroll_info`、`dom_tree`；不单独落盘的 `DOMState.selector_map` 指向同一份 `selector_map`），把 `selector_map` 还原成 `dict[int, DOMElementNode]`，每个条目带 `tag_name`、`attributes`、`bounds` 和子树里的文本。编码里仍然有损的部分（深度截断留下的 `__truncated__`、树内的 `__cycle__` 指针、不追踪的 `parent` 与私有字段）写在 `DOMObs` 的 docstring 里，`from_trace` 不伪造被截掉的节点，两种标记读回时直接丢掉。`parent` 不落盘，但可以由结构推出：`from_trace` 在每棵解码出的子树内按 children / shadow root / frame 文档重建它，所以 `frame_owners()` 在树内可用；条目根节点之上的部分不在条目里，条目根的 `parent` 为 `None`。一个按 `ObservationRegistrar` 全部注册类参数化的往返测试守住这条契约：encode 再 decode，JSON 形式不变。

写侧的配套修正：`DOMObs` 序列化 `selector_map` 时，每个条目用自己的 visited 集合。过去所有条目共享一个，嵌套在前一个已索引元素之下的元素被写成 `{"__cycle__": …}` 桩，丢了自己的 `bounds`（M18 的 BrowserUse trace 里 59/60、70/71、106/107 个条目是桩）。条目带完整子树，不做浅条目：BrowserUse 清洗出的元素节点不带 `text_content`，文本在后代 `#text` 节点里。代价随已索引元素的嵌套深度增长：把 M18 的 45 个 `dom.json`（BrowserUse 与 Skyvern-JS 清洗，38 个非空）按新编码重写，带缩进的文件中位数增大 14%、最多 65%（紧凑 JSON 中位数 31%、最多 98%），7 个文件超过 30%；Skyvern-JS 那几步 34.6 MB 的页面变成约 50 MB。这是下限：条目是从受深度限制的树里重建的。M33 的存储评估从这组数开始。M31 的元素重定位和 M32 的 index → 坐标换算直接读 `selector_map[index]`，不再各写一个解码器。

`DOMObs` 的解码器按 dataclass 字段的注解建解码计划（与编码器读同一份声明），认得标量、`Any`、dataclass、`X | None`、标量联合、`Literal`、`list`、`tuple`、`set` / `frozenset` 和 `dict`。其余泛型（如 `Sequence[str]`）在建计划时抛点名注解的 `TypeError`：那是编程错误，第一个读回 DOM 的测试就会暴露，而不是无限递归成一个 `RecursionError`。

**三种“读不到”必须互相可区分**：

| 情况 | 表现 | 含义 |
|---|---|---|
| 不存在：`result.json` 缺失；或所问的 step、fragment、调用、extra 文件、归档文件不在 manifest 里 | 返回 `None`（`result`、`step`、`observation_fragment`、`screenshot`、`archive_run_file`）或 `[]`（`llm_messages`、`extra_files`、`archive_files`）；`read_extra` / `output_schema` / `blob` 抛 `KeyError` | 没有发生：run 死在写 `result.json` 之前，或者那一步没有提交 |
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
| `format_version` 不支持 | 低于 2：只有 v2 形状的访问器抛错；高于 reader 支持的版本：构造 `RunTrace` 时就抛 |

把前两者都塌缩成 `None`，会让一批旧 schema 的 trace 静默地表现为“agent 没跑完”。`AgentResult.schema_version` 只接受当前版本：旧版本字段名对不上，接受它只会换来一条指着某个被改名字段的 pydantic 报错，而不是“这个 trace 太旧”。

**一个归一化入口。** 一个 run 必有的 `manifest.json`、`config.json`，以及 manifest 列出的每个 JSON 文件（step、fragment、调用记录、extra 索引、output schema），都经 `_read_bytes` 读字节、经同一个 `_parse` 解析和校验：model 用 `_read_model(path, model)` 读，fragment 的 JSON 先读成 dict 交给 `from_trace`。解析是严格 UTF-8 解码加标准库 `json.loads`，校验是 pydantic 的 Python 模式（`model_validate` / `validate_python`）。不用 pydantic 的 `validate_json`：它的 JSON 解析器拒绝超过 200 层的嵌套，而写侧用的 pydantic-core 序列化器能写到约 254 层，DOM 编码器的深度预算本身就到约 204 层——一个嵌套一百来层的页面（正是深度截断要照顾的页面）、一个深层的结构化回复或工具结果，都能顺利提交，却永远读不回来，还被报成"Invalid JSON"。`json.loads` 只受解释器递归上限约束，远高于写侧能写出的深度；真超过了，同样转成点名文件的 `TraceSchemaError`。非 UTF-8、JSON 损坏、空文件（`UnicodeDecodeError` / `JSONDecodeError`，都是 `ValueError`）和 schema 不匹配（`ValidationError`）都由 `_parse` 转成点名文件和 model 的 `TraceSchemaError`（`path` 属性就是那个文件；format_version、fragment key 和一步两个截图 fragment 的情形是 `manifest.json`）；`_read_bytes` 把 manifest 列了而磁盘上没有的文件同样转成 `TraceSchemaError`；其余 `OSError`（权限、I/O）原样上抛，那不是 trace 本身的问题。`result.json` 是唯一的例外，因为没有谁列出它：它自己读字节，不存在就返回 `None`（唯一返回 `None` 的缺文件情形），解析和校验失败同样转成点名文件的 `TraceSchemaError`；版本不符时，报错按 `result.json` 自己声明的版本说"这个 trace 早于当前 AgentResult schema"。

**落盘文件名带 provenance。** Observation 文件名用的是**全限定 fragment key**（`000_browser.dom.json`），不是协商后的 field name（`000_dom.json`）。后者是"本次装配里哪个 capability 赢了协商"的产物：同一个 `000_screenshot.png` 在 browser run 和 desktop run 里含义不同，而读 trace 的人无从分辨——"Record Once, Evaluate Many" 的前提是 trace 自带足够信息，evaluator 不需要回头猜配置。

同一份 provenance 在数据里再写一遍：`fragment_id` 是 `FragmentBase` 上的**真实字段**（默认值来自 `__registry_key__`），不是 `@property`——property 不进 `model_dump()`，于是每个 fragment 落盘时都是匿名的。

**二进制 fragment 的后缀来自字节本身，不来自配置。** 截图的编码是 capability 配置（`screenshot_format: png | jpeg`，默认 png 与旧行为逐字节相同），但 `ScreenshotObs.trace_binary()` 是从图像头读出后缀的——一个"声明的格式"字段能和字节不一致（provider 没听配置、fragment 从磁盘文件重建），而不一致会在几层之外变成 vision provider 的 400。同理，agent 拼 `data:` URL 的 media type、artifact 落盘时的后缀、evaluator 按后缀判类型，读的都是同一个来源（`spectragent/schemas/observation/image.py`）。

**trace 格式有版本。** `TraceManifest.format_version` 从 M27 起写 `2`（`TRACE_FORMAT_VERSION`）；M27 之前的 manifest 没有这个字段，按 1 读。v1 的 fragment 列表、成对的 `NNN_input.json` / `NNN_output.json` 和没有判别字段的失败 JSON 无法安全地类型化读回，所以 v2 形状的访问器（上面代码块的第二组）对 v1 trace 抛 `TraceSchemaError` 并点名版本；`config` / `result` / `step` / `step_records` / `step_count` 照常可读。不做兼容 shim（AGENTS.md §0.4）：要用图像 judge 重新给 v1 trace 打分，只能重跑任务。

**离线读者找得到 fragment 类。** 评测进程未必 import 过定义 fragment 的模块（browser 的 fragment 是被各自的 watchdog import 时才注册的）。`ObservationRegistrar.get(key)` 落空时，reader 懒执行一次 `discover("spectragent.schemas")` 和 `discover("spectragent.core.capabilities")` 再查；仍然落空就抛 `TraceSchemaError`，点名 key，并说明定义它的模块必须在读侧可导入。

### 5.6 环境证据的隐私

数据飞轮 PRD §5.10 的方案（用户裁决 D4，2026-10-09：剥离凭证、表单值脱敏、归档打标、导出前审查）在这里落成框架与 capability 的分工。**框架从不读、也从不改证据的字节**——采集和脱敏是 capability 的事（§4.9），框架不声称替任何证据脱敏。框架这一侧只做三件事：记录本 run 声明了什么；提供共享的规则与台账格式，让每个 capability 用同一套；在产物层面用 secret guard 检查。

| 对象 | 默认处理 | 归属 |
|---|---|---|
| 本 run 有没有环境证据、声明了哪些隐私标记 | `TraceConfig.env_evidence_recorded`；`env_evidence_privacy` = 所绑定归档器 `privacy_markers` 的有序并集。它只陈述归档器的声明，不证明字节里没有秘密 | 框架（M28） |
| 共享规则与脱敏台账 | `core/archive/privacy.py`：`PRIVACY_POLICY`（规则版本，`"af-privacy-1"`）、字段名模式 `password\|passwd\|pwd\|otp\|token\|secret\|card\|cvv`（子串匹配，有意宽）、凭证参数名模式（`key` / `apikey` / `sig` / `signature` / `auth` / `credential` 作尾段）、URL 规则、`RedactionLedger`、标记常量 `MARKER_REDACTED` / `MARKER_SESSION_STATE`。规则一改，`PRIVACY_POLICY` 必须升版本（测试用规则源码的摘要钉住：两个名字模式、占位符、URL 模式和每个规则函数的源码，URL 规则在内） | 框架（M28）；M29、M30 复用 |
| 环境 setup context（`TraceConfig.task_setup`） | 值级脱敏：凭证形状的映射键，其值（整棵子树）换成 `<redacted>`；URL（以 `scheme://` 开头的字符串）的 userinfo 整段换掉；名字是凭证形状的 query / fragment 参数，值换掉；其余字节原样。`null` 与空参数值没有可换的东西，原样保留、不记台账（`null` 要与"设置过"区分开）。台账一起记录。`record_secrets` 打开时与 `resolved` 一样原样记录，台账为空 | 框架（M28） |
| secret guard | 扫描整个 trace 目录（含 `env/`），gzip 文件（魔数 `1f 8b` 或 `.gz` 后缀）先解压再扫；正向对照：假归档器把凭证写进 `env/`（明文与 `.gz` 各一份），必须被发现 | 框架（M28） |
| 归档本身 | 永不导出；导出物只含截图、动作和 thought | 导出（M32 遵守） |
| 请求头 `Cookie` / `Authorization` / `Proxy-Authorization`（以及可配置的追加列表） | 采集时剥离，从不落盘 | browser（M29） |
| 响应头 `Set-Cookie` | 保留（回放的 cookie jar 需要）；归档器声明 `contains_session_state`，capability 的 run 记录同样标出；`logged_in: true` 的 run 必须显式写 `allow_session_state: true`，否则 `prepare()` 报错 | browser（M29） |
| DOM 原始采集里的输入值 | 按类型或 autocomplete 脱敏（password / one-time-code / cc-*） | browser（M29） |
| POST body | 按字段名模式脱敏，写入台账；回放时先用同一个函数脱敏再算匹配键 | browser（M29）；M30 复用 |

**脱敏写成 `<redacted>`，不是 `"<omitted>"`。** 后者是配置脱敏（`core/trace/redaction.py`，deny by default）的"设置了、没记录"；前者是"值被这条规则换掉了"，两者回答不同的问题。`<redacted>` 也不会被 secret guard 的凭证形状误认成凭证。

**台账**（`RedactionLedger`）：`policy`（写下它时的 `PRIVACY_POLICY`）加一串 `RedactionEntry(location, field, rule)`——`location` 是 JSON pointer（`/start_url`），`field` 是被换掉的键或参数名，`rule` 是 `field_name` / `url_userinfo` / `url_query` / `url_fragment` 之一，按遍历顺序。它让读者知道哪里被换过，而不是猜一个空值是不是本来就空。

**残余，写明。** `task_setup` 是没有字段声明的按任务数据，`core/trace/redaction.py` 那种"没标记就不记"在这里会把每个值都吞掉、让分叉任务（M31）无法重建环境，所以它走值级规则——规则只认得凭证形状的名字和 URL 里的凭证位置：一个藏在规则不认识的名字下面的秘密（`{"note": "the password is hunter2"}`）会原样记录；嵌在非凭证 query 参数里、没有百分号编码的 URL 的 userinfo 也一样。还有一条具体的：`BrowserCapability.setup` 接受 `cdp_url`，调用方若经 `env_context` 传一个 devtools URL（`ws://…/devtools/browser/<id>`），它会原样进 `task_setup`——发布的入口都不这么传（`TaskSetup.to_context()` 没有这个键，`spectragent-run` 只传 `start_url`），要覆盖它得新增规则并升 `PRIVACY_POLICY`。`TraceConfig.extra` 仍然不脱敏（调用方自己挑的字段）。v1 的数据来源选的是无需登录的公开站点；登录态采集是单独的决定。

---

## 6. conscribe 注册表总览

| 注册表 | 判别方式 | 说明 | 状态 |
|--------|---------|------|------|
| `AgentRegistrar` | `agent_id` (flat) | Agent 实现（领域知识），通过 `__wiring__` 限定可配置的 Loop / Capability 范围；实际兼容性在装配期校验 | ✅ |
| `AgentLoopRegistrar` | `loop_id` (flat) | Loop 实现（执行策略）；通过 `__required_slots__` 声明所需 agent 方法 | ✅（仅 ReactLoop；CodeAct/FunctionCalling/PlanExecute 待 P2/P3） |
| `LLMRegistrar` | `model_type.provider` (nested) | LLM provider 实现 | ✅ |
| `EnvironmentRegistrar` | `env_type` (flat) | 环境实现，通过 `__wiring__["capabilities"]` 注入 Capabilities，按前缀路由 action | ✅（仅 BrowserEnvironment；CompositeEnvironment 待） |
| `CapabilityRegistrar` | `capability_id` (flat) | 能力 = ActionGroup + Resolver 合体；持 backend wired field | ✅（仅 BrowserCapability；docker / tool_sandbox / desktop_vm / android / gpu_profiling 待） |
| `BrowserProviderRegistrar` | `provider_id` (flat) | Browser capability 的 backend：`local_chrome` / `lexmount` / `skyvern` / `browser_use` / `airtop` / `agentbay` / `hyperbrowser`。文件位于 `core/capabilities/browser/backends/` | ✅ |
| `DockerProviderRegistrar` | `provider_id` (flat) | Docker capability 的 backend（`local_docker` / `ssh_docker` ...） | P2 占位待实现 |
| `MemoryRegistrar` | `memory_id` (flat) | 记忆策略 | ✅（3 种；协议兼容性在 `prepare()` 校验，SummaryMemory 待实现） |
| `HookRegistrar` | `hook_id` (flat) | Lifecycle hooks (AOP) | ✅（trace_writer / metrics；live_monitor 待 P3） |
| `EvaluatorRegistrar` | `evaluator_id` (flat) | 评测策略 | 🚧 partial |
| `TaskLoaderRegistrar` | `benchmark_name` (flat) | 任务数据加载 | ✅ |
| `EvalLoaderRegistrar` | `benchmark_name` (flat) | 评测数据加载 | ✅ |
| `ActionRegistrar` | `action_id` (flat, dotted key `source.id`) | 每个 Action 独立注册；键含 source 前缀避免跨 capability 撞名（`browser.click` vs `desktop.click`） | ✅ |
| `ObservationRegistrar` | `fragment_id` (flat, dotted key `source.id`) | Observation fragment；键含 source 前缀（`browser.dom`、`docker.terminal`、`loop.action_result`）；agent / loop 的 `__wiring__` 写 capability-relative 短名（`dom`），协商期绑定到本次 environment 的 provider | ✅ |
| `ArchiverRegistrar` | `archiver_id` (flat, dotted key `source.id`) | 环境证据归档器；键含提供它的 capability 前缀（`browser.network`）。capability 的 `__wiring__["archiver"]` 声明 provides，trace hook 实例的 `archive` 列表声明 requires（全限定）；`negotiate_archive` 在 `prepare()` 校验，并在 `prepare()` 末尾绑定（§4.5） | 🚧 M28：框架机制（协商、绑定、trace 拉取），没有具体归档器，不进 `_collect_registrars()`；M29 随首批 browser 归档器进 Composer、由 YAML 选择 |
| `NegotiationResultRegistrar` | `result_type` (flat) | 协商结果类型；内置 `observation` / `action` / `archive` 自动注册，自定义协商协议注册自己的结果类型并作为 `target_class` 传给 `negotiate()` | ✅ |
| `DOMCleanerRegistrar` | `cleaner_type` (flat) | DOM 清洗策略 | ✅（browser 内部） |
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
| **新增 Archiver（环境证据归档器）** | 继承所属 capability 的 archiver 基类（框架基类 `ArchiverBase`），`__registry_key__ = "<capability>.<id>"`，旋钮写成 `Annotated` 构造参数，按需声明 `privacy_markers`；把键加进该 capability 的 `__wiring__["archiver"]`；capability 实现 `bind_archivers` / `collect_archive`（§4.9 归档契约）；把 capability 接入 `tests/_helpers/archive_contract.py` | `browser.network`、`browser.storage`、`browser.dom_raw`（M29）；以后的 `docker.fs_diff`、`android.ui_dump` |
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

**归档相关的 import 方向**（`tests/test_core/test_archive/test_archive_boundaries.py` 用 AST 守着：每一条 import 都算，函数里的、`if TYPE_CHECKING:` 下的也算——只为类型的依赖仍是设计上的依赖；名字 `TraceWriter` 从哪个模块 import、或经属性访问都算；包 `__init__` 顺带 import 了什么不在这几条的视野里，它们管的是每个模块自己点了哪些名字）：

- `core/runtime.py`、`core/negotiation/*`、`core/environments/*` 不 import `core.hooks.trace_writer`，也不 import 任何定义具体归档器的模块——runtime 只认 `ArchiveConsumer`，environment 只认 `ArchivingCapability`；
- `core/archive/*` 不 import `core.trace`、`core.hooks`、`core.negotiation`、`core.environments`、`core.capabilities`；
- `core/trace/*` 从 `core.archive` 只 import `core.archive.privacy`（`TaskSetupRecord` 里的台账类型）；归档文件以鸭子类型跨进 trace 层，WAL 的接口仍然只有 `TraceFile`；
- 新的归档与协商模型里没有 `price` / `cost` / `usd` / `dollar` 字段：只记 token，不记钱（2026-09-19）。

外部核心依赖：
- `conscribe` — 注册 + 配置生成
- `bubus` — 事件总线 (watchdog 事件分发)
- `agent-cdp` — Browser Capability 内部中间层 (BaseEvent, WatchdogBase, ScopeGroup, EventScope)
- `pydantic` v2 — 数据契约
- `websockets` — CDP WebSocket 连接（Browser Capability 内部）

---

## 11. Capability 内部中间层示例：Browser (agent-cdp)

每个 capability 各自管理自己与 backend 之间的中间层协议。以 Browser Capability 为例，内部通过 `agent-cdp` 实现事件驱动架构：

```
Browser Capability
├── agent-cdp 中间层
│   ├── CDPClient (WebSocket → Chrome/Chromium)
│   ├── ScopeGroup
│   │   ├── browser EventScope → CrashWatchdog
│   │   └── 每个已接管 tab（同一 tab/CDP session）
│   │       ├── 观测 EventScope → DOM / Action / Screenshot / Meta 等 Watchdogs
│   │       └── JS 执行 EventScope ← 跨 scope QUEUED 连接
│   │                              ← 共享 JavaScriptExecutionWatchdog 的 handler
│   ├── DOM Pipeline (策略选择 capture → clean → budget → serialize)
│   └── Input 拟人化层 (input/, §4.13)
│        ├── MouseTrajectoryRegistrar  → HumanMouse    → Input.dispatchMouseEvent
│        ├── TypingRhythmRegistrar     → HumanKeyboard → Input.dispatchKeyEvent
│        └── play_on_deadline —— 两条链共用的唯一时钟（绝对截止时间）
│
└── Backend (core/capabilities/browser/backends/, IoC-injected via __wiring__["backend"])
    ├── local_chrome, lexmount, skyvern, browser_use,
    │   agentbay, airtop, hyperbrowser
    └── 每个 backend 提供 async open() → ProviderSession.cdp_url
        capability.setup() 调 backend.open() 拿到 cdp_url 后启 agent-cdp
```

两个 per-tab scope 是 Skyvern JS 提取的编排选择，不是 agent-cdp 的基数约束：
scope 与 watchdog/handler 为 N:M。当前 DOMWatchdog 因持有该 tab 的 selector map 和 frame
发现状态而按 tab 建实例；JS 执行 watchdog 一个实例服务多个 tab，只维护从 CDP
attach/detach 事件得出的 tab→iframe session 归属表，以拒绝跨 tab 的 JS 请求，不持有
任何 tab 的 DOM 提取缓存或 cleaner 配置。
同一 scope 的 QUEUED handler 逐个等待，观测 scope 通过 `target_scope` 投递到另一个
JS scope 后可等待其结果而不自等待。`_attach_tab()` 成组创建并绑定两个 scope；
`switch_tab()` 懒附加走同一路径，`close_tab()` 和 teardown 成组释放。

`BrowserProfileConfig` 是命名预设（`standard` / `skyvern` / `vision_only` / `lightweight`），但只承载两类事实：**安装哪些 watchdog**（`watchdogs_exclude`，反向声明——注册表里没被排除的都装，名字在 setup 时对着注册表校验，写错是硬报错而不是静默 no-op），和**允许花多少次 CDP 往返**（`js_click_listener_detection`、`cross_site_iframe_capture`），外加 SecurityWatchdog 的域名白 / 黑名单。

**策略一律不在 profile 上**：`dom_cleaner`、`dom_serializer`、`mouse_trajectory`、`typing_rhythm` 全部是 `BrowserCapability` 上的 `__wiring__` 受体（§4.12 / §4.13）。profile 曾经用字符串名持有 cleaner 与 serializer，后果是消费者拿着名字去注册表里 `get(name)()` 现造一个默认实例，把 Composer 已经按 YAML 构造好的那个丢掉——策略的 knobs 生成在 config 里却无人可达。profile 也不支持 override 或增量修改：一个预设要么被整体选中，要么另起一个。区分规则是「谁为它买单」——改变一次 capture 花多少次 CDP 往返的，属于 profile；改变产出的 observation 让 prompt 花多少 token 的（`screenshot_quality`、`dom_max_elements`），是 capability 自己的 `Annotated` 标量。

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

完整序列化每步的 NegotiatedObservation。理由：存储不值钱，API 和时间值钱。完整 trajectory 数据可直接用于下游 RL 训练。TraceWriter Hook 序列化时利用 ObservationRegistrar 的 discriminated union 能力。

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
