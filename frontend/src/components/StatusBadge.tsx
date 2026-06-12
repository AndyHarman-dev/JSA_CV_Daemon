import type { JobState } from "../types";

interface StatusBadgeProps {
  state: JobState;
  className?: string;
}

const STATE_CONFIG: Record<
  JobState,
  { label: string; classes: string; spinner?: boolean }
> = {
  pending: { label: "Pending", classes: "bg-gray-200 text-gray-700" },
  running: {
    label: "Running",
    classes: "bg-blue-100 text-blue-700",
    spinner: true,
  },
  awaiting_input: { label: "Needs Input", classes: "bg-yellow-100 text-yellow-700" },
  fit_done: { label: "Assessed", classes: "bg-blue-100 text-blue-700" },
  unfit: { label: "Needs Review", classes: "bg-amber-100 text-amber-800" },
  cv_done: { label: "CV Done", classes: "bg-teal-100 text-teal-700" },
  cl_done: { label: "CL Done", classes: "bg-teal-100 text-teal-700" },
  review: { label: "Review", classes: "bg-purple-100 text-purple-700" },
  approved: { label: "Approved", classes: "bg-green-100 text-green-700" },
  failed: { label: "Failed", classes: "bg-red-100 text-red-700" },
  dismissed: { label: "Dismissed", classes: "bg-gray-100 text-gray-500" },
};

export function StatusBadge({ state, className = "" }: StatusBadgeProps) {
  const config = STATE_CONFIG[state];
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${config.classes} ${className}`}
    >
      {config.spinner && (
        <svg
          className="animate-spin h-3 w-3"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8v8H4z"
          />
        </svg>
      )}
      {config.label}
    </span>
  );
}
