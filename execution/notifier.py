"""Run-summary notifications.

A tiny, dependency-free notifier. Today it supports the ``stdout`` channel;
the ``_deliver`` hook is the single place to extend with Slack / email / etc.
The digest is always *built* (and returned) so callers can log it; it is only
*delivered* over the configured channel when ``notifications.enabled`` is true.
"""
from __future__ import annotations

from typing import Any, Callable


class Notifier:
    def __init__(self, cfg: dict | None = None, audit: Callable | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.channel = cfg.get("channel", "stdout")
        self._audit = audit

    def send_digest(self, run_id: str, mode: str, summary: dict,
                    account: Any = None) -> str:
        text = self._format(run_id, mode, summary, account)
        if self._audit:
            self._audit("INFO", "notification_digest",
                        {"run_id": run_id, "channel": self.channel,
                         "enabled": self.enabled, "summary": summary})
        if self.enabled:
            self._deliver(text)
        return text

    def _deliver(self, text: str) -> None:
        if self.channel == "stdout":
            print("\n📨 NOTIFICATION DIGEST\n" + text)
        # Extend here: elif self.channel == "slack": ... / "email": ...

    @staticmethod
    def _format(run_id: str, mode: str, summary: dict, account: Any) -> str:
        lines = [
            f"Run {run_id}  ({mode} mode)",
            f"  proposed={summary.get('orders_proposed', 0)} "
            f"approved={summary.get('orders_approved', 0)} "
            f"executed={summary.get('trades_executed', 0)} "
            f"(buys={summary.get('buys', 0)} sells={summary.get('sells', 0)})",
            f"  bought=${summary.get('gross_bought', 0):,.2f} "
            f"sold=${summary.get('gross_sold', 0):,.2f} "
            f"net=${summary.get('net_cash_flow', 0):,.2f}",
        ]
        if summary.get("needs_review"):
            lines.append(f"  ⚠️  {summary['needs_review']} order(s) NEED HUMAN REVIEW")
        if summary.get("rejected_or_skipped"):
            lines.append(f"  rejected/skipped={summary['rejected_or_skipped']}")
        if summary.get("equity") is not None:
            lines.append(
                f"  equity=${summary['equity']:,.2f} cash=${summary.get('cash', 0):,.2f} "
                f"drawdown={summary.get('drawdown_pct', 0):.2f}%")
        return "\n".join(lines)
