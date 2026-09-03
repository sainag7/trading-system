import { useState } from "react";
import { RefreshCw } from "lucide-react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
} from "recharts";
import { api } from "../lib/api";
import { Card, CardHead, Stat, useFetch, Empty, Spinner, Segmented, Callout } from "../components/ui";

const COST = "#8b5cf6";
const AXIS = "#94a3b8";
const GRID = "rgba(148,163,184,0.18)";

// The two backends may not bill to the same place: the Messages API bills the
// ANTHROPIC_API_KEY, while the Agent SDK path inherits Claude Code's OAuth and
// may draw on a Claude subscription instead. They are shown separately rather
// than summed into one number that would be true of neither.
const BACKEND_LABEL: Record<string, string> = {
  anthropic_api: "Messages API (API key)",
  claude_agent_sdk: "Agent SDK / MCP (Claude Code auth)",
};

type Window = "7" | "30" | "90";

export function Usage() {
  const [days, setDays] = useState<Window>("30");
  const { data, loading, error, reload } = useFetch(() => api.usage(Number(days)), [days]);

  if (loading && !data) return <div className="view"><Spinner /></div>;

  const t = data?.totals ?? {};
  const today = data?.today ?? {};

  return (
    <div className="view">
      <div className="view-head row between">
        <div>
          <h1>Usage</h1>
          <p>Tokens and cost for every LLM call the system makes — per day, agent and model.</p>
        </div>
        <button className="btn btn-sm" onClick={reload}><RefreshCw size={14} /> Refresh</button>
      </div>

      {/* A failed fetch must NEVER render as "nothing recorded" — that asserts
          something about your data that the request never established, and sends
          you hunting for a capture bug that isn't there. Report the failure. */}
      {error && (
        <Callout kind="danger">
          Couldn’t load usage: {error}. The data may well be recorded — this is the
          dashboard failing to read it, not the tracker failing to write it.
        </Callout>
      )}

      {!error && !data?.has_data && (
        <Callout kind="info">
          No usage recorded yet. Tracking starts from the first run after this feature
          was added — there is no history to backfill, so earlier runs are not counted.
        </Callout>
      )}

      <div style={{ marginBottom: 14 }}>
        <Segmented<Window> value={days} onChange={setDays} options={[
          { value: "7", label: "7 days" },
          { value: "30", label: "30 days" },
          { value: "90", label: "90 days" },
        ]} />
      </div>

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label="Today" value={today.cost_display ?? "—"}
          sub={`${today.calls ?? 0} calls · ${today.total_tokens_display ?? "0"} tokens`} />
        <Stat label={`Last ${days} days`} value={t.cost_display ?? "—"}
          sub={`${t.calls ?? 0} calls · ${t.total_tokens_display ?? "0"} tokens`} />
        <Stat label="All time" value={data?.all_time?.cost_display ?? "—"}
          sub={`${data?.all_time?.calls ?? 0} calls since tracking began`} />
        <Stat label="Failed calls" value={String(t.failed ?? 0)}
          tone={t.failed ? "neg" : undefined}
          sub="errored but still billed" />
      </div>

      {t.unpriced_calls > 0 && (
        <Callout kind="warn">
          {t.unpriced_calls} call{t.unpriced_calls === 1 ? "" : "s"} used a model with no
          entry in the pricing table, so their cost is unknown and is <strong>not</strong> included
          in the totals above. Add the model to <code>agents/pricing.py</code> to price them.
        </Callout>
      )}

      <Card>
        <CardHead title="Daily spend" />
        {!data?.daily?.length ? <Empty>Nothing recorded in this window.</Empty> : (
          <div style={{ height: 240 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.daily} margin={{ top: 6, right: 12, bottom: 0, left: 4 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="day_display" tick={{ fontSize: 11, fill: AXIS }}
                  tickLine={false} axisLine={{ stroke: GRID }} minTickGap={24} />
                <YAxis tick={{ fontSize: 11, fill: AXIS }} tickLine={false}
                  axisLine={false} width={64}
                  tickFormatter={(v: number) => `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: `1px solid ${GRID}`, borderRadius: 8 }}
                  labelStyle={{ color: AXIS }}
                  formatter={(v: any, _n: any, p: any) => [
                    `$${Number(v).toFixed(4)} · ${p?.payload?.total_tokens_display ?? ""} tokens`,
                    "Cost",
                  ]} />
                <Bar dataKey="cost_usd" fill={COST} radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card>
          <CardHead title="By agent" />
          <div className="table-wrap"><BreakdownTable rows={data?.by_agent ?? []} keyCol="agent" /></div>
        </Card>
        <Card>
          <CardHead title="By model" />
          <div className="table-wrap"><BreakdownTable rows={data?.by_model ?? []} keyCol="model" /></div>
        </Card>
      </div>

      <div style={{ marginTop: 16 }}>
      <Card>
        <CardHead title="By backend" />
        <p className="muted" style={{ padding: "0 14px 6px" }}>
          These two may bill to different accounts — the Messages API to your Anthropic
          API key, the Agent SDK path to whatever Claude Code is authenticated with.
        </p>
        <div className="table-wrap">
          <BreakdownTable rows={data?.by_backend ?? []} keyCol="backend" labels={BACKEND_LABEL} />
        </div>
      </Card>
      </div>
    </div>
  );
}

function BreakdownTable({ rows, keyCol, labels }: {
  rows: any[]; keyCol: string; labels?: Record<string, string>;
}) {
  if (!rows.length) return <Empty>Nothing recorded in this window.</Empty>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>{keyCol[0].toUpperCase() + keyCol.slice(1)}</th>
          <th className="num">Calls</th>
          <th className="num">In</th>
          <th className="num">Out</th>
          <th className="num">Cached</th>
          <th className="num">Cost</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r[keyCol]}>
            <td>
              {labels?.[r[keyCol]] ?? r[keyCol]}
              {r.failed > 0 && <span className="muted"> · {r.failed} failed</span>}
              {r.unpriced_calls > 0 && <span className="muted"> · {r.unpriced_calls} unpriced</span>}
            </td>
            <td className="num">{r.calls}</td>
            <td className="num">{r.input_display}</td>
            <td className="num">{r.output_display}</td>
            <td className="num">{r.cache_read_display}</td>
            <td className="num">{r.cost_display}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
