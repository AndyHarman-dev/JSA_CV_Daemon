// The REASONING card — the design handoff's `thinkCard`, both of its states.
//
// The body is a list of SEPARATED STEP ROWS, not one growing block of text: the raw
// reasoning buffer is chunked by `segmentReasoning` (see lib/reasoningSteps.ts for the
// rules and the prefix-stability invariant that keeps rendered rows from re-flowing
// mid-stream). Each closed step gets a check, the step still being written gets a
// spinner and a blinking cursor — that per-row affordance is the whole point of the
// chunking, and it is meaningless against an undifferentiated blob.
//
// Two states, matching the mock's `open = threadOpen[key] ?? !m.done`:
//   running  — accent2-tinted border + glow, ring spinner, expanded by default.
//   settled  — plain border, bolt icon, collapsed by default behind a step count.
//
// While running, only the last LIVE_STEP_WINDOW steps are rendered, behind a "+N
// earlier" toggle. Chunking alone separates the reasoning but does not BOUND it — a
// long trace would still inflate the card into a wall, which is the behaviour this
// card exists to stop. The window is lifted the moment the user asks for it, and never
// applies to a settled card (where the user opened it deliberately to read the thing).
import { useState } from "react";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { Icon } from "../theme/Icon";
import { panelBase, Spinner } from "../theme/chrome";
import { segmentReasoning, type ReasoningStep } from "../lib/reasoningSteps";
import { interleaveMockTools } from "../lib/reasoningMock";

const T = SHELL_THEME;

export const LIVE_STEP_WINDOW = 5;

function StepRow({ step, active, last }: { step: ReasoningStep; active: boolean; last: boolean }) {
  const t = useT();
  return (
    <div
      data-testid={step.kind === "tool" ? "reasoning-tool-step" : "reasoning-step"}
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 9,
        padding: "8px 12px",
        borderBottom: last ? "none" : `1px solid ${T.bd}`,
      }}
    >
      <span
        style={{
          flex: "none",
          marginTop: 1,
          display: "flex",
          color: active ? T.a : step.kind === "tool" ? T.accent2 : T.accent2,
        }}
      >
        {active ? (
          <Spinner color={T.a} size={7} />
        ) : (
          <Icon name={step.kind === "tool" ? "braces" : "check"} size={11} />
        )}
      </span>
      {step.kind === "tool" ? (
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ display: "flex", alignItems: "baseline", gap: 7, flexWrap: "wrap" }}>
            <span
              style={{
                font: `600 9px ${T.mono}`,
                letterSpacing: ".1em",
                color: T.ink3,
                textTransform: "uppercase",
              }}
            >
              {t("agentThread.toolBadge")}
            </span>
            <span style={{ font: `400 12px ${T.mono}`, color: T.ink }}>{step.name}</span>
          </span>
          {step.detail && (
            <span
              style={{
                font: `400 11.5px/1.5 ${T.mono}`,
                color: T.ink3,
                wordBreak: "break-word",
              }}
            >
              {step.detail}
            </span>
          )}
        </span>
      ) : (
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          {step.label && (
            <span style={{ font: `600 11.5px ${T.ui}`, color: T.ink }}>{step.label}</span>
          )}
          {step.text && (
            <span
              style={{
                font: `400 12px/1.5 ${T.ui}`,
                color: active ? T.ink : T.ink2,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {step.text}
              {active && <span style={{ color: T.a, animation: "jscursor 1s step-start infinite" }}> █</span>}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

export function ReasoningCard({ reasoning, running }: { reasoning: string; running: boolean }) {
  const t = useT();
  const [expanded, setExpanded] = useState(running);
  const [showAll, setShowAll] = useState(false);
  // Dev-only: the backend has no tool channel yet (nothing emits AgentChunk kind
  // "tool"), so this is the only way to see the tool row against real streaming
  // reasoning in the browser. Tree-shaken out of a production build by the DEV guard.
  const [mockTools, setMockTools] = useState(false);

  const { closed, open } = segmentReasoning(reasoning);
  const textSteps: ReasoningStep[] = open ? [...closed, open] : closed;
  // The `import.meta.env.DEV` half is what lets Rollup statically drop the fixture
  // from a production bundle — `mockTools` alone is runtime state it cannot prove
  // is never set (verified: no fixture strings survive `npm run build`).
  const steps = import.meta.env.DEV && mockTools ? interleaveMockTools(textSteps) : textSteps;
  const hasSteps = steps.length > 0;
  // Only the trailing step is still being written, and only while the turn is in flight.
  // Identity, not `length - 1`: with the DEV tool preview on, an interleaved tool row
  // can be the trailing entry, and the spinner/cursor belongs on the text step that is
  // actually still being written.
  const activeIndex = running && open ? steps.lastIndexOf(open) : -1;

  const windowed = running && !showAll && steps.length > LIVE_STEP_WINDOW;
  const visible = windowed ? steps.slice(steps.length - LIVE_STEP_WINDOW) : steps;
  const hiddenCount = steps.length - visible.length;

  const accentBorder = running ? `color-mix(in srgb, ${T.accent2} 45%, ${T.bd})` : T.bd;

  return (
    <div
      data-testid="reasoning-card"
      style={{
        ...panelBase(T, { border: accentBorder, chamfer: 10 }),
        boxShadow: running
          ? `0 0 0 1px color-mix(in srgb, ${T.accent2} 30%, transparent), 0 0 20px ${T.accent2}22`
          : "none",
        overflow: "hidden",
      }}
    >
      <div
        role={hasSteps ? "button" : undefined}
        tabIndex={hasSteps ? 0 : undefined}
        onClick={hasSteps ? () => setExpanded((v) => !v) : undefined}
        onKeyDown={
          hasSteps
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setExpanded((v) => !v);
                }
              }
            : undefined
        }
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "9px 12px",
          cursor: hasSteps ? "pointer" : "default",
        }}
      >
        {running ? (
          <Spinner color={T.accent2} size={8} />
        ) : (
          <span style={{ display: "flex", color: T.accent2, flex: "none" }}>
            <Icon name="bolt" size={12} />
          </span>
        )}
        <span
          style={{
            font: `600 10px ${T.mono}`,
            letterSpacing: ".1em",
            color: T.ink,
            textTransform: "uppercase",
          }}
        >
          {t("agentThread.reasoning")}
        </span>
        <span style={{ font: `400 11px ${T.mono}`, color: T.ink3 }}>
          {!hasSteps
            ? t("agentThread.thinking")
            : t("agentThread.reasoningStepCount").replace("{count}", String(steps.length))}
        </span>
        {import.meta.env.DEV && (
          <button
            type="button"
            title="dev only — preview the tool row with no backend channel"
            onClick={(e) => {
              e.stopPropagation();
              setMockTools((v) => !v);
            }}
            style={{
              marginLeft: 8,
              background: "transparent",
              border: `1px dashed ${T.bd2}`,
              borderRadius: 3,
              color: mockTools ? T.a : T.ink3,
              font: `500 9px ${T.mono}`,
              letterSpacing: ".08em",
              padding: "2px 5px",
              cursor: "pointer",
            }}
          >
            {`TOOLS:${mockTools ? "ON" : "OFF"}`}
          </button>
        )}
        {hasSteps && (
          <span
            style={{
              marginLeft: "auto",
              display: "flex",
              color: T.ink3,
              transform: expanded ? "rotate(90deg)" : "none",
              transition: "transform .12s ease",
            }}
          >
            <Icon name="chevron" size={11} />
          </span>
        )}
      </div>
      {hasSteps && expanded && (
        <div style={{ borderTop: `1px solid ${T.bd}` }}>
          {hiddenCount > 0 && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "7px 12px",
                background: T.sunk,
                border: "none",
                borderBottom: `1px solid ${T.bd}`,
                color: T.ink3,
                font: `400 11px ${T.mono}`,
                cursor: "pointer",
              }}
            >
              {t("agentThread.reasoningEarlierSteps").replace("{count}", String(hiddenCount))}
            </button>
          )}
          {visible.map((step, i) => (
            <StepRow
              key={`${steps.length - visible.length + i}`}
              step={step}
              active={steps.length - visible.length + i === activeIndex}
              last={i === visible.length - 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}
