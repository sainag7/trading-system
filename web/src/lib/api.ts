// Thin fetch wrapper around the FastAPI backend. Same-origin in production;
// the Vite dev server proxies /api to the local server.

const BASE = "/api";

async function req<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

const get = <T = any>(p: string) => req<T>(p);
const post = <T = any>(p: string, body?: any) =>
  req<T>(p, { method: "POST", body: JSON.stringify(body ?? {}) });
const put = <T = any>(p: string, body?: any) =>
  req<T>(p, { method: "PUT", body: JSON.stringify(body ?? {}) });
const del = <T = any>(p: string) => req<T>(p, { method: "DELETE" });

export const api = {
  // read-only
  status: () => get("/status"),
  ideas: () => get("/ideas"),
  ideaDetail: (t: string) => get(`/ideas/${encodeURIComponent(t)}`),
  deepdiveTickers: () => get("/deepdive/tickers"),
  deepdiveReports: (t: string) => get(`/deepdive/${encodeURIComponent(t)}/reports`),
  portfolio: (account?: string) =>
    get("/portfolio" + (account ? `?account=${encodeURIComponent(account)}` : "")),
  activity: () => get("/activity"),
  config: () => get("/config"),

  // config edit
  putConfig: (patch: any) => put("/config", patch),
  resetConfig: () => post("/config/reset"),

  // kill switch
  engageKill: () => post("/killswitch"),
  releaseKill: () => del("/killswitch"),

  // deep-dive remove/restore
  hideTicker: (t: string) => del(`/deepdive/${encodeURIComponent(t)}`),
  unhideTicker: (t: string) => post(`/deepdive/${encodeURIComponent(t)}/unhide`),

  // engine jobs
  scan: (account?: string) => post("/scan", { account }),
  research: (ticker: string) => post("/deepdive/research", { ticker }),

  // trading
  tradePlan: (mode: string, account?: string) =>
    post("/trade/plan", { mode, account }),
  tradeExecute: (plan_id: string, approved_indices: number[]) =>
    post("/trade/execute", { plan_id, approved_indices }),
  tradeLive: (confirm: string, account?: string) =>
    post("/trade/live", { confirm, account }),
  tradeDiscard: (plan_id: string) => post("/trade/discard", { plan_id }),
  tradePending: () => get("/trade/pending"),

  // automation
  schedule: () => get("/schedule"),
  scheduleInstall: (enable_live: boolean) => post("/schedule/install", { enable_live }),
  scheduleUninstall: () => del("/schedule"),
  diagnostics: () => get("/diagnostics"),
  runDiagnostic: (name: string) => post(`/diagnostics/${name}`),

  // jobs
  job: (id: string) => get(`/jobs/${id}`),
  jobStreamUrl: (id: string) => `${BASE}/jobs/${id}/stream`,
};
