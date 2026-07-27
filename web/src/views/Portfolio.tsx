import { useState } from "react";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  BarChart, Bar,
} from "recharts";
import { api } from "../lib/api";
import { Card, CardHead, Stat, useFetch, Empty, Spinner, Segmented } from "../components/ui";

const EQUITY = "#3b82f6";
const PEAK = "#14b8a6";
const AXIS = "#94a3b8";
const GRID = "rgba(148,163,184,0.18)";

export function Portfolio() {
  const [account, setAccount] = useState<string | undefined>(undefined);
  const { data, loading } = useFetch(() => api.portfolio(account), [account]);

  if (loading && !data) return <div className="view"><Spinner /></div>;

  return (
    <div className="view">
      <div className="view-head row between">
        <div>
          <h1>Portfolio</h1>
          <p>Equity, positions and P&amp;L for your connected accounts.</p>
        </div>
        {data?.accounts?.length > 1 && (
          <Segmented
            value={data.selected}
            onChange={(v) => setAccount(v)}
            options={data.accounts.map((a: any) => ({ value: a.key, label: a.label }))}
          />
        )}
      </div>

      {!data?.has_data ? (
        <Empty>No portfolio data yet — it appears once your real account is connected
          (recommend reads it read-only; preview/live trade it).</Empty>
      ) : data.empty ? (
        <Empty>No data for this account yet.</Empty>
      ) : (
        <>
          <div className="grid grid-4" style={{ marginBottom: 16 }}>
            <Stat label="Equity" value={<span className="num">{data.summary.equity_display}</span>} />
            <Stat label="Cash" value={<span className="num">{data.summary.cash_display}</span>} />
            <Stat label="Drawdown" value={<span className="num">{data.summary.drawdown_display}</span>} />
            <Stat label="Peak equity" value={<span className="num">{data.summary.peak_display}</span>} />
          </div>

          <Card>
            <CardHead title="Equity curve" />
            <div className="card-pad" style={{ height: 280 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={data.series} margin={{ top: 6, right: 12, bottom: 0, left: 4 }}>
                  <CartesianGrid stroke={GRID} vertical={false} />
                  <XAxis dataKey="ts_display" tick={{ fontSize: 11, fill: AXIS }}
                    tickLine={false} axisLine={{ stroke: GRID }} minTickGap={40} />
                  <YAxis tick={{ fontSize: 11, fill: AXIS }} tickLine={false}
                    axisLine={false} width={60}
                    tickFormatter={(v) => `$${Number(v).toLocaleString()}`} />
                  <Tooltip contentStyle={tooltipStyle}
                    formatter={(v: any) => `$${Number(v).toLocaleString()}`} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line type="monotone" dataKey="equity" name="Equity" stroke={EQUITY}
                    strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="peak_equity" name="Peak" stroke={PEAK}
                    strokeWidth={1.5} strokeDasharray="4 3" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Card>

          <div className="grid grid-2" style={{ marginTop: 16, gridTemplateColumns: "1.4fr 1fr" }}>
            <Card>
              <CardHead title="Positions" />
              <div className="table-wrap">
                {data.positions.length ? (
                  <table className="tbl">
                    <thead><tr><th>Ticker</th><th className="num">Shares</th>
                      <th className="num">Avg cost</th><th className="num">Value</th><th>Sector</th></tr></thead>
                    <tbody>
                      {data.positions.map((p: any) => (
                        <tr key={p.ticker}>
                          <td style={{ fontWeight: 650 }}>{p.ticker}</td>
                          <td className="num mono">{p.shares_display}</td>
                          <td className="num mono">{p.avg_cost_display}</td>
                          <td className="num mono">{p.market_value_display}</td>
                          <td className="muted">{p.sector}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <div className="card-pad"><Empty>No open positions recorded.</Empty></div>}
              </div>
            </Card>

            <Card>
              <CardHead title="Sector allocation" />
              <div className="card-pad" style={{ height: 260 }}>
                {data.sectors.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={data.sectors} layout="vertical"
                      margin={{ top: 0, right: 12, bottom: 0, left: 8 }}>
                      <CartesianGrid stroke={GRID} horizontal={false} />
                      <XAxis type="number" tick={{ fontSize: 11, fill: AXIS }} tickLine={false}
                        axisLine={false} tickFormatter={(v) => `$${Number(v).toLocaleString()}`} />
                      <YAxis type="category" dataKey="sector" width={90}
                        tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} />
                      <Tooltip contentStyle={tooltipStyle}
                        formatter={(v: any) => `$${Number(v).toLocaleString()}`} />
                      <Bar dataKey="market_value" name="Value" fill={EQUITY} radius={[0, 4, 4, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : <Empty>No sector data.</Empty>}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

const tooltipStyle = {
  background: "var(--surface)",
  border: "1px solid var(--border-strong)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--text)",
};
