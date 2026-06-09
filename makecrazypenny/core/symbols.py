"""Crypto symbol detection and canonicalization (CONTRACT.md §16).

Users (and agents) refer to a coin many ways — ``BTC``, ``btc``, ``$BTC``,
``BTC-USD``, ``BTC/USDT``, ``BTCUSD``, ``BTCUSDT``. The crypto providers, by
contrast, want one exchange-native perpetual symbol. This module canonicalizes
any of those forms to a single ``BASEQUOTE`` string (default/ mapped quote
``USDT``, which is what both Binance USDⓈ-M and Bybit linear perps use), and
exposes light helpers for detection and per-exchange formatting.

Pure standard library; importing this module never hits the network.
"""

from __future__ import annotations

#: Quote currencies we recognize when splitting a joined symbol like ``BTCUSDT``.
#: Order matters: longer/more-specific suffixes are tried first so ``USDT`` wins
#: over ``USD`` (``BTCUSDT`` -> base ``BTC``, not ``BTCT``).
_QUOTES: tuple[str, ...] = ("USDT", "USDC", "BUSD", "USDD", "TUSD", "DAI", "USD", "EUR")

#: USD-like quotes that map onto the USDT perpetual (the liquid, leveraged book).
_USD_LIKE: frozenset[str] = frozenset({"USD", "USDT", "USDC", "BUSD", "USDD", "TUSD", "DAI"})

#: Curated set of liquid crypto base assets — used both to *detect* a crypto
#: symbol heuristically and as the offline fallback universe
#: (:mod:`makecrazypenny.core.crypto_universe`). Kept deliberately to majors.
MAJOR_CRYPTO_BASES: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "MATIC", "LTC", "TRX", "BCH", "NEAR", "APT", "ARB", "OP", "SUI", "INJ",
    "ATOM", "FIL", "ETC", "UNI", "AAVE", "TIA", "SEI", "RUNE", "PEPE", "WIF",
    "SHIB", "TON", "ICP", "RNDR", "FTM", "ALGO", "HBAR", "STX", "IMX", "GALA",
)

_MAJOR_SET: frozenset[str] = frozenset(MAJOR_CRYPTO_BASES)


def _clean(symbol: str) -> str:
    """Upper-case, strip whitespace, and drop a leading ``$``."""
    s = str(symbol).strip().upper()
    if s.startswith("$"):
        s = s[1:]
    return s.strip()


def _norm_quote(quote: str) -> str:
    """Map any USD-like quote onto ``USDT``; otherwise pass it through."""
    q = quote.strip().upper()
    return "USDT" if q in _USD_LIKE else (q or "USDT")


def split_crypto(symbol: str) -> tuple[str, str]:
    """Split a symbol into ``(base, quote)`` with the quote mapped to USDT-like.

    Accepts separated forms (``BTC/USDT``, ``BTC-USD``, ``BTC_USDT``) and joined
    forms (``BTCUSDT``, ``BTCUSD``). A bare base (``BTC``) defaults to ``USDT``.
    """
    s = _clean(symbol)
    for sep in ("/", "-", "_", ":"):
        if sep in s:
            base, _, quote = s.partition(sep)
            return base.strip(), _norm_quote(quote)
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], _norm_quote(q)
    return s, "USDT"


def base_asset(symbol: str) -> str:
    """Return just the base asset (e.g. ``BTC`` from ``BTC/USDT``)."""
    return split_crypto(symbol)[0]


def canonical_crypto(symbol: str) -> str:
    """Canonicalize to a single ``BASEQUOTE`` perp symbol (e.g. ``BTCUSDT``)."""
    base, quote = split_crypto(symbol)
    return f"{base}{quote}"


#: Binance USDⓈ-M and Bybit linear perps both use the joined ``BASEQUOTE`` form,
#: so the per-exchange formatters are thin aliases over :func:`canonical_crypto`.
def to_binance_perp(symbol: str) -> str:
    """Format ``symbol`` as a Binance USDⓈ-M perpetual symbol (``BTCUSDT``)."""
    return canonical_crypto(symbol)


def to_bybit_perp(symbol: str) -> str:
    """Format ``symbol`` as a Bybit linear perpetual symbol (``BTCUSDT``)."""
    return canonical_crypto(symbol)


def is_crypto_symbol(symbol: str) -> bool:
    """Best-effort heuristic: does ``symbol`` look like a crypto pair?

    True when the symbol carries an explicit pair separator, ends in a crypto
    quote suffix (``USDT``/``USDC``/``BUSD``/``PERP``), or its base is a known
    major. Deliberately conservative so an equity ticker like ``BRK-B`` is not
    misread as crypto (``B`` is not a crypto quote and ``BRK`` is not a major).
    """
    s = _clean(symbol)
    if not s:
        return False
    if "/" in s:
        return True
    if s.endswith(("USDT", "USDC", "BUSD", "PERP")):
        return True
    base, _ = split_crypto(s)
    return base in _MAJOR_SET


__all__ = [
    "MAJOR_CRYPTO_BASES",
    "split_crypto",
    "base_asset",
    "canonical_crypto",
    "to_binance_perp",
    "to_bybit_perp",
    "is_crypto_symbol",
]
