"""Not-investment-advice disclaimer (see CONTRACT.md §2.5).

Every output that reaches a user must carry this disclaimer.
"""

from __future__ import annotations

DISCLAIMER: str = (
    "DISCLAIMER: This information is provided by MakeCrazyPenny for informational "
    "and educational purposes only. It is NOT investment advice, a recommendation, "
    "or a solicitation to buy or sell any security. Data may be delayed, incomplete, "
    "or inaccurate. Congressional and insider disclosures are subject to reporting "
    "lag. Always do your own research and consult a licensed financial professional "
    "before making any investment decision."
)


def with_disclaimer(text: str) -> str:
    """Append the not-investment-advice disclaimer to ``text``.

    Args:
        text: The user-facing report or message body.

    Returns:
        ``text`` followed by a blank line and the :data:`DISCLAIMER`.
    """
    body = text.rstrip()
    return f"{body}\n\n{DISCLAIMER}"


__all__ = ["DISCLAIMER", "with_disclaimer"]
