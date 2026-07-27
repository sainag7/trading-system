import { useState } from "react";
import {
  CalendarClock, Play, Trash2, Zap, Stethoscope, CheckCircle2, XCircle, Loader2,
} from "lucide-react";
import { api } from "../lib/api";
import { useJob } from "../lib/useJob";
import { Card, CardHead, useFetch, Callout, Modal } from "../components/ui";

export function Automation() {
  return (
    <div className="view">
      <div className="view-head">
        <h1>Automation</h1>
        <p>Manage the daily schedule and run diagnostics — all from here.</p>
      </div>
      <ScheduleCard />
      <div style={{ height: 16 }} />
      <DiagnosticsCard />
    </div>
  );
}

function ScheduleCard() {
  const { data, loading, reload } = useFetch(() => api.schedule(), []);
  const [working, setWorking] = useState(false);
  const [liveOpen, setLiveOpen] = useState(false);
  const [output, setOutput] = useState<string>("");

  const run = async (fn: () => Promise<any>) => {
    setWorking(true);
    try { const r = await fn(); setOutput(r.output || ""); reload(); }
    catch (e: any) { setOutput(e.message ?? "failed"); }
    finally { setWorking(false); }
  };

  return (
    <Card>
      <CardHead title="Daily schedule">
        <CalendarClock size={16} className="muted" />
      </CardHead>
      <div className="card-pad">
        {loading ? <span className="muted">Loading…</span> :
          !data?.supported ? (
            <Callout kind="info"><div>Scheduling is macOS-only (launchd). Not available on this machine.</div></Callout>
          ) : (
            <>
              <table className="tbl" style={{ marginBottom: 14 }}>
                <thead><tr><th>Job</th><th>Schedule</th><th>Status</th></tr></thead>
                <tbody>
                  {data.jobs.map((j: any) => (
                    <tr key={j.label}>
                      <td><div style={{ fontWeight: 600, textTransform: "capitalize" }}>{j.role} · {j.mode}</div>
                        <div className="small muted">{j.desc}</div></td>
                      <td className="muted small">{j.schedule}</td>
                      <td><span className={`badge ${j.installed ? "badge-green" : "badge-neutral"}`}>
                        {j.installed ? "installed" : "not installed"}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="row wrap">
                <button className="btn btn-primary" disabled={working}
                  onClick={() => run(() => api.scheduleInstall(false))}>
                  <Play size={15} /> Install advice job
                </button>
                <button className="btn btn-accent" disabled={working} onClick={() => setLiveOpen(true)}>
                  <Zap size={15} /> Enable autonomous live job
                </button>
                <button className="btn" disabled={working || !data.any_installed}
                  onClick={() => run(() => api.scheduleUninstall())}>
                  <Trash2 size={15} /> Remove all
                </button>
              </div>
              <p className="small muted" style={{ marginTop: 10 }}>
                The advice job runs weekdays 10:00 (read-only). The autonomous live job places real
                orders on the $100 agentic account — enable it only after a supervised commissioning run.
              </p>
              {output && <pre className="log" style={{ marginTop: 12, maxHeight: 180, overflow: "auto" }}>{output}</pre>}
              {data.log_tail && (
                <details style={{ marginTop: 12 }}>
                  <summary className="muted small" style={{ cursor: "pointer" }}>Scheduled run log</summary>
                  <pre className="log" style={{ marginTop: 8, maxHeight: 220, overflow: "auto" }}>{data.log_tail}</pre>
                </details>
              )}
            </>
          )}
      </div>

      {liveOpen && (
        <Modal title="Enable autonomous live trading" onClose={() => setLiveOpen(false)}>
          <Callout kind="danger"><Zap size={16} />
            <div>This schedules <strong>unattended real-money trading</strong> every weekday on the
              agentic ($100) account — no per-order confirmation. Only enable after commissioning.</div></Callout>
          <div className="row between" style={{ marginTop: 16 }}>
            <button className="btn" onClick={() => setLiveOpen(false)}>Cancel</button>
            <button className="btn btn-danger" disabled={working}
              onClick={() => { setLiveOpen(false); run(() => api.scheduleInstall(true)); }}>
              <Zap size={15} /> Enable live job
            </button>
          </div>
        </Modal>
      )}
    </Card>
  );
}

function DiagnosticsCard() {
  const { data } = useFetch(() => api.diagnostics(), []);
  const { runJob, busy, job } = useJob();
  const [results, setResults] = useState<Record<string, any>>({});

  const runCheck = (name: string) =>
    runJob(() => api.runDiagnostic(name), {
      label: `diagnostic: ${name}`,
      onDone: (j) => setResults((r) => ({ ...r, [name]: j.result })),
    });

  return (
    <Card>
      <CardHead title="Diagnostics"><Stethoscope size={16} className="muted" /></CardHead>
      <div className="card-pad">
        <p className="small muted" style={{ marginTop: 0 }}>
          Offline self-checks — hermetic, place nothing. Verify the pipeline and safety layers.
        </p>
        <table className="tbl">
          <tbody>
            {(data?.checks ?? []).map((c: any) => {
              const res = results[c.name];
              const running = busy && job?.label === `diagnostic: ${c.name}`;
              return (
                <tr key={c.name}>
                  <td style={{ fontWeight: 600, textTransform: "capitalize" }}>{c.name}</td>
                  <td className="muted small">{c.label}</td>
                  <td style={{ width: 90 }}>
                    {running ? <Loader2 className="spin" size={15} /> :
                      res ? (res.ok
                        ? <span className="badge badge-green"><CheckCircle2 size={12} /> pass</span>
                        : <span className="badge badge-red"><XCircle size={12} /> fail</span>) : null}
                  </td>
                  <td style={{ width: 90, textAlign: "right" }}>
                    <button className="btn btn-sm" disabled={busy} onClick={() => runCheck(c.name)}>Run</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
