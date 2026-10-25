# docs/glossary.md Format

## Structure

```md
# Spectragent — 术语表

本文件只定义术语，不含任何实现细节。实现见 `docs/architecture_design.md`。

## Agent 层

**Agent**：
承载领域知识的被动组件 —— 负责构造 prompt、解析输出。不持有 Loop 引用。
_避免_：Policy、Actor、Brain

**Loop**：
执行策略 —— 驱动 observe → think → act 的编排流程。
_避免_：Runner、Executor、Strategy

## Environment 层

**Capability**：
Environment 内一块可独立装配的能力单元，声明自己 provides 哪些 fragment、能执行哪些 action。
_避免_：Backend、Adapter、Plugin

**Resolver**：
Capability 内部生产单个 observation fragment 的组件。Browser Capability 用 Watchdog 充当 Resolver。
_避免_：Producer、Extractor、Collector
```

## Rules

- **Be opinionated.** When several words exist for one concept, pick the best and list the rest under `_避免_`. This matters more here than in most repos — the same idea shows up as *capability* / *backend* / *adapter* in different corners of the tree, and picking one is the point.
- **Keep definitions tight.** One or two sentences. Define what it IS, not what it does.
- **Only framework-specific terms.** `Loop`, `Capability`, `Resolver`, `fragment`, `negotiation`, `registrar`, `__wiring__` belong. General Python, asyncio, pytest, and Pydantic concepts do not, however heavily the repo leans on them. Before adding a term, ask: is this specific to Spectragent, or is it something any Python project would need to explain? Only the former belongs.
- **A term being a class name is fine.** Much of this vocabulary is also code identifiers. That does not disqualify it — `Resolver` is a domain concept that happens to have a class. What disqualifies a term is being a *general* concept, not being *implemented*.
- **Group under subheadings** by architectural layer (Agent 层 / Environment 层 / Action & Observation / Trace & 评测 / 注册与配置). A flat list is fine while the glossary is short.
- **Terms in English, definitions in 中文** — matching `docs/`.

## Single context

`spectragent/` is one context. One `docs/glossary.md`, created lazily on the first resolved term. No root `CONTEXT.md`, no `CONTEXT-MAP.md`, no per-package glossaries.
