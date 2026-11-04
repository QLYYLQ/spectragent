/** Presentation boundaries come from the queue, never from translated labels. */
export function sectionEvidence(item, section = null) {
  const belongs = (entry) => (entry.section ?? null) === section;
  return {
    ...item,
    run: section === null ? item.run : null,
    images: (item.images || []).filter(belongs),
    fields: (item.fields || []).filter(belongs),
    links: (item.links || []).filter(belongs),
  };
}

export function imageCanBeEdited(role, imageRoles) {
  return imageRoles == null || imageRoles.includes(role);
}

/** These are the served image's pixels; an unknown reading never becomes (0, 0). */
export function pointerPresentation(pointer) {
  const {width, height, x, y, viewport_x: viewportX, viewport_y: viewportY, status, problem} = pointer || {};
  const placed = status === "known" &&
    [width, height, x, y].every(Number.isFinite) && width > 0 && height > 0 &&
    x >= 0 && y >= 0 && x < width && y < height;
  const reading = status === "known" && [viewportX, viewportY].every(Number.isFinite)
    ? `鼠标位置 (${viewportX}, ${viewportY}) · 视口 CSS 像素`
    : placed ? `鼠标位置 (${x}, ${y}) · 图像像素` : null;
  const missing = problem || "鼠标位置未记录或无法映射到此图";
  return {
    placed,
    style: placed ? {left: `${x / width * 100}%`, top: `${y / height * 100}%`} : null,
    label: placed ? reading : reading ? `${reading}；${missing}` : missing,
    labelLeft: placed && x / width > 0.75,
  };
}

export function frameStatus(frame) {
  if (!frame) return "没有能明确关联到此步的动作前截图";
  return ({
    captured: "已拍摄",
    timeout: "拍摄超时",
    failed: "拍摄失败",
    no_tab: "页面已关闭或不可用",
    superseded: "拍摄已被后续操作取消",
  })[frame.outcome] || frame.outcome || "状态未记录";
}


export function recordedReply(record) {
  return record.origin === "restored" ? record.source_history?.source_reply : record.llm_output;
}


/** Pixel comparison remains able to cross collapsed evidence sections. */
export function comparisonEvidence(item, mode) {
  return mode === "difference" ? item.images || [] : sectionEvidence(item).images;
}

/** The folder's tabs: every committed step in order, each marked before, at or after the reviewed one. */
export function folderSteps(steps, anchor) {
  return [...steps]
    .sort((a, b) => a.index - b.index)
    .map((step) => ({ ...step, relation: step.index < anchor ? "past" : step.index > anchor ? "future" : "current" }));
}

/** The tab *offset* places from *index* (clamped to the first and last), or *index* itself. */
export function folderNeighbour(steps, index, offset) {
  const order = steps.map((step) => step.index);
  const at = order.indexOf(index);
  if (at < 0) return index;
  return order[Math.min(order.length - 1, Math.max(0, at + offset))];
}

/** The observation right after *index*: the next committed step, when it is the very next one. */
export function nextObservation(steps, index) {
  return steps.find((step) => step.index === index + 1) || null;
}

const clipped = (text, limit) => (text.length > limit ? `${text.slice(0, limit - 1)}…` : text);

/**
 * What a step's observation fragments say about the screen, read from their
 * recorded fields: the app or page in front, and the text an agent read of
 * it (a UI tree's printed elements, or a DOM's indexed elements). Absent
 * fields stay absent; nothing is inferred from a fragment's name.
 */
export function observationFacts(fragments = {}) {
  const values = Object.entries(fragments).filter(([, value]) => value && typeof value === "object");
  let front = null;
  let page = null;
  let text = null;
  for (const [, value] of values) {
    const app = typeof value.package === "string" ? value : value.device && typeof value.device.package === "string" ? value.device : null;
    if (!front && app?.package) front = [app.package, app.activity?.split(".").pop()].filter(Boolean).join(" · ");
    if (!page && typeof value.url === "string" && value.url) page = [value.page_title, value.url].filter(Boolean).join(" · ");
  }
  for (const [key, value] of values) {
    if (typeof value.serialized === "string" && value.serialized.trim()) {
      text = { source: key, lines: value.serialized.trim().split("\n") };
      break;
    }
    if (value.selector_map && typeof value.selector_map === "object") {
      const lines = Object.entries(value.selector_map).map(([index, node]) => {
        const attributes = node?.attributes || {};
        const name = attributes["aria-label"] || attributes.placeholder || attributes.title || node?.text_content || "";
        return `[${index}] <${node?.tag_name || "?"}> ${clipped(String(name).replace(/\s+/g, " ").trim(), 80)}`.trim();
      });
      if (lines.length) {
        text = { source: key, lines };
        break;
      }
    }
  }
  return { front, page, text };
}

/** The tallest height-over-width among images of known size, for a comparison row's shared box. */
export function tallestRatio(sizes) {
  const ratios = sizes.filter((size) => size?.width > 0 && size?.height > 0).map((size) => size.height / size.width);
  return ratios.length ? Math.max(...ratios) : null;
}
