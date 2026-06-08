"""Orchestration capability server: recursion + alerts (see CONTRACT.md §9.2).

This Layer-1 server exposes three MCP tools:

  * :func:`spawn_analyst` — a *bounded* recursive nested ``ClaudeSDKClient``. The
    mother orchestrator (or a sub-agent) can spin up a focused analyst to drill
    into a sub-question. Recursion is fenced by HARD guards read from
    :class:`Settings`: ``max_depth`` (default 3) and ``max_budget_usd``
    (default 1.0). Breaching a guard returns a structured *refusal* dict rather
    than raising (so the calling agent can reason about it), and when the Claude
    Agent SDK is not installed a clear stub-error dict is returned instead of
    crashing.
  * :func:`register_alert` — persist an alert configuration (a watchlist plus the
    kinds of events to watch) under the cache directory.
  * :func:`check_alerts` — compute *deltas* since the last run for the registered
    watchlist (new congressional trades + new analyst report events), persist the
    last-seen state under the cache directory so deltas survive restarts, and emit
    any new events to the configured sinks (console / file / webhook).

Design (CONTRACT.md §9.1):
  * Pure async **logic functions** first (``async def`` that call the module-level
    :func:`get_registry`), unit-testable with a monkeypatched ``get_registry``.
  * Thin ``@tool``-wrapped adapters that delegate to the logic functions and wrap
    every result in :func:`text_result`.
  * A module-level ``server = create_sdk_mcp_server(...)`` instance.
  * A guarded ``__main__`` stdio runner.

Import safety: the module imports cleanly without the Claude Agent SDK, without
any optional heavy library, without API keys, and without touching the network.
The only deltas-detection data access goes through the Layer-0 registry, which
reads through its own cache — so a sweep across a large watchlist does not blow
the rate budget.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..providers import get_registry  # re-exported for tests to monkeypatch
from ._common import normalize_symbol, text_result
from ._sdk import (
    SDK_AVAILABLE,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Default model used for a spawned analyst when the caller does not specify one.
DEFAULT_ANALYST_MODEL = "claude-haiku-4-5"

#: Estimated USD cost charged against the budget per spawned analyst. This is a
#: deliberately conservative flat estimate used only to enforce the HARD budget
#: guard without needing real usage accounting from the SDK.
ESTIMATED_COST_PER_SPAWN_USD = 0.25

#: The alert "kinds" this server knows how to compute deltas for.
KNOWN_ALERT_KINDS = ("congress", "analyst_ratings", "upgrades_downgrades")

#: Filenames (relative to the cache dir) used to persist alert config + state.
_ALERTS_CONFIG_FILE = "alerts_config.json"
_ALERTS_STATE_FILE = "alerts_state.json"


# ---------------------------------------------------------------------------
# Small persistence helpers (cache-dir backed JSON)
# ---------------------------------------------------------------------------


def _cache_dir(settings: Settings | None = None) -> Path:
    """Resolve (and create) the cache directory used for alert persistence."""
    settings = settings or Settings.from_env()
    return settings.resolve_cache_dir()


def _read_json(path: Path) -> dict[str, Any]:
    """Best-effort read of a JSON object file; return ``{}`` on any problem."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, obj: dict[str, Any]) -> bool:
    """Best-effort atomic-ish write of a JSON object. Returns success flag.

    Failures degrade silently (return ``False``) so a read-only / broken cache
    dir never crashes a tool.
    """
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(obj, fh, sort_keys=True)
        tmp.replace(path)
        return True
    except OSError:
        return False


def _delta_key(*parts: Any) -> str:
    """Build a stable, hashable string key identifying a single event.

    Used to de-duplicate events across runs; an event already present in the
    last-seen state is not re-emitted.
    """
    raw = json.dumps([str(p) for p in parts], sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 (non-crypto id)


# ---------------------------------------------------------------------------
# Sinks (console / file / webhook)
# ---------------------------------------------------------------------------


def _emit_console(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Print each event to stdout. Returns a small status dict."""
    for ev in events:
        print(f"[ALERT] {json.dumps(ev, sort_keys=True)}")
    return {"sink": "console", "ok": True, "count": len(events)}


def _emit_file(events: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """Append events as JSON lines to ``target``. Never raises."""
    status: dict[str, Any] = {"sink": "file", "target": target, "count": len(events)}
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, sort_keys=True) + "\n")
        status["ok"] = True
    except OSError as exc:
        status["ok"] = False
        status["error"] = str(exc)
    return status


async def _emit_webhook(events: list[dict[str, Any]], url: str) -> dict[str, Any]:
    """POST events to ``url`` via ``httpx`` (lazy-imported). Never raises.

    ``httpx`` is imported inside the function so the module stays importable in a
    minimal environment. If ``httpx`` is unavailable the sink reports a clear,
    non-fatal status.
    """
    status: dict[str, Any] = {"sink": "webhook", "target": url, "count": len(events)}
    if not events:
        status["ok"] = True
        return status
    try:
        import httpx  # lazy import (CONTRACT.md §2.2)
    except ImportError:
        status["ok"] = False
        status["error"] = "httpx not installed"
        return status
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"events": events})
        status["ok"] = 200 <= resp.status_code < 300
        status["status_code"] = resp.status_code
    except Exception as exc:  # network/transport errors are non-fatal
        status["ok"] = False
        status["error"] = f"{type(exc).__name__}: {exc}"
    return status


async def _dispatch_sinks(
    events: list[dict[str, Any]], sinks: dict[str, Any]
) -> list[dict[str, Any]]:
    """Emit ``events`` to each configured sink, collecting per-sink statuses.

    ``sinks`` shape (all optional)::

        {"console": true, "file": "<path>", "webhook": "<url>"}
    """
    results: list[dict[str, Any]] = []
    if sinks.get("console"):
        results.append(_emit_console(events))
    file_target = sinks.get("file")
    if file_target:
        results.append(_emit_file(events, str(file_target)))
    webhook_url = sinks.get("webhook")
    if webhook_url:
        results.append(await _emit_webhook(events, str(webhook_url)))
    return results


# ---------------------------------------------------------------------------
# Pure logic: spawn_analyst
# ---------------------------------------------------------------------------


async def spawn_analyst(
    role: str,
    task: str,
    context: str | None = None,
    model: str | None = None,
    depth: int = 0,
    *,
    settings: Settings | None = None,
    budget_spent_usd: float = 0.0,
) -> dict[str, Any]:
    """Spawn a bounded, nested analyst via a nested ``ClaudeSDKClient``.

    The spawned analyst is given ``role``/``task``/``context`` and runs as a
    short nested Claude session. Recursion is fenced by HARD guards from
    :class:`Settings`:

      * ``depth`` must be ``< settings.max_depth`` (default 3);
      * the projected budget after this spawn must be ``<= settings.max_budget_usd``
        (default 1.0).

    Guard breaches return a structured refusal dict (``{"refused": true, ...}``)
    rather than raising, so the calling agent can reason about the refusal. If the
    Claude Agent SDK is not installed, a clear stub-error dict
    (``{"error": ..., "sdk": false}``) is returned — the call never crashes.

    Args:
        role: The analyst persona / specialization (e.g. ``"technical-analyst"``).
        task: The concrete sub-task for the analyst to perform.
        context: Optional extra context to seed the analyst's prompt.
        model: Optional model id override; defaults to
            :data:`DEFAULT_ANALYST_MODEL`.
        depth: Current recursion depth (0 at the top level).
        settings: Optional explicit settings (defaults to ``Settings.from_env()``).
        budget_spent_usd: USD already spent by ancestor spawns in this chain;
            used to enforce the cumulative budget guard.

    Returns:
        On success: ``{"ok": true, "role": ..., "depth": ..., "model": ...,
        "result": <text>, "budget_spent_usd": <float>, "sdk": true}``.
        On a guard breach: ``{"refused": true, "reason": ..., ...}``.
        On a missing SDK: ``{"error": ..., "sdk": false}``.
    """
    settings = settings or Settings.from_env()
    max_depth = int(settings.max_depth)
    max_budget = float(settings.max_budget_usd)
    chosen_model = model or DEFAULT_ANALYST_MODEL

    # --- HARD guard: recursion depth -------------------------------------
    if depth < 0:
        return {
            "refused": True,
            "reason": "invalid_depth",
            "detail": "depth must be >= 0",
            "depth": depth,
        }
    if depth >= max_depth:
        return {
            "refused": True,
            "reason": "max_depth_exceeded",
            "detail": f"depth {depth} >= max_depth {max_depth}",
            "depth": depth,
            "max_depth": max_depth,
        }

    # --- HARD guard: budget ----------------------------------------------
    projected = float(budget_spent_usd) + ESTIMATED_COST_PER_SPAWN_USD
    if projected > max_budget:
        return {
            "refused": True,
            "reason": "max_budget_exceeded",
            "detail": (
                f"projected spend ${projected:.2f} exceeds "
                f"max_budget_usd ${max_budget:.2f}"
            ),
            "budget_spent_usd": float(budget_spent_usd),
            "projected_usd": projected,
            "max_budget_usd": max_budget,
        }

    # --- SDK availability -------------------------------------------------
    if not SDK_AVAILABLE:
        return {
            "error": (
                "claude_agent_sdk is not installed; spawn_analyst requires the "
                "Claude Agent SDK. Install with: pip install claude-agent-sdk"
            ),
            "sdk": False,
            "role": role,
            "depth": depth,
        }

    # --- Build the nested analyst prompt ---------------------------------
    prompt_parts = [
        f"You are a {role}. Stay strictly within this role.",
        f"Task: {task}",
    ]
    if context:
        prompt_parts.append(f"Context:\n{context}")
    prompt_parts.append(
        "Be concise and return only your findings. This is informational only "
        "and is NOT investment advice."
    )
    prompt = "\n\n".join(prompt_parts)

    options = ClaudeAgentOptions(model=chosen_model)

    # --- Drive the nested client -----------------------------------------
    try:
        collected: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            response = getattr(client, "receive_response", None)
            if callable(response):
                async for message in client.receive_response():
                    text = _extract_text(message)
                    if text:
                        collected.append(text)
            else:
                # Older/alternate SDK surface: query() may return the result.
                direct = await client.query(prompt)
                text = _extract_text(direct)
                if text:
                    collected.append(text)
        result_text = "\n".join(collected).strip()
    except Exception as exc:  # nested run failed — report, never crash caller
        return {
            "error": f"spawned analyst failed: {type(exc).__name__}: {exc}",
            "sdk": True,
            "role": role,
            "depth": depth,
        }

    return {
        "ok": True,
        "role": role,
        "task": task,
        "model": chosen_model,
        "depth": depth,
        "result": result_text,
        "budget_spent_usd": projected,
        "max_depth": max_depth,
        "max_budget_usd": max_budget,
        "sdk": True,
    }


def _extract_text(message: Any) -> str:
    """Best-effort extraction of text from an SDK message/response object.

    Handles a few plausible SDK message shapes without importing SDK types:
      * a plain string;
      * an object with a ``.content`` list of blocks each having ``.text``;
      * an object with a ``.text`` attribute;
      * a dict carrying ``content``/``text``.
    """
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    # dict-shaped
    if isinstance(message, dict):
        if isinstance(message.get("text"), str):
            return message["text"]
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and isinstance(blk.get("text"), str)
            )
        return ""
    # object-shaped
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = []
        for blk in content:
            text = getattr(blk, "text", None)
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else ""


# ---------------------------------------------------------------------------
# Pure logic: register_alert
# ---------------------------------------------------------------------------


async def register_alert(
    watchlist: list[str],
    kinds: list[str],
    sinks: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Persist an alert configuration under the cache directory.

    Args:
        watchlist: Symbols (or member names for congress) to watch. Symbols are
            normalized via :func:`normalize_symbol`.
        kinds: Event kinds to watch; each should be one of
            :data:`KNOWN_ALERT_KINDS`. Unknown kinds are reported under
            ``"ignored_kinds"`` but do not fail the call.
        sinks: Optional sink configuration
            (``{"console": bool, "file": path, "webhook": url}``). Defaults to
            console-only.
        settings: Optional explicit settings.

    Returns:
        ``{"registered": true, "watchlist": [...], "kinds": [...],
        "ignored_kinds": [...], "sinks": {...}, "config_path": <str>,
        "persisted": <bool>}``.
    """
    settings = settings or Settings.from_env()
    norm_watchlist = [normalize_symbol(s) for s in watchlist if str(s).strip()]

    valid_kinds = [k for k in kinds if k in KNOWN_ALERT_KINDS]
    ignored = [k for k in kinds if k not in KNOWN_ALERT_KINDS]
    if not valid_kinds:
        valid_kinds = list(KNOWN_ALERT_KINDS)

    effective_sinks = sinks if isinstance(sinks, dict) and sinks else {"console": True}

    config = {
        "watchlist": norm_watchlist,
        "kinds": valid_kinds,
        "sinks": effective_sinks,
        "updated_at": time.time(),
    }

    path = _cache_dir(settings) / _ALERTS_CONFIG_FILE
    persisted = _write_json(path, config)

    return {
        "registered": True,
        "watchlist": norm_watchlist,
        "kinds": valid_kinds,
        "ignored_kinds": ignored,
        "sinks": effective_sinks,
        "config_path": str(path),
        "persisted": persisted,
    }


# ---------------------------------------------------------------------------
# Pure logic: check_alerts (delta detection)
# ---------------------------------------------------------------------------


async def _fetch_congress_events(symbol: str) -> list[dict[str, Any]]:
    """Fetch congress trades for a symbol via the registry (read-through cache).

    Returns a list of normalized event dicts; an empty list on any failure
    (missing providers, all-providers-failed, etc.) so one bad symbol never
    aborts the whole sweep.
    """
    try:
        envelope = await get_registry().fetch("congress_trades", symbol=symbol)
    except Exception:
        return []
    data = envelope.get("data")
    rows = data if isinstance(data, list) else [data] if data else []
    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _delta_key(
            "congress",
            row.get("symbol", symbol),
            row.get("member"),
            row.get("transaction"),
            row.get("transaction_date"),
            row.get("disclosure_date"),
            row.get("amount_range"),
        )
        events.append(
            {
                "kind": "congress",
                "symbol": row.get("symbol", symbol),
                "key": key,
                "summary": (
                    f"{row.get('member', 'Unknown')} "
                    f"{row.get('transaction', '?')} {row.get('symbol', symbol)} "
                    f"({row.get('amount_range', 'n/a')})"
                ),
                "data": row,
            }
        )
    return events


async def _fetch_report_events(symbol: str, kinds: list[str]) -> list[dict[str, Any]]:
    """Fetch analyst-report deltas (ratings + upgrades/downgrades) for a symbol.

    Reads through the registry cache. Returns normalized event dicts; empty list
    on failure for any individual capability.
    """
    events: list[dict[str, Any]] = []

    if "analyst_ratings" in kinds:
        try:
            envelope = await get_registry().fetch("analyst_ratings", symbol=symbol)
            data = envelope.get("data")
            rows = data if isinstance(data, list) else [data] if data else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = _delta_key(
                    "analyst_ratings",
                    row.get("symbol", symbol),
                    row.get("period"),
                    row.get("strong_buy"),
                    row.get("buy"),
                    row.get("hold"),
                    row.get("sell"),
                    row.get("strong_sell"),
                )
                events.append(
                    {
                        "kind": "analyst_ratings",
                        "symbol": row.get("symbol", symbol),
                        "key": key,
                        "summary": (
                            f"Ratings {row.get('symbol', symbol)} "
                            f"{row.get('period', '?')}: "
                            f"SB{row.get('strong_buy', 0)}/B{row.get('buy', 0)}/"
                            f"H{row.get('hold', 0)}/S{row.get('sell', 0)}/"
                            f"SS{row.get('strong_sell', 0)}"
                        ),
                        "data": row,
                    }
                )
        except Exception:
            pass

    if "upgrades_downgrades" in kinds:
        try:
            envelope = await get_registry().fetch("upgrades_downgrades", symbol=symbol)
            data = envelope.get("data")
            rows = data if isinstance(data, list) else [data] if data else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = _delta_key(
                    "upgrades_downgrades",
                    row.get("symbol", symbol),
                    row.get("firm"),
                    row.get("action"),
                    row.get("from_grade"),
                    row.get("to_grade"),
                    row.get("date"),
                )
                events.append(
                    {
                        "kind": "upgrades_downgrades",
                        "symbol": row.get("symbol", symbol),
                        "key": key,
                        "summary": (
                            f"{row.get('firm', 'Unknown')} {row.get('action', '?')} "
                            f"{row.get('symbol', symbol)}: "
                            f"{row.get('from_grade', '?')} -> {row.get('to_grade', '?')}"
                        ),
                        "data": row,
                    }
                )
        except Exception:
            pass

    return events


async def check_alerts(
    *,
    settings: Settings | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute and emit alert deltas since the last run.

    Loads the persisted alert config (from :func:`register_alert`) unless one is
    supplied, sweeps the watchlist for the configured kinds (congress trades +
    analyst report events) through the Layer-0 cache, diffs against the persisted
    last-seen state, emits any *new* events to the configured sinks, and persists
    the updated state under the cache directory so deltas survive restarts.

    Args:
        settings: Optional explicit settings.
        config: Optional explicit alert config (overrides the persisted one);
            shape matches :func:`register_alert`'s persisted document.

    Returns:
        ``{"checked": true, "new_events": [...], "n_new": <int>,
        "watchlist": [...], "kinds": [...], "sinks_status": [...],
        "state_persisted": <bool>}``. If no alert is registered:
        ``{"checked": false, "reason": "no_alert_registered"}``.
    """
    settings = settings or Settings.from_env()
    cache_dir = _cache_dir(settings)

    if config is None:
        config = _read_json(cache_dir / _ALERTS_CONFIG_FILE)
    if not config or not config.get("watchlist"):
        return {"checked": False, "reason": "no_alert_registered"}

    watchlist = [normalize_symbol(s) for s in config.get("watchlist", []) if str(s).strip()]
    kinds = config.get("kinds") or list(KNOWN_ALERT_KINDS)
    sinks = config.get("sinks") or {"console": True}

    state_path = cache_dir / _ALERTS_STATE_FILE
    state = _read_json(state_path)
    seen: dict[str, Any] = state.get("seen") if isinstance(state.get("seen"), dict) else {}

    report_kinds = [k for k in kinds if k in ("analyst_ratings", "upgrades_downgrades")]
    want_congress = "congress" in kinds

    # --- Sweep the watchlist (read-through cache) ------------------------
    all_events: list[dict[str, Any]] = []
    for symbol in watchlist:
        if want_congress:
            all_events.extend(await _fetch_congress_events(symbol))
        if report_kinds:
            all_events.extend(await _fetch_report_events(symbol, report_kinds))

    # --- Diff against last-seen ------------------------------------------
    new_events: list[dict[str, Any]] = []
    now = time.time()
    for ev in all_events:
        key = ev["key"]
        if key not in seen:
            new_events.append(ev)
        seen[key] = now

    # --- Emit to sinks (only when there is something new) ----------------
    sinks_status: list[dict[str, Any]] = []
    if new_events:
        sinks_status = await _dispatch_sinks(new_events, sinks)

    # --- Persist updated state -------------------------------------------
    state_persisted = _write_json(
        state_path, {"seen": seen, "last_run": now}
    )

    return {
        "checked": True,
        "new_events": new_events,
        "n_new": len(new_events),
        "n_scanned": len(all_events),
        "watchlist": watchlist,
        "kinds": kinds,
        "sinks_status": sinks_status,
        "state_persisted": state_persisted,
    }


# ---------------------------------------------------------------------------
# MCP tool wiring (thin adapters → text_result)
# ---------------------------------------------------------------------------


@tool(
    "spawn_analyst",
    "Spawn a bounded, nested analyst sub-agent for a focused sub-task. "
    "Recursion is HARD-guarded by max_depth and max_budget_usd; breaching a "
    "guard returns a structured refusal. Requires the Claude Agent SDK.",
    {
        "role": str,
        "task": str,
        "context": str,
        "model": str,
        "depth": int,
    },
)
async def spawn_analyst_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`spawn_analyst`."""
    result = await spawn_analyst(
        role=args["role"],
        task=args["task"],
        context=args.get("context"),
        model=args.get("model"),
        depth=int(args.get("depth", 0) or 0),
    )
    return text_result(result)


@tool(
    "register_alert",
    "Register an alert watchlist and the kinds of events to watch "
    "(congress, analyst_ratings, upgrades_downgrades). Persisted under the "
    "cache directory.",
    {
        "watchlist": list,
        "kinds": list,
        "sinks": dict,
    },
)
async def register_alert_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`register_alert`."""
    result = await register_alert(
        watchlist=list(args.get("watchlist", [])),
        kinds=list(args.get("kinds", [])),
        sinks=args.get("sinks"),
    )
    return text_result(result)


@tool(
    "check_alerts",
    "Check the registered watchlist for new congressional trades and analyst "
    "report events since the last run, emit any new events to the configured "
    "sinks, and persist last-seen state under the cache directory.",
    {},
)
async def check_alerts_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP adapter for :func:`check_alerts`."""
    result = await check_alerts()
    return text_result(result)


# Module-level MCP server instance (a real server with the SDK, a descriptor
# namespace under the shim).
server = create_sdk_mcp_server(
    name="orchestration",
    version="0.1.0",
    tools=[spawn_analyst_tool, register_alert_tool, check_alerts_tool],
)


__all__ = [
    "spawn_analyst",
    "register_alert",
    "check_alerts",
    "spawn_analyst_tool",
    "register_alert_tool",
    "check_alerts_tool",
    "server",
    "get_registry",
]


# ---------------------------------------------------------------------------
# Guarded stdio runner
# ---------------------------------------------------------------------------


def _main() -> int:
    """Run the orchestration server over stdio. Returns a process exit code."""
    if not SDK_AVAILABLE:
        print(
            "claude_agent_sdk is not installed; the orchestration MCP server "
            "cannot run over stdio. Install with: pip install claude-agent-sdk",
            flush=True,
        )
        return 1
    try:
        from claude_agent_sdk import run_stdio_server  # type: ignore[import-not-found]
    except ImportError:
        try:
            # Alternate SDK surface: server object exposes its own runner.
            run = getattr(server, "run", None)
            if callable(run):
                run()
                return 0
        except Exception as exc:  # pragma: no cover - depends on SDK internals
            print(f"Failed to run orchestration server: {exc}", flush=True)
            return 1
        print(
            "claude_agent_sdk is installed but no stdio runner was found; "
            "this server object is intended to be mounted by a host.",
            flush=True,
        )
        return 1

    try:  # pragma: no cover - exercised only with the real SDK installed
        run_stdio_server(server)
        return 0
    except Exception as exc:  # pragma: no cover
        print(f"Failed to run orchestration server: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
