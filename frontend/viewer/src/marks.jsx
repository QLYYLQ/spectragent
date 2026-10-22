/** Action marks drawn over a screenshot, and the numbered list that explains them. */
import React from "react";
import { Keycap } from "./common.jsx";
import { legendRows, markLayout } from "./marks.js";
import "./marks.css";

const percent = (value, total) => `${(value / total) * 100}%`;

/**
 * The marks over an image. Positions are percentages of the served marks' own
 * pixel size, so they stay on their spot at every zoom; the dots and labels
 * keep one size on screen so they stay readable when the image is shrunk to
 * fit. Nothing here takes pointer events: boxes are still drawn underneath.
 */
export function MarkLayer({ data }) {
  const layout = markLayout(data);
  if (!layout.sets.some((set) => set.shapes.length)) return null;
  const at = (point) => ({
    left: percent(point.x, layout.width),
    top: percent(point.y, layout.height),
  });
  return (
    <div className="mark-layer" aria-hidden="true">
      <svg
        className="mark-lines"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        preserveAspectRatio="none"
      >
        {layout.sets.map((set, index) =>
          set.shapes
            .filter((shape) => shape.end)
            .map((shape) => (
              <g key={`${index}-${shape.number}`}>
                <line
                  x1={shape.x}
                  y1={shape.y}
                  x2={shape.end.x}
                  y2={shape.end.y}
                  stroke="#fff"
                  strokeWidth="6"
                  strokeLinecap="round"
                  vectorEffect="non-scaling-stroke"
                />
                <line
                  x1={shape.x}
                  y1={shape.y}
                  x2={shape.end.x}
                  y2={shape.end.y}
                  stroke={set.color}
                  strokeWidth="3"
                  strokeLinecap="round"
                  strokeDasharray={shape.kind === "scroll" ? "6 4" : undefined}
                  vectorEffect="non-scaling-stroke"
                />
              </g>
            )),
        )}
      </svg>
      {layout.sets.map((set, index) =>
        set.shapes.map((shape) => (
          <React.Fragment key={`${index}-${shape.number}`}>
            {shape.end && (
              <span
                className={`mark-head ${shape.kind}`}
                style={{
                  ...at(shape.end),
                  "--mark": set.color,
                  transform: `translate(-50%, -50%) rotate(${shape.angle}deg)`,
                }}
              />
            )}
            <span
              className={`mark-dot ${shape.kind}`}
              style={{ ...at(shape), "--mark": set.color, "--stack": shape.stack }}
            >
              {shape.number}
            </span>
            {shape.typed && (
              <span
                className="mark-typed"
                style={{ ...at(shape), "--mark": set.color, "--stack": shape.stack }}
              >
                “{shape.typed}”
              </span>
            )}
          </React.Fragment>
        )),
      )}
    </div>
  );
}

/** The numbered actions behind the marks; the ones not on the image say why. */
export function MarkLegend({ data, hidden = false }) {
  if (!data) return null;
  const rows = legendRows(data).filter((row) => row.marks.length || row.problem);
  if (!rows.length && !data.problem) return null;
  return (
    <div className={`mark-legend ${hidden ? "faded" : ""}`}>
      {data.problem && <p className="mark-problem">{data.problem}</p>}
      {rows.map((row, index) => (
        <div className="mark-set" key={index} style={{ "--mark": row.color }}>
          <span className="mark-set-label">
            <span className="mark-swatch" aria-hidden="true" />
            {row.label}
          </span>
          {row.problem && <span className="mark-problem">{row.problem}</span>}
          <ol>
            {row.marks.map((mark) => (
              <li
                key={mark.number}
                className={`${mark.drawn ? "" : "off-image"} ${mark.failed ? "failed" : ""}`}
                title={[mark.text, mark.note, mark.error && `出错：${mark.error}`]
                  .filter(Boolean)
                  .join("\n")}
              >
                <span className="mark-number">{mark.number}</span>
                <span className="mark-text">{mark.text}</span>
                {mark.note && <span className="mark-note">（{mark.note}）</span>}
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}

/** Show or hide every action mark on the page. */
export function MarkToggle({ show, onChange, keycap = true }) {
  return (
    <label className="toggle">
      <input type="checkbox" checked={show} onChange={(event) => onChange(event.target.checked)} />
      动作标记 {keycap && <Keycap>M</Keycap>}
    </label>
  );
}
