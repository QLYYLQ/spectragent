import assert from "node:assert/strict";
import test from "node:test";
import {
  MEMBERS_PER_PAGE,
  commitsOf,
  forkChips,
  layoutTask,
  loadGraphs,
  placementsToRead,
  readStored,
  restorationChips,
  runLabel,
  runSignature,
  taskGroups,
  taskSignature,
  taskSummary,
  verdictChip,
  visibleLines,
  writeStored,
} from "./taskGraph.js";

const TASK = '["web", "L1", "118"]';
const live = (key, patch = {}) => ({ key, task_key: TASK, benchmark: "web", subset: "L1", task_id: "118", task: "Find a video", verdicts: [], ...patch });
const fork = (key, checkpoint, patch = {}) => live(key, { task_id: `x/s${checkpoint}`, branch: { checkpoint_step: checkpoint, source_benchmark: "web", source_subset: "L1", source_task_id: "118" }, ...patch });
const line = (key, parent = null, depth = 0, patch = {}) => ({ key, parent, detached: null, depth, ...patch });
const steps = (...spec) => ({ data: { steps: spec.map(([step, origin = "policy"]) => ({ step, origin, actions: [], error: null, calls: 1 })) } });

/** The rows as [kind, key-or-id, step, lane] for readable assertions. */
const shape = (layout) => layout.rows.map((row) => [row.kind, row.key ?? row.id ?? null, row.step ?? null, row.lane]);

test("tasks group by the read side's task key and keep its header", () => {
  const header = { key: TASK, title: "Find a video", task_id: "118" };
  const rows = [fork("b", 2, { task_header: header }), live("a", { task_header: header }), live("c", { task_key: "other", task_header: { key: "other" } })];
  const [first, second] = taskGroups(rows);
  assert.deepEqual(first.runs.map((row) => row.key), ["b", "a"]);
  assert.equal(first.header, header);
  assert.equal(second.key, "other");
  assert.deepEqual(taskGroups([{ key: "x" }]), []);
});

test("a branch block sits after the commit it leaves, one lane deeper, and the source runs on", () => {
  const graph = {
    runs: { live: live("live"), b: fork("b", 2) },
    lines: [line("live"), line("b", { key: "live", step: 1 }, 1)],
    forks: [],
  };
  const layout = layoutTask(graph, { graphs: { live: steps([0], [1], [2], [3, "terminal"]), b: steps([0, "restored"], [1, "restored"], [2], [3, "terminal"]) } });
  assert.deepEqual(shape(layout), [
    ["root", null, null, 0],
    ["head", "live", null, 1],
    ["commit", "live", 0, 1],
    ["commit", "live", 1, 1],
    ["fork", "b", null, 2],
    ["commit", "b", 2, 2],
    ["tip", "b", null, 2],
    ["commit", "live", 2, 1],
    ["tip", "live", null, 1],
  ]);
  assert.equal(layout.lanes, 3);
  const forkRow = layout.rows[4];
  assert.equal(forkRow.elbow, 1);
  // the source's lane passes through the branch block
  assert.deepEqual(forkRow.through.map(([lane]) => lane), [1]);
  // restored steps are never drawn again
  assert.ok(!layout.rows.some((row) => row.key === "b" && row.step === 0));
});

test("a branch from the start leaves the head; lanes are nesting depth, not run count", () => {
  const graph = {
    runs: { live: live("live"), zero: fork("zero", 0), deep: fork("deep", 1) },
    lines: [line("live"), line("zero", { key: "live", step: -1 }, 1), line("deep", { key: "zero", step: 0 }, 2)],
    forks: [],
  };
  const layout = layoutTask(graph, { graphs: { live: steps([0]), zero: steps([0], [1]), deep: steps([0, "restored"], [1]) } });
  assert.deepEqual(shape(layout).slice(0, 5), [
    ["root", null, null, 0],
    ["head", "live", null, 1],
    ["fork", "zero", null, 2],
    ["commit", "zero", 0, 2],
    ["fork", "deep", null, 3],
  ]);
  assert.equal(layout.lanes, 4);
});

test("the root rail ends at the last top-level run, and runs without an edge come last as ghosts", () => {
  const graph = {
    runs: { a: live("a"), b: live("b"), lost: fork("lost", 3) },
    lines: [line("a"), line("b"), line("lost", null, 0, { detached: "根目录内找不到源运行" })],
    forks: [],
  };
  const layout = layoutTask(graph, { graphs: { a: steps([0]), b: steps([0]), lost: steps([3]) } });
  const heads = layout.rows.filter((row) => ["head", "ghost"].includes(row.kind));
  assert.deepEqual(heads.map((row) => [row.key, row.elbow, row.through.map(([lane]) => lane)]), [
    ["a", 0, [0]],
    ["b", 0, []],
    ["lost", null, []],
  ]);
});

test("gaps are drawn in step order and a line still being read says so", () => {
  const run = live("live", { missing_steps: [1] });
  assert.deepEqual(commitsOf(run, steps([0], [2], [3, "terminal"]).data).commits.map((c) => [c.kind, c.step]), [["commit", 0], ["gap", 1], ["commit", 2]]);
  const graph = { runs: { live: run }, lines: [line("live")], forks: [] };
  assert.deepEqual(shape(layoutTask(graph)).map((row) => row[0]), ["root", "head", "status", "tip"]);
  const failed = layoutTask(graph, { graphs: { live: { data: null, error: "boom" } } });
  assert.equal(failed.rows[2].error, "boom");
});

test("a failed action makes a red commit", () => {
  const data = { steps: [{ step: 0, origin: "policy", error: null, actions: [{ status: "failed" }] }] };
  assert.equal(commitsOf(live("x"), data).commits[0].kind, "fail");
});

test("a crowded task folds each fork point, lists its branches in pages and opens one at a time", () => {
  const keys = Array.from({ length: MEMBERS_PER_PAGE + 2 }, (_, i) => `b${i}`);
  const graph = {
    collapse: true,
    runs: Object.fromEntries([["live", live("live")], ...keys.map((key) => [key, fork(key, 1)])]),
    lines: [line("live"), ...keys.map((key) => line(key, { key: "live", step: 0 }, 1))],
    forks: [{ parent: "live", step: 0, branches: keys, summary: { restoration: { ok: 21, warn: 1 }, ended: { done: 22 }, verdicts: [] } }],
  };
  const graphs = { live: steps([0], [1]), b3: steps([0, "restored"], [1]) };
  const closed = layoutTask(graph, { graphs });
  assert.deepEqual(shape(closed).map((row) => row[0]), ["root", "head", "commit", "group", "commit", "tip"]);
  assert.equal(closed.rows[3].count, keys.length);
  assert.deepEqual(visibleLines(graph), ["live"]);
  const open = new Set(["live@0"]);
  const listed = layoutTask(graph, { graphs, open, expanded: new Set(["b3"]) });
  const kinds = listed.rows.map((row) => row.kind);
  assert.equal(kinds.filter((kind) => kind === "member").length, MEMBERS_PER_PAGE - 1);
  assert.deepEqual(listed.rows.filter((row) => row.key === "b3").map((row) => row.kind), ["fork", "commit", "tip"]);
  assert.equal(listed.rows.find((row) => row.kind === "more").remaining, 2);
  assert.deepEqual(visibleLines(graph, { open, expanded: new Set(["b3"]) }), ["live", "b3"]);
  const all = layoutTask(graph, { graphs, open, shown: { "live@0": 40 } });
  assert.ok(!all.rows.some((row) => row.kind === "more"));
});

test("fork chips say how restoration went and name the checkpoint's reasons", () => {
  assert.deepEqual(restorationChips({ restored: 2, prefix: { ok: 2, warn: 0, diverged: 0 }, checkpoint: { verdict: "ok", reasons: [] } }).map((c) => c.text), ["恢复 2 步 全部 ok", "检查点 ok"]);
  const warned = restorationChips({ restored: 3, prefix: { ok: 2, warn: 1, diverged: 0 }, checkpoint: { verdict: "warn", reasons: ["ui_tree_mismatch"] } });
  assert.deepEqual(warned.map((c) => [c.tone, c.text]), [["warn", "恢复 2 ok · 1 warn"], ["warn", "检查点 warn：界面树不一致"]]);
  const stopped = restorationChips({ restored: 3, prefix: { ok: 2, warn: 0, diverged: 1 }, checkpoint: null });
  assert.deepEqual(stopped.map((c) => c.tone), ["bad", "bad"]);
  assert.deepEqual(forkChips({ restoration: { ok: 11, warn: 1 }, ended: { done: 10, max_steps: 2 }, verdicts: [{ slug: "v", outcome: "成功", count: 8 }] }).map((c) => c.text),
    ["恢复 11 ok", "恢复 1 warn", "已完成 10 · 步数用完 2", "成功 8"]);
});

test("a verdict with only a score claims nothing more, and one not judged says so", () => {
  assert.equal(verdictChip({ verifier_id: "androidworld", status: "judged", label: null, score: 1 }).text, "androidworld 分数 1");
  assert.equal(verdictChip({ verifier_id: "judge", status: "judged", label: 0, score: 0 }).tone, "bad");
  assert.equal(verdictChip({ verifier_id: "judge", status: "not_judged" }).text, "judge 没判");
});

test("a folded task keeps its harness and its runs, branches and verdict tally", () => {
  const judged = { verifier_id: "androidworld", status: "judged", label: null, score: 1, slug: "s" };
  const group = { runs: [live("a", { agent_id: "m3a", model: "m", config_hash: "59f6c7bc00", verdicts: [judged] }), fork("b", 2, { agent_id: "m3a", model: "m", config_hash: "2d8c788a00", verdicts: [judged] }), live("c", { agent_id: "ui_tars", model: "m" })] };
  const summary = taskSummary(group);
  assert.deepEqual([summary.agents, summary.models, summary.configs, summary.runs, summary.branches, summary.unjudged], [["m3a", "ui_tars"], ["m"], ["59f6c7bc", "2d8c788a"], 2, 1, 1]);
  assert.deepEqual(summary.verdicts.map((v) => [v.text, v.count]), [["androidworld 分数 1", 2]]);
});

test("labels mark the fields that differ among a task's own runs", () => {
  const parts = runLabel({ agent_id: "m3a", model: "m", config_hash: "59f6c7bc00" }, ["agent_id"]);
  assert.deepEqual(parts.map((part) => [part.text, part.bold]), [["m3a", true], ["m", false], ["配置 59f6c7bc", false]]);
});

test("the fold choice survives a storage that refuses", () => {
  const refusing = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  assert.equal(readStored(refusing, "k", false), false);
  assert.doesNotThrow(() => writeStored(refusing, "k", true));
  const memory = new Map();
  const storage = { getItem: (key) => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value) };
  writeStored(storage, "k", true);
  assert.equal(readStored(storage, "k", false), true);
  assert.equal(readStored(undefined, "k", "fallback"), "fallback");
});

test("graph reads are bounded, report failures per run and stop queued requests on abort", async () => {
  let active = 0;
  let peak = 0;
  const results = {};
  await loadGraphs(["a", "bad", "c", "d", "e"], async (key) => {
    active++;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setImmediate(resolve));
    active--;
    if (key === "bad") throw new Error("unreadable");
    return key;
  }, (key, result) => { results[key] = result; }, new AbortController().signal, 2);
  assert.equal(peak, 2);
  assert.equal(results.bad.error, "unreadable");
  assert.equal(results.e.data, "e");
  const controller = new AbortController();
  const read = [];
  await loadGraphs(["a", "b", "c"], async (key) => { read.push(key); controller.abort(); return key; }, () => assert.fail("aborted result"), controller.signal, 1);
  assert.deepEqual(read, ["a"]);
});

test("a refresh reads a task's placement again only when its runs changed, and never asks for nothing", () => {
  const group = { runs: [live("b", { updated_at: "t1", trace_status: "complete" }), live("a", { updated_at: "t0", trace_status: "writing" })] };
  const signature = taskSignature(group);
  // the order the list gives the runs in does not matter
  assert.equal(taskSignature({ runs: [...group.runs].reverse() }), signature);
  assert.deepEqual(placementsToRead([], {}), []);
  assert.deepEqual(placementsToRead([{ key: TASK, signature }], {}), [TASK]);
  assert.deepEqual(placementsToRead([{ key: TASK, signature }], { [TASK]: { signature } }), []);
  const written = taskSignature({ runs: [group.runs[0], { ...group.runs[1], updated_at: "t2" }] });
  assert.deepEqual(placementsToRead([{ key: TASK, signature: written }], { [TASK]: { signature } }), [TASK]);
  assert.notEqual(runSignature({ key: "a", trace_status: "writing" }), runSignature({ key: "a", trace_status: "complete" }));
});
