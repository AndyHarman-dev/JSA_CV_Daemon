// PROMPT_INJECTOR — the per-job "vial" panel and its syringe trigger
// (design_handoff_prompt_injection/JSA App Shell.dc.html: `injectTriggerBtn`,
// `renderInjectorVial`). Only the `vial` variant and the `cursor` placement mode are
// ported: the handoff explores four chromes behind an `injectorStyle`/`injectorPlacement`
// prop this app has no plumbing for, so the other three would be unreachable dead code.
//
// The panel is mounted from App.tsx, NOT from the job row: the row lives inside an
// `overflow-y: auto` aside, which clips a position:fixed descendant.
import { useEffect, useState } from "react";
import { useStore } from "../store";
import { useT } from "../i18n/useT";
import { Icon } from "../theme/Icon";
import { panelBase, cornerMarks } from "../theme/chrome";
import { SHELL_THEME } from "../theme/tokens";
import type { InjectionPresetDTO, JobDTO, PromptInjectionDTO } from "../types";

const T = SHELL_THEME;

const BLANK: PromptInjectionDTO = { prefix: "", postfix: "", first_msg: "" };

type FieldKey = keyof PromptInjectionDTO;

interface FieldMeta {
  key: FieldKey;
  labelKey: string;
  hintKey: string;
  placeholderKey: string;
}

const FIELDS: FieldMeta[] = [
  {
    key: "prefix",
    labelKey: "promptInjector.prefixLabel",
    hintKey: "promptInjector.prefixHint",
    placeholderKey: "promptInjector.prefixPlaceholder",
  },
  {
    key: "postfix",
    labelKey: "promptInjector.postfixLabel",
    hintKey: "promptInjector.postfixHint",
    placeholderKey: "promptInjector.postfixPlaceholder",
  },
  {
    key: "first_msg",
    labelKey: "promptInjector.firstMsgLabel",
    hintKey: "promptInjector.firstMsgHint",
    placeholderKey: "promptInjector.firstMsgPlaceholder",
  },
];

function anyFilled(draft: PromptInjectionDTO): boolean {
  return FIELDS.some((f) => draft[f.key].trim() !== "");
}

// Ids are only ever generated here, but the preset list is a plain array the user owns
// end to end — nothing server-side enforces uniqueness, so every handle into the list is
// an index, never this id. See `PresetChips` below.
function newPresetId(): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `p${Date.now().toString(36)}${rand}`;
}

/** The 22x22 syringe toggle that sits immediately left of the LAUNCH pill on `queued`
 *  rows. Rendered as a role="button" span, not a real <button>, because JobList's row is
 *  itself a <button> and nesting one inside it is invalid HTML — same constraint (and
 *  same shape) as LaunchButton. */
export function InjectTrigger({ job }: { job: JobDTO }) {
  const openInjector = useStore((s) => s.openInjector);
  const t = useT();
  // Plain truthiness: the API returns the server-*normalized* object or null, so there is
  // never an all-blank injection to re-check for here.
  const has = Boolean(job.injection);

  const activate = (x: number, y: number) => openInjector(job.id, x, y);

  return (
    <span
      role="button"
      tabIndex={0}
      className="jbtn"
      title={t(has ? "promptInjector.triggerTitleActive" : "promptInjector.triggerTitle")}
      onClick={(e) => {
        e.stopPropagation(); // don't also trigger the row's onSelect
        activate(e.clientX, e.clientY);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          // A keyboard activation carries no pointer coordinates — anchor the panel to the
          // trigger itself instead of letting the clamp floor it into the top-left corner.
          const box = e.currentTarget.getBoundingClientRect();
          activate(box.left, box.bottom);
        }
      }}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 22,
        height: 22,
        flex: "none",
        border: `1px solid ${has ? T.aBorder : T.bd}`,
        borderRadius: T.btnRadius,
        background: has ? T.aSoft : "transparent",
        color: has ? T.a : T.ink3,
        cursor: "pointer",
      }}
    >
      <Icon name="syringe" size={12} />
    </span>
  );
}

function Field({
  meta,
  value,
  onChange,
}: {
  meta: FieldMeta;
  value: string;
  onChange: (v: string) => void;
}) {
  const t = useT();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <label
          htmlFor={`inject-${meta.key}`}
          style={{ font: `600 10px ${T.mono}`, letterSpacing: ".1em", color: T.a }}
        >
          {t(meta.labelKey)}
        </label>
        <span style={{ font: `400 10px ${T.ui}`, color: T.ink3 }}>{t(meta.hintKey)}</span>
      </div>
      <textarea
        id={`inject-${meta.key}`}
        className="jta"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t(meta.placeholderKey)}
        rows={3}
        style={{
          width: "100%",
          resize: "vertical",
          background: T.sunk,
          border: `1px solid ${T.bd2}`,
          borderRadius: T.btnRadius,
          padding: "9px 11px",
          font: `400 12.5px/1.5 ${T.mono}`,
          color: T.ink,
          outline: "none",
        }}
      />
    </div>
  );
}

function PresetChips({
  presets,
  onPaste,
  onDelete,
}: {
  presets: InjectionPresetDTO[];
  onPaste: (p: InjectionPresetDTO) => void;
  onDelete: (index: number) => void;
}) {
  const t = useT();
  if (presets.length === 0) {
    return (
      <div style={{ font: `400 11px ${T.ui}`, color: T.ink3, fontStyle: "italic" }}>
        {t("promptInjector.presetEmpty")}
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
      {presets.map((p, i) => (
        // Index, not `p.id`: the preset id is client-generated and nothing server-side
        // enforces uniqueness, so a duplicated id (a hand-edited file, a preset list
        // copied between machines) would make an id-keyed delete remove the wrong chip.
        // The PUT is a whole-list replace, so the index IS the authoritative handle.
        <span
          key={`${i}-${p.id}`}
          className="jbtn"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "5px 6px 5px 10px",
            border: `1px solid ${T.bd2}`,
            borderRadius: T.btnRadius,
            background: T.sunk,
            font: `500 11px ${T.ui}`,
            color: T.ink,
          }}
        >
          <button
            type="button"
            onClick={() => onPaste(p)}
            title={t("promptInjector.presetPasteTitle")}
            style={{
              border: "none",
              background: "transparent",
              color: "inherit",
              font: "inherit",
              padding: 0,
              cursor: "pointer",
            }}
          >
            {p.name}
          </button>
          <button
            type="button"
            onClick={() => onDelete(i)}
            title={t("promptInjector.presetDeleteTitle")}
            style={{
              border: "none",
              background: "transparent",
              color: T.ink3,
              cursor: "pointer",
              padding: 2,
              display: "flex",
            }}
          >
            <Icon name="x" size={9} />
          </button>
        </span>
      ))}
    </div>
  );
}

/** The panel itself. Mounted fresh on every open (App.tsx renders nothing while the
 *  injector is closed), so the draft starts from the job's persisted injection and is
 *  discarded on close — exactly the design's "draft is separate from what's persisted". */
function Vial({ job }: { job: JobDTO }) {
  const closeInjector = useStore((s) => s.closeInjector);
  const saveInjection = useStore((s) => s.saveInjection);
  const presets = useStore((s) => s.injectionPresets);
  const saveInjectionPresets = useStore((s) => s.saveInjectionPresets);
  const pos = useStore((s) => s.injectorPos);
  const t = useT();

  const [draft, setDraft] = useState<PromptInjectionDTO>(job.injection ?? BLANK);
  const [saveName, setSaveName] = useState("");

  // Escape closes and discards, same as Cancel — standard for a dismissible overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeInjector();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closeInjector]);

  const filled = FIELDS.filter((f) => draft[f.key].trim() !== "").length;

  const savePreset = () => {
    const name = saveName.trim();
    if (!name || !anyFilled(draft)) return; // ignored, per the design — no error state
    const preset: InjectionPresetDTO = {
      id: newPresetId(),
      name,
      ...draft,
      saved_at: new Date().toISOString(), // client-supplied; the server never generates it
    };
    setSaveName("");
    void saveInjectionPresets([preset, ...presets]);
  };

  return (
    <>
      {/* Transparent full-viewport backdrop — the vial variant is not dimmed. */}
      <div
        data-testid="injector-backdrop"
        onClick={closeInjector}
        style={{ position: "fixed", inset: 0, zIndex: 95, background: "transparent" }}
      />
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "fixed",
          left: pos.x,
          top: pos.y,
          zIndex: 96,
          width: 380,
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
          ...panelBase(T, { chamfer: 14 }),
          boxShadow: T.shadowMd,
        }}
      >
        {cornerMarks(T, T.aBorder, 10)}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "11px 14px",
            borderBottom: `1px solid ${T.bd}`,
            flex: "none",
          }}
        >
          <span style={{ color: T.a, display: "flex" }}>
            <Icon name="syringe" size={16} />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ font: `600 12px ${T.disp}`, letterSpacing: ".04em", color: T.ink }}>
              PROMPT_INJECTOR
            </div>
            <div
              style={{
                font: `400 10px ${T.mono}`,
                color: T.ink3,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {job.company} — {job.role}
            </div>
          </div>
          {/* Fill level: one bar lit per non-empty field. */}
          <div style={{ display: "flex", gap: 3, flex: "none" }}>
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                style={{
                  width: 5,
                  height: 12,
                  borderRadius: 1,
                  background: i < filled ? T.a : T.bd,
                  boxShadow: i < filled ? `0 0 5px ${T.a}` : "none",
                }}
              />
            ))}
          </div>
          <button
            type="button"
            className="jbtn"
            onClick={closeInjector}
            title={t("promptInjector.closeTitle")}
            style={{
              border: "none",
              background: "transparent",
              color: T.ink3,
              cursor: "pointer",
              padding: 3,
              display: "flex",
            }}
          >
            <Icon name="x" size={13} />
          </button>
        </div>

        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: "13px 14px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          {FIELDS.map((meta) => (
            <Field
              key={meta.key}
              meta={meta}
              value={draft[meta.key]}
              onChange={(v) => setDraft((d) => ({ ...d, [meta.key]: v }))}
            />
          ))}
          <div
            style={{
              borderTop: `1px solid ${T.bd}`,
              paddingTop: 10,
              display: "flex",
              flexDirection: "column",
              gap: 8,
            }}
          >
            <div style={{ font: `600 10px ${T.mono}`, letterSpacing: ".12em", color: T.ink3 }}>
              SAVED_DOSES
            </div>
            <PresetChips
              presets={presets}
              // Only the three injection fields are copied across — a preset's id/name/
              // saved_at would 422 the extra="forbid" PUT body if they ever rode along.
              onPaste={(p) => setDraft({ prefix: p.prefix, postfix: p.postfix, first_msg: p.first_msg })}
              onDelete={(i) => void saveInjectionPresets(presets.filter((_, idx) => idx !== i))}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <input
                value={saveName}
                onChange={(e) => setSaveName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") savePreset();
                }}
                placeholder={t("promptInjector.presetNamePlaceholder")}
                style={{
                  flex: 1,
                  minWidth: 0,
                  background: T.sunk,
                  border: `1px solid ${T.bd}`,
                  borderRadius: T.btnRadius,
                  padding: "7px 10px",
                  font: `400 11.5px ${T.ui}`,
                  color: T.ink,
                  outline: "none",
                }}
              />
              <button
                type="button"
                className="jghost"
                onClick={savePreset}
                disabled={!saveName.trim()}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 5,
                  padding: "7px 11px",
                  border: `1px solid ${T.bd2}`,
                  borderRadius: T.btnRadius,
                  background: T.surface,
                  color: T.ink,
                  font: `600 10.5px ${T.disp}`,
                  cursor: "pointer",
                  flex: "none",
                  opacity: saveName.trim() ? 1 : 0.5,
                }}
              >
                <Icon name="plus" size={11} />
                {t("promptInjector.presetSave")}
              </button>
            </div>
          </div>
        </div>

        <div style={{ flex: "none", padding: "11px 14px", borderTop: `1px solid ${T.bd}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button
              type="button"
              className="jbtn"
              // Empties the draft only — the job's persisted injection is untouched until
              // SAVE, and the panel deliberately stays open.
              onClick={() => setDraft(BLANK)}
              style={{
                padding: "8px 12px",
                border: `1px solid ${T.bd}`,
                borderRadius: T.btnRadius,
                background: "transparent",
                color: T.ink3,
                font: `600 11px ${T.disp}`,
                cursor: "pointer",
              }}
            >
              {t("promptInjector.clear")}
            </button>
            <button
              type="button"
              className="jbtn"
              onClick={closeInjector}
              style={{
                padding: "8px 12px",
                border: `1px solid ${T.bd}`,
                borderRadius: T.btnRadius,
                background: "transparent",
                color: T.ink2,
                font: `600 11px ${T.disp}`,
                cursor: "pointer",
              }}
            >
              {t("promptInjector.cancel")}
            </button>
            <button
              type="button"
              className="jprimary"
              // An all-blank draft is a valid save: the server normalizes it to NULL,
              // which is how the design clears a job's injection.
              onClick={() => void saveInjection(job.id, draft)}
              style={{
                marginLeft: "auto",
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                padding: "9px 18px",
                border: "none",
                borderRadius: T.btnRadius,
                background: T.a,
                color: "#06080B",
                font: `600 12px ${T.disp}`,
                letterSpacing: ".03em",
                cursor: "pointer",
                boxShadow: `0 1px 14px ${T.a}55`,
              }}
            >
              <Icon name="syringe" size={13} />
              {t("promptInjector.save")}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export function PromptInjector() {
  const jobId = useStore((s) => s.injectorJobId);
  const job = useStore((s) => (s.injectorJobId ? s.jobs[s.injectorJobId] : undefined));

  // `queued` is the only editable state (the API 400s off it), so an open panel closes
  // itself if the orchestrator moves the job out from under it.
  if (!jobId || !job || job.state !== "queued") return null;
  return <Vial key={jobId} job={job} />;
}
