"""Certified model router (M4, plan section 13).

Wraps the retry-capable ConverseClient (BedrockClient already does
bounded retry + exponential backoff + jitter per call, from M0) with a
circuit breaker and fallback across a *certified* route set -- fallback
candidates always come from `policies/route_sets.yaml`, never an
arbitrary or unevaluated model. A tenant with no route_set assigned (or
one that resolves to no configured fallbacks) gets exactly one candidate
-- the model resolved by `pipeline.enforce_model_allowlist` -- so it
behaves like a direct call, M0's original behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError, ConverseClient, ConverseResult
from .circuit_breaker import CircuitBreaker


@dataclass(frozen=True)
class RouteSet:
    name: str
    primary: str
    fallbacks: List[str] = field(default_factory=list)


class AllRoutesUnavailableError(Exception):
    """Every candidate model's circuit breaker is open -- no call was
    even attempted. Distinct from BedrockInvocationError, which means a
    call was attempted and failed."""

    def __init__(self, route_set_name: Optional[str]):
        super().__init__(
            f"all candidate models are circuit-open for route_set={route_set_name!r}"
        )


@dataclass(frozen=True)
class RoutedResult:
    result: ConverseResult
    model_id: str
    fallback: bool


def load_route_sets_from_yaml(path: str) -> Dict[str, RouteSet]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    route_sets: Dict[str, RouteSet] = {}
    for name, cfg in (raw.get("route_sets") or {}).items():
        cfg = cfg or {}
        route_sets[name] = RouteSet(
            name=name, primary=cfg["primary"], fallbacks=list(cfg.get("fallbacks", []))
        )
    return route_sets


class CertifiedRouter:
    def __init__(
        self,
        *,
        converse_client: ConverseClient,
        circuit_breaker: CircuitBreaker,
        route_sets: Dict[str, RouteSet],
    ):
        self.converse_client = converse_client
        self._breaker = circuit_breaker
        self._route_sets = route_sets

    def fallbacks_for(self, route_set_name: Optional[str]) -> List[str]:
        if route_set_name is None:
            return []
        route_set = self._route_sets.get(route_set_name)
        return list(route_set.fallbacks) if route_set else []

    def converse(
        self,
        *,
        primary_model_id: str,
        route_set_name: Optional[str],
        messages: List[BedrockChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> RoutedResult:
        """Tries primary_model_id, then the route set's fallbacks in
        order, skipping any model whose breaker is currently open.
        Returns the first success. Raises the last BedrockInvocationError
        if every attempted candidate failed, or AllRoutesUnavailableError
        if every candidate was skipped (all circuit-open)."""
        fallbacks = [m for m in self.fallbacks_for(route_set_name) if m != primary_model_id]
        candidates = [primary_model_id, *fallbacks]

        last_error: Optional[BedrockInvocationError] = None
        for index, model_id in enumerate(candidates):
            if not self._breaker.allow(model_id):
                continue
            try:
                result = self.converse_client.converse(
                    model_id=model_id, messages=messages, max_tokens=max_tokens, temperature=temperature
                )
            except BedrockInvocationError as exc:
                self._breaker.record_failure(model_id)
                last_error = exc
                continue
            self._breaker.record_success(model_id)
            return RoutedResult(result=result, model_id=model_id, fallback=(index > 0))

        if last_error is not None:
            raise last_error
        raise AllRoutesUnavailableError(route_set_name)
