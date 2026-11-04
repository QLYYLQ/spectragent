/**
 * Layout of the task view: one task's runs drawn like `git log --graph`, read
 * top-down from step 0. Which commit a branch leaves, which runs have no edge
 * and why, and which fork points fold come from the read side
 * (spectragent/inspection/graph.py, served at /api/task-graph); this file only
 * turns that placement and each run's committed steps into rows.
 */

import { reasonLabel } from "./evidence.js";

/** Branch rows an opened fork point lists at a time. */
export const MEMBERS_PER_PAGE = 20;
/** Lane colours cycle through this many CSS variables (--lane-0 …). */
export const LANE_COLORS = 6;

const DONE = { done: "已完成", max_steps: "步数用完", error: "出错", timeout: "超时", cancelled: "已取消", no_result: "没有结果" };
const STATUS = {
  succeeded: ["ok", "✓ 成功"],
  failed: ["bad", "✗ 失败"],
  skipped: ["muted", "– 没有执行"],
  no_result: ["warn", "? 没有返回结果"],
};

export const endLabel = (value) => DONE[value] || value || "没有结果";
export const callText = (calls) => (calls == null ? "未统计调用" : `${calls} 次调用`);

/**
 * The task list: rows grouped by the task key the read side gave each run,
 * in list order, each with the header the read side wrote for its task.
 */
export function taskGroups(rows) {
  const groups = new Map();
  for (const row of rows) {
    if (!row.task_key) continue;
    if (!groups.has(row.task_key)) groups.set(row.task_key, { key: row.task_key, header: row.task_header, runs: [] });
    groups.get(row.task_key).runs.push(row);
  }
  return [...groups.values()];
}

const unique = (values) => [...new Set(values.filter(Boolean))];

/** One verdict as a chip: its label, else its score (nothing more is claimed), else not judged. */
export function verdictChip(verdict) {
  const who = verdict.verifier_id;
  if (verdict.status !== "judged") return { tone: "neutral", text: `${who} 没判`, title: verdict.slug };
  if (verdict.label === 1 || verdict.label === 0)
    return { tone: verdict.label ? "good" : "bad", text: `${who} ${verdict.label ? "成功" : "失败"}`, title: verdict.slug };
  const score = Number.isFinite(verdict.score) ? `分数 ${verdict.score}` : "判了但没有分数";
  return { tone: "neutral", text: `${who} ${score}`, title: verdict.slug };
}

/** What a folded task says: who ran it, how many runs and branches, and each verdict's outcomes. */
export function taskSummary(group) {
  const branches = group.runs.filter((row) => row.branch).length;
  const tally = new Map();
  for (const row of group.runs)
    for (const verdict of row.verdicts || []) {
      const chip = verdictChip(verdict);
      const entry = tally.get(chip.text) || { ...chip, count: 0 };
      entry.count += 1;
      tally.set(chip.text, entry);
    }
  return {
    agents: unique(group.runs.map((row) => row.agent_id)),
    models: unique(group.runs.map((row) => row.model)),
    configs: unique(group.runs.map((row) => row.config_hash?.slice(0, 8))),
    runs: group.runs.length - branches,
    branches,
    unjudged: group.runs.filter((row) => !(row.verdicts || []).length).length,
    verdicts: [...tally.values()],
  };
}

/** The fork head's two chips: how the restored prefix went, and the checkpoint check. */
export function restorationChips(restoration) {
  if (!restoration) return [];
  const prefix = restoration.prefix || {};
  const restored = restoration.restored || 0;
  const counted = Object.entries(prefix).filter(([, count]) => count);
  const chips = [];
  if (!restored) chips.push({ tone: "neutral", text: "没有恢复前缀" });
  else if (counted.length === 1 && counted[0][0] === "ok") chips.push({ tone: "good", text: `恢复 ${restored} 步 全部 ok` });
  else
    chips.push({
      tone: prefix.diverged ? "bad" : "warn",
      text: `恢复 ${counted.map(([verdict, count]) => `${count} ${verdict}`).join(" · ") || `${restored} 步未检查`}`,
    });
  const check = restoration.checkpoint;
  if (check) {
    const why = (check.reasons || []).map(reasonLabel).join("、");
    chips.push({
      tone: { ok: "good", warn: "warn", diverged: "bad" }[check.verdict] || "neutral",
      text: `检查点 ${check.verdict}${why ? `：${why}` : ""}`,
    });
  } else chips.push({ tone: "bad", text: "检查点没有校验：恢复在它之前停下" });
  return chips;
}

/** A folded fork point's counts: restoration results, end reasons and verdict outcomes. */
export function forkChips(summary = {}) {
  const tone = { ok: "good", warn: "warn", diverged: "bad", unchecked: "neutral" };
  const restoration = Object.entries(summary.restoration || {}).map(([verdict, count]) => ({
    tone: tone[verdict] || "neutral",
    text: verdict === "unchecked" ? `${count} 个未检查` : `恢复 ${count} ${verdict}`,
  }));
  const ended = Object.entries(summary.ended || {});
  const verdicts = (summary.verdicts || []).map((entry) => ({
    tone: entry.outcome === "成功" ? "good" : entry.outcome === "失败" ? "bad" : "neutral",
    text: `${entry.outcome} ${entry.count}`,
    title: entry.slug,
  }));
  const ends = ended.map(([done, count]) => `${endLabel(done)} ${count}`).join(" · ");
  return [...restoration, ...(ends ? [{ tone: "neutral", text: ends }] : []), ...verdicts];
}

/** A commit's result: the first action that did not succeed decides it. */
export function commitStatus(step) {
  const worst = step.actions.find((action) => action.status !== "succeeded")?.status || "succeeded";
  const [tone, text] = STATUS[worst] || STATUS.no_result;
  return { tone, text, failed: Boolean(step.error) || step.actions.some((action) => action.status === "failed") };
}

/**
 * A line's commits, in step order: each policy step from its checkpoint on,
 * and each step its recording is missing (a gap). Restored steps and the
 * terminal observation are not commits; the terminal step ends the line.
 */
export function commitsOf(run, data) {
  const from = run.branch ? run.branch.checkpoint_step : 0;
  const steps = data?.steps || [];
  const policy = steps.filter((step) => step.origin === "policy" && step.step >= from);
  const terminal = steps.find((step) => step.origin === "terminal") || null;
  const last = Math.max(-1, ...steps.map((step) => step.step));
  const gaps = (run.missing_steps || []).filter((step) => step >= from && step < last);
  const commits = [
    ...policy.map((step) => ({ kind: commitStatus(step).failed ? "fail" : "commit", step: step.step, data: step })),
    ...gaps.map((step) => ({ kind: "gap", step })),
  ].sort((a, b) => a.step - b.step);
  return { commits, terminal };
}

const forkId = (key, step) => `${key}@${step}`;

/**
 * Every row of one task's graph, top-down. Each row names its lane, its
 * colour, what it draws in the gutter (a line in from above, a line on
 * below, an elbow from the lane it leaves) and the lanes passing through it.
 * The number of lanes is the nesting depth, not the run count.
 *
 * `graphs` holds each line's run graph by key ({data, error}; absent while
 * reading). In a folded task, `open` holds the opened fork points
 * ("parent@step"), `shown` how many branch rows each lists, and `expanded`
 * the branches opened from them.
 */
export function layoutTask(graph, { graphs = {}, open = new Set(), shown = {}, expanded = new Set() } = {}) {
  const lines = new Map(graph.lines.map((line) => [line.key, line]));
  const colour = new Map(graph.lines.map((line, index) => [line.key, index % LANE_COLORS]));
  const children = new Map();
  for (const line of graph.lines) {
    if (!line.parent) continue;
    const at = forkId(line.parent.key, line.parent.step);
    if (!children.has(at)) children.set(at, []);
    children.get(at).push(line.key);
  }
  const forks = new Map((graph.forks || []).map((fork) => [forkId(fork.parent, fork.step), fork]));
  const rows = [];
  const active = new Map();
  const emit = (row) =>
    rows.push({ top: false, bottom: false, elbow: null, ...row, through: [...active].filter(([lane]) => lane !== row.lane) });

  function branchesAt(key, step, lane) {
    const at = forkId(key, step);
    const keys = children.get(at) || [];
    if (!keys.length) return;
    if (!graph.collapse) {
      for (const child of keys) drawLine(lines.get(child), lane + 1, lane);
      return;
    }
    const opened = open.has(at);
    const color = colour.get(keys[0]);
    emit({ kind: "group", id: at, lane: lane + 1, color, elbow: lane, bottom: opened, fork: forks.get(at), count: keys.length, step, opened });
    if (!opened) return;
    active.set(lane + 1, color);
    const limit = shown[at] || MEMBERS_PER_PAGE;
    const listed = keys.slice(0, limit);
    listed.forEach((child, index) => {
      const more = index < listed.length - 1 || keys.length > limit;
      if (expanded.has(child)) drawLine(lines.get(child), lane + 1, null, { top: true, bottom: more });
      else emit({ kind: "member", key: child, lane: lane + 1, color: colour.get(child), top: true, bottom: more });
    });
    if (keys.length > limit) emit({ kind: "more", id: at, lane: lane + 1, color, top: true, remaining: keys.length - limit });
    active.delete(lane + 1);
  }

  function drawLine(line, lane, parentLane, { top = false, bottom = false } = {}) {
    const color = colour.get(line.key);
    const state = graphs[line.key];
    const run = graph.runs[line.key];
    const kind = line.detached ? "ghost" : run.branch ? "fork" : "head";
    emit({ kind, key: line.key, lane, color, top, bottom: true, elbow: line.detached ? null : parentLane, line });
    active.set(lane, color);
    branchesAt(line.key, -1, lane);
    const { commits, terminal } = commitsOf(run, state?.data);
    if (!state?.data) emit({ kind: "status", key: line.key, lane, color, top: true, bottom: true, error: state?.error || null });
    const drawn = new Set([-1]);
    for (const commit of commits) {
      emit({ ...commit, key: line.key, lane, color, top: true, bottom: true });
      drawn.add(commit.step);
      branchesAt(line.key, commit.step, lane);
    }
    // A fork point whose commit is not drawn (the run graph is still being
    // read or failed) keeps its branches, just before the tip.
    for (const at of children.keys()) {
      const split = at.lastIndexOf("@");
      const step = Number(at.slice(split + 1));
      if (at.slice(0, split) === line.key && !drawn.has(step)) branchesAt(line.key, step, lane);
    }
    emit({ kind: "tip", key: line.key, lane, color, top: true, bottom, terminal, loaded: Boolean(state?.data) });
    if (!bottom) active.delete(lane);
  }

  const tops = graph.lines.filter((line) => line.depth === 0 && !line.detached);
  emit({ kind: "root", lane: 0, color: "rail", bottom: tops.length > 0 });
  if (tops.length) active.set(0, "rail");
  tops.forEach((line, index) => {
    if (index === tops.length - 1) active.delete(0);
    drawLine(line, 1, 0);
  });
  for (const line of graph.lines.filter((line) => line.depth === 0 && line.detached)) drawLine(line, 1, null);
  const lanes = 1 + Math.max(0, ...rows.flatMap((row) => [row.lane, ...row.through.map(([lane]) => lane)]));
  return { rows, lanes };
}

/** The lines whose commits the graph shows: every line, except branches still inside a folded fork point. */
export function visibleLines(graph, { open = new Set(), expanded = new Set() } = {}) {
  if (!graph.collapse) return graph.lines.map((line) => line.key);
  return graph.lines
    .filter((line) => !line.parent || (open.has(forkId(line.parent.key, line.parent.step)) && expanded.has(line.key)))
    .map((line) => line.key);
}

/** The fields a run's label shows, marking the ones that differ among the task's own runs. */
export function runLabel(run, differs = []) {
  const time = run.created_at
    ? new Date(run.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    : null;
  return [
    { field: "agent_id", text: run.agent_id },
    { field: "model", text: run.model },
    { field: "config_hash", text: run.config_hash ? `配置 ${run.config_hash.slice(0, 8)}` : null },
    { field: "created_at", text: time },
  ]
    .filter((part) => part.text)
    .map((part) => ({ ...part, bold: differs.includes(part.field) }));
}

/** Aborts stop queued reads too; callers own both the queue and its lifetime. */
export async function loadGraphs(keys, read, onResult, signal, concurrency = 3) {
  let cursor = 0;
  const worker = async () => {
    while (!signal.aborted && cursor < keys.length) {
      const key = keys[cursor++];
      try {
        const data = await read(key, signal);
        if (!signal.aborted) onResult(key, { data, error: null });
      } catch (error) {
        if (!signal.aborted) onResult(key, { data: null, error: error.message });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, keys.length) }, worker));
}

/** A per-viewer choice; storage may be missing or refuse (a private window), and then it lasts the page. */
export function readStored(storage, key, fallback) {
  try {
    const value = storage?.getItem(key);
    return value == null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

export function writeStored(storage, key, value) {
  try {
    storage?.setItem(key, JSON.stringify(value));
  } catch {
    // A convenience only: without storage the choice lasts this page.
  }
}
