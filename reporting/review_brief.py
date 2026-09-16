"""Render a post-close review into ``reports/YYYY-MM-DD-review.md``.

Companion to :mod:`reporting.brief`. Where the morning brief answers *what
should I do with each stock*, this one answers *what did the agents get right
and wrong, and what changed because of it*. Read-only: it formats what the
review agent already persisted; it never calls a model or touches config.

Run standalone:  python -m reporting.review_brief [--account NUMBER] [--out-dir reports]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from server.format import DASH, fmt_money, fmt_pct

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "storage" / "trading.db"
_DEFAULT_OUT = _REPO / "reports"


def _p(v, *, signed=True) -> str:
    return DASH if v is None or v == "inf" else fmt_pct(float(v), signed=signed)


def _m(v) -> str:
    return DASH if v is None else fmt_money(float(v))


def _n(v) -> str:
    if v is None:
        return DASH
    if v == "inf":
        return "∞"
    return f"{float(v):.2f}"


def render_review(scorecard: dict, output: dict, proposals: list[dict],
                  *, account_label: str = "", day: str = "",
                  lesson_counts: dict | None = None) -> str:
    a = (scorecard.get("aggregates") or {})
    allst = a.get("all") or {}
    L: list[str] = []
    L.append(f"# Review — {day or scorecard.get('as_of', '')}")
    L.append("")
    L.append(f"*Post-close review{(' · ' + account_label) if account_label else ''} · "
             f"window {scorecard.get('window_days')}d · "
             f"{scorecard.get('n_closed', 0)} closed trade(s) in window*")
    L.append("")
    if output.get("day_summary"):
        L.append(output["day_summary"])
        L.append("")

    # ---- scorecard headline ----------------------------------------------
    L.append("## Scorecard")
    L.append("")
    L.append("| Closed | Win rate | Expectancy | Avg win | Avg loss | Profit factor | Avg R | Realized P&L |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    L.append(f"| {allst.get('n', 0)} | {_p(allst.get('win_rate'), signed=False)} | "
             f"{_p(allst.get('expectancy_pct'))} | {_p(allst.get('avg_win_pct'))} | "
             f"{_p(allst.get('avg_loss_pct'))} | {_n(allst.get('profit_factor'))} | "
             f"{_n(allst.get('avg_r'))} | {_m(allst.get('total_pnl_usd'))} |")
    L.append("")

    def bucket_table(title: str, groups: dict) -> None:
        if not groups:
            return
        L.append(f"**{title}**")
        L.append("")
        L.append("| Bucket | n | Win rate | Expectancy | P&L |")
        L.append("|---|---:|---:|---:|---:|")
        for k, s in groups.items():
            L.append(f"| {k} | {s.get('n', 0)} | {_p(s.get('win_rate'), signed=False)} | "
                     f"{_p(s.get('expectancy_pct'))} | {_m(s.get('total_pnl_usd'))} |")
        L.append("")

    bucket_table("By entry score", a.get("by_score_bucket") or {})
    bucket_table("By size (% of equity at entry)", a.get("by_size_bucket") or {})
    bucket_table("By exit trigger", a.get("by_exit_trigger") or {})

    calls = scorecard.get("calls") or {}
    by_action = calls.get("by_action") or {}
    if by_action:
        hs = calls.get("horizons") or []
        L.append("**What the agent's calls did next** (mean forward return · hit rate)")
        L.append("")
        L.append("| Call | n | " + " | ".join(f"+{h}d" for h in hs) + " |")
        L.append("|---|---:|" + "|".join("---:" for _ in hs) + "|")
        for act, s in by_action.items():
            cells = []
            for h in hs:
                m, hit = s.get(f"r{h}_mean"), s.get(f"r{h}_hit")
                cells.append(DASH if m is None else f"{_p(m)} · {_p(hit, signed=False)}")
            L.append(f"| {act} | {s.get('n', 0)} | " + " | ".join(cells) + " |")
        L.append("")

    # ---- closed legs ------------------------------------------------------
    legs = sorted(scorecard.get("closed_legs") or [], key=lambda l: l.get("exit_date") or "")
    if legs:
        L.append("## Closed trades (window)")
        L.append("")
        L.append("| Exit | Ticker | Held | Size | Entry→Exit | P&L | R | Trigger | +10d after |")
        L.append("|---|---|---:|---:|---|---:|---:|---|---:|")
        for l in legs[-25:]:
            size = f"{l['size_pct_of_equity']:.0f}%" if l.get("size_pct_of_equity") is not None else DASH
            held = f"{l['hold_days']}d" if l.get("hold_days") is not None else DASH
            L.append(f"| {l.get('exit_date') or DASH} | **{l['ticker']}** | {held} | {size} | "
                     f"{_m(l.get('entry_price'))}→{_m(l.get('exit_price'))} | "
                     f"{_m(l.get('pnl_usd'))} / {_p(l.get('pnl_pct'))} | {_n(l.get('r_multiple'))} | "
                     f"{l.get('exit_trigger') or DASH} | {_p(l.get('ret_10d_after_exit'))} |")
        L.append("")

    # ---- behaviour flags ---------------------------------------------------
    b = scorecard.get("behaviour") or {}
    flags = []
    conc = b.get("concentration") or {}
    if conc.get("top_pct") is not None:
        flags.append(f"Largest position: **{conc.get('top')}** at {conc['top_pct']:.0f}% of equity")
    if b.get("idle_cash_pct") is not None:
        flags.append(f"Idle cash: {b['idle_cash_pct']:.0f}% of equity")
    if b.get("dust_orders"):
        flags.append(f"Dust orders (under the minimum position): {len(b['dust_orders'])} — "
                     + ", ".join(f"{d['ticker']} {_m(d['usd'])}" for d in b["dust_orders"][:6]))
    if b.get("rebuys_within_5d"):
        flags.append("Sold-then-rebought within 5 days: "
                     + ", ".join(f"{r['ticker']} ({r['sold']}→{r['rebought']})"
                                 for r in b["rebuys_within_5d"][:6]))
    if b.get("take_profit_refires"):
        flags.append("Take-profit fired more than once on: "
                     + ", ".join(f"{k} ×{v}" for k, v in b["take_profit_refires"].items()))
    if b.get("broker_rejections"):
        flags.append(f"Broker rejections: {len(b['broker_rejections'])}")
    if b.get("unscored_holdings_latest_run"):
        flags.append("Held but unscored in the latest run: "
                     + ", ".join(b["unscored_holdings_latest_run"]))
    g = b.get("guardrail") or {}
    if g.get("rejected") or g.get("resized"):
        by = ", ".join(f"{k} ×{v}" for k, v in (g.get("by_check") or {}).items())
        flags.append(f"Guardrails: {g.get('rejected', 0)} rejected"
                     + (f" ({by})" if by else "") + f", {g.get('resized', 0)} resized")
    if b.get("decision_fallbacks"):
        flags.append(f"Decision agent fell back to the deterministic policy "
                     f"{b['decision_fallbacks']} time(s)")
    if flags:
        L.append("## Behaviour")
        L.append("")
        for f in flags:
            L.append(f"- {f}")
        L.append("")

    # ---- what worked / didn't ------------------------------------------------
    for title, key in (("What worked", "what_worked"), ("What didn't", "what_didnt")):
        items = output.get(key) or []
        if items:
            L.append(f"## {title}")
            L.append("")
            for it in items:
                L.append(f"- {it}")
            L.append("")

    # ---- lessons -------------------------------------------------------------
    lessons = output.get("lessons") or []
    L.append("## Lessons")
    L.append("")
    if lessons:
        for l in lessons:
            tag = {"new": "NEW", "reinforced": "reinforced", "retired": "RETIRED"}.get(
                l.get("status"), "")
            L.append(f"- **[{tag}]** {l.get('text')} _(confidence {l.get('confidence')}; "
                     f"applies to {', '.join(l.get('applies_to') or [])})_")
            if l.get("evidence"):
                L.append(f"  - evidence: {l['evidence']}")
        if lesson_counts:
            L.append("")
            L.append(f"_{lesson_counts.get('new', 0)} new · {lesson_counts.get('reinforced', 0)} "
                     f"reinforced · {lesson_counts.get('retired', 0)} retired_")
    else:
        L.append("_No lessons written today._")
    L.append("")

    # ---- proposals -------------------------------------------------------------
    L.append("## Parameter proposals")
    L.append("")
    if proposals:
        L.append("| Key | Current | Proposed | Applied | Status | Why |")
        L.append("|---|---:|---:|---:|---|---|")
        for p in proposals:
            L.append(f"| `{p.get('key')}` | {p.get('current')} | {p.get('proposed')} | "
                     f"{p.get('applied') if p.get('applied') is not None else DASH} | "
                     f"**{p.get('status')}** | {p.get('reason') or ''} |")
    else:
        L.append("_None._")
    L.append("")

    # ---- playbook ------------------------------------------------------------
    if output.get("playbook"):
        L.append("## Playbook (in force from the next run)")
        L.append("")
        L.append(output["playbook"])
        L.append("")

    L.append("---")
    L.append("")
    L.append("*Generated by the post-close review agent from the trade ledger. The scorecard "
             "is deterministic; the lessons and playbook are model output — read the evidence, "
             "not the label. Nothing here is financial advice.*")
    L.append("")
    return "\n".join(L)


def write_review(markdown: str, day: str, out_dir: str | Path = _DEFAULT_OUT) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{day}-review.md"
    path.write_text(markdown)
    return path


def main() -> None:
    from storage.db import Database
    ap = argparse.ArgumentParser(description="Render the latest post-close review")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    ap.add_argument("--account", default=None)
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    ap.add_argument("--summary", action="store_true",
                    help="print the one-line day summary only")
    args = ap.parse_args()
    db = Database(args.db)
    rev = db.latest_review(args.account)
    if not rev:
        raise SystemExit("No review found.")
    if args.summary:
        print(((rev.get("output") or {}).get("day_summary") or "review ready")[:200])
        return
    md = rev.get("markdown") or render_review(rev.get("scorecard") or {},
                                              rev.get("output") or {},
                                              db.proposals(rev.get("account")))
    print(write_review(md, rev.get("review_date") or "latest", args.out_dir))


if __name__ == "__main__":
    main()
