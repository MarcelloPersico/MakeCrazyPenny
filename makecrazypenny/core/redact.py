"""Secret redaction for error messages, logs, and UI surfaces.

Provider adapters authenticate with API keys, and some APIs (FMP, Alpha Vantage)
*require* the key as a URL query parameter. When such a request errors, the
exception message embeds the full URL — including the key. :func:`redact_secrets`
scrubs those values so a key never reaches an error string, a log line, the
dashboard, or an agent transcript.

This is the single chokepoint the provider registry uses when turning a provider
exception into a human-readable skip/failure reason.
"""

from __future__ import annotations

import re

# Matches a secret-bearing query parameter (``?token=...`` / ``&apikey=...``) and
# captures the ``name=`` prefix so the value can be replaced with ``***``. Value
# runs until the next separator (``&``, ``#``, whitespace, or a quote).
_SECRET_QUERY_PARAM = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|apikey|key|secret|access[_-]?token)=)[^&#\s'\"]+"
)

_REDACTED = "***"


def redact_secrets(text: object) -> str:
    """Return ``text`` (stringified) with secret query-parameter values masked.

    Idempotent and safe on any input (coerces to ``str``). Only the *values* of
    known secret parameters are replaced; the surrounding URL/text is preserved
    so the message stays useful for debugging.
    """
    if text is None:
        return ""
    return _SECRET_QUERY_PARAM.sub(rf"\1{_REDACTED}", str(text))


__all__ = ["redact_secrets"]
