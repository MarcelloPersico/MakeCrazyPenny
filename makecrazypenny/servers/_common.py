"""Shared helpers for the capability servers (see CONTRACT.md §7.2).

Small, dependency-light utilities used by every Layer-1 server:

  * :func:`text_result` — wrap any JSON-serializable object in the canonical MCP
    text-content envelope (CONTRACT.md §2.4).
  * :func:`json_default` — a ``json.dumps`` ``default`` hook that understands the
    core value dataclasses, ``datetime``/``date`` objects, and ``set``/``frozenset``.
  * :func:`normalize_symbol` — canonicalize a user-supplied ticker symbol.
  * :func:`report_result` — convenience wrapper that attaches the
    not-investment-advice disclaimer to a report-style result before encoding it.

Importing this module pulls in only the standard library plus ``core`` (also
standard-library-only), so it is safe to import without the Claude Agent SDK,
without any optional heavy library, and without touching the network.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from typing import Any

from ..core.disclaimer import DISCLAIMER


def json_default(o: Any) -> Any:
    """``json.dumps`` ``default`` hook for non-natively-serializable objects.

    Handles, in order:

      * objects exposing a ``to_dict()`` method (all core value types) — the
        method's return value is used directly;
      * dataclass *instances* — converted recursively via
        :func:`dataclasses.asdict`;
      * ``datetime`` / ``date`` objects — rendered with ``.isoformat()``;
      * ``set`` / ``frozenset`` — converted to a sorted-when-possible ``list``.

    Args:
        o: The object ``json.dumps`` could not serialize natively.

    Returns:
        A JSON-serializable stand-in for ``o``.

    Raises:
        TypeError: If ``o`` is of a type this hook does not know how to encode
            (so ``json.dumps`` surfaces a clear error).
    """
    to_dict = getattr(o, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        try:
            return sorted(o)
        except TypeError:
            return list(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def text_result(obj: Any) -> dict[str, Any]:
    """Wrap any JSON-serializable object as an MCP text-content result.

    Produces the exact return shape mandated for every MCP tool
    (CONTRACT.md §2.4)::

        {"content": [{"type": "text", "text": "<json-encoded string>"}]}

    The object is encoded with :func:`json_default`, so core value dataclasses,
    ``datetime``/``date`` objects, and sets are handled transparently.

    Args:
        obj: Any object encodable by :func:`json.dumps` with
            :func:`json_default` as the ``default`` hook.

    Returns:
        The MCP text-content envelope.
    """
    return {
        "content": [
            {"type": "text", "text": json.dumps(obj, default=json_default)},
        ]
    }


def normalize_symbol(symbol: str) -> str:
    """Canonicalize a user-supplied ticker symbol.

    Strips surrounding whitespace, removes a single leading ``$`` (common in
    cashtags), and upper-cases the result. For example ``" $aapl "`` becomes
    ``"AAPL"``.

    Args:
        symbol: The raw symbol as supplied by a caller or agent.

    Returns:
        The normalized, upper-cased symbol.
    """
    cleaned = symbol.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return cleaned.strip().upper()


def report_result(obj: Any, *, disclaimer_key: str = "disclaimer") -> dict[str, Any]:
    """Wrap a report-style result, attaching the not-investment-advice disclaimer.

    Convenience for report-producing tools whose output reaches a user
    (CONTRACT.md §2.5). When ``obj`` is a ``dict`` the :data:`DISCLAIMER` is added
    under ``disclaimer_key`` (without clobbering an existing value); otherwise the
    payload is nested under ``"result"`` alongside the disclaimer. The combined
    object is then encoded with :func:`text_result`.

    Args:
        obj: The report payload (typically a ``dict``).
        disclaimer_key: Key under which to store the disclaimer text.

    Returns:
        An MCP text-content envelope whose payload carries the disclaimer.
    """
    if isinstance(obj, dict):
        payload: dict[str, Any] = dict(obj)
        payload.setdefault(disclaimer_key, DISCLAIMER)
    else:
        payload = {"result": obj, disclaimer_key: DISCLAIMER}
    return text_result(payload)


__all__ = ["text_result", "json_default", "normalize_symbol", "report_result"]
