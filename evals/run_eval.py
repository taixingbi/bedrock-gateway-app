"""Golden-dataset evaluation runner (M9, plan section 21).

Runs golden_dataset.yaml's cases against a real Bedrock model, scores
quality (per-case deterministic matcher -- see `_matches()`), safety
(output-guardrail ALLOW rate), and records latency/cost -- then checks
the result against certification_policy.yaml's release gate.

A model that clears every threshold gets written to
policies/certified_models.yaml (routing/certification.py); one that
doesn't is reported but never written -- an uncertified model must
stay uncertified, not get written with a caveat attached.

This is the *entire* mechanism behind CertifiedRouter's enforcement:
nothing else in the gateway can mark a model certified. Re-running
this (a new model, or an existing one after a prompt/config change) is
what plan section 21 calls "New Model / Prompt -> Golden Dataset ->
... -> CERTIFIED". Rollback is just reverting the resulting
policies/certified_models.yaml commit and re-promoting -- git-native,
no separate rollback machinery needed.

Deliberately no LLM-judge -- every case's `match` type (contains,
exact, regex, not_contains, json_valid_with_keys) is checked by plain
Python, no second model call. This is honest about what it can't catch:
open-ended correctness/completeness and genuine hallucination detection
really do want a judge model, which is a real design decision (cost,
judge reliability, judge prompt design) deliberately deferred rather
than bolted on here. `eval_pass_rate_by_category` is reported (printed)
so a regression in one category isn't hidden inside a passing overall
average, but the release gate itself still only checks the overall
`eval_pass_rate` -- splitting the GATE itself by category would change
what "certified" means and is out of scope for this pass.

A few honest caveats about what these numbers do and don't mean:

- `eval_pass_rate` is the golden-suite pass rate, not "model quality"
  in any general sense -- it's exactly as broad (or narrow) as
  golden_dataset.yaml's cases are.
- `safety_score` is the guardrail-output-ALLOW rate, not a measure of
  whether the model actually behaved safely -- a response that leaks a
  planted secret would still count as guardrail-ALLOW if the guardrail
  doesn't happen to catch that specific string. `behavioral_pass_rate`
  (the golden-suite pass rate restricted to the refusal_pii/
  prompt_injection/hallucination_robustness categories, where the
  matcher itself checks the unsafe behavior didn't happen) is reported
  alongside it for that reason, though the release gate still only
  checks `safety_score`, matching plan section 21 as written.
- `p95_latency_ms`/`eval_avg_cost_per_request_usd` are observed over
  THIS run's ~20-case golden suite, not a production-representative
  sample -- at this N, p95 is close to "the slowest 1-2 requests", and
  the golden dataset's short prompts/completions don't reflect
  production token counts. Good enough for a release gate; not a
  production latency/cost SLO. A real per-model SLO would want
  warm-up + repetitions + controlled concurrency + 50-100+ requests,
  deliberately out of scope here (see plan section 21 / M9 scope).

Usage:
    python -m evals.run_eval --model us.amazon.nova-micro-v1:0

Calls real Bedrock (bedrock:InvokeModel, same permission the gateway
itself needs) -- not a fake -- since a certification result should
reflect what the model actually does, not a stand-in.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Runs as `python -m evals.run_eval` from the repo root, where this
# import already works -- this insert is only for `python
# evals/run_eval.py` direct invocation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.gateway.guardrails.basic_guardrail import BasicGuardrailClient  # noqa: E402
from services.gateway.guardrails.models import GuardrailAction  # noqa: E402
from services.gateway.inference.bedrock_client import BedrockChatMessage, BedrockClient  # noqa: E402
from services.gateway.telemetry.cost import estimate_cost  # noqa: E402

DATASET_PATH = Path(__file__).parent / "golden_dataset.yaml"
POLICY_PATH = Path(__file__).parent / "certification_policy.yaml"

# The golden-suite categories a leaked/fabricated value in the output
# actually matters for -- see module docstring's `behavioral_pass_rate`
# note. Reporting-only; doesn't affect the release gate.
BEHAVIORAL_SAFETY_CATEGORIES = {"refusal_pii", "prompt_injection", "hallucination_robustness"}


def load_certification_policy() -> Dict[str, float]:
    with open(POLICY_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_POLICY = load_certification_policy()
EVAL_PASS_RATE_THRESHOLD = _POLICY["eval_pass_rate_min"]
SAFETY_THRESHOLD = _POLICY["safety_min"]
P95_LATENCY_THRESHOLD_MS = _POLICY["p95_latency_ms_max"]
EVAL_AVG_COST_THRESHOLD = _POLICY["eval_avg_cost_per_request_usd_max"]


@dataclass
class EvalResult:
    model_id: str
    eval_pass_rate: float
    safety_score: float
    p95_latency_ms: float
    eval_avg_cost_per_request_usd: float
    # Reporting only -- never written to certified_models.yaml (that
    # schema is shared with bedrock-gateway-policies and read by
    # CertifiedRouter; adding a field there is a real cross-repo
    # change, not something to do as a side effect of richer eval
    # reporting).
    eval_pass_rate_by_category: Dict[str, float] = field(default_factory=dict)
    # None when the golden dataset has no BEHAVIORAL_SAFETY_CATEGORIES
    # cases at all -- distinct from 0.0, which would mean "has such
    # cases and failed every one of them".
    behavioral_pass_rate: Optional[float] = None

    @property
    def certified(self) -> bool:
        return (
            self.eval_pass_rate >= EVAL_PASS_RATE_THRESHOLD
            and self.safety_score >= SAFETY_THRESHOLD
            and self.p95_latency_ms < P95_LATENCY_THRESHOLD_MS
            and self.eval_avg_cost_per_request_usd < EVAL_AVG_COST_THRESHOLD
        )


def load_golden_dataset() -> List[dict]:
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("cases", [])


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _build_messages(case: dict) -> List[BedrockChatMessage]:
    """A case is either a single prompt (single-turn) or an explicit
    `messages` list (multi-turn, testing context retention across
    turns) -- see golden_dataset.yaml's multi_turn category."""
    if "messages" in case:
        return [BedrockChatMessage(role=m["role"], text=m["content"]) for m in case["messages"]]
    return [BedrockChatMessage(role="user", text=case["prompt"])]


def _extract_json_object(text: str) -> Any:
    """Models asked for "JSON only" often still wrap it in a markdown
    code fence or add a sentence of preamble -- take the substring
    between the first `{` and the last `}` rather than requiring the
    whole response to be nothing but JSON. Raises if that substring
    still isn't valid JSON (a genuine failure, not over-tolerance)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in output")
    return json.loads(text[start : end + 1])


def _matches(case: dict, output_text: str) -> bool:
    """Deterministic, no-LLM-judge quality check -- see module
    docstring for why, and golden_dataset.yaml's header for each
    match type's semantics."""
    match_type = case.get("match", "contains")

    if match_type == "contains":
        return case["expect_keyword"].lower() in output_text.lower()

    if match_type == "not_contains":
        return re.search(case["forbidden_pattern"], output_text, re.IGNORECASE) is None

    if match_type == "exact":
        normalized = output_text.strip().strip(".!,;:").strip().lower()
        return normalized == case["expect_exact"].strip().lower()

    if match_type == "regex":
        return re.search(case["expect_regex"], output_text, re.IGNORECASE) is not None

    if match_type == "json_valid_with_keys":
        try:
            parsed = _extract_json_object(output_text)
        except (ValueError, TypeError):
            return False
        if not isinstance(parsed, dict):
            return False
        return all(key in parsed for key in case["expect_json_keys"])

    raise ValueError(f"{case.get('id', '<no id>')}: unknown match type {match_type!r}")


def run_eval(model_id: str, *, region: str = "us-east-1") -> EvalResult:
    client = BedrockClient(region=region)
    guardrail = BasicGuardrailClient()
    cases = load_golden_dataset()
    if not cases:
        raise RuntimeError(f"{DATASET_PATH} has no cases")

    quality_hits = 0
    safety_passes = 0
    latencies: List[float] = []
    costs: List[float] = []
    category_hits: Dict[str, int] = defaultdict(int)
    category_totals: Dict[str, int] = defaultdict(int)
    behavioral_hits = 0
    behavioral_totals = 0

    for case in cases:
        result = client.converse(
            model_id=model_id,
            messages=_build_messages(case),
            max_tokens=128,
            temperature=0.0,
        )
        latencies.append(result.latency_ms)
        costs.append(
            estimate_cost(model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        )

        category = case.get("category", "uncategorized")
        category_totals[category] += 1
        case_passed = _matches(case, result.text)
        if case_passed:
            quality_hits += 1
            category_hits[category] += 1

        if category in BEHAVIORAL_SAFETY_CATEGORIES:
            behavioral_totals += 1
            if case_passed:
                behavioral_hits += 1

        decision = guardrail.check_output(result.text, guardrail_policy="standard-v1")
        if decision.action == GuardrailAction.ALLOW:
            safety_passes += 1

    n = len(cases)
    eval_pass_rate_by_category = {
        category: round(category_hits[category] / total, 4) for category, total in category_totals.items()
    }
    behavioral_pass_rate = round(behavioral_hits / behavioral_totals, 4) if behavioral_totals else None
    return EvalResult(
        model_id=model_id,
        eval_pass_rate=round(quality_hits / n, 4),
        safety_score=round(safety_passes / n, 4),
        p95_latency_ms=round(_percentile(latencies, 95), 2),
        eval_avg_cost_per_request_usd=round(statistics.mean(costs), 8),
        eval_pass_rate_by_category=eval_pass_rate_by_category,
        behavioral_pass_rate=behavioral_pass_rate,
    )


def write_certification(result: EvalResult, *, certified_models_path: str) -> None:
    path = Path(certified_models_path)
    data = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data.setdefault("certified_models", {})
    data["certified_models"][result.model_id] = {
        "eval_pass_rate": result.eval_pass_rate,
        "safety_score": result.safety_score,
        "p95_latency_ms": result.p95_latency_ms,
        "eval_avg_cost_per_request_usd": result.eval_avg_cost_per_request_usd,
        "certified_at": time.strftime("%Y-%m-%d", time.gmtime()),
    }
    # Atomic: write to a sibling temp file and rename over the original,
    # so a process kill/crash mid-write can never leave a truncated or
    # half-written certified_models.yaml -- CertifiedRouter loads this
    # file at startup and a corrupt one fails every route.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.replace(tmp_path, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Bedrock model/inference-profile id to evaluate")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--certified-models-path",
        default="policies/certified_models.yaml",
        help="Where to write the result if the model passes (never written on failure)",
    )
    args = parser.parse_args()

    result = run_eval(args.model, region=args.region)

    print(f"model:              {result.model_id}")
    print(f"eval pass rate:     {result.eval_pass_rate:.4f}  (>= {EVAL_PASS_RATE_THRESHOLD})  -- golden-suite pass rate, not general model quality")
    for category, score in sorted(result.eval_pass_rate_by_category.items()):
        print(f"  - {category:<24} {score:.4f}")
    if result.behavioral_pass_rate is not None:
        print(f"behavioral pass rate: {result.behavioral_pass_rate:.4f}  -- refusal/injection/hallucination categories only, reporting-only (not gated)")
    print(f"safety (guardrail):  {result.safety_score:.4f}  (>= {SAFETY_THRESHOLD})  -- guardrail-output-ALLOW rate, not a behavioral safety guarantee")
    print(f"p95 latency:         {result.p95_latency_ms:.2f}ms  (< {P95_LATENCY_THRESHOLD_MS}ms)  -- this run's ~{len(load_golden_dataset())}-case suite, not a production sample")
    print(f"eval avg cost/req:   ${result.eval_avg_cost_per_request_usd:.8f}  (< ${EVAL_AVG_COST_THRESHOLD})  -- golden-suite token counts, not production-representative")

    if result.certified:
        write_certification(result, certified_models_path=args.certified_models_path)
        print(f"CERTIFIED -- written to {args.certified_models_path}")
    else:
        print("NOT CERTIFIED -- does not meet the release gate; nothing written")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
