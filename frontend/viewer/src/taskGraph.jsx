/**
 * The task view: every task's runs drawn like `git log --graph`, one commit
 * per policy step, read top-down from step 0. Clicking a commit opens the
 * viewer's own step detail beside the graph (a bottom sheet on a phone).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { number, query, replaceRoute, runRoute, statusLabel } from "./common.jsx";
import {
  callText,
  commitStatus,
  endLabel,
  forkChips,
  layoutTask,
  loadGraphs,
  readStored,
  restorationChips,
  runLabel,
  taskGroups,
  taskSummary,
  verdictChip,
  visibleLines,
  writeStored,
} from "./taskGraph.js";
import "./taskGraph.css";

const href = (route) => `#${new URLSearchParams(route)}`;
const FOLD_KEY = "spectragent.viewer.tasks.folded";
const TASKS_PER_PAGE = 30;
const LANE_W = 18;
const X0 = 11;
const Y = 17;
const laneX = (lane) => X0 + lane * LANE_W;
const storage = () => {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
};

async function readJSON(url, signal) {
  const response = await fetch(url, { signal });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "记录读取失败");
  return data;
}

/** Each named run's graph, read three at a time and kept for this revision. */
function useGraphs(keys, revision, enabled) {
  const [data, setData] = useState({});
  const cache = useRef({ revision, data: {} });
  const signature = JSON.stringify(keys);
  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    if (cache.current.revision !== revision) cache.current = { revision, data: {} };
    setData({ ...cache.current.data });
    const needed = JSON.parse(signature).filter((key) => !cache.current.data[key]);
    loadGraphs(needed, (key, signal) => readJSON(query("graph", { key }), signal), (key, result) => {
      cache.current.data[key] = result;
      setData((previous) => ({ ...previous, [key]: result }));
    }, controller.signal);
    return () => controller.abort();
  }, [signature, revision, enabled]);
  return data;
}

/** Whether an element has come near the viewport (and so is worth reading for). */
function useNear(ref) {
  const [near, setNear] = useState(false);
  useEffect(() => {
    if (near || !ref.current) return undefined;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setNear(true);
    }, { rootMargin: "600px 0px" });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [near, ref]);
  return near;
}

function Chip({ chip }) {
  return <span className={`tg-chip ${chip.tone}`} title={chip.title}>{chip.text}</span>;
}

function Gutter({ row, width }) {
  const cls = (color) => (color === "rail" ? "tg-rail" : `tg-c${color}`);
  const x = laneX(row.lane);
  const dash = ["gap", "ghost"].includes(row.kind) ? "3 3" : undefined;
  const shapes = {
    commit: <circle className={`${cls(row.color)} tg-dot`} cx={x} cy={Y} r="5" />,
    fail: <circle className="tg-dot-bad" cx={x} cy={Y} r="5.5" />,
    gap: <circle className={`${cls(row.color)} tg-hollow`} cx={x} cy={Y} r="5" strokeDasharray="2 2" />,
    tip: <rect className={`${cls(row.color)} tg-hollow`} x={x - 5} y={Y - 5} width="10" height="10" />,
    head: <circle className={`${cls(row.color)} tg-hollow`} cx={x} cy={Y} r="3.5" />,
    fork: <circle className={`${cls(row.color)} tg-hollow`} cx={x} cy={Y} r="3.5" />,
    ghost: <circle className="tg-ghost tg-hollow" cx={x} cy={Y} r="5" strokeDasharray="2 2" />,
    root: <><circle className="tg-rail tg-hollow" cx={x} cy={Y} r="6" /><circle className="tg-rail tg-dot" cx={x} cy={Y} r="2" /></>,
    group: <><circle className={`${cls(row.color)} tg-hollow`} cx={x + 3} cy={Y - 3} r="4.5" /><circle className={`${cls(row.color)} tg-dot`} cx={x} cy={Y} r="4.5" /></>,
    member: <circle className={`${cls(row.color)} tg-hollow`} cx={x} cy={Y} r="3" />,
  };
  return (
    <div className="tg-gutter" aria-hidden="true">
      <svg width={width} height="100%">
        {row.through.map(([lane, color]) => (
          <line key={lane} className={cls(color)} x1={laneX(lane)} x2={laneX(lane)} y1="-1" y2="100%" />
        ))}
        {row.top && <line className={cls(row.color)} x1={x} x2={x} y1="-1" y2={Y} strokeDasharray={dash} />}
        {row.bottom && <line className={cls(row.color)} x1={x} x2={x} y1={Y} y2="100%" strokeDasharray={dash} />}
        {row.elbow != null && (
          <path className={cls(row.color)} d={`M${laneX(row.elbow)} 0 Q${laneX(row.elbow)} ${Y} ${x} ${Y}`} fill="none" />
        )}
        {shapes[row.kind]}
      </svg>
    </div>
  );
}

function Label({ run, differs, ordinal }) {
  const parts = runLabel(run, differs);
  return (
    <span className="tg-who">
      {parts.map((part, index) => (
        <React.Fragment key={part.field}>
          {index > 0 && " · "}
          {part.bold ? <b>{part.text}</b> : part.text}
        </React.Fragment>
      ))}
      {ordinal ? <b> #{ordinal}</b> : null}
    </span>
  );
}

function RunLinks({ run, selected, onToggle }) {
  const name = run.run_id || run.key;
  return (
    <>
      <a className="tg-rid" title={run.key} href={href(runRoute(run.key, { tab: "trajectory" }))}>{name}</a>
      <a className="tg-link" href={href(runRoute(run.key, { tab: "trajectory" }))} aria-label={`打开 ${name} 的完整轨迹`}>完整轨迹 ↗</a>
      {!run.context && (
        <label className="tg-cmp">
          <input type="checkbox" checked={selected.includes(run.key)} disabled={selected.length >= 8 && !selected.includes(run.key)}
            onChange={() => onToggle(run.key)} aria-label={`加入对比 ${name}`} />
          对比
        </label>
      )}
    </>
  );
}

function forkTitle(run) {
  const k = run.branch.checkpoint_step;
  return k ? `分叉 · 第 ${k} 步起重做` : "分叉 · 从第 0 步起重做";
}

function strategyText(run) {
  const strategy = run.strategy || {};
  const verify = strategy.verify === "warn" ? "不一致时记下继续" : strategy.verify === "strict" ? "不一致时停止" : null;
  const budget = strategy.continue_steps != null ? `最多再走 ${strategy.continue_steps} 步` : null;
  return [verify, budget, run.branch?.task_variant ? "换了任务" : null].filter(Boolean).join(" · ");
}

function tipChips(run) {
  const chips = [{ tone: "neutral", text: endLabel(run.agent_done) }];
  chips.push(...(run.verdicts || []).map(verdictChip));
  if (!(run.verdicts || []).length) chips.push({ tone: "neutral", text: "没有判定" });
  if (run.trace_status === "writing")
    chips.push({ tone: "warn", text: "记录未关闭（不代表仍在运行）" });
  else if (run.trace_status && run.trace_status !== "complete")
    chips.push({ tone: "warn", text: `记录${statusLabel(run.trace_status)} · 完整到第 ${run.complete_through ?? "?"} 步` });
  return chips;
}

function RowBody({ row, graph, ctx }) {
  const run = row.key ? graph.runs[row.key] : null;
  const pressed = (step, kind = "step") =>
    ctx.active?.key === row.key && ctx.active?.step === step && ctx.active?.kind === kind;
  const open = (step, kind) => ctx.onOpen({ key: row.key, step, kind });
  switch (row.kind) {
    case "root":
      return <div className="tg-body"><span className="tg-root-text">任务开始 · 每次运行都从这里出发</span></div>;
    case "head":
      return (
        <div className={`tg-body ${ctx.dimmed(run) ? "dim" : ""}`}>
          <span className="tg-ref live">{run.execution_kind === "replay" ? "回放" : "运行"}</span>
          <Label run={run} differs={graph.differs} ordinal={row.line.ordinal} />
          {run.context && <span className="tg-note">筛选外的源运行，只为显示分叉从哪里出发</span>}
          <RunLinks run={run} selected={ctx.selected} onToggle={ctx.onToggle} />
        </div>
      );
    case "fork":
    case "ghost": {
      const k = run.branch.checkpoint_step;
      const walked = row.line.parent && row.line.parent.key !== run.source_key;
      return (
        <div className={`tg-body ${ctx.dimmed(run) ? "dim" : ""}`}>
          <button type="button" className="tg-hit" data-nav aria-pressed={pressed(k, "fork")} onClick={() => open(k, "fork")}
            title="打开检查点：原始记录 ↔ 实际回放">
            <span className="tg-ref fork">{forkTitle(run)}</span>
            {restorationChips(run.restoration).map((chip) => <Chip key={chip.text} chip={chip} />)}
          </button>
          {row.line.detached && <span className="tg-issue">{row.line.detached}，不画连线</span>}
          {walked && <span className="tg-note">源是 {graph.runs[run.source_key]?.run_id || run.source_key}；第 {k - 1} 步在它的恢复前缀里，由上面这条运行决定</span>}
          <span className="tg-who-line"><Label run={run} differs={[]} />{strategyText(run) && <span className="tg-who"> · {strategyText(run)}</span>}</span>
          <RunLinks run={run} selected={ctx.selected} onToggle={ctx.onToggle} />
        </div>
      );
    }
    case "commit":
    case "fail": {
      const step = row.data;
      const status = commitStatus(step);
      const message = step.actions.length ? step.actions.map((action) => action.text).join(" · ") : step.error ? `出错：${step.error}` : "（没有动作）";
      return (
        <button type="button" className="tg-body tg-commit" data-nav aria-pressed={pressed(row.step)} onClick={() => open(row.step, "step")}>
          <span className="tg-sn">{row.step}</span>
          <span className="tg-msg" title={message}>{message}</span>
          <span className="tg-meta"><span className={`tg-st ${status.tone}`}>{status.text}</span> · {callText(step.calls)}</span>
        </button>
      );
    }
    case "gap":
      return (
        <div className="tg-body tg-commit">
          <span className="tg-sn">{row.step}</span>
          <span className="tg-msg muted">第 {row.step} 步没有提交（记录缺口）</span>
        </div>
      );
    case "status":
      return <div className={`tg-body ${row.error ? "tg-issue" : "tg-loading"}`}>{row.error || "读取已提交的步骤…"}</div>;
    case "tip": {
      const terminal = row.terminal;
      const chips = tipChips(run);
      const inner = (
        <>
          {terminal ? <><span className="tg-sn">{terminal.step}</span><span className="tg-tag">终止观测</span></> : <span className="tg-tag">{row.loaded ? "没有终止观测" : "终点"}</span>}
          {chips.map((chip) => <Chip key={chip.text} chip={chip} />)}
          {run.answer ? <span className="tg-ans">回答：{run.answer}</span> : null}
        </>
      );
      return terminal ? (
        <button type="button" className="tg-body tg-tip" data-nav aria-pressed={pressed(terminal.step, "tip")} onClick={() => open(terminal.step, "tip")}>{inner}</button>
      ) : <div className="tg-body tg-tip">{inner}</div>;
    }
    case "group": {
      const k = row.step + 1;
      return (
        <button type="button" className="tg-body" data-nav aria-expanded={row.opened} onClick={() => ctx.onFork(row.id)}>
          <span className="tg-ref fork">{row.count} 个分叉 · 第 {k} 步起重做</span>
          {forkChips(row.fork?.summary).map((chip) => <Chip key={chip.text} chip={chip} />)}
          <span className="tg-link">{row.opened ? "收起 ↑" : "展开 ↓"}</span>
        </button>
      );
    }
    case "member": {
      const chips = [{ tone: "neutral", text: endLabel(run.agent_done) }, ...(run.verdicts || []).map(verdictChip)];
      return (
        <button type="button" className="tg-body tg-member" data-nav onClick={() => ctx.onExpand(row.key)}>
          <span className="tg-ref fork small">b{run.branch.branch_index ?? ""}</span>
          <span className="tg-who">{run.run_id}</span>
          {restorationChips(run.restoration).slice(-1).map((chip) => <Chip key={chip.text} chip={chip} />)}
          {chips.map((chip) => <Chip key={chip.text} chip={chip} />)}
          <span className="tg-link">读它的提交 ↓</span>
        </button>
      );
    }
    case "more":
      return (
        <button type="button" className="tg-body tg-member" onClick={() => ctx.onMore(row.id)}>
          <span className="tg-who">还有 {row.remaining} 个分叉 · 再列出 {Math.min(row.remaining, 20)} 个 ↓</span>
        </button>
      );
    default:
      return null;
  }
}

/** ↑ and ↓ move between the commits and fork points of one task; Enter opens one. */
function moveFocus(event) {
  if (!["ArrowDown", "ArrowUp"].includes(event.key) || !event.target.matches?.("[data-nav]")) return;
  const all = [...event.currentTarget.querySelectorAll("[data-nav]")];
  const next = all[all.indexOf(event.target) + (event.key === "ArrowDown" ? 1 : -1)];
  if (next) {
    event.preventDefault();
    next.focus();
  }
}

function TaskDiagram({ graph, revision, near, ctx }) {
  const [open, setOpen] = useState(() => new Set());
  const [expanded, setExpanded] = useState(() => new Set());
  const [shown, setShown] = useState({});
  const keys = useMemo(() => visibleLines(graph, { open, expanded }), [graph, open, expanded]);
  const graphs = useGraphs(keys, revision, near);
  const layout = layoutTask(graph, { graphs, open, shown, expanded });
  const width = X0 * 2 + (layout.lanes - 1) * LANE_W;
  const toggle = (setter, value) => setter((old) => {
    const next = new Set(old);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  });
  const rowCtx = {
    ...ctx,
    onFork: (id) => toggle(setOpen, id),
    onExpand: (key) => toggle(setExpanded, key),
    onMore: (id) => setShown((old) => ({ ...old, [id]: (old[id] || 20) + 20 })),
  };
  return (
    <div className="tg-graph" role="list" aria-label={`${graph.task.task_id || "本任务"} 的运行图`} style={{ "--gw": `${width}px` }} onKeyDown={moveFocus}>
      {layout.rows.map((row, index) => (
        <div className={`tg-row ${row.kind}`} role="listitem" key={`${row.kind}-${row.key ?? row.id ?? ""}-${row.step ?? ""}-${index}`}>
          <Gutter row={row} width={width} />
          <RowBody row={row} graph={graph} ctx={rowCtx} />
        </div>
      ))}
    </div>
  );
}

function TaskSection({ group, graph, error, folded, onFold, revision, ctx }) {
  const ref = useRef(null);
  const near = useNear(ref);
  const summary = taskSummary(group);
  const identity = `${group.benchmark || "未记录基准测试"}${group.subset ? ` / ${group.subset}` : ""} · ${group.taskId ? `任务 ${group.taskId}` : "任务身份不完整，单独列出"}`;
  return (
    <section className={`tg-task ${folded ? "folded" : ""}`} ref={ref} aria-label={group.title}>
      <header className="tg-task-head">
        <button type="button" className="tg-fold" aria-expanded={!folded} onClick={onFold} aria-label={folded ? "展开这个任务的步骤" : "收起这个任务的步骤"}>
          <span aria-hidden="true">{folded ? "+" : "−"}</span>
        </button>
        <div className="tg-task-copy">
          <div className="tg-task-id">{identity}</div>
          <h3>{group.title}</h3>
          <div className="tg-harness">
            <span><b>{summary.runs}</b> 次运行</span>
            <span><b>{summary.branches}</b> 个分叉</span>
            <span className="tg-harness-who">{summary.agents.join("、") || "智能体未记录"} · {summary.models.join("、") || "模型未记录"} · 配置 {summary.configs.join("、") || "未记录"}</span>
            <span className="tg-tally">
              {summary.verdicts.map((chip) => <span key={chip.text} className={`tg-chip ${chip.tone}`} title={chip.title}>{chip.text} ×{chip.count}</span>)}
              {summary.unjudged > 0 && <span className="tg-chip neutral">{summary.unjudged} 次没有判定</span>}
            </span>
          </div>
        </div>
      </header>
      {!folded && (error ? <p className="tg-issue tg-task-error">{error}</p> : graph ? (
        <TaskDiagram graph={graph} revision={revision} near={near} ctx={ctx} />
      ) : <p className="tg-loading tg-task-error">读取任务的运行图…</p>)}
    </section>
  );
}

function DetailPanel({ active, graph, onClose, renderStep }) {
  const run = active && graph?.runs[active.key];
  return (
    <aside className={`tg-detail ${active ? "open" : ""}`} aria-label="提交详情">
      <div className="tg-detail-bar">
        <span className="tg-eyebrow">{active ? `${run?.branch ? "分叉" : "运行"} · ${run?.run_id || active.key}` : "提交详情"}</span>
        {active && <a className="tg-link" href={href(runRoute(active.key, { tab: "trajectory", step: active.step }))}>在运行页打开 ↗</a>}
        <button type="button" className="tg-close" onClick={onClose} aria-label="关闭详情">×</button>
      </div>
      <div className="tg-detail-body">
        {!active ? (
          <p className="tg-empty">点一个提交，这里打开那一步的观测、动作、模型调用和归档证据；点分叉点，打开检查点的“原始记录 ↔ 实际回放”。↑ ↓ 在提交之间移动，Enter 打开。</p>
        ) : (
          <>
            {active.kind === "fork" && run?.branch && (
              <div className="tg-fork-note">
                <strong>{forkTitle(run)}</strong>
                <span>源 {graph.runs[run.source_key]?.run_id || run.source_key || "不可用"} · {strategyText(run)}</span>
                <span className="tg-chips">{restorationChips(run.restoration).map((chip) => <Chip key={chip.text} chip={chip} />)}</span>
              </div>
            )}
            {renderStep(active.key, active.step)}
          </>
        )}
      </div>
    </aside>
  );
}

export function TaskGraphs({ visible, revision, selected, onToggle, route, renderStep }) {
  const groups = useMemo(() => taskGroups(visible), [visible]);
  const visibleKeys = useMemo(() => new Set(visible.map((row) => row.key)), [visible]);
  const [folded, setFolded] = useState(() => readStored(storage(), FOLD_KEY, false));
  const [overrides, setOverrides] = useState({});
  const [limit, setLimit] = useState(TASKS_PER_PAGE);
  const page = groups.slice(0, limit);
  const isFolded = (key) => overrides[key] ?? folded;
  const wanted = page.filter((group) => !isFolded(group.key)).map((group) => group.key);
  const [graphs, setGraphs] = useState({ revision, data: {}, errors: {} });
  const signature = JSON.stringify(wanted);
  useEffect(() => {
    const keys = JSON.parse(signature).filter((key) => graphs.revision !== revision || !graphs.data[key]);
    if (!keys.length && graphs.revision === revision) return undefined;
    const controller = new AbortController();
    const params = new URLSearchParams(keys.map((key) => ["task", key]));
    readJSON(`/api/task-graph?${params}`, controller.signal)
      .then((list) => setGraphs((old) => ({
        revision,
        data: { ...(old.revision === revision ? old.data : {}), ...Object.fromEntries(list.map((graph) => [graph.task.key, graph])) },
        errors: {},
      })))
      .catch((error) => {
        if (error.name !== "AbortError")
          setGraphs((old) => ({ ...old, revision, errors: Object.fromEntries(keys.map((key) => [key, error.message])) }));
      });
    return () => controller.abort();
  }, [signature, revision]);
  const active = route.open ? { key: route.open, step: Number(route.step), kind: route.at || "step" } : null;
  const activeGraph = active && Object.values(graphs.data).find((graph) => graph.runs[active.key]);
  const setActive = (next) => {
    const { open: _o, step: _s, at: _a, ...rest } = route;
    replaceRoute(next ? { ...rest, view: "runs", open: next.key, step: next.step, at: next.kind } : { ...rest, view: "runs" });
  };
  useEffect(() => {
    if (!route.open) return undefined;
    const close = (event) => { if (event.key === "Escape") setActive(null); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [route]);
  const foldAll = () => {
    const next = !folded;
    setFolded(next);
    setOverrides({});
    writeStored(storage(), FOLD_KEY, next);
  };
  const ctx = {
    selected,
    onToggle,
    active,
    onOpen: setActive,
    dimmed: (run) => !run.context && !visibleKeys.has(run.key),
  };
  return (
    <div className={`tg-layout ${active ? "has-detail" : ""}`}>
      <div className="tg-tasks">
        <div className="tg-intro">
          <p>{number(groups.length)} 个任务 · 每个策略步是一个提交，分叉从源运行的上一个提交离开，同一步的两个决定并排。</p>
          <button type="button" className="tg-fold-all" aria-pressed={folded} onClick={foldAll}>
            {folded ? "展开全部步骤" : "收起全部步骤"}
          </button>
        </div>
        {!folded && (
          <ul className="tg-legend" aria-label="图例">
            <li><svg width="16" height="16" aria-hidden="true"><circle className="tg-c1 tg-dot" cx="8" cy="8" r="5" /></svg>策略步 = 提交</li>
            <li><svg width="16" height="16" aria-hidden="true"><circle className="tg-dot-bad" cx="8" cy="8" r="5" /></svg>有失败动作</li>
            <li><svg width="22" height="16" aria-hidden="true"><path className="tg-c3" d="M3 0 Q3 8 18 8" fill="none" /><circle className="tg-c3 tg-hollow" cx="18" cy="8" r="3.5" /></svg>分叉点：带恢复结果</li>
            <li><svg width="16" height="16" aria-hidden="true"><rect className="tg-c1 tg-hollow" x="3" y="3" width="10" height="10" /></svg>终点：结束原因与判定</li>
            <li><svg width="16" height="16" aria-hidden="true"><circle className="tg-c1 tg-hollow" cx="8" cy="8" r="5" strokeDasharray="2 2" /></svg>记录缺口</li>
          </ul>
        )}
        {page.map((group) => (
          <TaskSection
            key={group.key}
            group={group}
            graph={graphs.data[group.key]}
            error={graphs.errors[group.key]}
            folded={isFolded(group.key)}
            onFold={() => setOverrides((old) => ({ ...old, [group.key]: !isFolded(group.key) }))}
            revision={revision}
            ctx={ctx}
          />
        ))}
        {limit < groups.length && (
          <button className="button tg-more-tasks" onClick={() => setLimit((old) => old + TASKS_PER_PAGE)}>
            再显示 {Math.min(TASKS_PER_PAGE, groups.length - limit)} 个任务
          </button>
        )}
      </div>
      <DetailPanel active={active} graph={activeGraph} onClose={() => setActive(null)} renderStep={renderStep} />
    </div>
  );
}
