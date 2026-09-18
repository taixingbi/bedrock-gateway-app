#!/usr/bin/env bash
# Manual smoke test against the deployed gateway's AWS_IAM/SigV4 route.
# Assumes an IAM role, generates trace_id/span_id/session_id/request_id,
# and posts one /v1/chat request with the full header set (see
# telemetry/middleware.py + telemetry/logging.py for what each becomes
# in the gateway's logs).
#
# Usage:
#   ./scripts/smoke-test-iam.sh [role-arn] [message] [api-url]
#
#   role-arn  default: arn:aws:iam::646821141010:role/finance-manual-smoke-test
#             (real monthly_budget, see policies/iam_tenants.yaml for
#             every configured principal -> tenant mapping)
#   message   default: "Say hi in one word."
#   api-url   default: the dev IAM route
#
# For the dedicated-account isolation path (tenant-a/tenant-b, plan.md
# section 29.2) this single assume-role isn't enough -- that needs a
# second hop (the tenant's own role assuming a *-platform-caller role
# in the management account) before calling. Not scripted here since
# it's a two-account, two-role setup rather than a single parameter.
set -euo pipefail

ROLE_ARN="${1:-arn:aws:iam::646821141010:role/finance-manual-smoke-test}"
MESSAGE="${2:-Say hi in one word.}"
API_URL="${3:-https://as1n3q8d33.execute-api.us-east-1.amazonaws.com/iam/v1/chat}"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

CREDS=$(aws sts assume-role \
  --role-arn "$ROLE_ARN" \
  --role-session-name smoke-test \
  --query 'Credentials' --output json)
AWS_ACCESS_KEY_ID=$(echo "$CREDS" | python3 -c "import json,sys; print(json.load(sys.stdin)['AccessKeyId'])")
AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | python3 -c "import json,sys; print(json.load(sys.stdin)['SecretAccessKey'])")
AWS_SESSION_TOKEN=$(echo "$CREDS" | python3 -c "import json,sys; print(json.load(sys.stdin)['SessionToken'])")

REQUEST_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
SESSION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
TRACE_ID=$(python3 -c "import secrets; print(secrets.token_hex(16))")
PARENT_SPAN_ID=$(python3 -c "import secrets; print(secrets.token_hex(8))")
TRACEPARENT="00-${TRACE_ID}-${PARENT_SPAN_ID}-01"

echo "role=$ROLE_ARN"
echo "request_id=$REQUEST_ID"
echo "session_id=$SESSION_ID"
echo "trace_id=$TRACE_ID"
echo "---"

BODY=$(MESSAGE="$MESSAGE" python3 -c "import json, os; print(json.dumps({'messages': [{'role': 'user', 'content': os.environ['MESSAGE']}]}))")

curl -sS -i -X POST "$API_URL" \
  --aws-sigv4 "aws:amz:us-east-1:execute-api" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
  -H "x-request-id: $REQUEST_ID" \
  -H "x-session-id: $SESSION_ID" \
  -H "traceparent: $TRACEPARENT" \
  -H "content-type: application/json" \
  -d "$BODY"
echo
