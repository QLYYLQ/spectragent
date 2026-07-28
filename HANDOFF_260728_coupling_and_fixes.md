# spectragent (All4One) — 架构冲突与修复交接报告

> **生成日期**：2026-07-28
> **审查基线**：`main` @ `d830831` ("fix 非整数比例缩放导致element坐标错误的问题")
> **审查目标**：以**代码为准**（文档为辅），评估当前实现相对于设计目标
> **multi-agent × multi-domain × multi-benchmark** 的耦合与阻塞
> **测试基线**：`uv run pytest` → **560 passed in 2.28s**（全绿；见 §3.3 为何这不构成保证）

---

## 0. 如何使用这份文档

### 0.1 置信度标记

本文每一条结论都带标记，**不要一视同仁地相信**：

| 标记 | 含义 |
|---|---|
| `✅R` | 主审查进程**亲自 live repro**（跑了 Python 复现） |
| `✅V` | 经**独立对抗性验证**（一个专门被要求"反驳它"的 agent 打不倒） |
| `✅D` | **磁盘/实测证据**（真实 trace 文件、pytest 输出） |
| `⚠️U` | 报告了但**未做对抗验证**（因 top-3/lens 的验证配额）—— 动手前请先自己核 |
| `❌X` | 被验证驳回或严重降级 —— **记录在案以免重复发现** |

### 0.2 阅读路径

- **只想知道该干什么** → §7 修复计划
- **想知道"为什么框架现在是这样"** → §2 主干 + §2.3 失败签名
- **要动某个子系统** → §5 冲突清单里 grep 文件名
- **要写新 domain / 新 benchmark / 新 agent** → §4 三条轴的实测结论
- **接手 debug** → §6 Bug 清单

### 0.3 一个前置说明

CLAUDE.md §0 写着"**Source of truth is `docs/architecture_design.md`, not the code**"。
**这次审查反过来**：用户明确要求"以实现为主，文档为辅"，因为文档和代码都有过时内容。
所以本文所有"分歧"条目的写法是：**代码做了什么** → **文档声称什么**，
并且**不假设文档是对的** —— 有些地方是代码落后于文档，有些地方是文档描述了从未实现的东西。

---

## 1. TL;DR

### 1.1 一句话

> 骨架是真的，但**装配层是断的**，**记录层记不下足够评测的东西**，
> 而**实例身份模型只有"类"没有"个体"**，这三件事分别卡死 multi-domain、multi-benchmark、multi-agent。

### 1.2 十条最重要的

| # | 结论 | 证据 | 轴 |
|---|---|---|---|
| 1 | `ConfigComposer` **永远不注入 `env.capabilities`** —— committed 的 YAML 路径端到端是死的 | `✅R` | 全部 |
| 2 | 实例身份 = conscribe **registry key（类身份）** —— 同类两个实例物理上无法表示 | `✅V` | multi-agent |
| 3 | `AgentLoopBase.weave` 把运行态存在 **loop 实例**上 —— 第二次 weave 静默改写第一个 agent 的 env/llm/memory | `✅V` | multi-agent |
| 4 | `session_id`（CDP tab handle）是**框架级 Action ABI 的必填参数**，且**没有任何生产者** | `✅V` | multi-domain |
| 5 | WAL step record 只有 `{step, llm_output}`，**没有任何 hook 拦截 `act`** | `✅D` | multi-benchmark |
| 6 | **没有 evaluator 注册表**、没有 `spectragent/evaluator/`、没有 benchmark runner | `✅R` | multi-benchmark |
| 7 | `output_format=None` 写死 —— **结构化输出全框架是死的**；而且**修了会立刻全崩** | `✅V` | 正确性 |
| 8 | 每个 LLM 的 **`provider:` 子配置块被静默丢弃** —— Azure/Bedrock/OpenRouter 参数 YAML 不可达 | `✅R` | 配置 |
| 9 | timeout/exception 路径**不 dispatch `on_run_end`** —— 14 个真实 trace 里 **9 个**永远 `status: running` | `✅D` | multi-benchmark |
| 10 | agent 的 `__wiring__` 硬编码**全限定 key**（`browser.click`）—— agent 与 capability 类型级焊死 | `⚠️U` | multi-domain |
| 11 | **构建出的 wheel 里没有任何非 `.py` 文件** —— `pip install` 后 agent 第一次 `prepare()` 就 `FileNotFoundError` | `✅R` | 打包 |

### 1.3 三条轴的一句话判决

- **multi-domain**：`core/` 在 *import* 层是干净的（零 `capabilities.browser` / `agent_cdp` 跨界 import），但在 **ABI 层**是浏览器形状的。加一个 ToolSandbox/SWE capability 至少要改 `core/actions/base.py`、`core/environments/{base,_registrar}.py`、`core/loops/react.py`、`core/runtime.py`、`schemas/observation/{fragments,dynamic_model}.py`、`schemas/action.py`、`schemas/agent_result.py`、`core/trace/{wal,reader}.py`。
- **multi-benchmark**：Record Once, Evaluate Many 的 **evaluate 半边完全不存在**，而且 **record 半边记不下足够评测的东西**。task loader 层硬编码 `benchmark_data/browser/` 并强制给每个 task 打 `setup_type="browser"`，非 browser benchmark **连加载都做不到**。
- **multi-agent**：被**身份模型**卡死，不是缺功能。三种形态实测：**(a) 编排型无法表示**（conscribe 生成标量 Literal → `UnresolvedWiringError`）；**(b) 共享 Env 能组装不能跑**（`ComposeError` + weave 覆盖）；**(c) 各自 Env 不可能两次**（env 按 `env_type` 塌缩 + TraceWriter 单全局 step 计数）。最危险的不是硬失败，是**软失败**：并发 agent 会产出归属错乱的 trace，且每个 agent 报告所有 agent 的 token。

---

## 2. 框架现状：代码里真实的主干

### 2.1 真实调用链（不是文档里的）

```
demo_yaml_compose.py:84  /  demo_debug.py            ◄── spectragent/ 内部没有任何 entry point
 │                                                       (唯一的 __main__ 是 scripts/generate_all.py)
 ├─ ConfigComposer.__init__                                composer.py:141
 │    conscribe.discover(spectragent.{agents,core,schemas}) composer.py:155  (刻意排除 .generated)
 │    build_composed_config(registrars, inline_wiring=True) composer.py:167
 │
 ├─ compose(raw) ──► ComposedRun(by_layer, top_level_cfg)   composer.py:184-198
 │    _instantiate() per (layer, registry_key), DEDUPE       composer.py:211-213  ★ 类身份去重
 │      └─ _resolve_wired() 只对 type(cfg).model_fields 里的字段生效
 │                                                          composer.py:225,231  ★ env.capabilities 永不被访问
 │      └─ 普通字段: if field_name not in init_params: continue
 │                                                          composer.py:264-265  ★ provider: 块被丢弃
 │
 └─ AgentRuntime.from_composed(run).execute(task)           runtime.py:60,140
      prepare()                                             runtime.py:66
        1. loop._validate_slots(agent)                      runtime.py:87   鸭子检查 callable()，非 isinstance
        2. negotiate_observation(agent, loop, env)          runtime.py:90   ★ loop 方恒为 (∅,∅)
        3. negotiate_action(agent, env)                     runtime.py:93
        4. negotiate_memory(loop, memory)                   runtime.py:96
        5. Weaver(hooks).wrap(x, role∈{agent,env,llm})      runtime.py:101-103  ★ 3 个字面量 role
        6. env.get_action_space(active) → build_guarded_act runtime.py:106-113
        7. loop.weave(agent, **8 个 ctx key)                runtime.py:116 ─► loops/_registrar.py:118,124
      agent.prepare(task, descs) → memory.add_messages      runtime.py:163-170  ★ 只在 execute() 里，prepare() 不做
      _dispatch_lifecycle("on_run_start")                   runtime.py:173  ★ 不传 task_id / loop / llm
      asyncio.wait_for(prepared_agent.run(task))            runtime.py:183
        └─ ReactLoop._execute                               react.py:91     ~200 行单函数，零抽取
             env.observe(negotiated_obs, session_id)        react.py:127 ─► environments/_registrar.py:187
                build_observation_model(...)                每步重建一个 Pydantic 类
                cap.resolve_fragment(k, session_id=)        environments/_registrar.py:193
             populate_loop_fragments(...) → model_copy      react.py:130 ─► _loop_fragments.py:51  ★ 返回新对象
             agent.build_react_prompt(obs)                  react.py:146
             memory.add_messages / get_context_messages     react.py:147-148
             output_model = agent.get_react_output_model()  react.py:157   ★ DEAD STORE
             llm.ainvoke(context, output_format=None)       react.py:162   ★ 写死 None
             agent.parse_react_output(raw)                  react.py:172
             act_fn(action, session_id=) ─► guarded_act ─►  react.py:252 → action_guard.py:59
               env.act (前缀路由) ─► cap.execute_action     environments/_registrar.py:144,162
                 ─► impl.execute(params, capability=self, session_id=sid)   capability.py:528
      _dispatch_lifecycle("on_run_end", result)             runtime.py:189  ★ 仅成功路径
                                                            ★ timeout(:216) / exception(:225) 永远到不了这里

不在链路上：env.setup() / env.teardown()   ── 只有 demo 脚本调用
不在链路上：TaskLoader / EvalLoader        ── 注册了，by_layer 恒为 {}
不存在   ：evaluator / benchmark runner
```

### 2.2 承重的四个想法

| # | 想法 | 位置 | 兑现程度 |
|---|---|---|---|
| **(a)** | conscribe `__wiring__` 三元组 `(registry, required, optional)` 同时变成 `Literal[...]` 配置约束 **和** `WiredField` setattr receptor | `agents/_registrar.py:40`、各 agent/capability | **约束层兑现了**（`_composed.py:813` 的 `observation: Literal[...]` 是真的）；**注入层没兑现**（§5.1.1） |
| **(b)** | 协商是纯函数，在任何副作用之前产出冻结结果 | `core/negotiation/` | **一半兑现**：env 方真的检查，loop 方恒空、fragment 名冲突不检查（§5.2.3、§5.5.2） |
| **(c)** | `build_guarded_act` 用名字集合闭合 prompt↔capability 缺口 | `core/action_guard.py:59` | **完全兑现**。这是全仓库最干净的一块 |
| **(d)** | AOP 方法拦截取代 emitter | `core/aop/` | **机制兑现，绑定没兑现**：role 是 3 个字面量，`agent.run` 结构性不可拦截，handler 拿不到 target（§5.5.3） |

### 2.3 一以贯之的失败签名 ★

> **声明层严谨，消费层各自重新推导。**

conscribe 忠实地把 `__wiring__` 变成类型化 config 和 registrar —— 然后
`ConfigComposer` / `AgentRuntime` / `ReactLoop` / `BrowserCapability` / `TaskLoaderBase`
**每一个都用一条手工维护的旁路重新推导自己要的东西**。
结果永远是同一个形状：**config 通过校验、协商通过、值被静默丢弃或静默变错**。

| 消费方 | 旁路 | 后果 | 条目 |
|---|---|---|---|
| `composer.py:225` | 按 `type(cfg).model_fields` 驱动注入 | `env.capabilities` 永不注入 | §5.1.1 |
| `composer.py:264` | `if field_name not in init_params: continue` | `provider:` 整块丢弃 | §5.1.3 |
| `react.py:321` | loop fragment builder 是字面量 dict | 新 Loop / 新 loop fragment → step-0 KeyError | §5.5.2 |
| `capability.py:981` | watchdog 是三张字符串表 + 函数内 factory | "加 fragment = 加一个类"是假的 | §5.5.4 |
| `runtime.py:101` | AOP role 是 3 个字面量 | `role="capability"` 注册成功、永不触发 | §5.5.3 |
| `task/_registrar.py:129` | `setdefault("setup_type","browser")` | 非 browser benchmark 静默变成 browser task | §5.3.4 |

**这是本次审查最有价值的单条结论**：6 个独立审查视角里有 4 个撞上同一个形状。
修单点是打地鼠；真正该问的是"为什么每个消费方都要自己再推导一遍"。

---

## 3. 事实基线

### 3.1 主审查进程亲自复现的（`✅R`）

```python
# ── R1. env.capabilities 永不注入 ────────────────────────────────
run = composer.compose(yaml.safe_load(open('configs/browser_use_local.yaml')))
run.env.capabilities
# AttributeError: Wired attribute 'capabilities' on BrowserEnvironment has not
#   been injected yet. Expected injection from registry 'capability'.
# 而 capability 实例本身是建好的：
run.instances('capability')
# [BrowserCapability(..., backend=LocalChromeProvider(...))]   ← 建好了，没接上

# ── R2. provider: 子配置被丢弃 ──────────────────────────────────
raw['agent'][0]['llm'] = {'model_type':'openai','model':'gpt-4o',
    'provider':{'name':'azure','azure_endpoint':'https://X.openai.azure.com',
                'api_version':'2024-10-01'}}
run.agent.llm
# ChatAzureOpenAI | model=gpt-4o
#   azure_endpoint = None
#   api_version    = None
#   azure_deployment = None

# ── R3. generated/_composed.py import 即炸，被静默吞掉 ──────────
import spectragent.generated._composed
# NameError: name 'AwsProviderConfig' is not defined
from spectragent.generated import ComposedConfig
# None        ← generated/__init__.py:15 的裸 except Exception 吞了

# ── R4. agent_cdp 自带 WatchdogRegistrar，spectragent 零引用 ────
from agent_cdp import WatchdogRegistrar     # 导入成功
$ grep -rn "WatchdogRegistrar" spectragent/ tests/
# (无输出)
```

### 3.2 磁盘证据（`✅D`）

对 `traces/` 下 **14 个真实 run** 的普查：

| trace | steps | observations | result.json | status |
|---|---|---|---|---|
| 0f42f6ec | 5 | 20 | **NO** | **running** |
| 1b699d30 | 7 | 28 | **NO** | **running** |
| 1e7ead34 | 17 | 68 | **NO** | **running** |
| 3ce48f5d | 7 | 28 | yes | completed |
| 5fad93ca | 7 | 28 | yes | completed |
| 60fe02a7 | 7 | 28 | **NO** | **running** |
| 86db4922 | 8 | 32 | yes | completed |
| 88c1e67e | 11 | 44 | yes | completed |
| a0737f78 | 18 | 72 | **NO** | **running** |
| b65cb199 | 4 | 16 | **NO** | **running** |
| c1b8e387 | 5 | 20 | **NO** | **running** |
| e09dfe4a | 5 | 20 | **NO** | **running** |
| ef5d6ba9 | 30 | 120 | yes | completed |
| fd305ce1 | 2 | 8 | **NO** | **running** |

**9/14 永远停在 `running` 且无 `result.json`** —— 见 §5.3.5。

每一步的 step record 长这样（`traces/1b699d30/steps/000.json`）：

```json
{
  "step": 0,
  "llm_output": "{\n  \"memory\": \"...\",\n  \"action\": [{\"browser.navigate\": {\"url\": \"...\"}}]\n}"
}
```

**没有 actions，没有 action_results，没有 token_usage，没有 observation_summary。**

每步的 observation 文件恒为 4 个：`NNN_dom.json`、`NNN_screenshot.json`、
`NNN_browser_meta.json`、`NNN_timestamp.json`
—— **从来没有 `task` / `step_info` / `history`**，尽管 `browser_use.py:88` 把 `loop.task` 标为 **required**。
（`NNN_timestamp.json` 恒为 `0.0`，是垃圾文件，见 §6.7.4）

每个 `config.json` 恒为：
```json
{"agent_id":"browser_use","loop_id":"","env_type":"","llm_model":"","task":"...","task_id":"","extra":{}}
```
—— `task_id` 恒空，即使 `result.json` 证明该 run 确实有 `task_id='demo-debug-001'`。

### 3.3 为什么 560 个测试全绿不构成保证

```
$ uv run pytest -q
560 passed in 2.28s
```

**2.28 秒 / 560 个测试** = 纯 mock 单测，零真实 I/O、零集成。
更关键的是 **测试主动绕过了生产路径**：

- `tests/_helpers/assembly.py:45` → `env.capabilities = caps_map  # type: ignore[attr-defined]`
  手工 setattr，正是 §5.1.1 那个 blocker 的绕过。整个 60 个 runtime/environment 测试**结构性看不见它**。
- `tests/test_core/test_environments/test_registration.py:25` → `instance.capabilities = {}` 同上。
- `tests/test_core/test_trace/test_reader.py:136` 的 fixture 写**裸 bytes**，
  而生产 writer 永远写 Pydantic 对象 —— 测试通过，但断言的契约在生产中从不成立（§6.5.3）。
- `core/llm/` 子系统 **pytest 覆盖率为 0**（没有 `tests/test_core/test_llm/`）。

**验收标准建议**：任何修复必须附带一个"组装 shipped YAML 并调 `prepare()`，不打任何补丁"的集成测试。

---

## 4. 三条轴的实测结论

### 4.1 multi-domain — 思想实验的答案

**问题**：我要加一个 `ToolSandboxCapability`（tau-bench 风格：无 DOM、无 tab、无截图，动作是 JSON tool call）
和一个 `SWECapability`（repo checkout + shell；观测是文件树和命令输出）。
`spectragent/core/` 里 `capabilities/browser/` **之外**我必须改什么？

**答案（实测）**：

| 必须改的文件 | 为什么 |
|---|---|
| `core/actions/base.py` | `execute(..., session_id: str)` 必填、且 docstring 教你去摸 `_scopes`/`_selector_maps`/`_cdp` |
| `core/environments/base.py` + `_registrar.py` | `observe/act` 签名带 `session_id`；`setup(**kwargs)` 无区分广播 |
| `core/loops/react.py` | `session_id` 透传；loop fragment builder 硬编码 |
| `core/runtime.py` | `prepare/execute` 的 `session_id` 参数；AOP 3 role 字面量 |
| `schemas/observation/fragments.py` | 硬编码 browser DOM dataclass 内部（含私有字段 `_compound_children`），零测试覆盖 |
| `schemas/observation/dynamic_model.py` | 剥前缀导致跨 capability 同名 fragment 冲突 |
| `schemas/action.py` | `ActionResult` 只有一个 `extracted_content: str` 载荷 |
| `schemas/agent_result.py` | 顶层保留 `browser_id` / `screenshots` |
| `core/trace/wal.py` + `reader.py` | 唯一的二进制词汇是 PNG；唯一的内容访问器是 `screenshots` |
| `core/data_loader/_common.py` + `task/_registrar.py` | 硬编码 `browser/` 目录 + 强制 `setup_type="browser"` |
| `core/composer.py` | 否则 `env.capabilities` 根本注入不进去 |

**外加一条类型级阻塞**（`⚠️U`）：
`ActionBase.__registry_key__ = "{source}.{local_id}"`，`negotiate_action` 做的是**全限定名集合减法**。
`agents/ui_tars.py:45-59` 硬编码：
```python
"capability": ("capability", ["browser"]),
"action": ("action", ["browser.click_coordinate", "browser.type", "browser.done"], ...)
```
UI-TARS 自己的配置项写着 `prompt_template: COMPUTER_USE | MOBILE_USE | GROUNDING`，
prompt 放在 `prompts/browser/agents/ui_tars` —— 一个 **computer-use 模型被永久钉死在 browser 上**。
要跑 OSWorld，`desktop.click_coordinate` 和 `browser.click_coordinate` 是两个不同 registry key，协商必然失配。
`environments/_registrar.py:36-38` 显示这是从"运行时加前缀"**主动改成**"注册键带前缀"的。

### 4.2 multi-benchmark — 新 benchmark 今天要提供什么

**答案**：一个 `TaskLoader` 子类和一个 `EvalLoader` 子类。**然后就没有然后了** ——
没有地方注册评分器，没有 runner 会调用它们，产出的 trace 也没有 benchmark 身份。

具体到目标 benchmark：

| Benchmark | 交付物 | 今天卡在哪 |
|---|---|---|
| SWE-bench | unified diff + `FAIL_TO_PASS`/`PASS_TO_PASS` 测试通过 | 数据路径硬编码 `browser/`；`setup` 强制 `BrowserSetup`（无 `DockerSetup`）；`answer: str` 装不下 patch；跑测试需要**存活的容器**，而 `on_run_end` 只拿到 result |
| tau-bench | tool-call 轨迹 + 最终 DB state | 消息模型**没有 tool role**，`ainvoke` **没有 `tools` 参数** —— 结构性无法表示；WAL 不记 action |
| GAIA | 精确字符串匹配 | 最接近可行，但没有 evaluator 注册表；trace 无 benchmark 身份 |
| OSWorld | VM 状态谓词 | 同 SWE-bench 的存活环境问题 + `desktop.*` registry key 问题 |
| KernelBench | 加速比 + 实测 timing | `answer: str` 装不下结构化 timing |
| Online-Mind2Web | **WebJudge 图像评委**（prompt 已在仓库里！） | `RunTrace.screenshots` 恒返回 `[]`（§6.5.3）→ 评委静默降级到弱的纯文本路径 |

### 4.3 multi-agent — 三种形态逐一实测

用户明确要求**三种形态全要**。实测结果：

#### (a) 编排型 orchestrator → workers：**无法表示** `✅V`

```python
# 在 AgentBase 子类上声明
__wiring__ = {..., "workers": ("agent", ["ui_tars"])}
workers: dict[str, Any]
```
conscribe 生成的字段是 `typing.Literal['ui_tars']` —— 一个**类名字符串**，不是嵌套 config。
于是：
```
YAML: agent: [{agent_id: orch_probe, workers: ui_tars}]
  → UnresolvedWiringError: agent[0].workers: expected list of nested configs
    or a single nested config, got str='ui_tars'            (composer.py:327)

YAML: agent: [{agent_id: orch_probe, workers: [{agent_id: ui_tars}, ...]}]
  → ValidationError: Input should be 'ui_tars'
```
**没有第三种写法。声明委派本身就让 config 变得不可加载。**

> 注意这里的**不对称**：`EnvironmentBase.capabilities` 的裸类注解产出**零个** config 字段（静默失败），
> 而 agent→agent 产出一个**标量 Literal**（响亮失败）。同一个根因，两种不同的失败模式。

#### (b) 协作型 两个 agent 共享一个 Environment：**能组装，不能跑** `✅V`

```python
run.agent
# ComposeError: Layer 'agent': expected exactly 1 instance, got 2.   (composer.py:81→67-71)
```
绕过 `single()` 之后更糟 —— 两个 agent 共用**同一个** `ReactLoop`：
```python
loop = ReactLoop()
loop.weave(a1, env='ENV1', llm='LLM1', memory='MEM1', act_fn='ACT1')
loop.weave(a2, env='ENV2', llm='LLM2', memory='MEM2', act_fn='ACT2')
loop.ctx   # {'env':'ENV2','llm':'LLM2','memory':'MEM2','act_fn':'ACT2'}
await a1.run('t')   # ← 跑在 ENV2 上
```
**编排者的 guarded_act（窄化到委派动作）被 worker 的 guarded_act（浏览器动作）替换。**
action guard 的全部意义就是 prompt/capability 一致性 —— 它现在守错了 agent。

#### (c) 评测型 / 各自 Env：**两重不可能** `✅V`

- 两个 `environment` 条目按 `env_type` **塌缩成一个实例**（`composer.py:211` 的类身份去重）。
- `TraceWriter` 每次 `on_run_start` **新开一个 uuid 目录**，用**单个全局 `_current_step`**。
  实测两次 `on_run_start` → 两个不相干的目录（`7f8c6133`、`bb021e0c`），第一个被遗弃在 `status: running`。

#### (d) 最危险的不是硬失败，是软失败 `✅V`

同类两个实例**静默塌缩，丢弃第二个的配置**：
```python
# 两个 agent，各自带 inline 的 llm/memory/capability
a.agent_loop is b.agent_loop  # True
a.memory     is b.memory      # True   ← ui_tars 要 max_messages:50，拿到 200
a.llm        is b.llm         # True   ← 要 gpt-4o-mini，拿到 gpt-4o
a.capability is b.capability  # True
```
**两个 agent 共用一个 memory buffer 意味着 agent B 读得到 agent A 的对话。**

并发跑一个共享 TraceWriter（两个协程，中间一个 `await asyncio.sleep(0)`）：
```
steps/000.json            == {'step': 0, 'llm_output': 'OUT_ORCH_0'}
observations/000_observation.json == 'OBS_WORKER_0'     ← 观测归属错了 agent
文件数：4 步只产出 3 个 observation —— OBS_ORCH_0 被静默丢失
```

> **判决**：框架会先**自信地记录错误的数据**，很久以后才有人发现它其实不支持 multi-agent。

---

## 5. 冲突清单

### 5.1 装配层（Composer / 配置）

#### 5.1.1 `ConfigComposer` 永远不注入 `env.capabilities` — **BLOCKER** `✅R` `✅V`

**四个独立视角收敛到同一行。**

```python
# composer.py:225 —— 注入由 *config model 的字段* 驱动，而不是由类的 wiring 驱动
for field_name in type(cfg).model_fields:
    ...
# composer.py:239-247 —— 对 capabilities 永远到不了
is_receptor = (field_name in init_params
               or field_name in class_hints
               or isinstance(getattr(cls, field_name, None), WiredField))
```
```python
# environments/browser.py:19-21
__wiring__ = {"capabilities": ("capability", ["browser"])}
# generated/_composed.py —— BrowserEnvironmentConfig 只有一个字段
class BrowserEnvironmentConfig(BaseModel):
    env_type: Literal['browser'] = 'browser'
```
实测：`parse_wiring(BrowserEnvironment)` → `['capabilities']`，
`isinstance(BrowserEnvironment.capabilities, WiredField)` → `True`，
但 `list(BrowserEnvironmentConfig.model_fields)` == `['env_type']`。

**失败链路**：`prepare()` → `runtime.py:90 negotiate_observation`
→ `observation.py:104 env_provides=set(env.provided_fragments)`
→ `environments/_registrar.py:84 for cap in self.capabilities.values()` → `AttributeError`。

**为什么至今没人发现**：
- `tests/_helpers/assembly.py:45` 手工 setattr
- `demo_debug.py:251-268` 有个函数就叫 `inject_env_capabilities()`，docstring 写着 "works around a Composer gap"
- `configs/browser_use_local.yaml:58-63` 的注释**描述了代码行为的反面**

**额外损害**：`capabilities` 是全包**唯一**的 `dict[X]` 型 wired receptor，
所以 `_resolve_wired` 的 dict/list 分支（`composer.py:294-299`）—— 多 capability 组合恰恰需要的那段机器 ——
**零真实类覆盖**，只被 `tests/test_core/test_composer/` 的合成 registrar 摸过。

#### 5.1.2 实例身份 = registry key（类身份） — **BLOCKER** `✅V`

```python
# composer.py:211-213
cached = by_layer[layer].get(key)      # key == __registry_key__，是类身份
if cached is not None: return cached
# composer.py:276
by_layer[layer][key] = instance
# composer.py:296 —— dict 型 receptor 也用类身份做 key
return {_registry_key_of(inst, path): inst for inst in items}
```
后果见 §4.3(d)。**这是 multi-agent 的根**，也是"两个 gpt-4o 实例"、"两个 browser tab env"、
"orchestrator 和 worker 各有 memory" 全部做不到的原因。

#### 5.1.3 每个 LLM 的 `provider:` 子配置块被静默丢弃 — **MAJOR** `✅R`

```python
# composer.py:263-266
# Regular config field.
if field_name not in init_params:
    continue  # conscribe sometimes adds declarative-only fields
init_kwargs[field_name] = _unwrap_nested_model(value)
```
`provider` 在**全部 15 个 provider 上都是只读 `@property`**，永远不是 `__init__` 参数 → 整块丢弃。

**YAML 不可达的参数清单**：

| provider | 丢失的字段 |
|---|---|
| `openai.azure` | `api_version`, `azure_endpoint`, `azure_deployment`, `azure_ad_token`, `use_responses_api` |
| `anthropic.aws` | `aws_access_key`, `aws_secret_key`, `aws_session_token`, `aws_region` |
| `openai.openrouter` | `http_referer`, `extra_body` |
| `openai.vercel` | `provider_options` |
| `openai.cerebras` / `openai.deepseek` | `client_params` |

Azure 和 Bedrock 只能靠环境变量兜底；其余**根本没法配**。
而 `configs/browser_use_local.yaml:26-29` 里就明晃晃写着 `provider: {name: direct}`。

#### 5.1.4 `generated/_composed.py` import 即抛 `NameError`，被静默吞掉 — **MAJOR** `✅R`

```python
# generated/__init__.py:13-16
try:
    from spectragent.generated._composed import ComposedConfig
except Exception:  # noqa: BLE001 — codegen may be mid-update or have latent issues
    ComposedConfig = None
```
```
NameError: name 'AwsProviderConfig' is not defined
  at _composed.py:371  provider: AwsProviderConfig = AwsProviderConfig(name='aws', ...)
```
**9 个 `*ProviderConfig` 类**被当作注解**和运行时构造调用**发射，但从未被定义：
`AwsProviderConfig`、`DirectProviderConfig`、`DefaultProviderConfig`、`AzureProviderConfig`、
`CerebrasProviderConfig`、`DeepseekProviderConfig`、`GroqProviderConfig`、`OpenrouterProviderConfig`、`VercelProviderConfig`。
更糟：`DirectProviderConfig` 被发射**两次**且形状不兼容（`:391` anthropic、`:586` openai）。

**根因在 conscribe**：`config/codegen.py:597-618` 的 `_value_to_source` 把嵌套 pydantic model `repr()` 进源码。
（conscribe 是自有库，可改。）

**连带影响**：
- `discover('spectragent')` 会在 `_composed.py:371` 崩
- `spectragent-gen` 只是因为 `generate_all.py:83-86` 在 `:91 discover()` **之前**先 unlink 了 `_composed*` 才活着 —— 这个顺序依赖是承重的
- `ConfigComposer` 靠 `composer.py:151-156` 显式排除 `spectragent.generated` 并在内存里重建同样的模型来躲开 —— **意味着 committed 的那个产物是装饰性的**

#### 5.1.5 Hook 无法从 YAML 配置 — **MAJOR** `⚠️U`

conscribe 从 `__init__` 参数派生 config 字段；`HookBase` 一个都没定义
→ `TraceWriterHookConfig`（`generated/_composed.py:334-338`）**只有 `hook_id`**
→ `TraceWriter(trace_dir='x')` 抛 `TypeError`。
每个使用方只能事后 setattr（`demo_debug.py:343`）。

#### 5.1.6 `action_overrides` / `mouse_trajectory_overrides` YAML 不可达 — **MAJOR** `⚠️U`

`capability.py:209,217` 是普通 dataclass 字段，在 `__config_annotated_only__ = True` 下不进 config，
而 `BrowserCapabilityConfig` 是 `extra="forbid"`。

**后果**：设计文档指着当作*主*配置面的**每一个 per-action 旋钮**都只能在 Python 里改 ——
`SettleableAction.post_delay_ms`、`ScrollAction.max_amount`、`NavigateAction.wait_until`、
`ScreenshotAction.output_dir`、`WaitAction.max_seconds`、全部 trajectory 参数。
而 `actions/_mixins.py:29-31` 还在教用户"通过 `BrowserCapability.action_overrides` 配置" —— YAML 会直接拒绝。

#### 5.1.7 三个 registrar 被排除在 codegen 之外 — **MINOR** `⚠️U`

`generate_all.py:40-75` 漏了 `MouseTrajectoryRegistrar`、`DOMCleanerRegistrar`、`DOMSerializerRegistrar`
→ `generated/_composed.py:780` 发射 `mouse_trajectory: str`（裸 str）而不是 `Literal`
→ 拼错会被 `mouse.py:90-100` 的裸 `except Exception` 静默降级成 `instant`（§6.6.4）。

---

### 5.2 multi-domain 耦合

#### 5.2.1 `session_id` — CDP tab handle 进了框架 ABI — **BLOCKER** `✅V`

**四层协议全被穿透**：

| 层 | 位置 | 签名 |
|---|---|---|
| Action Protocol | `core/actions/base.py:37-43` | `execute(params, *, capability, session_id: str)` — **必填、非 Optional** |
| Environment Protocol | `core/environments/base.py:53-57, 72-76` | `observe(negotiated, session_id)` / `act(action, session_id)`，注释写着 "Optional session/**tab** routing" |
| Runtime | `core/runtime.py:69, 124, 145, 154` | `prepare/execute(*, session_id=None)` |
| Loop | `core/loops/react.py:112, 127, 252` | `ctx["session_id"]` 一路透传 |

**唯一的定义方在浏览器 capability**：`capability.py:445` `self._default_session_id = sid`
（来自 CDP `Target.attachToTarget`），`:509` `sid = kwargs.get("session_id") or self._default_session_id`，
`:513-514` `return ActionResult(success=False, error=f"Unknown session_id: {sid}")`。

**关键事实**：**没有任何框架组件能构造它**。
grep 全仓库 + demo 脚本，零个非 None 的生产者。生产路径上恒为 `None`，
最后被 `or self._default_session_id` 吞掉。**纯耦合，零收益。**

`EnvironmentBase` 还把这个标量**不加区分地广播给每个 capability**
（`_registrar.py:162` act、`:193-196` observe）。

**实测破坏**：注册一个真实 `ToolSandboxCapability` 后，
`env.act(Action(name='tool_sandbox.call_tool'), session_id='CDP-TAB-ABC')`
把一个 Chrome DevTools target sessionId 直接塞进 `CallToolAction.execute`。
反过来，未来若有多租户 runtime 传一个 run-scoped id，
`capability.py:513-514` 会让**每一个浏览器动作**返回 `success=False, error="Unknown session_id: run-7"`。

> **验证者的修正**：原报告推论"两个 agent 共享 env 会因 session_id 冲突"被驳回 —— 今天根本没有 multi-agent 路径，
> 那条推论 unreachable。但**结构性判断被确认**：这是唯一的 tenancy 维度，而它是个浏览器 tab id。

#### 5.2.2 Action ABI 注入整个 Capability 且 docstring 祝福访问私有态 — **MAJOR** `❌X→MINOR`

```python
# core/actions/base.py:44-51 —— 框架级 Protocol
"""Execute one action instance against the injected capability.

The capability is injected as a whole to avoid designing a
per-action "dependency bag". Actions are conceptually internals
of their capability, so they may touch its private state
(``_scopes``, ``_selector_maps``, ``_cdp``, etc.).
"""
```
**12 个** action 文件真的在 `capability._scopes[session_id]`（不是原报告说的 2 个）。
`capabilities/_registrar.py:94` 还 duck-type 了子类私有名 `_actions_by_name`。

> **降级理由**（对抗验证）：新 capability 可以定义自己的私有面，这**不构成 multi-domain 阻塞**。
> 保留的部分是：**框架级 Protocol 的文档里写着浏览器私有字段名**，
> 这是让每个未来 domain 都抄错模式的**设计声明**。

#### 5.2.3 `NegotiatedObservation` 剥掉 capability 前缀 — **MAJOR→MINOR** `✅V`

```python
# schemas/observation/dynamic_model.py:53-61
for key in sorted(active_fragments):
    field_name = key.rsplit(".", 1)[-1]      # ← 剥前缀
    if field_name in fields:
        raise ValueError("Duplicate observation field name ...")
```
而 `negotiate_observation`（`observation.py:70-77`）**没有等价检查**。
`build_observation_model` 的唯一生产调用点在 `environments/_registrar.py:187` —— **每步调一次**。

**实测**：`swe.terminal` + `docker.terminal` 同时激活 → 协商静默通过 →
`env.observe()` 在 **step 0** 抛 `ValueError: Duplicate observation field name 'terminal'`。

> **验证者的两处修正**：
> 1. **内存中 provenance 没丢** —— `obs.terminal.fragment_id == "docker.terminal"` 可用（`fragment_id` 是 `@property`）。
>    只在 **WAL** 里丢：`model_dump()` 不含 property，且 `trace_writer.py:129` 用剥掉前缀的字段名命名文件。
> 2. 这是**刻意的 fail-fast**（有准确的错误信息），且**今天不可达**（9 个已注册 fragment 无同名冲突）。
>
> **但它暴露了一个真实的不对称**：`actions/_registrar.py:36` 明确写着保持 capability
> "collision-free (browser.click vs desktop.click)"，Action 端端到端保留全限定名 ——
> **Observation 端剥掉**。架构文档声称的 "Action system — symmetric to Observation" 是假的。

#### 5.2.4 `schemas/observation/fragments.py` 硬编码 browser DOM 内部 — **MAJOR** `⚠️U`

```python
# schemas/observation/fragments.py:27
_DOM_SKIP_FIELDS: frozenset[str] = frozenset(
    {"parent", "content_document", "shadow_roots", "_compound_children"}
)
```
共享的、号称 domain-neutral 的 fragment 模块里写死了 browser DOM dataclass 的字段名，
**包括一个私有字段 `_compound_children`**，且**零测试覆盖**。

#### 5.2.5 消息模型没有 tool role，`ainvoke` 没有 `tools` 参数 — **BLOCKER** `⚠️U` ★

```python
# core/llm/messages.py:128
role: Literal["user", "system", "assistant"]
# core/llm/messages.py:236
BaseMessage = UserMessage | SystemMessage | AssistantMessage
# core/llm/base.py:46-48
async def ainvoke(self, messages, output_format=None, **kwargs) -> ...
```
`AssistantMessage.tool_calls` 是**出站的一半，没有入站对应物**。

**原生 function calling —— τ-bench / ToolBench / GAIA 和大多数 SWE agent 的主要交互模式 ——
是结构性无法表示的，不只是没实现。**

更糟：`sliding_window.py:25` 和 `token_budget.py:31` **宣称** `supports_protocols = {"user_assistant", "tool_call"}`，
所以 memory 协商会通过，然后在构造消息时才炸。

> ⚠️ **这条我标了未验证，但如果属实，它挡住的是一整类 benchmark。建议优先复核。**

#### 5.2.6 `EnvironmentBase.setup(**kwargs)` 无区分广播 — **MINOR** `⚠️U`

```python
# environments/_registrar.py:214-216
async def setup(self, **kwargs):
    for cap in self.capabilities.values():
        await cap.setup(**kwargs)
```
无命名空间、无未知键报错（`CapabilityBase.setup` 和 `BrowserCapability.setup` 都吞额外参数）。
两个 capability 想各自要 `timeout`/`image`/`url` 无法独立配置；打错字被所有人静默丢弃。

#### 5.2.7 WAL 唯一的二进制词汇是 PNG — **MINOR** `⚠️U`

```python
# core/trace/wal.py:135-136
is_binary = isinstance(data, bytes)
ext = ".png" if is_binary else ".json"
```
任何其他二进制产物（音频、视频、PDF、core dump、GPU trace）都会被写成 `.png` 后缀。

#### 5.2.8 `prompts/` 是 domain-keyed 目录树，无 loader 无 registrar — **MINOR** `⚠️U`

`agents/browser_use.py:28`、`skyvern.py:26`、`ui_tars.py:32` 全部硬编码
`PROMPT_PATH / "browser" / "agents" / <name>`，各用各的模板引擎（`str.format` vs Jinja2），
无共享 loader，`prompt_template: str` 而不是从目录派生的 `Literal`。

---

### 5.3 multi-benchmark 耦合

#### 5.3.1 没有 evaluator 注册表 — **BLOCKER** `✅R`

```python
# scripts/generate_all.py:61-75 —— 枚举了框架的每一层，评测不在其中
return {
    "browser_provider": BrowserProviderRegistrar,
    "action": ActionRegistrar,
    "capability": CapabilityRegistrar,
    "agent_loop": AgentLoopRegistrar,
    "llm": LLMRegistrar,
    "memory": MemoryRegistrar,
    "environment": EnvironmentRegistrar,
    "hook": HookRegistrar,
    "task_loader": TaskLoaderRegistrar,
    "eval_loader": EvalLoaderRegistrar,
    "agent": AgentRegistrar,
}
```
`ls spectragent/` → `agents benchmark_data core generated prompts schemas scripts` —— **没有 `evaluator`**。
`grep -rn "EvaluatorRegistrar|evaluator_id" spectragent/ tests/ configs/` → **零命中**。

`spectragent/core/data_loader/eval/` 是**参考数据** loader（只构造 `EvalReference` 行），**不是** evaluator。

`schemas/` 里 `EvalResult`/`EvalSummary`/`EvalDetails`/`FailureClassification`/`MetricStats`
约 160 行，**包内零生产者**。

唯一真实的评分器在 gitignore 的 `benchmarks/*/eval/src/run.py`，
它们 `from spectragent.utils import (...)` —— 而 **`spectragent.utils` 不存在**（`ModuleNotFoundError`）。

> **评测是这个框架里唯一一条没有 registry 的可变轴。**

#### 5.3.2 没有 benchmark runner，也没有 benchmark 身份 — **BLOCKER** `✅R` `✅D`

- `execute(task: str, ...)` 收**自由文本**；没有 `TaskDefinition` 参数、没有 loader 参数、没有 benchmark 参数
- `grep -rn 'AgentRuntime' spectragent/` 只有 `runtime.py` 自己 + docstring
- `spectragent/` 下唯一的 `if __name__` 是 `scripts/generate_all.py`
- `TaskLoaderRegistrar`/`EvalLoaderRegistrar` 被 demo 传给 Composer，`by_layer` 条目恒为 `{}`
- `TraceConfig` 没有 `benchmark`、没有 `subset`；`task_id` 从不转发
  （`runtime.py:146` 有它，`:173-179` 的 dispatch 不传）
- `trace_writer.py:73` 硬编码 `loop_id=""  # Loop info not directly available here`

**实测**（§3.2）：14 个 trace 的 `config.json` 全是 `task_id=''`，包括 `traces/88c1e67e`
—— 它的 `result.json` 证明该 run 确实有 `task_id='demo-debug-001'`。

**后果**：指着 `traces/` 问"`traces/0f42f6ec` 是哪个 BrowseComp task？" —— 答不出来。
`EvalLoaderRegistrar.get(benchmark)(subset=...).get(task_id)` 这个查询**构造不出来**。

#### 5.3.3 WAL step record 只有 `{step, llm_output}`，无 hook 拦截 `act` — **BLOCKER** `✅D`

```python
# core/hooks/trace_writer.py:142-147  —— 唯一的 step 内容写入方
# Write step record skeleton
step_record = {
    "step": step,
    "llm_output": _extract_completion_text(result),
}
step_rel = self._wal.write_step_record(step, step_record)
```
```python
# core/hooks/trace_writer.py:40-43 —— 只拦了两个
__intercepts__ = [
    Intercept("observe", role="env", advice="after"),
    Intercept("ainvoke", role="llm", advice="after"),
]
```
真正的 `StepRecord`（`schemas/step_record.py:18-27`，带 `actions`/`action_results`/`timestamp`）
**由 Loop 在 `react.py:260-266` 构造** —— 然后**交给了没有人**（只喂内存里的 `HistoryObs`）。

唯一的部分补偿是 `AgentResult.action_history: list[str]` —— 一个扁平化的
`f"{action.name}({action.params})"`（`react.py:254`），**结果被丢弃**，
而且它住在 `result.json` 里，**9/14 的真实 trace 没有这个文件**。

#### 5.3.4 数据路径硬编码 `browser/` + 强制 `BrowserSetup` — **BLOCKER** `✅R`

```python
# core/data_loader/_common.py:15
_BENCHMARK_DATA_ROOT = BENCHMARK_DATA_ROOT / "browser"
def resolve_data_dir(registry_key, data_dir):
    if data_dir is not None: return Path(data_dir)
    return _BENCHMARK_DATA_ROOT / registry_key
```
实测：`resolve_data_dir('swebench', None)` → `.../benchmark_data/browser/swebench`
而 `benchmark_data/__init__.py:1` 明写 `"""Benchmark data — organized by environment type (browser / desktop / mobile)."""`，
`benchmark_data/desktop/` 和 `mobile/` 存在但**永远无法被寻址**。

```python
# core/data_loader/task/_registrar.py:126-131
setup_data = dict(raw_copy.pop("setup", {}) or {})
if "start_url" in raw_copy:
    setup_data.setdefault("start_url", raw_copy.pop("start_url"))
setup_data.setdefault("setup_type", "browser")      # ← 无条件
```
```python
# schemas/task.py:44-47 —— "判别联合" 只有一个成员
TaskSetup = Annotated[BrowserSetup, Field(discriminator="setup_type")]
```
实测：把真实 SWE-bench 记录喂给 `env_type='docker'` 的 loader →
`env_type = docker` 但 `type(td.setup).__name__ == 'BrowserSetup'`，`start_url=None`。
`base_commit`、`repo`、`FAIL_TO_PASS`、`PASS_TO_PASS` 全部落进 `extra="allow"` 的无类型顶层键。
`get_task_context()`（`task/_registrar.py:86-91`）因为 `isinstance(task.setup, BrowserSetup) and task.setup.start_url`
返回 `{}` —— **容器镜像、checkout commit、决定判分的测试 node-id，全部到不了 capability。**

**静默失败**：找不到 `data_info.json` → `load_split_map` 只 `logger.warning` 返回 `{}`
→ `_load` warning "Subset 'All' not in split map" → **静默返回零个 task**。用户拿到一个长度 0 的 loader，无异常。

#### 5.3.5 timeout / exception 路径不 dispatch `on_run_end` — **BLOCKER** `✅D`

```python
runtime.py:189  await self._dispatch_lifecycle("on_run_end", result=result)   # 仅成功路径
runtime.py:203-215  timeout 分支构造了一个完好的 AgentResult ...
runtime.py:216-221  ... 然后只 dispatch on_run_error，直接 return —— on_run_end 永远到不了
runtime.py:225-230  exception 分支同上，然后 re-raise
```
`trace_writer.py:85 self._wal.write_result(result)` 是**全包唯一**的 `write_result` 调用点，
而它住在 `on_run_end` 里。`on_run_error` 只调 `finalize(RunStatus.ERROR)`。

**磁盘实测**：9/14 trace 是 `status=running` 且无 `result.json`（§3.2）。

**附加**：`partial_steps=[]` 在 `:219` 和 `:228` 都是硬编码的，
因为 `AgentRuntime` 拿不到 Loop 的 `step_records`（它是 `react.py:116` 的局部变量）
—— `hooks/base.py:47-50` 的崩溃路径 hook 契约**结构性无法履行**。

也**没有任何 recovery/resume 代码**，尽管有 "crash recovery reads manifest.json" 的声称。

#### 5.3.6 没有 env-state 评分钩子 — **BLOCKER** `⚠️U`

```python
# core/hooks/base.py:43-45
async def on_run_end(self, *, result: Any) -> None:
```
没有 `env`、没有 `capability`、没有 session handle。

而且 `AgentRuntime` **根本不驱动环境生命周期**：
`grep -rn '\.setup(\|\.teardown(' spectragent/` 只命中 `environments/_registrar.py:216,220`
—— 在 `EnvironmentBase` 自己内部，而 `spectragent/` 里没有人调它。
唯一的驱动是 demo 脚本，且在 `finally` 里 teardown（`demo_yaml_compose.py:109-116`）。

**不存在这样一个生命周期点：环境状态既(a)还活着、又(b)能被框架认识的组件够到。**
`on_run_end` 在 `execute()` 内部触发但不给 env 引用；等 `execute()` 返回给持有 `run.env` 的调用方时，
调用方的 `finally` 已经拆了。

SWE-bench 要在容器里跑 `pytest <FAIL_TO_PASS>`；OSWorld 要对活 VM 跑谓词。**两者都表达不了。**

#### 5.3.7 唯一的判分信号是一个字符串 + agent 自述 — **MAJOR** `✅V`

```python
# react.py:271-283 —— 唯一的成功路径
answer=last_done_result.extracted_content or "",
agent_success=last_done_result.success,
```
`last_done_result.success` 来自 `DoneAction.execute`（`browser/actions/done.py:44-48`）的
`success=bool(params.get("success", True))` —— **直接读 LLM 自己的 action 参数**。
而 `skyvern.py:190` 和 `ui_tars.py:258` 在 done 参数里**硬编码 `"success": True`**。

max_steps 路径（`react.py:286-295`）不传 `answer` → 默认 `""`，
且 schema **主动禁止**在那里记录判决（`agent_result.py:112-116`）：
```python
if self.agent_success is not None and self.agent_done != AgentDoneStatus.DONE:
    raise ValueError(...)
```

**破坏**：一个 SWE agent 写出了正确的 patch 但耗尽 max_steps →
`answer=""`、`agent_success=None`，**它写的 patch 根本不在 result 里**。

> **验证者的修正**：`extra="forbid"` **不**阻止增量扩展 —— `agent_metadata: dict[str, Any]`
> （`agent_result.py:100`）是显式的扩展槽。**但它的注释写着 `# eval scripts must not read this`**
> —— 唯一的逃生口在契约上对 grader 是禁区。

#### 5.3.8 `RunTrace.screenshots` 恒返回 `[]` — **MAJOR** `✅V`

```python
# core/trace/reader.py:121-127
return sorted(obs_dir.glob("*_screenshot.png"))
# core/trace/wal.py:135-136
ext = ".png" if isinstance(data, bytes) else ".json"
```
`TraceWriter` **永远**递交 Pydantic `ScreenshotObs`，从不是 bytes → 永远选 `.json`。
`ScreenshotObs` 把 PNG base64 塞进那个 JSON（`fragments.py:105-111`）。

实测：`RunTrace(Path('traces/88c1e67e')).screenshots` → `[]`，而目录里有 11 个 `NNN_screenshot.json`。
全 14 个 trace 共 **131 个 `*_screenshot.json`，0 个 `.png`**。

**破坏**：Online-Mind2Web 用 WebJudge（图像评委），prompt 已经在
`spectragent/prompts/browser/evaluators/Online-Mind2Web/judge_image_{system,user}.txt`。
grader 调 `trace.screenshots` 拿到 `[]`，**静默降级**到 `webjudge_user_no_images.txt`
—— 产出一个不同的、更弱的判决，**无报错无警告**。

顺带：每张截图付 ~33% base64 膨胀，塞在 reader 的 `.png` 分支永远够不到的 JSON 里。

#### 5.3.9 evaluator prompt 有两份完全相同的树，都是死的 — **MAJOR** `✅V`

- `spectragent/prompts/browser/evaluators/` —— git 跟踪，18 个 `.txt`，**零 Python 消费者**
- `benchmarks/prompts/` —— gitignore（`.gitignore:41`），是旧 grader 真正读的
- `diff -rq` 两者**字节相同**
- 旧 grader 本身也是坏的（`import spectragent.utils` → `ModuleNotFoundError`）

**而且目录名和 registry key 对不上**：
目录是 `BrowseComp` / `LexBench-Browser` / `Online-Mind2Web`；
registry key 是 `browsecomp` / `lexbench_browser` / `online_mind2web`。
→ 未来的 Evaluator 需要一张手工别名表，**正是 registrar 设计要消灭的那种硬编码查表**。

外加：`pip install spectragent` 装到的是**死的那棵树**，不是活的那棵。

#### 5.3.10 一个 ComposedRun 只能跑一个 task；memory 从不 reset — **MAJOR** `⚠️U`

`clear()` 在 Protocol 上（`memory/base.py:49`），四个策略都实现了，**零调用点**。
`AgentRuntime.execute` 每次调用都重新播种 system prompt（`:170`）却不清空
→ 用同一个 `ComposedRun` 跑 N 个 benchmark task，
**task 1 的完整轨迹 + 第二份 system prompt 会进 task 2 的 context**。

#### 5.3.11 `EvalLoader` 没有 normalization hook — **MINOR** `⚠️U`

```python
# eval/_registrar.py:86-99
ref = EvalReference(task_id=raw["task_id"], ...)     # 硬索引
```
task 侧刻意提供了相反的东西（`task/_registrar.py:110-121` 的 `_normalize_raw`，
docstring 写着 "Subclasses targeting non-browser benchmarks should override"）。

**隐患已在 shipped 数据里**：`benchmark_data/browser/lexbench_browser/tasks_private.jsonl`
的键是 `id` 而不是 `task_id` → `KeyError`。今天安全只是因为它不在 `data_info.json` 的 split map 里。

---

### 5.4 multi-agent 耦合

见 §4.3 的实测。核心条目：

| # | 条目 | 位置 | 级别 |
|---|---|---|---|
| 1 | `ComposedRun.agent` 是 `single("agent")`，2 个 agent 连 `AgentRuntime` 都构造不出来 | `composer.py:67-71, 81-82` | BLOCKER `✅V` |
| 2 | 实例身份 = registry key，同类两实例静默塌缩 | `composer.py:211-213, 276, 296` | BLOCKER `✅V` |
| 3 | `weave` 把 ctx 存在 loop 实例上，第二次 weave 覆盖第一次 | `loops/_registrar.py:118, 124` | BLOCKER `✅V` |
| 4 | agent→agent wiring 不可表示（标量 Literal → `UnresolvedWiringError`） | `composer.py:327` | BLOCKER `⚠️U` |
| 5 | `TraceWriter` 单 observation 槽 + 单全局 step 计数 → 归属错乱 | `trace_writer.py:50-53, 109, 118, 158` | BLOCKER `⚠️U` |
| 6 | `MetricsCollector` 全局累加并覆写每个 result | `metrics_collector.py:86` | MAJOR `❌X→MINOR` |
| 7 | AOP handler 拿不到 target 引用 → 无法归因到 agent/model | `aop/proxy.py:44, 99-104` | MAJOR `✅V` |
| 8 | `Intercept("run", role="agent")` 永不触发；未知 role 静默 no-op | `aop/proxy.py:43-47`、`loops/_registrar.py:124` | MINOR `✅V`（原报告说 role="agent" 整体死，**已修正**：只有 `run` 死） |

**#7 的细节**：
```python
# aop/proxy.py:44 —— 快照的是 *bound method*，所以 self 不在 args 里
original = getattr(target, method_name, None)
# aop/proxy.py:102-110 —— handler 只拿到三样东西
await handler(result, args=args, kwargs=kwargs)
```
`weaver.py:69-91` 知道 `role`，但在 `_build_wrapped_method` 之前就丢掉了。
后果：`metrics_collector.py:152-161` 的 `_extract_model_name` 只读 `kwargs["model"]`，
而唯一的生产调用点 `react.py:162` 不传 → **`AgentUsage.by_model` 永久为 `None`**
（5 个磁盘上的 `result.json` 全部确认 `by_model: null`），
`metrics_collector.py:133-149` 那 17 行**不可达**，`AgentResult.model_id` 每个 trace 都是 `""`，
`TraceConfig.llm_model` **全仓库只有一次出现：它自己的声明**。

**跨模型比较从产物层面是不可能的。**

---

### 5.5 七原则违反

| 原则 | 判决 | 关键证据 |
|---|---|---|
| **Protocol-First** | ❌ 从未被强制 | conscribe metaclass 用 `protocol_check=False` 注册（`.venv/.../conscribe/registration/auto.py:113`）；真正的门是 `callable(getattr(agent, slot))`（`loops/_registrar.py:93-96`）。实测 `class Bogus(AgentBase)` 干净注册，`isinstance(Bogus(), BaseAgent)` 是 `False`。→ `execution_dag.md` M03 声称 "AgentRuntime 在运行前通过 isinstance 检查实际兼容性" |
| **Config-Driven** | ❌ 端到端不成立 | Composer 实例化了没人读的 YAML config（agent 上多出一个 `ClickAction`），又丢掉了所有人都需要的 YAML config（`env.capabilities`、`provider:`、`action_overrides`）。见 §5.1 |
| **Strategy-Pluggable** | ⚠️ 部分 | Watchdog 不是 registrar（§5.5.4）；loop fragment builder 不是 registrar（§5.5.2）；evaluator 没有 registrar（§5.3.1） |
| **Zero Global State** | ✅ 基本兑现 | 没发现模块级可变单例 |
| **Composition over Inheritance** | ✅ 基本兑现 | — |
| **Record Once, Evaluate Many** | ❌ 两个半边都不成立 | record 半边不记 action/loop-fragment/身份（§5.3.3、§5.5.5）；evaluate 半边不存在（§5.3.1） |
| **AOP Cross-cutting** | ⚠️ 机制对，绑定死 | role 是 3 个字面量；`agent.run` 不可拦截；handler 无 target（§5.4 #7、#8） |

#### 5.5.1 "三方协商"实际是两方 — `✅V`

```
grep -rn '__wiring__' spectragent/core/loops/     → 零命中
parse_wiring(ReactLoop)                           → []
extract_wiring(ReactLoop, "observation")          → (set(), set())
```
→ `runtime.py:89` 的注释 `# Step 2: Observation three-party negotiation` 里，**第三方恒为空**。
**每一个被激活的 loop-source fragment，都是因为某个 *agent* 要它。**

#### 5.5.2 loop-source fragment 完全绕过协商 — **MAJOR** `✅V`

```python
# core/negotiation/observation.py:70-82
for frag_key in all_requested:
    source = getattr(frag_cls, "__source__", "env")
    if source == "loop":
        loop_sourced.add(frag_key)       # ← 无 builder 检查，required 和 optional 一视同仁
    elif frag_key in env_provides:
        env_sourced.add(frag_key)
    elif frag_key in all_required:
        raise IncompatibleFragmentError(...)
```
builder 表是 `react.py:321-330` 的**硬编码 4 项 dict**。

**实测**：注册 `loop.plan`（`__source__='loop'`），agent 把它列为**仅 optional** →
协商干净通过 → `populate_loop_fragments` 在 **step 0** 抛
`KeyError: "No builder registered for loop-source fragment 'loop.plan'"`。
**仅仅把一个 fragment 标为 optional 就会硬崩，而不是优雅降级。**

而且任何第二个 Loop（CodeAct/PlanExec）**拿不到 `loop.task`**，除非重新实现 `react.py:321-330`
—— 而 `loop.task` 是任务文本抵达每个 agent 的唯一途径（`browser_use.py:132-133`）。

#### 5.5.3 AOP 织入是硬编码的 3 对象列表 — **MAJOR** `✅V`

```python
# runtime.py:101-103
wrapped_agent = weaver.wrap(agent, role="agent")
wrapped_env   = weaver.wrap(env,   role="env")
wrapped_llm   = weaver.wrap(llm,   role="llm")
```
`Intercept.role` 是**无约束的 `str`**（`advice.py:46`），`Weaver._build_index` 无条件索引（`weaver.py:66`）。
→ `role="capability"` / `role="memory"` / 打错字**注册干净、出现在 `intercepted_methods`、永不触发**，
**零日志、零异常**（连 DEBUG 都没有）。

`AOPProxy.__init__`（`proxy.py:42-47`）对**目标方法在 wrap 时不存在**的 intercept **静默丢弃**，
没有 `else`、没有日志 —— 与缺 *handler* 的路径（在类创建时就 raise）**方向相反**。

`loops/_registrar.py:124` 的 `agent.run = run` 落在 `AOPProxy` 自己的 `__dict__` 上
（proxy 没有 `__setattr__`）→ 永久遮蔽 `_wrapped_methods["run"]`。
同样的遮蔽也发生在 `runtime.py:133-134` 的 `__active_descriptors__` / `__memory__`。

**顺带打破一条 docstring**：`runtime.py:76-78` 说 "After return, `run.agent.run(task)` is fully self-contained"
—— `run.agent` 是**未包装的** agent，一旦存在 proxy 它就没有 `.run`。

#### 5.5.4 Watchdog/Resolver 轴不是 registrar — **MAJOR** `✅V`

加一个 `browser.network` fragment 要改**至少 9 处**：

| 位置 | 内容 |
|---|---|
| `capability.py:78-79` | `_TAB_WATCHDOGS` / `_BROWSER_WATCHDOGS` 字符串集合 |
| `capability.py:99-102` | `_ObserveCycleCache` 写死字段的 dataclass |
| `capability.py:116-119` | `__wiring__["observation"]` |
| `capability.py:553-561` | `resolve_fragment` 的 if/elif 链 |
| `capability.py:626-630` | `BrowserStateRequestEvent` 的 include 标志 |
| `capability.py:654-667` | 结果收割循环 |
| `capability.py:981-1006` | `_WATCHDOG_FACTORY`（函数内 factory） |
| `watchdogs/events.py:19-26` | 事件 schema |
| `profiles.py` 每个 `BUILTIN_PROFILES` 条目 | watchdog 名字列表 |

→ `capability.py:10-11` 和 CLAUDE.md §2.4 声称 "adding a new fragment = adding one class, the capability does not change"。

**更尖锐的事实** `✅R`：`agent_cdp` **本身就带一个 conscribe `WatchdogRegistrar`**
（`from agent_cdp import WatchdogRegistrar` 导入成功），
而 `grep -rn "WatchdogRegistrar" spectragent/ tests/` **零命中**。
**注册表是现成的，被三张手工字符串表绕过了。**

而且 `capability.py:1015` 对未知 watchdog 只 `logger.warning("Unknown watchdog type: %s", wd_type)` 然后继续
→ fragment 解析成 `None` → 因为 env-source fragment 在 `dynamic_model.py:71` 是必填 →
pydantic `ValidationError`，错误信息指向 fragment 而不是 watchdog。

**顺带一个性能事实**：`_populate_cycle` 用 `self.provided_fragments`（capability **全集**）算 include 标志，
**不是**协商出的 active 集 → UI-TARS 只要 screenshot，**每步照样全量抽 DOM**。协商省成本的意义被抵消。

#### 5.5.5 Record Once, Evaluate Many 不成立 — **MAJOR** `✅D`

`TraceWriter` 记录的**既不是 agent 实际看到的 observation，也不是它做的任何 action**：

```python
# trace_writer.py:105-109
async def after_observe(self, result, *, args, kwargs, **ctx):
    self._pending_observation = result       # ← env.observe 的返回值，loop 字段还是 None
```
```python
# _loop_fragments.py:51
return obs.model_copy(update=update)          # ← Loop 拿到的是 *另一个对象*
```
```python
# trace_writer.py:126-128
for field_name in obs.model_fields:
    value = getattr(obs, field_name, None)
    if value is not None:                     # ← 跳过仍为 None 的 loop 字段
```
→ 全 14 个 trace 每步只写 `dom` / `screenshot` / `browser_meta` / `timestamp`，
**从来没有 `task` / `step_info` / `history`**。

> **验证者补充的缓解**：逐字的 LLM context **确实**被记在 `messages/NNN_input.json` 里，
> 所以信息没丢，只是**通过 fragment API 够不到**。

#### 5.5.6 Config-Driven 双向失效 — **MAJOR** `✅V`

不只是丢配置，还会**凭空注入没人读的配置**：

实测：`run.agent.__dict__["action"] == ClickAction(post_delay_ms=0)`，capability 上也有一个。
`composer.py:231` 注入是因为 `"action"` 恰好是一个 composer layer 且 `action` 是个 `WiredField`；
而 `"observation"` 被跳过只是因为**调用方的 registrars dict 里没有 observation 这一层**。
→ **同一份声明，行为取决于一个不相关的调用方字典**。
那个 YAML 必填字段是惰性的 —— 分发只用 `_actions_by_name`。

#### 5.5.7 同名 config 字段在两层，一个生效一个惰性 — **MINOR** `✅V`

`browser_use.py:108,110`、`skyvern.py:70`、`ui_tars.py:62` 的
`max_steps` / `timeout_seconds` —— **零读者**。
`configs/browser_use_local.yaml:14` 设 `max_steps: 30`，实际生效的是 `ReactLoop.max_steps = 100`，
而 `react.py:325` 告诉模型的也是 **100**。Pydantic 两个都接受。

---

### 5.6 文档 vs 代码漂移表

| 文档声称 | 位置 | 代码现实 | 标记 |
|---|---|---|---|
| `EvaluatorRegistrar` in `evaluator/` 🚧 partial | CLAUDE.md §2.2 | 包不存在，零命中 | `✅R` |
| "adding a new fragment = adding one class" | CLAUDE.md §2.4、`capability.py:10` | 要改 9 处（§5.5.4） | `✅V` |
| `WatchdogRegistrar` 规格 | `architecture_design.md:752-771` | 只命中两份文档，零代码；而 agent_cdp 已经有一个 | `✅R` |
| "generator entry points are the `generate_config.py` files scattered under each registrar directory" | CLAUDE.md §3.4 | 只有 `scripts/generate_all.py`；`core/loops/` 没有 `generate_config.py` | `✅V` |
| "Step 2: Observation **three-party** negotiation" | `runtime.py:89` | 第三方（Loop）恒为 `(∅,∅)` | `✅V` |
| "The loop stays per-instance — multi-loop uses `copy.deepcopy`" | `loops/_registrar.py:10-11` | `grep deepcopy spectragent/` → 零命中 | `✅V` |
| "After return, `run.agent.run(task)` is fully self-contained" | `runtime.py:76-78` | system prompt 和 `_action_descriptors` 只在 `execute()` 里播种；且 `run.agent` 是未包装对象 | `✅V` |
| "The returned dict is forwarded by `AgentRuntime` to `Capability.setup()`" | `data_loader/task/base.py:42-44`、`capability.py:375` | `AgentRuntime` 不 import data_loader，也不调 `env.setup()` | `✅R` |
| "Action system — symmetric to Observation" | CLAUDE.md §2.5 | Action 端保留全限定名，Observation 端剥前缀 | `✅V` |
| "illegal combinations fail in Pydantic validation, never at runtime" | CLAUDE.md §2.3 | 三个反例：fragment 名冲突、loop fragment 无 builder、`resolve_fragment` 未知 key —— 全在 step 0 抛 | `✅V` |
| "AgentRuntime 在运行前通过 isinstance 检查实际兼容性" | `execution_dag.md` M03 | `protocol_check=False`；真正的门是 `callable()` 鸭子检查 | `✅V` |
| "no `# deprecated` re-exports … no parallel old/new class pairs" | CLAUDE.md §0.4 | `schemas/_types.py:138-142`（4 个别名，1 个再导出到 `schemas/__init__.py:5,46`）；`core/llm/__init__.py:68-71`（`ChatAnthropicBedrock`/`ChatAWSBedrock`/`ChatOCIRaw`） | `✅R` |
| "crash recovery reads `manifest.json`" | CLAUDE.md §2.7 | 无任何 recovery/resume 代码 | `⚠️U` |
| `configs/README.md` 的 quick start | `configs/README.md:14-22` | `demo_yaml_compose.py` + 两份 YAML 都跑不起来（§5.1.1） | `✅R` |
| YAML 注释 "Composer collects every entry from the top-level `capability:` list ... and injects them as `env.capabilities`" | `configs/browser_use_local.yaml:58-63` | 完全相反 | `✅R` |
| `architecture_design.md §14.2` "Hierarchical / Multi-agent 编排 ... 具体设计待定" | — | **文档诚实**。multi-agent 是**缺席**，不是设计错 | — |

---

## 6. Bug 清单（与耦合无关的正确性问题）

### 6.1 Loop / Agent

#### 6.1.1 `output_model` 算了就扔 — **BLOCKER** `✅V` ★ *五个视角独立报告*
```python
# react.py:157-162
output_model = (agent.get_react_output_model()
                if hasattr(agent, "get_react_output_model") else None)
completion = await llm.ainvoke(context, output_format=None)     # ← 写死 None
```
`grep -n 'output_model' react.py` → 50, 59, 152, 157, 158 —— **赋值后从未被引用**。
紧邻上方的 7 行注释（`:150-156`）断言的是**相反**的事。

**杀死的东西**：11 个 provider 里约 600 行结构化输出代码、`SchemaOptimizer`、
Anthropic 原生 `output_config` 分支、Bedrock `toolConfig`、4 个 openai soft-mode 标志、
`BrowserUseAgent._build_per_run_output_model` —— 全部不可达。

**连带**：per-run 的动作集窄化在文本路径上也是惰性的 ——
实测 `parse_react_output(json.dumps({"memory":"m","action":[{"totally_hallucinated_verb":{}}]}))`
返回 `[Action(name='browser.totally_hallucinated_verb')]`，无 Pydantic 拒绝
（因为 `browser_use.py:246` 校验的是宽松的基类 `BrowserUseOutput`，不是 `self._per_run_output_model`）。
幻觉动词只在一步和一整个 prompt 的 token 之后才被 `guarded_act` 抓到。

#### 6.1.2 修了 6.1.1 会立刻全崩 — **BLOCKER** `✅V` ★
```python
# browser_use.py:230-231
if isinstance(llm_output, BrowserUseOutput):
    return _items_to_actions(llm_output.action)
# browser_use.py:522, 537-538
class _ActionItem(RootModel[dict[str, dict[str, Any]]]): ...
class _PerRunOutput(BrowserUseOutput): action: list[_ActionItem]
# browser_use.py:568-573
if not isinstance(raw, dict) or len(raw) != 1:
    raise ParseError(f"Each action must be a single-key dict ...")
```
`_PerRunOutput` **是** `BrowserUseOutput` 子类 → `isinstance` 快路径触发 →
但字段类型从 `list[dict[str, Any]]` 窄化成了 `list[_ActionItem]` → `_items_to_actions` 鸭子检查 `isinstance(raw, dict)` 失败。

实测：`ParseError: Each action must be a single-key dict like {'navigate': {...}};
got _ActionItem(root={'browser.click': {'index': 1}})`

**12 个围绕 `_build_per_run_output_model` 的单测全部只校验*模型*，从不走 `parse_react_output` 的往返** → CI 全绿。

#### 6.1.3 `total_usage` 建了空的从不累加 — **MAJOR** `✅V`
```python
react.py:118  total_usage = AgentUsage()
react.py:223/278/292  usage=total_usage,      # 逐字传递
```
`grep -n 'total_usage' react.py` → 恰好这 4 行，**无 `+=`、无重新赋值**。
`completion.usage` 在 `react.py:162-164` 在作用域内，被丢弃。

**恶化点**：`AgentMetrics.usage` 是 `AgentUsage | None`，
Loop 塞进去一个**全零的对象**而不是 `None` → 覆盖掉 schema 自己的"未测量"哨兵
→ **未测量的 run 和真的零 token 的 run 无法区分**，下游 `if usage is not None` 守卫会通过并读到零。

唯一的真实写入方是 opt-in 的 `MetricsCollector.on_run_end`。
实测：`configs/browser_use_local.yaml` 组装出来 `run.hooks == []`
（两份 config 都没有 `hook:` 键，`generated/_composed.py:886` 默认 `[]`）
→ **文档路径上 `result.json` 记录 `total_tokens: 0`**。

三个残留即使开了 hook 也在：`total_cost` 结构上恒为 `0.0`（全仓库无定价表）、
`by_model` 恒 `null`（§5.4 #7）、`StepRecord.token_usage` **零写入方**。

#### 6.1.4 `on_run_end` 的 hook 顺序是承重的且无强制 — **MAJOR** `✅V`
`_dispatch_lifecycle`（`runtime.py:240-248`）按 `run.hooks`（即 YAML）顺序 await 阻塞 hook；
`MetricsCollector` 原地改 `result.metrics`（`:85-88`），`TraceWriter` 立刻序列化（`:85`）。
`HookBase` 上**没有 `priority` 字段**。
→ `hook: [trace_writer, metrics]` 会让**每个 `result.json` 的 token 都是 0**，静默。
`demo_debug.py:344` 只是碰巧顺序对了。

#### 6.1.5 prompt 和执行的 max_actions 默认不一致 — **MAJOR** `⚠️U`
`browser_use.py:299` 把 `max_actions=3` 渲染进 system prompt；
`react.py:237-238` 按 `ReactLoop.max_actions_per_step`（**默认 1**）截断，
**无日志、无反馈消息、`StepRecord` 里无标记**。
模型被告知可以发 3 个，第 2、3 个被静默丢弃，于是它重发已经"执行过"的步骤。

#### 6.1.6 `EnvironmentStatus.FAILED` 从 Loop 不可达 — **MINOR** `⚠️U`
`react.py:225,280,294` 三个 `_build_result` 调用点全部硬编码 `env_status="success"`，
`runtime.py:207` 在 timeout 时也传 `SUCCESS`。**崩掉的浏览器和健康的浏览器在 `AgentResult` 里无法区分。**

而且 Loop **完全没有针对 env/LLM/action 失败的异常处理** ——
`react.py:171-173` 是 `_execute` 里唯一的 `try:`，
所以一次瞬时 CDP 断连或一个 429 会**带走整个 run**，而不是产出 `env_status=FAILED`。

### 6.2 Agent 实现

#### 6.2.1 `SkyvernAgent` 四处独立坏死 — **BLOCKER**（对该 agent）`⚠️U`
- **(a)** `_action_history` 在 `prepare()`（`skyvern.py:144`）里被清空，紧接着在 `:242` 被读
  → history window 永远渲染成 `(none)`，`action_history_window` 配置项无效
- **(b)** `_build_system_prompt` 传了 `action_space` 等 4 个模板从不引用的变量，
  模板引用了 8 个 agent 从不提供的变量 → Jinja 渲染成空
  → **协商出的动作集根本没进 prompt**
- **(c)** `SkyvernOutput` 期望 `element_id`/`is_done`，而 shipped 的 `extract-action.md` 教模型输出 `"id"` 和**大写枚举**
  → 实测模板形状的 JSON 产出 `Action(name='browser.CLICK')` → `guarded_act` **100% 拒绝**；
  `COMPLETE` → `browser.COMPLETE` 意味着 **agent 永远无法终止**
- **(d)** `skyvern.py:277` `dom_str = str(element_tree)` 把裸 `DOMState` dataclass repr 灌进 prompt，而不是 `serialized_dom`

#### 6.2.2 `UITarsAgent` 把 `browser.type` 声明为必需且必然失败 — **MAJOR** `⚠️U`
`ui_tars.py:55, 272-273` 发 `Action(name='browser.type', params={'content': ...})` **无 `index`**；
`TypeAction.execute` 要 `params["index"]` → 返回 `ActionResult(success=False, error="Missing param: 'index'")`。
**没有 `browser.type_coordinate` 与 `browser.click_coordinate` 对应。**

### 6.3 Memory

#### 6.3.1 `SimpleMemory` 静默驱逐 system prompt — **MAJOR** `⚠️U`
`memory/simple.py:35-36` 没有 `keep_system` 守卫（`SlidingWindowMemory` 有）。
实测：`SimpleMemory(max_messages=3)` 喂 `[System, User, Assistant, User]` → 返回 `['user','assistant','user']`。
→ 跑到一半 agent 丢掉动作列表，开始输出不可解析内容，**`ParseError` 级联，且没有任何诊断指向 memory**。
`tests/test_agents/test_browser_agents_runtime_compat.py:224` 已经在构造 `SimpleMemory(max_messages=10)`。

#### 6.3.2 `SummaryMemory` 从不 summarise — **MAJOR** `⚠️U`
`summary.py:45,70` 的 `set_llm()` 和 `summarise()` **零调用点**；`_llm` 恒 `None`；
`[Conversation Summary]` 分支不可达；`max_tokens` 从不读。
选 `memory_id: summary` 得到的是一个**无上限 buffer**，只是把 SystemMessage 挪到前面。

#### 6.3.3 `TokenBudgetMemory` 的估算把图片当 1 token — **MAJOR** `⚠️U`
`token_estimator` 从不读；`_estimate_tokens` 读 `message.text`，**丢掉 image part**。
实测：一个 400,030 字符的 base64 PNG 估算为 **1 token**。

### 6.4 LLM 层

#### 6.4.1 `ChatOCI` 原地修改调用方的 SystemMessage — **MAJOR** `⚠️U`
`oci.py:189-192` `modified = list(messages)` 是**浅拷贝**，而 `SimpleMemory.get_messages` 也返回浅拷贝
→ system prompt **每步增长一整个 JSON-schema 块，无上限**，
且这个变异**持久化进 WAL 和之后所有 provider 调用**。

#### 6.4.2 Google serializer 在 `include_system_in_user=True` 时丢掉整条第一个 user message — **MAJOR** `⚠️U`
`google_format.py:81-93`。实测：`[System('SYS'), User([Text('USER TEXT'), Image(png)])]`
序列化成 `[Content(parts=[Part(text='SYS')], role='user')]`。
**开 vision 时 Gemini 只收到 system prompt。无异常、无日志。**

#### 6.4.3 Anthropic `seed` 每次调用必抛 `TypeError` — **MAJOR** `⚠️U`
`anthropic_base.py:104-105`。`anthropic 0.86.0` 的 `AsyncMessages.create` 没有 `seed` 也没有 `**kwargs`。
`:249` 的裸 `except Exception` 把它转成不透明的 `ModelProviderError`。
**这个字段在生成的 config 里 —— 正好是做可复现实验的人会设的那个。**

同类：`ChatOpenRouter` 把 `extra_body` splat 成顶层 kwargs（`openai_openrouter.py:58-59`）；
`ChatAzureOpenAI.use_responses_api` 类型是 `str` 但用 `isinstance(..., bool)` 测（`openai_azure.py:45,80-84`）
→ `"true"`/`"false"` 是死的。

#### 6.4.4 `ChatDeepSeek` / `ChatOllama` 报告 `usage=None` — **MINOR** `⚠️U`
`openai_deepseek.py:102`、`ollama.py:63,75` —— 两个 SDK 响应上计数器都是有的。
**Ollama 恰恰是离线可复现 benchmark 的首选，而那里 token 记账最重要。**

#### 6.4.5 结构化输出 15 份手搓，声明式表零引用 — **MAJOR** `✅V`
```
grep -rn "model_families|detect_model_family" spectragent/core/llm/providers/ | wc -l   → 0
```
`core/llm/model_families/` 有 `supports_json_schema`、`prefers_tool_for_structured`、
`supports_strict_mode`、`schema_requires_gemini_fix` —— **零 provider 引用**，
而 `_base.py` 的 docstring 谎称 "Providers query ModelFamily to..."。
provider 改为内联再嗅探模型名：`google.py:138-139`、`openai_azure.py:83-84`、
`openai_vercel.py:48-49`、`openai_base.py:168-169`。

`SchemaOptimizer._make_strict_compatible`（`optimizer.py:208-220`）**无条件**
把 `required` 改写成全部属性并强制 `additionalProperties: false`，
把 browser-use 的输出形状**编码成了全部 8 个调用方的全局策略**，**无 opt-out、零测试**。

实测破坏：`tool_calls: list[dict[str, Any]]` 编译成
`{"items": {"type":"object","additionalProperties": false}}`
→ strict 模式下模型只能发 `[{}]`，**每个 tool call 的参数全部静默丢失**。
`bedrock.py:100-113` 另外还独立地把嵌套子模型压平成 `{"type":"string"}` 并剥掉数组的 `items`。

> **注意时序**：这一切今天都不可达，因为 `react.py:162` 写死了 `output_format=None`。
> **先修 6.1.1/6.1.2，否则这块的整合无法测试。**

#### 6.4.6 `ChatBrowserUse` 是坐在通用 `core/llm` 里的 domain 专用 provider — **MINOR** `✅V`
`browser_use.py:66-80` 的 `ainvoke` 加宽了 `BaseChatModel` Protocol，带一个 domain 命名的参数
`request_type: str = "browser_agent"`，`:86` 恒发到线上且**无法覆盖**
（生成的 `BrowserUseDefaultLLMConfig` 里没有这个字段）。
`:121-129` 的结构化输出解析器**硬编码 `"action"` 字段名**。

#### 6.4.7 `AnthropicMessageSerializer` 把多个 SystemMessage 塌成最后一个 — **MINOR** `⚠️U`
`anthropic_format.py:336`。同一份 message list 在 Anthropic 和 OpenAI 上产生**实质不同的 prompt**。
今天潜伏是因为 `AgentRuntime` 只播一个、`ReactLoop` 用 `UserMessage` 做 parse-error 反馈 ——
**但三个过滤型 memory 策略主动把 SystemMessage 提到前面，正在鼓励那个会坏的形状。**

### 6.5 Trace / WAL

#### 6.5.1 `_atomic_write` 掩盖真异常 + 泄漏临时文件 + 不持久 — **MAJOR** `⚠️U`
`core/trace/wal.py:47-51`。如果失败发生在 `os.rename`，fd 已经关闭，
`os.get_inheritable(fd)` **自己先抛**。
实测（patch `os.rename` 抛 `OSError('DISK FULL')`）：调用方看到的是
`OSError [Errno 9] Bad file descriptor`，且 `.tmp` 文件残留。
rename 后**没有父目录 fsync** → WAL commit 是原子的但**不是持久的**。

#### 6.5.2 `TraceWriter` 在 Pydantic **实例**上迭代 `model_fields` — **MINOR** `⚠️U`
`trace_writer.py:126-127`。pydantic 2.11 起废弃、3.0 移除；在 2.12.5 上**每步一条 DeprecationWarning**。

#### 6.5.3 `RunTrace.screenshots` 恒 `[]` — 见 §5.3.8

#### 6.5.4 `NegotiatedObservation.timestamp` 从不赋值 — **MINOR** `✅D`
`dynamic_model.py:44-46` 恒为 `0.0`，且 `TraceWriter` 每步写一个内容为 `0.0` 的垃圾
`NNN_timestamp.json`（14 个 trace 全部确认）。

### 6.6 Browser Capability

#### 6.6.1 `browser.switch_tab` 报成功但什么都不做 — **MAJOR** `⚠️U`
`browser/actions/switch_tab.py:36-41` 发 `SwitchTabEvent`；
`browser/watchdogs/action_watchdog.py:303-313` **没有任何 watchdog 连接它**。
`agent_cdp` 的 event 是 born-completed → `await event` 立刻返回。
**LLM 被告知有这个动作、调用它、拿到 `success=True`、页面纹丝不动。**
`CloseTabEvent` 既不发也不处理；`create_tab`/`close_tab` **零生产调用者**。

#### 6.6.2 7 个云 browser provider 里 6 个在 `setup()` 崩 — **MAJOR** `⚠️U`
`browser/capability.py:401-406` 给 `backend.open()` 传 `viewport=`；
只有 `LocalChromeProvider.open(self, **kwargs)` 能吞（而且忽略它）。
实测：`SkyvernProvider().open(viewport={...})` → `TypeError`。
**`configs/browser_use_skyvern.yaml` 是仓库里 shipped 的配置，跑不了。**

另外 `viewport_width`/`viewport_height` **从来到不了浏览器** ——
全仓库零个 `setDeviceMetricsOverride`。
Skyvern/Airtop 算出来的 `metadata["cdp_headers"]` **没人读**，
所以它们的 CDP 握手会因缺少 auth 被拒。

#### 6.6.3 partial-failure 重试并不重试 — **MINOR** `⚠️U`
`capability.py:669-678` 的注释声称"will retry instead of serving empty fragments forever"，
代码只是 `return` 而不设置 `cache.dom`，下一个调用方拿到 pydantic `ValidationError`
（因为 env fragment 在 `dynamic_model.py:71` 是必填）。

#### 6.6.4 `HumanMouse` 静默降级 — **MINOR** `⚠️U`
`browser/input/mouse.py:90-100` 捕获裸 `Exception`，
而因为 `MouseTrajectoryRegistrar` 不在 `generate_all.py:40-75` 里，
该字段是裸 `str` 无 `Literal` 校验 → **打错字静默关掉 humanized 鼠标运动**。

#### 6.6.5 JS click-listener 检测永久关闭；`DownloadsWatchdog` 惰性 — **MINOR** `⚠️U`
`dom_watchdog.py:44,52,172`：`enable_js_listener_detection` 默认 `False`，
唯一的构造点（`capability.py:982-987`）从不传它
→ `has_js_click_listener` 恒 `False`，**141 行的 `dom/js/event_listeners.py` 是死代码**。
`DownloadsWatchdog` 同理 —— `Browser.setDownloadBehavior` 全仓库零发送，其 CDP 事件永不触发。

#### 6.6.6 per-watchdog 的 `CDPEventBridge` 从不关闭 — **MINOR** `⚠️U`
`popups_watchdog.py:43`、`crash_watchdog.py:41`、`downloads_watchdog.py:51` 创建的 bridge，
`teardown`/`close_tab`（`capability.py:459-464, 781-786`）**碰都不碰**
（那两处只关 `self._bridges`，即 page-lifecycle 的）。
每次 `close_tab` 都留下一个绑在已关闭 scope 上的回调；
`scope.emit` 在关闭的 scope 上抛 `RuntimeError`，被 `cdp_client.py:222-228` 用 `logger.exception` 吞掉。

### 6.7 其他

#### 6.7.1 `IncompatibleFragmentError` 只报告一个任意的缺失 fragment — **MINOR** `✅V`
`negotiation/observation.py:70,79-82` 迭代一个普通 `set`，在第一个 miss 时抛 `required={frag_key}`。
缺 3 个的话用户要修 3 次重跑 3 次，**而且每次报的那个在不同进程间会变**。
`action.py:54-60` 正确地算了完整的缺失集 —— **不对称**。

#### 6.7.2 多余的 `ClickAction` 挂在 agent 和 capability 上 — 见 §5.5.6

#### 6.7.3 构建出的 wheel 里没有任何数据文件 — **BLOCKER** `✅R` ★

`pyproject.toml:71-73` 只有：
```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["spectragent*"]
```
**没有 `package-data`、没有 `include-package-data`、没有 `MANIFEST.in`**（文件不存在）。

**实测构建**：
```
$ uv build --wheel --out-dir /tmp/afwheel
Successfully built spectragent-0.3.0-py3-none-any.whl

wheel 总条目          : 228
非 .py / 非 dist-info : 5      ← 全部是 .pyi 存根
  .md   : 0
  .txt  : 0
  .jsonl: 0
  .json : 0
```

**丢失的是 131 个文件**（`spectragent/prompts/` + `spectragent/benchmark_data/` 下的全部非 `.py`）：
- **运行时真正读的 agent prompt** —— `browser_use.py:295` `template = prompt_file.read_text()`
- 全部 benchmark 数据（`l1.jsonl`、`tasks.jsonl`、`data_info.json`、`Online_Mind2Web*.json` …）
- 18 个 evaluator prompt `.txt`

**后果**：`pip install spectragent` 之后，`BrowserUseAgent.prepare()` 在
`_build_system_prompt` → `prompt_file.read_text()` 处直接 `FileNotFoundError`。
**框架作为已安装的包是完全不可用的。本地开发（editable / 源码树）永远看不出来。**

**修法**（3 行）：
```toml
[tool.setuptools.package-data]
"spectragent" = ["prompts/**/*", "benchmark_data/**/*"]
```
或 `include-package-data = true` + `MANIFEST.in`。
**验收**：`uv build --wheel` 后 wheel 里 `.md`/`.txt`/`.jsonl`/`.json` 计数均 > 0，
且在干净 venv 里 `pip install` 后能跑通 `--dry-run`。

---

## 7. 修复计划

### 7.1 依赖顺序总览

```
                    ┌─────────────────────────────────────────┐
   P0 装配层        │ F1  Composer 注入 (parse_wiring 驱动)     │ ◄── 一切的前提
                    └────────────────┬────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
┌───────────────┐          ┌──────────────────┐         ┌──────────────────┐
│ F2 实例身份    │          │ F4 output_format │         │ F5 BenchmarkRunner│
│   (config-path)│          │  + _PerRunOutput │         │  + trace 身份     │
└───────┬───────┘          └──────────────────┘         └────────┬─────────┘
        ▼                                                        ▼
┌───────────────┐                                       ┌──────────────────┐
│ F3 weave 无状态│                                       │ F6 补全 trace     │
└───────┬───────┘                                       │  (拦 act + 真 rec)│
        │                                               └────────┬─────────┘
        │                                                        ▼
        │                                               ┌──────────────────┐
        │                                               │ F7 Evaluator 注册 │
        │                                               └──────────────────┘
        ▼
┌───────────────────────────────┐   ┌──────────────────────────────────┐
│ F8 session_id 移出 ABI        │   │ F9 Capability registrar 化        │
└───────────────────────────────┘   └──────────────────────────────────┘
                    │                            │
                    └──────────┬─────────────────┘
                               ▼
                    ┌──────────────────────────┐
                    │ F10 registry key 去前缀   │ ◄── 单独立项
                    └──────────────────────────┘
```

### 7.2 逐项 spec

---

#### **F1 — `ConfigComposer` 的 wired receptor 注入改为 `parse_wiring` 驱动**

| | |
|---|---|
| **级别** | P0 / BLOCKER |
| **解锁** | **全部三条轴**。任何新 domain 的 Environment 都会撞同一个 `AttributeError` |
| **依赖** | 无 |
| **对应冲突** | §5.1.1、§5.1.3、§5.5.6 |

**现状**
`composer.py:225` 以 `type(cfg).model_fields` 驱动注入；
`capabilities` 是 `WiredField` 但 codegen 不把它变成 config 字段 → 结构性不可见。
同一个循环的 `:264-265` 又把 `provider:` 这类"非 `__init__` 参数"的嵌套块整个丢掉。

**改法**
1. `_instantiate` 中，把 **`parse_wiring(cls)` 作为 receptor 的主驱动**，config model 只用来**提供值**。
2. 当某个 wiring spec 没有对应的 config 字段时，直接从 `by_layer[spec.registry_name]` 解析
   —— 这正是"声明式多实例 receptor"的语义。
3. **删掉** `composer.py:239-247` 的 `is_receptor` 启发式。
   以 wiring 为驱动后，agent 那个纯声明的 `observation` 字段自然消失，
   §5.5.6 里多余 setattr 到 agent 和 capability 上的 `ClickAction` 也一并消失。
4. 让 codegen 把 `dict[X]` / `list[X]` 型 receptor 暴露成 config 字段。
5. 教 `_instantiate` 把非 `__init__` 的嵌套 wired 子配置（`provider:` 块）**走 setattr 或 splat**，而不是丢弃。

**触及**
`spectragent/core/composer.py`（`_instantiate`、`_resolve_wired`、`_registry_key_of`）；
`spectragent/scripts/generate_all.py` + conscribe codegen；
`tests/_helpers/assembly.py:37-46`（**删掉手工 setattr**）；
`demo_debug.py:251-268`（**删掉 `inject_env_capabilities()`**）；
`configs/*.yaml`（删掉惰性的 `observation:` / `action:` 块）。

**验收标准** ★
```python
# 新增集成测试，不打任何补丁
run = ConfigComposer(registrars=ALL).compose(yaml.safe_load(open('configs/browser_use_local.yaml')))
assert run.env.capabilities == {'browser': <BrowserCapability>}
AgentRuntime.from_composed(run).prepare()          # 不抛
assert run.agent.llm.azure_endpoint == 'https://...'   # provider: 块生效
assert not hasattr(run.agent, 'action')            # 不再有多余的 ClickAction
```

---

#### **F2 — 实例身份从 registry key 换成 config-path / 显式 `id:`**

| | |
|---|---|
| **级别** | P0 / BLOCKER |
| **解锁** | **multi-agent 三种形态全部** + per-agent memory + 同 provider 两个模型 + 多 capability 实例 |
| **依赖** | F1 |
| **对应冲突** | §5.1.2、§4.3(b)(d)、§5.4 #1 #2 |

**现状**
`composer.py:211-213` 用 `__registry_key__`（**类**身份）做 `by_layer` 的键并去重；
`:296` 的 dict 型 receptor 也用它；`:379-398` 的 `_registry_key_of` 同上。
→ 对象图**物理上装不下同一个类的两个活实例**。

**改法**
1. `by_layer` 改为按**实例身份**索引：一个 config 提供的 `id:` 字段，缺省回退 `f"{registry_key}#{ordinal}"`。
2. `_registry_key_of`（`:379-398`）对 dict 型 receptor 返回**实例 id**。
3. `__registry_key__` **只保留**作为进 registrar 的**类查找**。
4. `:211-213` 的去重变成 correct-by-construction（一个实例 id 只建一次），
   并对**两个条目声明同一个实例 id** 抛硬错误而不是静默覆盖。
5. **删除** `ComposedRun` 的单数便捷属性 `agent`/`loop`/`llm`/`memory`（`:80-101`），
   改为 `AgentRuntime.for_agent(run, agent_instance_id)`，
   **从该 agent 自己的 wired receptor 解析** loop/llm/memory，而不是 run 全局查找。
   `env` 可以合法地保持 run-scoped，但必须是**显式共享**而不是隐式单数。

> 按 repo 政策这是**删除，不是加 shim**：单数访问器每个恰好只有一个消费者（`runtime.py:81-85`）。

**触及**
`spectragent/core/composer.py`（约 80-100 行）；`spectragent/core/runtime.py:53, 80-85`；
生成的 per-key config model 需要一个可选 `id` 字段（`generate_all.py` + conscribe codegen）；
所有 `configs/*.yaml`；`tests/test_core/test_composer/test_composer.py`；两个 demo 驱动。

**验收标准**
```python
# 两个同类 agent，各自的配置都保留
run = compose({'agent': [
    {'id':'orch',   'agent_id':'browser_use', 'max_steps':30, 'memory':{'memory_id':'simple','max_messages':200}},
    {'id':'worker', 'agent_id':'browser_use', 'max_steps':99, 'memory':{'memory_id':'simple','max_messages':5}},
]})
a, b = run.instances('agent')
assert a.max_steps == 30 and b.max_steps == 99
assert a.memory is not b.memory
assert a.memory.max_messages == 200 and b.memory.max_messages == 5
```

---

#### **F3 — `weave()` 无状态化：`ctx` 移进闭包**

| | |
|---|---|
| **级别** | P0 / BLOCKER |
| **解锁** | multi-agent（N 个 agent 共用一个 `loop_id`）。**F2 没有 F3 等于白做** |
| **依赖** | F2 |
| **对应冲突** | §5.4 #3、§4.3(b) |
| **成本** | **约 6 行** |

**现状**
```python
# loops/_registrar.py:115-125
self.ctx = deps                    # ← 整字典替换，存在 loop 实例上
loop = self
async def run(task, **kw):
    return await loop._execute(agent, task, **kw)     # ← 闭包捕获的是 self，不是 deps
agent.run = run
```

**改法**
```python
def weave(self, agent, **deps):
    self._validate_slots(agent)
    ctx = dict(deps)                                   # ← 局部
    loop = self
    async def run(task: str, **kw):
        return await loop._execute(agent, task, _ctx=ctx, **kw)
    agent.run = run
    return agent
```
- 删除 `AgentLoopBase` 上的 `ctx: dict = field(...)` 声明（`_registrar.py:76`）
- 删除 `AgentLoop` Protocol 上的 `ctx: dict[str, Any]` 成员（`loops/base.py:37`）
- `ReactLoop._execute` 改读参数（`react.py:91-113`）
- **删掉** `_registrar.py:10-11` 那句从未实现的 `copy.deepcopy` docstring

**触及**
`core/loops/_registrar.py`、`core/loops/base.py`、`core/loops/react.py:91-113`、
`core/runtime.py:116-127`、`tests/test_core/test_loops/test_weave.py`、
`tests/test_core/test_runtime/test_agent_runtime.py:151`。

**验收标准**
```python
loop = ReactLoop()
loop.weave(a1, env='ENV1', llm='LLM1', memory='MEM1', act_fn='ACT1')
loop.weave(a2, env='ENV2', llm='LLM2', memory='MEM2', act_fn='ACT2')
# a1 仍然跑在 ENV1 上
assert not hasattr(loop, 'ctx')
```

---

#### **F4 — 放开 `output_format` 并在同一个 commit 里修 `_items_to_actions`**

| | |
|---|---|
| **级别** | P0 |
| **解锁** | multi-domain（结构化输出是任何 tool-use / function-calling loop 的前提）；复活 ~600 行 provider 代码 + per-run 动作集窄化 |
| **依赖** | 无（但**必须两个一起改**） |
| **对应冲突** | §6.1.1、§6.1.2 |

**改法**
1. `react.py:162` → `completion = await llm.ainvoke(context, output_format=output_model)`
2. `browser_use.py` 的 `_items_to_actions` 顶部解包 RootModel：
   `raw = raw.root if isinstance(raw, RootModel) else raw`
   —— 或者更干净：给 `_PerRunOutput` 一个 `model_validator(mode='after')`
   把 `action` 拍平回 `list[dict]`，让两个输出模型在下游 wire-identical
3. `browser_use.py:246` 改为对 `self._per_run_output_model or BrowserUseOutput` 校验，
   让"Pydantic 侧带合法动词集的拒绝"在文本路径上也真的发生
4. 删掉 `react.py:150-156` 那段已经变假的注释
5. `output_format` 非 None 时返回的是 `ChatInvokeCompletion[T]`，
   所以 `react.py:241-245` 的 `raw_output.model_dump_json()` 分支变活 ——
   与 `:184-188` 的 `str(raw_output)` 孪生分支**对齐序列化行为**

**触及**
`core/loops/react.py:157-162, 184-188, 241-245`；
`agents/browser_use.py:230-231, 246, 479-542, 551-591`；
**新建 `tests/test_core/test_llm/`（该子系统目前 pytest 覆盖率为 0）**。

**验收标准** ★
```python
# 往返测试
a.prepare(task='t', action_descriptors=[...])
M = a.get_react_output_model()
out = M.model_validate({'memory':'m','action':[{'browser.click':{'index':1}}]})
assert a.parse_react_output(out) == [Action(name='browser.click', params={'index':1})]
# 并断言 llm.ainvoke 收到非 None 的 output_format
```

---

#### **F5 — `BenchmarkRunner` + trace 身份闭环**

| | |
|---|---|
| **级别** | P1 |
| **解锁** | **multi-benchmark 的根** —— 一个无法与 `EvalReference` join 的 trace 无法被任何东西评测 |
| **依赖** | F1 |
| **对应冲突** | §5.3.2、§5.3.5、§5.3.6、§5.3.10 |

**改法**
1. 新建 `BenchmarkRunner`，按 task 驱动完整生命周期：
   ```
   for task_id in loader.task_ids():
       td  = loader[task_id]
       run = composer.compose(cfg)                       # 或复用 + memory.clear()
       await run.env.setup(**loader.get_task_context(task_id))
       result = await AgentRuntime.from_composed(run).execute(
           task=td.task_text, task_id=task_id, timeout=...)
       await run.env.teardown()
   ```
2. **把 env 生命周期移进 `AgentRuntime.execute`**（`on_run_start` 前 setup，`on_run_end` 后 teardown），
   让顺序成为框架保证而不是调用方约定。
   这同时修好 `TaskLoader.get_task_context() → Capability.setup()` 这条今天不存在的链路。
3. `on_run_start` dispatch（`runtime.py:173-179`）**补齐** `task_id`（`:146` 已在作用域内）、
   `loop=self._run.loop`、`llm=self._run.llm`。
4. `TraceConfig`（`trace/models.py:58-72`）**新增** `benchmark: str`、`subset: str`；
   `trace_writer.py:70-78` 填充 `loop_id` 和 `llm_model`。
5. **修 §5.3.5**：每条终止路径都 dispatch `on_run_end`，
   结果本身用 `agent_done=TIMEOUT|ERROR` 表达，`on_run_error` 只留异常细节。
   exception 分支在 re-raise 前构造 `AgentResult(agent_done=ERROR, env_status=FAILED, error=str(exc))`。
6. **修 §5.3.6**：`on_run_end(*, result, env)` —— 让 env-state 评分 hook 能在 teardown 前探活状态。
7. **修 §5.3.10**：`execute()` 开头 `memory.clear()`，或由 runner 保证每 task 一个 `ComposedRun`。
8. `partial_steps` 要么让 Loop 把 `step_records` 暴露到 Runtime 够得到的地方，要么从 Protocol 里删掉。

**触及**
新模块 `spectragent/core/benchmark_runner.py`（或 `scripts/`）；
`core/runtime.py:140-230`；`core/trace/models.py`；`core/hooks/base.py:37-50`；
`core/hooks/trace_writer.py:61-101`；`core/hooks/metrics_collector.py`（签名）；
`pyproject.toml [project.scripts]`。

**验收标准**
```python
# 一次 timeout 的 run 也要留下完整 trace
t = RunTrace(trace_dir)
assert t.manifest.status == 'error'          # 不是 'running'
assert t.result is not None
assert t.config.benchmark == 'online_mind2web' and t.config.task_id == '...'
assert t.config.llm_model and t.config.loop_id
```

---

#### **F6 — 补全 trace：拦 `act`、写真实 `StepRecord`、重定向 observation buffer**

| | |
|---|---|
| **级别** | P1 |
| **解锁** | multi-benchmark 轨迹评分（τ-bench tool-call 序列、Mind2Web step matching、SWE 命令轨迹） |
| **依赖** | F5 |
| **对应冲突** | §5.3.3、§5.5.5、§6.1.3、§6.1.4、§5.3.8 |

**改法**
1. `TraceWriter.__intercepts__` 增加 `Intercept("act", role="env", advice="after")`，
   把 `(Action, ActionResult)` 对累积进 step buffer。
   —— **更好的做法**：让 Loop 把它已经构造好的 `StepRecord`（`react.py:260-266`）
   通过一个可拦截的 Protocol 方法交出去，WAL 直接写 `record.model_dump()`。
   这同时修好恒空的 `observation_summary` / `token_usage`。
2. **删掉** `trace_writer.py:143-146` 那个 `# Write step record skeleton` 的两键 dict。
3. observation buffer 从 `after_observe` 改到
   `Intercept("build_react_prompt", role="agent", advice="before")`
   —— proxy 已经路由这个调用（`runtime.py:116` weave 到 proxy 上），
   注意按 `proxy.py:125-131` 需要**同步 handler**。
4. `HookBase` 增加 `priority: int` ClassVar，`_dispatch_lifecycle` 排序（修 §6.1.4）。
5. `react.py:118` 的 `total_usage` 改回 `None`，让"未测量"可检测；
   在 `:162` 之后累加 `completion.usage`（给 `AgentUsage` 加 `merge()`/`__iadd__`）；
   在 `:206-212` 和 `:260-266` 给 `StepRecord` 打上 `token_usage`。
   `MetricsCollector` 改为**增量式**（成本富化、per-model 拆分）而非权威覆写 ——
   它现在用 LLM 调用次数覆盖 Loop 自己的 `steps=len(step_records)`，这是两个不同的量。
6. screenshot 的落盘契约下沉到 fragment 层（修 §5.3.8）：
   `ScreenshotObs` 从 `image_bytes` 写 `NNN_screenshot.png` + 一个 `.json` sidecar 放 viewport 元数据，
   这样 reader 现有的 `.png` glob 和 `get_observation` 双路径**同时变正确**，
   顺带不再往 JSON 里塞几 MB base64。
7. 删掉 `build_observation_model` 里恒 `0.0` 的 `timestamp` 字段（修 §6.5.4）。

**触及**
`core/hooks/trace_writer.py`（约 40 行）；`core/hooks/_registrar.py`（`priority`）；
`core/runtime.py:240-248`；`core/loops/react.py:118, 162-164, 206-212, 260-266`；
`schemas/agent_result.py`（`AgentUsage` merge）；`core/trace/wal.py`、`reader.py`；
`schemas/observation/{fragments,dynamic_model}.py`；
`tests/test_core/test_hooks/`、`tests/test_core/test_trace/`。

> **注意**：on-disk step-record 格式会变，现有 14 个 trace 变成 legacy。

**验收标准**
```python
rec = RunTrace(d).get_step_record(0)
assert set(rec) >= {'step','llm_output','actions','action_results','timestamp','token_usage'}
assert RunTrace(d).screenshots                                  # 非空
assert 'task' in {p.name.split('_',1)[1].rsplit('.',1)[0] for p in obs_files}  # loop fragment 落盘了
assert result.metrics.usage.total_tokens > 0                    # 无 hook 时也非零
```

---

#### **F7 — 建立 `EvaluatorRegistrar` + `spectragent/evaluator/`**

| | |
|---|---|
| **级别** | P1 |
| **解锁** | **multi-benchmark 落地**。评测是今天唯一一条没有 plug point 的可变轴 |
| **依赖** | F5、F6（要有值得评的 trace） |
| **对应冲突** | §5.3.1、§5.3.9、§5.3.4、§5.3.11 |

**改法**
1. 新建 `spectragent/evaluator/`：
   ```python
   EvaluatorRegistrar = create_registrar("evaluator", BaseEvaluator,
                                          discriminator_field="evaluator_id")
   class BaseEvaluator(Protocol):
       async def evaluate(self, trace: RunTrace, reference: EvalReference) -> EvalResult: ...
   ```
   **trace 进，判决出，不碰活环境**（需要活环境的 env-state 评分走 F5 的 `on_run_end(env=...)`）。
2. `generate_all.py:61-75` 加一行 `"evaluator": EvaluatorRegistrar` → 变成 YAML 可选层。
3. 把 gitignore 的 `benchmarks/*/eval/src/` 三个 grader 移植成注册的 `EvaluatorBase` 子类，
   用和 loader 相同的 `benchmark_name` 键。
4. **prompt 树合并成一份**，目录名对齐 registry key
   （`BrowseComp` → `browsecomp` 等），并从 `browser/` 下面移出来（评分 prompt 不是 browser 关注点），
   这样 Evaluator 能用 `self.__registry_key__` 直接解析，**无需别名表**。
   删掉 `benchmarks/prompts/` 和不可 import 的 `benchmarks/*/eval/src/`。
5. **顺带修 data loader**（§5.3.4）：
   - `resolve_data_dir(env_type, registry_key, data_dir)` → `BENCHMARK_DATA_ROOT / env_type / registry_key`
   - `TaskSetup` 变成真正的判别联合，加 `DockerSetup` / `VMSetup`；
     `_normalize_raw` 从 `self.env_type` 推导 `setup_type`，不再 `setdefault("browser")`
   - 每个 setup variant 自带 `to_context() -> dict`，让 `get_task_context` 不再 `isinstance(..., BrowserSetup)`
   - 把"解析出零个 task"从静默 warning 改成**硬错误**
   - `EvalLoaderBase` 抽出可覆写的 `_normalize_raw`（§5.3.11）

**触及**
新包 `spectragent/evaluator/`；`scripts/generate_all.py`；重新生成 `spectragent/generated/`；
`core/data_loader/{_common.py, task/_registrar.py, eval/_registrar.py}`；
`schemas/task.py`；3 个目录 `git mv`；新建 `tests/test_evaluator/`。

**验收标准**
```
uv run spectragent-bench --benchmark online_mind2web --subset test --config configs/....yaml
# → traces/ 下每个 task 一个目录，evals/ 下有 EvalResult，最后打印 EvalSummary
```

---

#### **F8 — `session_id` 移出框架 ABI**

| | |
|---|---|
| **级别** | P1 |
| **解锁** | multi-domain；同时为 multi-agent 的 tenancy 腾出干净位置 |
| **依赖** | 无（但改 F2/F3 之后做更省事） |
| **对应冲突** | §5.2.1、§5.2.6 |

**改法**（**删除，不是加 shim**）
1. 从下列位置**删掉** `session_id`：
   `BaseAction.execute`、`Environment.observe/act`、`EnvironmentBase.act/observe`、
   `ReactLoop._execute`、`AgentRuntime.prepare/execute`
2. 子上下文选择是 **capability 内部**的事：
   `BrowserCapability` 已经跟踪 `_default_session_id`，也已经有 `browser.switch_tab` 动作
   —— 那才是表达"换我的子上下文"的 domain-neutral 方式（顺带见 §6.6.1，那个动作现在是坏的）
3. 如果将来某个 capability 真的需要 per-call 路由，
   给它一个**按 `capability_id` 查表的不透明 `context: Mapping[str, Any]`**，
   **绝不是一个全局标量**
4. 顺带修 §5.2.6：`EnvironmentBase.setup(context: Mapping[str, Mapping[str, Any]])` 按 capability id 分发

**触及**
`core/actions/base.py`、`core/environments/{base,_registrar}.py`、`core/loops/react.py`、
`core/runtime.py`、**全部 14 个 browser action 类** + `capability.py`（改读 `self._default_session_id`）、
`tests/test_core/test_loops/*`、`tests/test_core/test_capabilities/*`、`tests/test_core/test_aop/test_weaver.py`。

---

#### **F9 — Browser Capability 的 registrar 化 + 协商驱动的成本控制**

| | |
|---|---|
| **级别** | P1 |
| **解锁** | multi-domain（让 "Watchdog = Resolver" 变成真的）；**纯视觉 agent 每步成本立刻减半** |
| **依赖** | F1 |
| **对应冲突** | §5.5.4 |

**改法**
1. **改用 `agent_cdp` 已有的 `WatchdogRegistrar`**（`✅R` 它已经存在），
   替掉 `_TAB_WATCHDOGS` / `_BROWSER_WATCHDOGS` / `_WATCHDOG_FACTORY` 三张手工表。
   每个 watchdog 声明 `level: ClassVar[Literal["tab","browser"]]` 和 `provides: frozenset[str]`，
   依赖通过一个小的注入 context 对象拿，而不是 bespoke lambda。
2. `provided_fragments` 和 `BrowserStateRequestEvent` 的 include 标志
   **从协商结果推导**，而不是从静态 `__wiring__`
   → `CapabilityBase.setup` 增加一个协商参数。
   **这一条单独就能让 UI-TARS 这类纯视觉 agent 每步不再全量抽 DOM。**
3. `_ObserveCycleCache` 从写死字段的 dataclass 改成 `dict[str, FragmentBase]`，
   `resolve_fragment` 的 if/elif 链随之消失。
4. 未知 watchdog 从 `logger.warning` 改成**硬错误**。
5. 顺带把 `MouseTrajectoryRegistrar` / `DOMCleanerRegistrar` / `DOMSerializerRegistrar`
   注册进 `generate_all.py:40-75`（修 §5.1.7、§6.6.4）。

**触及**
`capability.py` 约 7 处（`:78-79, 99-102, 116-119, 553-561, 626-630, 654-667, 981-1006`）；
`profiles.py:27-52`；`watchdogs/events.py`；`core/capabilities/base.py`（setup 签名）；
`scripts/generate_all.py`。

---

#### **F10 — 全限定 registry key 解耦**（单独立项）

| | |
|---|---|
| **级别** | P2 / 单独立项 |
| **解锁** | 一个 agent 跨 domain 复用（UI-TARS 同时跑 browser / desktop / android） |
| **依赖** | F1–F9 |
| **对应冲突** | §4.1 末尾、§5.2.3 |

**现状**
`ActionBase.__registry_key__ = "{source}.{local_id}"`，
agent `__wiring__` 硬编码 `browser.click` 这类全限定名，
`negotiate_action` 做全限定名的集合减法 → agent 与 capability 类型级焊死。

**改法（两个方向，需要决策）**
- **A**：agent 声明**短名**（`click`/`type`/`dom`），Environment 在组装时按 capability 前缀解析。
  协商在短名空间做，descriptor 出去时才加前缀。
- **B**：改 conscribe，让 `__wiring__` 支持 **capability-relative 键**
  —— 因为 conscribe 是自有库，这可能比在 spectragent 里绕更便宜。

> **建议**：动手前先在 conscribe 侧评估 B。这是唯一一处"改上游可能比改下游便宜"的地方。

**触及**：几乎所有 agent、所有 action、negotiation、generated。**不要和别的改动混在一个 PR 里。**

---

### 7.3 快速修复（不阻塞任何人，可以随时捡）

| 项 | 位置 | 改法 | 冲突 |
|---|---|---|---|
| `IncompatibleFragmentError` 只报一个缺失项 | `negotiation/observation.py:70,79-82` | 先收集完整缺失集再抛，对齐 `action.py:54-60` | §6.7.1 |
| fragment 名冲突检查上移 | `dynamic_model.py:53-61` → `_observation_rules` | 约 5 行，把 step-0 崩溃变成 config 错误 | §5.2.3 |
| `build_observation_model` 每步重建类 | `environments/_registrar.py:187` | 按 `active_fragments` frozenset 缓存 | §2.1 |
| `generated/__init__.py` 的裸 except | `:15` | 收窄成 `except ImportError`，加一个 import 冒烟测试 | §5.1.4 |
| 删除 deprecated 别名 | `schemas/_types.py:138-142`、`core/llm/__init__.py:68-71` | 直接删（CLAUDE.md §0.4） | §5.6 |
| `TraceWriter` 在实例上迭代 `model_fields` | `trace_writer.py:126-127` | 改用 `type(obs).model_fields` | §6.5.2 |
| `_atomic_write` 掩盖异常 | `wal.py:47-51` | 修 fd 生命周期 + 加父目录 fsync | §6.5.1 |
| `AOPProxy.__setattr__` | `aop/proxy.py` | 转发到 `_target`；方法解析改懒加载 | §5.5.3 |
| `Weaver` role 校验 | `aop/weaver.py:43-91` | 对无人认领的 role 和缺失的目标方法**报错/警告** | §5.5.3 |
| advice 签名加 target/role/method | `aop/proxy.py:99-110` | 两个值都已在作用域内；所有 shipped handler 都以 `**ctx` 结尾，**是增量改动** | §5.4 #7 |
| `SimpleMemory` 加 `keep_system` | `memory/simple.py:35-36` | 抄 `SlidingWindowMemory` | §6.3.1 |
| `browser.switch_tab` | `watchdogs/action_watchdog.py` | 接上 `SwitchTabEvent` handler，否则**从 `__wiring__` 里删掉这个动作** | §6.6.1 |
| 云 provider `viewport=` | `capability.py:401-406` | 别往 `backend.open()` 传；改走 `setDeviceMetricsOverride` | §6.6.2 |
| `agent.max_steps` 惰性 | `browser_use.py:108` 等 | 要么删字段，要么让 Runtime 用它覆盖 loop | §5.5.7 |
| prompt/执行 max_actions 不一致 | `browser_use.py:299` vs `react.py:237` | 用同一个来源；截断时至少加日志 | §6.1.5 |
| **★ 打包遗漏（3 行，BLOCKER）** | `pyproject.toml:71-73` | 加 `[tool.setuptools.package-data]` 收 `prompts/**/*` + `benchmark_data/**/*`；**实测 wheel 里 0 个数据文件** | §6.7.3 |

---

## 8. 未验证线索（未做对抗验证，动手前请自行核实）

按建议核实优先级排序：

> 原列表第 1 项"打包遗漏"**已在本轮验证并确认**，已升级为 BLOCKER，移至 §6.7.3。

1. **★ 消息模型无 tool role / `ainvoke` 无 `tools` 参数**（§5.2.5）—— 如属实，挡住 τ-bench/ToolBench/GAIA/SWE **一整类**。
2. `AOPProxy` 只代理普通属性访问（`proxy.py:49-54`）—— 被包装的 env/llm **不能**做 context manager、不能迭代、不能调用、不能 `len()`。而 `CDPClient` 已经实现了 `__aenter__/__aexit__`。
3. `_apply_around_chain`（`proxy.py:168-214`）**没有任何 async 路径测试** —— 唯一的 around 测试断言的是"around 用在 sync 方法上会抛"。
4. 非阻塞 hook handler 是 5 处裸 `asyncio.create_task(...)` 且丢弃 handle（`proxy.py:94,106,185,196`；`runtime.py:248`）—— 中途可被 GC，从不 drain。今天潜伏是因为两个 shipped hook 都是 `blocking=True`。
5. `TraceWriter` 把一次 `llm.ainvoke` 等同于一步（`:118,158`）—— 任何非 1:1 调用节奏的 loop（CodeAct、PlanExecute、reflection、self-consistency）会重复计数或丢观测。
6. `parse_file`（`data_loader/_common.py:53-57`）对缺 `tasks` 键的 JSON 对象**返回空列表且无警告** —— 任何用不同 wrapper key 的 benchmark 静默变成空数据集。
7. `EvalLoaderBase._load` 无 `_normalize_raw`；shipped 的 `lexbench_browser/tasks_private.jsonl` 用 `id` 而非 `task_id` → 加载中途 `KeyError`。
8. 没有任何生命周期点让"活着的环境状态"对"框架认识的组件"可见（§5.3.6 的另一种表述）。
9. `AnthropicMessageSerializer` 塌缩 SystemMessage（§6.4.7）。

---

## 附录 A：需要用户/负责人决策的开放问题

| # | 问题 | 为什么需要决策 |
|---|---|---|
| A1 | **F10 走方向 A（短名 + 组装时解析）还是 B（改 conscribe 支持 capability-relative 键）？** | conscribe 是自有库；改上游可能比在 spectragent 里绕便宜得多 |
| A2 | **multi-agent 的三种形态是否都要在同一轮做？** | 用户已确认三种全要。但 (a) 编排型还需要 A2A 协议层（`architecture_design.md §14.2` 标记"待定"），(b)(c) 只需要 F2+F3 |
| A3 | **`agent_success` 要不要改名？** | 它是 LLM 自述，不是判决，而 `skyvern.py:190`/`ui_tars.py:258` 还硬编码 `True`。判决应属于 `EvalResult.predicted_label` |
| A4 | **现有 14 个 trace 要不要迁移？** | F6 会改 on-disk step-record 格式 |
| A5 | **`docs/architecture_design.md` 要不要按本文回写？** | 本文列了 15 处文档漂移（§5.6）。CLAUDE.md 目前声称文档是 source of truth，与本次审查的前提冲突 |

---

## 附录 B：本次审查的方法与元数据

### B.1 方法

```
Phase 1  Map        10 个 subagent 并行精读子系统（代码优先，文档视为不可靠）
Phase 2  Hunt       6 个耦合视角，跨子系统摘要驱动
Phase 3  Verify     每个视角 top-3 结论交给专门被要求"反驳它"的验证者
Phase 4  Synthesize 综合
外加     主进程独立复读主干 + 4 次 live repro + 磁盘普查 + pytest 基线
```

### B.2 元数据

| 指标 | 值 |
|---|---|
| Agent 总数 | 35（10 mapper + 6 lens + 18 verifier + 1 synthesizer） |
| Subagent token | 约 6,474,000（含一次因 ECONNRESET 的 synthesizer 重跑） |
| 工具调用 | 2,326 |
| 原始 finding | 51 |
| 通过对抗验证 | 15 |
| **被驳回 / 降级** | **3** |
| 未验证（top-3 配额外） | 33 |
| 子系统 map | 10 |

### B.3 被驳回的结论（记录在案，避免重复发现）`❌X`

1. **"Action ABI 注入整个 capability 阻塞新 domain"** → 降为 MINOR。
   新 capability 可以定义自己的私有面。保留的只是"框架 Protocol 文档里写着浏览器私有字段名"这个**设计声明问题**。
2. **"`MetricsCollector` 全局累加会让 multi-agent 串味"** → 降为 MINOR。
   累加器是 per-hook-instance 且在 `on_run_start` 归零，单实例只支持一个 in-flight run；
   而 `ComposedRun.agent` 的 `single()` 让那个场景**今天不可达**。
   真实存在的是 `_extract_model_name` 恒返回 `""`（§5.4 #7）。
3. **"两个 agent 共享 env 会因 session_id 冲突"** → 降为 MINOR。
   今天没有 multi-agent 路径，该推论 unreachable。**结构性判断保留**（§5.2.1）。

### B.4 对主报告的修正记录

审查过程中被验证者纠正的四处（**已在正文中体现**）：

1. `AgentResult` 的 `extra="forbid"` **不**阻止扩展 —— `agent_metadata` 是显式槽（§5.3.7）
2. Observation provenance 在**内存中没丢** —— `obs.terminal.fragment_id` 可用；只在 WAL 丢（§5.2.3）
3. `role="agent"` **不是**死的 —— `build_react_prompt`/`parse_react_output`/`prepare` 都能正常拦截；**只有 `run` 是死的**（§5.5.3）
4. "三方协商"的第三方（Loop）**恒为空** —— 实际是两方（§5.5.1）

### B.5 复现环境

```
repo      /Users/qly/QLY/code/af  @ main d830831
python    3.12 (.venv)
pytest    560 passed in 2.28s
pydantic  2.12.5
anthropic 0.86.0
conscribe / agent-cdp  自有库（可改，但不在本仓库）
```
