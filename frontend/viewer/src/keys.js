/**
 * The review keyboard: one key per decision, read from KeyboardEvent.code so
 * Shift+digit means the same thing on every keyboard layout.
 */

export const SHORTCUTS = [
  { keys: ["1", "…", "9"], text: "Choose an answer (for the highlighted question)" },
  { keys: ["↑", "↓"], text: "Move between questions" },
  { keys: ["Shift", "1", "…", "9"], text: "Pick the box kind (or change the selected box)" },
  { keys: ["A"], text: "Accept the suggestion and go to the next item" },
  { keys: ["E"], text: "Copy the suggestion into your label to edit it" },
  { keys: ["R"], text: "Reject the suggestion without a replacement" },
  { keys: ["Enter"], text: "Save and go to the next item" },
  { keys: ["S"], text: "Skip this item" },
  { keys: ["J", "→"], text: "Next item in the filter" },
  { keys: ["K", "←"], text: "Previous item in the filter" },
  { keys: ["N"], text: "Write a note (⌘ Enter saves from the note)" },
  { keys: ["Delete"], text: "Delete the selected box" },
  { keys: ["←", "↑", "→", "↓"], text: "Nudge the selected box (Shift: 10 px)" },
  { keys: ["Esc"], text: "Deselect the box, or leave the note" },
  { keys: ["G"], text: "Show or hide the suggested boxes" },
  { keys: ["C"], text: "Compare: side by side, flip, difference" },
  { keys: ["F"], text: "Flip to the other image while flipping" },
  { keys: ["+", "−", "0"], text: "Zoom in, zoom out, fit" },
  { keys: ["?"], text: "Show these shortcuts" },
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
 * `event`: { key, code, shiftKey, altKey, ctrlKey, metaKey, inText }
 * `context`: { choices, kinds, suggestion, selection, questions }
 * (`choices` counts the highlighted question's choices when there are questions)
 */
export function keyAction(event, context) {
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
