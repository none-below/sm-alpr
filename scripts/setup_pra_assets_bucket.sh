#!/usr/bin/env bash
# Create the PRA-assets S3 bucket and its scoped IAM principals.
#
# Bucket: general purpose, account-regional namespace (the name can never be
# re-registered by another account, even after deletion), private, SSE-S3.
# Write-once is enforced two ways:
#   - Object Lock default retention: no stored version can be deleted or
#     altered until retention expires.
#   - Bucket policy requiring If-None-Match on every object write: a key can
#     be written exactly once, so a re-upload can't even add a new version.
#     Uploaders must use `aws s3 cp --no-overwrite` (or put-object
#     --if-none-match '*'); a 412 PreconditionFailed means "already stored".
#     CopyObject into the bucket is blocked as a side effect.
#
# Principals (inline IAM user policies; reuse the JSON for OIDC roles later):
#   <prefix>-writer  PutObject only -- cannot read, list, delete, or set retention
#   <prefix>-reader  Get/List only  -- enough for DuckDB s3:// globbing
#
# Re-runnable without undoing deliberate changes: each bucket setting and IAM
# policy is applied only when it's missing. One that differs from this
# script's default (public access opened later, a stricter lock, extra
# lifecycle rules) is reported and left alone unless REAPPLY=1.
# Run with an admin profile:
#
#   AWS_PROFILE=sm-alpr-admin scripts/setup_pra_assets_bucket.sh
#   AWS_PROFILE=sm-alpr-admin MINT_KEYS=1 scripts/setup_pra_assets_bucket.sh
#
# MINT_KEYS=1 creates one access key per principal (skipped if it already has
# one) and writes it straight into local profiles <prefix>-writer and
# <prefix>-reader in ~/.aws/credentials. The secret is never printed or put
# on a command line, and a key that can't be saved is deleted from IAM.
#
# PRA_S3_REGION / PRA_S3_PREFIX override the bucket's region and name prefix.
set -euo pipefail

# Same env overrides and defaults as scripts/pra_s3_upload.py (a test keeps them in sync).
REGION="${PRA_S3_REGION:-us-west-2}"
PREFIX="${PRA_S3_PREFIX:-sm-alpr-pra}"   # <= 37 chars; the -<acct>-<region>-an suffix takes the rest
LOCK_MODE="${LOCK_MODE:-GOVERNANCE}"   # GOVERNANCE: admin can bypass. COMPLIANCE: nobody can, incl. root
LOCK_YEARS="${LOCK_YEARS:-10}"
WRITER="${PREFIX}-writer"
READER="${PREFIX}-reader"

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
BUCKET="${PREFIX}-${ACCOUNT_ID}-${REGION}-an"
ARN="arn:aws:s3:::${BUCKET}"

bucket_policy() {
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["${ARN}", "${ARN}/*"],
      "Condition": {"Bool": {"aws:SecureTransport": "false"}}
    },
    {
      "Sid": "DenyWritesWithoutIfNoneMatch",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "${ARN}/*",
      "Condition": {
        "Null": {"s3:if-none-match": "true"},
        "Bool": {"s3:ObjectCreationOperation": "true"}
      }
    }
  ]
}
EOF
}

writer_policy() {
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteOnly",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "${ARN}/*"
    }
  ]
}
EOF
}

reader_policy() {
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "${ARN}"
    },
    {
      "Sid": "ReadObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectAttributes"],
      "Resource": "${ARN}/*"
    }
  ]
}
EOF
}

# Print the policies without touching AWS (ACCOUNT_ID must be set).
if [[ "${1:-}" == "--print-policies" ]]; then
  echo "# bucket: ${BUCKET}"; bucket_policy; writer_policy; reader_policy
  exit 0
fi

echo "account ${ACCOUNT_ID}, bucket ${BUCKET}"

json_covers() {  # <desired> <current>: exit 0 if every field of desired has the same value in current
  python3 - "$1" "$2" <<'EOF'
import json, sys

def covers(d, c):
    if isinstance(d, dict):
        return isinstance(c, dict) and all(k in c and covers(v, c[k]) for k, v in d.items())
    if isinstance(d, list):
        return isinstance(c, list) and len(d) == len(c) and all(map(covers, d, c))
    return d == c

sys.exit(0 if covers(json.loads(sys.argv[1]), json.loads(sys.argv[2])) else 1)
EOF
}

apply_setting() {  # <name> <current json, empty if unset> <desired json> <command...>
  local name=$1 current=$2 desired=$3
  shift 3
  if [[ -z "$current" || "$current" == "null" || "$current" == "None" ]]; then
    "$@" >/dev/null
    echo "  $name: set"
  elif json_covers "$desired" "$current"; then
    echo "  $name: ok"
  elif [[ "${REAPPLY:-0}" == "1" ]]; then
    "$@" >/dev/null
    echo "  $name: differed; re-applied (REAPPLY=1)"
  else
    echo "  $name: differs from this script's default; left as is (REAPPLY=1 overwrites)" >&2
    echo "    current: $current" >&2
    echo "    default: $desired" >&2
  fi
}

s3get() {  # <get-command> <query>: the bucket setting as JSON, empty if unset
  aws s3api "$1" --bucket "$BUCKET" --region "$REGION" --query "$2" --output json 2>/dev/null || true
}

# --- bucket ---------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  echo "bucket exists"
else
  location=()
  [[ "$REGION" != "us-east-1" ]] && location=(--create-bucket-configuration "LocationConstraint=${REGION}")
  # Object Lock at creation also turns on versioning (required by Object Lock).
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --bucket-namespace account-regional \
    --object-lock-enabled-for-bucket \
    ${location[@]+"${location[@]}"} >/dev/null
  echo "created bucket"
fi

pab='{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}'
apply_setting "public access block" "$(s3get get-public-access-block PublicAccessBlockConfiguration)" "$pab" \
  aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
  --public-access-block-configuration "$pab"

ownership='{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}'
apply_setting "object ownership" "$(s3get get-bucket-ownership-controls OwnershipControls)" "$ownership" \
  aws s3api put-bucket-ownership-controls --bucket "$BUCKET" --region "$REGION" \
  --ownership-controls "$ownership"

# SSE-S3, not KMS: anonymous/public reads can't decrypt SSE-KMS objects.
encryption='{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}'
apply_setting "encryption" "$(s3get get-bucket-encryption ServerSideEncryptionConfiguration)" "$encryption" \
  aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
  --server-side-encryption-configuration "$encryption"

retention="{\"DefaultRetention\":{\"Mode\":\"${LOCK_MODE}\",\"Years\":${LOCK_YEARS}}}"
apply_setting "object lock default retention" \
  "$(s3get get-object-lock-configuration ObjectLockConfiguration.Rule)" "$retention" \
  aws s3api put-object-lock-configuration --bucket "$BUCKET" --region "$REGION" \
  --object-lock-configuration "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":${retention}}"

# The writer can't list, so it can't find or clean up its own failed multipart
# uploads; expire them instead of paying for orphaned parts.
lifecycle='[{"ID":"abort-incomplete-mpu","Status":"Enabled","Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]'
apply_setting "lifecycle rules" "$(s3get get-bucket-lifecycle-configuration Rules)" "$lifecycle" \
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
  --lifecycle-configuration "{\"Rules\":${lifecycle}}"

apply_setting "bucket policy" \
  "$(aws s3api get-bucket-policy --bucket "$BUCKET" --region "$REGION" --query Policy --output text 2>/dev/null || true)" \
  "$(bucket_policy)" \
  aws s3api put-bucket-policy --bucket "$BUCKET" --region "$REGION" --policy "$(bucket_policy)"
echo "bucket configured"

# --- principals -------------------------------------------------------------
ensure_user() {  # <user> <policy-json>
  if ! aws iam get-user --user-name "$1" >/dev/null 2>&1; then
    aws iam create-user --user-name "$1" >/dev/null
    echo "created user $1"
  fi
  apply_setting "$1 policy" \
    "$(aws iam get-user-policy --user-name "$1" --policy-name "${PREFIX}-access" \
         --query PolicyDocument --output json 2>/dev/null || true)" \
    "$2" \
    aws iam put-user-policy --user-name "$1" --policy-name "${PREFIX}-access" --policy-document "$2"
}

ensure_user "$WRITER" "$(writer_policy)"
ensure_user "$READER" "$(reader_policy)"
echo "principals configured"

mint_key() {  # <user>: new key -> local profile of the same name
  local keys local_id
  keys=$(aws iam list-access-keys --user-name "$1" --query 'AccessKeyMetadata[].AccessKeyId' --output text)
  if [[ -n "$keys" ]]; then
    local_id=$(aws configure get aws_access_key_id --profile "$1" 2>/dev/null || true)
    # --output text separates IDs with tabs; match whole lines, not substrings.
    if [[ -n "$local_id" ]] && tr '\t' '\n' <<<"$keys" | grep -qxF "$local_id"; then
      echo "$1: local profile already holds its key"
    else
      echo "$1 has key(s) $keys in IAM that aren't in the local profile." >&2
      echo "  If none are in use elsewhere: aws iam delete-access-key --user-name $1 --access-key-id <id>, then re-run." >&2
    fi
    return
  fi
  local id secret
  read -r id secret < <(aws iam create-access-key --user-name "$1" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
  # printf is a builtin and the CSV goes in on stdin, so the secret never
  # appears in any process's argv (visible to other users via ps).
  if ! printf 'User name,Access key ID,Secret access key\n%s,%s,%s\n' "$1" "$id" "$secret" \
      | aws configure import --csv file:///dev/stdin >/dev/null; then
    # Don't strand a key nobody holds; the next run would refuse to mint.
    aws iam delete-access-key --user-name "$1" --access-key-id "$id"
    echo "saving the key for $1 failed; deleted it from IAM" >&2
    return 1
  fi
  aws configure set region "$REGION" --profile "$1"
  echo "minted key for $1 -> profile $1"
}

if [[ "${MINT_KEYS:-0}" == "1" ]]; then
  mint_key "$WRITER"
  mint_key "$READER"
fi

echo "done: s3://${BUCKET}"
