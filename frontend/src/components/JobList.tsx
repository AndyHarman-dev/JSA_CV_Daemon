import { useStore } from "../store";
import { StatusBadge } from "./StatusBadge";
import type { JobDTO, JobState } from "../types";
import { SHELL_THEME } from "../theme/tokens";

const T = SHELL_THEME;

const TIER_COLOR: Record<string, string> = { A: T.green, B: T.accent2, C: T.ink3 };

interface Group {
  label: string;
  states: JobState[];
}

const GROUPS: Group[] = [
  { label: "Inbox", states: ["awaiting_input"] },
  { label: "Needs Review", states: ["unfit"] },
  { label: "Running", states: ["running", "pending", "fit_done", "cv_done", "cl_done"] },
  { label: "Review", states: ["review"] },
  { label: "Done", states: ["approved"] },
  { label: "Failed", states: ["failed"] },
  { label: "Dismissed", states: ["dismissed"] },
];

function JobRow({
  job,
  isSelected,
  onSelect,
}: {
  job: JobDTO;
  isSelected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className="jrow"
      onClick={onSelect}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        width: "100%",
        textAlign: "left",
        padding: "9px 11px",
        border: `1px solid ${isSelected ? T.aBorder : "transparent"}`,
        borderRadius: T.btnRadius,
        background: isSelected ? T.surface : "transparent",
        cursor: "pointer",
      }}
    >
      {isSelected && (
        <span
          style={{
            position: "absolute",
            left: 0,
            top: 7,
            bottom: 7,
            width: 2,
            background: T.a,
            boxShadow: `0 0 8px ${T.a}`,
          }}
        />
      )}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span
          style={{
            font: `600 12.5px ${T.ui}`,
            color: T.ink,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {job.company}
        </span>
        <span style={{ font: `600 9.5px ${T.mono}`, color: TIER_COLOR[job.tier] ?? T.ink3, flex: "none" }}>
          T{job.tier}
        </span>
      </div>
      <div
        style={{
          font: `400 11.5px ${T.ui}`,
          color: T.ink2,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {job.role}
      </div>
      <StatusBadge state={job.state} />
    </button>
  );
}

export function JobList() {
  const jobs = useStore((s) => Object.values(s.jobs));
  const selectedId = useStore((s) => s.selectedId);
  const selectJob = useStore((s) => s.selectJob);

  return (
    <nav
      style={{
        width: "100%",
        height: "100%",
        background: T.subtle,
        borderRight: `1px solid ${T.bd}`,
        overflow: "auto",
        padding: "16px 12px",
      }}
    >
      <div
        style={{
          font: `600 10px ${T.mono}`,
          letterSpacing: ".16em",
          color: T.ink3,
          textTransform: "uppercase",
          padding: "0 4px 12px",
        }}
      >
        PROCESS_QUEUE
      </div>
      {GROUPS.map((group) => {
        const groupJobs = jobs.filter((j) => (group.states as string[]).includes(j.state));
        if (groupJobs.length === 0) return null;
        return (
          <section key={group.label} style={{ marginBottom: 16 }}>
            <h2
              style={{
                display: "flex",
                alignItems: "center",
                gap: 7,
                padding: "0 4px 7px",
                margin: 0,
                font: `600 10px ${T.mono}`,
                letterSpacing: ".14em",
                color: T.ink3,
                textTransform: "uppercase",
              }}
            >
              <span>{group.label}</span>
              <span style={{ flex: 1, height: 1, background: T.bd }} />
              <span style={{ font: `400 9.5px ${T.mono}`, color: T.ink3, textTransform: "none" }}>
                {groupJobs.length}
              </span>
            </h2>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {groupJobs.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  isSelected={job.id === selectedId}
                  onSelect={() => selectJob(job.id)}
                />
              ))}
            </div>
          </section>
        );
      })}
      {jobs.length === 0 && (
        <p style={{ font: `400 13px ${T.ui}`, color: T.ink3, fontStyle: "italic", padding: "0 4px" }}>
          No jobs loaded.
        </p>
      )}
    </nav>
  );
}
