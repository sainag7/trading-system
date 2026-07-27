// Client-side formatting for raw numeric fields inside research/analysis blobs
// (the server pre-formats top-level ts/money fields; these are the nested ones).
const DASH = "—";
const isNum = (v: any): v is number => typeof v === "number" && !Number.isNaN(v);

export const money = (v: any, d = 2) =>
  isNum(v) ? `$${v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d })}` : DASH;
export const num = (v: any, d = 2) =>
  isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: d }) : DASH;
export const pct = (v: any, d = 1) => (isNum(v) ? `${v.toFixed(d)}%` : DASH);
export const signedPct = (v: any, d = 1) => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(d)}%` : DASH);
export const bigMoney = (v: any) => {
  if (!isNum(v)) return DASH;
  const abs = Math.abs(v);
  if (abs >= 1e12) return `$${(v / 1e12).toFixed(1)}T`;
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return money(v, 0);
};
