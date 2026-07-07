// Globe pill + searchable dropdown in the CV Structure Editor top bar. Sets the global
// output/UI language preference (jsa/store/preferences.py on the backend) — not per-job,
// not per-session. See .claude/designs/language_preference_design.zip for the fidelity
// reference (`renderLangPill()` in `CV Structure Editor.dc.html`).
import { useRef, useState } from "react";
import { useStore } from "../../store";
import { useT } from "../../i18n/useT";
import { useOutsideClick } from "../../hooks/useOutsideClick";
import { panelBase, cornerMarks } from "../../theme/chrome";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";

const T = EDITOR_THEME;

export function LanguagePill() {
  const language = useStore((s) => s.language);
  const languages = useStore((s) => s.languages);
  const setLanguage = useStore((s) => s.setLanguage);
  const t = useT();

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useOutsideClick(ref, open, () => setOpen(false));

  const q = query.trim().toLowerCase();
  const filtered = q
    ? languages.filter(
        ([code, english, native]) =>
          code.toLowerCase().includes(q) ||
          english.toLowerCase().includes(q) ||
          native.toLowerCase().includes(q)
      )
    : languages;

  return (
    <div style={{ position: "relative" }} ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="cvghost"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 7,
          padding: "7px 11px",
          border: `1px solid ${T.bd2}`,
          borderRadius: T.btnRadius,
          background: T.surface,
          color: T.ink,
          cursor: "pointer",
        }}
      >
        <Icon name="globe" size={13} color={T.a} />
        <span style={{ font: `600 11px ${T.mono}`, letterSpacing: ".04em" }}>
          {language.toUpperCase()}
        </span>
        <span
          style={{
            display: "inline-flex",
            transform: open ? "rotate(180deg)" : "none",
            transition: "transform .12s ease",
          }}
        >
          <Icon name="chevron" size={11} color={T.ink3} />
        </span>
      </button>

      {open && (
        <div
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: 6,
            width: 260,
            zIndex: 40,
            ...panelBase(T, { chamfer: 12 }),
            boxShadow: T.shadowMd,
            animation: "cvfade .14s ease",
          }}
        >
          {cornerMarks(T, T.a, 9)}
          <div
            style={{
              padding: "9px 12px 7px",
              font: `600 9.5px ${T.mono}`,
              letterSpacing: ".12em",
              textTransform: "uppercase",
              color: T.ink3,
              borderBottom: `1px solid ${T.bd}`,
            }}
          >
            {t("lang.panelHeader")}
          </div>

          <div style={{ padding: 8, borderBottom: `1px solid ${T.bd}` }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "6px 8px",
                background: "#0B0F15",
                border: `1px solid ${T.bd}`,
                borderRadius: T.btnRadius,
              }}
            >
              <Icon name="search" size={13} color={T.ink3} />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("lang.searchPlaceholder")}
                style={{
                  flex: 1,
                  background: "transparent",
                  border: "none",
                  outline: "none",
                  font: `400 13px ${T.ui}`,
                  color: T.ink,
                }}
              />
            </div>
          </div>

          <div style={{ maxHeight: 240, overflowY: "auto", padding: 4 }}>
            {filtered.length === 0 && (
              <div
                style={{
                  padding: "18px 8px",
                  textAlign: "center",
                  fontStyle: "italic",
                  color: T.ink3,
                  font: `400 12.5px ${T.ui}`,
                }}
              >
                {t("lang.noMatches")}
              </div>
            )}
            {filtered.map(([code, english, native]) => {
              const selected = code === language;
              return (
                <button
                  key={code}
                  type="button"
                  onClick={() => {
                    void setLanguage(code);
                    setOpen(false);
                    setQuery("");
                  }}
                  className="cvkindbtn"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    width: "100%",
                    padding: "7px 8px",
                    border: "none",
                    borderRadius: T.btnRadius,
                    background: selected
                      ? `color-mix(in srgb, ${T.a} 14%, ${T.surface})`
                      : "transparent",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                >
                  <span style={{ font: `600 9.5px ${T.mono}`, width: 22, flex: "none", color: T.ink2 }}>
                    {code.toUpperCase()}
                  </span>
                  <span style={{ font: `500 14px ${T.ui}`, flex: 1, color: T.ink }}>{english}</span>
                  <span style={{ font: `400 11.5px ${T.ui}`, color: T.ink3 }}>{native}</span>
                  {selected && <Icon name="check" size={13} color={T.a} />}
                </button>
              );
            })}
          </div>

          <div
            style={{
              padding: "7px 12px",
              font: `400 11px ${T.ui}`,
              color: T.ink3,
              borderTop: `1px solid ${T.bd}`,
            }}
          >
            {t("lang.footer")}
          </div>
        </div>
      )}
    </div>
  );
}
