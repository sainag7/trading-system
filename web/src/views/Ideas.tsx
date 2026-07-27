import { useState } from "react";
import { RefreshCw, ExternalLink } from "lucide-react";
import { api } from "../lib/api";
import { money, num, pct, signedPct, bigMoney } from "../lib/fmt";
import {
  Card, CardHead, ActionBadge, ScoreBar, useFetch, Empty, Spinner, Modal, Segmented, Callout,
} from "../components/ui";

export function Ideas() {
  const { data, loading, error, reload } = useFetch(() => api.ideas(), []);
  const [detail, setDetail] = useState<string | null>(null);

  if (loading) return <div className="view"><Spinner /></div>;

  return (
    <div className="view">
      <div className="view-head row between">
        <div>
          <h1>Ideas</h1>
          <p>{data?.has_data
            ? `Latest ${data.run.profile} scan · ${data.run.mode} mode · ${data.run.ts_display}`
            : "Ranked recommendations from your latest scan."}</p>
        </div>
        <button className="btn btn-sm" onClick={reload}><RefreshCw size={14} /> Refresh</button>
      </div>

      {error && <Callout kind="danger">{error}</Callout>}

      {!data?.has_data ? (
        <Empty>No recommendations yet — run a scan from the Overview to generate ideas.</Empty>
      ) : (
        <>
          {data.market_view && (
            <div style={{ marginBottom: 14 }}>
              <Callout kind={data.fallback ? "warn" : "info"}>
                <div><strong>Market view:</strong> {data.market_view}
                  {data.fallback && " (deterministic heuristics — no LLM reply this run)"}</div>
              </Callout>
            </div>
          )}

          <Card>
            <div className="table-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Ticker</th><th>Action</th><th>Composite</th><th>Tech</th>
                    <th>Fund</th><th>Sent</th><th>Setup</th><th className="num">Conf</th>
                    <th className="num">Size</th><th className="num">Stop</th><th className="num">Target</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r: any) => (
                    <tr key={r.ticker} className="clickable" onClick={() => setDetail(r.ticker)}>
                      <td style={{ fontWeight: 650 }}>{r.ticker}</td>
                      <td><ActionBadge action={r.action} /></td>
                      <td><ScoreBar value={r.composite} /></td>
                      <td><ScoreBar value={r.tech} /></td>
                      <td><ScoreBar value={r.fund} /></td>
                      <td><ScoreBar value={r.sent} /></td>
                      <td className="muted">{r.setup}</td>
                      <td className="num">{r.conf ?? "—"}</td>
                      <td className="num mono">{r.target_display}</td>
                      <td className="num mono">{r.stop_display}</td>
                      <td className="num mono">{r.tp_display}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          {data.exits?.length > 0 && (
            <>
              <div className="section-title">Exit suggestions (open positions)</div>
              <Card>
                <div className="table-wrap">
                  <table className="tbl">
                    <thead><tr><th>Ticker</th><th>Action</th><th>Trigger</th><th>Reason</th></tr></thead>
                    <tbody>
                      {data.exits.map((e: any, i: number) => (
                        <tr key={i}>
                          <td style={{ fontWeight: 650 }}>{e.ticker}</td>
                          <td><ActionBadge action={e.action} /></td>
                          <td className="muted">{e.trigger}</td>
                          <td className="muted">{e.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </>
          )}
        </>
      )}

      {detail && <IdeaDetail ticker={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: any }) {
  return (
    <div className="stat" style={{ padding: "10px 12px" }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value num" style={{ fontSize: 16 }}>{value}</div>
    </div>
  );
}

function IdeaDetail({ ticker, onClose }: { ticker: string; onClose: () => void }) {
  const { data, loading } = useFetch(() => api.ideaDetail(ticker), [ticker]);
  const [tab, setTab] = useState("why");
  const [raw, setRaw] = useState(false);

  const tech = data?.technicals ?? {};
  const fund = data?.fundamentals ?? {};
  const news = data?.news ?? {};
  const sc = data?.scoring ?? {};

  return (
    <div className="scrim" onClick={onClose}>
      <div className="modal" style={{ width: 640 }} onClick={(e) => e.stopPropagation()}>
        <div className="card-head">
          <h3 style={{ fontSize: 16 }}>{ticker}</h3>
          {data && <ActionBadge action={data.action} />}
          <div className="spacer" />
          <button className="btn btn-icon btn-ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="modal-pad">
          {loading || !data ? <Spinner /> : (
            <>
              <div className="row wrap" style={{ gap: 8, marginBottom: 6 }}>
                <span className="badge badge-blue">composite {num(data.composite, 0)}</span>
                <span className="badge badge-neutral">setup {data.setup ?? "—"}</span>
                <span className="badge badge-neutral">conf {data.conf ?? "—"}</span>
              </div>
              {data.thesis && <p style={{ marginTop: 6, marginBottom: 14 }}>{data.thesis}</p>}

              <div style={{ marginBottom: 14 }}>
                <Segmented value={tab} onChange={setTab} options={[
                  { value: "why", label: "Why" }, { value: "tech", label: "Technicals" },
                  { value: "fund", label: "Fundamentals" }, { value: "news", label: "News" },
                  { value: "scoring", label: "Scoring" }, { value: "guard", label: "Guardrail" },
                ]} />
              </div>

              {tab === "why" && (
                <div>
                  {data.rationale && <p><strong>Decision:</strong> {data.rationale}</p>}
                  {data.key_risks?.length > 0 && (
                    <ul style={{ margin: "8px 0", paddingLeft: 18 }}>
                      {data.key_risks.map((r: string, i: number) => <li key={i}>{r}</li>)}
                    </ul>
                  )}
                  <p className="small muted">Plan: stop {data.plan.stop_display} · target{" "}
                    {data.plan.target_display} · by {data.plan.max_hold_until}</p>
                </div>
              )}

              {tab === "tech" && (
                <div className="grid grid-4">
                  <Metric label="Price" value={money(tech.price)} />
                  <Metric label="Trend" value={`${tech.trend ?? "—"}`} />
                  <Metric label="RSI(14)" value={num(tech.rsi14)} />
                  <Metric label="ATR %" value={pct(tech.atr20_pct)} />
                  <Metric label="SMA50" value={money(tech.sma50)} />
                  <Metric label="SMA200" value={money(tech.sma200)} />
                  <Metric label="vs 52w hi" value={pct(tech.distance_from_52w_high_pct)} />
                  <Metric label="Vol vs avg" value={num(tech.volume_vs_avg)} />
                </div>
              )}

              {tab === "fund" && (
                <div className="grid grid-4">
                  <Metric label="Revenue TTM" value={bigMoney(fund.revenue_ttm)} />
                  <Metric label="Rev growth" value={pct(fund.revenue_growth_yoy)} />
                  <Metric label="EPS TTM" value={num(fund.eps_ttm)} />
                  <Metric label="EPS growth" value={pct(fund.eps_growth_yoy)} />
                  <Metric label="P/E" value={num(fund.pe_ratio)} />
                  <Metric label="P/S" value={num(fund.ps_ratio)} />
                  <Metric label="D/E" value={num(fund.debt_to_equity)} />
                  <Metric label="Next earnings" value={fund.next_earnings_date ?? "—"} />
                </div>
              )}

              {tab === "news" && (
                <div>
                  <p className="small muted">Aggregate {num(news.aggregate_score)} ({news.aggregate_label ?? "—"})
                    · {news.article_count ?? 0} articles · source {news.source ?? "none"}</p>
                  {(news.headlines ?? []).slice(0, 8).map((h: any, i: number) => (
                    <div key={i} className="row" style={{ padding: "5px 0", borderBottom: "1px solid var(--border)" }}>
                      <div style={{ flex: 1 }}>
                        {h.url ? <a href={h.url} target="_blank" rel="noreferrer">{h.title} <ExternalLink size={11} /></a> : h.title}
                      </div>
                      {typeof h.sentiment_score === "number" && (
                        <span className={`badge ${h.sentiment_score >= 0 ? "badge-green" : "badge-red"}`}>
                          {signedPct(h.sentiment_score * 100)}
                        </span>
                      )}
                    </div>
                  ))}
                  {!(news.headlines ?? []).length && <p className="muted small">No headlines for this run.</p>}
                </div>
              )}

              {tab === "scoring" && (
                <div className="grid grid-4">
                  <Metric label="Composite" value={num(sc.composite, 0)} />
                  <Metric label="Technical" value={num(sc.technical, 0)} />
                  <Metric label="Fundamental" value={num(sc.fundamental, 0)} />
                  <Metric label="Sentiment" value={num(sc.sentiment, 0)} />
                </div>
              )}

              {tab === "guard" && (
                data.guardrail ? (
                  <div>
                    <p><strong>{data.guardrail.approved ? "✓ Approved" : "✗ Rejected"}</strong>
                      {data.guardrail.resized && " (resized)"} — requested{" "}
                      {data.guardrail.requested_display} → approved {data.guardrail.approved_display}</p>
                    <ul style={{ paddingLeft: 18 }}>
                      {data.guardrail.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}
                    </ul>
                  </div>
                ) : <p className="muted">No guardrail record — this name was hold/pass (no order proposed).</p>
              )}

              <div className="divider" />
              <button className="btn btn-sm btn-ghost" onClick={() => setRaw(!raw)}>
                {raw ? "Hide" : "Show"} raw data
              </button>
              {raw && <pre className="log" style={{ marginTop: 10, maxHeight: 260, overflow: "auto" }}>
                {JSON.stringify(data.raw, null, 2)}</pre>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
