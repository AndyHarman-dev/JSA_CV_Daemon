import { useEffect } from "react";
import { useStore } from "../store";
import type { ToastItem } from "../store";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase } from "../theme/chrome";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;
const AUTO_DISMISS_MS = 9000;

function ToastCard({ toast }: { toast: ToastItem }) {
  const dismissToast = useStore((s) => s.dismissToast);
  const selectJob = useStore((s) => s.selectJob);
  const t = useT();

  useEffect(() => {
    const timer = setTimeout(() => dismissToast(toast.id), AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [toast.id, dismissToast]);

  const message = t(toast.kind === "backend" ? "toast.backendSwitched" : "toast.modelSwitched", {
    job: toast.jobLabel,
    from: toast.from,
    to: toast.to,
  });

  return (
    <div
      role="alert"
      onClick={() => selectJob(toast.jobId)}
      style={{
        ...panelBase(T, {
          bg: `color-mix(in srgb, ${T.danger} 10%, ${T.surface})`,
          border: `1px solid color-mix(in srgb, ${T.danger} 50%, ${T.bd})`,
          chamfer: 10,
        }),
        boxShadow: T.shadowMd,
        width: 320,
        maxWidth: "calc(100vw - 32px)",
        padding: "10px 12px",
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        cursor: "pointer",
        pointerEvents: "auto",
      }}
    >
      <span style={{ color: T.danger, flex: "none", marginTop: 1 }}>
        <Icon name="alert" size={15} />
      </span>
      <p style={{ flex: 1, margin: 0, font: `400 12px/1.5 ${T.ui}`, color: T.ink }}>{message}</p>
      <button
        type="button"
        title={t("toast.dismissTitle")}
        onClick={(e) => {
          e.stopPropagation();
          dismissToast(toast.id);
        }}
        style={{
          flex: "none",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 20,
          height: 20,
          border: "none",
          background: "transparent",
          color: T.ink3,
          cursor: "pointer",
          padding: 0,
        }}
      >
        <Icon name="x" size={13} />
      </button>
    </div>
  );
}

/** Bottom-right stack of BF-19 backend/model-switch notifications. See CLAUDE.md's
 * "Backend fallback chain (BF-19)" — a switch silently re-asks any pending follow-up
 * question on the new backend/model, which otherwise looks like the pipeline hanging. */
export function Toast() {
  const toasts = useStore((s) => s.toasts);
  if (toasts.length === 0) return null;

  return (
    <div
      style={{
        position: "fixed",
        bottom: 16,
        right: 16,
        zIndex: 70,
        display: "flex",
        flexDirection: "column-reverse",
        gap: 8,
        pointerEvents: "none",
      }}
    >
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} />
      ))}
    </div>
  );
}
