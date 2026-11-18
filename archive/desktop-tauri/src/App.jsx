import { useState } from "react";
import SourcesScreen from "./screens/SourcesScreen";
import QueryScreen from "./screens/QueryScreen";
import CitationExplorerScreen from "./screens/CitationExplorerScreen";
import SettingsScreen from "./screens/SettingsScreen";

const NAV_ITEMS = [
  { id: "sources", label: "Sources", icon: "\u{1F4C1}" }, // folder
  { id: "query", label: "Query", icon: "\u{1F4AC}" }, // speech balloon
  { id: "citations", label: "Citations", icon: "\u{1F50D}" }, // magnifying glass
  { id: "settings", label: "Settings", icon: "\u{2699}\u{FE0F}" }, // gear
];

function App() {
  const [activeScreen, setActiveScreen] = useState("sources");
  // Lifted so clicking a CitationChip in QueryScreen can navigate to the
  // Citation Explorer with that citation pre-selected/expanded.
  const [selectedCitation, setSelectedCitation] = useState(null);

  const navigateToCitation = (citation) => {
    setSelectedCitation(citation);
    setActiveScreen("citations");
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-slate-100 text-slate-900">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="flex items-center gap-2 px-5 py-5">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-slate-900 text-sm font-bold text-white">
            A
          </span>
          <span className="text-base font-semibold text-slate-900">Attest</span>
        </div>

        <nav className="flex-1 space-y-1 px-3">
          {NAV_ITEMS.map((item) => {
            const isActive = activeScreen === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setActiveScreen(item.id)}
                className={`flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-slate-900 text-white"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                }`}
              >
                <span aria-hidden="true">{item.icon}</span>
                {item.label}
              </button>
            );
          })}
        </nav>

        <div className="border-t border-slate-200 px-5 py-4 text-xs text-slate-400">
          Local-first, evidence-backed
          <br />
          UI preview &middot; mock data only
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto px-8 py-8">
        {activeScreen === "sources" && <SourcesScreen />}
        {activeScreen === "query" && <QueryScreen onSelectCitation={navigateToCitation} />}
        {activeScreen === "citations" && (
          <CitationExplorerScreen selectedCitation={selectedCitation} />
        )}
        {activeScreen === "settings" && <SettingsScreen />}
      </main>
    </div>
  );
}

export default App;
