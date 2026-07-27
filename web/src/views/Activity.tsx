import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { Card, CardHead, useFetch, Empty, Spinner, Segmented } from "../components/ui";

export function Activity() {
  const { data, loading, reload } = useFetch(() => api.activity(), []);
  const [tab, setTab] = useState<"decisions" | "orders" | "fills" | "audit">("decisions");

  if (loading && !data) return <div className="view"><Spinner /></div>;

  return (
    <div className="view">
      <div className="view-head row between">
        <div>
          <h1>Activity</h1>
          <p>Decisions, orders, fills and the audit log — most recent first.</p>
        </div>
        <button className="btn btn-sm" onClick={reload}><RefreshCw size={14} /> Refresh</button>
      </div>

      <div style={{ marginBottom: 14 }}>
        <Segmented value={tab} onChange={setTab} options={[
          { value: "decisions", label: `Decisions (${data?.decisions.length ?? 0})` },
          { value: "orders", label: `Orders (${data?.orders.length ?? 0})` },
          { value: "fills", label: `Fills (${data?.fills.length ?? 0})` },
          { value: "audit", label: `Audit (${data?.audit.length ?? 0})` },
        ]} />
      </div>

      <Card>
        <div className="table-wrap">
          {tab === "decisions" && <DecisionsTable rows={data?.decisions ?? []} />}
          {tab === "orders" && <OrdersTable rows={data?.orders ?? []} />}
          {tab === "fills" && <FillsTable rows={data?.fills ?? []} />}
          {tab === "audit" && <AuditTable rows={data?.audit ?? []} />}
        </div>
      </Card>
    </div>
  );
}

function DecisionsTable({ rows }: { rows: any[] }) {
  if (!rows.length) return <Pad>No decisions recorded yet.</Pad>;
  return (
    <table className="tbl">
      <thead><tr><th>Time</th><th>Ticker</th><th>Action</th><th className="num">Requested</th>
        <th>Verdict</th><th className="num">Approved</th><th>Reasons</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className="muted">{r.ts_display}</td>
            <td style={{ fontWeight: 600 }}>{r.ticker}</td>
            <td>{r.action}</td>
            <td className="num mono">{r.requested_display}</td>
            <td><span className={`badge ${r.approved ? "badge-green" : "badge-red"}`}>
              {r.approved ? "approved" : "rejected"}{r.resized ? " · resized" : ""}</span></td>
            <td className="num mono">{r.approved_display}</td>
            <td className="muted small" style={{ whiteSpace: "normal", maxWidth: 320 }}>
              {(r.reasons || []).join("; ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OrdersTable({ rows }: { rows: any[] }) {
  if (!rows.length) return <Pad>No orders yet.</Pad>;
  return (
    <table className="tbl">
      <thead><tr><th>Time</th><th>Mode</th><th>Ticker</th><th>Side</th>
        <th className="num">Qty</th><th className="num">Notional</th><th>Status</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className="muted">{r.ts_display}</td><td>{r.mode}</td>
            <td style={{ fontWeight: 600 }}>{r.ticker}</td><td>{r.side}</td>
            <td className="num mono">{r.qty_display}</td>
            <td className="num mono">{r.notional_display}</td>
            <td><StatusBadge status={r.status} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FillsTable({ rows }: { rows: any[] }) {
  if (!rows.length) return <Pad>No fills yet.</Pad>;
  return (
    <table className="tbl">
      <thead><tr><th>Time</th><th>Ticker</th><th>Side</th>
        <th className="num">Qty</th><th className="num">Price</th><th className="num">Notional</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className="muted">{r.ts_display}</td>
            <td style={{ fontWeight: 600 }}>{r.ticker}</td><td>{r.side}</td>
            <td className="num mono">{r.qty_display}</td>
            <td className="num mono">{r.price_display}</td>
            <td className="num mono">{r.notional_display}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AuditTable({ rows }: { rows: any[] }) {
  if (!rows.length) return <Pad>No audit events yet.</Pad>;
  const cls: Record<string, string> = {
    INFO: "badge-neutral", WARN: "badge-amber", ERROR: "badge-red", HALT: "badge-red",
  };
  return (
    <table className="tbl">
      <thead><tr><th>Time</th><th>Level</th><th>Event</th><th>Detail</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className="muted">{r.ts_display}</td>
            <td><span className={`badge ${cls[r.level] ?? "badge-neutral"}`}>{r.level}</span></td>
            <td className="mono small">{r.event}</td>
            <td className="muted small" style={{ whiteSpace: "normal", maxWidth: 380 }}>{r.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StatusBadge({ status }: { status: string }) {
  const s = (status || "").toLowerCase();
  const cls = ["filled", "submitted"].includes(s) ? "badge-green"
    : ["rejected", "error", "needs_review"].includes(s) ? "badge-red" : "badge-neutral";
  return <span className={`badge ${cls}`}>{status}</span>;
}

function Pad({ children }: { children: any }) {
  return <div className="card-pad"><Empty>{children}</Empty></div>;
}
