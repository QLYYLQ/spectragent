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
| `@Autowired` interface | Loop 兼容性 Protocol（`ReactCompatible`、`CodeActCompatible`） |
| `@Qualifier` / 跨 Bean 依赖 | `__wiring__` 跨注册表依赖声明（如 Agent 声明兼容哪些 Loop） |
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
__wiring__ 声明跨注册表依赖 ← 配置生成时自动约束为 Literal[...] 合法组合
```

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

    # __wiring__: 声明兼容的 loop、需要的 capability、observation
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

**关键设计：AOP 透明织入**。AgentRuntime 通过 Weaver 将 hook 拦截器织入 agent/env/llm 的方法调用中。Loop weave 时拿到的 agent/env/llm 实际上是 AOPProxy 包装后的对象——`env.observe()` 调用时，AOP 自动在调用前/后触发 hook handler。Loop 代码中零 hook 感知，零 emit 调用。

**AOP 拦截点与 Loop 的对应关系**：

| 拦截点 | advice | 触发时机 | 典型 Hook 用途 |
|--------|--------|---------|---------------|
| `env.observe` | after | 每步 observe 返回后 | TraceWriter 缓存 observation |
| `llm.ainvoke` | after | 每步 LLM 返回后 | TraceWriter commit 当步数据；MetricsCollector 累加 token |
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

        # 4. AOP 织入 — Weaver 包装 agent / env / llm
        weaver = Weaver(self._hooks)
        wrapped_agent = weaver.wrap(agent, role="agent")
        wrapped_env = weaver.wrap(env, role="env")
        wrapped_llm = weaver.wrap(llm, role="llm")

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
            session_id=session_id, task_id=task_id, **kwargs,
        )

        # 7. 拦截点审计 —— 必须在 weave 之后：agent.run 到这一步才存在
        weaver.audit_intercepts()
        return wrapped_agent

    # ── execute(task): 一站式驱动 ───────────────────────────────────
    async def execute(self, task, *, timeout=300, config=None, **kwargs):
        prepared_agent = self.prepare(**kwargs)

        await self._dispatch_lifecycle("on_run_start",
            agent=self._run.agent, env=self._run.env,
            task=task, config=config or {})

        try:
            result = await asyncio.wait_for(
                prepared_agent.run(task), timeout=timeout
            )
            await self._dispatch_lifecycle("on_run_end", result=result)
            return result
        except TimeoutError:
            ...   # build timeout AgentResult
        except Exception as exc:
            await self._dispatch_lifecycle(
                "on_run_error", error=exc, partial_steps=[]
            )
            raise
```

**为什么两段式**：`prepare()` 是纯装配（slot 校验 → 协商 → AOP 织入 → loop weave），返回值就是一个 self-contained 的 agent —— 测试和需要细粒度控制的调用方可以直接 `await prepared_agent.run(task)`，跳过 timeout/lifecycle 包装。`execute()` 只是 prepare + 围绕 `agent.run` 加 timeout 和 lifecycle hook。两段都是独立的、零全局状态的 run 上下文，并行多个 run 互不干扰。

**AOP 织入流程**：Weaver 读取所有 hook 的 `__intercepts__` 声明（如 `Intercept("observe", role="env", advice="after")`），为 agent/env/llm 生成 `AOPProxy` 包装。Loop weave 时拿到的是 wrapped 对象——调 `env.observe()` 时 AOP 自动触发 `TraceWriter.after_observe()`，调 `llm.ainvoke()` 自动触发 `MetricsCollector.after_ainvoke()`。Loop 代码零 hook 感知。

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
- 不在 init_params 也不在 class_hints → 纯声明性约束（如 `agent.observation` 的 `Literal[...]` 限制），Composer 跳过，Pydantic 仍校验

**注入方式**：init param → 进 `__init__` 的 kwargs；class_hints-only → 通过 `setattr(instance, name, value)` 注入（conscribe `WiredField` descriptor 接受这次写入并把值放到 instance dict，遮蔽自身）。

**与 AgentRuntime 的边界**：`AgentRuntime` 只接收 `ComposedRun`，**不**直接看 conscribe schema、**不**自己解析 YAML。`ConfigComposer` 是唯一懂 conscribe 内部约定（receptor 检测、dict/list 形态、wiring 解析）的层。增删 wiring grammar、调整 discover 路径、加新 layer 都只动 Composer。

### 4.3 Environment 抽象

**设计决策**：Environment 通过 `__wiring__` 注入 Capabilities，是 Capability 的组合者和路由器。Environment 自身走 conscribe 注册表。多 capability 场景通过前缀路由 action（`browser.click` → browser capability）。

```python
EnvironmentRegistrar = create_registrar("environment", Environment, discriminator_field="env_type")

@runtime_checkable
class Environment(Protocol):
    # 已注入的 capabilities，由 ConfigComposer 通过 setattr 写入
    capabilities: dict[str, BaseCapability]

    # 能力发现（从注入的 capabilities 聚合）
    def capability_ids(self) -> frozenset[str]: ...
    def has_capability(self, cap_id: str) -> bool: ...

    # 按需观察 — 委托给 capabilities 的 resolver
    async def observe(self, negotiated: NegotiationResult,
                      session_id: str | None = None) -> NegotiatedObservation: ...

    # 动作执行 — 前缀路由到对应 capability
    async def act(self, action: Action, session_id: str | None = None) -> ActionResult: ...

    # Action space — 聚合所有 capabilities 的 descriptors，可按 active_actions 收窄
    def get_action_space(self, *, active_actions: frozenset[str] | None = None,
                         **context) -> list[ActionDescriptor]: ...

    # 生命周期 — 把 setup/teardown 转发给所有 capabilities（capability 自己内部调 backend.open/close）
    async def setup(self, **kwargs) -> None: ...
    async def teardown(self) -> None: ...

    @property
    def is_alive(self) -> bool: ...
```

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
```

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

Config 时，AgentRuntime 读取三方 wiring 执行协商：

```
env-sourced ∧ Agent/Loop required ∧ Env 提供  → 激活 resolver，填充到 Observation
env-sourced ∧ Agent/Loop optional ∧ Env 提供  → 激活 resolver，填充到 Observation
env-sourced ∧ Env 提供 ∧ 无人请求             → 不激活 resolver（性能优化，如 UITars 不算 DOM）
env-sourced ∧ Agent/Loop required ∧ Env 不提供 → CONFIG ERROR（不兼容组合）
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

| 维度 | Observation | Action |
|---|---|---|
| 注册器 | `ObservationRegistrar` (`"observation"`) | `ActionRegistrar` (`"action"`) |
| 基类 | `FragmentBase` (pydantic bridge) | `ActionBase` (dataclass + conscribe metaclass) |
| 注册单元 | 每个 fragment 一个类 (`DOMObs` / `ScreenshotObs` / ...) | 每个 action 一个类 (`ClickAction` / `ScrollAction` / ...) |
| 注册 key 命名空间 | `source.id` dotted key (`browser.dom` / `browser.screenshot` / `docker.terminal` / `loop.action_result`) | `source.id` dotted key (`browser.click` / `browser.scroll` / `sandbox.bash`) |
| Agent wiring | `"observation": ("observation", [required], [optional])` — 默认写 capability-relative 短名（`screenshot`），写全限定键即钉死 provider | `"action": ("action", [required], [optional])` — 同左（`click_coordinate` / `browser.click_coordinate`） |
| Capability wiring | `"observation": ("observation", [provides])` | `"action": ("action", [provides])` |
| Environment 聚合 | `provided_fragments` (全限定键集合) | `provided_actions` (全限定键集合) |
| 协商函数 | `negotiate_observation(agent, loop, env)` → `ObservationNegotiationResult` | `negotiate_action(agent, env)` → `ActionNegotiationResult` (loop 不参与) |
| 协商失败 | `IncompatibleFragmentError` | `IncompatibleActionError` |
| wiring 提取 | `extract_wiring(cls, "observation")` | `extract_wiring(cls, "action")` |
| 统一入口 | `negotiate(target_class, wiring_name, rules, **kwargs)` — 见 `spectragent/core/negotiation/engine.py` | 同左 |
| 副作用/状态配置位置 | Capability 端 (e.g. `DOMWatchdog.cleaner_type` 是 profile 的一部分) | **Capability 端** (e.g. `ScrollAction.default_amount` 是 capability 持有的 action 实例的 conscribe 字段) |
| 运行时使用 | `env.observe(negotiated, session_id)` 只解析 active fragment | `guarded_act` 拒绝 non-active action；`active_descriptors` 只暴露 active set |
| Loop 参与协商 | 是（`ActionResultObs` 等 loop-sourced fragment） | **否**（action 都是 env-sourced，loop 只是驱动器） |

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

**设计决策**：TraceWriter、MetricsCollector、LiveMonitor 等横切关注点通过 AOP Weaver 透明织入。Loop 不持有任何 hook/emitter 引用，调用 `env.observe()` / `llm.ainvoke()` 时 AOP 自动触发对应 hook handler。hook 走 conscribe 注册表，通过 YAML 配置启用和参数化。

**两层 hook 接口**：

1. **Lifecycle 回调**（结构边界）：`on_run_start/end/error`，由 AgentRuntime 在运行开始/结束/异常时直接调用。
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
    async def on_run_start(self, *, agent, env, task, config) -> None: ...
    async def on_run_end(self, *, result: AgentResult) -> None: ...
    async def on_run_error(self, *, error, partial_steps) -> None: ...
```

**AOP 拦截声明**：Hook 通过 `__intercepts__` 声明要拦截的方法、目标角色和 advice 类型：

```python
class Intercept:
    method: str           # 目标方法名（如 "observe", "ainvoke"）
    role: str             # 目标角色（"env", "agent", "llm"）
    advice: AdviceType    # BEFORE, AFTER, AROUND

class AdviceType(Enum):
    BEFORE = "before"     # 方法调用前执行，接收 args/kwargs
    AFTER = "after"       # 方法调用后执行，接收返回值
    AROUND = "around"     # 包装整个调用，接收 proceed 可调用对象
```

Handler 命名约定：`{advice}_{method}` — 如 `after_observe`、`after_ainvoke`、`around_build_react_prompt`。

**目标方法抛异常时，`after` 不触发。** 通常这是对的：没返回的调用没什么可记。但有**一个** LLM 错误是可恢复的——结构化回复不符合 schema（`StructuredOutputValidationError`），Loop 会把模型自己的文本喂回去继续跑。这一次调用如果没人记，trace 里就会出现一个 messages 文件干脆缺失的 step，而且缺的正是研究者最想看的那些 step（模型答错的那些），metrics 的 token 数也会比 result 自己算的少一次调用。所以 `trace_writer` / `metrics` 各加一条 `around_ainvoke`：`try: await proceed() except ...: 记下来再 raise`。成功路径仍然走 `after`（`around` 的最内层就是 before/after 链），两者不会重复记账。

这里没有引入第四种 advice。"哪些失败的调用该进 trace"是个值得单独决定的问题，而 `around` 已经能表达这一个确定的答案。

**Weaver 织入流程**：

```python
# AgentRuntime.execute() 中：
weaver = Weaver(active_hooks)
wrapped_env = weaver.wrap(env, role="env")       # env.observe → after_observe 自动触发
wrapped_llm = weaver.wrap(llm, role="llm")       # llm.ainvoke → after_ainvoke 自动触发
wrapped_agent = weaver.wrap(agent, role="agent")

# Loop 接收 wrapped 对象，调用方法时 hook 透明拦截
await loop.run(wrapped_agent, wrapped_env, task, llm=wrapped_llm, ...)
```

`AOPProxy` **懒解析**：wrapped 方法在首次属性访问时构建，并在底层属性被重新绑定时重建。这是必须的——`agent.run` 要到 `Loop.weave()` 之后才存在，wrap 时预构建会把框架的招牌拦截点 `Intercept("run", role="agent")` 判死。未被拦截的属性访问透明委托给原始对象；如果某个角色没有任何拦截器，`weaver.wrap()` 直接返回原始对象（零开销）。

代价是校验也变懒了，所以装配期补了两道检查，都在 `AgentRuntime.prepare()` 里：`weaver.raise_for_unclaimed_roles()`（wrap 之后）抓「声明了没人 wrap 的 role」，`weaver.audit_intercepts()`（**`loop.weave()` 之后**，这是答案第一次可知的时刻）抓「role 被 wrap 了但拦截仍然永远不会触发」——本次运行没有该对象（env-less run 里的 `env` 拦截）、目标没有该属性（`env.obsserve` 拼写错误）、advice 与方法形状不匹配。两者都 raise：拦到空处的 hook 与没配的 hook 等价，配置错误应当在装配期暴露，而不是等到第一次 LLM 调用。

`AOPProxy` 的透明度边界也是显式的：CPython 的特殊方法查找走 `type(obj)`，不经过 `__getattr__`，所以代理只对**具名属性**透明。`__bool__` / `__eq__` / `__hash__` 转发给 target（它们过去会静默给出错误答案）；`__enter__` / `__exit__` / `__aenter__` / `__aexit__` / `__iter__` / `__aiter__` 在 target 实现时转发、否则抛 `UnsupportedProxyProtocolError`（`TypeError` 子类，消息里点名 target）。这之外的 dunder 一律不声明——声明 `__call__` 会让 `callable(proxy)` 对任何 target 都返回 True，用一个安静的谎话换掉一次响亮的失败。

**`blocking` 的唯一语义 = 并发**：`blocking=True` 内联 await handler，被拦截的方法要等它做完才返回；`blocking=False` 把 handler 交给后台任务，调用立刻继续。错误隔离是**推论**而非第二重语义——非阻塞 handler 抛错时它所修饰的调用早已返回，没有可传播的对象，因此只能记日志。三种形状不成立，装配期以 `UnsupportedAdviceError` 拒绝：同步目标上的 `blocking=False`（没有可延后的调用）、`around` 上的 `blocking=False`（handler 本身就是那次调用，返回值经由它的 `proceed()`）、handler 与目标的 sync/async 形状不匹配。

推论要写清楚：**`blocking=False` 的 handler 进不了 trace**。它在 WAL commit 点之后才结束，算出来的东西落不到任何产物上。所以 `trace_writer` / `metrics` 必须是 blocking，而 `blocking=False` 只适合下游无人读取的旁路（即 `live_monitor`）。这些后台任务由 `BackgroundTasks` 持有强引用，并在 `AgentRuntime._finish()` 里 env 拆除之前 drain（默认 5s，超时则取消并 WARNING 点名 hook）——「fire and forget」不等于「fire and never run」：裸 `asyncio.create_task()` 丢掉句柄时，事件循环只持弱引用，handler 可能被 GC 掉，也可能因为没人等而根本没执行。

**已知 Hook 实现**：

| hook_id | 状态 | blocking | `__intercepts__` | 职责 |
|---------|------|----------|-------------------|------|
| `trace_writer` | ✅ | True | `after_ainvoke(llm)`, `around_ainvoke(llm)`, `after_record_step(loop)` | 缓存每步 LLM 消息 → Loop 交出 StepRecord 时 commit 当步数据到 WAL |
| `metrics` | ✅ | True | `after_ainvoke(llm)`, `around_ainvoke(llm)` | 从 ChatInvokeCompletion 提取 token usage，内存累加 |
| `live_monitor` | **P3 未实现** | False | `after_observe(env)`, `after_ainvoke(llm)` | WebSocket 推送实时状态。设计保留位，目前框架内未注册；按本表的 schema 实现一个 `HookBase` 子类即可纳入。 |

**与 emitter.dispatch() 的权衡**：

AOP 方案让 Loop 代码完全干净 — 没有 `emitter` 参数，没有 `dispatch` 调用，甚至不知道 hook 的存在。代价是只能拦截实际存在的方法调用。对于不映射到方法调用的语义事件（如 PlanExecuteLoop 的「规划完成」），解决方案是将该语义建模为 Agent Protocol 方法（如 `agent.parse_plan()` → `after_parse_plan` hook），AOP 自然覆盖。

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

**当前状态**：Memory 走 `MemoryRegistrar`（`memory_id`, flat），AgentBase 通过 `__wiring__["memory"] = "memory"`（Mode 1，自动发现）注入；Loop 直接调 `memory.add_message` / `memory.get_context_messages`，**不做协议协商**。

```
MemoryRegistrar = create_registrar("memory", BaseMemoryManager, discriminator_field="memory_id")

BaseMemoryManager (Protocol)
    ├── add_message(message)
    ├── get_context_messages() → list[BaseMessage]
    ├── clear()
    └── memory_id: str
         │
         ├── SimpleMemory          所有消息，有上限
         ├── SlidingWindowMemory   最近 N 条消息
         ├── TokenBudgetMemory     在 token 预算内尽量保留
         └── SummaryMemory         LLM 压缩摘要
```

> **未来工作（P1，未实现）**：Memory 协议协商——不同 Loop 范式需要不同的消息协议（ReAct 用 user/assistant，FunctionCalling 需要 tool_call/tool_result，PlanExecute 需要 planning/execution 分区）。届时 Loop 声明 `__wiring__["memory"] = ("memory", ["user_assistant", ...])`，Memory 声明支持的协议，AgentRuntime 在 prepare 阶段做 "Loop requires ⊆ Memory supports" 校验。在那之前，新 Loop 的 memory 兼容性由 Loop 自己在 `_execute` 内处理（如把 tool_call 退化为 assistant message）。

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
```

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
- **超时/资源控制** — AgentRuntime 对 loop.run() 加 timeout
- **评分** — AgentRuntime 按 benchmark 注册的 evaluator 执行

Capability 本身不需要知道日志、超时、评分的存在。

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

### 5.2 写入协议

```
写入顺序（每步）:
  1. 先写不可变的内容物（observation fragments, screenshot 文件）
  2. 再写 step record（引用上述文件路径）
  3. 最后原子更新 manifest（记录最新完成的 step 编号）← commit point

crash recovery:
  - 读 manifest → 知道最后一个完整的 step
  - manifest 之后的孤立文件 → 未完成的 step，安全忽略
```

原子更新使用 POSIX `write-then-rename`：

```python
def atomic_write_json(path: Path, data: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)  # POSIX 原子操作
    except:
        os.unlink(tmp_path)
        raise
```

### 5.3 目录结构

```
traces/
  ├── suite_index.json             # 可选：后置生成的聚合索引
  │
  └── {run_id}/
      ├── manifest.json            # 进度指针（每步原子更新）← commit point
      ├── config.yaml              # 完整配置快照（run 开始时写一次）
      ├── result.json              # AgentResult（run 完成时写一次）
      │
      ├── steps/                   # 每步记录
      │   ├── 000.json
      │   ├── 001.json
      │   └── ...
      │
      ├── observations/            # Observation fragments（每步追加）
      │   ├── 000_browser.dom.json         # 文件名 = 全限定 fragment key
      │   ├── 000_browser.screenshot.png   # 后缀取自图像字节头（jpeg 则是 .jpg）
      │   ├── 001_browser.dom.json
      │   ├── 001_docker.terminal.json     # SWE 场景
      │   └── ...
      │
      ├── messages/                # LLM 完整交互记录（每步追加）
      │   ├── 000_input.json
      │   ├── 000_output.json
      │   └── ...
      │
      └── evals/                   # 评测结果（后置，可多次运行不同 judge）
          ├── exact_match_v1.json
          ├── llm_judge_gpt4o_v1.json
          └── llm_judge_claude_v2.json   # 新 judge 无需重跑 agent
```

### 5.4 TraceWriter 作为 Hook 实现

TraceWriter 不是 AgentRuntime 的核心组件，而是一个 Hook，通过 AOP Weaver 透明织入 + YAML 配置参数化：

```python
class TraceWriter(HookBase):
    __registry_key__ = "trace_writer"
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="after"),
        Intercept("record_step", role="loop", advice="after"),
    ]

    trace_dir: Annotated[str, Field(default="traces")] = "traces"
    blocking = True  # 确认落盘后才继续

    async def on_run_start(self, *, agent, env, task, config):
        run_id = str(uuid.uuid4())[:8]
        self._wal = WALWriter(Path(self.trace_dir), run_id)
        self._wal.init_run(trace_config)

    # ── AOP 拦截 handler ──────────────────────────────────
    async def after_ainvoke(self, result, *, args, kwargs, **ctx):
        """缓存本步的 LLM input/output — 等 commit point 一起写入。"""
        self._pending_messages.append((args[0], result))

    async def after_record_step(self, result, *, args, kwargs, **ctx):
        """Commit point：Loop 交出 StepRecord 时，写入 observation +
        LLM messages + step record，并原子更新 manifest。以「步完成」
        而非「LLM 返回」为边界，是因为一步可以有多次 LLM 调用。"""
        # 1. 写入 observation fragments（agent 实际看到的那一份）
        # 2. 写入本步累积的 LLM input/output messages
        # 3. 写入 step record
        # 4. 原子 commit（write-then-rename manifest）
        self._wal.commit_step(step=..., ...)

    async def on_run_end(self, *, result):
        self._wal.write_result(result)
        self._wal.finalize(RunStatus.COMPLETED)
```

> **Commit 粒度说明**：与传统的 `step_end` 事件不同，AOP 方案以 `after_ainvoke` 为 commit point — 这是每步中最昂贵的操作（LLM 推理数秒，不可重试）。Action 执行结果不包含在本步 commit 中，但会在下一步的 observation 中自然被捕获。

### 5.5 RunTrace 的 lazy loading

30MB 的 trace 不一次性读入内存。不同 judge 按需加载不同部分：

```python
class RunTrace:
    def __init__(self, trace_dir: Path):
        self._dir = trace_dir
        self._manifest = json.loads((trace_dir / "manifest.json").read_text())

    @property
    def result(self) -> AgentResult | None: ...    # lazy load result.json

    @property
    def step_records(self) -> list[StepRecord]: ...

    def step(self, idx: int) -> StepRecord | None: ...  # lazy load steps/NNN.json

    def observation_fragment(self, step: int, fragment_type: str) -> ObservationFragment | None: ...

    def screenshot(self, step: int) -> bytes | None: ...

    def llm_messages(self, step: int) -> list[dict] | None: ...
```

`exact_match` judge 只读 `result.json`（几 KB），`visual_judge` 才会加载所有 screenshot。

**读回来的必须是写进去的那个类型。** Writer 拿到的是 `StepRecord` / `AgentResult`，reader 就必须还原成同一个 model，而不是 `dict[str, Any]` —— 否则字段名只能靠翻 JSON 文件去猜，猜错（`rec["step_index"]`，实际是 `rec.step`）能通过全部静态检查、到运行时才炸。这也是 Evaluator 唯一的输入契约。

**三种“读不到”必须互相可区分**：

| 情况 | 表现 | 含义 |
|---|---|---|
| 文件不存在 | 返回 `None` | run 死在写 `result.json` 之前，没到任何终态 |
| 文件存在但 schema 过旧/损坏 | 抛 `TraceSchemaError` | trace 本身不可读，重跑评测也没用，只能重跑任务 |
| 评测器自己失败 | `EvaluationError` | 与 trace 无关（judge 429、prompt 缺失等） |

把前两者都塌缩成 `None`，会让一批旧 schema 的 trace 静默地表现为“agent 没跑完”。`AgentResult.schema_version` 只接受当前版本：旧版本字段名对不上，接受它只会换来一条指着某个被改名字段的 pydantic 报错，而不是“这个 trace 太旧”。

**落盘文件名带 provenance。** Observation 文件名用的是**全限定 fragment key**（`000_browser.dom.json`），不是协商后的 field name（`000_dom.json`）。后者是"本次装配里哪个 capability 赢了协商"的产物：同一个 `000_screenshot.png` 在 browser run 和 desktop run 里含义不同，而读 trace 的人无从分辨——"Record Once, Evaluate Many" 的前提是 trace 自带足够信息，evaluator 不需要回头猜配置。

同一份 provenance 在数据里再写一遍：`fragment_id` 是 `FragmentBase` 上的**真实字段**（默认值来自 `__registry_key__`），不是 `@property`——property 不进 `model_dump()`，于是每个 fragment 落盘时都是匿名的。

**二进制 fragment 的后缀来自字节本身，不来自配置。** 截图的编码是 capability 配置（`screenshot_format: png | jpeg`，默认 png 与旧行为逐字节相同），但 `ScreenshotObs.trace_binary()` 是从图像头读出后缀的——一个"声明的格式"字段能和字节不一致（provider 没听配置、fragment 从磁盘文件重建），而不一致会在几层之外变成 vision provider 的 400。同理，agent 拼 `data:` URL 的 media type、`RunTrace.screenshots` 的 glob、evaluator 按后缀判类型，读的都是同一个来源（`spectragent/schemas/observation/image.py`）。

因此 `observation_fragment` 加具体类型的前置条件已经满足（key 就是 registry key，`ObservationRegistrar.get(key)` 直接给出类）；只等一个真正要读它的 evaluator，因为那同时会改掉一个公开访问器的返回类型。

---

## 6. conscribe 注册表总览

| 注册表 | 判别方式 | 说明 | 状态 |
|--------|---------|------|------|
| `AgentRegistrar` | `agent_id` (flat) | Agent 实现（领域知识），通过 `__wiring__` 声明兼容的 Loop / Capability | ✅ |
| `AgentLoopRegistrar` | `loop_id` (flat) | Loop 实现（执行策略）；通过 `__required_slots__` 声明所需 agent 方法 | ✅（仅 ReactLoop；CodeAct/FunctionCalling/PlanExecute 待 P2/P3） |
| `LLMRegistrar` | `model_type.provider` (nested) | LLM provider 实现 | ✅ |
| `EnvironmentRegistrar` | `env_type` (flat) | 环境实现，通过 `__wiring__["capabilities"]` 注入 Capabilities，按前缀路由 action | ✅（仅 BrowserEnvironment；CompositeEnvironment 待） |
| `CapabilityRegistrar` | `capability_id` (flat) | 能力 = ActionGroup + Resolver 合体；持 backend wired field | ✅（仅 BrowserCapability；docker / tool_sandbox / desktop_vm / android / gpu_profiling 待） |
| `BrowserProviderRegistrar` | `provider_id` (flat) | Browser capability 的 backend：`local_chrome` / `lexmount` / `skyvern` / `browser_use` / `airtop` / `agentbay` / `hyperbrowser`。文件位于 `core/capabilities/browser/backends/` | ✅ |
| `DockerProviderRegistrar` | `provider_id` (flat) | Docker capability 的 backend（`local_docker` / `ssh_docker` ...） | P2 占位待实现 |
| `MemoryRegistrar` | `memory_id` (flat) | 记忆策略 | ✅（4 种；协议协商待 P1） |
| `HookRegistrar` | `hook_id` (flat) | Lifecycle hooks (AOP) | ✅（trace_writer / metrics；live_monitor 待 P3） |
| `EvaluatorRegistrar` | `evaluator_id` (flat) | 评测策略 | 🚧 partial |
| `TaskLoaderRegistrar` | `benchmark_name` (flat) | 任务数据加载 | ✅ |
| `EvalLoaderRegistrar` | `benchmark_name` (flat) | 评测数据加载 | ✅ |
| `ActionRegistrar` | `action_id` (flat, dotted key `source.id`) | 每个 Action 独立注册；键含 source 前缀避免跨 capability 撞名（`browser.click` vs `desktop.click`） | ✅ |
| `ObservationRegistrar` | `fragment_id` (flat, dotted key `source.id`) | Observation fragment；键含 source 前缀（`browser.dom`、`docker.terminal`、`loop.action_result`）；agent / loop 的 `__wiring__` 写 capability-relative 短名（`dom`），协商期绑定到本次 environment 的 provider | ✅ |
| `NegotiationResultRegistrar` | `result_type` (flat) | 协商结果类型；内置 `observation` / `action` 自动注册，自定义协商协议注册自己的结果类型并作为 `target_class` 传给 `negotiate()` | ✅ |
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
| **`__wiring__` 跨注册表依赖** | Agent↔Loop 兼容性、Env↔Capability 注入、Capability↔Backend 注入、Observation 协商 |
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
    observation: browser.dom        # declarative-only literal (Pydantic 校验)
    action: { action_id: browser.click }   # declarative-only union pick
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
| **新增 Mouse Trajectory / Typing Rhythm 策略** | 继承 `MouseTrajectoryBase` / `TypingRhythmBase`，设 `__registry_key__`，实现 Protocol 全部成员（含 `click_hold_ms` / `aim_offset_px`），knobs 写成 `__init__` 关键字参数 | 新的运动模型；已训练模型的新导出格式属于 `learned` 的 backend，不是新策略（§4.13） |
| **新增注册表（全新变化轴）** | `create_registrar(...)` + `Base(metaclass=...Meta)`；**同一次改动**里把行写进 §6 与 CLAUDE.md §2.2（`tests/test_docs/test_registrar_coverage.py` 会核对，判别字段必须与 `discriminator_field` 逐字一致）；若这根轴要由 YAML 选择，还须在 `scripts/generate_all.py::_collect_registrars` 注册，否则字段发成裸 `str` | 滚轮节奏、触屏手势。反例：`NegotiationResultRegistrar` 不由 YAML 选择，因此正确地不在 Composer 里 |
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
       │              → Weaver 织入 → loop.weave(agent)             │
       │              → patch agent.run = closure(loop._execute)   │
       ├── execute(task): on_run_start → asyncio.wait_for(         │
       │                  agent.run(task), timeout=...)            │
       │                  → on_run_end                              │
       │                                                           │
       │ AOP 拦截：env.observe → TraceWriter.after_observe          │
       │           llm.ainvoke → MetricsCollector.after_ainvoke    │
       │                       → TraceWriter.after_ainvoke (commit)│
       │                                                           │
       └── 输出 AgentResult                                          │
                │                                                  │
                ▼                                                  ▼
           AgentResult ──→ RunTrace (full persistence)     TaskDefinition
                               │                           EvalReference
                               ▼
                          Evaluator ──→ EvalResult ──→ EvalSummary
```

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

Memory 也通过 `__wiring__` 注入和协商。Loop 声明需要的消息协议（user_assistant / tool_call / planning 等），Memory 声明支持的协议。协商规则：Loop requires ⊆ Memory supports。详见 4.8 节。

### 13.3 Observation 二次协商（已决定）

**方案 C — config 时 + runtime 启动前都做**。Config 时做静态验证（wiring 声明层面），AgentRuntime.execute() 启动前做二次验证（env setup 后 provides 可能因参数变化）。

### 13.4 Observation 完整序列化（已决定）

完整序列化每步的 NegotiatedObservation。理由：存储不值钱，API 和时间值钱。完整 trajectory 数据可直接用于下游 RL 训练。TraceWriter Hook 序列化时利用 ObservationRegistrar 的 discriminated union 能力。

### 13.5 ToolBench 动态 tool 定义来源（已决定）

Tool 定义来自 **TaskLoader.get_task_context()**，每个 task 提供不同的 tools。流程：TaskLoader 提供 tools → AgentRuntime 传给 ToolSandboxCapability.setup(task_tools=...) → Capability.get_action_descriptors() 动态生成 → FunctionCallingLoop 转换为 LLM tools 参数。每换一个 task 重新 setup + 重新生成 descriptors。

### 13.6 conscribe 三元素 tuple（已确认）

conscribe 0.6.1 将支持 `__wiring__` Mode 2 三元素 tuple（`required + optional`），`WiringSpec` 扩展 `optional_keys` 字段。

---

## 14. 待决问题

### 14.1 Memory 消息协议的具体设计

Loop 声明 `"memory": ("memory", ["user_assistant", "tool_call"])` 中的协议 key 具体是什么？需要定义：
- 基础协议 key 清单（`user_assistant`, `tool_call`, `planning`, `code_execution` 等）
- 每种协议对应的 BaseMessage 子类型
- Memory 实现如何声明和适配不同协议

### 14.2 Hierarchical / Multi-agent 编排

DD 1.4 提到走独立的 A2A 通讯协议设计，不属于 agent loop 范畴。具体设计待定。
