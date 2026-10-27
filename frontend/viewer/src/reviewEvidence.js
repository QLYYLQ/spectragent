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
  return steps.filter((step) => step.index < anchor).sort((a, b) => a.index - b.index);
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
