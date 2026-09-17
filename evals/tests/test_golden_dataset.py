import re
import unittest

from ..run_eval import load_golden_dataset

_REQUIRED_FIELDS_BY_MATCH = {
    "contains": ["expect_keyword"],
    "exact": ["expect_exact"],
    "regex": ["expect_regex"],
    "not_contains": ["forbidden_pattern"],
    "json_valid_with_keys": ["expect_json_keys"],
}


class GoldenDatasetSanityTests(unittest.TestCase):
    """Not testing model behavior (that needs a real Bedrock call, see
    run_eval()) -- just that the dataset itself is well-formed, so a
    typo in golden_dataset.yaml fails fast in CI instead of surfacing
    as a confusing KeyError mid-certification-run."""

    def setUp(self):
        self.cases = load_golden_dataset()

    def test_dataset_is_not_trivially_small(self):
        """The whole point of this pass -- regression guard against
        accidentally shrinking back to a handful of cases."""
        self.assertGreaterEqual(len(self.cases), 20)

    def test_covers_multiple_categories(self):
        categories = {case.get("category", "uncategorized") for case in self.cases}
        self.assertGreaterEqual(len(categories), 6)

    def test_every_case_has_a_unique_id(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)), "duplicate case ids found")

    def test_every_case_has_required_fields_for_its_match_type(self):
        for case in self.cases:
            match_type = case.get("match", "contains")
            self.assertIn(match_type, _REQUIRED_FIELDS_BY_MATCH, f"{case['id']}: unknown match type {match_type!r}")
            for field_name in _REQUIRED_FIELDS_BY_MATCH[match_type]:
                self.assertIn(field_name, case, f"{case['id']}: missing {field_name!r} for match type {match_type!r}")

    def test_every_case_has_a_prompt_or_messages(self):
        for case in self.cases:
            self.assertTrue(
                "prompt" in case or "messages" in case,
                f"{case['id']}: needs either 'prompt' (single-turn) or 'messages' (multi-turn)",
            )

    def test_regex_and_forbidden_patterns_actually_compile(self):
        for case in self.cases:
            for field_name in ("expect_regex", "forbidden_pattern"):
                if field_name in case:
                    try:
                        re.compile(case[field_name])
                    except re.error as exc:
                        self.fail(f"{case['id']}: invalid regex in {field_name!r}: {exc}")

    def test_multi_turn_messages_have_role_and_content(self):
        for case in self.cases:
            if "messages" not in case:
                continue
            for turn in case["messages"]:
                self.assertIn("role", turn, f"{case['id']}: a turn is missing 'role'")
                self.assertIn("content", turn, f"{case['id']}: a turn is missing 'content'")
                self.assertIn(turn["role"], ("user", "assistant"), f"{case['id']}: invalid role {turn['role']!r}")


if __name__ == "__main__":
    unittest.main()
