"""Layer 0 (write path): authenticated order execution (CONTRACT.md §17).

Every other external call in MakeCrazyPenny is a **read-only, keyless**
``Provider.fetch(capability)``. This package is the deliberate exception: the one
**authenticated, state-mutating** path, used only for **paper trading on the
Hyperliquid testnet**. It is isolated here (not folded into ``providers/``)
precisely because trading is an *action* endpoint that needs a wallet secret and
mutates account state — it does not fit the capability-fetch abstraction.

Hard safety rails (see :mod:`makecrazypenny.execution.hyperliquid`):

* **Testnet-locked** — only the testnet base URL is ever used; there is no
  mainnet field anywhere in :class:`~makecrazypenny.core.config.Settings`.
* **Import-safe** — the Hyperliquid SDK and ``eth_account`` are imported lazily,
  so importing this package never requires the ``trade`` extra and never hits the
  network.
* **Secret-safe** — the signing key is read from ``MCP_HL_PRIVATE_KEY`` and every
  error string is routed through :func:`makecrazypenny.core.redact.redact_secrets`.
"""

from __future__ import annotations

from .hyperliquid import ExecutionError, HyperliquidPaperClient

__all__ = ["HyperliquidPaperClient", "ExecutionError"]
