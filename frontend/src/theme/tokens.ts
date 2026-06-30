// Cyberpunk daemon theme tokens — ported verbatim from the design handoff
// (.claude/designs/design_handoff_cyberpunk_redesign/*.dc.html `get T()`).
// Tailwind v3 here cannot evaluate `color-mix()` at build time, so chrome that needs
// accent-derived translucency renders via these tokens + inline styles, not Tailwind
// utilities. See CLAUDE.md-adjacent plan notes for the full rationale.

export type Skin = "terminal" | "hud";
export type Density = "comfortable" | "compact";

export interface Theme {
  a: string; // accent (per-surface: amber for shell, red for editor)
  accent2: string; // fixed cyan "system/live" signal — never swapped
  danger: string;
  violet: string;
  green: string;
  ui: string; // body font stack
  mono: string; // telemetry/code font stack
  disp: string; // display/label/button font stack
  canvas: string;
  surface: string;
  subtle: string;
  sunk: string;
  bd: string;
  bd2: string;
  ink: string;
  ink2: string;
  ink3: string;
  aSoft: string;
  aSoft2: string;
  aBorder: string;
  pad: number;
  gap: number;
  secGap: number;
  chamfer: boolean; // true under "hud" skin
  radius: number;
  btnRadius: number;
  shadowSm: string;
  shadowMd: string;
}

export interface PaperTheme {
  a: string;
  ui: string;
  mono: string;
  serifF: string;
  surface: string;
  bd: string;
  ink: string;
  ink2: string;
  ink3: string;
}

const FONT_UI = '"IBM Plex Sans", system-ui, sans-serif';
const FONT_MONO = '"Share Tech Mono", ui-monospace, monospace';
const FONT_DISP = '"Chakra Petch", system-ui, sans-serif';

export function makeTheme(accent: string, skin: Skin = "hud", density: Density = "comfortable"): Theme {
  const chamfer = skin === "hud";
  const compact = density === "compact";
  return {
    a: accent,
    accent2: "#33E6E6",
    danger: "#FF4655",
    violet: "#9D7BFF",
    green: "#8FE3A0",
    ui: FONT_UI,
    mono: FONT_MONO,
    disp: FONT_DISP,
    canvas: "#070A0F",
    surface: "#10151D",
    subtle: "#0E141B",
    sunk: "#0B0F15",
    bd: "rgba(140,190,210,0.16)",
    bd2: "rgba(140,190,210,0.30)",
    ink: "#E8EEF2",
    ink2: "#8B97A6",
    ink3: "#4D5868",
    aSoft: `color-mix(in srgb, ${accent} 14%, #10151D)`,
    aSoft2: `color-mix(in srgb, ${accent} 24%, #10151D)`,
    aBorder: `color-mix(in srgb, ${accent} 55%, #10151D)`,
    pad: compact ? 13 : 17,
    gap: compact ? 8 : 12,
    secGap: compact ? 12 : 18,
    chamfer,
    radius: chamfer ? 0 : 3,
    btnRadius: chamfer ? 7 : 2,
    shadowSm: "0 1px 2px rgba(0,0,0,.4)",
    shadowMd: "0 10px 32px rgba(0,0,0,.55)",
  };
}

// The Document/Split paper preview stays light/print-realistic in both surfaces — never
// reskinned dark. Fixed regardless of accent/skin (the design pins this palette).
export const paperT: PaperTheme = {
  a: "#2563EB",
  ui: FONT_UI,
  mono: FONT_MONO,
  serifF: '"Newsreader", Georgia, serif',
  surface: "#FCFBF8",
  bd: "#E3DFD6",
  ink: "#211C16",
  ink2: "#6B6358",
  ink3: "#A39B8D",
};

// Fixed per-surface instances (confirmed: amber shell, red editor, hud skin, comfortable density).
export const SHELL_THEME: Theme = makeTheme("#F4CE4A", "hud", "comfortable");
export const EDITOR_THEME: Theme = makeTheme("#FF4655", "hud", "comfortable");
