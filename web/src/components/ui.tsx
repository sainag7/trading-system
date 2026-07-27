import { ReactNode, useCallback, useEffect, useState } from "react";
import { Loader2, Inbox, X } from "lucide-react";

// ---- data fetching hook --------------------------------------------------
export function useFetch<T = any>(fn: () => Promise<T>, deps: any[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    fn()
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => { reload(); }, [reload]);
  return { data, loading, error, reload };
}

// ---- primitives ----------------------------------------------------------
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`card ${className}`}>{children}</div>;
}

export function CardHead({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card-head">
      <h3>{title}</h3>
      <div className="spacer" />
      {children}
    </div>
  );
}

export function Stat({ label, value, sub, tone }: {
  label: string; value: ReactNode; sub?: ReactNode; tone?: "pos" | "neg";
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
      {sub != null && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

const ACTION_CLASS: Record<string, string> = {
  buy: "badge-buy", add: "badge-add", trim: "badge-trim",
  sell: "badge-sell", hold: "badge-hold", pass: "badge-pass",
};
export function ActionBadge({ action }: { action: string }) {
  const a = (action || "").toLowerCase();
  return (
    <span className={`badge ${ACTION_CLASS[a] ?? "badge-neutral"}`}>
      <span className="dot" />{a || "—"}
    </span>
  );
}

export function ScoreBar({ value }: { value: number | null | undefined }) {
  const v = typeof value === "number" ? Math.max(0, Math.min(100, value)) : null;
  const color = v == null ? "var(--border-strong)"
    : v >= 70 ? "var(--green)" : v >= 45 ? "var(--primary)" : "var(--amber)";
  return (
    <div className="scorebar">
      <div className="track"><div className="fill" style={{ width: `${v ?? 0}%`, background: color }} /></div>
      <span className="val">{v == null ? "—" : Math.round(v)}</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty"><Inbox /><div>{children}</div></div>;
}

export function Callout({ kind = "info", children }: {
  kind?: "info" | "warn" | "danger" | "ok"; children: ReactNode;
}) {
  return <div className={`callout callout-${kind}`}>{children}</div>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="row muted" style={{ padding: 24, justifyContent: "center" }}>
      <Loader2 className="spin" size={18} /> {label ?? "Loading…"}
    </div>
  );
}

export function Modal({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode;
}) {
  return (
    <div className="scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="card-head">
          <h3>{title}</h3><div className="spacer" />
          <button className="btn btn-icon btn-ghost" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="modal-pad">{children}</div>
      </div>
    </div>
  );
}

export function Segmented<T extends string>({ options, value, onChange }: {
  options: { value: T; label: string }[]; value: T; onChange: (v: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button key={o.value} className={o.value === value ? "active" : ""}
          onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}
