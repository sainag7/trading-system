import { useState } from "react";
import { Play, TrendingUp, Search, ArrowRight, AlertTriangle } from "lucide-react";
import { api } from "../lib/api";
import { useStatus } from "../lib/status";
import { useJob } from "../lib/useJob";
import { Card, CardHead, Stat, ActionBadge, ScoreBar, useFetch, Callout, Empty } from "../components/ui";
import { View } from "../nav";

export function Overview({ onNavigate }: { onNavigate: (v: View) => void }) {
  const { status, reload } = useStatus();
  const { runJob, busy } = useJob();
  const ideas = useFetch(() => api.ideas(), []);
  const [profile, setProfile] = useState<string>("");

  const prof = profile || status?.profile || "swing";

  const runScan = () =>
    runJob(() => api.scan(prof), {
      label: `${prof} scan`,
      onDone: () => { reload(); ideas.reload(); },
    });

  const topIdeas = (ideas.data?.rows ?? [])
    .filter((r: any) => ["buy", "add", "trim"].includes(r.action))
    .slice(0, 6);

  return (
    <div className="view">
      <div className="view-head">
        <h1>Overview</h1>
        <p>Your trading system at a glance — run a scan, review ideas, act.</p>
      </div>

      {status && status.warnings.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Callout kind="warn">
            <AlertTriangle size={16} />
            <div>{status.warnings.join("  ·  ")}</div>
          </Callout>
        </div>
      )}

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label="Market" value={status?.market.label ?? "—"} sub={status?.market.now_et} />
        <Stat label="Default mode" value={status?.mode ?? "—"} sub={`profile ${status?.profile ?? "—"}`} />
        <Stat label="LLM backend" value={status?.backend ?? "—"}
          sub={status?.data_ok ? "market data ok" : "no market data"} />
        <Stat label="Last scan" value={status?.latest_run?.profile ?? "none yet"}
          sub={status?.latest_run?.ts_display ?? "run a scan to begin"} />
      </div>

      <div className="grid grid-2" style={{ marginBottom: 16, gridTemplateColumns: "1.1fr 1fr" }}>
        <Card>
          <CardHead title="Quick actions" />
          <div className="card-pad">
            <div className="row wrap" style={{ marginBottom: 12 }}>
              <select className="input" style={{ width: 150 }} value={prof}
                onChange={(e) => setProfile(e.target.value)}>
                {(status?.profiles ?? ["swing"]).map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <button className="btn btn-primary" onClick={runScan} disabled={busy}>
                <Play size={15} /> Run scan (advice)
              </button>
            </div>
            <div className="row wrap">
              <button className="btn" onClick={() => onNavigate("trade")}>
                <TrendingUp size={15} /> Plan trades
              </button>
              <button className="btn" onClick={() => onNavigate("deepdive")}>
                <Search size={15} /> Deep dive a stock
              </button>
            </div>
            <p className="small muted" style={{ marginTop: 12 }}>
              A scan runs the full pipeline in <strong>advice mode</strong> — it never places a
              trade. Head to <a onClick={() => onNavigate("trade")}>Trade</a> to place orders with
              per-order approval.
            </p>
          </div>
        </Card>

        <Card>
          <CardHead title="Account">
            <button className="btn btn-sm btn-ghost" onClick={() => onNavigate("portfolio")}>
              Portfolio <ArrowRight size={13} />
            </button>
          </CardHead>
          <div className="card-pad">
            {status?.accounts.length ? (
              <table className="tbl">
                <tbody>
                  {status.accounts.map((a) => (
                    <tr key={a.role}>
                      <td style={{ textTransform: "capitalize", fontWeight: 600 }}>{a.role}</td>
                      <td className="mono muted">{a.number ?? "—"}</td>
                      <td className="muted">
                        {a.role === "agentic" ? "autonomous ($100)" : "advice / manual"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="muted small">Single-account mode (no accounts block configured).</p>}
          </div>
        </Card>
      </div>

      <Card>
        <CardHead title="Top ideas">
          <button className="btn btn-sm btn-ghost" onClick={() => onNavigate("ideas")}>
            All ideas <ArrowRight size={13} />
          </button>
        </CardHead>
        <div className="table-wrap">
          {topIdeas.length ? (
            <table className="tbl">
              <thead>
                <tr><th>Ticker</th><th>Action</th><th>Composite</th><th>Setup</th>
                  <th className="num">Conf</th><th className="num">Size</th></tr>
              </thead>
              <tbody>
                {topIdeas.map((r: any) => (
                  <tr key={r.ticker} className="clickable" onClick={() => onNavigate("ideas")}>
                    <td style={{ fontWeight: 650 }}>{r.ticker}</td>
                    <td><ActionBadge action={r.action} /></td>
                    <td><ScoreBar value={r.composite} /></td>
                    <td className="muted">{r.setup}</td>
                    <td className="num">{r.conf ?? "—"}</td>
                    <td className="num mono">{r.target_display}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="card-pad">
              <Empty>No recommendations yet — run a scan to generate ideas.</Empty>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
