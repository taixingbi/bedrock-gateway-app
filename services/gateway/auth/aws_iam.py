"""AWS IAM (SigV4) caller identity, verified upstream.

This module does **not** verify SigV4 signatures -- that's API Gateway's
`AWS_IAM` authorizer's job (see infra/modules/api_gateway). By the time a
request reaches this app on the IAM route, API Gateway has already
verified the caller's SigV4 signature and overwritten
`x-platform-principal-arn`/`x-platform-account-id` with its own verified
`$context.identity.*` values, discarding whatever the client sent. On
every other route those same headers are stripped entirely. Trusting
them here is therefore only as safe as that infra invariant -- see
auth/identity.py's module docstring.

All this module does is map a verified IAM principal ARN to the
tenant_id/application_id/roles it should have, the IAM-path equivalent of
what a JWT's claims already carry directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

import yaml
from opentelemetry import trace
from opentelemetry.propagate import inject

from .identity import AuthError

HEADER_PRINCIPAL_ARN = "x-platform-principal-arn"
HEADER_ACCOUNT_ID = "x-platform-account-id"

_tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class IamPrincipalGrant:
    tenant_id: str
    application_id: str
    roles: List[str] = field(default_factory=list)


class IamTenantResolver(Protocol):
    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        """Return the grant for a verified IAM principal ARN, or raise AuthError.

        `request_id`/`session_id` are optional and only meaningful to
        `HttpIamTenantResolver` (forwarded as the `x-request-id`/
        `x-session-id` headers so a decision logged by
        platform-authz-service can be correlated back to the request
        that triggered it) -- every in-process resolver ignores them."""
        ...

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        """M10 portal: every configured ARN pattern -> grant, for the
        Applications page. Not a live/authoritative list of *callers*
        (nothing observes who's actually calling), just the configured
        grants -- the JWT auth path has no equivalent registry at all
        (any application_id in a validly-signed token works), so this
        can only ever show the AWS_IAM/SigV4 side of the picture."""
        ...


class FileIamTenantResolver:
    """Loads `policies/iam_tenants.yaml`'s `iam_principals` map once at
    startup, the IAM-path equivalent of `FilePolicyStore`/`tenants.yaml`.

    Matching: an exact ARN match wins; otherwise a pattern ending in "*"
    matches any ARN sharing that prefix (for
    "arn:...:assumed-role/<role>/*" session-name wildcards, since an
    assumed-role ARN's last segment is the caller-chosen session name,
    not something we can enumerate in advance).

    Tolerant of a missing file (empty mapping, so every principal 403s
    with UNKNOWN_IAM_PRINCIPAL) rather than failing app startup -- not
    every environment uses the IAM auth path.
    """

    def __init__(self, path: str):
        self._exact: Dict[str, IamPrincipalGrant] = {}
        self._prefixes: Dict[str, IamPrincipalGrant] = {}

        if not path or not os.path.exists(path):
            return

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for arn_pattern, entry in (data.get("iam_principals") or {}).items():
            grant = IamPrincipalGrant(
                tenant_id=entry["tenant_id"],
                application_id=entry["application_id"],
                roles=list(entry.get("roles") or []),
            )
            if arn_pattern.endswith("*"):
                self._prefixes[arn_pattern[:-1]] = grant
            else:
                self._exact[arn_pattern] = grant

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        grant = self._exact.get(principal_arn)
        if grant is not None:
            return grant

        for prefix, candidate in self._prefixes.items():
            if principal_arn.startswith(prefix):
                return candidate

        raise AuthError(
            f"no tenant mapping for IAM principal '{principal_arn}'",
            code="UNKNOWN_IAM_PRINCIPAL",
        )

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        merged: Dict[str, IamPrincipalGrant] = dict(self._exact)
        merged.update({f"{prefix}*": grant for prefix, grant in self._prefixes.items()})
        return merged


class ProvisionedIamTenantResolver(IamTenantResolver, Protocol):
    """M11: an IamTenantResolver that also supports onboarding's
    create-only write -- `InMemoryIamTenantResolver` and
    `DynamoDbIamTenantResolver` both satisfy this; `FileIamTenantResolver`
    deliberately doesn't (existing hand-managed grants are never
    created through this path)."""

    def put_grant(self, principal_arn: str, grant: IamPrincipalGrant) -> None:
        """Raises PrincipalAlreadyMappedError on conflict."""
        ...


class PrincipalAlreadyMappedError(Exception):
    """M11: raised by put_grant() on a conditional-write conflict --
    the authoritative check, not a caller's own resolve()-then-
    put_grant(), which would still race under concurrent onboarding
    requests for the same ARN."""

    def __init__(self, principal_arn: str):
        super().__init__(f"principal '{principal_arn}' is already mapped")
        self.principal_arn = principal_arn


class InMemoryIamTenantResolver:
    """M11: in-memory ProvisionedIamTenantResolver, the AWS_IAM-path
    equivalent of policy/store.py's InMemoryPolicyStore -- what tests
    and any environment without a principal-mappings table configured
    get instead of DynamoDbIamTenantResolver."""

    def __init__(self) -> None:
        self._grants: Dict[str, IamPrincipalGrant] = {}

    def put_grant(self, principal_arn: str, grant: IamPrincipalGrant) -> None:
        if principal_arn in self._grants:
            raise PrincipalAlreadyMappedError(principal_arn)
        self._grants[principal_arn] = grant

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        grant = self._grants.get(principal_arn)
        if grant is None:
            raise AuthError(
                f"no tenant mapping for IAM principal '{principal_arn}'",
                code="UNKNOWN_IAM_PRINCIPAL",
            )
        return grant

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        return dict(self._grants)


class DynamoDbIamTenantResolver:
    """Real, durable principal-mapping storage (M11, plan section
    22.3) -- the AWS_IAM-path equivalent of policy/store.py's
    DynamoDbPolicyStore, for the exact same reason: a principal mapping
    *provisioned* through onboarding (onboarding/provisioning.py)
    shouldn't require a `policies/iam_tenants.yaml` PR to take effect.
    Used as a layer (`LayeredIamTenantResolver` below), never a
    replacement for `FileIamTenantResolver`.

    Exact-ARN lookups only -- no prefix/wildcard matching. An
    onboarding request names one specific principal ARN; the "*"
    session-name-wildcard case (FileIamTenantResolver's `_prefixes`)
    only makes sense for hand-authored, review-gated entries.
    """

    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put_grant(self, principal_arn: str, grant: IamPrincipalGrant) -> None:
        """Provisioning-only: fails if this ARN is already mapped here,
        so onboarding can never silently overwrite an existing
        provisioned grant. Does not protect against colliding with a
        *file*-configured ARN -- callers must check
        `LayeredIamTenantResolver` first."""
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item={
                    "principal_arn": principal_arn,
                    "tenant_id": grant.tenant_id,
                    "application_id": grant.application_id,
                    "roles": list(grant.roles),
                },
                ConditionExpression="attribute_not_exists(principal_arn)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise PrincipalAlreadyMappedError(principal_arn) from exc
            raise

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        response = self._table.get_item(Key={"principal_arn": principal_arn})
        item = response.get("Item")
        if item is None:
            raise AuthError(
                f"no tenant mapping for IAM principal '{principal_arn}'",
                code="UNKNOWN_IAM_PRINCIPAL",
            )
        return IamPrincipalGrant(
            tenant_id=item["tenant_id"],
            application_id=item["application_id"],
            roles=list(item.get("roles", [])),
        )

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        merged: Dict[str, IamPrincipalGrant] = {}
        kwargs: Dict[str, object] = {}
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                merged[item["principal_arn"]] = IamPrincipalGrant(
                    tenant_id=item["tenant_id"],
                    application_id=item["application_id"],
                    roles=list(item.get("roles", [])),
                )
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return merged


class LayeredIamTenantResolver:
    """`primary` (provisioned/DynamoDB) checked first, `fallback`
    (hand-configured/file) second -- additive, mirrors
    policy/store.py's LayeredPolicyStore. A given ARN can only ever be
    mapped in one layer (provisioning refuses to create a grant for an
    ARN the fallback already maps -- see onboarding/provisioning.py)."""

    def __init__(self, *, primary: ProvisionedIamTenantResolver, fallback: IamTenantResolver):
        self._primary = primary
        self._fallback = fallback

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        try:
            return self._primary.resolve(principal_arn, request_id=request_id, session_id=session_id)
        except AuthError:
            return self._fallback.resolve(principal_arn, request_id=request_id, session_id=session_id)

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        merged = dict(self._fallback.list_grants())
        merged.update(self._primary.list_grants())
        return merged


class HttpIamTenantResolver:
    """M12 (plan.md Section 5): delegates principal mapping to
    platform-authz-service's `POST /v1/authorize` instead of resolving
    it in-process. Satisfies the same `IamTenantResolver` Protocol
    every other resolver in this file does -- wired in at main.py as a
    straight swap for `LayeredIamTenantResolver` when
    `AUTHZ_SERVICE_URL` is configured, so no route handler or pipeline
    stage changes; this is the seam the Protocol already existed for.

    urllib.request, not a new HTTP client dependency -- same choice
    jwt_verifier.py's JwksVerifier already made for its own outbound
    call.

    `ca_cert_pem`, when set, pins TLS verification to exactly that CA
    instead of the system trust store -- authz-service's ALB cert is
    issued by a private CA (bedrock-gateway-infra's
    aws_acmpca_certificate_authority.internal), which no public trust
    store knows about, so the default `ssl` behavior would reject it.
    Empty means "use the system default" (plain HTTP in dev/tests, or
    an environment that hasn't set this up).
    """

    def __init__(self, *, base_url: str, timeout_s: float = 5.0, ca_cert_pem: str = ""):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._ssl_context = None
        if ca_cert_pem:
            import ssl

            self._ssl_context = ssl.create_default_context(cadata=ca_cert_pem)

    def resolve(
        self, principal_arn: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> IamPrincipalGrant:
        import json
        import urllib.error
        import urllib.request

        with _tracer.start_as_current_span("authz.authorize") as span:
            body = json.dumps(
                {"identity": {"subject": principal_arn, "auth_type": "aws_iam"}, "action": "llm.invoke"}
            ).encode("utf-8")
            headers = {"content-type": "application/json"}
            if request_id:
                # Lets platform-authz-service's own decision log carry the
                # SAME request_id as this request's gateway.chat/gateway.access
                # lines, instead of minting an unrelated one -- see
                # authz-service's main.py, which honors this header.
                headers["x-request-id"] = request_id
            if session_id:
                headers["x-session-id"] = session_id
            # W3C traceparent -- lets authz-service's own span be a
            # CHILD of this one (same trace_id), not an unrelated trace.
            # inject() writes into whatever dict-like carrier it's given
            # using the process's configured propagator (W3C TraceContext
            # by default), no manual header formatting needed.
            inject(headers)
            request = urllib.request.Request(
                f"{self._base_url}/v1/authorize",
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout_s, context=self._ssl_context
                ) as resp:  # noqa: S310 (fixed internal URL, not user input)
                    data = json.loads(resp.read())
            except urllib.error.URLError as exc:
                span.set_attribute("error", str(exc))
                raise AuthError(
                    f"authz-service unavailable: {exc}", code="AUTHORIZATION_SERVICE_UNAVAILABLE"
                ) from exc

            span.set_attribute("authz.decision", data["decision"])
            if data["decision"] != "ALLOW":
                raise AuthError(
                    f"no tenant mapping for IAM principal '{principal_arn}'", code="UNKNOWN_IAM_PRINCIPAL"
                )
            return IamPrincipalGrant(
                tenant_id=data["tenant_id"], application_id=data["application_id"], roles=list(data["roles"])
            )

    def list_grants(self) -> Dict[str, IamPrincipalGrant]:
        import json
        import urllib.request

        with urllib.request.urlopen(
            f"{self._base_url}/v1/grants", timeout=self._timeout_s, context=self._ssl_context
        ) as resp:  # noqa: S310
            data = json.loads(resp.read())
        return {
            arn: IamPrincipalGrant(
                tenant_id=g["tenant_id"], application_id=g["application_id"], roles=list(g["roles"])
            )
            for arn, g in data["grants"].items()
        }
