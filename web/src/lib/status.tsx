import { createContext, useContext, useEffect, useRef, useState, ReactNode } from "react";
import { api } from "./api";

export interface Status {
  market: { open: boolean; trading_day: boolean; label: string; now_et: string };
  backend: string;
  data_ok: boolean;
  healthy: boolean;
  warnings: string[];
  mode: string;
  profile: string;
  profiles: string[];
  kill_switch: { engaged: boolean; pinned: boolean };
  accounts: { role: string; number: string | null; label: string }[];
  latest_run: { run_id: string; mode: string; profile: string; ts_display: string } | null;
}

const Ctx = createContext<{ status: Status | null; reload: () => void }>({
  status: null, reload: () => {},
});
export const useStatus = () => useContext(Ctx);

export function StatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const timer = useRef<number>();

  const reload = () => { api.status().then(setStatus).catch(() => {}); };

  useEffect(() => {
    reload();
    timer.current = window.setInterval(reload, 20000);
    return () => window.clearInterval(timer.current);
  }, []);

  return <Ctx.Provider value={{ status, reload }}>{children}</Ctx.Provider>;
}
