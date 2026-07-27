import {
  createContext, useCallback, useContext, useRef, useState, ReactNode,
} from "react";
import { api } from "./api";

export interface JobState {
  id: string | null;
  kind: string;
  label: string;
  status: "starting" | "running" | "done" | "error";
  log: string;
  result: any;
  error: string | null;
}

interface JobCtx {
  job: JobState | null;
  busy: boolean;
  runJob: (
    start: () => Promise<any>,
    opts?: { label?: string; onDone?: (job: JobState) => void }
  ) => Promise<void>;
  dismiss: () => void;
}

const Ctx = createContext<JobCtx>(null as any);
export const useJob = () => useContext(Ctx);

export function JobProvider({ children }: { children: ReactNode }) {
  const [job, setJob] = useState<JobState | null>(null);
  const esRef = useRef<EventSource | null>(null);

  const closeEs = () => {
    esRef.current?.close();
    esRef.current = null;
  };

  const runJob = useCallback(
    async (start: () => Promise<any>, opts?: { label?: string; onDone?: (j: JobState) => void }) => {
      closeEs();
      setJob({
        id: null, kind: "", label: opts?.label ?? "Working…",
        status: "starting", log: "", result: null, error: null,
      });
      let started: any;
      try {
        started = await start();
      } catch (e: any) {
        setJob((j) => j && { ...j, status: "error", error: e.message ?? String(e) });
        return;
      }
      const id: string = started.id;
      const base: JobState = {
        id, kind: started.kind ?? "", label: started.label ?? opts?.label ?? "Working…",
        status: "running", log: "", result: null, error: null,
      };
      setJob(base);

      const es = new EventSource(api.jobStreamUrl(id));
      esRef.current = es;
      es.addEventListener("log", (ev: MessageEvent) => {
        setJob((j) => (j && j.id === id ? { ...j, log: j.log + ev.data } : j));
      });
      es.addEventListener("status", async (ev: MessageEvent) => {
        const finalStatus = ev.data as "done" | "error";
        closeEs();
        let result: any = null;
        let error: string | null = null;
        try {
          const full = await api.job(id);
          result = full.result;
          error = full.error;
        } catch {
          /* ignore */
        }
        setJob((j) => {
          if (!j || j.id !== id) return j;
          const next = { ...j, status: finalStatus, result, error };
          opts?.onDone?.(next);
          return next;
        });
      });
      es.onerror = () => {
        // The stream closes normally when a job finishes; only treat as an error
        // while still running.
        setJob((j) => {
          if (j && j.id === id && j.status === "running") {
            closeEs();
            return { ...j, status: "error", error: "lost connection to job stream" };
          }
          return j;
        });
      };
    },
    []
  );

  const dismiss = useCallback(() => {
    closeEs();
    setJob(null);
  }, []);

  const busy = !!job && (job.status === "running" || job.status === "starting");

  return <Ctx.Provider value={{ job, busy, runJob, dismiss }}>{children}</Ctx.Provider>;
}
