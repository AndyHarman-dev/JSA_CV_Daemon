import { useState, useEffect, useCallback } from "react";
import { api } from "../api";
import type { FollowUpDTO } from "../types";
import { ChatBox } from "./ChatBox";
import { MarkdownPreview } from "./MarkdownPreview";

interface Props {
  jobId: string;
}

export function FollowUpPane({ jobId }: Props) {
  const [followUp, setFollowUp] = useState<FollowUpDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchFollowUp = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const job = await api.getJob(jobId);
      const open = job.follow_ups.find((f) => f.answered_at === null) ?? null;
      setFollowUp(open);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    fetchFollowUp().catch((err: unknown) => {
      console.error("FollowUpPane fetchFollowUp error:", err);
    });
  }, [fetchFollowUp]);

  if (loading) {
    return (
      <div className="text-sm text-gray-500 px-1">
        Loading follow-up…
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
        {error}
      </div>
    );
  }

  if (followUp === null) {
    return (
      <div className="text-sm text-gray-500 px-1">
        Waiting for follow-up data…
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-500">
        Follow-up Question
      </h3>
      <div className="rounded border border-yellow-300 bg-yellow-50 px-4 py-3">
        <MarkdownPreview markdown={followUp.question} className="text-gray-800" />
      </div>
      <ChatBox
        kind="answer"
        jobId={jobId}
        followUpId={followUp.id}
        onSubmitted={fetchFollowUp}
      />
    </div>
  );
}
