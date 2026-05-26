import { useEffect } from "react";
import { useStore } from "./store";
import { connectWS } from "./ws";
import { JobList } from "./components/JobList";
import { JobDetail } from "./components/JobDetail";

const WS_STATUS_DOT: Record<
  "connecting" | "open" | "closed",
  { color: string; label: string }
> = {
  connecting: { color: "bg-yellow-400", label: "Connecting" },
  open: { color: "bg-green-500", label: "Connected" },
  closed: { color: "bg-red-500", label: "Disconnected" },
};

function Header() {
  const wsStatus = useStore((s) => s.wsStatus);
  const dot = WS_STATUS_DOT[wsStatus];

  return (
    <header className="flex items-center justify-between px-4 py-2 bg-white border-b border-gray-200 flex-shrink-0">
      <span className="font-semibold text-gray-800 text-sm tracking-tight">
        JSA — Job Search Assistant
      </span>
      <div className="flex items-center gap-1.5">
        <div className={`w-2.5 h-2.5 rounded-full ${dot.color}`} />
        <span className="text-xs text-gray-500">{dot.label}</span>
      </div>
    </header>
  );
}

function App() {
  const refetchAll = useStore((s) => s.refetchAll);

  useEffect(() => {
    refetchAll().catch((err: unknown) => {
      console.error("Initial refetchAll failed:", err);
    });
    connectWS();
  }, [refetchAll]);

  return (
    <div className="flex flex-col h-screen bg-gray-50">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        {/* Left rail */}
        <aside className="w-80 flex-shrink-0 bg-white border-r border-gray-200 overflow-y-auto">
          <JobList />
        </aside>
        {/* Right pane */}
        <main className="flex-1 overflow-y-auto">
          <JobDetail />
        </main>
      </div>
    </div>
  );
}

export default App;
