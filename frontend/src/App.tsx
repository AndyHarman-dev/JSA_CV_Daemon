import { useEffect } from "react";
import { useStore } from "./store";
import { connectWS } from "./ws";
import { Header } from "./components/Header";
import { JobList } from "./components/JobList";
import { JobDetail } from "./components/JobDetail";

function App() {
  const refetchAll = useStore((s) => s.refetchAll);
  const selectedId = useStore((s) => s.selectedId);

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
        {/* Left rail — always visible on desktop; hidden on mobile when a job is selected */}
        <aside
          className={`
            w-full md:w-80 flex-shrink-0 bg-white border-r border-gray-200 overflow-y-auto
            ${selectedId !== undefined ? "hidden md:block" : "block"}
          `}
        >
          <JobList />
        </aside>
        {/* Right pane — always visible on desktop; hidden on mobile when no job is selected */}
        <main
          className={`
            flex-1 overflow-y-auto
            ${selectedId !== undefined ? "block" : "hidden md:block"}
          `}
        >
          <JobDetail />
        </main>
      </div>
    </div>
  );
}

export default App;
