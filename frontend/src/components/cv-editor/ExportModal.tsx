// Export dialog: a read-only light "paper" preview of the CV as it will be exported, a page
// bottom-margin control, and two backend-free download actions (HTML / print-to-PDF). Ported
// from the reference standalone editor's export modal (~/Desktop/cv-editor.html:1069-1093).
//
// `bottomMargin` is owned by the caller (CvEditor), not local state here: the reference keeps
// it on its top-level component, so it survives the export dialog being closed and reopened
// within the same session — a modal-local `useState` would silently reset it to the default
// every time, since this component fully unmounts on close.
import { useEditorStore } from "../../editorStore";
import { useT } from "../../i18n/useT";
import { panelBase, cornerMarks } from "../../theme/chrome";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";
import { buildExportBody, downloadHtml, downloadPdf } from "./cvExport";

const T = EDITOR_THEME;

interface ExportModalProps {
  bottomMargin: number;
  setBottomMargin: (n: number) => void;
}

export function ExportModal({ bottomMargin, setBottomMargin }: ExportModalProps) {
  const cv = useEditorStore((s) => s.cv);
  const toggleExport = useEditorStore((s) => s.toggleExport);
  const t = useT();
  if (!cv) return null;

  const previewHtml = buildExportBody(cv);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100,
        background: "rgba(4,6,9,.72)",
        backdropFilter: "blur(3px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 28,
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) toggleExport();
      }}
    >
      <div
        style={{
          position: "relative",
          width: "min(880px,100%)",
          maxHeight: "92vh",
          display: "flex",
          flexDirection: "column",
          ...panelBase(T, { chamfer: 16 }),
          boxShadow: T.shadowMd,
          overflow: "hidden",
        }}
      >
        {cornerMarks(T, T.a, 11)}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 18px", borderBottom: `1px solid ${T.bd}`, flex: "none" }}>
          <div>
            <div style={{ font: `600 15px ${T.disp}`, color: T.ink, letterSpacing: ".02em" }}>{t("exportModal.title")}</div>
            <div style={{ display: "flex", alignItems: "center", gap: 5, font: `400 10.5px ${T.mono}`, color: T.ink3, letterSpacing: ".08em" }}>
              <span
                style={{ width: 6, height: 6, borderRadius: 6, background: T.accent2, boxShadow: `0 0 6px ${T.accent2}`, animation: "cyblink 2s ease-in-out infinite" }}
              />
              {t("exportModal.subtitle")}
            </div>
          </div>
          <button
            type="button"
            title={t("exportModal.closeTitle")}
            onClick={toggleExport}
            className="cvbtn"
            style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 28, height: 28, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
          >
            <Icon name="x" size={15} />
          </button>
        </div>

        <div style={{ flex: 1, minHeight: 0, overflow: "auto", background: T.canvas, padding: "26px 22px" }}>
          <div
            style={{
              width: 680,
              maxWidth: "100%",
              margin: "0 auto",
              background: "#FCFBF8",
              border: "1px solid #E3DFD6",
              borderRadius: 4,
              boxShadow: "0 1px 2px rgba(0,0,0,.2),0 20px 44px rgba(0,0,0,.4)",
              padding: "44px 46px",
              minHeight: 400,
            }}
          >
            <div dangerouslySetInnerHTML={{ __html: previewHtml }} />
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 14, padding: "14px 18px", borderTop: `1px solid ${T.bd}`, flex: "none" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ font: `600 10px ${T.mono}`, letterSpacing: ".08em", color: T.ink3, textTransform: "uppercase", whiteSpace: "nowrap" }}>
              {t("exportModal.bottomMarginLabel")}
            </span>
            <input
              type="range"
              min={0}
              max={96}
              step={2}
              value={bottomMargin}
              onChange={(e) => setBottomMargin(Number(e.target.value))}
              style={{ width: 120, accentColor: T.a }}
              title={t("exportModal.bottomMarginTitle")}
            />
            <span style={{ font: `400 11px ${T.mono}`, color: T.ink2, minWidth: 32 }}>{bottomMargin}px</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button
              type="button"
              onClick={() => downloadHtml(cv, bottomMargin)}
              className="cvghost"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                padding: "9px 16px",
                border: `1px solid ${T.bd2}`,
                borderRadius: T.btnRadius,
                background: T.surface,
                color: T.ink,
                font: `600 12px ${T.disp}`,
                letterSpacing: ".04em",
                cursor: "pointer",
              }}
            >
              <Icon name="braces" size={14} />
              {t("exportModal.downloadHtml")}
            </button>
            <button
              type="button"
              onClick={() => downloadPdf(cv, bottomMargin)}
              className="cvprimary"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                padding: "9px 18px",
                border: "none",
                borderRadius: T.btnRadius,
                background: T.a,
                color: "#06080B",
                font: `600 12px ${T.disp}`,
                letterSpacing: ".04em",
                cursor: "pointer",
                boxShadow: `0 1px 14px ${T.a}55`,
              }}
            >
              <Icon name="doc" size={14} />
              {t("exportModal.downloadPdf")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
