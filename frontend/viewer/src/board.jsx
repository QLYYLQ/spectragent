/** Images under review, with the person's boxes drawn, moved and resized on them. */
import React, { useEffect, useRef, useState } from "react";
import {
  HANDLES,
  MIN_BOX,
  boxFromDrag,
  boxLabel,
  boxStyle,
  hasArea,
  moveBox,
  resizeBox,
  toImagePoint,
} from "./boxes.js";
import { MarkLayer, MarkLegend } from "./marks.jsx";

export const ZOOMS = ["fit", 1, 1.5, 2, 3];
export const COMPARE_MODES = [
  { value: "side", text: "并排" },
  { value: "flip", text: "切换" },
  { value: "difference", text: "差异" },
];

export function zoomText(zoom) {
  return zoom === "fit" ? "适合窗口" : `${Math.round(zoom * 100)}%`;
}

/**
 * images: [{ role, text, src, detail, marks }] (`marks`: the served action
 * marks of that image, or null)
 * regions: the draft's regions; kinds: [{ value, text, color }] or null
 */
export function ImageBoard({
  images,
  regions = [],
  suggested = [],
  showSuggested = true,
  kinds = null,
  activeKind = null,
  editable = false,
  selected = null,
  onSelect = () => {},
  onChange = () => {},
  compare = "side",
  flipIndex = 0,
  zoom = "fit",
  fit = "window",
  showMarks = true,
}) {
  // Sizes belong to the images on show; a new set starts unmeasured, so a
  // drag never maps through the previous item's image size.
  const signature = images.map((image) => image.src).join("\n");
  const [measured, setMeasured] = useState({ signature, sizes: {} });
  const [differencePair, setDifferencePair] = useState([]);
  const sizes = measured.signature === signature ? measured.sizes : {};
  const setSizes = (update) =>
    setMeasured((old) => {
      const base = old.signature === signature ? old.sizes : {};
      const next = update(base);
      return next === base && old.signature === signature ? old : { signature, sizes: next };
    });
  const mode = images.length < 2 ? "side" : compare;
  const base = images.find((image) => image.role === differencePair[0]) || images[0];
  const top =
    images.find((image) => image.role === differencePair[1] && image !== base) ||
    images.find((image) => image !== base);
  const baseSize = sizes[base?.role];
  const topSize = sizes[top?.role];
  const comparable =
    baseSize && topSize && baseSize.width === topSize.width && baseSize.height === topSize.height;

  // Every panel uses the same space below the aligned image row. Observe the
  // stage too: asynchronously loaded legends and expanded facts move that row.
  const board = useRef(null);
  const [room, setRoom] = useState(null);
  useEffect(() => {
    const element = board.current;
    if (!element) return undefined;
    const measure = () => {
      const viewport = element.querySelector(".board-viewport");
      if (!viewport) return;
      const top = fit === "below" ? viewport.getBoundingClientRect().top + window.scrollY : 120;
      setRoom(Math.max(260, window.innerHeight - top - 20));
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (element.parentElement) observer.observe(element.parentElement);
    measure();
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [signature, mode, fit]);
  const colors = Object.fromEntries((kinds || []).map((kind) => [kind.value, kind]));
  const record = (role) => (size) =>
    setSizes((old) =>
      old[role]?.width === size.width && old[role]?.height === size.height
        ? old
        : { ...old, [role]: size },
    );
  if (!images.length)
    return (
      <div className="board-empty">这个条目没有可以比较的图片。</div>
    );
  const panel = (image, extra = {}) => (
    <ImagePanel
      key={image.role}
      image={image}
      size={sizes[image.role]}
      onSize={record(image.role)}
      regions={regions}
      suggested={showSuggested ? suggested : []}
      colors={colors}
      activeKind={activeKind}
      editable={editable && Boolean(kinds)}
      selected={selected}
      onSelect={onSelect}
      onChange={onChange}
      zoom={zoom}
      room={room}
      showMarks={showMarks}
      {...extra}
    />
  );
  if (mode === "difference") {
    return (
      <div className="board single" ref={board}>
        <div className="board-pair" role="group" aria-label="选择差异图片">
          {[base, top].map((image, index) => (
            <label key={index}>
              {index === 0 ? "图片一" : "图片二"}
              <select
                value={image.role}
                onChange={(event) =>
                  setDifferencePair(
                    index === 0 ? [event.target.value, top.role] : [base.role, event.target.value],
                  )
                }
              >
                {images.map((option) => (
                  <option
                    key={option.role}
                    value={option.role}
                    disabled={option.role === (index === 0 ? top.role : base.role)}
                  >
                    {option.text || option.role}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
        {!comparable && baseSize && topSize && (
          <p className="board-note">
            两张图大小不同，差异只是大致的。
          </p>
        )}
        {panel(base, {
          overlay: top,
          onOverlaySize: record(top.role),
          caption: `“${base.text || base.role}”和“${top.text || top.role}”的差异：黑色表示像素没有变化。`,
          editable: false,
          marks: null,
          regions: [],
          suggested: [],
        })}
      </div>
    );
  }
  if (mode === "flip") {
    const shown = images[flipIndex % images.length];
    return (
      <div className="board single" ref={board}>
        {panel(shown, {
          caption: `正在看第 ${(flipIndex % images.length) + 1} 张，共 ${images.length} 张。按 F 切换。`,
        })}
      </div>
    );
  }
  return (
    <div
      className="board side"
      ref={board}
      style={{
        gridTemplateColumns: `repeat(${images.length}, minmax(0, 1fr))`,
      }}
    >
      {images.map((image) => panel(image))}
    </div>
  );
}

function ImagePanel({
  image,
  size,
  onSize,
  regions,
  suggested,
  colors,
  activeKind,
  editable,
  selected,
  onSelect,
  onChange,
  zoom,
  room,
  overlay = null,
  onOverlaySize = null,
  caption = null,
  showMarks = true,
  marks = image.marks,
}) {
  const surface = useRef(null);
  const [drag, setDrag] = useState(null);
  const [failed, setFailed] = useState(null);
  const [overlayFailed, setOverlayFailed] = useState(null);
  useEffect(() => {
    setFailed(null);
    setDrag(null);
  }, [image.src]);
  const overlayProblem = overlayFailed?.src === overlay?.src ? overlayFailed?.message : null;
  const own = regions
    .map((region, index) => ({ region, index }))
    .filter(({ region }) => region.image === image.role);
  const proposals = suggested.filter((region) => region.image === image.role);
  const drawing = editable && activeKind != null;

  function point(event) {
    return toImagePoint(
      event.clientX,
      event.clientY,
      surface.current.getBoundingClientRect(),
      size,
    );
  }

  function pointerDown(event) {
    if (event.button !== 0 || !size) return;
    const handle = event.target.closest("[data-handle]");
    const box = event.target.closest("[data-box]");
    const at = point(event);
    // Alt draws over an existing box, for a detail inside a larger area.
    if (box && !(drawing && event.altKey)) {
      const index = Number(box.dataset.box);
      onSelect(index);
      if (!editable) return;
      // Both gestures work from the box as it was at pointer-down.
      const origin = regions[index].box;
      setDrag(
        handle
          ? { mode: "resize", index, handle: handle.dataset.handle, origin, box: origin }
          : { mode: "move", index, start: at, origin, box: origin },
      );
    } else {
      onSelect(null);
      if (!drawing) return;
      setDrag({ mode: "draw", start: at, box: [at.x, at.y, at.x, at.y] });
    }
    surface.current.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function pointerMove(event) {
    if (!drag) return;
    const at = point(event);
    const box =
      drag.mode === "draw"
        ? boxFromDrag(drag.start, at, size)
        : drag.mode === "move"
          ? moveBox(drag.origin, at.x - drag.start.x, at.y - drag.start.y, size)
          : resizeBox(drag.origin, drag.handle, at, size);
    setDrag({ ...drag, box });
  }

  function pointerUp() {
    if (!drag) return;
    if (drag.mode === "draw") {
      if (hasArea(drag.box, MIN_BOX)) {
        onChange([...regions, { image: image.role, kind: activeKind, box: drag.box }]);
        onSelect(regions.length);
      }
    } else if (drag.box.some((value, i) => value !== regions[drag.index].box[i])) {
      onChange(
        regions.map((region, index) =>
          index === drag.index ? { ...region, box: drag.box } : region,
        ),
      );
    }
    setDrag(null);
  }

  // Preserve the image's aspect ratio within the board's shared fit bounds.
  const width =
    !size
      ? "100%"
      : zoom === "fit"
        ? room
          ? `min(100%, ${Math.floor(room * (size.width / size.height))}px)`
          : "100%"
        : `${size.width * zoom}px`;
  return (
    <figure className="board-panel">
      <figcaption>
        <strong>{image.text || image.role}</strong>
        {image.detail && <span className="board-detail">{image.detail}</span>}
        {size && (
          <span className="board-size">
            {size.width} × {size.height}
          </span>
        )}
        <a href={image.src} target="_blank" rel="noreferrer">
          原图
        </a>
      </figcaption>
      <div className="board-meta">
        {caption && <p className="board-caption">{caption}</p>}
        {marks && <MarkLegend data={marks} hidden={!showMarks} />}
      </div>
      <div className="board-viewport">
        {failed || overlayProblem ? (
          <div className="board-missing" role="alert">
            {failed || overlayProblem}
          </div>
        ) : (
          <div
            className={`board-surface ${drawing ? "drawing" : ""}`}
            style={{ width }}
            ref={surface}
            onPointerDown={pointerDown}
            onPointerMove={pointerMove}
            onPointerUp={pointerUp}
            onPointerCancel={() => setDrag(null)}
          >
            <img
              src={image.src}
              alt={image.text || image.role}
              draggable="false"
              onLoad={(event) =>
                onSize({
                  width: event.currentTarget.naturalWidth,
                  height: event.currentTarget.naturalHeight,
                })
              }
              onError={() => explainFailure(image, setFailed)}
            />
            {overlay && (
              <img
                key={overlay.src}
                className="board-difference"
                src={overlay.src}
                alt=""
                draggable="false"
                onLoad={(event) =>
                  onOverlaySize({
                    width: event.currentTarget.naturalWidth,
                    height: event.currentTarget.naturalHeight,
                  })
                }
                onError={() =>
                  explainFailure(overlay, (message) => setOverlayFailed({ src: overlay.src, message }))
                }
              />
            )}
            {marks && showMarks && <MarkLayer data={marks} />}
            {size &&
              proposals.map((region, index) => (
                <div
                  key={`s${index}`}
                  className={`region suggested ${tagPlacement(region.box, size)}`}
                  style={{ ...boxStyle(region.box, size), "--kind": colors[region.kind]?.color }}
                >
                  <span className="region-tag" title="建议的框">
                    {colors[region.kind]?.text || region.kind}
                  </span>
                </div>
              ))}
            {size &&
              own.map(({ region, index }) => {
                const box = drag && drag.index === index ? drag.box : region.box;
                return (
                  <div
                    key={index}
                    data-box={index}
                    className={`region ${selected === index ? "selected" : ""} ${editable ? "editable" : ""} ${tagPlacement(box, size)}`}
                    style={{ ...boxStyle(box, size), "--kind": colors[region.kind]?.color }}
                    title={`${colors[region.kind]?.text || region.kind}: ${boxLabel(box)}`}
                  >
                    <span className="region-tag">
                      {colors[region.kind]?.text || region.kind}
                    </span>
                    {editable &&
                      selected === index &&
                      HANDLES.map((handle) => (
                        <span
                          key={handle}
                          data-handle={handle}
                          className={`region-handle ${handle}`}
                        />
                      ))}
                  </div>
                );
              })}
            {size && drag?.mode === "draw" && (
              <div
                className="region drawing-box"
                style={{ ...boxStyle(drag.box, size), "--kind": colors[activeKind]?.color }}
              />
            )}
          </div>
        )}
      </div>
    </figure>
  );
}

/** Keep a box's tag inside the image: below the top edge, left of the right edge. */
function tagPlacement([x0, y0, x1], size) {
  return `${y0 < size.height * 0.05 ? "low" : ""} ${x1 > size.width * 0.8 && x0 > size.width * 0.5 ? "right" : ""}`;
}

/** An <img> error has no body; ask once more to show the viewer's reason. */
function explainFailure(image, setFailed) {
  const name = image.text || image.role;
  setFailed(`“${name}”加载不出来。`);
  fetch(image.src)
    .then((response) => response.json())
    .then((data) => {
      if (typeof data.detail === "string")
        setFailed(`“${name}”加载不出来：${data.detail}`);
    })
    .catch(() => {});
}
