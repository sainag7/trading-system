import { useState } from "react";
import {
  Sun, Moon, Circle, ShieldAlert, ShieldCheck, Wifi, WifiOff, Cpu,
} from "lucide-react";
import { useStatus } from "../lib/status";
import { api } from "../lib/api";
import { Callout } from "./ui";

/**
 * Warns that this UI is newer than the server process behind it.
 *
 * Rendered inside <main> rather than in the TopBar header, which is a
 * single-row flex bar. Without this, a stale server's missing endpoints just
 * 404 and each view renders as though it had no data.
 */
export function StaleBanner() {
  const { stale } = useStatus();
  if (!stale) return null;
  return (
    <Callout kind="warn">
      This dashboard was rebuilt after the server started, so newer features may
      have no API behind them and will look empty. Restart it — quit the server
      and re-run <code>./start.command</code>.
    </Callout>
  );
}

function useTheme() {
  const [theme, setTheme] = useState<string>(
    () => document.documentElement.getAttribute("data-theme") || "system"
  );
  const set = (t: string) => {
    if (t === "system") {
      document.documentElement.removeAttribute("data-theme");
      localStorage.removeItem("theme");
    } else {
      document.documentElement.setAttribute("data-theme", t);
      localStorage.setItem("theme", t);
    }
    setTheme(t);
  };
  const isDark = theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  return { isDark, toggle: () => set(isDark ? "light" : "dark") };
}

export function TopBar({ title }: { title: string }) {
  const { status, reload } = useStatus();
  const { isDark, toggle } = useTheme();
  const [busy, setBusy] = useState(false);
  const ks = status?.kill_switch;

  const toggleKill = async () => {
    setBusy(true);
    try {
      if (ks?.engaged) await api.releaseKill();
      else await api.engageKill();
      reload();
    } catch (e: any) {
      alert(e.message ?? "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <header className="topbar">
      <h2 style={{ fontSize: 16 }}>{title}</h2>
      <div className="spacer" />

      {status && (
        <>
          <span className="pill" title={status.market.now_et}>
            <Circle size={9} fill={status.market.open ? "var(--green)" : "var(--text-faint)"}
              color={status.market.open ? "var(--green)" : "var(--text-faint)"} />
            Market {status.market.label}
          </span>
          <span className="pill" title="LLM backend">
            <Cpu size={13} /> {status.backend}
          </span>
          <span className="pill" title={status.data_ok ? "Market data OK" : "No market data"}>
            {status.data_ok ? <Wifi size={13} /> : <WifiOff size={13} color="var(--amber)" />}
            data {status.data_ok ? "ok" : "none"}
          </span>
          <button
            className={`btn btn-sm ${ks?.engaged ? "btn-danger" : ""}`}
            onClick={toggleKill}
            disabled={busy || (ks?.engaged && ks?.pinned)}
            title={ks?.pinned ? "Pinned in config — release there" : "Emergency halt for all trading"}
          >
            {ks?.engaged ? <ShieldAlert size={15} /> : <ShieldCheck size={15} />}
            {ks?.engaged ? "Kill: ON" : "Kill switch"}
          </button>
        </>
      )}

      <button className="btn btn-icon btn-ghost" onClick={toggle}
        aria-label="Toggle theme" title="Toggle light/dark">
        {isDark ? <Sun size={16} /> : <Moon size={16} />}
      </button>
    </header>
  );
}
