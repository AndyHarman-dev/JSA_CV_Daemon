import { useState, useEffect, useCallback } from "react";
import { api } from "../api";
import type { FollowUpDTO } from "../types";
import { ChatBox } from "./ChatBox";
import { MarkdownPreview } from "./MarkdownPreview";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase } from "../theme/chrome";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

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
      <div style={{ font: `400 13px ${T.ui}`, color: T.ink3 }}>Loading follow-up…</div>
    );
  }

  if (error) {
    return (
      <div
        style={{
          position: "relative",
          ...panelBase(T, {
            bg: `color-mix(in srgb, ${T.danger} 8%, ${T.surface})`,
            border: `1px solid color-mix(in srgb, ${T.danger} 45%, ${T.bd})`,
            chamfer: 10,
          }),
          padding: "11px 14px",
          font: `400 13px/1.5 ${T.ui}`,
          color: T.ink,
        }}
      >
        {error}
      </div>
    );
  }

  if (followUp === null) {
    return (
      <div style={{ font: `400 13px ${T.ui}`, color: T.ink3, fontStyle: "italic" }}>
        Waiting for follow-up data…
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div
        style={{
          font: `600 10px ${T.mono}`,
          letterSpacing: ".14em",
          color: T.a,
          textTransform: "uppercase",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <Icon name="inbox" size={12} />
        AGENT_QUERY · BLOCKING
      </div>
      <div
        style={{
          position: "relative",
          ...panelBase(T, { bg: T.aSoft, border: `1px solid ${T.aBorder}`, chamfer: 10 }),
          padding: "13px 15px",
        }}
      >
        <MarkdownPreview markdown={followUp.question} style={{ color: T.ink }} />
      </div>
      <ChatBox kind="answer" jobId={jobId} followUpId={followUp.id} onSubmitted={fetchFollowUp} />
    </div>
  );
}
