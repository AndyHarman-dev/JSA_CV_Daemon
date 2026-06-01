import { useState, useEffect } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobState } from "../types";
import { MarkdownPreview } from "./MarkdownPreview";
import { ChatBox } from "./ChatBox";

type ExportFormat = "pdf" | "docx";

interface ExportedPaths {
  cv_path: string;
  cl_path: string;
}

type ExportLinks = Partial<Record<ExportFormat, ExportedPaths>>;

interface Props {
  jobId: string;
}

type TabKey = "cv" | "cl";

interface DocState {
  markdown: string;
  loading: boolean;
  error: string | null;
}

const emptyDoc: DocState = { markdown: "", loading: true, error: null };

export function ReviewPane({ jobId }: Props) {
  const state = useStore((s) => s.jobs[jobId]?.state as JobState | undefined);
  const [activeTab, setActiveTab] = useState<TabKey>("cv");
  const [cvDoc, setCvDoc] = useState<DocState>(emptyDoc);
  const [clDoc, setClDoc] = useState<DocState>(emptyDoc);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);

  // Export state
  const [exportFormat, setExportFormat] = useState<ExportFormat>("pdf");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportLinks, setExportLinks] = useState<ExportLinks>({});

  // Seed initial PDF links from the approved job's existing document paths
  useEffect(() => {
    if (state !== "approved") return;
    let cancelled = false;
    api.getJob(jobId)
      .then((job) => {
        if (cancelled) return;
        const cvDoc = job.documents.find((d) => d.stage === "cv_adjust");
        const clDoc = job.documents.find((d) => d.stage === "cover_letter");
        const initialLinks: ExportLinks = {};
        // Paths stored in DB are absolute; extract last two segments (slug/filename)
        // to form the relpath consumed by GET /api/files/<relpath>.
        const toRel = (abs: string) => abs.replace(/^.*?([^/]+\/[^/]+)$/, "$1");
        // Seed PDF link if pdf_path exists on both cv and cl docs
        if (cvDoc?.pdf_path && clDoc?.pdf_path) {
          initialLinks["pdf"] = {
            cv_path: toRel(cvDoc.pdf_path),
            cl_path: toRel(clDoc.pdf_path),
          };
        }
        // Seed DOCX link if docx_path exists
        if (cvDoc?.docx_path && clDoc?.docx_path) {
          initialLinks["docx"] = {
            cv_path: toRel(cvDoc.docx_path),
            cl_path: toRel(clDoc.docx_path),
          };
        }
        if (Object.keys(initialLinks).length > 0) {
          setExportLinks((prev) => ({ ...initialLinks, ...prev }));
        }
      })
      .catch(() => {
        // Best-effort: if we can't seed initial links, the user can still export
      });
    return () => { cancelled = true; };
  }, [jobId, state]);

  useEffect(() => {
    setCvDoc(emptyDoc);
    setClDoc(emptyDoc);

    let cancelled = false;

    api.getDocument(jobId, "cv_adjust")
      .then((doc) => {
        if (!cancelled) {
          setCvDoc({ markdown: doc.markdown, loading: false, error: null });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setCvDoc({
            markdown: "",
            loading: false,
            error: err instanceof Error ? err.message : String(err),
          });
        }
      });

    api.getDocument(jobId, "cover_letter")
      .then((doc) => {
        if (!cancelled) {
          setClDoc({ markdown: doc.markdown, loading: false, error: null });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setClDoc({
            markdown: "",
            loading: false,
            error: err instanceof Error ? err.message : String(err),
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [jobId]);

  function triggerDownload(relPath: string): void {
    const a = document.createElement("a");
    a.href = `/api/files/${relPath}`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function handleExport() {
    setExporting(true);
    setExportError(null);
    try {
      const result = await api.exportJob(jobId, exportFormat);
      // Accumulate download links — each format stores cv + cl paths independently
      setExportLinks((prev) => ({
        ...prev,
        [exportFormat]: { cv_path: result.cv_path, cl_path: result.cl_path },
      }));
      // Trigger browser downloads immediately for both files
      triggerDownload(result.cv_path);
      triggerDownload(result.cl_path);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(false);
    }
  }

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

  const activeDoc = activeTab === "cv" ? cvDoc : clDoc;

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

      {/* Document preview */}
      {activeDoc.loading ? (
        <div className="text-sm text-gray-500">Loading…</div>
      ) : activeDoc.error ? (
        <div className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {activeDoc.error}
        </div>
      ) : (
        <MarkdownPreview
          markdown={activeDoc.markdown}
          className="max-h-[60vh] rounded border border-gray-200 bg-white p-4"
        />
      )}

      {/* Actions */}
      {state === "approved" ? (
        <div className="flex flex-col gap-3">
          <div className="rounded border border-green-300 bg-green-50 px-4 py-3 text-sm font-semibold text-green-700">
            Approved
          </div>

          {/* Format toggle + Export button */}
          <div className="flex items-center gap-3 flex-wrap">
            <div className="flex rounded border border-gray-300 overflow-hidden text-sm font-medium">
              <button
                type="button"
                className={`px-3 py-1.5 transition-colors ${
                  exportFormat === "pdf"
                    ? "bg-blue-600 text-white"
                    : "bg-white text-gray-700 hover:bg-gray-50"
                }`}
                onClick={() => setExportFormat("pdf")}
                disabled={exporting}
              >
                PDF
              </button>
              <button
                type="button"
                className={`px-3 py-1.5 border-l border-gray-300 transition-colors ${
                  exportFormat === "docx"
                    ? "bg-blue-600 text-white"
                    : "bg-white text-gray-700 hover:bg-gray-50"
                }`}
                onClick={() => setExportFormat("docx")}
                disabled={exporting}
              >
                DOCX
              </button>
            </div>
            <button
              type="button"
              className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={exporting}
              onClick={() => {
                handleExport().catch((err: unknown) => {
                  console.error("ReviewPane export error:", err);
                });
              }}
            >
              {exporting ? (
                <span className="flex items-center gap-2">
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-white border-t-transparent" />
                  Exporting…
                </span>
              ) : (
                "Export & Download"
              )}
            </button>
          </div>

          {/* Export error */}
          {exportError && (
            <p className="text-sm text-red-600">{exportError}</p>
          )}

          {/* Accumulated download links — one row per exported format */}
          {(["pdf", "docx"] as ExportFormat[]).map((fmt) => {
            const links = exportLinks[fmt];
            if (!links) return null;
            return (
              <div key={fmt} className="flex flex-col gap-1 text-sm">
                <span className="font-medium text-gray-700 uppercase text-xs tracking-wider">
                  {fmt}
                </span>
                <div className="flex gap-4">
                  <a
                    href={`/api/files/${links.cv_path}`}
                    download
                    className="text-blue-600 hover:underline"
                  >
                    Download CV
                  </a>
                  <a
                    href={`/api/files/${links.cl_path}`}
                    download
                    className="text-blue-600 hover:underline"
                  >
                    Download Cover Letter
                  </a>
                </div>
              </div>
            );
          })}
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
              "Approve & Export PDFs"
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
