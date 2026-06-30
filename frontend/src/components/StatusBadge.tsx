import type { JobState } from "../types";
import { SHELL_THEME } from "../theme/tokens";
import { Badge } from "../theme/chrome";

interface StatusBadgeProps {
  state: JobState;
  className?: string;
}

export function StatusBadge({ state, className = "" }: StatusBadgeProps) {
  return <Badge T={SHELL_THEME} state={state} className={className} />;
}
