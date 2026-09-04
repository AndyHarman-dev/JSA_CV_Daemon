// Shared icon set — superset of both design handoff files' inline SVG icon maps
// (JSA App Shell.dc.html `icon()` + CV Structure Editor.dc.html `icon()`/`grip()`).
// 16x16 viewBox, stroke 1.55, round caps/joins — ported verbatim per-path.
import type { ReactNode } from "react";

export type IconName =
  | "x" | "plus" | "minus" | "check" | "trash" | "refresh" | "send" | "download" | "chevron"
  | "alert" | "mail" | "phone" | "pin" | "bolt" | "server" | "inbox" | "doc" | "work"
  | "link" | "back" | "up" | "down" | "undo" | "redo" | "braces" | "spark" | "eye"
  | "copy" | "text" | "list" | "tag" | "cap" | "blocks" | "cols" | "globe" | "search" | "play"
  | "pencil" | "star";

interface IconProps {
  name: IconName;
  size?: number;
  color?: string;
  className?: string;
}

const PATHS: Record<IconName, string[]> = {
  x: ["M4 4l8 8M12 4l-8 8"],
  plus: ["M8 3.2v9.6M3.2 8h9.6"],
  minus: ["M3.2 8h9.6"],
  check: ["M3 8.4l3.3 3.4L13 4.6"],
  trash: ["M3 4.5h10M6.4 4.5V3.2h3.2v1.3M4.6 4.5l.6 8.3h5.6l.6-8.3"],
  refresh: ["M3 7.6a5 5 0 0 1 8.8-3.2M13 3v3.4h-3.4", "M13 8.4a5 5 0 0 1-8.8 3.2M3 13V9.6h3.4"],
  send: ["M2.5 8L13 3l-3.6 10.5-2-4.7-4.9-1.8z"],
  download: ["M8 2.5v7.2M5 7l3 2.7L11 7", "M3 12.5h10"],
  chevron: ["M5 5.5L8 9l3-3.5"],
  alert: ["M8 2.5l6.2 11H1.8z"],
  mail: ["M2.5 4.5l5.5 4.5 5.5-4.5"],
  phone: ["M4 2.5h2l1 3-1.4 1.2a8 8 0 0 0 3.7 3.7l1.2-1.4 3 1V13a1 1 0 0 1-1 1C7.8 14 2 8.2 2 3.5a1 1 0 0 1 1-1z"],
  pin: ["M8 14s4.5-4.4 4.5-7.8a4.5 4.5 0 1 0-9 0C3.5 9.6 8 14 8 14z"],
  bolt: ["M8.6 1.8L3 9h3.4l-1 5.2L13 7H9.4z"],
  server: [],
  inbox: ["M2 3.2h12v9.6H2z", "M2 8h3.4l1 2h3.2l1-2H14"],
  doc: ["M5.8 5.4h4.4M5.8 8h4.4M5.8 10.6h2.6"],
  work: ["M6 5V4a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1"],
  link: ["M6.5 9.5l3-3M7 5.2l.8-.8a2.4 2.4 0 0 1 3.4 3.4l-.9.9M9 10.8l-.8.8a2.4 2.4 0 0 1-3.4-3.4l.9-.9"],
  back: ["M10.5 3.5L5 8l5.5 4.5"],
  up: ["M3.5 9.5L8 5l4.5 4.5"],
  down: ["M3.5 6.5L8 11l4.5-4.5"],
  undo: ["M6.5 4L3 7.2l3.5 3.2", "M3 7.2h6.2a3.4 3.4 0 0 1 0 6.8H6.6"],
  redo: ["M9.5 4L13 7.2l-3.5 3.2", "M13 7.2H6.8a3.4 3.4 0 0 0 0 6.8h2.6"],
  braces: ["M6.2 3C4.6 3 5 5 5 6.2c0 1-.5 1.6-1.6 1.8 1.1.2 1.6.8 1.6 1.8C5 11 4.6 13 6.2 13", "M9.8 3c1.6 0 1.2 2 1.2 3.2 0 1 .5 1.6 1.6 1.8-1.1.2-1.6.8-1.6 1.8 0 1.2.4 3.2-1.2 3.2"],
  spark: ["M8 2.2l1.3 3.9 3.9 1.3-3.9 1.3L8 12.6 6.7 8.7 2.8 7.4l3.9-1.3z"],
  eye: ["M1.2 8S3.6 3.6 8 3.6 14.8 8 14.8 8 12.4 12.4 8 12.4 1.2 8 1.2 8z"],
  copy: ["M3.2 10.8V4a.8.8 0 0 1 .8-.8h6.8"],
  text: ["M3.5 4.5h9M3.5 8h9M3.5 11.5h5.5"],
  list: ["M6.8 4.5h6M6.8 8h6M6.8 11.5h4"],
  tag: ["M8.3 2.8H12a1.2 1.2 0 0 1 1.2 1.2v3.7a1 1 0 0 1-.3.7l-5 5a1 1 0 0 1-1.4 0L3 9.5a1 1 0 0 1 0-1.4l5-5a1 1 0 0 1 .3-.3z"],
  cap: ["M8 3L1.8 6 8 9l6.2-3L8 3z", "M4.5 7.3v3c0 .9 1.6 1.7 3.5 1.7s3.5-.8 3.5-1.7v-3"],
  blocks: [],
  cols: ["M8 2.8v10.4"],
  globe: ["M1.8 8h12.4", "M8 1.8c-2.2 1.8-2.2 10.6 0 12.4", "M8 1.8c2.2 1.8 2.2 10.6 0 12.4"],
  search: ["M11.2 11.2L14 14"],
  play: [],
  pencil: ["M10.6 2.8a1.6 1.6 0 0 1 2.3 2.3L5.6 12.4l-3.1.8.8-3.1z", "M9.4 4l2.3 2.3"],
  star: ["M8 2.2l1.8 3.7 4.1.6-3 2.9.7 4.1L8 11.6l-3.6 1.9.7-4.1-3-2.9 4.1-.6z"],
};

// Icons that mix paths with non-path primitives (rects/circles/lines) — rendered explicitly
// below rather than forced through the path-only PATHS table.
function extraShapes(name: IconName): ReactNode {
  switch (name) {
    case "alert":
      return (
        <>
          <line x1={8} y1={6.4} x2={8} y2={9.4} />
          <circle cx={8} cy={11.4} r={0.35} fill="currentColor" stroke="none" />
        </>
      );
    case "mail":
      return <rect x={2} y={3.5} width={12} height={9} rx={1.2} />;
    case "server":
      return (
        <>
          <rect x={2.5} y={2.5} width={11} height={4.2} rx={1} />
          <rect x={2.5} y={9.3} width={11} height={4.2} rx={1} />
          <circle cx={11} cy={4.6} r={0.55} fill="currentColor" stroke="none" />
          <circle cx={11} cy={11.4} r={0.55} fill="currentColor" stroke="none" />
        </>
      );
    case "doc":
      return <rect x={3.5} y={2.2} width={9} height={11.6} rx={1.2} />;
    case "work":
      return <rect x={2.5} y={5} width={11} height={7.5} rx={1.2} />;
    case "eye":
      return <circle cx={8} cy={8} r={1.9} />;
    case "copy":
      return <rect x={5.2} y={5.2} width={7.6} height={7.6} rx={1.6} />;
    case "list":
      return (
        <>
          <circle cx={4} cy={4.5} r={1} fill="currentColor" stroke="none" />
          <circle cx={4} cy={8} r={1} fill="currentColor" stroke="none" />
          <circle cx={4} cy={11.5} r={1} fill="currentColor" stroke="none" />
        </>
      );
    case "tag":
      return <circle cx={10.3} cy={5.7} r={0.9} />;
    case "blocks":
      return (
        <>
          <rect x={2.5} y={3} width={11} height={3.4} rx={1} />
          <rect x={2.5} y={9.6} width={11} height={3.4} rx={1} />
        </>
      );
    case "cols":
      return <rect x={2.4} y={2.8} width={11.2} height={10.4} rx={1.4} />;
    case "globe":
      return <circle cx={8} cy={8} r={6.2} />;
    case "search":
      return <circle cx={6.5} cy={6.5} r={4.2} />;
    case "play":
      return <polygon points="5,3.4 12.5,8 5,12.6" fill="currentColor" stroke="none" />;
    default:
      return null;
  }
}

export function Icon({ name, size = 16, color, className }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.55}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ display: "block", color: color || "currentColor", flex: "none" }}
    >
      {PATHS[name].map((d, i) => (
        <path key={i} d={d} />
      ))}
      {extraShapes(name)}
    </svg>
  );
}

export function Grip({ color = "#5A6472", size = 16 }: { color?: string; size?: number }) {
  const dots: [number, number][] = [[5, 4], [11, 4], [5, 8], [11, 8], [5, 12], [11, 12]];
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" style={{ display: "block" }}>
      {dots.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r={1.25} fill={color} />
      ))}
    </svg>
  );
}
