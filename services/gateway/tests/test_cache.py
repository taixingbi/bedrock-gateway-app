import unittest

from starlette.testclient import TestClient

from ..cache.keys import build_cache_key
from ..cache.store import CachedResponse, InMemoryResponseCache
from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from .auth_fixtures import auth_header, get_auth_fixture
from .fake_clock import FakeClock
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, policy_epoch=1, guardrail_policy="standard-v1")
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class CacheKeyTests(unittest.TestCase):
    def _key(self, **overrides):
        defaults = dict(
            tenant_id="acme",
            application_id="app1",
            policy=_policy(),
            model_id="model-a",
            max_tokens=100,
            temperature=0.5,
            messages=[{"role": "user", "content": "hi"}],
        )
        defaults.update(overrides)
        return build_cache_key(**defaults)

    def test_identical_inputs_produce_identical_key(self):
        self.assertEqual(self._key(), self._key())

    def test_different_tenant_produces_different_key(self):
        self.assertNotEqual(self._key(), self._key(tenant_id="other-tenant"))

    def test_different_messages_produce_different_key(self):
        other_messages = [{"role": "user", "content": "bye"}]
        self.assertNotEqual(self._key(), self._key(messages=other_messages))

    def test_different_policy_epoch_produces_different_key(self):
        self.assertNotEqual(self._key(), self._key(policy=_policy(policy_epoch=2)))

    def test_different_guardrail_version_produces_different_key(self):
        self.assertNotEqual(
            self._key(), self._key(policy=_policy(guardrail_policy="finance-strict-v1"))
        )


class InMemoryResponseCacheTests(unittest.TestCase):
    def test_miss_returns_none(self):
        cache = InMemoryResponseCache()
        self.assertIsNone(cache.get("missing-key"))

    def test_set_then_get_hits(self):
        cache = InMemoryResponseCache()
        value = CachedResponse(text="hi", stop_reason="end_turn", input_tokens=1, output_tokens=1, model_id="m")
        cache.set("k1", value)
        self.assertEqual(cache.get("k1"), value)

    def test_ttl_expiry(self):
        clock = FakeClock()
        cache = InMemoryResponseCache(ttl_s=10.0, clock=clock)
        value = CachedResponse(text="hi", stop_reason="end_turn", input_tokens=1, output_tokens=1, model_id="m")
        cache.set("k1", value)

        clock.advance(5.0)
        self.assertIsNotNone(cache.get("k1"))  # still within TTL

        clock.advance(6.0)  # total 11s, past TTL
        self.assertIsNone(cache.get("k1"))

    def test_lru_eviction_at_max_entries(self):
        cache = InMemoryResponseCache(max_entries=2)
        v = lambda i: CachedResponse(text=str(i), stop_reason=None, input_tokens=0, output_tokens=0, model_id="m")

        cache.set("a", v(1))
        cache.set("b", v(2))
        cache.set("c", v(3))  # evicts "a" (least recently used)

        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))


class CacheIntegrationTests(unittest.TestCase):
    def _app(self, *, converse_client, policy_store):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings,
            converse_client=converse_client,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
        )
        return TestClient(app), fixture

    def test_identical_request_is_a_cache_hit_on_second_call(self):
        fake = FakeConverseClient(response_text="cached answer")
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme")})
        client, fixture = self._app(converse_client=fake, policy_store=policy_store)
        token = fixture.token(tenant_id="acme")
        body = {"messages": [{"role": "user", "content": "same question"}]}

        first = client.post("/v1/chat", json=body, headers=auth_header(token))
        second = client.post("/v1/chat", json=body, headers=auth_header(token))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["cache_hit"])
        self.assertTrue(second.json()["cache_hit"])
        self.assertEqual(second.json()["output"], "cached answer")
        self.assertEqual(len(fake.calls), 1)  # Bedrock called only once

    def test_policy_epoch_bump_invalidates_cache(self):
        """Going through the real admin endpoint (not mutating the policy
        store directly) so the M2 push-invalidation actually fires --
        otherwise the gateway's PolicySnapshotCache would keep serving the
        pre-bump policy for up to policy_cache_ttl_s regardless of what
        changed underneath it."""
        fake = FakeConverseClient(response_text="first answer")
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme")})
        client, fixture = self._app(converse_client=fake, policy_store=policy_store)
        token = fixture.token(tenant_id="acme")
        admin_token = fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"])
        body = {"messages": [{"role": "user", "content": "same question"}]}

        first = client.post("/v1/chat", json=body, headers=auth_header(token))
        client.put(
            "/v1/admin/tenants/acme/state",
            json={"state": "ACTIVE"},  # unchanged state, but still bumps policy_epoch + invalidates
            headers=auth_header(admin_token),
        )
        second = client.post("/v1/chat", json=body, headers=auth_header(token))

        self.assertFalse(first.json()["cache_hit"])
        self.assertFalse(second.json()["cache_hit"])  # epoch bump forced a miss
        self.assertEqual(len(fake.calls), 2)


if __name__ == "__main__":
    unittest.main()
