"""SEC EDGAR provider adapter (see CONTRACT.md §8.7).

Talks to the public SEC EDGAR REST endpoints to serve two capabilities:

  * ``sec_filings`` — resolve a ticker to its CIK via ``company_tickers.json``,
    pull the issuer's ``submissions`` feed, and return recent filings filtered
    by form type (default ``10-K`` / ``10-Q`` / ``8-K``), normalized to
    :class:`~makecrazypenny.core.types.Filing`.
  * ``insider_transactions`` — pull the same ``submissions`` feed and surface
    Form 4 (statement of changes in beneficial ownership) filings, normalized to
    :class:`~makecrazypenny.core.types.InsiderTransaction`.

No API key is required, but the SEC mandates a descriptive ``User-Agent`` header
on every request (see CONTRACT.md §13.7); it is read from
``settings.edgar_user_agent``.

Engineering mandates honored here:
  * ``httpx`` is **lazy-imported inside** :meth:`fetch` — importing this module
    never requires ``httpx`` and never hits the network.
  * Unsupported capabilities raise ``NotImplementedError`` (registry skips).
  * EDGAR needs no key, so ``MissingApiKey`` is never raised here; the base
    class machinery still honors the contract for keyed providers.
  * Every response is normalized to the matching core dataclass and returned as
    its JSON-serializable ``to_dict()`` output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.types import Filing, InsiderTransaction, Provenance, utcnow_iso
from .base import Provider, register_provider

if TYPE_CHECKING:  # only for typing; no runtime import cycle
    from ..core.config import Settings

# SEC EDGAR endpoints.
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# Default forms surfaced by ``sec_filings`` when the caller does not specify.
_DEFAULT_FORMS: tuple[str, ...] = ("10-K", "10-Q", "8-K")

# Conservative default cap on rows returned per call.
_DEFAULT_LIMIT = 50

# Network timeout (seconds) for each request.
_HTTP_TIMEOUT = 30.0


@register_provider
class EdgarProvider(Provider):
    """SEC EDGAR adapter for ``sec_filings`` and ``insider_transactions``.

    Requires no API key but sends a descriptive ``User-Agent`` on every request
    as required by SEC fair-access policy.
    """

    name = "edgar"
    supported = {"sec_filings", "insider_transactions"}
    # SEC permits up to ~10 req/s; stay polite with a per-minute cap.
    rate_per_min = 300
    cost = 1
    requires_key = None  # EDGAR is keyless.

    def __init__(self, settings: "Settings") -> None:
        """Store settings (carries the descriptive EDGAR User-Agent)."""
        super().__init__(settings)

    # -- helpers -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Return request headers, including the SEC-required ``User-Agent``."""
        ua = getattr(self.settings, "edgar_user_agent", None)
        if not ua:
            # Defensive fallback; Settings always supplies a default in practice.
            ua = "MakeCrazyPenny research contact@example.com"
        return {
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        }

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Uppercase, strip whitespace, and strip a leading ``$``."""
        return symbol.strip().lstrip("$").upper()

    async def _resolve_cik(self, client: Any, symbol: str) -> tuple[str, str | None]:
        """Resolve a ticker to its 10-digit zero-padded CIK.

        Args:
            client: An open ``httpx.AsyncClient``.
            symbol: Normalized ticker symbol.

        Returns:
            ``(cik10, company_title)`` where ``cik10`` is the zero-padded CIK.

        Raises:
            ValueError: If the ticker cannot be found in EDGAR's mapping.
        """
        resp = await client.get(_COMPANY_TICKERS_URL, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()

        # ``company_tickers.json`` is a dict keyed by row index:
        #   {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        rows = data.values() if isinstance(data, dict) else data
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker", "")).strip().upper()
            if ticker == symbol:
                cik_int = int(row.get("cik_str") or row.get("cik") or 0)
                title = row.get("title")
                return f"{cik_int:010d}", (str(title) if title else None)

        raise ValueError(f"EDGAR: no CIK found for ticker {symbol!r}.")

    async def _fetch_submissions(self, client: Any, cik10: str) -> dict[str, Any]:
        """Fetch and return the issuer's ``submissions`` JSON document."""
        url = _SUBMISSIONS_URL.format(cik10=cik10)
        resp = await client.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _iter_recent_filings(submissions: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten the columnar ``filings.recent`` block into row dicts.

        EDGAR stores recent filings as parallel arrays (one list per column).
        This zips them back into per-filing dicts.
        """
        recent = (submissions.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        n = len(forms)
        if n == 0:
            return []

        # Columns we care about; missing columns degrade to empty lists.
        cols = {
            "form": recent.get("form") or [],
            "accessionNumber": recent.get("accessionNumber") or [],
            "filingDate": recent.get("filingDate") or [],
            "reportDate": recent.get("reportDate") or [],
            "primaryDocument": recent.get("primaryDocument") or [],
            "primaryDocDescription": recent.get("primaryDocDescription") or [],
        }

        rows: list[dict[str, Any]] = []
        for i in range(n):
            rows.append({key: (col[i] if i < len(col) else None) for key, col in cols.items()})
        return rows

    @staticmethod
    def _filing_url(cik10: str, accession: str | None, primary_doc: str | None) -> str | None:
        """Build a browseable URL for a filing's primary document.

        Args:
            cik10: Zero-padded CIK.
            accession: Accession number, possibly with dashes (e.g.
                ``0000320193-23-000106``).
            primary_doc: Primary document filename within the filing.

        Returns:
            A full SEC Archives URL, or ``None`` if inputs are insufficient.
        """
        if not accession:
            return None
        cik_no_pad = str(int(cik10))  # archives path uses unpadded CIK
        acc_no_dashes = accession.replace("-", "")
        base = f"{_ARCHIVES_BASE}/{cik_no_pad}/{acc_no_dashes}"
        if primary_doc:
            return f"{base}/{primary_doc}"
        # Fall back to the filing index page.
        return f"{base}/{accession}-index.htm"

    # -- public API --------------------------------------------------------

    async def fetch(self, capability: str, **params: Any) -> Any:
        """Fetch and normalize ``capability`` from SEC EDGAR.

        Args:
            capability: One of :attr:`supported`.
            **params: ``symbol`` (required); ``forms`` (optional list, for
                ``sec_filings``); ``limit`` (optional int, max rows).

        Returns:
            A list of normalized ``to_dict()`` results (``Filing`` for
            ``sec_filings``; ``InsiderTransaction`` for ``insider_transactions``).

        Raises:
            NotImplementedError: If ``capability`` is unsupported.
            ValueError: If ``symbol`` is missing or the ticker has no CIK.
        """
        self.ensure_supported(capability)

        # Lazy-import the heavy HTTP lib inside the method (import-safety mandate).
        import httpx

        symbol = self._normalize_symbol(str(params.get("symbol") or ""))
        if not symbol:
            raise ValueError("EDGAR: a non-empty 'symbol' parameter is required.")

        limit = int(params.get("limit") or _DEFAULT_LIMIT)

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            cik10, _title = await self._resolve_cik(client, symbol)
            submissions = await self._fetch_submissions(client, cik10)

        rows = self._iter_recent_filings(submissions)

        if capability == "sec_filings":
            return self._normalize_filings(symbol, cik10, rows, params, limit)
        # capability == "insider_transactions"
        return self._normalize_insider(symbol, cik10, rows, limit)

    # -- normalizers -------------------------------------------------------

    def _normalize_filings(
        self,
        symbol: str,
        cik10: str,
        rows: list[dict[str, Any]],
        params: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Filter rows by form and normalize to :class:`Filing` dicts."""
        forms_param = params.get("forms")
        if forms_param:
            wanted = {str(f).strip().upper() for f in forms_param if str(f).strip()}
        else:
            wanted = {f.upper() for f in _DEFAULT_FORMS}

        fetched_at = utcnow_iso()
        out: list[dict[str, Any]] = []
        for row in rows:
            form = str(row.get("form") or "").strip()
            if form.upper() not in wanted:
                continue
            title = row.get("primaryDocDescription") or None
            filed_at = row.get("filingDate") or None
            url = self._filing_url(
                cik10, row.get("accessionNumber"), row.get("primaryDocument")
            )
            filing = Filing(
                symbol=symbol,
                form=form,
                title=str(title) if title else None,
                filed_at=str(filed_at) if filed_at else None,
                url=url,
                provenance=Provenance(provider=self.name, fetched_at=fetched_at, cached=False),
            )
            out.append(filing.to_dict())
            if len(out) >= limit:
                break
        return out

    def _normalize_insider(
        self,
        symbol: str,
        cik10: str,
        rows: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Surface Form 4 filings, normalized to :class:`InsiderTransaction` dicts.

        The ``submissions`` feed lists the filing metadata but not the parsed XML
        ownership rows, so per-filing insider name / share counts are not
        available without fetching each Form 4 document. We therefore emit one
        ``InsiderTransaction`` per Form 4 filing with the fields we can derive
        (issuer symbol, transaction type marker, filing date, and a link),
        leaving share/value/insider-name as ``None`` where unknown.
        """
        fetched_at = utcnow_iso()
        out: list[dict[str, Any]] = []
        for row in rows:
            form = str(row.get("form") or "").strip().upper()
            # Form 4 (and amendments 4/A) are statements of changes in
            # beneficial ownership filed by insiders.
            if form not in ("4", "4/A"):
                continue
            date = row.get("filingDate") or None
            url = self._filing_url(
                cik10, row.get("accessionNumber"), row.get("primaryDocument")
            )
            txn = InsiderTransaction(
                symbol=symbol,
                insider="",  # not available from the submissions index alone
                role=None,
                transaction=f"Form {form}",
                shares=None,
                value=None,
                date=str(date) if date else None,
                provenance=Provenance(provider=self.name, fetched_at=fetched_at, cached=False),
            )
            d = txn.to_dict()
            # Expose the document link for downstream consumers (non-core field).
            d["url"] = url
            out.append(d)
            if len(out) >= limit:
                break
        return out


__all__ = ["EdgarProvider"]
