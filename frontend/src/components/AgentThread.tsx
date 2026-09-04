import { Fragment, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useStore } from "../store";
import type { TranscriptTurn } from "../types";
import { ChatBox, type ChatBoxHandle } from "./ChatBox";
import { MarkdownPreview } from "./MarkdownPreview";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { Icon } from "../theme/Icon";
import { panelBase } from "../theme/chrome";
import { ReasoningCard } from "./ReasoningCard";

const T = SHELL_THEME;

export type AgentThreadMode = "answer" | "none" | "revise";

interface Props {
  jobId: string;
  mode: AgentThreadMode;
  // Only meaningful for mode="revise" — forwarded to ChatBox's fixedTarget so a
  // cv_review-scoped thread never offers a cover-letter revision target.
  fixedTarget?: "cv";
}

// The open FollowUp for `answer` mode is derived from the transcript itself — a
// "question" turn whose follow_up_id has no matching "answer" turn yet. Mirrors the
// old FollowUpPane's `follow_ups.find(f => f.answered_at === null)`, just projected
// through the transcript instead of the full job.
function findOpenFollowUpId(turns: TranscriptTurn[]): number | null {
  const answered = new Set(
    turns.filter((turn) => turn.kind === "answer" && turn.follow_up_id != null).map((turn) => turn.follow_up_id)
  );
  const questions = turns.filter((turn) => turn.kind === "question" && turn.follow_up_id != null);
  const open = questions.find((turn) => !answered.has(turn.follow_up_id));
  return open?.follow_up_id ?? null;
}

// A short, decisive suggestion (no trailing separator/ellipsis, roughly under ~40
// chars) sends immediately; a longer one populates the textarea for editing instead.
function shouldSendImmediately(suggestion: string): boolean {
  const trimmed = suggestion.trim();
  if (trimmed.length === 0 || trimmed.length > 40) return false;
  if (/(\.\.\.|…|[:;,\-–—])$/.test(trimmed)) return false;
  return true;
}

function SuggestedReplyChips({
  suggestions,
  onSend,
  onPopulate,
}: {
  suggestions: string[];
  onSend: (text: string) => void;
  onPopulate: (text: string) => void;
}) {
  const t = useT();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          font: `600 10px ${T.mono}`,
          letterSpacing: ".1em",
          color: T.ink3,
          textTransform: "uppercase",
        }}
      >
        <Icon name="bolt" size={12} />
        {t("agentThread.suggestedReplies")}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {suggestions.map((suggestion, i) => (
          <button
            key={`${i}-${suggestion}`}
            type="button"
            onClick={() => {
              if (shouldSendImmediately(suggestion)) {
                onSend(suggestion);
              } else {
                onPopulate(suggestion);
              }
            }}
            style={{
              padding: "6px 12px",
              borderRadius: 999,
              border: `1px solid ${T.bd2}`,
              background: T.sunk,
              color: T.ink,
              font: `400 12px ${T.ui}`,
              cursor: "pointer",
              textAlign: "left",
            }}
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}

function avatarFor(role: TranscriptTurn["role"], t: (key: string) => string) {
  const isAgent = role === "assistant";
  return (
    <span
      title={isAgent ? t("agentThread.agentLabel") : t("agentThread.youLabel")}
      style={{
        flex: "none",
        width: 24,
        height: 24,
        borderRadius: 24,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        font: `700 11px ${T.mono}`,
        background: isAgent ? T.aSoft : T.sunk,
        border: `1px solid ${isAgent ? T.aBorder : T.bd2}`,
        color: isAgent ? T.a : T.ink2,
      }}
    >
      {isAgent ? "A" : "Y"}
    </span>
  );
}

// A settled agent turn. `turn.reasoning` is already persisted and served by
// jsa/api/transcript.py — it was simply never rendered, so a turn's reasoning used to
// vanish the moment the live buffer was cleared. Here it comes back as the card's
// settled state: bolt icon, a check per step, collapsed behind a step count.
function TurnBubble({ turn }: { turn: TranscriptTurn }) {
  const t = useT();
  const isUser = turn.role === "user";
  const reasoning = !isUser && turn.reasoning ? turn.reasoning.trim() : "";
  // Persisted tool rows (see types.ts's TranscriptTurn.tools) — same shape as the live
  // streamBuffers entry, so this turn's settled card renders identically to how it
  // looked while live. In practice the backend attaches these to `plumbing` turns, which
  // render through the hoisted card in AgentThread's map, not through TurnBubble; this
  // stays wired so a question turn (ask_user's park) also shows them. Nullable — defaults
  // to no tool rows rather than erroring.
  const tools = !isUser && turn.tools ? turn.tools : [];
  return (
    <div
      style={{
        display: "flex",
        flexDirection: isUser ? "row-reverse" : "row",
        alignItems: "flex-start",
        gap: 8,
        width: "100%",
      }}
    >
      {avatarFor(turn.role, t)}
      <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: "82%", width: "100%" }}>
        {(reasoning.length > 0 || tools.length > 0) && (
          <ReasoningCard reasoning={reasoning} running={false} tools={tools} />
        )}
        <div
          style={{
            display: "flex",
            gap: 8,
            justifyContent: isUser ? "flex-end" : "flex-start",
            font: `600 10px ${T.mono}`,
            letterSpacing: ".08em",
            color: T.ink3,
            textTransform: "uppercase",
          }}
        >
          <span>{isUser ? t("agentThread.youLabel") : t("agentThread.agentLabel")}</span>
          {turn.created_at && <span>{new Date(turn.created_at).toLocaleTimeString()}</span>}
        </div>
        <div
          style={{
            ...panelBase(T, { bg: isUser ? T.sunk : T.aSoft, border: isUser ? T.bd2 : T.aBorder, chamfer: 10 }),
            padding: "10px 13px",
            font: `400 13px/1.55 ${T.ui}`,
            color: T.ink,
          }}
        >
          <MarkdownPreview markdown={turn.text} style={{ color: T.ink }} />
        </div>
      </div>
    </div>
  );
}

function NoticeLine({ turn }: { turn: TranscriptTurn }) {
  const t = useT();
  const badge = turn.kind === "verdict" ? t("agentThread.verdictBadge") : t("agentThread.deliveryBadge");
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        justifyContent: "center",
        font: `400 11.5px ${T.ui}`,
        color: T.ink3,
        fontStyle: "italic",
      }}
    >
      <span
        style={{
          font: `600 9px ${T.mono}`,
          letterSpacing: ".1em",
          color: T.ink3,
          textTransform: "uppercase",
        }}
      >
        {badge}
      </span>
      <span>{turn.text}</span>
    </div>
  );
}

// Phase 8 — the live, in-progress agent turn rendered from the streaming buffer.
// The REASONING card is the ONE live-feedback widget for the pre-content phase — it
// is never absent while the turn has no content yet, regardless of whether the
// backend actually streamed reasoning (claude-cli's thinking_delta) or not (a
// structured-mode/google-cli session, or a routed model with no reasoning_content
// channel at all): with real reasoning it shows the streamed text chunked into steps,
// otherwise it falls back to a static "Thinking…" placeholder — there must always be
// SOME live affordance, never a silent gap. Once content starts arriving, the card only
// shows if real reasoning was actually captured (no stale "Thinking…" once the model
// has visibly moved on to answering).
//
// The card itself (both its running and settled states, the step chunking, and the
// live step window that stops a long trace from inflating it) lives in ReasoningCard.
function LiveBubble({
  content,
  reasoning,
  tools,
}: {
  content: string;
  reasoning: string;
  tools: { name: string; detail: string; ok: boolean; at: number }[];
}) {
  const t = useT();
  const hasReasoning = reasoning.trim().length > 0;
  const hasContent = content.trim().length > 0;
  // A tool call is live-feedback-worthy on its own, even before any reasoning text has
  // streamed — the same "there must always be SOME live affordance" rule that keeps the
  // card up for a pre-content "Thinking…" placeholder.
  const showReasoningCard = hasReasoning || tools.length > 0 || !hasContent;
  return (
    <div style={{ display: "flex", flexDirection: "row", alignItems: "flex-start", gap: 8, width: "100%" }}>
      {avatarFor("assistant", t)}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: "82%", width: "100%" }}>
        {showReasoningCard && <ReasoningCard reasoning={reasoning} running tools={tools} />}
        {hasContent && (
          <div
            style={{
              ...panelBase(T, { bg: T.aSoft, border: T.aBorder, chamfer: 10 }),
              padding: "10px 13px",
              font: `400 13px/1.55 ${T.ui}`,
              color: T.ink,
            }}
          >
            <MarkdownPreview markdown={content} style={{ color: T.ink }} />
          </div>
        )}
      </div>
    </div>
  );
}

// Docked composer — implements the design handoff's sticky-footer mechanic
// (.claude/designs/floating_input_box.zip, "Docked Chat Input"). `position: sticky;
// bottom: 0` on this wrapper, as the last element inside the page's single scrolling
// ancestor (`<main overflow-y-auto>` in App.tsx), docks it to the bottom of the visible
// pane once content overflows — no JS scroll math. The negative margins bleed it to the
// full width of JobDetail's padded content column and cancel the extra bottom padding
// JobDetail reserves for this (see JobDetail.tsx's `80px` bottom padding) so it sits
// flush at the true bottom instead of floating above empty space. Do not reintroduce a
// maxHeight/overflow wrapper around the turns list above — that was the actual bug: a
// second, independent scroll box that never grew to reclaim the space the composer
// vacates when a follow-up is answered.
function StickyComposerDock({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        position: "sticky",
        bottom: 0,
        zIndex: 8,
        marginLeft: -26,
        marginRight: -26,
        marginBottom: -80,
        paddingLeft: 26,
        paddingRight: 26,
        paddingTop: 14,
        paddingBottom: 20,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        background: `linear-gradient(rgba(7,10,15,0), ${T.surface} 22%)`,
        borderTop: `1px solid ${T.bd2}`,
        boxShadow: "0 -16px 28px -12px rgba(0,0,0,.5)",
      }}
    >
      {children}
    </div>
  );
}

function PlumbingLine({ turn }: { turn: TranscriptTurn }) {
  const t = useT();
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "8px 10px",
        background: T.sunk,
        border: `1px dashed ${T.bd2}`,
        borderRadius: T.radius,
      }}
    >
      <span
        style={{
          font: `600 9px ${T.mono}`,
          letterSpacing: ".1em",
          color: T.ink3,
          textTransform: "uppercase",
        }}
      >
        {t("agentThread.plumbingBadge")} · {turn.role}
      </span>
      <span style={{ font: `400 11.5px/1.5 ${T.mono}`, color: T.ink3, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {turn.text}
      </span>
    </div>
  );
}

export function AgentThread({ jobId, mode, fixedTarget }: Props) {
  const t = useT();
  const transcript = useStore((s) => s.transcripts[jobId]);
  const fetchTranscript = useStore((s) => s.fetchTranscript);
  const streamBuffer = useStore((s) => s.streamBuffers[jobId]);
  // A live turn's buffer entry only exists once the FIRST agent_chunk WS event
  // arrives -- but a turn that streams no reasoning AND has its content chunks
  // filtered (structured mode, see _reasoning_only in the backend layer) or a
  // turn on a non-streaming backend never sends one at all, so streamBuffer can
  // stay undefined for the turn's ENTIRE duration. Falling back to job.state ===
  // "running" here is what makes LiveBubble's placeholder actually show up in
  // that case -- without it, "the UI feedback must be shown anyway" silently
  // depended on at least one chunk having streamed, which is exactly the case
  // it's meant to cover when there is none.
  const isJobRunning = useStore((s) => s.jobs[jobId]?.state === "running");
  const liveBuffer = streamBuffer ?? (isJobRunning ? { stage: "", content: "", reasoning: "", tools: [] } : undefined);
  const [loading, setLoading] = useState(transcript === undefined);
  const [error, setError] = useState<string | null>(null);
  const [showInternals, setShowInternals] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const chatBoxRef = useRef<ChatBoxHandle | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(useStore.getState().transcripts[jobId] === undefined);
    setError(null);
    fetchTranscript(jobId)
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, fetchTranscript]);

  const turns = transcript ?? [];

  // Auto-scroll only while the user is already parked at the bottom. This effect fires
  // on EVERY streamed reasoning token, and the REASONING card is expanded by default
  // while a turn is live (see ReasoningCard) — so an unconditional scrollIntoView yanks
  // the viewport back down every few milliseconds and makes it impossible to read back
  // up the thread mid-turn. The sentinel below the thread is the probe: if it was in
  // view before this update, the user was at the bottom and wants to follow along.
  // Defaults to the previous always-scroll behaviour where IntersectionObserver is
  // unavailable (jsdom).
  const atBottomRef = useRef(true);
  useEffect(() => {
    const sentinel = bottomRef.current;
    if (!sentinel || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      atBottomRef.current = entries[entries.length - 1].isIntersecting;
    });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!atBottomRef.current) return;
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [turns.length, liveBuffer?.content.length, liveBuffer?.reasoning.length]);

  const openFollowUpId = mode === "answer" ? findOpenFollowUpId(turns) : null;
  const openFollowUpTurn =
    openFollowUpId != null
      ? turns.find((turn) => turn.kind === "question" && turn.follow_up_id === openFollowUpId)
      : undefined;
  const openFollowUpSuggestions =
    openFollowUpTurn?.suggested_replies && openFollowUpTurn.suggested_replies.length > 0
      ? openFollowUpTurn.suggested_replies
      : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: 0 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          font: `600 10px ${T.mono}`,
          letterSpacing: ".14em",
          color: T.a,
          textTransform: "uppercase",
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Icon name="inbox" size={12} />
          AGENT_THREAD
        </span>
        <button
          type="button"
          onClick={() => setShowInternals((v) => !v)}
          style={{
            background: "transparent",
            border: "none",
            color: T.ink3,
            font: `500 10px ${T.mono}`,
            letterSpacing: ".04em",
            cursor: "pointer",
            textTransform: "none",
          }}
        >
          {showInternals ? t("agentThread.hideInternals") : t("agentThread.showInternals")}
        </button>
      </div>

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 12,
          padding: "2px 2px 4px",
        }}
      >
        {loading && (
          <div style={{ font: `400 13px ${T.ui}`, color: T.ink3 }}>{t("agentThread.loading")}</div>
        )}
        {!loading && error && (
          <div style={{ font: `400 13px ${T.ui}`, color: T.danger }}>{error || t("agentThread.error")}</div>
        )}
        {!loading && !error && turns.length === 0 && (
          <div style={{ font: `400 13px ${T.ui}`, color: T.ink3, fontStyle: "italic" }}>
            {t("agentThread.empty")}
          </div>
        )}
        {!loading &&
          !error &&
          turns.map((turn) => {
            if (turn.kind === "plumbing") {
              // A plumbing turn carrying persisted tool rows renders its REASONING card
              // ALWAYS, outside the showInternals gate — that card is the settled twin of
              // the LiveBubble card the same turn showed while it was streaming, and
              // hiding it behind "show internals" would mean tool activity vanished on
              // reload (the revision-tool-use plan's :500, "live and settled look
              // identical"). The raw machine text stays gated, as it always was.
              //
              // Asymmetry worth knowing about: `turn.reasoning` is only ever attached to
              // plumbing turns (jsa/api/transcript.py), so this branch is the ONLY place
              // a settled turn's reasoning becomes visible — a non-tool turn's reasoning
              // is still not rendered anywhere. That is a pre-existing gap in the
              // reasoning-step-chunking work, deliberately not widened here.
              const toolMarks = turn.role !== "user" && turn.tools ? turn.tools : [];
              return (
                <Fragment key={turn.seq}>
                  {toolMarks.length > 0 && (
                    <ReasoningCard
                      reasoning={turn.reasoning ? turn.reasoning.trim() : ""}
                      running={false}
                      tools={toolMarks}
                    />
                  )}
                  {showInternals && <PlumbingLine turn={turn} />}
                </Fragment>
              );
            }
            if (turn.kind === "verdict" || turn.kind === "delivery") {
              return <NoticeLine key={turn.seq} turn={turn} />;
            }
            return <TurnBubble key={turn.seq} turn={turn} />;
          })}
        {liveBuffer && (
          <LiveBubble content={liveBuffer.content} reasoning={liveBuffer.reasoning} tools={liveBuffer.tools} />
        )}
        <div ref={bottomRef} />
      </div>

      {mode === "answer" && openFollowUpId != null && (
        <StickyComposerDock>
          {openFollowUpSuggestions && (
            <SuggestedReplyChips
              suggestions={openFollowUpSuggestions}
              onSend={(text) => {
                chatBoxRef.current?.sendText(text).catch((err: unknown) => {
                  console.error("Suggested reply send error:", err);
                });
              }}
              onPopulate={(text) => chatBoxRef.current?.populateText(text)}
            />
          )}
          <ChatBox
            ref={chatBoxRef}
            kind="answer"
            jobId={jobId}
            followUpId={openFollowUpId}
            onSubmitted={() => fetchTranscript(jobId)}
          />
        </StickyComposerDock>
      )}
      {mode === "revise" && (
        <StickyComposerDock>
          <ChatBox
            kind="revise"
            jobId={jobId}
            fixedTarget={fixedTarget}
            onSubmitted={() => fetchTranscript(jobId)}
          />
        </StickyComposerDock>
      )}
    </div>
  );
}
