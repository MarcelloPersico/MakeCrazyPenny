"""Shared pytest fixtures.

The suite is contractually deterministic and OFFLINE: tests must not read the
developer's real ``.env`` or any ambient API keys, and must not depend on whether
keys happen to be set on the host. The autouse fixture below enforces that — it
scrubs the relevant environment variables and neutralizes ``.env`` loading for
every test, so results are identical on a clean CI box and on a dev machine with
a fully populated ``.env``.
"""

from __future__ import annotations

import pytest

# Env vars that could otherwise leak real configuration into "offline" tests.
_SCRUBBED_ENV_VARS = (
    "ALPHA_VANTAGE_API_KEY",
    "FINNHUB_API_KEY",
    "FMP_API_KEY",
    "MARKETAUX_API_KEY",
    "MCP_CACHE_DIR",
    "MCP_CAPABILITY_CHAINS",
    "MCP_EDGAR_USER_AGENT",
    "MCP_L2_CACHE",
    # Execution layer (CONTRACT.md §17): never read a real wallet key in tests.
    "MCP_HL_PRIVATE_KEY",
    "MCP_HL_ACCOUNT_ADDRESS",
    "MCP_HL_TESTNET_URL",
    "MCP_HL_SLIPPAGE",
    # Swarm extension (CONTRACT.md §18): info-URL override + risk-gate knobs.
    "MCP_HL_INFO_URL",
    "MCP_SWARM_RISK_GATE",
    "MCP_SWARM_MAX_POSITIONS",
    "MCP_SWARM_MAX_DAILY_LOSS_PCT",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test independent of the host environment and any ``.env``.

    Removes the API-key / config env vars and replaces ``Settings``' optional
    ``.env`` loader with a no-op, so ``Settings.from_env()`` cannot repopulate
    them from a real ``.env`` mid-test. A test that needs a key still sets it
    explicitly (via ``monkeypatch.setenv`` or by constructing ``Settings``), and
    that runs after this fixture, so it wins.
    """
    for var in _SCRUBBED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "makecrazypenny.core.config._load_dotenv_if_available",
        lambda: None,
        raising=True,
    )
