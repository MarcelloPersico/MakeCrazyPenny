"""Layer 1: agent-agnostic capability MCP servers.

Each server (technical, sentiment, congress, reports, synthesis, orchestration)
exposes pure async logic functions plus MCP tool wiring. Servers depend ONLY on
the provider registry + core; no server imports another server, EXCEPT
``synthesis``, which may import the read-only logic functions of ``technical``
and ``reports`` for cross-domain composition.

The Claude Agent SDK is optional at import time: ``servers/_sdk.py`` provides
graceful shims so every module imports without the SDK installed.
"""
