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
# Re-runnable: every step either no-ops or re-applies the same config.
# Run with an admin profile:
#
#   AWS_PROFILE=sm-alpr-admin scripts/setup_pra_assets_bucket.sh
#   AWS_PROFILE=sm-alpr-admin MINT_KEYS=1 scripts/setup_pra_assets_bucket.sh
#
# MINT_KEYS=1 creates one access key per principal (skipped if it already has
# one) and writes it straight into local profiles <prefix>-writer and
# <prefix>-reader in ~/.aws/credentials -- the secret is never printed.
set -euo pipefail

REGION="${REGION:-us-west-2}"
PREFIX="${PREFIX:-sm-alpr-pra}"        # <= 37 chars; the -<acct>-<region>-an suffix takes the rest
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

aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-ownership-controls --bucket "$BUCKET" --region "$REGION" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'

# SSE-S3, not KMS: anonymous/public reads can't decrypt SSE-KMS objects.
aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}'

aws s3api put-object-lock-configuration --bucket "$BUCKET" --region "$REGION" \
  --object-lock-configuration \
  "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":{\"DefaultRetention\":{\"Mode\":\"${LOCK_MODE}\",\"Years\":${LOCK_YEARS}}}}"

# The writer can't list, so it can't find or clean up its own failed multipart
# uploads; expire them instead of paying for orphaned parts.
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
  --lifecycle-configuration \
  '{"Rules":[{"ID":"abort-incomplete-mpu","Status":"Enabled","Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}' \
  >/dev/null

aws s3api put-bucket-policy --bucket "$BUCKET" --region "$REGION" \
  --policy "$(bucket_policy)"
echo "bucket configured (${LOCK_MODE}, ${LOCK_YEARS}y default retention)"

# --- principals -------------------------------------------------------------
ensure_user() {  # <user> <policy-json>
  if ! aws iam get-user --user-name "$1" >/dev/null 2>&1; then
    aws iam create-user --user-name "$1" >/dev/null
    echo "created user $1"
  fi
  aws iam put-user-policy --user-name "$1" --policy-name "${PREFIX}-access" \
    --policy-document "$2"
}

ensure_user "$WRITER" "$(writer_policy)"
ensure_user "$READER" "$(reader_policy)"
echo "principals configured"

mint_key() {  # <user>: new key -> local profile of the same name
  local n
  n=$(aws iam list-access-keys --user-name "$1" --query 'length(AccessKeyMetadata)' --output text)
  if [[ "$n" != "0" ]]; then
    echo "$1 already has an access key; not minting another"
    return
  fi
  local id secret
  read -r id secret < <(aws iam create-access-key --user-name "$1" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
  aws configure set aws_access_key_id "$id" --profile "$1"
  aws configure set aws_secret_access_key "$secret" --profile "$1"
  aws configure set region "$REGION" --profile "$1"
  echo "minted key for $1 -> profile $1"
}

if [[ "${MINT_KEYS:-0}" == "1" ]]; then
  mint_key "$WRITER"
  mint_key "$READER"
fi

echo "done: s3://${BUCKET}"
