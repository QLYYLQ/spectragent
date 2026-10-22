/**
 * The review keyboard: one key per decision, read from KeyboardEvent.code so
 * Shift+digit means the same thing on every keyboard layout.
 */

export const SHORTCUTS = [
  { keys: ["1", "…", "9"], text: "选答案（几个问题时作用于高亮的那一题）" },
  { keys: ["↑", "↓"], text: "在问题之间移动" },
  { keys: ["Shift", "1", "…", "9"], text: "选框的类别（或改选中框的类别）" },
  { keys: ["A"], text: "接受建议并进入下一条" },
  { keys: ["E"], text: "把建议复制到你的标注里再修改" },
  { keys: ["R"], text: "驳回建议，不给替代答案" },
  { keys: ["V"], text: "显示或隐藏建议（模型的判断）" },
  { keys: ["Enter"], text: "保存并进入下一条" },
  { keys: ["S"], text: "跳过这一条" },
  { keys: ["J", "→"], text: "下一条（按当前筛选）" },
  { keys: ["K", "←"], text: "上一条（按当前筛选）" },
  { keys: ["N"], text: "写备注（在备注里按 ⌘ Enter 保存）" },
  { keys: ["Delete"], text: "删除选中的框" },
  { keys: ["←", "↑", "→", "↓"], text: "微调选中的框（按住 Shift：每次 10 像素）" },
  { keys: ["Esc"], text: "取消选中的框，或离开备注" },
  { keys: ["G"], text: "显示或隐藏建议的框" },
  { keys: ["M"], text: "显示或隐藏动作标记" },
  { keys: ["C"], text: "对比方式：并排、切换、差异" },
  { keys: ["F"], text: "切换模式下换到下一张图" },
  { keys: ["+", "−", "0"], text: "放大、缩小、适合窗口" },
  { keys: ["I"], text: "打开或关闭标注说明" },
  { keys: ["?"], text: "显示快捷键" },
];

const ARROWS = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

/**
 * The action a key press asks for, or null to leave the key to the browser.
 *
 * `event`: { key, code, shiftKey, altKey, ctrlKey, metaKey, repeat, inText }
 * `context`: { choices, kinds, suggestion, selection, questions }
 * (`choices` counts the highlighted question's choices when there are questions)
 */
export function keyAction(event, context) {
  const action = decide(event, context);
  // A held key repeats: only nudging a box may repeat, never a decision.
  if (action && event.repeat && action.type !== "nudge") return null;
  return action;
}

function decide(event, context) {
  const command = event.ctrlKey || event.metaKey;
  if (event.inText) {
    if (event.key === "Escape") return { type: "leaveText" };
    if (command && event.key === "Enter") return { type: "save" };
    return null;
  }
  if (command || event.altKey) return null;
  const digit = /^Digit([0-9])$/.exec(event.code || "");
  if (digit && digit[1] !== "0") {
    const index = Number(digit[1]) - 1;
    if (event.shiftKey)
      return index < context.kinds ? { type: "kind", index } : null;
    return index < context.choices ? { type: "choice", index } : null;
  }
  if (context.selection && ARROWS[event.key]) {
    const step = event.shiftKey ? 10 : 1;
    const [dx, dy] = ARROWS[event.key];
    return { type: "nudge", dx: dx * step, dy: dy * step };
  }
  if ((context.questions || 0) > 1 && (event.key === "ArrowUp" || event.key === "ArrowDown"))
    return { type: "question", step: event.key === "ArrowUp" ? -1 : 1 };
  switch (event.key) {
    case "Enter":
      return { type: "save" };
    case "j":
    case "ArrowRight":
      return { type: "next" };
    case "k":
    case "ArrowLeft":
      return { type: "previous" };
    case "s":
      return { type: "skip" };
    case "n":
      return { type: "note" };
    case "c":
      return { type: "compare" };
    case "f":
      return { type: "flip" };
    case "g":
      return context.suggestion ? { type: "toggleSuggestion" } : null;
    case "v":
      return context.suggestion ? { type: "revealSuggestion" } : null;
    case "m":
      return { type: "marks" };
    case "i":
      return { type: "guide" };
    case "a":
      return context.suggestion ? { type: "accept" } : null;
    case "e":
      return context.suggestion ? { type: "edit" } : null;
    case "r":
      return context.suggestion ? { type: "reject" } : null;
    case "Delete":
    case "Backspace":
      return context.selection ? { type: "deleteBox" } : null;
    case "Escape":
      return { type: "escape" };
    case "+":
    case "=":
      return { type: "zoom", step: 1 };
    case "-":
    case "_":
      return { type: "zoom", step: -1 };
    case "0":
      return { type: "zoom", step: 0 };
    case "?":
      return { type: "help" };
    default:
      return null;
  }
}

/** True when typing in this element should keep its keys. */
export function isTextTarget(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName.toLowerCase();
  if (tag === "textarea" || tag === "select" || target.isContentEditable) return true;
  if (tag !== "input") return false;
  return !["checkbox", "radio", "button", "submit", "range"].includes(
    (target.type || "").toLowerCase(),
  );
}
