from __future__ import annotations

import re


HIGH_IMPACT = re.compile(
    r"\b(?:confirmed decision|user constraint|privacy|security|authentication|authorization|"
    r"data loss|deletion|retention|encryption|public api|billing|payment|compliance|"
    r"gdpr|ccpa|personal data|personally identifiable|pii|health data|financial data)\b",
    re.IGNORECASE,
)
MEDIUM_IMPACT = re.compile(
    r"\b(?:cost|latency|performance|operability|monitoring|deployment|dependency|scalability|reliability)\b",
    re.IGNORECASE,
)


def classify_materiality(topic: str, options: list[str], conflicts_with_confirmed: bool = False) -> str:
    """Classify routing impact deterministically; high conflicts may need a user."""
    if conflicts_with_confirmed:
        return "high"
    evidence = " ".join((topic, *options))
    if HIGH_IMPACT.search(evidence):
        return "high"
    if MEDIUM_IMPACT.search(evidence):
        return "medium"
    return "low"
