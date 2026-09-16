import React, { useState } from "react";
import { GraduationCap, RefreshCw, Play, Check, X, Archive } from "lucide-react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Cell,
} from "recharts";
import { api } from "../lib/api";
import { useJob } from "../lib/useJob";
import { Card, CardHead, Stat, useFetch, Empty, Spinner, Callout } from "../components/ui";

const AXIS = "#94a3b8";
const GRID = "rgba(148,163,184,0.18)";
const POS = "#16a34a";
const NEG = "#dc2626";

const money = (v: any) =>
  v == null ? "—" : `${v < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(2)}`;
const pct = (v: any, signed = true) =>
  v == null || v === "inf" ? "—" : `${signed && v > 0 ? "+" : ""}${Number(v).toFixed(1)}%`;
const num = (v: any) => (v == null ? "—" : v === "inf" ? "∞" : Number(v).toFixed(2));

export function Learning() {
  const { data, loading, error, reload } = useFetch(() => api.review(), []);
  const { runJob, busy } = useJob();
  const [acting, setActing] = useState<string | null>(null);

  const runReview = () =>
    runJob(() => api.reviewRun(), { label: "review", onDone: () => reload() });

  const act = async (key: string, fn: () => Promise<any>) => {
    setActing(key);
    try { await fn(); reload(); }
    catch (e: any) { alert(e?.message ?? String(e)); }
    finally { setActing(null); }
  };

  if (loading && !data) return <div className="view"><Spinner /></div>;

  const sc = data?.scorecard ?? {};
  const agg = sc.aggregates ?? {};
  const all = agg.all ?? {};
  const out = data?.latest?.output ?? {};
  const settings = data?.settings ?? {};
  const lessons: any[] = data?.lessons ?? [];
  const active = lessons.filter((l) => l.status === "active");
  const retired = lessons.filter((l) => l.status !== "active");
  const proposals: any[] = data?.proposals ?? [];
  const pending = proposals.filter((p) => p.status === "proposed");
  const settled = proposals.filter((p) => p.status !== "proposed");
  const gateOpen = (data?.n_closed ?? 0) >= (settings.min_closed_trades ?? 20);

  const bucketRows = Object.entries(agg.by_score_bucket ?? {}).map(([k, v]: any) => ({
    bucket: k, n: v.n, expectancy: v.expectancy_pct ?? 0, win: v.win_rate, pnl: v.total_pnl_usd,
  }));
  const sizeRows = Object.entries(agg.by_size_bucket ?? {}).map(([k, v]: any) => ({
    bucket: k, n: v.n, expectancy: v.expectancy_pct ?? 0, win: v.win_rate, pnl: v.total_pnl_usd,
  }));

  return (
    <div className="view">
      <div className="view-head row between">
        <div>
          <h1>Learning</h1>
          <p>
            The post-close review agent scores every closed trade and every call the agents
            made, then writes lessons back into tomorrow's prompts.
            {data?.account_label && <> Account: <strong>{data.account_label}</strong>.</>}
          </p>
        </div>
        <div className="row">
          <button className="btn btn-sm" onClick={reload}><RefreshCw size={14} /> Refresh</button>
          <button className="btn btn-primary btn-sm" onClick={runReview} disabled={busy}>
            <Play size={14} /> Run review now
          </button>
        </div>
      </div>

      {error && (
        <Callout kind="danger">Couldn't load the review: {error}.</Callout>
      )}

      {!error && !data?.has_data && (
        <Callout kind="info">
          <GraduationCap size={16} />
          <div>
            No review yet. It runs automatically at 16:30 ET on trading days once the live
            schedule is installed, or run one now. It reads the ledger only — nothing is traded.
          </div>
        </Callout>
      )}

      {data?.has_data && (
        <>
          <div className="grid grid-4" style={{ marginBottom: 16 }}>
            <Stat label="Closed trades" value={String(all.n ?? 0)}
              sub={`last ${sc.window_days ?? settings.window_days}d · ${sc.n_closed_all_time ?? all.n ?? 0} all time`} />
            <Stat label="Win rate" value={pct(all.win_rate, false)}
              sub={`avg win ${pct(all.avg_win_pct)} · avg loss ${pct(all.avg_loss_pct)}`} />
            <Stat label="Expectancy" value={pct(all.expectancy_pct)}
              tone={all.expectancy_pct > 0 ? "pos" : all.expectancy_pct < 0 ? "neg" : undefined}
              sub={`profit factor ${num(all.profit_factor)} · avg R ${num(all.avg_r)}`} />
            <Stat label="Realized P&L" value={money(all.total_pnl_usd)}
              tone={all.total_pnl_usd > 0 ? "pos" : all.total_pnl_usd < 0 ? "neg" : undefined}
              sub={data?.latest?.ts_display ? `reviewed ${data.latest.ts_display}` : ""} />
          </div>

          {out.day_summary && (
            <Card>
              <CardHead title={`Day summary — ${data?.latest?.review_date ?? ""}`} />
              <div className="card-pad"><p style={{ margin: 0 }}>{out.day_summary}</p></div>
            </Card>
          )}

          <div className="grid grid-2" style={{ marginTop: 16 }}>
            <Card>
              <CardHead title="Expectancy by entry score" />
              {bucketRows.length ? <BucketChart rows={bucketRows} /> : <Empty>No closed trades yet.</Empty>}
            </Card>
            <Card>
              <CardHead title="Expectancy by position size" />
              {sizeRows.length ? <BucketChart rows={sizeRows} /> : <Empty>No closed trades yet.</Empty>}
            </Card>
          </div>

          <div style={{ marginTop: 16 }}>
            <Card>
              <CardHead title="What the agent's calls did next">
                <span className="muted small">mean forward return · hit rate</span>
              </CardHead>
              <CallsTable calls={sc.calls} />
            </Card>
          </div>

          <div className="grid grid-2" style={{ marginTop: 16 }}>
            <Card>
              <CardHead title="What worked" />
              <Bullets items={out.what_worked} empty="Nothing flagged." />
            </Card>
            <Card>
              <CardHead title="What didn't" />
              <Bullets items={out.what_didnt} empty="Nothing flagged." />
            </Card>
          </div>

          <div style={{ marginTop: 16 }}>
            <Card>
              <CardHead title={`Lessons in force (${active.length})`}>
                <span className="muted small">fed to the decision & monitor agents each run</span>
              </CardHead>
              {!active.length ? <Empty>No active lessons.</Empty> : (
                <div className="table-wrap">
                  <table className="tbl">
                    <thead><tr>
                      <th>Lesson</th><th>Applies to</th><th className="num">Confidence</th>
                      <th className="num">Reinforced</th><th></th>
                    </tr></thead>
                    <tbody>
                      {active.map((l) => (
                        <FragmentRows key={l.id} detail={l.evidence} colSpan={5}>
                          <td style={{ whiteSpace: "normal", minWidth: 320 }}>{l.text}</td>
                          <td>{(l.applies_to ?? []).join(", ")}</td>
                          <td className="num">{l.confidence}</td>
                          <td className="num">{l.times_reinforced ?? 0}×</td>
                          <td>
                            <button className="btn btn-ghost btn-sm" disabled={acting === l.id}
                              title="Retire this lesson (it stops being fed to the agents)"
                              onClick={() => act(l.id, () => api.reviewRetire(l.id))}>
                              <Archive size={13} /> Retire
                            </button>
                          </td>
                        </FragmentRows>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {retired.length > 0 && (
                <p className="muted small" style={{ padding: "8px 14px" }}>
                  {retired.length} retired lesson{retired.length === 1 ? "" : "s"} not shown.
                </p>
              )}
            </Card>
          </div>

          <div style={{ marginTop: 16 }}>
            <Card>
              <CardHead title="Parameter proposals">
                <span className="muted small">
                  {settings.auto_apply
                    ? (gateOpen
                      ? "auto-apply ON — in-bounds proposals are applied by the review"
                      : `auto-apply ON, gated: ${data?.n_closed ?? 0} / ${settings.min_closed_trades} closed trades`)
                    : "auto-apply OFF — apply by hand"}
                </span>
              </CardHead>
              {!proposals.length ? <Empty>No proposals yet.</Empty> : (
                <div className="table-wrap">
                  <table className="tbl">
                    <thead><tr>
                      <th>When</th><th>Key</th><th className="num">Current</th>
                      <th className="num">Proposed</th><th className="num">Applied</th>
                      <th>Status</th><th></th>
                    </tr></thead>
                    <tbody>
                      {[...pending, ...settled].map((p) => (
                        <FragmentRows key={p.id} colSpan={7}
                          detail={[p.reason, p.evidence].filter(Boolean).join(" — ")}>
                          <td>{p.ts_display}</td>
                          <td className="mono">{p.key}</td>
                          <td className="num">{p.current ?? "—"}</td>
                          <td className="num">{p.proposed ?? "—"}</td>
                          <td className="num">{p.applied ?? "—"}</td>
                          <td><span className={`badge badge-${STATUS_TONE[p.status] ?? "neutral"}`}>{p.status}</span></td>
                          <td>
                            {p.status === "proposed" && (
                              <div className="row">
                                <button className="btn btn-primary btn-sm" disabled={acting === `a${p.id}`}
                                  onClick={() => act(`a${p.id}`, () => api.reviewApply(p.id))}>
                                  <Check size={13} /> Apply
                                </button>
                                <button className="btn btn-ghost btn-sm" disabled={acting === `d${p.id}`}
                                  onClick={() => act(`d${p.id}`, () => api.reviewDismiss(p.id))}>
                                  <X size={13} /> Dismiss
                                </button>
                              </div>
                            )}
                          </td>
                        </FragmentRows>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="muted small" style={{ padding: "8px 14px" }}>
                Only whitelisted <code>strategy:</code> keys, clamped to their bounds and one step per
                review. <code>risk:</code> limits are never touched by the review agent.
              </p>
            </Card>
          </div>

          {out.playbook && (
            <div style={{ marginTop: 16 }}>
              <Card>
                <CardHead title="Playbook">
                  <span className="muted small">rewritten in full each review · handed to the decision agent</span>
                </CardHead>
                <div className="card-pad">
                  <pre className="log" style={{ fontFamily: "inherit", fontSize: 13.5 }}>{out.playbook}</pre>
                </div>
              </Card>
            </div>
          )}

          <div style={{ marginTop: 16 }}>
            <Card>
              <CardHead title="Closed trades in window" />
              <LegsTable legs={sc.closed_legs ?? []} />
            </Card>
          </div>

          {(sc.open_positions ?? []).length > 0 && (
            <div style={{ marginTop: 16 }}>
              <Card>
                <CardHead title="Open positions (from the ledger)" />
                <div className="table-wrap">
                  <table className="tbl">
                    <thead><tr>
                      <th>Ticker</th><th className="num">Held</th><th className="num">Size</th>
                      <th className="num">Avg cost</th><th className="num">Price</th>
                      <th className="num">Unrealized</th><th className="num">MFE</th><th className="num">MAE</th>
                      <th className="num">Entry score</th>
                    </tr></thead>
                    <tbody>
                      {sc.open_positions.map((p: any) => (
                        <tr key={p.ticker}>
                          <td><strong>{p.ticker}</strong></td>
                          <td className="num">{p.hold_days ?? "—"}d</td>
                          <td className="num">{p.size_pct_of_equity != null ? `${p.size_pct_of_equity.toFixed(0)}%` : "—"}</td>
                          <td className="num">{money(p.avg_cost)}</td>
                          <td className="num">{money(p.price)}</td>
                          <td className={`num ${p.unrealized_pct > 0 ? "pos" : p.unrealized_pct < 0 ? "neg" : ""}`}>{pct(p.unrealized_pct)}</td>
                          <td className="num">{pct(p.mfe_pct)}</td>
                          <td className="num">{pct(p.mae_pct)}</td>
                          <td className="num">{p.entry_score ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </div>
          )}

          {(data?.history ?? []).length > 1 && (
            <p className="muted small" style={{ marginTop: 12 }}>
              {data.history.length} reviews on record; the latest is shown. Each writes
              <code> reports/&lt;date&gt;-review.md</code>.
            </p>
          )}
        </>
      )}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  applied: "green", proposed: "amber", rejected: "red", dismissed: "neutral",
};

/** A compact table row plus an optional full-width detail line beneath it.
 *  Long evidence text goes in the detail row so it never squeezes into one
 *  narrow, page-tall cell between nowrap columns. */
function FragmentRows({ children, detail, colSpan }: {
  children: React.ReactNode; detail?: string | null; colSpan: number;
}) {
  return (
    <>
      <tr className={detail ? "has-detail" : undefined}>{children}</tr>
      {detail && (
        <tr className="detail-row">
          <td colSpan={colSpan} className="muted small"
            style={{ whiteSpace: "normal", paddingTop: 0 }}>
            {detail}
          </td>
        </tr>
      )}
    </>
  );
}

function Bullets({ items, empty }: { items?: string[]; empty: string }) {
  if (!items?.length) return <Empty>{empty}</Empty>;
  return (
    <ul style={{ margin: 0, padding: "10px 14px 12px 30px" }}>
      {items.map((it, i) => <li key={i} style={{ marginBottom: 6 }}>{it}</li>)}
    </ul>
  );
}

function BucketChart({ rows }: { rows: any[] }) {
  return (
    <div style={{ height: 200 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 6, right: 12, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="bucket" tick={{ fontSize: 11, fill: AXIS }} tickLine={false}
            axisLine={{ stroke: GRID }} />
          <YAxis tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} width={48}
            tickFormatter={(v: number) => `${v.toFixed(0)}%`} />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: `1px solid ${GRID}`, borderRadius: 8 }}
            labelStyle={{ color: AXIS }}
            formatter={(v: any, _n: any, p: any) => [
              `${pct(v)} expectancy · ${p?.payload?.n} trades · win ${pct(p?.payload?.win, false)} · ${money(p?.payload?.pnl)}`,
              "",
            ]} />
          <Bar dataKey="expectancy" radius={[3, 3, 0, 0]}>
            {rows.map((r, i) => <Cell key={i} fill={r.expectancy >= 0 ? POS : NEG} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function CallsTable({ calls }: { calls: any }) {
  const by = calls?.by_action ?? {};
  const hs: number[] = calls?.horizons ?? [];
  const rows = Object.entries(by);
  if (!rows.length) return <Empty>No calls scored yet.</Empty>;
  return (
    <div className="table-wrap">
      <table className="tbl">
        <thead><tr>
          <th>Call</th><th className="num">n</th>
          {hs.map((h) => <th key={h} className="num">+{h}d</th>)}
        </tr></thead>
        <tbody>
          {rows.map(([action, s]: any) => (
            <tr key={action}>
              <td><strong>{action}</strong></td>
              <td className="num">{s.n}</td>
              {hs.map((h) => (
                <td key={h} className="num">
                  {s[`r${h}_mean`] == null ? "—" : (
                    <span className={s[`r${h}_mean`] > 0 ? "pos" : s[`r${h}_mean`] < 0 ? "neg" : ""}>
                      {pct(s[`r${h}_mean`])} <span className="muted">· {pct(s[`r${h}_hit`], false)}</span>
                    </span>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LegsTable({ legs }: { legs: any[] }) {
  if (!legs.length) return <Empty>No closed trades in the window.</Empty>;
  const sorted = [...legs].sort((a, b) => (b.exit_date ?? "").localeCompare(a.exit_date ?? ""));
  return (
    <div className="table-wrap">
      <table className="tbl">
        <thead><tr>
          <th>Exit</th><th>Ticker</th><th className="num">Held</th><th className="num">Size</th>
          <th className="num">Score</th><th>Setup</th><th className="num">Entry → Exit</th>
          <th className="num">P&L</th><th className="num">R</th><th>Trigger</th><th className="num">+10d after</th>
        </tr></thead>
        <tbody>
          {sorted.map((l, i) => (
            <tr key={i}>
              <td>{l.exit_date}</td>
              <td><strong>{l.ticker}</strong></td>
              <td className="num">{l.hold_days ?? "—"}d</td>
              <td className="num">{l.size_pct_of_equity != null ? `${l.size_pct_of_equity.toFixed(0)}%` : "—"}</td>
              <td className="num">{l.entry_score ?? "—"}</td>
              <td>{l.entry_setup ?? "—"}</td>
              <td className="num">{money(l.entry_price)} → {money(l.exit_price)}</td>
              <td className={`num ${l.pnl_usd > 0 ? "pos" : l.pnl_usd < 0 ? "neg" : ""}`}>
                {money(l.pnl_usd)} <span className="muted">/ {pct(l.pnl_pct)}</span>
              </td>
              <td className="num">{num(l.r_multiple)}</td>
              <td>{l.exit_trigger ?? "—"}</td>
              <td className="num">{pct(l.ret_10d_after_exit)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
