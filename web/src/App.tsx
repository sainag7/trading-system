import { useEffect, useState } from "react";
import { CandlestickChart } from "lucide-react";
import { NAV, TITLES, View } from "./nav";
import { StatusProvider } from "./lib/status";
import { JobProvider } from "./lib/useJob";
import { TopBar, StaleBanner } from "./components/TopBar";
import { LogDrawer } from "./components/LogDrawer";
import { Overview } from "./views/Overview";
import { Ideas } from "./views/Ideas";
import { DeepDive } from "./views/DeepDive";
import { Portfolio } from "./views/Portfolio";
import { Trade } from "./views/Trade";
import { Automation } from "./views/Automation";
import { Activity } from "./views/Activity";
import { Usage } from "./views/Usage";
import { Settings } from "./views/Settings";

const validView = (h: string): View => {
  const v = h.replace(/^#\/?/, "") as View;
  return NAV.some((n) => n.id === v) ? v : "overview";
};

export default function App() {
  const [view, setViewState] = useState<View>(() => validView(window.location.hash));

  const setView = (v: View) => { window.location.hash = v; setViewState(v); };

  // Keep view in sync with the URL hash (back/forward + deep links).
  useEffect(() => {
    const onHash = () => setViewState(validView(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <StatusProvider>
      <JobProvider>
        <div className="app">
          <aside className="sidebar">
            <div className="brand">
              <span className="brand-mark"><CandlestickChart size={18} /></span>
              Trading System
            </div>
            {NAV.map((n) => (
              <button key={n.id} className={`nav-item ${view === n.id ? "active" : ""}`}
                onClick={() => setView(n.id)}>
                <n.icon size={17} /> {n.label}
              </button>
            ))}
            <div className="nav-spacer" />
            <div className="nav-foot">
              Advice, research & trading — all local. Binds to localhost only.
            </div>
          </aside>

          <TopBar title={TITLES[view]} />

          <main className="main">
            <StaleBanner />
            <nav className="mobile-nav">
              {NAV.map((n) => (
                <button key={n.id} className={`nav-item ${view === n.id ? "active" : ""}`}
                  style={{ width: "auto" }} onClick={() => setView(n.id)}>
                  <n.icon size={16} /> {n.label}
                </button>
              ))}
            </nav>
            <ViewSwitch view={view} onNavigate={setView} />
          </main>

          <LogDrawer />
        </div>
      </JobProvider>
    </StatusProvider>
  );
}

function ViewSwitch({ view, onNavigate }: { view: View; onNavigate: (v: View) => void }) {
  switch (view) {
    case "overview": return <Overview onNavigate={onNavigate} />;
    case "ideas": return <Ideas />;
    case "deepdive": return <DeepDive />;
    case "portfolio": return <Portfolio />;
    case "trade": return <Trade />;
    case "automation": return <Automation />;
    case "activity": return <Activity />;
    case "usage": return <Usage />;
    case "settings": return <Settings />;
  }
}
