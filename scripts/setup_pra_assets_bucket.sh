#!/usr/bin/env bash
# Create the PRA-assets S3 bucket and its scoped IAM principals.
#
# Bucket: general purpose, account-regional namespace (the name can never be
# re-registered by another account, even after deletion), private, SSE-S3.
# Uploads asking for any other encryption (SSE-KMS, DSSE-KMS; SSE-C is blocked
# at the bucket) are denied: anonymous readers couldn't decrypt them if the
# bucket is ever opened.
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
#   AWS_PROFILE=sm-alpr-admin scripts/setup_pra_assets_bucket.sh --check
#   AWS_PROFILE=sm-alpr-admin MINT_KEYS=1 scripts/setup_pra_assets_bucket.sh
#   scripts/setup_pra_assets_bucket.sh --print-policies   # no AWS calls
#
# --check reads everything and writes nothing. Exit status: 0 everything
# matches (or was set), 2 bad arguments or name, 3 something
# differs from the defaults and was left as is (with --check: anything that
# a run would set, create or leave different).
# Any other non-zero status (1, or aws-cli's 252-255) means an AWS call failed.
#
# MINT_KEYS=1 creates one access key per principal (skipped if it already has
# one) and writes it straight into local profiles <prefix>-writer and
# <prefix>-reader in ~/.aws/credentials. The secret is never printed or put
# on a command line, and a key that can't be saved is deleted from IAM.
#
# PRA_S3_REGION / PRA_S3_PREFIX override the bucket's region and name prefix.
set -euo pipefail

MODE=apply
case "${1:-}" in
  "") ;;
  --check) MODE=check ;;
  --print-policies) MODE=print ;;
  -h | --help) sed -n '2,/^set -euo pipefail/{/^set -euo/d;s/^# \{0,1\}//;p;}' "$0"; exit 0 ;;
  *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac
if (( $# > 1 )); then echo "one argument at most (see --help)" >&2; exit 2; fi

# Same env overrides and defaults as scripts/pra_s3_upload.py (a test keeps them in sync).
REGION="${PRA_S3_REGION:-us-west-2}"
PREFIX="${PRA_S3_PREFIX:-sm-alpr-pra}"   # <= 37 chars; the -<acct>-<region>-an suffix takes the rest
LOCK_MODE="${LOCK_MODE:-GOVERNANCE}"   # GOVERNANCE: admin can bypass. COMPLIANCE: nobody can, incl. root
LOCK_YEARS="${LOCK_YEARS:-10}"
WRITER="${PREFIX}-writer"
READER="${PREFIX}-reader"

if [[ "$MODE" == print ]]; then
  ACCOUNT_ID="${PRA_S3_ACCOUNT_ID:-<account-id>}"  # printing needs no AWS calls
else
  # Always the signed-in account's: a stray ACCOUNT_ID in the environment must
  # not point this at a different bucket name from the uploader's.
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
fi
BUCKET="${PREFIX}-${ACCOUNT_ID}-${REGION}-an"
if [[ "$MODE" != print ]] && (( ${#BUCKET} > 63 )); then
  echo "bucket name ${BUCKET} is ${#BUCKET} characters; S3 allows 63 (shorten PRA_S3_PREFIX)" >&2
  exit 2
fi
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
      "Sid": "DenyEncryptionOtherThanSSES3",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "${ARN}/*",
      "Condition": {
        "Null": {"s3:x-amz-server-side-encryption": "false"},
        "StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}
      }
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

# Print the policies without touching AWS.
if [[ "$MODE" == print ]]; then
  echo "# bucket: ${BUCKET}"; bucket_policy; writer_policy; reader_policy
  exit 0
fi

echo "account ${ACCOUNT_ID}, bucket ${BUCKET}"

ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT

json_same() {  # <a> <b>: exit 0 if the two JSON documents are equal, key order aside
  python3 - "$1" "$2" <<'EOF'
import json, sys
sys.exit(0 if json.loads(sys.argv[1]) == json.loads(sys.argv[2]) else 1)
EOF
}

# Print a setting's current JSON, or nothing when AWS answers with the given
# "not configured" error code. Any other failure (expired credentials,
# throttling, access denied) stops the script: mistaking it for "unset" would
# overwrite a deliberate setting with the default.
read_setting() {  # <not-configured error code> <command...>
  local code=$1 out
  shift
  if out=$("$@" 2>"$ERR"); then
    printf '%s' "$out"
  elif ! grep -qF "($code)" "$ERR"; then
    echo "couldn't read current setting ($*):" >&2
    cat "$ERR" >&2
    return 1
  fi
}

# Apply a setting only when it's unset. One that differs from this script's
# default in any way is reported and left alone unless REAPPLY=1; "ok" means
# exactly equal, so an added condition or statement never passes as ok.
DIFFERS=0   # settings left different from the defaults
PENDING=0   # --check: changes a run would make

apply_setting() {  # <name> <current json, empty if unset> <desired json> <command...>
  local name=$1 current=$2 desired=$3
  shift 3
  if [[ -z "$current" || "$current" == "null" ]]; then
    if [[ "$MODE" == check ]]; then
      PENDING=$((PENDING + 1))
      echo "  $name: unset; a run would set it"
    else
      "$@" >/dev/null
      echo "  $name: set"
    fi
  elif json_same "$desired" "$current"; then
    echo "  $name: ok"
  elif [[ "${REAPPLY:-0}" == "1" && "$MODE" != check ]]; then
    "$@" >/dev/null
    echo "  $name: differed; re-applied (REAPPLY=1)"
  else
    DIFFERS=$((DIFFERS + 1))
    echo "  $name: differs from this script's default; left as is (REAPPLY=1 overwrites)" >&2
    echo "    current: $current" >&2
    echo "    default: $desired" >&2
  fi
}

s3get() {  # <not-configured code> <get-command> <query>
  read_setting "$1" aws s3api "$2" --bucket "$BUCKET" --region "$REGION" --query "$3" --output json
}

# --- bucket ---------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>"$ERR"; then
  echo "bucket exists"
elif grep -qF "(404)" "$ERR" && [[ "$MODE" == check ]]; then
  echo "bucket doesn't exist; a run would create it"
  exit 3
elif grep -qF "(404)" "$ERR"; then
  location=()
  [[ "$REGION" != "us-east-1" ]] && location=(--create-bucket-configuration "LocationConstraint=${REGION}")
  # Object Lock at creation also turns on versioning (required by Object Lock).
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --bucket-namespace account-regional \
    --object-lock-enabled-for-bucket \
    ${location[@]+"${location[@]}"} >/dev/null
  echo "created bucket"
else
  cat "$ERR" >&2
  exit 1
fi

# Each read is its own assignment so that, under set -e, a failed read stops
# the script before anything is written.
pab='{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}'
cur=$(s3get NoSuchPublicAccessBlockConfiguration get-public-access-block PublicAccessBlockConfiguration)
apply_setting "public access block" "$cur" "$pab" \
  aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
  --public-access-block-configuration "$pab"

ownership='{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}'
cur=$(s3get OwnershipControlsNotFoundError get-bucket-ownership-controls OwnershipControls)
apply_setting "object ownership" "$cur" "$ownership" \
  aws s3api put-bucket-ownership-controls --bucket "$BUCKET" --region "$REGION" \
  --ownership-controls "$ownership"

# SSE-S3, not KMS: anonymous/public reads can't decrypt SSE-KMS objects. SSE-C
# (customer-held keys) is blocked, as AWS now does by default for new buckets:
# an object only its uploader's key can decrypt has no place in a shared archive.
encryption='{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false,"BlockedEncryptionTypes":{"EncryptionType":["SSE-C"]}}]}'
cur=$(s3get ServerSideEncryptionConfigurationNotFoundError get-bucket-encryption ServerSideEncryptionConfiguration)
apply_setting "encryption" "$cur" "$encryption" \
  aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
  --server-side-encryption-configuration "$encryption"

retention="{\"DefaultRetention\":{\"Mode\":\"${LOCK_MODE}\",\"Years\":${LOCK_YEARS}}}"
cur=$(s3get ObjectLockConfigurationNotFoundError get-object-lock-configuration ObjectLockConfiguration.Rule)
apply_setting "object lock default retention" "$cur" "$retention" \
  aws s3api put-object-lock-configuration --bucket "$BUCKET" --region "$REGION" \
  --object-lock-configuration "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":${retention}}"

# The writer can't list, so it can't find or clean up its own failed multipart
# uploads; expire them instead of paying for orphaned parts.
lifecycle='[{"ID":"abort-incomplete-mpu","Status":"Enabled","Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]'
cur=$(s3get NoSuchLifecycleConfiguration get-bucket-lifecycle-configuration Rules)
apply_setting "lifecycle rules" "$cur" "$lifecycle" \
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
  --lifecycle-configuration "{\"Rules\":${lifecycle}}"

cur=$(read_setting NoSuchBucketPolicy \
  aws s3api get-bucket-policy --bucket "$BUCKET" --region "$REGION" --query Policy --output text)
apply_setting "bucket policy" "$cur" "$(bucket_policy)" \
  aws s3api put-bucket-policy --bucket "$BUCKET" --region "$REGION" --policy "$(bucket_policy)"
echo "bucket $([[ "$MODE" == check ]] && echo checked || echo configured)"

# --- principals -------------------------------------------------------------
ensure_user() {  # <user> <policy-json>
  local cur
  if aws iam get-user --user-name "$1" >/dev/null 2>"$ERR"; then
    :
  elif grep -qF "(NoSuchEntity)" "$ERR" && [[ "$MODE" == check ]]; then
    PENDING=$((PENDING + 1))
    echo "  $1: doesn't exist; a run would create it"
  elif grep -qF "(NoSuchEntity)" "$ERR"; then
    aws iam create-user --user-name "$1" >/dev/null
    echo "created user $1"
  else
    cat "$ERR" >&2
    return 1
  fi
  cur=$(read_setting NoSuchEntity aws iam get-user-policy --user-name "$1" \
    --policy-name "${PREFIX}-access" --query PolicyDocument --output json)
  apply_setting "$1 policy" "$cur" "$2" \
    aws iam put-user-policy --user-name "$1" --policy-name "${PREFIX}-access" --policy-document "$2"
  if aws iam get-user --user-name "$1" >/dev/null 2>&1; then
    check_no_other_grants "$1"
  fi
}

# The inline policy above is meant to be the user's only permission. Anything
# else (a managed policy, another inline policy, a group) could widen it, so
# report it as a difference; this script never removes grants it didn't make.
check_no_other_grants() {  # <user>
  local managed inline groups
  managed=$(aws iam list-attached-user-policies --user-name "$1" \
    --query 'AttachedPolicies[].PolicyArn' --output text)
  # Read first, filter after: a failed read must stop the script (set -e), not
  # vanish into the filter's "|| true" and look like "no other policies".
  inline=$(aws iam list-user-policies --user-name "$1" --query 'PolicyNames' --output text)
  inline=$(tr '\t' '\n' <<<"$inline" | grep -vxF -e "${PREFIX}-access" -e "" || true)
  groups=$(aws iam list-groups-for-user --user-name "$1" --query 'Groups[].GroupName' --output text)
  if [[ -n "$managed$inline$groups" ]]; then
    DIFFERS=$((DIFFERS + 1))
    echo "  $1: has permissions beyond its ${PREFIX}-access policy; left as is" >&2
    if [[ -n "$managed" ]]; then echo "    managed policies: $managed" >&2; fi
    if [[ -n "$inline" ]]; then echo "    other inline policies: $(tr '\n' ' ' <<<"$inline")" >&2; fi
    if [[ -n "$groups" ]]; then echo "    groups: $groups" >&2; fi
  else
    echo "  $1: no other permissions"
  fi
}

ensure_user "$WRITER" "$(writer_policy)"
ensure_user "$READER" "$(reader_policy)"
echo "principals $([[ "$MODE" == check ]] && echo checked || echo configured)"

mint_key() {  # <user>: new key -> local profile of the same name
  local keys local_id
  keys=$(aws iam list-access-keys --user-name "$1" --query 'AccessKeyMetadata[].AccessKeyId' --output text)
  if [[ -n "$keys" ]]; then
    local_id=$(aws configure get aws_access_key_id --profile "$1" 2>/dev/null || true)
    # --output text separates IDs with tabs; match whole lines, not substrings.
    if [[ -n "$local_id" ]] && tr '\t' '\n' <<<"$keys" | grep -qxF "$local_id"; then
      echo "$1: local profile already holds its key"
    else
      DIFFERS=$((DIFFERS + 1))  # asked for a key and none was provided: not a success
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

if [[ "${MINT_KEYS:-0}" == "1" && "$MODE" == check ]]; then
  echo "MINT_KEYS ignored with --check"
elif [[ "${MINT_KEYS:-0}" == "1" && "$(aws configure get cli_history 2>/dev/null || true)" == "enabled" ]]; then
  # The CLI would record create-access-key's response, secret included, in ~/.aws/cli/history.
  echo "cli_history is enabled and would record the new secrets; disable it" \
    "(aws configure set cli_history disabled) and re-run" >&2
  exit 1
elif [[ "${MINT_KEYS:-0}" == "1" ]]; then
  mint_key "$WRITER"
  mint_key "$READER"
fi

if (( DIFFERS || PENDING )); then
  echo "s3://${BUCKET}: ${DIFFERS} setting(s) differ from the defaults, ${PENDING} change(s) pending (see above)" >&2
  exit 3
fi
echo "done: s3://${BUCKET}"
