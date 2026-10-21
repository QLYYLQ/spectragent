/**
 * Box geometry for region labels. A box is [x0, y0, x1, y1] in pixels of
 * the image's original size; the screen only ever shows a scaled copy.
 */

/** Drags shorter than this many image pixels are clicks, not boxes. */
export const MIN_BOX = 3;

export const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

export function normalizeBox([x0, y0, x1, y1]) {
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

export function clampBox([x0, y0, x1, y1], size) {
  return [
    clamp(x0, 0, size.width),
    clamp(y0, 0, size.height),
    clamp(x1, 0, size.width),
    clamp(y1, 0, size.height),
  ];
}

export function roundBox(box) {
  return box.map((value) => Math.round(value));
}

export function hasArea([x0, y0, x1, y1], minimum = 1) {
  return x1 - x0 >= minimum && y1 - y0 >= minimum;
}

/** A pointer position on the shown image, in original image pixels. */
export function toImagePoint(clientX, clientY, rect, size) {
  return {
    x: clamp(((clientX - rect.left) * size.width) / rect.width, 0, size.width),
    y: clamp(((clientY - rect.top) * size.height) / rect.height, 0, size.height),
  };
}

export function boxFromDrag(start, end, size) {
  return roundBox(clampBox(normalizeBox([start.x, start.y, end.x, end.y]), size));
}

/** Move without changing size; the box stops at the image edges. */
export function moveBox([x0, y0, x1, y1], dx, dy, size) {
  const sx = clamp(dx, -x0, size.width - x1);
  const sy = clamp(dy, -y0, size.height - y1);
  return roundBox([x0 + sx, y0 + sy, x1 + sx, y1 + sy]);
}

/** Drag one corner or edge to a point; the opposite side stays put. */
export function resizeBox(box, handle, point, size) {
  let [x0, y0, x1, y1] = box;
  const x = clamp(point.x, 0, size.width);
  const y = clamp(point.y, 0, size.height);
  if (handle.includes("w")) x0 = x;
  if (handle.includes("e")) x1 = x;
  if (handle.includes("n")) y0 = y;
  if (handle.includes("s")) y1 = y;
  const next = roundBox(normalizeBox([x0, y0, x1, y1]));
  return hasArea(next) ? next : box;
}

/** CSS placement of a box over its image, in percent so zoom never moves it. */
export function boxStyle([x0, y0, x1, y1], size) {
  return {
    left: `${(x0 / size.width) * 100}%`,
    top: `${(y0 / size.height) * 100}%`,
    width: `${((x1 - x0) / size.width) * 100}%`,
    height: `${((y1 - y0) / size.height) * 100}%`,
  };
}

export function boxLabel([x0, y0, x1, y1]) {
  return `${Math.round(x1 - x0)} × ${Math.round(y1 - y0)} at ${Math.round(x0)}, ${Math.round(y0)}`;
}

export function sameRegion(a, b) {
  return (
    a.image === b.image &&
    a.kind === b.kind &&
    a.box.length === b.box.length &&
    a.box.every((value, index) => value === b.box[index])
  );
}

/** Regions compare as a multiset of (image, kind, box), the way the store compares them. */
export function sameRegions(left = [], right = []) {
  if (left.length !== right.length) return false;
  const remaining = [...right];
  for (const region of left) {
    const index = remaining.findIndex((other) => sameRegion(region, other));
    if (index < 0) return false;
    remaining.splice(index, 1);
  }
  return true;
}
