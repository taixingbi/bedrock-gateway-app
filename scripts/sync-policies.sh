#!/usr/bin/env bash
# Refreshes policies/ from the canonical bedrock-gateway-policies repo.
#
# Interim mechanism (see policies/*.yaml's banner comment): until the
# gateway has a working S3/DynamoDB-backed PolicyStore, the Docker image
# needs a local copy of the policy YAML to bake in at build time. This
# script is that copy step, run by hand whenever the policies repo
# changes -- not on a schedule, not in CI. Delete this script (and
# policies/) once phase 2 lands.
#
# Usage:
#   ./scripts/sync-policies.sh [path-to-bedrock-gateway-policies-checkout]
#   (defaults to ../bedrock-gateway-policies, i.e. a sibling checkout)
set -euo pipefail

SRC="${1:-../bedrock-gateway-policies}/environments/dev"
DST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/policies"

if [ ! -d "$SRC" ]; then
  echo "error: $SRC not found -- pass the path to a bedrock-gateway-policies checkout" >&2
  exit 1
fi

for f in tenants.yaml route_sets.yaml iam_tenants.yaml certified_models.yaml; do
  banner=$(sed -n '/^# ====/,/^# ====/p' "$DST/$f")
  { printf '%s\n' "$banner"; cat "$SRC/$f"; } > "$DST/$f.tmp"
  mv "$DST/$f.tmp" "$DST/$f"
  echo "synced $f from $SRC"
done
