"""Plan section 34.4: unified, durable, metadata-only audit event.
Store-level tests here (InMemoryRequestAuditStore's bounded ring
buffer, S3RequestAuditStore's key shape); /v1/chat wiring in
RequestAuditWiringTests below.
"""
import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..telemetry.request_audit import InMemoryRequestAuditStore, RequestAuditEvent, S3RequestAuditStore
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


def _event(**overrides) -> RequestAuditEvent:
    defaults = dict(
        request_id="req-1", tenant_id="acme", application_id="app1", principal="u1",
        action="chat.completion", status=200,
    )
    defaults.update(overrides)
    return RequestAuditEvent(**defaults)


class InMemoryRequestAuditStoreTests(unittest.TestCase):
    def test_write_appends_event(self):
        store = InMemoryRequestAuditStore()

        store.write(_event())

        self.assertEqual(len(store.events), 1)
        self.assertEqual(store.events[0].request_id, "req-1")

    def test_ring_buffer_drops_oldest_beyond_maxlen(self):
        """A long-running ECS task with no bucket configured must not
        leak memory forever -- oldest events are dropped, not an
        unbounded list."""
        store = InMemoryRequestAuditStore(maxlen=3)

        for i in range(5):
            store.write(_event(request_id=f"req-{i}"))

        self.assertEqual(len(store.events), 3)
        self.assertEqual([e.request_id for e in store.events], ["req-2", "req-3", "req-4"])


class FakeS3Client:
    def __init__(self) -> None:
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)


class S3RequestAuditStoreTests(unittest.TestCase):
    def test_writes_one_object_keyed_by_tenant_and_date(self):
        from datetime import datetime, timezone

        fake_client = FakeS3Client()
        store = S3RequestAuditStore(
            bucket="my-audit-bucket", client=fake_client,
            clock=lambda: datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc),
        )

        store.write(_event(request_id="req-42", tenant_id="finance"))

        self.assertEqual(len(fake_client.put_calls), 1)
        call = fake_client.put_calls[0]
        self.assertEqual(call["Bucket"], "my-audit-bucket")
        self.assertEqual(call["Key"], "finance/2026/09/19/req-42.json")
        self.assertEqual(call["ServerSideEncryption"], "AES256")

    def test_body_contains_no_prompt_or_response_text_fields(self):
        """The whole point of this being always-on (not opt-in) is
        that it carries metadata only -- confirms the serialized body
        has no field that could ever hold raw prompt/response content."""
        import json

        fake_client = FakeS3Client()
        store = S3RequestAuditStore(bucket="b", client=fake_client)

        store.write(_event(request_id="req-1", tenant_id="acme"))

        body = json.loads(fake_client.put_calls[0]["Body"])
        text_bearing_keys = {"input_text", "output_text", "text", "content", "prompt", "response"}
        self.assertFalse(text_bearing_keys & set(body.keys()))


class RequestAuditWiringTests(unittest.TestCase):
    """Confirms request_audit_store is actually threaded into
    /v1/chat, not just unit-tested against the store classes directly."""

    def _app(self, *, policy_store, request_audit_store):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings, converse_client=FakeConverseClient(), token_verifier=fixture.verifier,
            policy_store=policy_store, request_audit_store=request_audit_store,
        )
        return TestClient(app), fixture

    def test_successful_chat_writes_allow_event_with_full_fields(self):
        policy_store = InMemoryPolicyStore({"acme": _policy(policy_epoch=3)})
        audit_store = InMemoryRequestAuditStore()
        client, fixture = self._app(policy_store=policy_store, request_audit_store=audit_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(audit_store.events), 1)
        event = audit_store.events[0]
        self.assertEqual(event.tenant_id, "acme")
        self.assertEqual(event.application_id, "risk-chat")  # auth_fixtures default
        self.assertEqual(event.status, 200)
        self.assertEqual(event.authz_decision, "ALLOW")
        self.assertEqual(event.policy_version, 3)
        self.assertIsNotNone(event.model)
        self.assertIsNotNone(event.estimated_cost)

    def test_kill_switch_rejection_writes_deny_event(self):
        policy_store = InMemoryPolicyStore(
            {"acme": _policy(state=TenantState.SUSPENDED)}
        )
        audit_store = InMemoryRequestAuditStore()
        client, fixture = self._app(policy_store=policy_store, request_audit_store=audit_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(audit_store.events), 1)
        event = audit_store.events[0]
        self.assertEqual(event.authz_decision, "DENY")
        self.assertEqual(event.status, 403)

    def test_no_store_configured_is_a_silent_noop(self):
        """The default (no request_audit_store override, no bucket
        env var) must never break a request -- see main.py's
        InMemoryRequestAuditStore fallback."""
        policy_store = InMemoryPolicyStore({"acme": _policy()})
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings, converse_client=FakeConverseClient(), token_verifier=fixture.verifier,
            policy_store=policy_store,
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
