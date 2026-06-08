"""MakeCrazyPenny — agentic financial-analysis platform.

Three-layer architecture (see CONTRACT.md):
  Layer 0 ``providers/``     — shared, rate-limited, cached data-access registry.
  Layer 1 ``servers/``       — agent-agnostic capability MCP servers.
  Layer 2 ``orchestration/`` — Claude Agent SDK reasoning / orchestration.

Not investment advice. Output is informational only.
"""

__version__ = "0.1.0"
