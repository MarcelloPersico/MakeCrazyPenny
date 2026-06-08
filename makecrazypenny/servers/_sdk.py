"""Graceful Claude Agent SDK shims (see CONTRACT.md §7.1).

Layer-1 server modules and the Layer-2 orchestration modules wire themselves to
the Claude Agent SDK for MCP tool registration and (for orchestration) nested
agent reasoning. The SDK is an *optional* import: the engineering mandates
(CONTRACT.md §2.2) require that every module import cleanly — without the SDK
installed and without touching the network — so that the pure async logic
functions stay unit-testable in a minimal environment.

This module performs the import once, behind a ``try``/``except ImportError``.
When the SDK is present, the genuine ``tool``, ``create_sdk_mcp_server`` and the
SDK classes are re-exported. When it is absent, lightweight no-op fallbacks take
their place:

* :func:`tool` becomes a decorator that returns the wrapped function **unchanged**
  (the async logic stays directly callable), attaching ``._mcp_tool`` metadata.
* :func:`create_sdk_mcp_server` returns a small descriptor namespace object so
  ``server = create_sdk_mcp_server(...)`` succeeds at module import time.
* :class:`ClaudeSDKClient`, :class:`ClaudeAgentOptions` and
  :class:`AgentDefinition` are minimal stubs that store their kwargs (and, for
  the client, raise a clear error if actually driven).

The boolean :data:`HAS_SDK` reports which path was taken. ``SDK_AVAILABLE`` is
kept as an alias for the name used elsewhere in the contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

try:
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AgentDefinition,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        SdkMcpTool,
        create_sdk_mcp_server,
    )
    from claude_agent_sdk import tool as _real_tool  # type: ignore[import-not-found]

    HAS_SDK = True

    class _CallableTool(SdkMcpTool):  # type: ignore[type-arg,misc]
        """A real :class:`SdkMcpTool` that is *also* directly callable.

        The genuine ``claude_agent_sdk.tool`` decorator returns a plain
        ``SdkMcpTool`` dataclass instance, which is **not** callable — invoking
        ``await my_tool({...})`` raises ``TypeError``. CONTRACT.md §7.1/§13.4
        require the decorated object to keep its underlying async logic directly
        callable so tests can bypass MCP. This subclass preserves every field
        the SDK reads (``name``, ``description``, ``input_schema``, ``handler``,
        ``annotations``) — so ``create_sdk_mcp_server`` still accepts it via
        duck typing — and adds ``__call__`` delegating to ``handler``.
        """

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            """Delegate to the wrapped async ``handler`` (returns its coroutine)."""
            return self.handler(*args, **kwargs)

    def tool(
        name: str,
        description: str,
        input_schema: dict[str, Any] | Any,
    ) -> Callable[[Callable[..., Any]], Any]:
        """Wrapper around ``claude_agent_sdk.tool`` (CONTRACT.md §7.1).

        The real decorator yields a non-callable ``SdkMcpTool``; this wrapper
        re-boxes it as a :class:`_CallableTool` so the decorated object stays
        directly callable/awaitable (its underlying async logic is reachable
        without going through MCP), while remaining a valid ``SdkMcpTool`` for
        ``create_sdk_mcp_server``. The ``_mcp_tool`` metadata and ``__wrapped__``
        reference mirror the no-SDK shim for introspection parity.
        """

        def deco(fn: Callable[..., Any]) -> _CallableTool:
            sdk_tool = _real_tool(name, description, input_schema)(fn)
            callable_tool = _CallableTool(
                name=sdk_tool.name,
                description=sdk_tool.description,
                input_schema=sdk_tool.input_schema,
                handler=sdk_tool.handler,
                annotations=sdk_tool.annotations,
            )
            callable_tool._mcp_tool = {  # type: ignore[attr-defined]
                "name": name,
                "description": description,
                "schema": input_schema,
            }
            callable_tool.__wrapped__ = fn  # type: ignore[attr-defined]
            return callable_tool

        return deco

except ImportError:
    HAS_SDK = False

    def tool(
        name: str,
        description: str,
        input_schema: dict[str, Any] | Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """No-op fallback for ``claude_agent_sdk.tool``.

        Returns a decorator that leaves the wrapped function **unchanged** so the
        underlying (typically ``async def``) logic function remains directly
        importable and callable by tests without going through MCP. The tool
        metadata is recorded on the function's ``_mcp_tool`` attribute for
        introspection.

        Args:
            name: The MCP tool name (e.g. ``"get_ohlcv"``).
            description: Human-readable description of the tool.
            input_schema: The tool's input schema, in the SDK's simple
                ``{"param": type}`` form (stored as-is).

        Returns:
            A decorator returning its argument unchanged with ``_mcp_tool``
            metadata attached.
        """

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            fn._mcp_tool = {  # type: ignore[attr-defined]
                "name": name,
                "description": description,
                "schema": input_schema,
            }
            return fn

        return deco

    def create_sdk_mcp_server(
        name: str,
        version: str = "0.1.0",
        tools: Any = (),
    ) -> SimpleNamespace:
        """No-op fallback for ``claude_agent_sdk.create_sdk_mcp_server``.

        Returns a lightweight descriptor namespace instead of a real MCP server,
        so a module-level ``server = create_sdk_mcp_server(...)`` succeeds when
        the SDK is absent. The returned object exposes ``name``, ``version``,
        ``tools`` (as a list) and a ``_stub`` marker.

        Args:
            name: The server name (e.g. ``"technical"``).
            version: Semantic version string for the server.
            tools: An iterable of decorated tool functions to register.

        Returns:
            A :class:`types.SimpleNamespace` describing the would-be server.
        """
        return SimpleNamespace(
            name=name,
            version=version,
            tools=list(tools),
            _stub=True,
        )

    class ClaudeSDKClient:
        """Minimal stub for ``claude_agent_sdk.ClaudeSDKClient``.

        Stores any constructor kwargs. The SDK is not installed, so attempting
        to actually drive the client (entering its async context or querying)
        raises a clear :class:`RuntimeError` rather than failing obscurely.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Store positional/keyword arguments for later inspection."""
            self.args = args
            self.kwargs = kwargs

        def _unavailable(self) -> "RuntimeError":
            """Build the standard 'SDK not installed' error."""
            return RuntimeError(
                "claude_agent_sdk is not installed; ClaudeSDKClient is a no-op "
                "stub. Install with: pip install claude-agent-sdk"
            )

        async def __aenter__(self) -> "ClaudeSDKClient":
            """Refuse to enter the async context without the real SDK."""
            raise self._unavailable()

        async def __aexit__(self, *exc: Any) -> None:
            """No-op async context exit."""
            return None

        async def query(self, *args: Any, **kwargs: Any) -> Any:
            """Refuse to query without the real SDK."""
            raise self._unavailable()

    class ClaudeAgentOptions:
        """Minimal stub for ``claude_agent_sdk.ClaudeAgentOptions``.

        Accepts and stores arbitrary keyword arguments so callers can build an
        options descriptor (e.g. in ``orchestration/agents.py``) without the SDK
        present. Stored kwargs are exposed both as attributes and via ``kwargs``.
        """

        def __init__(self, **kwargs: Any) -> None:
            """Store options kwargs as attributes and in ``self.kwargs``."""
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class AgentDefinition:
        """Minimal stub for ``claude_agent_sdk.AgentDefinition``.

        Accepts and stores arbitrary keyword arguments (e.g. ``description``,
        ``prompt``, ``tools``, ``model``) so agent definitions can be declared
        without the SDK installed. Stored kwargs are exposed both as attributes
        and via ``kwargs``.
        """

        def __init__(self, **kwargs: Any) -> None:
            """Store definition kwargs as attributes and in ``self.kwargs``."""
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)


# Alias for the name used elsewhere in the contract/spec.
SDK_AVAILABLE: bool = HAS_SDK

__all__ = [
    "HAS_SDK",
    "SDK_AVAILABLE",
    "tool",
    "create_sdk_mcp_server",
    "ClaudeSDKClient",
    "ClaudeAgentOptions",
    "AgentDefinition",
]
