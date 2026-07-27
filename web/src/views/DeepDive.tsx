import { useEffect, useState } from "react";
import { Search, X, RotateCcw } from "lucide-react";
import { api } from "../lib/api";
import { money, num, pct, signedPct, bigMoney } from "../lib/fmt";
import { useJob } from "../lib/useJob";
import { Card, CardHead, useFetch, Empty, Spinner, Callout } from "../components/ui";

export function DeepDive() {
  const tickers = useFetch(() => api.deepdiveTickers(), []);
  const { runJob, busy } = useJob();
  const [input, setInput] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const list: any[] = tickers.data?.tickers ?? [];
  const hidden: string[] = tickers.data?.hidden ?? [];

  // Keep a valid selection as the list changes.
  useEffect(() => {
    if (list.length && (!selected || !list.find((t) => t.ticker === selected))) {
      setSelected(list[0].ticker);
    }
    if (!list.length) setSelected(null);
  }, [tickers.data]); // eslint-disable-line

  const research = () => {
    const t = input.trim().toUpperCase();
    if (!t) return;
    runJob(() => api.research(t), {
      label: `deep research ${t}`,
      onDone: () => { setInput(""); tickers.reload(); setSelected(t); },
    });
  };

  const remove = async (t: string) => {
    await api.hideTicker(t);
    tickers.reload();
  };
  const restore = async (t: string) => {
    await api.unhideTicker(t);
    tickers.reload();
  };

  return (
    <div className="view">
      <div className="view-head">
        <h1>Deep dive</h1>
        <p>Read-only deep-research briefings. Nothing here places a trade.</p>
      </div>

      <Card>
        <div className="card-pad">
          <div className="row wrap">
            <input className="input" style={{ flex: 1, minWidth: 200 }}
              placeholder="Research a stock — e.g. NVDA"
              value={input} disabled={busy}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && research()} />
            <button className="btn btn-primary" onClick={research} disabled={busy || !input.trim()}>
              <Search size={15} /> Research
            </button>
          </div>
          <p className="small muted" style={{ marginTop: 8 }}>
            Runs a read-only briefing (explain mode) — takes a minute or two. Saved below, newest first.
          </p>
        </div>
      </Card>

      {list.length > 0 && (
        <div className="row wrap" style={{ margin: "16px 0" }}>
          {list.map((t) => (
            <span key={t.ticker}
              className="pill"
              style={{
                cursor: "pointer",
                borderColor: selected === t.ticker ? "var(--primary)" : undefined,
                color: selected === t.ticker ? "var(--primary)" : undefined,
              }}
              onClick={() => setSelected(t.ticker)}>
              {t.ticker}
              <span className="faint small">{t.report_count}</span>
              <button className="btn btn-icon btn-ghost" style={{ padding: 2, marginLeft: 2 }}
                title={`Remove ${t.ticker} from deep dive`}
                onClick={(e) => { e.stopPropagation(); remove(t.ticker); }}>
                <X size={13} />
              </button>
            </span>
          ))}
        </div>
      )}

      {tickers.loading ? <Spinner /> :
        !list.length ? <Empty>No deep-research reports yet — research a ticker above.</Empty> :
        selected ? <ReportPanel ticker={selected} /> : null}

      {hidden.length > 0 && (
        <details style={{ marginTop: 20 }}>
          <summary className="muted small" style={{ cursor: "pointer" }}>
            Removed tickers ({hidden.length})
          </summary>
          <div className="row wrap" style={{ marginTop: 10 }}>
            {hidden.map((t) => (
              <button key={t} className="pill" onClick={() => restore(t)}
                title={`Restore ${t}`} style={{ cursor: "pointer" }}>
                <RotateCcw size={12} /> {t}
              </button>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function ReportPanel({ ticker }: { ticker: string }) {
  const { data, loading } = useFetch(() => api.deepdiveReports(ticker), [ticker]);
  const [idx, setIdx] = useState(0);
  useEffect(() => setIdx(0), [ticker]);

  if (loading) return <Spinner />;
  const reports: any[] = data?.reports ?? [];
  if (!reports.length) return <Empty>No reports for {ticker}.</Empty>;
  const cur = reports[idx];

  return (
    <Card>
      <CardHead title={`${ticker} briefing`}>
        {reports.length > 1 && (
          <select className="input" style={{ width: 220 }} value={idx}
            onChange={(e) => setIdx(Number(e.target.value))}>
            {reports.map((r, i) => <option key={i} value={i}>{r.ts_display}</option>)}
          </select>
        )}
      </CardHead>
      <div className="card-pad">
        <Report report={cur.report} />
      </div>
    </Card>
  );
}

function Report({ report }: { report: any }) {
  const snap = report.snapshot ?? {};
  const v = report.verdict ?? {};
  const rets = report.price_action?.returns ?? {};
  const wim = report.why_it_moved ?? {};
  const e = report.earnings ?? {};
  const f = report.fundamentals ?? {};
  const [raw, setRaw] = useState(false);

  const verdictKind = ["buy", "add"].includes((v.action || "").toLowerCase()) ? "ok"
    : ["sell", "trim", "avoid"].includes((v.action || "").toLowerCase()) ? "danger" : "info";

  return (
    <div>
      <div className="row wrap between">
        <div>
          <h3 style={{ fontSize: 17 }}>{report.ticker}{snap.name ? ` · ${snap.name}` : ""}</h3>
          <div className="small muted">{snap.sector ?? ""}
            {typeof snap.price === "number" ? ` · ${money(snap.price)}` : ""}
            {typeof snap.market_cap === "number" ? ` · mkt cap ${bigMoney(snap.market_cap)}` : ""}
            {report.as_of ? ` · as of ${report.as_of}` : ""}</div>
        </div>
      </div>

      {v.action && (
        <div style={{ margin: "14px 0" }}>
          <Callout kind={verdictKind as any}>
            <div>
              <strong style={{ textTransform: "uppercase" }}>{v.action}</strong>
              {" "}· confidence {v.confidence ?? "—"} · {v.profile ?? "swing"} profile
              {v.rationale && <div style={{ marginTop: 4, color: "var(--text)" }}>{v.rationale}</div>}
            </div>
          </Callout>
        </div>
      )}

      {snap.summary && <p>{snap.summary}</p>}

      <div className="grid grid-3" style={{ margin: "14px 0" }}>
        {[["1d", "d1"], ["5d", "d5"], ["1m", "m1"], ["3m", "m3"], ["YTD", "ytd"]].map(([lbl, k]) => (
          <div key={k} className="stat" style={{ padding: "10px 12px" }}>
            <div className="stat-label">{lbl}</div>
            <div className={`stat-value num ${typeof rets[k] === "number" ? (rets[k] >= 0 ? "pos" : "neg") : ""}`}
              style={{ fontSize: 16 }}>{signedPct(rets[k])}</div>
          </div>
        ))}
      </div>

      <Section title="Why it moved">
        <p>{wim.summary ?? "—"}</p>
        {(wim.drivers ?? []).map((d: any, i: number) => (
          <p key={i} className="small muted">• {d.claim} — “{d.headline}”{d.date ? ` (${d.date})` : ""}</p>
        ))}
      </Section>

      <Section title="Earnings">
        {e.next_date ? (
          <p>{e.next_date}{e.days_until != null ? ` (in ~${e.days_until}d)` : ""}
            {e.event_risk && <span className="badge badge-amber" style={{ marginLeft: 8 }}>event risk in horizon</span>}</p>
        ) : <p className="muted">date unavailable</p>}
        {e.note && <p className="small muted">{e.note}</p>}
      </Section>

      <Section title="Fundamentals">
        <p className="small">
          P/E {num(f.pe_ratio)} · P/S {num(f.ps_ratio)} · EPS growth {pct(f.eps_growth_yoy)}
          {" "}· D/E {num(f.debt_to_equity)} · FCF {bigMoney(f.free_cash_flow)}
        </p>
      </Section>

      {(report.scenarios ?? []).length > 0 && (
        <Section title="Scenarios (conditional levels — not a forecast)">
          <div className="grid grid-3">
            {report.scenarios.map((s: any, i: number) => (
              <div key={i} className="card card-pad" style={{ padding: 12 }}>
                <div className="badge badge-neutral" style={{ textTransform: "uppercase" }}>{s.name}</div>
                <div style={{ marginTop: 6, fontWeight: 600 }}>
                  {typeof s.target_level === "number" ? money(s.target_level) : "—"}</div>
                <p className="small muted" style={{ marginTop: 4 }}>if {s.condition}</p>
                {s.narrative && <p className="small">{s.narrative}</p>}
              </div>
            ))}
          </div>
        </Section>
      )}

      <div className="grid grid-2">
        {(report.risks ?? []).length > 0 && (
          <Section title="Key risks">
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {report.risks.slice(0, 6).map((r: string, i: number) => <li key={i}>{r}</li>)}
            </ul>
          </Section>
        )}
        {(report.watch_next ?? []).length > 0 && (
          <Section title="Watch next">
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {report.watch_next.slice(0, 5).map((w: string, i: number) => <li key={i}>{w}</li>)}
            </ul>
          </Section>
        )}
      </div>

      {report.disclaimer && <p className="small faint" style={{ marginTop: 12 }}>{report.disclaimer}</p>}

      <div className="divider" />
      <button className="btn btn-sm btn-ghost" onClick={() => setRaw(!raw)}>
        {raw ? "Hide" : "Show"} raw report
      </button>
      {raw && <pre className="log" style={{ marginTop: 10, maxHeight: 300, overflow: "auto" }}>
        {JSON.stringify(report, null, 2)}</pre>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: any }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div className="section-title" style={{ margin: "0 0 6px" }}>{title}</div>
      {children}
    </div>
  );
}
