"""Provisioning worker (M11, plan section 22.3): turns an APPROVED
OnboardingRequest into a real principal mapping and, if needed, a real
tenant policy -- writes only, no Terraform, no redeploy. Runs inline on
the approve HTTP call for the MVP (no separate queue/worker process --
provisioning here is a handful of DynamoDB writes, not a long-running
job like M7's async jobs).

Deliberately conservative about what "onboarding" is allowed to touch:
it only ever *creates* new rows, never updates an existing tenant's
policy or an existing principal's grant. A request naming a tenant_id
or principal_arn that already exists (in either the file-based config
or a previously-provisioned row) fails outright -- see
ApplicationAlreadyExistsError -- rather than silently overwriting
something a platform admin (or an earlier onboarding request) already
configured. Widening an existing application's quota/models is a
change request against that resource, not a new onboarding request;
this module doesn't implement that yet (plan section 22.4).
"""
from __future__ import annotations

from ..auth.aws_iam import IamPrincipalGrant, PrincipalAlreadyMappedError, ProvisionedIamTenantResolver
from ..policy.models import TenantAlreadyExistsError, TenantPolicy, TenantState, UnknownTenantError
from ..policy.store import PolicyStore, ProvisionedPolicyStore
from .models import AuthType, OnboardingRequest


class ApplicationAlreadyExistsError(Exception):
    pass


def provision(
    request: OnboardingRequest,
    *,
    policy_store: PolicyStore,
    policy_store_primary: ProvisionedPolicyStore,
    iam_tenant_resolver_primary: ProvisionedIamTenantResolver,
) -> None:
    """`policy_store` is the layered (read-through) view, used only to
    check whether tenant_id already exists anywhere before creating;
    `policy_store_primary`/`iam_tenant_resolver_primary` are the
    provisioned-only stores actually written to -- provisioning is the
    one caller allowed to write there, while every other caller (the
    chat/admin pipeline) only ever reads through the layered view.

    Raises ApplicationAlreadyExistsError on a real conflict (caller
    should mark the request FAILED); a boto3 ClientError propagates for
    a transient AWS-side failure, same handling.
    """
    if request.auth_type == AuthType.OAUTH:
        raise ApplicationAlreadyExistsError(
            "auth_type=oauth is not automated yet (plan section 22.6) -- "
            "provision this application's Cognito/OIDC client by hand, then "
            "add its tenant_id to tenants.yaml/route it through the JWT path "
            "directly; there is nothing this onboarding flow can do for it"
        )

    tenant_is_new = True
    try:
        policy_store.get(request.tenant_id)
        tenant_is_new = False
    except UnknownTenantError:
        pass

    if tenant_is_new:
        try:
            policy_store_primary.create(
                TenantPolicy(
                    tenant_id=request.tenant_id,
                    state=TenantState.ACTIVE,
                    models=list(request.requested_models),
                    rpm_limit=request.rpm_limit,
                    guardrail_policy=request.guardrail_policy,
                    monthly_budget=request.monthly_budget,
                )
            )
        except TenantAlreadyExistsError as exc:
            raise ApplicationAlreadyExistsError(str(exc)) from exc

    if request.principal_arn:
        try:
            iam_tenant_resolver_primary.put_grant(
                request.principal_arn,
                IamPrincipalGrant(
                    tenant_id=request.tenant_id,
                    application_id=request.application_id,
                    roles=["developer"],
                ),
            )
        except PrincipalAlreadyMappedError as exc:
            raise ApplicationAlreadyExistsError(str(exc)) from exc
