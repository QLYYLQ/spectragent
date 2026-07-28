# F10 — 全限定 registry key 解耦：立项材料

> 来源：`HANDOFF_260728_coupling_and_fixes.md` §7.2 F10 + §4.1 末尾 + §5.2.3
> 状态：**未实施**，本轮（F1–F9 + F4.5）刻意排除
> 需要的决策：走方案 A 还是方案 B

---

## 1. 问题

`ActionBase.__registry_key__` 是全限定点分键 `"{source}.{local_id}"`：

```python
# core/actions/_registrar.py:33-34, 79-81
__registry_key__ = "browser.click"
@property
def source(self) -> str:          # 前缀段 = 拥有它的 capability
    return self.__registry_key__.split(".", 1)[0]
```

agent 的 `__wiring__` 因此硬编码全限定名：

```python
# agents/ui_tars.py
"capability": ("capability", ["browser"]),
"action": ("action", ["browser.click_coordinate", "browser.type", "browser.done"], ...)
```

而 `negotiate_action` 做的是**全限定名的集合减法**。

**后果**：agent 与 capability 在**类型层面焊死**。UI-TARS 是一个 computer-use 模型，它自己的配置项写着
`prompt_template: COMPUTER_USE | MOBILE_USE | GROUNDING` —— 但它的 prompt 放在
`prompts/browser/agents/ui_tars`，wiring 钉在 `browser.*`。要跑 OSWorld，
`desktop.click_coordinate` 和 `browser.click_coordinate` 是**两个不同的 registry key**，协商必然失配。

`core/environments/_registrar.py:36-38` 显示这是从"运行时加前缀"**主动改成**"注册键带前缀"的 ——
所以这不是遗留，是一个当时有理由的设计选择，现在需要重新评估。

---

## 2. 为什么前缀本身是对的（不要退回去）

`actions/_registrar.py:36` 写得很明确：全限定名让 capability 之间
**collision-free（`browser.click` vs `desktop.click`）**。这个价值是真的：

- 组合环境（`browser + tool_sandbox`）里两个 capability 各有 `click`/`call`，必须能区分
- action 分发在 `EnvironmentBase` 按前缀路由，descriptor 出给 LLM 时也要唯一

**所以 F10 不是"去掉前缀"，是"把前缀从 agent 的声明面挪到组装/分发面"。**

---

## 3. 一个已确认的不对称（顺带要一起修）

同一个仓库里，Action 端和 Observation 端对前缀的处理是**相反**的：

| | Action | Observation |
|---|---|---|
| registry key | `browser.click` | `browser.dom` |
| 协商空间 | **全限定名** | 全限定名 |
| 交付给消费者 | **保留全限定名** | **剥掉前缀** → `obs.dom` |
| 冲突检测 | `action.py:54-60` 算完整缺失集 | `dynamic_model.py:53-61` 在 step 0 抛 `ValueError` |

CLAUDE.md §2.5 声称 "Action system — symmetric to Observation"。**这句话是假的。**

实测：`swe.terminal` + `docker.terminal` 同时激活 → 协商静默通过 →
`env.observe()` 在 **step 0** 抛 `Duplicate observation field name 'terminal'`。
（今天不可达，因为 9 个已注册 fragment 无同名冲突。）

内存中 provenance 没丢（`obs.terminal.fragment_id` 是 `@property`），
但 **WAL 里丢了** —— `model_dump()` 不含 property，且 trace writer 用剥掉前缀的字段名命名文件。

**F10 应当把这两端的策略统一，而不是只改 Action 端。**

---

## 4. 两个方案

### 方案 A —— 短名声明 + 组装时解析（纯 spectragent 内）

agent `__wiring__` 声明**短名**：

```python
"action": ("action", ["click_coordinate", "type", "done"], ...)
```

- Environment 在组装时按自己持有的 capability 前缀，把短名解析成全限定名
- 协商在**短名空间**做集合减法
- descriptor 出给 LLM 时才加前缀（或者根据是否单 capability 决定加不加，沿用现有约定）

**优点**
- 不动上游，改动完全可控
- 和现有的"单 capability 环境不加前缀、组合环境加前缀"约定天然一致
- UI-TARS 一份 wiring 同时适配 browser / desktop / android

**缺点**
- 短名在**跨 capability 时会歧义**：组合环境里 `browser.click` 和 `desktop.click` 都匹配短名 `click`。
  需要一条消歧规则（agent 额外声明 capability 亲和？按 wiring 里 capability 列表的顺序？显式 `browser.click` 仍然合法作为逃生口？）
  —— **这是方案 A 的主要设计负担，必须先定，不能边写边定。**
- conscribe 生成的 `Literal[...]` 约束会变成短名空间，配置层的"非法组合在 Pydantic 阶段失败"这条保证需要重新验证

### 方案 B —— 改 conscribe 支持 capability-relative 键

让 `__wiring__` 原生理解"相对于某个 capability 的键"，由 conscribe 在生成 `Literal` 时展开。

**优点**
- 消歧规则表达在**声明层**而不是解析层，语义更清楚
- 一次改动同时惠及 Observation 端（同一个不对称的另一半）
- conscribe 是自有库，已经证明可以快速改 + 发版（本轮刚发了 1.2.0）

**缺点**
- 上游改动面更大，且 `agent-cdp` 也依赖 conscribe（虽然它零 `__wiring__` 声明，风险低）
- 需要设计一套新的 wiring 语法（第四种 mode），而现有三种 mode 已经是承重结构

**handoff 的建议**：动手前先在 conscribe 侧评估 B ——
"这是唯一一处'改上游可能比改下游便宜'的地方"。

---

## 5. 本轮的新信息（handoff 写完之后才知道的）

这些会影响 A/B 的取舍：

1. **conscribe 已证明是可快速迭代的**。本轮改了三个 codegen bug、发了 1.2.0，往返成本低于预期 —— 这让方案 B 比 handoff 写作时更可行。
2. **conscribe 1.2.0 让容器型 receptor 变成 `list[Literal[...]]`**，多 capability 组合环境**第一次真正可从 YAML 声明**。这意味着"组合环境里的短名歧义"从理论问题变成了**马上会遇到的实际问题** —— 提高了方案 A 消歧规则的紧迫性。
3. **F9 发现 dataclass + conscribe metaclass 有顺序陷阱**：metaclass 跑在 `@dataclass` 之前，wiring 会把 `WiredField` 描述符装成字段默认值。任何在 dataclass 类上新增 `__wiring__` 条目的方案都要先趟这个坑。
4. **F4.5 指出 `ToolDefinition` ↔ `ActionDescriptor` 近乎同构**。如果 action 命名空间要重构，应当**同时**考虑让 action 协商直接喂 `tools=` ——否则 F10 之后还要再动一次同一批文件。

---

## 6. 建议的执行顺序（无论 A 还是 B）

F10 的 blast radius 是"几乎所有 agent + 所有 action + negotiation + generated"，
所以**必须**独立成一个 PR，并且：

1. 先定消歧规则（A）或 wiring 语法（B），写成一页纸，确认后再动代码
2. 同时修 Observation 端的剥前缀不对称（§5.2.3），否则修完 Action 端两边更不一致
3. 把 `dynamic_model.py` 的 step-0 `ValueError` 上移到协商期（§7.3 已列，约 5 行）
4. 顺带评估是否合并 `ToolDefinition` / `ActionDescriptor`
5. 验收：一个 agent（UI-TARS）在 **不改 wiring** 的前提下，同时通过 browser 和一个 mock desktop capability 的协商

---

## 7. 待决策

| # | 问题 |
|---|---|
| **D1** | 走 A 还是 B？ |
| **D2** | 若走 A：组合环境里的短名消歧规则是什么？（capability 亲和声明 / wiring 顺序 / 全限定名作为逃生口） |
| **D3** | Observation 端要不要在同一个 PR 里改成和 Action 端一致？（建议：要） |
| **D4** | 要不要顺带合并 `ToolDefinition` 和 `ActionDescriptor`？ |
