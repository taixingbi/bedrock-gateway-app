"""Guardrail decision model (M3, plan sections 10-11)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GuardrailAction(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class SafetyClass(str, Enum):
    STRICT = "STRICT"
    STANDARD = "STANDARD"
    LOW_RISK = "LOW_RISK"


@dataclass(frozen=True)
class GuardrailDecision:
    action: GuardrailAction
    category: Optional[str] = None  # e.g. "PII", "PROMPT_INJECTION"
    reason: Optional[str] = None
