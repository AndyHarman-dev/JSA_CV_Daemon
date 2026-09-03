import { AgentThread } from "./AgentThread";

interface Props {
  jobId: string;
}

// Thin wrapper kept for its existing call sites (JobDetail's `awaiting_input` branch) —
// all rendering and open-FollowUp derivation now live in AgentThread, which reads the
// full transcript instead of the single-question shape this pane used to hold.
export function FollowUpPane({ jobId }: Props) {
  return <AgentThread jobId={jobId} mode="answer" />;
}
