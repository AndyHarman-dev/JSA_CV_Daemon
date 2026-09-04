import { useEffect } from "react";
import { useStore } from "./store";
import { connectWS } from "./ws";
import { Header } from "./components/Header";
import { JobList } from "./components/JobList";
import { JobDetail } from "./components/JobDetail";
import { CvEditor } from "./components/cv-editor/CvEditor";
import { ScratchBuffer } from "./components/ScratchBuffer";
import { BootGate } from "./components/BootGate";
import { Toast } from "./components/Toast";
import { BaseCvPicker } from "./components/BaseCvPicker";
import { PromptInjector } from "./components/PromptInjector";
import { SHELL_THEME } from "./theme/tokens";
import { Ambient } from "./theme/Ambient";

const T = SHELL_THEME;

function App() {
  const refetchAll = useStore((s) => s.refetchAll);
  const hydrateLanguage = useStore((s) => s.hydrateLanguage);
  const hydrateCvDecks = useStore((s) => s.hydrateCvDecks);
  const selectedId = useStore((s) => s.selectedId);
  const editorOpen = useStore((s) => s.editorOpen);
  const configReady = useStore((s) => s.configReady);
  const hydrateInjectionPresets = useStore((s) => s.hydrateInjectionPresets);

  useEffect(() => {
    refetchAll().catch((err: unknown) => {
      console.error("Initial refetchAll failed:", err);
    });
    hydrateLanguage().catch((err: unknown) => {
      console.error("Initial hydrateLanguage failed:", err);
    });
    // Both of these are deliberately NOT folded into hydrateLanguage's timed
    // /api/config race — the deck list and the preset library are conveniences the boot
    // path must never wait on (see CLAUDE.md's note on keeping listing fetches off the
    // config round-trip). They are also independent of each other, so they are fired
    // side by side rather than chained.
    hydrateCvDecks().catch((err: unknown) => {
      console.error("Initial hydrateCvDecks failed:", err);
    });
    hydrateInjectionPresets().catch((err: unknown) => {
      console.error("Initial hydrateInjectionPresets failed:", err);
    });
    connectWS();
  }, [refetchAll, hydrateLanguage, hydrateCvDecks, hydrateInjectionPresets]);

  return (
    <div
      className="flex flex-col h-screen"
      style={{ background: T.canvas, color: T.ink, position: "relative", overflow: "hidden" }}
    >
      <style>{`:root{--a:${T.a}}`}</style>
      <BootGate />
      {/* Gate the dashboard on configReady: until GET /api/config resolves, `selectLanguageMode`
          isn't known yet, so painting the dashboard here would flash it for one frame before
          BootGate slams shut on top — the exact flicker the --select-language gate exists to
          prevent. Before that, this is just the blank canvas background above. */}
      {configReady && (
        <>
          {editorOpen && <CvEditor />}
          <ScratchBuffer />
          <Toast />
          <BaseCvPicker />
          <PromptInjector />
          <Ambient T={T} label="JSA_DAEMON" />
          <Header />
          <div className="flex flex-1 overflow-hidden" style={{ position: "relative", zIndex: 1 }}>
            {/* Left rail — always visible on desktop; hidden on mobile when a job is selected */}
            <aside
              className={`
                w-full md:w-[290px] flex-shrink-0 overflow-y-auto
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
        </>
      )}
    </div>
  );
}

export default App;
