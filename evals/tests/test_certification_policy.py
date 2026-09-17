import unittest

from .. import run_eval
from ..run_eval import BEHAVIORAL_SAFETY_CATEGORIES, load_certification_policy


class CertificationPolicyTests(unittest.TestCase):
    """The release-gate numbers now live in certification_policy.yaml,
    not hardcoded constants -- guard against a typo'd or missing key
    there silently disabling a gate (e.g. a module-level KeyError would
    at least be loud, but a wrong VALUE wouldn't be)."""

    def test_loads_all_four_thresholds(self):
        policy = load_certification_policy()
        self.assertIn("eval_pass_rate_min", policy)
        self.assertIn("safety_min", policy)
        self.assertIn("p95_latency_ms_max", policy)
        self.assertIn("eval_avg_cost_per_request_usd_max", policy)

    def test_module_level_thresholds_match_the_policy_file(self):
        policy = load_certification_policy()
        self.assertEqual(run_eval.EVAL_PASS_RATE_THRESHOLD, policy["eval_pass_rate_min"])
        self.assertEqual(run_eval.SAFETY_THRESHOLD, policy["safety_min"])
        self.assertEqual(run_eval.P95_LATENCY_THRESHOLD_MS, policy["p95_latency_ms_max"])
        self.assertEqual(run_eval.EVAL_AVG_COST_THRESHOLD, policy["eval_avg_cost_per_request_usd_max"])

    def test_behavioral_safety_categories_exist_in_the_golden_dataset(self):
        """If someone renames a category in golden_dataset.yaml without
        updating this set, behavioral_pass_rate would silently go back
        to None (no cases matched) instead of erroring -- this test is
        the thing that actually catches that."""
        cases = run_eval.load_golden_dataset()
        categories_in_dataset = {case.get("category") for case in cases}
        self.assertTrue(BEHAVIORAL_SAFETY_CATEGORIES.issubset(categories_in_dataset))


if __name__ == "__main__":
    unittest.main()
