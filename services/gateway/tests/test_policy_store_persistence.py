import tempfile
import unittest
from pathlib import Path

import yaml

from ..policy.models import TenantPolicy
from ..policy.store import _item_to_policy, _policy_to_item, load_policies_from_yaml


class DebugCaptureRetentionDaysYamlTests(unittest.TestCase):
    """debug_capture_retention_days is new (audit-store work) -- neither
    load path had any coverage for it before, so this guards both the
    happy path (set) and the default (unset -> None, not 0 or a crash)."""

    def _write(self, tenants_cfg: dict) -> str:
        d = tempfile.mkdtemp()
        path = Path(d) / "tenants.yaml"
        path.write_text(yaml.safe_dump({"tenants": tenants_cfg}))
        return str(path)

    def test_retention_days_set_round_trips(self):
        path = self._write(
            {
                "acme": {
                    "state": "ACTIVE", "models": [], "rpm_limit": 60, "guardrail_policy": "standard-v1",
                    "policy_epoch": 1, "debug_capture_enabled": True, "debug_capture_retention_days": 30,
                }
            }
        )
        store = load_policies_from_yaml(path)
        policy = store.get("acme")
        self.assertEqual(policy.debug_capture_retention_days, 30)

    def test_retention_days_unset_defaults_to_none(self):
        path = self._write(
            {
                "acme": {
                    "state": "ACTIVE", "models": [], "rpm_limit": 60, "guardrail_policy": "standard-v1",
                    "policy_epoch": 1,
                }
            }
        )
        store = load_policies_from_yaml(path)
        policy = store.get("acme")
        self.assertIsNone(policy.debug_capture_retention_days)


class DebugCaptureRetentionDaysDynamoItemTests(unittest.TestCase):
    def test_set_value_round_trips_through_item(self):
        policy = TenantPolicy(tenant_id="acme", debug_capture_retention_days=45)
        item = _policy_to_item(policy)
        self.assertEqual(item["debug_capture_retention_days"], 45)
        restored = _item_to_policy(item)
        self.assertEqual(restored.debug_capture_retention_days, 45)

    def test_unset_value_omitted_from_item_not_written_as_null(self):
        policy = TenantPolicy(tenant_id="acme")
        item = _policy_to_item(policy)
        self.assertNotIn("debug_capture_retention_days", item)
        restored = _item_to_policy(item)
        self.assertIsNone(restored.debug_capture_retention_days)


if __name__ == "__main__":
    unittest.main()
