"""Quantitative analysis primitives (CONTRACT.md §10.7).

Pure, deterministic computations layered on the free OHLCV / fundamentals data —
factor signals, position sizing, market-regime detection, and backtesting. Each
module separates a **pure core** (operates on plain lists/dicts, no I/O) from a
thin **async fetcher** that pulls data through the Layer-0 registry, mirroring the
server pattern so the cores stay unit-testable offline with no SDK, no keys, and
no network.

These power the evidence/decision engine (``orchestration.debate`` folds factor
signals into scoring and risk sizing + regime into the decision) and the
portfolio/backtest surfaces.
"""

from __future__ import annotations

__all__: list[str] = []
