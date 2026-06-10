"""Secret redaction for error messages, logs, and UI surfaces.

Provider adapters authenticate with API keys, and some APIs (FMP, Alpha Vantage)
*require* the key as a URL query parameter. When such a request errors, the
exception message embeds the full URL — including the key. :func:`redact_secrets`
scrubs those values so a key never reaches an error string, a log line, the
dashboard, or an agent transcript.

This is the single chokepoint the provider registry uses when turning a provider
exception into a human-readable skip/failure reason. The execution layer
(CONTRACT.md §17) routes its error strings through the same chokepoint so a
Hyperliquid wallet **private key** (64 hex chars, with or without ``0x``) can
never leak into a tool result, log line, or transcript.
"""

from __future__ import annotations

import re

# Matches a secret-bearing query parameter (``?token=...`` / ``&apikey=...``) and
# captures the ``name=`` prefix so the value can be replaced with ``***``. Value
# runs until the next separator (``&``, ``#``, whitespace, or a quote).
_SECRET_QUERY_PARAM = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|apikey|key|secret|access[_-]?token)=)[^&#\s'\"]+"
)

# Matches an ``NAME=value`` assignment of a known secret env var (e.g. a leaked
# ``MCP_HL_PRIVATE_KEY=0x...`` in a config dump), capturing the ``NAME=`` prefix.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:MCP_HL_PRIVATE_KEY|private[_-]?key|secret[_-]?key)\s*[=:]\s*)[^\s,;'\"]+"
)

# Matches an Ethereum private key (64 hex chars, with or without the ``0x``
# prefix) as a standalone token. A 40-hex *address* is deliberately NOT matched —
# wallet addresses are not secret and stay readable in output. The ``(?![0-9a-fA-F])``
# guard avoids clipping the first 64 chars of a longer hex blob.
_PRIVATE_KEY = re.compile(r"(?i)\b(0x)?[0-9a-f]{64}(?![0-9a-fA-F])")

_REDACTED = "***"


def redact_secrets(text: object) -> str:
    """Return ``text`` (stringified) with secret query-parameter values masked.

    Idempotent and safe on any input (coerces to ``str``). Only the *values* of
    known secret parameters are replaced; the surrounding URL/text is preserved
    so the message stays useful for debugging.
    """
    if text is None:
        return ""
    out = _SECRET_QUERY_PARAM.sub(rf"\1{_REDACTED}", str(text))
    out = _SECRET_ASSIGNMENT.sub(rf"\1{_REDACTED}", out)
    out = _PRIVATE_KEY.sub(_REDACTED, out)
    return out


__all__ = ["redact_secrets"]
