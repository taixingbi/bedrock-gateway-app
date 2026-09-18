"""AWS Bedrock ApplyGuardrail-backed GuardrailClient -- the real swap
target `basic_guardrail.py`'s own docstring names ("a real deployment
replaces this with Bedrock Guardrails ... behind the same
`GuardrailClient` Protocol").

Scoping note: `guardrail_policy` is NOT yet mapped to distinct AWS
Guardrail resources -- every guardrail_policy value actually in use
today (`finance-v1`, `standard-v1`) resolves to
`fail_closed.classify_safety() == STANDARD` anyway, so one shared
`guardrail_id`/`guardrail_version` covers current usage. Per-policy AWS
guardrails (a stricter one for a `-strict` tenant, say) is a real next
step, deliberately deferred until a tenant actually needs a genuinely
different guardrail rather than built speculatively here.

boto3 imported lazily (only when actually constructed) -- same
reasoning as every other real AWS-backed client in this codebase
(BedrockClient, DynamoDbJobStore, S3AuditStore, ...).
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from .client import GuardrailCheckError
from .models import GuardrailAction, GuardrailDecision


class BedrockGuardrailClient:
    def __init__(
        self,
        *,
        guardrail_id: str,
        guardrail_version: str,
        region: str = "us-east-1",
        client: Optional[Any] = None,
    ):
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=region)
        self._client = client

    def check_input(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        return self._apply(text, source="INPUT")

    def check_output(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        return self._apply(text, source="OUTPUT")

    def _apply(self, text: str, *, source: str) -> GuardrailDecision:
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self._guardrail_id,
                guardrailVersion=self._guardrail_version,
                source=source,
                content=[{"text": {"text": text}}],
            )
        except Exception as exc:
            # Any failure to complete the check (timeout, throttling,
            # 5xx, network) -- distinct from a completed BLOCK decision,
            # see client.py's GuardrailCheckError docstring. Caught
            # broadly (not just botocore.ClientError) since a network-
            # level failure from boto3 isn't always a ClientError.
            raise GuardrailCheckError(f"Bedrock ApplyGuardrail failed: {exc}") from exc

        if response.get("action") == "GUARDRAIL_INTERVENED":
            category, reason = _summarize_assessments(response.get("assessments", []))
            return GuardrailDecision(action=GuardrailAction.BLOCK, category=category, reason=reason)

        return GuardrailDecision(action=GuardrailAction.ALLOW)


def _summarize_assessments(assessments: List[dict]) -> Tuple[str, str]:
    """ApplyGuardrail can report multiple simultaneous policy hits across
    several policy types; GuardrailDecision only carries one
    category/reason (the same shape BasicGuardrailClient already
    returns), so this takes the first substantive hit found rather than
    representing all of them -- checked in the same PII-first order
    BasicGuardrailClient's own _check() uses."""
    for assessment in assessments:
        pii = assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", [])
        if pii:
            return "PII", f"detected: {pii[0].get('type', 'unknown')}"

        regexes = assessment.get("sensitiveInformationPolicy", {}).get("regexes", [])
        if regexes:
            return "PII", f"matched pattern: {regexes[0].get('name', 'unknown')}"

        content_filters = assessment.get("contentPolicy", {}).get("filters", [])
        if content_filters:
            return "CONTENT_FILTER", f"matched filter: {content_filters[0].get('type', 'unknown')}"

        topics = assessment.get("topicPolicy", {}).get("topics", [])
        if topics:
            return "DENIED_TOPIC", f"matched topic: {topics[0].get('name', 'unknown')}"

        words = assessment.get("wordPolicy", {})
        if words.get("customWords") or words.get("managedWordLists"):
            return "DENIED_WORD", "matched denylisted word"

    return "GUARDRAIL", "Bedrock guardrail intervened"
