import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobState } from "../types";

const WS_STATUS_DOT: Record<
  "connecting" | "open" | "closed",
  { color: string; label: string }
> = {
  connecting: { color: "bg-yellow-400", label: "Connecting" },
  open: { color: "bg-green-500", label: "Connected" },
  closed: { color: "bg-red-500", label: "Disconnected" },
};

const RUNNING_STATES: JobState[] = ["running", "pending", "cv_done", "cl_done"];
const INBOX_STATES: JobState[] = ["awaiting_input"];
const REVIEW_STATES: JobState[] = ["review"];
const DONE_STATES: JobState[] = ["approved"];
const FAILED_STATES: JobState[] = ["failed"];

interface CountBadgeProps {
  label: string;
  count: number;
  className: string;
}

function CountBadge({ label, count, className }: CountBadgeProps) {
  if (count === 0) return null;
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${className}`}>
      {label}: {count}
    </span>
  );
}

export function Header() {
  const wsStatus = useStore((s) => s.wsStatus);
  const jobs = useStore((s) => s.jobs);
  const dot = WS_STATUS_DOT[wsStatus];

  type BackendState = { status: "loading" } | { status: "ok"; value: string } | { status: "error" };
  const [backendState, setBackendState] = useState<BackendState>({ status: "loading" });

  useEffect(() => {
    api
      .config()
      .then((cfg) => {
        const b = cfg["backend"];
        setBackendState(
          typeof b === "string"
            ? { status: "ok", value: b }
            : { status: "error" }
        );
      })
      .catch(() => {
        setBackendState({ status: "error" });
      });
  }, []);

  const jobList = Object.values(jobs);

  function countByStates(states: JobState[]): number {
    return jobList.filter((j) => states.includes(j.state)).length;
  }

  const running = countByStates(RUNNING_STATES);
  const inbox = countByStates(INBOX_STATES);
  const review = countByStates(REVIEW_STATES);
  const done = countByStates(DONE_STATES);
  const failed = countByStates(FAILED_STATES);

  return (
    <header className="flex items-center justify-between px-4 py-2 bg-white border-b border-gray-200 flex-shrink-0 gap-4">
      {/* Left: App name + backend */}
      <div className="flex items-center gap-3 flex-shrink-0">
        <span className="font-bold text-gray-800 text-sm tracking-tight md:hidden">
          JSA
        </span>
        <span className="font-bold text-gray-800 text-sm tracking-tight hidden md:inline">
          JSA — Job Search Assistant
        </span>
        {backendState.status === "loading" && (
          <span className="text-xs text-gray-400">…</span>
        )}
        {backendState.status === "ok" && (
          <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-500 text-xs font-mono">
            {backendState.value}
          </span>
        )}
        {/* status === "error": hide gracefully — render nothing */}
      </div>

      {/* Center: Aggregate counts */}
      <div className="hidden md:flex items-center gap-2 flex-wrap">
        <CountBadge
          label="Running"
          count={running}
          className="bg-blue-100 text-blue-700"
        />
        <CountBadge
          label="Inbox"
          count={inbox}
          className="bg-yellow-100 text-yellow-700"
        />
        <CountBadge
          label="Review"
          count={review}
          className="bg-purple-100 text-purple-700"
        />
        <CountBadge
          label="Done"
          count={done}
          className="bg-green-100 text-green-700"
        />
        <CountBadge
          label="Failed"
          count={failed}
          className="bg-red-100 text-red-700"
        />
      </div>

      {/* Right: WS status */}
      <div className="flex items-center gap-1.5 flex-shrink-0">
        <div className={`w-2.5 h-2.5 rounded-full ${dot.color}`} />
        <span className="text-xs text-gray-500">{dot.label}</span>
      </div>
    </header>
  );
}
