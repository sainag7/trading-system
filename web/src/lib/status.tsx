import { createContext, useContext, useEffect, useRef, useState, ReactNode } from "react";
import { api } from "./api";

export interface Status {
  market: { open: boolean; trading_day: boolean; label: string; now_et: string };
  backend: string;
  data_ok: boolean;
  healthy: boolean;
  warnings: string[];
  mode: string;
  kill_switch: { engaged: boolean; pinned: boolean };
  accounts: { role: string; number: string | null; label: string }[];
  latest_run: { run_id: string; mode: string; ts_display: string } | null;
  /** When the server process started. Absent on servers older than this field. */
  server_started_ts?: string;
}

const Ctx = createContext<{ status: Status | null; stale: boolean; reload: () => void }>({
  status: null, stale: false, reload: () => {},
});
export const useStatus = () => useContext(Ctx);

/**
 * True when this bundle is newer than the server process serving it.
 *
 * FastAPI mounts the frontend as StaticFiles on a directory, so it re-reads the
 * build from disk on every request. A long-running server therefore keeps
 * serving a rebuilt UI whose new API routes it has never registered — the UI
 * calls them, gets a 404, and (before this check) just looked like missing data.
 *
 * A MISSING `server_started_ts` also counts as stale: only a server predating
 * this field can omit it, which is exactly the case worth catching, and it means
 * the check works on the very first deploy without a version to bump.
 */
function isStale(status: Status | null): boolean {
  if (!status) return false;                 // nothing fetched yet — say nothing
  if (import.meta.env.DEV) return false;     // dev server rebuilds constantly
  if (!status.server_started_ts) return true;
  // Compare as instants, not strings: Python writes "+00:00" and the build stamp
  // writes "Z", so a lexical compare of two equal instants would disagree.
  const built = Date.parse(__BUILD_TIME__);
  const started = Date.parse(status.server_started_ts);
  if (Number.isNaN(built) || Number.isNaN(started)) return false;
  return built > started;
}

export function StatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const timer = useRef<number>();

  const reload = () => { api.status().then(setStatus).catch(() => {}); };

  useEffect(() => {
    reload();
    timer.current = window.setInterval(reload, 20000);
    return () => window.clearInterval(timer.current);
  }, []);

  return (
    <Ctx.Provider value={{ status, stale: isStale(status), reload }}>
      {children}
    </Ctx.Provider>
  );
}
