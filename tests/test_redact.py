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


def test_redacts_private_key_with_and_without_prefix() -> None:
    key = "0x" + "a1b2c3d4" * 8  # 64 hex chars
    assert key.lower()[2:] not in redact_secrets(f"failed to sign with {key}").lower()
    bare = "deadbeef" * 8  # 64 hex, no 0x
    assert bare not in redact_secrets(f"key={bare}")


def test_does_not_redact_wallet_address() -> None:
    # A 40-hex wallet address is NOT a secret and must stay readable.
    addr = "0x" + "ab" * 20  # 40 hex chars
    assert addr in redact_secrets(f"account {addr} has equity 1000")


def test_redacts_private_key_assignment() -> None:
    out = redact_secrets("MCP_HL_PRIVATE_KEY=0xSECRETVALUE123")
    assert "0xSECRETVALUE123" not in out
    assert "MCP_HL_PRIVATE_KEY=***" in out
