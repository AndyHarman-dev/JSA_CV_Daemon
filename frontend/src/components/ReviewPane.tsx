import { useState, useEffect } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobState } from "../types";
import { ChatBox } from "./ChatBox";

interface Props {
  jobId: string;
}

type TabKey = "cv" | "cl";

interface DocPaths {
  pdfUrl: string | null;
  docxUrl: string | null;
}

const emptyPaths: DocPaths = { pdfUrl: null, docxUrl: null };

/** Convert an absolute filesystem path stored in the DB to a /api/files/<relpath> URL. */
function toFileUrl(absPath: string | null | undefined): string | null {
  if (!absPath) return null;
  // Extract last two path segments: slug/filename
  const rel = absPath.replace(/^.*?([^/\\]+[/\\][^/\\]+)$/, "$1").replace(/\\/g, "/");
  return `/api/files/${rel}`;
}

export function ReviewPane({ jobId }: Props) {
  const state = useStore((s) => s.jobs[jobId]?.state as JobState | undefined);
  const [activeTab, setActiveTab] = useState<TabKey>("cv");
  const [cvPaths, setCvPaths] = useState<DocPaths>(emptyPaths);
  const [clPaths, setClPaths] = useState<DocPaths>(emptyPaths);
  const [pathsLoading, setPathsLoading] = useState(true);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);

  // Fetch document paths whenever jobId or state changes.
  // By the time state === "review", the pipeline has already rendered both formats.
  useEffect(() => {
    let cancelled = false;
    setPathsLoading(true);

    api.getJob(jobId)
      .then((job) => {
        if (cancelled) return;

        // Find latest version per stage (documents may be unordered)
        const latestCv = job.documents
          .filter((d) => d.stage === "cv_adjust")
          .sort((a, b) => b.version - a.version)[0];
        const latestCl = job.documents
          .filter((d) => d.stage === "cover_letter")
          .sort((a, b) => b.version - a.version)[0];

        setCvPaths({
          pdfUrl: toFileUrl(latestCv?.pdf_path),
          docxUrl: toFileUrl(latestCv?.docx_path),
        });
        setClPaths({
          pdfUrl: toFileUrl(latestCl?.pdf_path),
          docxUrl: toFileUrl(latestCl?.docx_path),
        });
        setPathsLoading(false);
      })
      .catch(() => {
        if (!cancelled) setPathsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [jobId, state]);

  async function handleApprove() {
    setApproving(true);
    setApproveError(null);
    try {
      await api.approve(jobId);
      await useStore.getState().refetchAll();
    } catch (err) {
      setApproveError(err instanceof Error ? err.message : String(err));
    } finally {
      setApproving(false);
    }
  }

  const activePaths = activeTab === "cv" ? cvPaths : clPaths;

  return (
    <div className="flex flex-col gap-4">
      {/* Tab bar */}
      <div className="flex border-b border-gray-200">
        <button
          type="button"
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "cv"
              ? "border-blue-500 text-blue-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
          onClick={() => setActiveTab("cv")}
        >
          CV / Resume
        </button>
        <button
          type="button"
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "cl"
              ? "border-blue-500 text-blue-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
          onClick={() => setActiveTab("cl")}
        >
          Cover Letter
        </button>
      </div>

      {/* Document preview — PDF iframe */}
      {pathsLoading ? (
        <div className="flex items-center justify-center h-[60vh] rounded border border-gray-200 bg-gray-50">
          <span className="text-sm text-gray-500">Loading preview…</span>
        </div>
      ) : activePaths.pdfUrl ? (
        <iframe
          key={activePaths.pdfUrl}
          src={activePaths.pdfUrl}
          className="w-full h-[60vh] rounded border border-gray-200"
          title={activeTab === "cv" ? "CV / Resume Preview" : "Cover Letter Preview"}
        />
      ) : (
        <div className="flex items-center justify-center h-32 rounded border border-gray-200 bg-gray-50">
          <span className="text-sm text-gray-400 italic">
            Preview rendering in progress…
          </span>
        </div>
      )}

      {/* Download links — shown for both review and approved states */}
      {!pathsLoading && (activePaths.pdfUrl || activePaths.docxUrl) && (
        <div className="flex flex-wrap gap-2">
          {activePaths.pdfUrl && (
            <a
              href={activePaths.pdfUrl}
              download
              className="inline-flex items-center gap-1.5 rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50"
            >
              ↓ PDF
            </a>
          )}
          {activePaths.docxUrl && (
            <a
              href={activePaths.docxUrl}
              download
              className="inline-flex items-center gap-1.5 rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50"
            >
              ↓ DOCX
            </a>
          )}
        </div>
      )}

      {/* State-specific actions */}
      {state === "approved" ? (
        <div className="rounded border border-green-300 bg-green-50 px-4 py-3 text-sm font-semibold text-green-700">
          ✓ Approved
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {approveError && (
            <p className="text-sm text-red-600">{approveError}</p>
          )}
          <button
            type="button"
            className="self-start rounded bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={approving}
            onClick={() => {
              handleApprove().catch((err: unknown) => {
                console.error("ReviewPane approve error:", err);
              });
            }}
          >
            {approving ? (
              <span className="flex items-center gap-2">
                <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-white border-t-transparent" />
                Approving…
              </span>
            ) : (
              "Approve"
            )}
          </button>
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">
              Request Revision
            </h4>
            <ChatBox kind="revise" jobId={jobId} />
          </div>
        </div>
      )}
    </div>
  );
}
