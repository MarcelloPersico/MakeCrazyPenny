"""Tests for secret redaction in error messages."""

from __future__ import annotations

from makecrazypenny.core.redact import redact_secrets


def test_redacts_token_query_param() -> None:
    url = "https://finnhub.io/api/v1/quote?symbol=AAPL&token=FAKEKEYTESTONLY123"
    out = redact_secrets(url)
    assert "FAKEKEYTESTONLY123" not in out
    assert "token=***" in out
    assert "symbol=AAPL" in out  # non-secret params preserved


def test_redacts_apikey_variants() -> None:
    assert "SECRET" not in redact_secrets("https://x/y?apikey=SECRET")
    assert "SECRET" not in redact_secrets("https://x/y?api_key=SECRET&z=1")
    assert "SECRET" not in redact_secrets("https://x/y?z=1&apiKey=SECRET")
    # leading '?token=' (first param) is also caught
    assert "SECRET" not in redact_secrets("https://x/y?token=SECRET")


def test_redaction_is_idempotent_and_total() -> None:
    once = redact_secrets("?token=ABC123")
    assert redact_secrets(once) == once
    assert "ABC123" not in once


def test_non_string_and_empty_inputs() -> None:
    assert redact_secrets(None) == ""
    assert redact_secrets(404) == "404"
    assert redact_secrets("no secrets here") == "no secrets here"
