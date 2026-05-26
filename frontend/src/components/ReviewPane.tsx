import { useState, useEffect } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobState } from "../types";
import { MarkdownPreview } from "./MarkdownPreview";
import { ChatBox } from "./ChatBox";

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
