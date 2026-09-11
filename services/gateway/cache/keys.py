"""Response cache key derivation (M4, plan section 12).

Includes every field the plan calls out: tenant_id, guardrail_version,
policy_epoch. A policy_epoch bump -- any tenant state change or config
edit that goes through `policy/store.py` -- makes every previously-cached
response for that tenant an automatic miss, without scanning and evicting
keys.

`prompt_template_version` / `tool_schema_version` / `retrieval_context_hash`
are reserved (always None) until prompt templates, tool-calling, and
retrieval exist. `model_route_id` is the tenant's route_set name (or
"default" if unassigned); `model_version` is the target model_id resolved
*before* routing/fallback -- the cache key represents "what would this
request produce," addressed by the model actually asked for, not
whichever certified fallback happened to serve it (fallback only occurs
after a cache miss forces a real call -- see routing/router.py).
"""
from __future__ import annotations

import hashlib
import json
from typing import List, Optional

from ..policy.models import TenantPolicy


def build_cache_key(
    *,
    tenant_id: str,
    application_id: str,
    policy: TenantPolicy,
    model_id: str,
    max_tokens: int,
    temperature: float,
    messages: List[dict],
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "application_id": application_id,
        "model_route_id": policy.route_set or "default",
        "model_version": model_id,
        "inference_parameters": {"max_tokens": max_tokens, "temperature": temperature},
        "prompt_template_version": None,
        "normalized_messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "tool_schema_version": None,
        "guardrail_version": policy.guardrail_policy,
        "policy_epoch": policy.policy_epoch,
        "retrieval_context_hash": None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_messages(messages) -> List[dict]:
    """Adapts pydantic ChatMessage objects (api/schemas.py) to the plain
    dicts build_cache_key expects, keeping this module free of an api/
    import (cache shouldn't depend on the HTTP layer)."""
    return [{"role": m.role, "content": m.content} for m in messages]
