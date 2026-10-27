import React, { useEffect, useMemo, useRef, useState } from "react";
import { Badge, number, query, runRoute, statusLabel } from "./common.jsx";
import { graphColumns, graphEdges, graphSteps, loadGraphs, taskGroups, taskLanes } from "./taskGraph.js";
import { actionRoute } from "./routing.js";
import "./taskGraph.css";

const href = (route) => `#${new URLSearchParams(route)}`;
const ROW_HEIGHT = 88;
const STEP_WIDTH = 42;
const TOP = 50;
const COLORS = ["#cc6532", "#4c79a3", "#667e52", "#a56b89", "#a28a35", "#497f7b"];
const color = (key) => COLORS[[...key].reduce((n, c) => n + c.codePointAt(0), 0) % COLORS.length];
const actionStatus = { succeeded: "已执行", failed: "执行失败", skipped: "没有执行", no_result: "没有返回结果" };

async function readGraph(key, signal) {
  const response = await fetch(query("graph", { key }), { signal });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "轨迹读取失败");
  return data;
}

function useGraphs(keys, revision) {
  const [data, setData] = useState({});
  const cache = useRef({ revision, data: {} });
  const signature = JSON.stringify(keys);
  useEffect(() => {
    const controller = new AbortController();
    if (cache.current.revision !== revision) cache.current = { revision, data: {} };
    setData({ ...cache.current.data });
    const needed = JSON.parse(signature).filter((key) => !cache.current.data[key]);
    loadGraphs(needed, readGraph, (key, result) => {
      cache.current.data[key] = result;
      setData((previous) => ({ ...previous, [key]: result }));
    }, controller.signal);
    return () => controller.abort();
  }, [signature, revision]);
  return data;
}

function StepDot({ row, step, x, y, trajectory }) {
  const failed = Boolean(step.error || step.actions.some((action) => action.status === "failed"));
  const title = `第 ${step.step} 步 · ${statusLabel(step.origin)}${failed ? " · 有错误" : ""}`;
  return (
    <div className={`graph-node ${step.origin} ${failed ? "failed" : ""}`} style={{ left: x, top: y }}>
      <a className="graph-dot" href={href(runRoute(row.key, { tab: "trajectory", step: step.step }))} aria-label={`${row.run_id || row.key}，${title}`}>
        <span aria-hidden="true">{failed ? "!" : step.origin === "terminal" ? "□" : ""}</span>
      </a>
      <span className="graph-step-number" aria-hidden="true">{step.step}</span>
      <div className="graph-node-popup">
        <strong>{title}</strong>
        {!trajectory ? <p>没有记录动作内容</p> : step.actions.length ? (
          <ol>
            {step.actions.map((action) => (
              <li key={action.index}>
                <a href={href(actionRoute(row.key, step.step, action.index))}>
                  {action.text}<small>{actionStatus[action.status] || action.status} · 打开动作 {action.index + 1}</small>
                </a>
              </li>
            ))}
          </ol>
        ) : <p>{step.origin === "terminal" ? "终止观测，没有新决定" : "这一步没有动作"}</p>}
        <a className="text-button" href={href(runRoute(row.key, { tab: "trajectory", step: step.step }))}>查看这一步全部证据 →</a>
      </div>
    </div>
  );
}

function RunCaption({ lane, data, selected, onToggle }) {
  const row = lane.row;
  const restored = data?.steps?.filter((step) => step.origin === "restored") || [];
  const lastRestore = restored.at(-1);
  return (
    <div className={`graph-run-caption ${lane.context ? "context" : ""}`} style={{ "--lane": color(row.key) }}>
      <div className="graph-run-heading">
        {!lane.context && (
          <input type="checkbox" checked={selected.includes(row.key)} disabled={selected.length >= 8 && !selected.includes(row.key)}
            onChange={() => onToggle(row.key)} aria-label={`加入对比 ${row.run_id || row.key}`} />
        )}
        <a className="graph-run-title" title={row.key} href={href(runRoute(row.key, { tab: "trajectory" }))}>{row.run_id || row.key.split("/").at(-1)}</a>
        <a className="graph-whole" href={href(runRoute(row.key, { tab: "trajectory" }))} aria-label={`打开 ${row.run_id || row.key} 的完整轨迹`}>完整轨迹 ↗</a>
      </div>
      <div className="graph-run-meta">
        {row.execution_kind ? <Badge value={row.execution_kind} /> : <span>源运行</span>}
        {row.branch && <span>从第 {row.branch.checkpoint_step} 步分叉{row.branch.task_variant ? " · 换任务" : ""}</span>}
        {row.agent_done && <Badge value={row.agent_done} />}
      </div>
      <div className="graph-run-note">
        {lane.context ? <span>源运行在筛选外 · 保留来源位置</span> : <span title={row.model}>{row.agent_id || ""}{row.model ? ` · ${row.model}` : ""}</span>}
        {lastRestore && <a href={href(runRoute(row.key, { tab: "trajectory", step: lastRestore.step }))}>恢复前缀 {restored.length} 步 ↗</a>}
      </div>
    </div>
  );
}

function TaskDiagram({ group, allRows, revision, selected, onToggle }) {
  const [labelWidth, setLabelWidth] = useState(() => window.matchMedia("(max-width: 650px)").matches ? 180 : 280);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 650px)");
    const resized = () => setLabelWidth(media.matches ? 180 : 280);
    media.addEventListener("change", resized);
    return () => media.removeEventListener("change", resized);
  }, []);
  const allLanes = useMemo(() => taskLanes(group.runs, allRows), [group.runs, allRows]);
  const [limit, setLimit] = useState(9);
  const lanes = allLanes.slice(0, limit);
  const data = useGraphs(lanes.map((lane) => lane.row.key), revision);
  const columns = graphColumns(lanes, data);
  const positions = new Map(columns.map((step, index) => [step, labelWidth + 28 + index * STEP_WIDTH]));
  const width = Math.max(800, labelWidth + 68 + columns.length * STEP_WIDTH);
  const height = TOP + ROW_HEIGHT * lanes.length + 146;
  const edges = graphEdges(lanes, data);
  return (
    <>
      <div className="graph-help">
        <span><i className="graph-legend policy" /> 策略步</span>
        <span><i className="graph-legend terminal" /> 终止观测</span>
        <span><i className="graph-legend restored" /> 恢复步</span>
        <span>点圆点打开步骤 · 悬停或聚焦后可选动作 · 点运行名打开完整轨迹</span>
        <span>分叉从检查点的原动作之前开始</span>
      </div>
      <div className="task-graph-scroll" tabIndex="0" aria-label={`${group.taskId || "本任务"} 的运行分支图，可横向滚动`}>
        <div className="task-graph-canvas" style={{ width, height, "--graph-label-width": `${labelWidth}px` }}>
          <div className="graph-axis-title">运行 / 来源</div>
          <div className="graph-axis-steps" style={{ left: labelWidth }}>步骤编号 · 分叉线连接实际记录的检查点</div>
          <svg className="graph-lines" width={width} height={height} aria-hidden="true">
            {edges.map((edge) => {
              const x = positions.get(edge.step);
              const y1 = TOP + edge.from * ROW_HEIGHT;
              const y2 = TOP + edge.to * ROW_HEIGHT;
              return <path key={`${edge.from}-${edge.to}`} d={`M ${x} ${y1} L ${x - 14} ${y1 + 20} L ${x - 14} ${y2 - 16} L ${x} ${y2}`} stroke={color(lanes[edge.to].row.key)} />;
            })}
            {lanes.flatMap((lane, index) => {
              const steps = graphSteps(lane.row, data[lane.row.key]?.data);
              return steps.slice(1).map((step, i) => (
                <line key={`${lane.row.key}-${step.step}`} x1={positions.get(steps[i].step)} x2={positions.get(step.step)}
                  y1={TOP + index * ROW_HEIGHT} y2={TOP + index * ROW_HEIGHT} stroke={color(lane.row.key)}
                  strokeDasharray={step.step === steps[i].step + 1 ? undefined : "3 4"} />
              ));
            })}
          </svg>
          {lanes.map((lane, index) => {
            const state = data[lane.row.key];
            const steps = graphSteps(lane.row, state?.data);
            const checkpoint = lane.row.branch?.checkpoint_step;
            const sourceData = lane.parent ? data[lane.parent]?.data : null;
            const missingCheckpoint = sourceData && !sourceData.steps.some((step) => step.step === checkpoint);
            const sourceLane = lanes.find((candidate) => candidate.row.key === lane.parent);
            const foldedCheckpoint = sourceData && !missingCheckpoint && sourceLane
              && !graphSteps(sourceLane.row, sourceData).some((step) => step.step === checkpoint);
            const issue = lane.issue || (missingCheckpoint ? `源运行没有已提交的第 ${checkpoint} 步，不绘制连接`
              : foldedCheckpoint ? `源检查点第 ${checkpoint} 步位于收起的恢复前缀，打开源运行查看` : null);
            const y = TOP + index * ROW_HEIGHT;
            return (
              <React.Fragment key={lane.row.key}>
                <div className="graph-row-rule" style={{ top: y + ROW_HEIGHT / 2 - 6 }} />
                <div className="graph-caption-slot" style={{ top: y - 26 }}>
                  <RunCaption lane={lane} data={state?.data} selected={selected} onToggle={onToggle} />
                </div>
                {steps.map((step) => (
                  <div key={step.step} style={{ "--lane": color(lane.row.key) }}>
                    <StepDot row={lane.row} step={step} x={positions.get(step.step)} y={y} trajectory={state.data.recording_trajectory} />
                  </div>
                ))}
                {(!state || state.error || !steps.length) && (
                  <span className={`graph-row-message ${state?.error ? "error" : ""}`} title={state?.error || undefined} style={{ left: labelWidth + 20, top: y - 9 }}>
                    {!state ? "读取已提交的步骤…" : state.error || (lane.row.branch ? "尚无新决定；可打开恢复前缀查看停在哪里" : "没有已提交的步")}
                  </span>
                )}
                {issue && <span className="graph-source-issue" style={{ left: labelWidth + 20, top: y + 29 }}>{issue}</span>}
              </React.Fragment>
            );
          })}
        </div>
      </div>
      <div className="graph-footer">
        <span>显示 {lanes.length} / {allLanes.length} 次运行{allLanes.some((lane) => lane.context) ? "（含筛选外的源运行）" : ""} · 分叉恢复前缀已收起</span>
        {limit < allLanes.length && <button className="button" onClick={() => setLimit((old) => old + 8)}>再展开 {Math.min(8, allLanes.length - limit)} 次运行 ↓</button>}
        {limit > 9 && <button className="text-button" onClick={() => setLimit(9)}>收起新增运行</button>}
      </div>
    </>
  );
}

export function TaskGraphs({ rows, visible, revision, selected, onToggle }) {
  const groups = useMemo(() => taskGroups(rows, visible), [rows, visible]);
  const [open, setOpen] = useState(null);
  const [limit, setLimit] = useState(30);
  return (
    <div className="task-groups">
      <p className="task-groups-intro">{number(groups.length)} 个任务 · 同一任务的原运行和分叉放在一起，展开后查看步骤与来源。</p>
      {groups.slice(0, limit).map((group) => {
        const expanded = open === group.key;
        const forks = group.runs.filter((row) => row.branch).length;
        return (
          <section className={`task-group ${expanded ? "expanded" : ""}`} key={group.key}>
            <button className="task-group-heading" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : group.key)}>
              <span className="task-group-chevron" aria-hidden="true">{expanded ? "−" : "+"}</span>
              <span className="task-group-copy">
                <span className="task-group-identity">{group.benchmark || "未记录基准测试"}{group.subset ? ` / ${group.subset}` : ""} · {group.taskId ? `任务 ${group.taskId}` : "任务身份不完整，单独列出"}</span>
                <strong>{group.title}</strong>
              </span>
              <span className="task-group-count"><b>{group.runs.length - forks}</b> 原运行 <b>{forks}</b> 分叉</span>
            </button>
            {expanded && <TaskDiagram key={group.key} group={group} allRows={rows} revision={revision} selected={selected} onToggle={onToggle} />}
          </section>
        );
      })}
      {limit < groups.length && <button className="button task-groups-more" onClick={() => setLimit((old) => old + 30)}>再显示 {Math.min(30, groups.length - limit)} 个任务</button>}
    </div>
  );
}
