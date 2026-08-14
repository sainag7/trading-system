import { useEffect, useState } from "react";
import {
  ClipboardList, Zap, ShieldAlert, CheckCircle2, XCircle, AlertTriangle, Ban,
} from "lucide-react";
import { api } from "../lib/api";
import { useStatus } from "../lib/status";
import { useJob } from "../lib/useJob";
import { Card, CardHead, ActionBadge, Callout, Modal, Empty } from "../components/ui";

export function Trade() {
  const { status, reload } = useStatus();
  const { runJob, busy } = useJob();
  const [account, setAccount] = useState<string>("");
  const [plan, setPlan] = useState<any>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [halt, setHalt] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const [liveOpen, setLiveOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");

  const acct = account || (status?.accounts.find((a) => a.role === "agentic")?.role)
    || status?.accounts[0]?.role || "";

  // Recover a pending plan after a refresh.
  useEffect(() => {
    api.tradePending().then((p) => { if (p.pending) { setPlan(p); selectAll(p); } }).catch(() => {});
  }, []);

  const selectAll = (p: any) => setSelected(new Set((p.orders ?? []).map((o: any) => o.idx)));
  const toggle = (idx: number) => setSelected((s) => {
    const n = new Set(s); n.has(idx) ? n.delete(idx) : n.add(idx); return n;
  });

  const doPlan = () => {
    setHalt(null); setResult(null); setPlan(null);
    runJob(() => api.tradePlan("preview", acct), {
      label: "preview plan",
      onDone: (job) => {
        if (job.status === "error") return;
        const r = job.result;
        if (!r) return;
        if (r.halted) { setHalt(r.message || r.reason); return; }
        if (!r.can_execute) { setHalt(r.message || "Nothing cleared the guardrails — no orders to place."); return; }
        setPlan(r); selectAll(r);
      },
    });
  };

  const doExecute = () => {
    const indices = [...selected];
    runJob(() => api.tradeExecute(plan.plan_id, indices), {
      label: "placing approved orders",
      onDone: (job) => {
        if (job.status === "error") return;
        setResult(job.result); setPlan(null); reload();
      },
    });
  };

  const doDiscard = async () => {
    if (plan?.plan_id) await api.tradeDiscard(plan.plan_id);
    setPlan(null); setResult(null);
  };

  const doLive = () => {
    setLiveOpen(false); setConfirmText(""); setHalt(null); setResult(null);
    runJob(() => api.tradeLive("I UNDERSTAND", acct), {
      label: "placing LIVE orders",
      onDone: (job) => {
        if (job.status === "error") return;
        const r = job.result;
        if (r?.halted) { setHalt(r.message || r.reason); return; }
        setResult(r); reload();
      },
    });
  };

  return (
    <div className="view">
      <div className="view-head">
        <h1>Trade</h1>
        <p>Plan orders, approve them one by one, then place — or run live in one click.
          Every order passes the guardrails and re-checks the kill switch.</p>
      </div>

      {status?.kill_switch.engaged && (
        <div style={{ marginBottom: 14 }}>
          <Callout kind="danger"><ShieldAlert size={16} />
            <div>Kill switch is ON — trading is halted. Release it (top bar) to place orders.</div></Callout>
        </div>
      )}

      <Card>
        <CardHead title="Set up a run" />
        <div className="card-pad">
          <div className="row wrap" style={{ gap: 14 }}>
            <label className="field" style={{ margin: 0 }}>
              <span>Account</span>
              <select className="input" style={{ width: 200 }} value={acct}
                onChange={(e) => setAccount(e.target.value)}>
                {(status?.accounts ?? []).map((a) => (
                  <option key={a.role} value={a.role}>{a.label}</option>
                ))}
                {!status?.accounts.length && <option value="">default</option>}
              </select>
            </label>
            <div className="row" style={{ alignSelf: "flex-end", gap: 10 }}>
              <button className="btn btn-primary" onClick={doPlan}
                disabled={busy || status?.kill_switch.engaged}>
                <ClipboardList size={15} /> Plan trades
              </button>
              <button className="btn btn-accent" onClick={() => setLiveOpen(true)}
                disabled={busy || status?.kill_switch.engaged}>
                <Zap size={15} /> Run live
              </button>
            </div>
          </div>
          <p className="small muted" style={{ marginTop: 12 }}>
            <strong>Plan trades</strong> proposes orders for your approval (nothing is placed until you
            click Place). <strong>Run live</strong> places every guardrail-approved order after you type a
            confirmation — real money on the <strong>{acct || "default"}</strong> account.
          </p>
        </div>
      </Card>

      {halt && (
        <div style={{ marginTop: 16 }}>
          <Callout kind="warn"><AlertTriangle size={16} /><div>{halt}</div></Callout>
        </div>
      )}

      {result && <ResultCard result={result} />}

      {plan && (
        <div style={{ marginTop: 16 }}>
          <Card>
            <CardHead title={`Proposed orders · ${plan.mode} · ${plan.account ?? "default"}`}>
              <span className="small muted">
                equity {plan.account_summary.equity_display} · cash {plan.account_summary.cash_display}
              </span>
            </CardHead>
            <div className="table-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th style={{ width: 34 }}></th><th>Side</th><th>Ticker</th>
                    <th className="num">Shares</th><th className="num">~Price</th>
                    <th className="num">Notional</th><th>Rationale</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.orders.map((o: any) => (
                    <tr key={o.idx}>
                      <td>
                        <input type="checkbox" checked={selected.has(o.idx)}
                          onChange={() => toggle(o.idx)} aria-label={`approve ${o.ticker}`} />
                      </td>
                      <td><ActionBadge action={o.action} /></td>
                      <td style={{ fontWeight: 650 }}>{o.ticker}</td>
                      <td className="num mono">{o.shares_display}</td>
                      <td className="num mono">{o.price_display}</td>
                      <td className="num mono">{o.notional_display}
                        {o.resized && <span className="badge badge-amber" style={{ marginLeft: 6 }}>resized</span>}</td>
                      <td className="muted small" style={{ whiteSpace: "normal", maxWidth: 260 }}>{o.rationale}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card-pad row wrap between">
              <span className="small muted">{selected.size} of {plan.orders.length} selected</span>
              <div className="row">
                <button className="btn" onClick={doDiscard} disabled={busy}>Discard</button>
                <button className="btn btn-primary" onClick={doExecute} disabled={busy || !selected.size}>
                  <CheckCircle2 size={15} /> Place {selected.size} order{selected.size === 1 ? "" : "s"}
                </button>
              </div>
            </div>
          </Card>

          {plan.rejected?.length > 0 && (
            <details style={{ marginTop: 12 }}>
              <summary className="muted small" style={{ cursor: "pointer" }}>
                Blocked by guardrails ({plan.rejected.length})
              </summary>
              <Card className="" >
                <div className="table-wrap">
                  <table className="tbl">
                    <thead><tr><th>Side</th><th>Ticker</th><th>Reasons</th></tr></thead>
                    <tbody>
                      {plan.rejected.map((r: any, i: number) => (
                        <tr key={i} style={{ opacity: 0.75 }}>
                          <td>{r.side}</td><td style={{ fontWeight: 600 }}>{r.ticker}</td>
                          <td className="muted small">{(r.reasons || []).join("; ") || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </details>
          )}
        </div>
      )}

      {liveOpen && (
        <Modal title="Confirm live trading" onClose={() => setLiveOpen(false)}>
          <Callout kind="danger"><Zap size={16} />
            <div>This places <strong>real orders with real money</strong> via Robinhood on the{" "}
              <strong>{acct || "default"}</strong> account. They have passed the
              guardrails, but you are responsible.</div></Callout>
          <label className="field" style={{ marginTop: 16 }}>
            <span>Type <strong>I UNDERSTAND</strong> to proceed</span>
            <input className="input" value={confirmText} autoFocus
              onChange={(e) => setConfirmText(e.target.value)} placeholder="I UNDERSTAND" />
          </label>
          <div className="row between" style={{ marginTop: 8 }}>
            <button className="btn" onClick={() => setLiveOpen(false)}>Cancel</button>
            <button className="btn btn-danger" onClick={doLive}
              disabled={confirmText.trim().toUpperCase() !== "I UNDERSTAND"}>
              <Zap size={15} /> Place live orders
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function ResultCard({ result }: { result: any }) {
  const s = result.summary;
  return (
    <div style={{ marginTop: 16 }}>
      <Card>
        <CardHead title="Run result" />
        <div className="card-pad">
          {result.placed === 0 && !s ? (
            <div className="row"><Ban size={16} className="muted" />
              <span>{result.message || "No orders were placed."}</span></div>
          ) : (
            <div className="grid grid-4">
              <div className="stat" style={{ padding: "10px 12px" }}>
                <div className="stat-label">Executed</div>
                <div className="stat-value num" style={{ fontSize: 18 }}>{s?.trades_executed ?? 0}</div>
              </div>
              <div className="stat" style={{ padding: "10px 12px" }}>
                <div className="stat-label">Bought</div>
                <div className="stat-value num" style={{ fontSize: 18 }}>{s?.gross_bought_display ?? "—"}</div>
              </div>
              <div className="stat" style={{ padding: "10px 12px" }}>
                <div className="stat-label">Sold</div>
                <div className="stat-value num" style={{ fontSize: 18 }}>{s?.gross_sold_display ?? "—"}</div>
              </div>
              <div className="stat" style={{ padding: "10px 12px" }}>
                <div className="stat-label">Needs review</div>
                <div className={`stat-value num ${s?.needs_review ? "neg" : ""}`} style={{ fontSize: 18 }}>
                  {s?.needs_review ?? 0}</div>
              </div>
            </div>
          )}
          {s?.needs_review > 0 && (
            <div style={{ marginTop: 12 }}>
              <Callout kind="warn"><XCircle size={16} />
                <div>{s.needs_review} order(s) need review — check Activity → Audit log.</div></Callout>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
