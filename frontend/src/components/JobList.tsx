import { useStore } from "../store";
import { StatusBadge } from "./StatusBadge";
import type { JobDTO, JobState } from "../types";

interface Group {
  label: string;
  states: JobState[];
}

const GROUPS: Group[] = [
  { label: "Inbox", states: ["awaiting_input"] },
  { label: "Running", states: ["running", "pending", "cv_done", "cl_done"] },
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
      onClick={onSelect}
      className={`w-full text-left px-3 py-2 rounded flex items-center justify-between gap-2 hover:bg-gray-100 transition-colors ${
        isSelected ? "bg-blue-50 border border-blue-200" : ""
      }`}
    >
      <span className="text-sm font-medium text-gray-800 truncate min-w-0">
        {job.company} — {job.role}
      </span>
      <StatusBadge state={job.state} />
    </button>
  );
}

export function JobList() {
  const jobs = useStore((s) => Object.values(s.jobs));
  const selectedId = useStore((s) => s.selectedId);
  const selectJob = useStore((s) => s.selectJob);

  return (
    <nav className="flex flex-col gap-4 p-3">
      {GROUPS.map((group) => {
        const groupJobs = jobs.filter((j) =>
          (group.states as string[]).includes(j.state)
        );
        if (groupJobs.length === 0) return null;
        return (
          <section key={group.label}>
            <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1 px-1">
              {group.label}
            </h2>
            <div className="flex flex-col gap-0.5">
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
        <p className="text-sm text-gray-400 italic px-1">No jobs loaded.</p>
      )}
    </nav>
  );
}
