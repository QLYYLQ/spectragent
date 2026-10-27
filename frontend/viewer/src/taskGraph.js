/** Task and lineage identities come from recorded fields, never run names. */
const present = (value) => value !== null && value !== undefined && String(value).trim() !== "";

export function taskIdentity(row, byKey = new Map(), visited = new Set()) {
  const branch = row.branch;
  const identity = branch
    ? [branch.source_benchmark, branch.source_subset, branch.source_task_id]
    : [row.benchmark, row.subset, row.task_id];
  if (present(identity[0]) && present(identity[2]))
    return { key: JSON.stringify(identity.map((value) => value ?? "")), benchmark: identity[0], subset: identity[1] || "", taskId: identity[2] };
  // An explicit resolved source can still identify a family with incomplete task metadata.
  if (branch && row.source_resolution === "found" && byKey.has(row.source_key) && !visited.has(row.key)) {
    const next = new Set(visited).add(row.key);
    const source = byKey.get(row.source_key);
    if (!next.has(source.key)) return taskIdentity(source, byKey, next);
  }
  return { key: `run:${row.key}`, benchmark: identity[0] || "", subset: identity[1] || "", taskId: identity[2] || "", incomplete: true };
}

export function taskGroups(allRows, visibleRows = allRows) {
  const byKey = new Map(allRows.map((row) => [row.key, row]));
  const groups = new Map();
  for (const row of visibleRows) {
    const identity = taskIdentity(row, byKey);
    if (!groups.has(identity.key)) groups.set(identity.key, { ...identity, runs: [], title: "" });
    const group = groups.get(identity.key);
    group.runs.push(row);
    const sourceTask = row.branch?.task_variant?.source_task_text;
    if (!group.title || !row.branch) group.title = sourceTask || row.task || row.task_id || row.key;
  }
  return [...groups.values()];
}

/** Include filtered sources as context; ambiguous/missing references never become edges. */
export function taskLanes(runs, allRows) {
  const byKey = new Map(allRows.map((row) => [row.key, row]));
  const visible = new Set(runs.map((row) => row.key));
  const nodes = new Map();
  const ensure = (row) => {
    if (nodes.has(row.key)) return nodes.get(row.key);
    const node = { row, context: !visible.has(row.key), parent: null, issue: null, children: [] };
    nodes.set(row.key, node);
    if (row.branch) {
      if (row.source_resolution === "found" && row.source_key) {
        if (row.source_key === row.key) node.issue = "来源引用了运行自身";
        else {
          node.parent = row.source_key;
          ensure(byKey.get(row.source_key) || { key: row.source_key, context_only: true });
        }
      } else node.issue = sourceIssue(row);
    }
    return node;
  };
  for (const row of runs) ensure(row);
  for (const node of nodes.values()) {
    const seen = new Set([node.row.key]);
    let parent = node.parent;
    while (parent) {
      if (seen.has(parent)) {
        node.parent = null;
        node.issue = "来源引用形成循环，无法连接";
        break;
      }
      seen.add(parent);
      parent = nodes.get(parent)?.parent;
    }
  }
  for (const node of nodes.values()) if (node.parent) nodes.get(node.parent).children.push(node);
  const compare = (a, b) => (a.row.branch?.checkpoint_step ?? -1) - (b.row.branch?.checkpoint_step ?? -1)
    || (a.row.created_at || "").localeCompare(b.row.created_at || "") || a.row.key.localeCompare(b.row.key);
  const lanes = [];
  const visit = (node, depth = 0) => {
    lanes.push({ ...node, depth });
    for (const child of node.children.sort(compare)) visit(child, depth + 1);
  };
  for (const root of [...nodes.values()].filter((node) => !node.parent).sort(compare)) visit(root);
  return lanes;
}

export function sourceIssue(row) {
  const messages = {
    ambiguous: "源运行 ID 对应多份记录，无法确定连接",
    outside_root: "源运行不在查看器根目录内",
    unreadable: "源运行无法读取",
    missing: "根目录内未找到源运行（可能不在此根内）",
  };
  return messages[row.source_resolution] || "没有可确认的源运行连接";
}

export function graphSteps(row, data) {
  return (data?.steps || []).filter((step) => !row.branch || step.origin !== "restored");
}

export function graphColumns(lanes, data) {
  const values = new Set();
  for (const lane of lanes) {
    for (const step of graphSteps(lane.row, data[lane.row.key]?.data)) values.add(step.step);
    if (lane.parent && Number.isInteger(lane.row.branch?.checkpoint_step)) values.add(lane.row.branch.checkpoint_step);
  }
  return [...values].sort((a, b) => a - b);
}

/** An edge requires the actual source checkpoint and a new decision there. */
export function graphEdges(lanes, data) {
  const positions = new Map(lanes.map((lane, index) => [lane.row.key, index]));
  const edges = [];
  for (const [index, lane] of lanes.entries()) {
    if (!positions.has(lane.parent)) continue;
    const checkpoint = lane.row.branch?.checkpoint_step;
    const source = graphSteps(lanes[positions.get(lane.parent)].row, data[lane.parent]?.data);
    const child = data[lane.row.key]?.data?.steps || [];
    if (source.some((step) => step.step === checkpoint)
      && child.some((step) => step.step === checkpoint && step.origin === "policy"))
      edges.push({ from: positions.get(lane.parent), to: index, step: checkpoint });
  }
  return edges;
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
