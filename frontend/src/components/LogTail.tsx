import { useEffect, useRef } from "react";
import { useStore } from "../store";
import type { LogEntry } from "../types";

interface LogTailProps {
  jobId: string;
  maxLines?: number;
}

const LEVEL_CLASSES: Record<LogEntry["level"], string> = {
  info: "bg-gray-100 text-gray-600",
  warn: "bg-yellow-100 text-yellow-700",
  error: "bg-red-100 text-red-700",
};

function formatTime(ts: number): string {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

export function LogTail({ jobId, maxLines = 50 }: LogTailProps) {
  const logs = useStore((s) =>
    s.logs.filter((l) => l.job_id === jobId).slice(-maxLines)
  );
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (logs.length === 0) {
    return (
      <div className="text-xs text-gray-400 italic px-2 py-1">No log entries yet.</div>
    );
  }

  return (
    <div className="font-mono text-xs overflow-y-auto max-h-48 border border-gray-200 rounded bg-gray-50">
      {logs.map((entry, i) => (
        <div key={i} className="flex items-start gap-2 px-2 py-0.5 border-b border-gray-100 last:border-b-0">
          <span className="text-gray-400 flex-shrink-0">{formatTime(entry.ts)}</span>
          <span
            className={`px-1 rounded flex-shrink-0 uppercase text-[10px] font-bold ${LEVEL_CLASSES[entry.level]}`}
          >
            {entry.level}
          </span>
          <span className="break-all text-gray-700">{entry.text}</span>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
