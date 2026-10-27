import assert from "node:assert/strict";
import test from "node:test";
import { graphColumns, graphEdges, graphSteps, loadGraphs, sourceIssue, taskGroups, taskLanes } from "./taskGraph.js";

const source = (key, patch = {}) => ({ key, benchmark: "browser", subset: "L1", task_id: "118", task: "Find a video", ...patch });
const fork = (key, parent = "live", patch = {}) => ({ key, task_id: "synthetic-branch-id", source_key: parent, source_resolution: "found", branch: {
  source_benchmark: "browser", source_subset: "L1", source_task_id: "118", source_run_id: parent, checkpoint_step: 4,
}, ...patch });
const graph = (steps) => ({ data: { steps: steps.map(([step, origin = "policy"]) => ({ step, origin, actions: [] })) } });

test("task grouping uses benchmark, subset and source task metadata, never text or path names", () => {
  const rows = [source("live"), fork("record-looking-name"), source("another-benchmark", { benchmark: "other" }), source("another-subset", { subset: "L2" })];
  const groups = taskGroups(rows);
  assert.equal(groups.length, 3);
  assert.deepEqual(groups[0].runs.map((run) => run.key), ["live", "record-looking-name"]);
});

test("incomplete task identities remain independent unless an explicit unique source resolves them", () => {
  const a = source("a", { benchmark: "", task_id: "" });
  const b = source("b", { benchmark: "", task_id: "" });
  const child = fork("child", "a", { branch: { source_run_id: "a", checkpoint_step: 0 } });
  assert.deepEqual(taskGroups([a, b, child]).map((group) => group.runs.map((run) => run.key)), [["a", "child"], ["b"]]);
  assert.equal(taskGroups([a, b, { ...child, source_resolution: "ambiguous" }]).length, 3);
});

test("task variants retain their source task grouping while naming the original task", () => {
  const child = fork("variant", "live", { task: "Different question", branch: { ...fork("x").branch, task_variant: { source_task_text: "Original question" } } });
  const group = taskGroups([child])[0];
  assert.equal(group.taskId, "118");
  assert.equal(group.title, "Original question");
});

test("filtered source runs remain explicit context, including a source outside the list's server filter", () => {
  const original = source("live");
  const child = fork("fork");
  const lanes = taskLanes([child], [original, child]);
  assert.deepEqual(lanes.map((lane) => [lane.row.key, lane.parent, lane.context]), [["live", null, true], ["fork", "live", false]]);
  const serverFiltered = taskLanes([child], [child]);
  assert.equal(serverFiltered[0].row.context_only, true);
  assert.equal(serverFiltered[1].parent, "live");
});

test("missing, ambiguous and self-referential sources are not connected", () => {
  for (const resolution of ["missing", "ambiguous", "outside_root"]) {
    const child = fork("f", "live", { source_resolution: resolution });
    const lanes = taskLanes([child], [source("live"), child]);
    assert.equal(lanes.length, 1);
    assert.equal(lanes[0].parent, null);
    assert.ok(lanes[0].issue);
  }
  const self = fork("self", "self");
  assert.match(taskLanes([self], [self])[0].issue, /自身/);
  assert.match(sourceIssue({ source_resolution: "outside_root" }), /根目录/);
});

test("cyclic provenance does not recurse forever or fabricate a root relation", () => {
  const rows = [fork("a", "b"), fork("b", "a")];
  const lanes = taskLanes(rows, rows);
  assert.equal(lanes.length, 2);
  assert.ok(lanes.some((lane) => lane.issue?.includes("循环")));
});

test("branch lines need both a committed source checkpoint and a new branch decision", () => {
  const rows = [source("live"), fork("fork")];
  const lanes = taskLanes(rows, rows);
  const data = { live: graph([[0], [4], [5, "terminal"]]), fork: graph([[0, "restored"], [4], [5, "terminal"]]) };
  assert.deepEqual(graphSteps(rows[1], data.fork.data).map((step) => step.step), [4, 5]);
  assert.deepEqual(graphEdges(lanes, data), [{ from: 0, to: 1, step: 4 }]);
  assert.deepEqual(graphColumns(lanes, data), [0, 4, 5]);
  assert.deepEqual(graphEdges(lanes, { ...data, live: graph([[0], [5]]) }), []);
  assert.deepEqual(graphEdges(lanes, { ...data, fork: graph([[0, "restored"], [3, "restored"]]) }), []);
  assert.deepEqual(graphEdges(lanes, { ...data, fork: graph([[4, "terminal"]]) }), []);
});

test("restored source-prefix nodes hidden from a nested branch cannot anchor visible edges", () => {
  const rows = [fork("parent", "missing", { source_resolution: "missing" }), fork("child", "parent")];
  const lanes = taskLanes(rows, rows);
  const data = { parent: graph([[4, "restored"], [8]]), child: graph([[4]]) };
  assert.deepEqual(graphEdges(lanes, data), []);
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
