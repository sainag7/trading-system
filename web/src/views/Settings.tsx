import { useState, ReactNode } from "react";
import { Save, RotateCcw, ShieldAlert, Plus, X, Check } from "lucide-react";
import { api } from "../lib/api";
import { useStatus } from "../lib/status";
import { Card, CardHead, useFetch, Spinner, Callout } from "../components/ui";

export function Settings() {
  const { data, loading, reload } = useFetch(() => api.config(), []);
  const { reload: reloadStatus } = useStatus();
  const [version, setVersion] = useState(0);

  const onSaved = () => { reload(); reloadStatus(); setVersion((v) => v + 1); };

  if (loading || !data) return <div className="view"><Spinner /></div>;

  return (
    <div className="view" key={version}>
      <div className="view-head">
        <h1>Settings</h1>
        <p>Edits are saved to <code>config.local.yaml</code> (merged over your base config).
          API keys live in <code>.env</code> and are never shown here.</p>
      </div>

      <General cfg={data} onSaved={onSaved} />
      <Watchlist cfg={data} onSaved={onSaved} />
      <Weights cfg={data} onSaved={onSaved} />
      <StrategyCard cfg={data} onSaved={onSaved} />
      <Discovery cfg={data} onSaved={onSaved} />
      <ResearchCard cfg={data} onSaved={onSaved} />
      <Models cfg={data} onSaved={onSaved} />
      <DataCard cfg={data} onSaved={onSaved} />
      <ExecutionCard cfg={data} onSaved={onSaved} />
      <Accounts cfg={data} onSaved={onSaved} />
      <RiskCard cfg={data} onSaved={onSaved} />
      <ResetCard cfg={data} onSaved={onSaved} />
    </div>
  );
}

// ---- reusable field inputs ------------------------------------------------
function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="field" style={{ margin: 0 }}><span>{label}</span>{children}</label>;
}
function Num({ label, value, onChange, step = 1 }: any) {
  return <Field label={label}><input className="input" type="number" step={step}
    value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} /></Field>;
}
function Txt({ label, value, onChange, placeholder }: any) {
  return <Field label={label}><input className="input" value={value ?? ""} placeholder={placeholder}
    onChange={(e) => onChange(e.target.value)} /></Field>;
}
function Sel({ label, value, options, onChange }: any) {
  return <Field label={label}><select className="input" value={value}
    onChange={(e) => onChange(e.target.value)}>
    {options.map((o: string) => <option key={o} value={o}>{o}</option>)}</select></Field>;
}
function Tog({ label, value, onChange }: any) {
  return <label className="check" style={{ padding: "8px 0" }}>
    <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />{label}</label>;
}

function useSaver(onSaved: () => void) {
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = async (patch: any) => {
    setSaving(true); setErr(null);
    try { await api.putConfig(patch); setDone(true); setTimeout(() => setDone(false), 2000); onSaved(); }
    catch (e: any) { setErr(e.message ?? "failed"); }
    finally { setSaving(false); }
  };
  return { save, saving, done, err };
}

function SaveBtn({ onClick, saving, done, children }: any) {
  return <button className="btn btn-primary" onClick={onClick} disabled={saving}>
    {done ? <Check size={15} /> : <Save size={15} />} {done ? "Saved" : (children ?? "Save")}
  </button>;
}

function SectionCard({ title, children, footer, err }: any) {
  return (
    <Card className="" >
      <div style={{ marginTop: 16 }} />
      <CardHead title={title} />
      <div className="card-pad">
        {children}
        {err && <div style={{ marginTop: 10 }}><Callout kind="danger">{err}</Callout></div>}
        <div className="row" style={{ marginTop: 14 }}>{footer}</div>
      </div>
    </Card>
  );
}

// ---- sections -------------------------------------------------------------
function General({ cfg, onSaved }: any) {
  const [mode, setMode] = useState(cfg.general.mode);
  const [profile, setProfile] = useState(cfg.general.profile);
  const s = useSaver(onSaved);
  return (
    <SectionCard title="General" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ mode, profile })} saving={s.saving} done={s.done} />}>
      <div className="grid grid-2">
        <Sel label="Default mode (CLI runs)" value={mode} options={cfg.general.modes} onChange={setMode} />
        <Sel label="Default profile" value={profile} options={cfg.general.profiles} onChange={setProfile} />
      </div>
      {mode === "live" && <div style={{ marginTop: 10 }}>
        <Callout kind="warn">Default <code>live</code> mode affects scheduled/CLI runs. Dashboard scans stay advice-only.</Callout></div>}
    </SectionCard>
  );
}

function Watchlist({ cfg, onSaved }: any) {
  const [universe, setUniverse] = useState<string[]>(cfg.universe);
  const [sectors, setSectors] = useState<Record<string, string>>({ ...cfg.sectors });
  const [add, setAdd] = useState("");
  const s = useSaver(onSaved);
  const addTicker = () => {
    const t = add.trim().toUpperCase();
    if (t && !universe.includes(t)) setUniverse([...universe, t]);
    setAdd("");
  };
  return (
    <SectionCard title="Watchlist & sectors" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ universe, sectors })} saving={s.saving} done={s.done} />}>
      <div className="row wrap" style={{ marginBottom: 12 }}>
        {universe.map((t) => (
          <span key={t} className="pill">{t}
            <button className="btn btn-icon btn-ghost" style={{ padding: 2 }}
              onClick={() => setUniverse(universe.filter((x) => x !== t))}><X size={12} /></button>
          </span>
        ))}
      </div>
      <div className="row" style={{ marginBottom: 14 }}>
        <input className="input" style={{ maxWidth: 220 }} placeholder="Add ticker e.g. TSLA"
          value={add} onChange={(e) => setAdd(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addTicker()} />
        <button className="btn" onClick={addTicker}><Plus size={14} /> Add</button>
      </div>
      <div className="section-title" style={{ marginTop: 4 }}>Sector overrides</div>
      <div className="grid grid-3">
        {universe.map((t) => (
          <Field key={t} label={t}>
            <input className="input" value={sectors[t] ?? ""} placeholder="sector"
              onChange={(e) => setSectors({ ...sectors, [t]: e.target.value })} />
          </Field>
        ))}
      </div>
    </SectionCard>
  );
}

function Weights({ cfg, onSaved }: any) {
  const w = cfg.analysis?.weights ?? {};
  const [tech, setTech] = useState(w.technical ?? 0.4);
  const [fund, setFund] = useState(w.fundamental ?? 0.35);
  const [sent, setSent] = useState(w.sentiment ?? 0.25);
  const s = useSaver(onSaved);
  const sum = (tech + fund + sent) || 1;
  return (
    <SectionCard title="Analysis weights" err={s.err}
      footer={<><SaveBtn onClick={() => s.save({ analysis: { weights: { technical: tech, fundamental: fund, sentiment: sent } } })}
        saving={s.saving} done={s.done} /><span className="small muted">normalized from {sum.toFixed(2)}</span></>}>
      <div className="grid grid-3">
        <Num label="Technical" value={tech} step={0.05} onChange={setTech} />
        <Num label="Fundamental" value={fund} step={0.05} onChange={setFund} />
        <Num label="Sentiment" value={sent} step={0.05} onChange={setSent} />
      </div>
    </SectionCard>
  );
}

function StrategyCard({ cfg, onSaved }: any) {
  const st = cfg.strategy ?? {};
  const [d, setD] = useState({ ...st });
  const s = useSaver(onSaved);
  const set = (k: string) => (v: any) => setD({ ...d, [k]: v });
  return (
    <SectionCard title="Strategy scoring" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ strategy: d })} saving={s.saving} done={s.done} />}>
      <div className="grid grid-4">
        <Num label="Min score to buy" value={d.min_score_to_buy} onChange={set("min_score_to_buy")} />
        <Num label="Min score to add" value={d.min_score_to_add} onChange={set("min_score_to_add")} />
        <Num label="Trim below score" value={d.trim_below_score} onChange={set("trim_below_score")} />
        <Num label="Exit below score" value={d.exit_below_score} onChange={set("exit_below_score")} />
        <Num label="Default stop %" value={d.default_stop_loss_pct} step={0.01} onChange={set("default_stop_loss_pct")} />
        <Num label="Default take-profit %" value={d.default_take_profit_pct} step={0.01} onChange={set("default_take_profit_pct")} />
        <Num label="Max holding days" value={d.max_holding_days} onChange={set("max_holding_days")} />
        <Num label="Target portfolio size" value={d.target_portfolio_size} onChange={set("target_portfolio_size")} />
      </div>
    </SectionCard>
  );
}

function Discovery({ cfg, onSaved }: any) {
  const [d, setD] = useState({ ...(cfg.discovery ?? {}) });
  const s = useSaver(onSaved);
  const set = (k: string) => (v: any) => setD({ ...d, [k]: v });
  return (
    <SectionCard title="Discovery" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ discovery: d })} saving={s.saving} done={s.done} />}>
      <div className="row wrap" style={{ gap: 20 }}>
        <Tog label="Dynamic discovery" value={d.dynamic_discovery} onChange={set("dynamic_discovery")} />
        <Tog label="yfinance screeners" value={d.use_yfinance_screeners} onChange={set("use_yfinance_screeners")} />
        <Tog label="Yahoo trending" value={d.use_yahoo_trending} onChange={set("use_yahoo_trending")} />
        <Tog label="Reddit" value={d.use_reddit} onChange={set("use_reddit")} />
      </div>
      <div className="grid grid-4" style={{ marginTop: 10 }}>
        <Num label="Max discovered" value={d.max_discovered} onChange={set("max_discovered")} />
        <Num label="Min avg volume" value={d.min_avg_volume} step={100000} onChange={set("min_avg_volume")} />
        <Num label="Min price $" value={d.min_price} onChange={set("min_price")} />
        <Num label="Max price $" value={d.max_price} onChange={set("max_price")} />
      </div>
    </SectionCard>
  );
}

function ResearchCard({ cfg, onSaved }: any) {
  const r = cfg.research ?? {}; const rec = cfg.recommend ?? {};
  const [llm, setLlm] = useState(r.llm_enrichment ?? false);
  const [conc, setConc] = useState(r.concurrency ?? 5);
  const [hypo, setHypo] = useState(rec.hypothetical_cash ?? 10000);
  const s = useSaver(onSaved);
  return (
    <SectionCard title="Research & sizing" err={s.err}
      footer={<SaveBtn saving={s.saving} done={s.done}
        onClick={() => s.save({ research: { llm_enrichment: llm, concurrency: conc }, recommend: { hypothetical_cash: hypo } })} />}>
      <div className="row wrap" style={{ gap: 20, marginBottom: 8 }}>
        <Tog label="Per-ticker LLM notes (slower)" value={llm} onChange={setLlm} />
      </div>
      <div className="grid grid-2">
        <Num label="Research concurrency" value={conc} onChange={setConc} />
        <Num label="Hypothetical cash $ (recommend)" value={hypo} step={500} onChange={setHypo} />
      </div>
    </SectionCard>
  );
}

function Models({ cfg, onSaved }: any) {
  const [d, setD] = useState({ ...(cfg.models ?? {}) });
  const s = useSaver(onSaved);
  const set = (k: string) => (v: any) => setD({ ...d, [k]: v });
  return (
    <SectionCard title="Per-agent models" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ models: d })} saving={s.saving} done={s.done} />}>
      <div className="grid grid-2">
        {Object.keys(d).map((k) => <Txt key={k} label={k} value={d[k]} onChange={set(k)} />)}
      </div>
    </SectionCard>
  );
}

function DataCard({ cfg, onSaved }: any) {
  const [d, setD] = useState({ ...(cfg.data ?? {}) });
  const s = useSaver(onSaved);
  const set = (k: string) => (v: any) => setD({ ...d, [k]: v });
  return (
    <SectionCard title="Data providers" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ data: d })} saving={s.saving} done={s.done} />}>
      <div className="row wrap" style={{ gap: 20, marginBottom: 8 }}>
        <Tog label="yfinance fallback" value={d.use_yfinance_fallback} onChange={set("use_yfinance_fallback")} />
        <Tog label="FRED macro" value={d.use_fred} onChange={set("use_fred")} />
      </div>
      <div className="grid grid-2">
        <Num label="Cache TTL (minutes)" value={d.cache_ttl_minutes} onChange={set("cache_ttl_minutes")} />
      </div>
    </SectionCard>
  );
}

function ExecutionCard({ cfg, onSaved }: any) {
  const [d, setD] = useState({ ...(cfg.execution ?? {}) });
  const s = useSaver(onSaved);
  const set = (k: string) => (v: any) => setD({ ...d, [k]: v });
  return (
    <SectionCard title="Execution" err={s.err}
      footer={<SaveBtn onClick={() => s.save({ execution: d })} saving={s.saving} done={s.done} />}>
      <div className="grid grid-3">
        <Sel label="Order type" value={d.order_type ?? "limit"} options={["limit", "market"]} onChange={set("order_type")} />
        <Num label="Limit slippage %" value={d.limit_slippage_pct} step={0.001} onChange={set("limit_slippage_pct")} />
        <Num label="Max order retries" value={d.max_order_retries} onChange={set("max_order_retries")} />
      </div>
      <div style={{ marginTop: 6 }}>
        <Tog label="Read live account (read-only) in recommend/explain" value={d.read_live_account} onChange={set("read_live_account")} />
      </div>
    </SectionCard>
  );
}

function Accounts({ cfg, onSaved }: any) {
  const accts = cfg.accounts ?? {};
  const [d, setD] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(accts).map(([role, m]: any) => [role, m?.number ?? ""]))
  );
  const s = useSaver(onSaved);
  if (!Object.keys(accts).length) return null;
  const patch = { accounts: Object.fromEntries(Object.entries(d).map(([role, number]) => [role, { number }])) };
  return (
    <SectionCard title="Accounts" err={s.err}
      footer={<SaveBtn onClick={() => s.save(patch)} saving={s.saving} done={s.done} />}>
      <Callout kind="warn"><ShieldAlert size={16} />
        <div>Account numbers route real money. The <strong>agentic</strong> account is the autonomous
          $100 book; <strong>individual</strong> is advice-only.</div></Callout>
      <div className="grid grid-2" style={{ marginTop: 12 }}>
        {Object.keys(accts).map((role) => (
          <Txt key={role} label={`${role} account number`} value={d[role]}
            onChange={(v: string) => setD({ ...d, [role]: v })} />
        ))}
      </div>
    </SectionCard>
  );
}

function RiskCard({ cfg, onSaved }: any) {
  const [d, setD] = useState({ ...(cfg.risk ?? {}) });
  const [confirm, setConfirm] = useState(false);
  const s = useSaver(onSaved);
  const set = (k: string) => (v: any) => setD({ ...d, [k]: v });
  const save = () => {
    if (!confirm) { s.err; return; }
    const noTrade = Array.isArray(d.no_trade_list) ? d.no_trade_list
      : String(d.no_trade_list || "").split(",").map((t) => t.trim().toUpperCase()).filter(Boolean);
    s.save({ risk: { ...d, no_trade_list: noTrade } });
  };
  return (
    <SectionCard title="Hard risk limits" err={s.err}
      footer={<><SaveBtn onClick={save} saving={s.saving || !confirm} done={s.done}>Save risk limits</SaveBtn></>}>
      <Callout kind="danger"><ShieldAlert size={16} />
        <div>These are your hard safety limits, enforced on every order. Changing them changes your safety budget.</div></Callout>
      <div className="grid grid-4" style={{ marginTop: 12 }}>
        <Num label="Max positions" value={d.max_positions} onChange={set("max_positions")} />
        <Num label="Max position %" value={d.max_position_pct} step={0.01} onChange={set("max_position_pct")} />
        <Num label="Max sector %" value={d.max_sector_pct} step={0.05} onChange={set("max_sector_pct")} />
        <Num label="Per-trade max $" value={d.per_trade_max_usd} step={25} onChange={set("per_trade_max_usd")} />
        <Num label="Daily max trades" value={d.daily_max_trades} onChange={set("daily_max_trades")} />
        <Num label="Min cash reserve %" value={d.min_cash_reserve_pct} step={0.05} onChange={set("min_cash_reserve_pct")} />
        <Num label="Drawdown halt %" value={d.max_account_drawdown_halt_pct} step={0.01} onChange={set("max_account_drawdown_halt_pct")} />
        <Num label="Min trade $" value={d.min_trade_usd} onChange={set("min_trade_usd")} />
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <Tog label="Allow fractional shares" value={d.allow_fractional_shares} onChange={set("allow_fractional_shares")} />
      </div>
      <Txt label="No-trade list (comma-separated)"
        value={Array.isArray(d.no_trade_list) ? d.no_trade_list.join(", ") : d.no_trade_list}
        onChange={set("no_trade_list")} />
      <label className="check" style={{ marginTop: 10 }}>
        <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
        I understand this changes my hard safety limits
      </label>
    </SectionCard>
  );
}

function ResetCard({ cfg, onSaved }: any) {
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  const has = cfg.overrides && Object.keys(cfg.overrides).length > 0;
  const reset = async () => { setBusy(true); try { await api.resetConfig(); onSaved(); } finally { setBusy(false); } };
  return (
    <SectionCard title="Reset overrides"
      footer={<button className="btn btn-danger" disabled={!sure || busy || !has} onClick={reset}>
        <RotateCcw size={15} /> Reset all overrides</button>}>
      <p className="muted small" style={{ marginTop: 0 }}>
        {has ? "Removes config.local.yaml and returns every setting to your base config.yaml."
          : "No overrides set — you're on the base config."}</p>
      {has && <label className="check"><input type="checkbox" checked={sure}
        onChange={(e) => setSure(e.target.checked)} /> Really reset all dashboard overrides</label>}
    </SectionCard>
  );
}
