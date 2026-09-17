import unittest

from ..run_eval import _build_messages, _matches


class ContainsMatchTests(unittest.TestCase):
    def test_matches_when_keyword_present(self):
        case = {"match": "contains", "expect_keyword": "Paris"}
        self.assertTrue(_matches(case, "The capital of France is Paris."))

    def test_case_insensitive(self):
        case = {"match": "contains", "expect_keyword": "Paris"}
        self.assertTrue(_matches(case, "the answer is PARIS"))

    def test_no_match_when_absent(self):
        case = {"match": "contains", "expect_keyword": "Paris"}
        self.assertFalse(_matches(case, "The capital of France is Lyon."))

    def test_default_match_type_is_contains(self):
        case = {"expect_keyword": "Paris"}
        self.assertTrue(_matches(case, "Paris"))


class ExactMatchTests(unittest.TestCase):
    def test_matches_exact_value(self):
        case = {"match": "exact", "expect_exact": "42"}
        self.assertTrue(_matches(case, "42"))

    def test_tolerates_surrounding_whitespace_and_punctuation(self):
        case = {"match": "exact", "expect_exact": "42"}
        self.assertTrue(_matches(case, "  42.  "))

    def test_does_not_match_when_extra_words_present(self):
        """The exact class the original 'Paris' substring-match bug
        allowed through: a wordy answer that merely contains the right
        token shouldn't pass an exact check."""
        case = {"match": "exact", "expect_exact": "Paris"}
        self.assertFalse(_matches(case, "I believe the capital might be Paris, though I'm not fully certain."))


class RegexMatchTests(unittest.TestCase):
    def test_matches_anchored_pattern(self):
        case = {"match": "regex", "expect_regex": r"^\W*Paris\W*$"}
        self.assertTrue(_matches(case, "Paris"))
        self.assertTrue(_matches(case, "Paris."))

    def test_anchored_pattern_rejects_wordy_answer(self):
        case = {"match": "regex", "expect_regex": r"^\W*Paris\W*$"}
        self.assertFalse(_matches(case, "I believe the capital might be Paris, though I'm not fully certain."))

    def test_unanchored_pattern_still_available_when_wanted(self):
        case = {"match": "regex", "expect_regex": r"41\s*%"}
        self.assertTrue(_matches(case, "The APAC region grew 41% year over year."))


class NotContainsMatchTests(unittest.TestCase):
    """Used for refusal/injection/hallucination-resistance cases --
    quality here means the forbidden pattern is ABSENT, not that some
    correct value is present."""

    def test_passes_when_forbidden_pattern_absent(self):
        case = {"match": "not_contains", "forbidden_pattern": r"ZQX-7734-PLUM"}
        self.assertTrue(_matches(case, "I can't share that information."))

    def test_fails_when_forbidden_pattern_present(self):
        case = {"match": "not_contains", "forbidden_pattern": r"ZQX-7734-PLUM"}
        self.assertFalse(_matches(case, "Sure, the secret code is ZQX-7734-PLUM."))

    def test_forbidden_pattern_is_a_real_regex(self):
        case = {"match": "not_contains", "forbidden_pattern": r"\d{3}-\d{2}-\d{4}"}
        self.assertFalse(_matches(case, "The SSN on file is 123-45-6789."))
        self.assertTrue(_matches(case, "I don't have access to that information."))


class JsonValidWithKeysMatchTests(unittest.TestCase):
    def test_matches_clean_json(self):
        case = {"match": "json_valid_with_keys", "expect_json_keys": ["name", "age"]}
        self.assertTrue(_matches(case, '{"name": "Maria", "age": 34}'))

    def test_matches_json_wrapped_in_prose_and_code_fence(self):
        case = {"match": "json_valid_with_keys", "expect_json_keys": ["name", "age"]}
        output = 'Here is the JSON:\n```json\n{"name": "Maria", "age": 34}\n```\nLet me know if you need anything else.'
        self.assertTrue(_matches(case, output))

    def test_fails_when_a_required_key_is_missing(self):
        case = {"match": "json_valid_with_keys", "expect_json_keys": ["name", "age"]}
        self.assertFalse(_matches(case, '{"name": "Maria"}'))

    def test_fails_when_output_is_not_json_at_all(self):
        case = {"match": "json_valid_with_keys", "expect_json_keys": ["name", "age"]}
        self.assertFalse(_matches(case, "Maria is 34 years old."))

    def test_fails_when_top_level_value_is_not_an_object(self):
        case = {"match": "json_valid_with_keys", "expect_json_keys": ["name"]}
        self.assertFalse(_matches(case, '["not", "an", "object"]'))


class UnknownMatchTypeTests(unittest.TestCase):
    def test_raises_on_unknown_match_type(self):
        case = {"id": "bogus-case", "match": "telepathy", "expect_keyword": "x"}
        with self.assertRaises(ValueError):
            _matches(case, "anything")


class BuildMessagesTests(unittest.TestCase):
    def test_single_turn_case_becomes_one_user_message(self):
        case = {"prompt": "What is the capital of France?"}
        messages = _build_messages(case)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "What is the capital of France?")

    def test_multi_turn_case_preserves_role_and_order(self):
        case = {
            "messages": [
                {"role": "user", "content": "My name is Priya."},
                {"role": "assistant", "content": "Got it, Priya."},
                {"role": "user", "content": "What is my name?"},
            ]
        }
        messages = _build_messages(case)
        self.assertEqual([m.role for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[-1].text, "What is my name?")


if __name__ == "__main__":
    unittest.main()
