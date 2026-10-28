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

export function trajectorySteps(steps, anchor) {
  const ordered = [...steps].sort((a, b) => a.index - b.index);
  return {
    past: ordered.filter((step) => step.index < anchor),
    current: ordered.find((step) => step.index === anchor) || null,
    future: ordered.filter((step) => step.index > anchor),
  };
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
