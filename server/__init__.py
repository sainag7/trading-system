"""Web-app backend for the trading system.

A thin FastAPI layer over the existing (already-importable) Python backend —
``Orchestrator``, ``Config``, ``Database``, ``Executor``, ``market`` and the
scheduling scripts. No order-placement logic lives here: every trade still routes
through ``Orchestrator``/``Executor`` and its guardrails/kill-switch. The server
binds to localhost only (it can place real orders).
"""
