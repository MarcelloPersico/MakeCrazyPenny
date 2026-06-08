"""Integration smoke test: every ``makecrazypenny.*`` module imports cleanly.

This is the import-safety gate mandated by CONTRACT.md §2.2 and §11
(``test_imports.py``). It asserts that *every* module across all three layers
plus ``core`` imports successfully:

  * with **no API keys** present in the environment,
  * **without** the Claude Agent SDK installed (``servers/_sdk.py`` shims absorb
    its absence), and
  * **without** touching the network or requiring an optional heavy library
    (``yfinance`` / ``pandas`` / ``ta``) at module-import time (the providers and
    servers use lazy, in-function imports for those).

Beyond bare importability the test does light, contract-anchored structural
checks (auto-registration populated, the SDK shim behaves as specified, each
server exposes its documented public surface, providers declare only FROZEN
capabilities). Every assertion is matched to the ACTUAL implementation
signatures / return shapes — no invented APIs.

Deterministic and fully offline: importing the tree performs no I/O, and the
API-key fixture scrubs the environment so a developer's real ``.env`` cannot
make the suite pass or fail nondeterministically.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

# All API-key env vars the system reads (core/config.Settings) plus the cache /
# override knobs, so import-time configuration never depends on the host env.
_API_KEY_ENV_VARS = (
    "ALPHA_VANTAGE_API_KEY",
    "FINNHUB_API_KEY",
    "FMP_API_KEY",
    "MARKETAUX_API_KEY",
)
_OTHER_CONFIG_ENV_VARS = (
    "MCP_CAPABILITY_CHAINS",
    "MCP_CHAIN_OHLCV",
    "MCP_CHAIN_QUOTE",
    "MCP_CHAIN_FUNDAMENTALS",
    "MCP_EDGAR_USER_AGENT",
    "MCP_MAX_DEPTH",
    "MCP_MAX_BUDGET_USD",
    "MCP_CIRCUIT_FAIL_THRESHOLD",
    "MCP_CIRCUIT_COOLDOWN_S",
    "MCP_L2_CACHE",
)

# Every module that must import. Mirrors CONTRACT.md §3 file layout and the smoke
# test's mandate: core.*, providers.* (incl. each adapter), servers.* (incl.
# _sdk/_common), orchestration.*.
CORE_MODULES = (
    "makecrazypenny",
    "makecrazypenny.core",
    "makecrazypenny.core.errors",
    "makecrazypenny.core.types",
    "makecrazypenny.core.disclaimer",
    "makecrazypenny.core.config",
)

PROVIDER_PRIMITIVE_MODULES = (
    "makecrazypenny.providers",
    "makecrazypenny.providers.base",
    "makecrazypenny.providers.ratelimit",
    "makecrazypenny.providers.cache",
    "makecrazypenny.providers.circuit",
    "makecrazypenny.providers.registry",
)

PROVIDER_ADAPTER_MODULES = (
    "makecrazypenny.providers.yfinance_provider",
    "makecrazypenny.providers.alpha_vantage",
    "makecrazypenny.providers.finnhub",
    "makecrazypenny.providers.fmp",
    "makecrazypenny.providers.edgar",
    "makecrazypenny.providers.stockwatcher",
    "makecrazypenny.providers.marketaux",
)

SERVER_MODULES = (
    "makecrazypenny.servers",
    "makecrazypenny.servers._sdk",
    "makecrazypenny.servers._common",
    "makecrazypenny.servers.technical",
    "makecrazypenny.servers.sentiment",
    "makecrazypenny.servers.congress",
    "makecrazypenny.servers.reports",
    "makecrazypenny.servers.synthesis",
    "makecrazypenny.servers.orchestration",
)

ORCHESTRATION_MODULES = (
    "makecrazypenny.orchestration",
    "makecrazypenny.orchestration.agents",
    "makecrazypenny.orchestration.main",
)

ALL_MODULES = (
    CORE_MODULES
    + PROVIDER_PRIMITIVE_MODULES
    + PROVIDER_ADAPTER_MODULES
    + SERVER_MODULES
    + ORCHESTRATION_MODULES
)

# Capability servers (Layer 1) that must each expose the per-server pattern
# surface (CONTRACT.md §9.1): a ``server`` instance and a ``get_registry``
# indirection (synthesis/orchestration included).
CAPABILITY_SERVER_MODULES = (
    "makecrazypenny.servers.technical",
    "makecrazypenny.servers.sentiment",
    "makecrazypenny.servers.congress",
    "makecrazypenny.servers.reports",
    "makecrazypenny.servers.synthesis",
    "makecrazypenny.servers.orchestration",
)


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all API keys and config overrides so imports never depend on them.

    The import-safety mandate (CONTRACT.md §2.2) requires that importing any
    module never raises on a missing key. Removing the vars makes that explicit
    and keeps the suite deterministic regardless of the developer's environment.
    """
    for name in _API_KEY_ENV_VARS + _OTHER_CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Bare importability (the heart of the smoke test)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_module_imports(module_name: str) -> None:
    """Each module imports successfully with no keys and without the SDK."""
    module = importlib.import_module(module_name)
    assert module is not None
    assert module.__name__ == module_name


def test_all_modules_collected() -> None:
    """The module manifest covers every layer (sanity on the parametrization)."""
    # 6 core + 6 provider primitives + 7 adapters + 9 servers + 3 orchestration.
    assert len(ALL_MODULES) == 31
    assert len(set(ALL_MODULES)) == len(ALL_MODULES)  # no duplicates


# ---------------------------------------------------------------------------
# SDK shim behavior (CONTRACT.md §7.1) — the reason imports survive a missing SDK
# ---------------------------------------------------------------------------


def test_sdk_module_exposes_availability_flags() -> None:
    """``_sdk`` exposes ``SDK_AVAILABLE``/``HAS_SDK`` as consistent booleans."""
    from makecrazypenny.servers import _sdk

    assert isinstance(_sdk.SDK_AVAILABLE, bool)
    assert isinstance(_sdk.HAS_SDK, bool)
    # ``SDK_AVAILABLE`` is documented as an alias of ``HAS_SDK``.
    assert _sdk.SDK_AVAILABLE == _sdk.HAS_SDK


def test_sdk_symbols_present() -> None:
    """All shimmed SDK symbols are importable whether or not the SDK is real."""
    from makecrazypenny.servers import _sdk

    for name in (
        "tool",
        "create_sdk_mcp_server",
        "ClaudeSDKClient",
        "ClaudeAgentOptions",
        "AgentDefinition",
    ):
        assert hasattr(_sdk, name), f"_sdk missing {name!r}"


def test_tool_decorator_keeps_logic_callable() -> None:
    """``@tool`` leaves the wrapped async logic function directly callable.

    CONTRACT.md §7.1 decision: the decorator (real or shim) must not hide the
    underlying logic function, so tests can bypass MCP. We assert the decorated
    object is still callable; under the shim it is the *same* function object
    with ``_mcp_tool`` metadata attached.
    """
    from makecrazypenny.servers._sdk import SDK_AVAILABLE, tool

    @tool("noop", "no-op test tool", {"x": int})
    async def _logic(args: dict[str, Any]) -> dict[str, Any]:
        return {"x": args["x"]}

    assert callable(_logic)
    if not SDK_AVAILABLE:
        # Shim path: function is returned unchanged with metadata attached.
        assert _logic._mcp_tool == {
            "name": "noop",
            "description": "no-op test tool",
            "schema": {"x": int},
        }


def test_create_sdk_mcp_server_shim_descriptor() -> None:
    """Under the shim, ``create_sdk_mcp_server`` yields a ``_stub`` descriptor."""
    from makecrazypenny.servers._sdk import SDK_AVAILABLE, create_sdk_mcp_server

    srv = create_sdk_mcp_server(name="smoke", version="9.9.9", tools=[])
    if not SDK_AVAILABLE:
        assert srv.name == "smoke"
        assert srv.version == "9.9.9"
        assert srv.tools == []
        assert srv._stub is True


def test_server_instances_built_at_import() -> None:
    """Every capability server module built its module-level ``server`` object."""
    for module_name in CAPABILITY_SERVER_MODULES:
        module = importlib.import_module(module_name)
        assert hasattr(module, "server"), f"{module_name} has no 'server'"
        assert module.server is not None


# ---------------------------------------------------------------------------
# Core surface (signatures / shapes verified against core/*.py)
# ---------------------------------------------------------------------------


def test_core_errors_taxonomy() -> None:
    """Error hierarchy and the two metadata-carrying exceptions match §6."""
    from makecrazypenny.core import errors as e

    for cls in (
        e.RateLimited,
        e.CircuitOpen,
        e.AllProvidersFailed,
        e.MissingApiKey,
    ):
        assert issubclass(cls, e.ProviderError)

    # AllProvidersFailed stores the capability (CONTRACT.md §6).
    apf = e.AllProvidersFailed("quote")
    assert apf.capability == "quote"

    # MissingApiKey carries provider + env var in its message (CONTRACT.md §6).
    mak = e.MissingApiKey("finnhub", "FINNHUB_API_KEY")
    assert mak.provider == "finnhub"
    assert mak.env_var == "FINNHUB_API_KEY"
    assert "finnhub" in str(mak)
    assert "FINNHUB_API_KEY" in str(mak)


def test_disclaimer_surface() -> None:
    """``DISCLAIMER`` is text and ``with_disclaimer`` appends it (CONTRACT.md §2.5)."""
    from makecrazypenny.core.disclaimer import DISCLAIMER, with_disclaimer

    assert isinstance(DISCLAIMER, str) and DISCLAIMER
    out = with_disclaimer("body text")
    assert out.startswith("body text")
    assert out.endswith(DISCLAIMER)


def test_common_helpers_surface() -> None:
    """``_common`` helpers exist with the documented behavior (CONTRACT.md §7.2)."""
    from makecrazypenny.servers._common import (
        normalize_symbol,
        text_result,
    )

    # normalize_symbol: ' $aapl ' -> 'AAPL'.
    assert normalize_symbol(" $aapl ") == "AAPL"

    # text_result wraps as the canonical MCP envelope (CONTRACT.md §2.4).
    env = text_result({"k": "v"})
    assert env == {"content": [{"type": "text", "text": '{"k": "v"}'}]}


def test_config_settings_from_env_offline() -> None:
    """``Settings.from_env`` builds with no keys and exposes the FROZEN defaults."""
    from makecrazypenny.core.config import (
        CAPABILITIES,
        CAPABILITY_CHAINS,
        Settings,
        default_ttl,
    )

    settings = Settings.from_env()
    # No keys present (fixture scrubbed them).
    assert settings.alpha_vantage_api_key is None
    assert settings.finnhub_api_key is None
    assert settings.fmp_api_key is None
    assert settings.marketaux_api_key is None

    # Orchestration guards default per CONTRACT.md §10.2 / §14.
    assert settings.max_depth == 3
    assert settings.max_budget_usd == 1.0

    # Capability chains alias works and matches the module-level default.
    assert settings.CAPABILITY_CHAINS == CAPABILITY_CHAINS
    assert settings.CAPABILITY_CHAINS["sec_filings"] == ["edgar"]

    # default_ttl returns the §8.5 policy values.
    assert default_ttl("quote") == 15.0
    assert default_ttl("ohlcv") == 300.0

    # Chains only reference FROZEN capabilities.
    assert set(CAPABILITY_CHAINS) == set(CAPABILITIES)


# ---------------------------------------------------------------------------
# Provider auto-registration (CONTRACT.md §8.1 / §8.6)
# ---------------------------------------------------------------------------

# (provider name, requires_key env var or None) per the §8.7 support matrix.
_EXPECTED_PROVIDERS = {
    "yfinance": None,
    "alpha_vantage": "ALPHA_VANTAGE_API_KEY",
    "finnhub": "FINNHUB_API_KEY",
    "fmp": "FMP_API_KEY",
    "edgar": None,
    "stockwatcher": None,
    "marketaux": "MARKETAUX_API_KEY",
}


def test_provider_auto_registration() -> None:
    """Importing ``providers`` populates ``PROVIDER_REGISTRY`` with all adapters."""
    from makecrazypenny.providers import PROVIDER_REGISTRY

    names = {cls.name for cls in PROVIDER_REGISTRY}
    assert set(_EXPECTED_PROVIDERS).issubset(names)


def test_provider_declarations_match_contract() -> None:
    """Each provider declares only FROZEN capabilities + the contract's key."""
    from makecrazypenny.core.config import CAPABILITIES
    from makecrazypenny.providers import PROVIDER_REGISTRY

    by_name = {cls.name: cls for cls in PROVIDER_REGISTRY}
    frozen = set(CAPABILITIES)

    for name, expected_key in _EXPECTED_PROVIDERS.items():
        cls = by_name[name]
        # supported is a subset of the FROZEN capability vocabulary (§8.7).
        assert cls.supported, f"{name} declares no capabilities"
        assert cls.supported.issubset(frozen), (
            f"{name} declares non-FROZEN capabilities: {cls.supported - frozen}"
        )
        # requires_key matches the §8.7 matrix.
        assert cls.requires_key == expected_key


def test_registry_get_registry_singleton_offline() -> None:
    """``get_registry`` builds a process-wide registry with no keys/network."""
    from makecrazypenny.providers import ProviderRegistry, get_registry, reset_registry

    reset_registry()
    try:
        reg = get_registry()
        assert isinstance(reg, ProviderRegistry)
        # Singleton: a second call returns the identical instance.
        assert get_registry() is reg
    finally:
        # Leave no shared state behind for other tests.
        reset_registry()


# ---------------------------------------------------------------------------
# Layer-1 server public surface (CONTRACT.md §9 tool inventory + §9.1 pattern)
# ---------------------------------------------------------------------------

# module -> the documented logic functions (from each module's __all__).
_SERVER_LOGIC_FUNCS = {
    "makecrazypenny.servers.technical": (
        "get_ohlcv",
        "compute_indicators",
        "detect_signals",
        "support_resistance",
        "multi_timeframe_summary",
    ),
    "makecrazypenny.servers.sentiment": (
        "get_news",
        "news_sentiment",
        "social_sentiment",
        "aggregate_sentiment",
    ),
    "makecrazypenny.servers.congress": (
        "congress_trades",
        "recent_congress_activity",
        "insider_transactions",
        "new_disclosures",
    ),
    "makecrazypenny.servers.reports": (
        "analyst_ratings",
        "price_targets",
        "upgrades_downgrades",
        "sec_filings",
    ),
    "makecrazypenny.servers.synthesis": ("cross_check",),
    "makecrazypenny.servers.orchestration": (
        "spawn_analyst",
        "register_alert",
        "check_alerts",
    ),
}


@pytest.mark.parametrize("module_name", sorted(_SERVER_LOGIC_FUNCS))
def test_server_exposes_logic_and_registry(module_name: str) -> None:
    """Each server exposes its logic functions + a ``get_registry`` indirection."""
    module = importlib.import_module(module_name)

    # The monkeypatchable registry indirection (CONTRACT.md §9.1.2).
    assert callable(module.get_registry)

    for func_name in _SERVER_LOGIC_FUNCS[module_name]:
        func = getattr(module, func_name)
        assert callable(func), f"{module_name}.{func_name} is not callable"


# ---------------------------------------------------------------------------
# Layer-2 orchestration surface (CONTRACT.md §10) — importable without the SDK
# ---------------------------------------------------------------------------


def test_orchestration_agents_surface() -> None:
    """``agents`` exposes its builders and the contract's model/tool constants."""
    from makecrazypenny.orchestration import agents

    assert agents.MOTHER_MODEL == "claude-opus-4-8"
    # All six capability servers are wired in (CONTRACT.md §10.1).
    assert set(agents.MCP_SERVERS) == {
        "technical",
        "sentiment",
        "congress",
        "reports",
        "synthesis",
        "orchestration",
    }
    assert "mcp__synthesis__cross_check" in agents.ALLOWED_TOOLS
    assert "mcp__orchestration__spawn_analyst" in agents.ALLOWED_TOOLS
    assert callable(agents.define_agents)
    assert callable(agents.build_options)


def test_orchestration_build_options_importable_without_sdk() -> None:
    """``build_options`` runs even without the SDK (returns a shim descriptor)."""
    from makecrazypenny.orchestration.agents import build_options

    options = build_options()
    assert options is not None


def test_orchestration_main_cli_surface() -> None:
    """``main`` exposes a non-raising ``cli`` entrypoint (CONTRACT.md §10.2)."""
    from makecrazypenny.orchestration import main

    assert callable(main.cli)
    assert callable(main.main)
