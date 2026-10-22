/**
 * Action marks on an image. The viewer's Python side turns each recorded
 * action into a mark in the image's own pixels (spectragent/inspection/marks.py);
 * this file only lays the served marks out for drawing and for the legend.
 */

/** One colour per set of marks: the first set (the image's own step) magenta, then blue, amber. */
export const MARK_COLORS = ["#d6197a", "#1f6feb", "#c46a00", "#168052"];

export function markColor(index) {
  return MARK_COLORS[((index % MARK_COLORS.length) + MARK_COLORS.length) % MARK_COLORS.length];
}

/** How long a scroll's direction arrow is, in image pixels (about a fourteenth of the image). */
export function arrowLength(width, height) {
  return Math.max(24, Math.round(Math.max(width || 0, height || 0) / 14));
}

/** The angle of the line from `a` to `b`, in degrees (an image keeps its aspect ratio on screen). */
export function angle(a, b) {
  return (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI;
}

const DIRECTION = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };

/**
 * Everything to draw for one served image: per set, its colour, and per mark
 * that has a point the shapes (in image pixels). Marks without a point stay
 * in the legend only.
 */
export function markLayout(data) {
  if (!data || !data.width || !data.height) return { width: 0, height: 0, length: 0, sets: [] };
  const length = arrowLength(data.width, data.height);
  // Dots on (nearly) the same spot, such as a click and then typing there,
  // are fanned out sideways on screen so every number stays readable.
  const placed = [];
  const stack = (x, y) => {
    const near = placed.filter(
      (other) => Math.abs(other.x - x) <= length / 6 && Math.abs(other.y - y) <= length / 6,
    ).length;
    placed.push({ x, y });
    return near;
  };
  const sets = (data.sets || []).map((set, index) => ({
    color: markColor(index),
    label: set.label,
    shapes: (set.marks || [])
      .filter((mark) => mark.x != null && mark.y != null && mark.kind)
      .map((mark) => {
        const shape = {
          number: mark.number,
          kind: mark.kind,
          x: mark.x,
          y: mark.y,
          stack: stack(mark.x, mark.y),
        };
        if (mark.kind === "drag" && mark.end_x != null && mark.end_y != null)
          shape.end = { x: mark.end_x, y: mark.end_y };
        if (mark.kind === "scroll" && DIRECTION[mark.direction]) {
          const [dx, dy] = DIRECTION[mark.direction];
          shape.end = { x: mark.x + dx * length, y: mark.y + dy * length };
        }
        if (shape.end) shape.angle = angle(shape, shape.end);
        if (mark.kind === "type" && mark.typed) shape.typed = mark.typed;
        return shape;
      }),
  }));
  return { width: data.width, height: data.height, length, sets };
}

/** The legend rows: every mark of every set, with whether it is drawn and why not. */
export function legendRows(data) {
  return (data?.sets || []).map((set, index) => ({
    color: markColor(index),
    label: set.label || "动作",
    problem: set.problem || null,
    marks: (set.marks || []).map((mark) => ({
      number: mark.number,
      text: mark.text,
      drawn: Boolean(mark.kind && mark.x != null && mark.y != null),
      note:
        mark.kind && (mark.x == null || mark.y == null)
          ? mark.unplaced || "位置未知，没有画在图上"
          : mark.kind
            ? null
            : "没有位置，不画在图上",
      failed: mark.succeeded === false,
      error: mark.error || null,
    })),
  }));
}

/** Whether a served image has any mark or legend row worth showing. */
export function hasMarks(data) {
  return Boolean(data?.sets?.some((set) => (set.marks || []).length || set.problem));
}
