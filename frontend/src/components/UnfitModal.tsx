import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO } from "../types";

interface UnfitModalProps {
  job: JobDTO;
}

/**
 * Centered overlay shown when a job is parked in the `unfit` state by the
 * fit-assessment stage. Presents the agent's reason and two choices:
 *   - Dismiss Job (red)  → same as the existing dismiss action (→ dismissed)
 *   - Ignore (gray)      → proceed through the remaining stages (→ fit_done)
 */
export function UnfitModal({ job }: UnfitModalProps) {
  const refetchAll = useStore((s) => s.refetchAll);
  const [busy, setBusy] = useState<null | "dismiss" | "ignore">(null);
  const [error, setError] = useState<string | null>(null);

  async function run(action: "dismiss" | "ignore") {
    setBusy(action);
    setError(null);
    try {
      if (action === "dismiss") {
        await api.dismiss(job.id);
      } else {
        await api.ignoreFit(job.id);
      }
      await refetchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(null);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
        <h2 className="text-lg font-bold text-gray-900">
          You may not be a fit for this role
        </h2>
        <p className="mt-1 text-sm text-gray-500">
          {job.company} — {job.role}
        </p>

        <div className="mt-4 rounded border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          {job.fit_reason || "The assessment flagged a significant mismatch."}
        </div>

        <p className="mt-4 text-sm text-gray-600">
          You can dismiss this job, or ignore the assessment and tailor your
          application anyway.
        </p>

        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => { run("ignore").catch(console.error); }}
            className="text-sm px-4 py-2 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            {busy === "ignore" ? "Continuing…" : "Ignore"}
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => { run("dismiss").catch(console.error); }}
            className="text-sm px-4 py-2 rounded border border-red-300 text-red-600 hover:bg-red-50 disabled:opacity-50"
          >
            {busy === "dismiss" ? "Dismissing…" : "Dismiss Job"}
          </button>
        </div>
      </div>
    </div>
  );
}
