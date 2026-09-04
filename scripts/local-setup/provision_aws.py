#!/usr/bin/env python3
"""Provision every AWS resource Biofarm needs, into any AWS account, in one pass.

Creates (or adopts, if already present) an S3 bucket for product images, a
CloudFront distribution fronting it via Origin Access Control, a least-privilege
IAM user for the backend, and a Cognito user pool wired for the hosted-UI login
flow - then writes the resulting values into both repos' .env files.

Every step is check-then-create, so re-running is the normal way to resume after
a partial failure. Resource names are derived from the account id, which makes
them globally unique without random suffixes and lets a re-run find what a
previous run created even if the state file was lost.

    export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...
    python provision_aws.py --region us-east-2 --admin-email you@example.com

Use --dry-run first to see the plan. Requires boto3, which ships with the
backend's requirements, so run it with the backend venv's python if your system
python doesn't have it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:  # pragma: no cover - environment guidance, not logic
    sys.exit(
        "boto3 is required.\n"
        "  Biofarm_Backend/.venv/Scripts/python.exe .claude/skills/local-setup/scripts/provision_aws.py ...\n"
        "or: pip install boto3"
    )


def find_workspace_root(start: Path) -> Path:
    """Find the directory holding both app repos.

    Located by searching upward rather than by a fixed depth, because this script
    runs both from a checkout of the knowledge-base repo and from a skill
    installed under a project's .claude/skills/ - different depths, same answer.
    Set BIOFARM_ROOT to override when the repos live somewhere unusual.
    """
    override = os.environ.get("BIOFARM_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / "Biofarm_Backend").is_dir() and (candidate / "Biofarm_Frontend").is_dir():
            return candidate
    return here


ROOT = find_workspace_root(Path(__file__).parent)
BACKEND_ENV = ROOT / "Biofarm_Backend" / ".env"
FRONTEND_ENV = ROOT / "Biofarm_Frontend" / ".env"
STATE_FILE = ROOT / ".aws-provision-state.json"

# CloudFront managed policy: Managed-CachingOptimized. A stable, AWS-owned id -
# using it avoids hand-rolling a cache policy that would need its own upkeep.
CACHE_POLICY_CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"

IMAGE_KEY_PREFIX = "products"

# Raised when short-lived credentials (ada, SSO, assume-role) lapse mid-run.
# Worth naming explicitly so the failure reads as "refresh and re-run" rather
# than as a bug in provisioning.
EXPIRED_CREDENTIAL_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "RequestExpired",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
}


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

class Log:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run

    def step(self, msg: str) -> None:
        print(f"\n=== {msg}")

    def created(self, msg: str) -> None:
        print(f"  + {msg}")

    def exists(self, msg: str) -> None:
        print(f"  . {msg} (already present)")

    def info(self, msg: str) -> None:
        print(f"    {msg}")

    def warn(self, msg: str) -> None:
        print(f"  ! {msg}")

    def plan(self, msg: str) -> None:
        print(f"  ~ would {msg}")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# .env writing
# --------------------------------------------------------------------------

def merge_env(path: Path, updates: dict[str, str], log: Log) -> None:
    """Set keys in a .env file without disturbing anything else in it.

    Existing keys are rewritten in place so comments, ordering, and unrelated
    values survive; new keys are appended under a dated header. Values the caller
    passes as None are skipped, which lets the caller leave a key alone when it
    has nothing authoritative to say about it.
    """
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append(f"# added by local-setup provisioner {datetime.now().strftime('%Y-%m-%d')}")
        out.extend(f"{k}={v}" for k, v in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    log.created(f"wrote {len(updates)} value(s) to {path.relative_to(ROOT)}")


def read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return None


# --------------------------------------------------------------------------
# resource steps
# --------------------------------------------------------------------------

def ensure_bucket(s3, bucket: str, region: str, origins: list[str], log: Log, dry: bool) -> None:
    log.step(f"S3 bucket {bucket}")
    try:
        s3.head_bucket(Bucket=bucket)
        log.exists("bucket")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "403"):
            raise
        if code == "403":
            raise SystemExit(
                f"Bucket name '{bucket}' exists in another AWS account. Pass --bucket-name to choose a different one."
            )
        if dry:
            log.plan(f"create bucket {bucket} in {region}")
        else:
            kwargs: dict[str, Any] = {"Bucket": bucket}
            # us-east-1 is the API's default and rejects an explicit constraint.
            if region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**kwargs)
            log.created(f"bucket {bucket}")

    if dry:
        log.plan("block public access and set CORS")
        return

    # Images are served through CloudFront only; the bucket itself stays private.
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,  # the OAC policy below is a bucket policy
            "RestrictPublicBuckets": False,
        },
    )
    log.created("public access block")

    # PUT is what the browser does with a presigned URL; ETag must be exposed for
    # the upload to be verifiable client-side.
    s3.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "PUT"],
                    "AllowedOrigins": origins,
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        },
    )
    log.created(f"CORS for {', '.join(origins)}")


def ensure_oac(cf, name: str, log: Log, dry: bool) -> str | None:
    log.step(f"CloudFront origin access control {name}")
    paginator = cf.get_paginator("list_origin_access_controls") if cf.can_paginate("list_origin_access_controls") else None
    items: list[dict] = []
    if paginator:
        for page in paginator.paginate():
            items.extend(page.get("OriginAccessControlList", {}).get("Items", []))
    else:
        items = cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", [])

    for item in items:
        if item.get("Name") == name:
            log.exists(f"OAC {item['Id']}")
            return item["Id"]

    if dry:
        log.plan(f"create OAC {name}")
        return None

    resp = cf.create_origin_access_control(
        OriginAccessControlConfig={
            "Name": name,
            "Description": "Biofarm product images",
            "SigningProtocol": "sigv4",
            "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3",
        }
    )
    oac_id = resp["OriginAccessControl"]["Id"]
    log.created(f"OAC {oac_id}")
    return oac_id


def find_distribution(cf, origin_domain: str) -> dict | None:
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for dist in page.get("DistributionList", {}).get("Items", []) or []:
            for origin in dist.get("Origins", {}).get("Items", []) or []:
                if origin.get("DomainName") == origin_domain:
                    return dist
    return None


def ensure_distribution(cf, bucket: str, region: str, oac_id: str | None, state: dict, log: Log, dry: bool) -> tuple[str | None, str | None]:
    origin_domain = f"{bucket}.s3.{region}.amazonaws.com"
    log.step(f"CloudFront distribution for {origin_domain}")

    existing = find_distribution(cf, origin_domain)
    if existing:
        log.exists(f"distribution {existing['Id']} -> {existing['DomainName']}")
        if existing.get("Status") != "Deployed":
            log.warn(f"status is {existing.get('Status')} - images 404 until it reaches Deployed (5-15 min)")
        return existing["Id"], existing["DomainName"]

    if dry:
        log.plan(f"create distribution fronting {origin_domain}")
        return None, None

    origin_id = f"s3-{bucket}"
    resp = cf.create_distribution(
        DistributionConfig={
            "CallerReference": f"biofarm-{int(time.time())}",
            "Comment": "Biofarm product images",
            "Enabled": True,
            "Origins": {
                "Quantity": 1,
                "Items": [
                    {
                        "Id": origin_id,
                        "DomainName": origin_domain,
                        "OriginAccessControlId": oac_id or "",
                        # Required even with OAC; empty string means "no legacy OAI".
                        "S3OriginConfig": {"OriginAccessIdentity": ""},
                    }
                ],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": origin_id,
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 2,
                    "Items": ["GET", "HEAD"],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "Compress": True,
                "CachePolicyId": CACHE_POLICY_CACHING_OPTIMIZED,
            },
            # North America + Europe only; cheapest tier that covers the use case.
            "PriceClass": "PriceClass_100",
        }
    )
    dist = resp["Distribution"]
    log.created(f"distribution {dist['Id']} -> {dist['DomainName']}")
    log.info("takes 5-15 minutes to reach Deployed; uploads work immediately, display does not")
    return dist["Id"], dist["DomainName"]


def ensure_bucket_policy(s3, bucket: str, account_id: str, dist_id: str | None, log: Log, dry: bool) -> None:
    log.step("S3 bucket policy for CloudFront")
    if not dist_id:
        log.warn("no distribution id yet - skipping (re-run after the distribution exists)")
        return
    if dry:
        log.plan("grant cloudfront.amazonaws.com s3:GetObject scoped to this distribution")
        return

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontServicePrincipal",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
                "Condition": {
                    "StringEquals": {
                        "AWS:SourceArn": f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"
                    }
                },
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    log.created("bucket policy scoped to the distribution")


def ensure_iam_user(iam, user_name: str, bucket: str, state: dict, log: Log, dry: bool) -> tuple[str | None, str | None]:
    """Create the backend's IAM user and return (access_key_id, secret) if newly minted.

    The secret is only ever returned by AWS at creation time, so it is written to
    .env by the caller and deliberately never stored in the state file.
    """
    log.step(f"IAM user {user_name}")
    try:
        iam.get_user(UserName=user_name)
        log.exists("user")
        created_user = False
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        if dry:
            log.plan(f"create IAM user {user_name} with S3 access to {bucket}/{IMAGE_KEY_PREFIX}/*")
            return None, None
        iam.create_user(UserName=user_name, Tags=[{"Key": "project", "Value": "biofarm"}])
        log.created(f"user {user_name}")
        created_user = True

    if dry:
        log.plan("attach least-privilege S3 policy and create an access key")
        return None, None

    # Scoped to the image prefix only - the backend never needs bucket-wide access.
    iam.put_user_policy(
        UserName=user_name,
        PolicyName="BiofarmS3ImageAccess",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "S3ImageAccess",
                        "Effect": "Allow",
                        "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                        "Resource": f"arn:aws:s3:::{bucket}/{IMAGE_KEY_PREFIX}/*",
                    }
                ],
            }
        ),
    )
    log.created("inline policy BiofarmS3ImageAccess")

    # Reuse the key already in .env when it still exists upstream; minting a new
    # one on every run would silently exhaust the 2-key limit.
    existing_id = read_env_value(BACKEND_ENV, "AWS_ACCESS_KEY_ID")
    live_keys = iam.list_access_keys(UserName=user_name).get("AccessKeyMetadata", [])
    live_ids = {k["AccessKeyId"] for k in live_keys}

    if existing_id and existing_id in live_ids and not created_user:
        log.exists(f"access key {existing_id} (already in .env)")
        return None, None

    if len(live_keys) >= 2:
        oldest = min(live_keys, key=lambda k: k["CreateDate"])
        iam.delete_access_key(UserName=user_name, AccessKeyId=oldest["AccessKeyId"])
        log.warn(f"deleted oldest access key {oldest['AccessKeyId']} to stay under the 2-key limit")

    key = iam.create_access_key(UserName=user_name)["AccessKey"]
    log.created(f"access key {key['AccessKeyId']} (secret goes to .env only)")
    return key["AccessKeyId"], key["SecretAccessKey"]


def find_user_pool(cognito, name: str) -> str | None:
    paginator = cognito.get_paginator("list_user_pools")
    for page in paginator.paginate(MaxResults=60):
        for pool in page.get("UserPools", []):
            if pool.get("Name") == name:
                return pool["Id"]
    return None


def ensure_user_pool(cognito, name: str, log: Log, dry: bool) -> str | None:
    log.step(f"Cognito user pool {name}")
    pool_id = find_user_pool(cognito, name)
    if pool_id:
        log.exists(f"pool {pool_id}")
        return pool_id
    if dry:
        log.plan(f"create user pool {name} (email sign-in, self-registration on)")
        return None

    resp = cognito.create_user_pool(
        PoolName=name,
        # Email as the username is what the frontend assumes when it maps a
        # Cognito identity onto its User type.
        UsernameAttributes=["email"],
        AutoVerifiedAttributes=["email"],
        # Self-registration on: without it only console-created users can sign in.
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": False},
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 8,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": False,
            }
        },
        Schema=[{"Name": "email", "AttributeDataType": "String", "Required": True, "Mutable": True}],
        AccountRecoverySetting={"RecoveryMechanisms": [{"Priority": 1, "Name": "verified_email"}]},
    )
    pool_id = resp["UserPool"]["Id"]
    log.created(f"pool {pool_id}")
    return pool_id


def ensure_app_client(cognito, pool_id: str | None, client_name: str, callbacks: list[str], log: Log, dry: bool) -> str | None:
    log.step(f"Cognito app client {client_name}")
    if not pool_id:
        log.warn("no pool id - skipping")
        return None

    paginator = cognito.get_paginator("list_user_pool_clients")
    for page in paginator.paginate(UserPoolId=pool_id, MaxResults=60):
        for c in page.get("UserPoolClients", []):
            if c.get("ClientName") == client_name:
                log.exists(f"client {c['ClientId']}")
                return c["ClientId"]

    if dry:
        log.plan(f"create public app client with callbacks {callbacks}")
        return None

    resp = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=client_name,
        # No secret: Amplify's browser PKCE flow cannot use one, and a client with
        # a secret fails the login silently rather than with a clear error.
        GenerateSecret=False,
        SupportedIdentityProviders=["COGNITO"],
        CallbackURLs=callbacks,
        LogoutURLs=callbacks,
        AllowedOAuthFlows=["code"],
        AllowedOAuthScopes=["openid", "email", "profile"],
        # Must be true before the callback/scope/flow settings above take effect.
        AllowedOAuthFlowsUserPoolClient=True,
        ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"],
        PreventUserExistenceErrors="ENABLED",
    )
    client_id = resp["UserPoolClient"]["ClientId"]
    log.created(f"client {client_id}")
    return client_id


def ensure_domain(cognito, pool_id: str | None, prefix: str, region: str, log: Log, dry: bool) -> str | None:
    log.step(f"Cognito hosted-UI domain {prefix}")
    if not pool_id:
        log.warn("no pool id - skipping")
        return None

    desc = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    if desc.get("Domain"):
        existing = f"{desc['Domain']}.auth.{region}.amazoncognito.com"
        log.exists(existing)
        return existing

    if dry:
        log.plan(f"create domain {prefix}")
        return None

    try:
        cognito.create_user_pool_domain(Domain=prefix, UserPoolId=pool_id)
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidParameterException":
            raise SystemExit(
                f"Cognito domain prefix '{prefix}' is taken (prefixes are globally unique). "
                "Re-run with --domain-prefix <something-else>."
            )
        raise
    domain = f"{prefix}.auth.{region}.amazoncognito.com"
    log.created(domain)
    return domain


def ensure_admin_group(cognito, pool_id: str | None, log: Log, dry: bool) -> None:
    log.step("Cognito Admin group")
    if not pool_id:
        log.warn("no pool id - skipping")
        return
    if dry:
        log.plan("create group 'Admin'")
        return
    try:
        cognito.create_group(
            GroupName="Admin",
            UserPoolId=pool_id,
            Description="Grants access to /admin routes. Name is case-sensitive.",
        )
        log.created("group Admin")
    except ClientError as e:
        if e.response["Error"]["Code"] != "GroupExistsException":
            raise
        log.exists("group Admin")


def ensure_admin_user(cognito, pool_id: str | None, email: str, password: str, log: Log, dry: bool) -> None:
    log.step(f"Cognito admin user {email}")
    if not pool_id:
        log.warn("no pool id - skipping")
        return
    if dry:
        log.plan(f"create {email}, set a permanent password, add to Admin")
        return

    try:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            # Suppressed because the password is set permanently on the next call;
            # an invite email would only offer a temporary one.
            MessageAction="SUPPRESS",
        )
        log.created(f"user {email}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "UsernameExistsException":
            raise
        log.exists(f"user {email}")

    cognito.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName="Admin")
    log.created("password set and added to Admin group")


# --------------------------------------------------------------------------
# teardown
# --------------------------------------------------------------------------

def teardown(session, state: dict, log: Log) -> None:
    """Delete what this script created. CloudFront is the slow part."""
    region = state.get("region")
    bucket = state.get("bucket")
    dist_id = state.get("distribution_id")
    pool_id = state.get("user_pool_id")
    user_name = state.get("iam_user")

    if dist_id:
        cf = session.client("cloudfront")
        log.step(f"disabling and deleting distribution {dist_id}")
        try:
            cfg = cf.get_distribution_config(Id=dist_id)
            etag, config = cfg["ETag"], cfg["DistributionConfig"]
            if config["Enabled"]:
                config["Enabled"] = False
                etag = cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=config)["ETag"]
                log.info("disabled; waiting for it to finish deploying (this takes several minutes)")
                cf.get_waiter("distribution_deployed").wait(Id=dist_id)
                etag = cf.get_distribution_config(Id=dist_id)["ETag"]
            cf.delete_distribution(Id=dist_id, IfMatch=etag)
            log.created("distribution deleted")
        except ClientError as e:
            log.warn(f"could not delete distribution: {e}")

    if bucket:
        s3 = session.client("s3")
        log.step(f"emptying and deleting bucket {bucket}")
        try:
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=bucket):
                objects = [
                    {"Key": o["Key"], "VersionId": o["VersionId"]}
                    for key in ("Versions", "DeleteMarkers")
                    for o in page.get(key, [])
                ]
                if objects:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
            s3.delete_bucket(Bucket=bucket)
            log.created("bucket deleted")
        except ClientError as e:
            log.warn(f"could not delete bucket: {e}")

    if user_name:
        iam = session.client("iam")
        log.step(f"deleting IAM user {user_name}")
        try:
            for k in iam.list_access_keys(UserName=user_name).get("AccessKeyMetadata", []):
                iam.delete_access_key(UserName=user_name, AccessKeyId=k["AccessKeyId"])
            for p in iam.list_user_policies(UserName=user_name).get("PolicyNames", []):
                iam.delete_user_policy(UserName=user_name, PolicyName=p)
            iam.delete_user(UserName=user_name)
            log.created("IAM user deleted")
        except ClientError as e:
            log.warn(f"could not delete IAM user: {e}")

    if pool_id:
        cognito = session.client("cognito-idp", region_name=region)
        log.step(f"deleting user pool {pool_id}")
        try:
            desc = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
            if desc.get("Domain"):
                cognito.delete_user_pool_domain(Domain=desc["Domain"], UserPoolId=pool_id)
            cognito.delete_user_pool(UserPoolId=pool_id)
            log.created("user pool deleted")
        except ClientError as e:
            log.warn(f"could not delete user pool: {e}")

    STATE_FILE.unlink(missing_ok=True)
    print("\nTeardown finished. The .env files still hold the old values - rerun provisioning to replace them.\n")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.lower()).strip("-")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--region", default="us-east-2", help="AWS region for the bucket and user pool (default us-east-2)")
    ap.add_argument("--project", default="biofarm", help="name prefix for created resources (default biofarm)")
    ap.add_argument("--profile", help="AWS profile to use instead of environment credentials")
    ap.add_argument("--bucket-name", help="override the derived bucket name")
    ap.add_argument("--domain-prefix", help="override the derived Cognito domain prefix (globally unique)")
    ap.add_argument("--frontend-origin", default="http://localhost:5174", help="origin allowed in S3 CORS (default http://localhost:5174)")
    ap.add_argument("--callback-url", action="append", help="Cognito callback URL; repeatable (default <frontend-origin>/auth/callback)")
    ap.add_argument("--admin-email", help="also create this user and put it in the Admin group")
    ap.add_argument("--admin-password", default="BiofarmAdmin123", help="password for --admin-email (default BiofarmAdmin123)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without creating anything")
    ap.add_argument("--teardown", action="store_true", help="delete resources recorded in the state file")
    args = ap.parse_args()

    log = Log(args.dry_run)
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()

    try:
        identity = session.client("sts").get_caller_identity()
    except (NoCredentialsError, ClientError) as e:
        sys.exit(
            f"Could not authenticate to AWS: {e}\n\n"
            "boto3 reads the same credential chain the AWS CLI does, so whatever\n"
            "vends your credentials (ada, aws sso login, static keys) works here too:\n"
            "  - env vars:      export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...\n"
            "  - named profile: --profile <name>, or export AWS_PROFILE=<name>\n"
            "If credentials were vended a while ago, they may simply have expired -\n"
            "refresh them and run this again."
        )

    account_id = identity["Account"]
    state = load_state()

    if args.teardown:
        if not state:
            sys.exit(f"No state file at {STATE_FILE} - nothing recorded to tear down.")
        print(f"About to delete Biofarm resources in account {account_id}:")
        print(json.dumps({k: v for k, v in state.items() if k != "updated_at"}, indent=2))
        if input("\nType the account id to confirm: ").strip() != account_id:
            sys.exit("Aborted.")
        teardown(session, state, log)
        return 0

    region = args.region
    # Account-derived names are globally unique and deterministic, so a re-run
    # finds the same resources even without the state file.
    bucket = args.bucket_name or f"{slug(args.project)}-images-{account_id}"
    domain_prefix = args.domain_prefix or f"{slug(args.project)}-{account_id}"
    pool_name = f"{args.project}-users"
    client_name = f"{args.project}-web"
    iam_user = f"{args.project}-backend"
    callbacks = args.callback_url or [f"{args.frontend_origin.rstrip('/')}/auth/callback"]

    print(f"Account {account_id}  region {region}" + ("  [DRY RUN]" if args.dry_run else ""))
    print(f"Caller  {identity['Arn']}")

    s3 = session.client("s3", region_name=region)
    cf = session.client("cloudfront")
    iam = session.client("iam")
    cognito = session.client("cognito-idp", region_name=region)

    try:
        ensure_bucket(s3, bucket, region, [args.frontend_origin], log, args.dry_run)
        oac_id = ensure_oac(cf, f"{args.project}-images-oac", log, args.dry_run)
        dist_id, dist_domain = ensure_distribution(cf, bucket, region, oac_id, state, log, args.dry_run)
        ensure_bucket_policy(s3, bucket, account_id, dist_id, log, args.dry_run)
        access_key_id, secret_key = ensure_iam_user(iam, iam_user, bucket, state, log, args.dry_run)

        pool_id = ensure_user_pool(cognito, pool_name, log, args.dry_run)
        client_id = ensure_app_client(cognito, pool_id, client_name, callbacks, log, args.dry_run)
        domain = ensure_domain(cognito, pool_id, domain_prefix, region, log, args.dry_run)
        ensure_admin_group(cognito, pool_id, log, args.dry_run)
        if args.admin_email:
            ensure_admin_user(cognito, pool_id, args.admin_email, args.admin_password, log, args.dry_run)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in EXPIRED_CREDENTIAL_CODES:
            raise
        # Vended credentials are short-lived, and a full run can outlast them.
        # Every step is check-then-create, so refreshing and re-running resumes
        # from here rather than duplicating what already succeeded.
        sys.exit(
            f"\nAWS credentials expired partway through ({code}).\n"
            "Refresh them and run the same command again - each step checks before\n"
            "it creates, so the run resumes rather than starting over."
        )

    if args.dry_run:
        print("\nDry run complete - nothing was created. Re-run without --dry-run to apply.\n")
        return 0

    state.update(
        {
            "account_id": account_id,
            "region": region,
            "bucket": bucket,
            "oac_id": oac_id,
            "distribution_id": dist_id,
            "distribution_domain": dist_domain,
            "iam_user": iam_user,
            "access_key_id": access_key_id or state.get("access_key_id"),
            "user_pool_id": pool_id,
            "app_client_id": client_id,
            "cognito_domain": domain,
        }
    )
    save_state(state)

    log.step("writing .env files")
    cloudfront_url = f"https://{dist_domain}" if dist_domain else None
    merge_env(
        BACKEND_ENV,
        {
            "DATABASE_URL": read_env_value(BACKEND_ENV, "DATABASE_URL")
            or "postgresql+psycopg://biofarm:biofarm@localhost:5432/oasis",
            "AUTH_BYPASS": read_env_value(BACKEND_ENV, "AUTH_BYPASS") or "true",
            "STRIPE_BYPASS": read_env_value(BACKEND_ENV, "STRIPE_BYPASS") or "true",
            "COGNITO_REGION": region,
            "COGNITO_USER_POOL_ID": pool_id,
            "AWS_REGION": region,
            "S3_BUCKET_NAME": bucket,
            "CLOUDFRONT_URL": cloudfront_url,
            "AWS_ACCESS_KEY_ID": access_key_id,
            "AWS_SECRET_ACCESS_KEY": secret_key,
        },
        log,
    )
    merge_env(
        FRONTEND_ENV,
        {
            "VITE_API_BASE_URL": read_env_value(FRONTEND_ENV, "VITE_API_BASE_URL")
            or "http://127.0.0.1:8000/api/v1",
            "VITE_COGNITO_USER_POOL_ID": pool_id,
            "VITE_COGNITO_USER_POOL_CLIENT_ID": client_id,
            "VITE_COGNITO_DOMAIN": domain,
            "VITE_COGNITO_REDIRECT_SIGN_IN": callbacks[0],
            "VITE_COGNITO_REDIRECT_SIGN_OUT": callbacks[0],
            "VITE_STRIPE_BYPASS": read_env_value(FRONTEND_ENV, "VITE_STRIPE_BYPASS") or "true",
        },
        log,
    )

    print(f"\nState written to {STATE_FILE.name}. Summary:\n")
    print(f"  bucket        {bucket}")
    print(f"  cloudfront    {cloudfront_url}")
    print(f"  user pool     {pool_id}")
    print(f"  app client    {client_id}")
    print(f"  hosted UI     https://{domain}" if domain else "  hosted UI     (not created)")
    if args.admin_email:
        print(f"  admin login   {args.admin_email} / {args.admin_password}")
    if secret_key:
        print("\n  A new IAM secret key was written to Biofarm_Backend/.env. AWS will not show it again.")
    print("\nCloudFront needs 5-15 minutes before images render. Everything else is usable now.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
