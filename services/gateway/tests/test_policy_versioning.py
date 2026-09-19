"""Plan section 33: policy versioning/approval/rollback -- store-level
unit tests (InMemoryPolicyStore.apply_change/rollback/list_history,
policy/validation.py). API-level tests (propose/approve/reject/rollback
endpoints, ABAC, role gating) live in test_policy_change_requests.py.
"""
import unittest

from ..policy.models import (
    NoPriorPolicyVersionError,
    PolicyEpochConflictError,
    TenantPolicy,
    TenantSlo,
    TenantState,
    UnknownTenantError,
)
from ..policy.store import InMemoryPolicyStore
from ..policy.validation import PolicyValidationError, validate_policy_changes


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class ApplyChangeTests(unittest.TestCase):
    def test_applies_changes_and_bumps_epoch(self):
        store = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})

        updated = store.apply_change("acme", {"rpm_limit": 500}, expected_epoch=1)

        self.assertEqual(updated.rpm_limit, 500)
        self.assertEqual(updated.policy_epoch, 2)
        self.assertEqual(store.get("acme").rpm_limit, 500)

    def test_unrelated_fields_are_untouched(self):
        store = InMemoryPolicyStore(
            {"acme": _policy(rpm_limit=60, monthly_budget=1000.0, policy_epoch=1)}
        )

        updated = store.apply_change("acme", {"rpm_limit": 500}, expected_epoch=1)

        self.assertEqual(updated.monthly_budget, 1000.0)

    def test_slo_partial_update_merges_onto_existing_slo(self):
        store = InMemoryPolicyStore(
            {"acme": _policy(slo=TenantSlo(p95_latency_ms=2000.0), policy_epoch=1)}
        )

        updated = store.apply_change("acme", {"slo": {"p95_latency_ms": 500.0}}, expected_epoch=1)

        self.assertEqual(updated.slo.p95_latency_ms, 500.0)

    def test_stale_epoch_raises_conflict(self):
        store = InMemoryPolicyStore({"acme": _policy(policy_epoch=3)})

        with self.assertRaises(PolicyEpochConflictError) as ctx:
            store.apply_change("acme", {"rpm_limit": 500}, expected_epoch=1)

        self.assertEqual(ctx.exception.expected_epoch, 1)
        self.assertEqual(ctx.exception.actual_epoch, 3)

    def test_unknown_tenant_raises(self):
        store = InMemoryPolicyStore({})

        with self.assertRaises(UnknownTenantError):
            store.apply_change("ghost", {"rpm_limit": 500}, expected_epoch=1)


class RollbackTests(unittest.TestCase):
    def test_restores_prior_version_as_new_forward_epoch(self):
        store = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        store.apply_change("acme", {"rpm_limit": 500}, expected_epoch=1)  # epoch 2

        rolled_back = store.rollback("acme", target_epoch=1)

        self.assertEqual(rolled_back.rpm_limit, 60)
        self.assertEqual(rolled_back.policy_epoch, 3)  # forward, not back to 1
        self.assertEqual(store.get("acme").rpm_limit, 60)

    def test_unknown_epoch_raises(self):
        store = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})

        with self.assertRaises(NoPriorPolicyVersionError):
            store.rollback("acme", target_epoch=99)

    def test_unknown_tenant_raises(self):
        store = InMemoryPolicyStore({})

        with self.assertRaises(UnknownTenantError):
            store.rollback("ghost", target_epoch=1)


class ListHistoryTests(unittest.TestCase):
    def test_newest_first_excludes_current(self):
        store = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        store.apply_change("acme", {"rpm_limit": 100}, expected_epoch=1)  # epoch 2
        store.apply_change("acme", {"rpm_limit": 200}, expected_epoch=2)  # epoch 3

        history = store.list_history("acme")

        self.assertEqual([p.policy_epoch for p in history], [2, 1])
        self.assertEqual([p.rpm_limit for p in history], [100, 60])
        # current (epoch 3, rpm_limit=200) is not in history
        self.assertNotIn(3, [p.policy_epoch for p in history])

    def test_set_state_also_archives(self):
        """apply_change/rollback aren't the only writes that should
        historize -- set_state (the kill switch) shares the same
        _replace() path so a state flip shows up in history too."""
        store = InMemoryPolicyStore({"acme": _policy(state=TenantState.ACTIVE, policy_epoch=1)})

        store.set_state("acme", TenantState.SUSPENDED)

        history = store.list_history("acme")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].state, TenantState.ACTIVE)

    def test_empty_history_for_never_changed_tenant(self):
        store = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})

        self.assertEqual(store.list_history("acme"), [])

    def test_unknown_tenant_raises(self):
        store = InMemoryPolicyStore({})

        with self.assertRaises(UnknownTenantError):
            store.list_history("ghost")


class ValidatePolicyChangesTests(unittest.TestCase):
    def test_empty_changes_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({})

    def test_immutable_field_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"tenant_id": "other"})

    def test_state_field_rejected(self):
        """state has its own dedicated, audited path (set_state) -- a
        generic field edit must not be able to bypass it."""
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"state": "SUSPENDED"})

    def test_unknown_field_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"not_a_real_field": 1})

    def test_valid_rpm_limit_accepted(self):
        validate_policy_changes({"rpm_limit": 500})  # no raise

    def test_negative_rpm_limit_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"rpm_limit": 0})

    def test_valid_slo_accepted(self):
        validate_policy_changes({"slo": {"p95_latency_ms": 500.0}})  # no raise

    def test_slo_unknown_key_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"slo": {"p99_latency_ms": 500.0}})

    def test_negative_monthly_budget_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"monthly_budget": -1})

    def test_valid_models_list_accepted(self):
        validate_policy_changes({"models": ["model-a", "model-b"]})  # no raise

    def test_non_string_model_rejected(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy_changes({"models": ["model-a", 123]})


if __name__ == "__main__":
    unittest.main()
