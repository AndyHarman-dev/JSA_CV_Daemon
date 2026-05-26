import { useEffect } from "react";
import { useStore } from "./store";
import { connectWS } from "./ws";
import { Header } from "./components/Header";
import { JobList } from "./components/JobList";
import { JobDetail } from "./components/JobDetail";

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
