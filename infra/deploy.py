"""Builds, publishes and deploys the digital twin, identically on a developer machine and on CodeBuild.

  deploy {ec2,serverless}   everything end to end (what infra/deploy.ps1 / deploy-serverless.ps1 run)
  build {ec2,serverless}    build the package zip into --out-dir and print its path
  publish {ec2,serverless}  upload a built zip and write the stack's code parameters as a CloudFormation
                            template configuration file (for a pipeline's CloudFormation deploy action)
  publish-site              sync serverless/web to the site bucket and invalidate CloudFront
  waf                       deploy the WAF web ACL stack (us-east-1) and print its ARN

Run with the deploy dependency group, e.g.:  uv run --group deploy python infra/deploy.py deploy serverless
"""
import argparse
import getpass
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from dotenv import dotenv_values, load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFRA = os.path.join(ROOT, "infra")
WEB_DIR = os.path.join(ROOT, "serverless", "web")

# App stacks and data live in the EU; CloudFront web ACLs can only be created in us-east-1 (rules only, no data)
DEFAULT_REGION = "eu-north-1"
WAF_REGION = "us-east-1"
DEFAULT_SECRET_NAME = "digitaltwin/openai-api-key"
INLINE_TEMPLATE_LIMIT = 51_200  # bytes CloudFormation accepts as TemplateBody

EC2_APP_FILES = ["app.py", "database.py", "helpers.py", "rag_engine.py", "pyproject.toml", "uv.lock", "linkedin.pdf"]
# Fixed timestamps and permissions make the zip byte-for-byte reproducible: unchanged sources -> same key -> no redeploy
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


@dataclass(frozen=True)
class Target:
    stack_name: str
    template: str
    key_prefix: str      # must match the template's CodeKey AllowedPattern
    waf_post_rate: int   # WAF POSTs per IP per 5 minutes: Gradio sends 2 per chat message, the serverless UI 1


TARGETS = {
    "ec2": Target("digitaltwin", "digitaltwin.yaml", "digitaltwin/", 60),
    "serverless": Target("digitaltwin-serverless", "serverless.yaml", "lambda/", 30),
}


def artifact_bucket_name(account, region):
    return f"digitaltwin-artifacts-{account}-{region}"


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

# ----------------------------------------------------
# Build
# ----------------------------------------------------

def build_ec2(out_dir):
    """Zips the Gradio app reproducibly and names it after its content hash."""
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = os.path.join(out_dir, "app.zip.tmp")
    with zipfile.ZipFile(tmp_path, "w") as zf:
        for name in sorted(EC2_APP_FILES):
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(ROOT, name), "rb") as f:
                zf.writestr(info, f.read())
    path = os.path.join(out_dir, f"app-{_sha256_file(tmp_path)[:12]}.zip")
    os.replace(tmp_path, path)
    return path


def build_serverless(out_dir):
    """Embeds the resume if linkedin.pdf changed, then builds the arm64 Lambda zip (serverless/package.py)."""
    load_dotenv(os.path.join(ROOT, ".env"))  # OPENAI_API_KEY for embedding; CodeBuild provides it as an env var
    sys.path.insert(0, os.path.join(ROOT, "serverless"))
    import package  # serverless/package.py; imported lazily because it pulls in the app's heavier dependencies

    package.build_index()
    return package.build_zip(out_dir)


BUILDERS = {"ec2": build_ec2, "serverless": build_serverless}

# ----------------------------------------------------
# Artifacts and secrets
# ----------------------------------------------------

def ensure_artifact_bucket(s3, bucket, region):
    """Private, TLS-only, versioned bucket for deployment packages (stacks pin packages by object version)."""
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        print(f"Creating artifact bucket {bucket}")
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        })
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }],
        }))
    # Idempotent, and also fixes buckets created before versioning was required
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})


def publish_artifact(s3, bucket, key, path):
    """Uploads a package unless the (content-hashed) key already exists; returns its VersionId and Sha256.

    The EC2 instance verifies Sha256 before unpacking the package as root; both stacks pin VersionId.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        if head.get("VersionId") and head.get("Metadata", {}).get("sha256"):
            print(f"Package s3://{bucket}/{key} already uploaded (version {head['VersionId']})")
            return {"VersionId": head["VersionId"], "Sha256": head["Metadata"]["sha256"]}
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise
    sha256 = _sha256_file(path)
    with open(path, "rb") as f:
        response = s3.put_object(Bucket=bucket, Key=key, Body=f, Metadata={"sha256": sha256})
    if not response.get("VersionId"):
        raise RuntimeError(f"Bucket {bucket} returned no version id; is versioning enabled?")
    print(f"Uploaded s3://{bucket}/{key} (version {response['VersionId']})")
    return {"VersionId": response["VersionId"], "Sha256": sha256}


def ensure_openai_secret(secretsmanager, name):
    """Creates the secret holding OPENAI_API_KEY on first use (from the environment, .env, or a prompt)."""
    try:
        secretsmanager.describe_secret(SecretId=name)
        return
    except secretsmanager.exceptions.ResourceNotFoundException:
        pass
    key = (os.environ.get("OPENAI_API_KEY")
           or dotenv_values(os.path.join(ROOT, ".env")).get("OPENAI_API_KEY")
           or getpass.getpass("OPENAI_API_KEY: "))
    print(f"Creating secret {name}")
    secretsmanager.create_secret(Name=name, SecretString=key.strip())

# ----------------------------------------------------
# CloudFormation
# ----------------------------------------------------

NO_CHANGES_REASONS = ("didn't contain changes", "No updates are to be performed")


def stack_status(cfn, stack_name):
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except ClientError as e:
        if "does not exist" in e.response["Error"]["Message"]:
            return None
        raise


def stack_outputs(cfn, stack_name):
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _failure_summary(cfn, stack_name, limit=5):
    events = cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
    failed = [f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}"
              for e in events if e["ResourceStatus"].endswith("_FAILED")]
    return "; ".join(failed[:limit]) or "see the stack events in the CloudFormation console"


def deploy_stack(cfn, stack_name, template_path, parameters):
    """Creates or updates a stack through a change set and returns its outputs.

    Like `aws cloudformation deploy`: parameters not given keep their previous value on updates (template defaults
    on creation), and a template without changes is a no-op rather than an error.
    """
    with open(template_path, encoding="utf-8") as f:
        body = f.read()
    if len(body.encode("utf-8")) > INLINE_TEMPLATE_LIMIT:
        raise RuntimeError(f"{template_path} exceeds {INLINE_TEMPLATE_LIMIT} bytes; upload it to S3 and use TemplateURL")

    status = stack_status(cfn, stack_name)
    if status == "ROLLBACK_COMPLETE":
        raise RuntimeError(f"{stack_name} is in ROLLBACK_COMPLETE (its first creation failed); delete it and deploy again")
    creating = status in (None, "REVIEW_IN_PROGRESS")

    declared = [p["ParameterKey"] for p in cfn.get_template_summary(TemplateBody=body)["Parameters"]]
    unknown = sorted(set(parameters) - set(declared))
    if unknown:
        raise RuntimeError(f"{os.path.basename(template_path)} has no parameter(s) {', '.join(unknown)}")
    existing = set() if creating else {
        p["ParameterKey"] for p in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Parameters", [])
    }
    stack_params = []
    for key in declared:
        if key in parameters:
            stack_params.append({"ParameterKey": key, "ParameterValue": str(parameters[key])})
        elif key in existing:
            stack_params.append({"ParameterKey": key, "UsePreviousValue": True})

    change_set = f"deploy-{time.strftime('%Y%m%d-%H%M%S')}"
    print(f"{stack_name}: creating change set {change_set} ({'CREATE' if creating else 'UPDATE'})")
    cfn.create_change_set(
        StackName=stack_name, ChangeSetName=change_set, TemplateBody=body, Parameters=stack_params,
        Capabilities=["CAPABILITY_IAM"], ChangeSetType="CREATE" if creating else "UPDATE",
    )
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=stack_name, ChangeSetName=change_set, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except WaiterError:
        reason = cfn.describe_change_set(StackName=stack_name, ChangeSetName=change_set).get("StatusReason", "")
        if any(marker in reason for marker in NO_CHANGES_REASONS):
            cfn.delete_change_set(StackName=stack_name, ChangeSetName=change_set)
            print(f"{stack_name}: no changes")
            return stack_outputs(cfn, stack_name)
        raise RuntimeError(f"change set for {stack_name} failed: {reason}") from None

    cfn.execute_change_set(StackName=stack_name, ChangeSetName=change_set)
    print(f"{stack_name}: deploying (CloudFront changes can take 15+ minutes)...")
    waiter = cfn.get_waiter("stack_create_complete" if creating else "stack_update_complete")
    try:
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 240})
    except WaiterError:
        raise RuntimeError(f"{stack_name} deployment failed: {_failure_summary(cfn, stack_name)}") from None
    print(f"{stack_name}: deployed")
    return stack_outputs(cfn, stack_name)


def deploy_waf(session, stack_name, post_rate_limit):
    """The CloudFront web ACL must live in us-east-1, whatever region the app stacks use."""
    cfn = session.client("cloudformation", region_name=WAF_REGION)
    outputs = deploy_stack(cfn, stack_name, os.path.join(INFRA, "waf.yaml"), {"PostRateLimitPer5Min": post_rate_limit})
    return outputs["WebAclArn"]


def reserved_concurrency(lambda_client, wanted=5):
    """Global cap on simultaneous replies; 0 when the account quota is too low to reserve any.

    Lambda keeps 100 executions unreserved, so new accounts (quota 10) cannot reserve concurrency.
    """
    limit = lambda_client.get_account_settings()["AccountLimit"]["ConcurrentExecutions"]
    if limit - wanted < 100:
        print(f"WARNING: Lambda concurrency quota is {limit}, too low to reserve any; deploying without the global "
              "concurrency cap (WAF per-IP limits still apply). Request a quota increase to enable it.")
        return 0
    return wanted

# ----------------------------------------------------
# Static site (serverless)
# ----------------------------------------------------

def publish_site(session, stack_name):
    """Syncs serverless/web to the site bucket (upload changed, delete removed) and invalidates CloudFront."""
    outputs = stack_outputs(session.client("cloudformation"), stack_name)
    bucket, distribution_id = outputs["SiteBucketName"], outputs["DistributionId"]
    s3 = session.client("s3")

    remote = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = obj["ETag"].strip('"')

    local = {}
    for dirpath, _, filenames in os.walk(WEB_DIR):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            local[os.path.relpath(path, WEB_DIR).replace(os.sep, "/")] = path

    uploaded = 0
    for key, path in sorted(local.items()):
        with open(path, "rb") as f:
            data = f.read()
        # Single-part uploads with SSE-S3 have the content MD5 as ETag, so unchanged files are skipped
        if remote.get(key) == hashlib.md5(data, usedforsecurity=False).hexdigest():
            continue
        content_type = CONTENT_TYPES.get(os.path.splitext(key)[1].lower(), "application/octet-stream")
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        uploaded += 1

    stale = sorted(set(remote) - set(local))
    for start in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[start:start + 1000]], "Quiet": True})

    print(f"Site {bucket}: {uploaded} uploaded, {len(stale)} deleted, {len(local) - uploaded} unchanged")
    if uploaded or stale:
        session.client("cloudfront").create_invalidation(DistributionId=distribution_id, InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"deploy-{time.time_ns()}",
        })
        print(f"Invalidated CloudFront distribution {distribution_id}")

# ----------------------------------------------------
# End-to-end flows
# ----------------------------------------------------

def publish_code(session, target_name, zip_path):
    """Uploads a built package and returns the stack parameters that point at it."""
    target = TARGETS[target_name]
    account = session.client("sts").get_caller_identity()["Account"]
    s3 = session.client("s3")
    bucket = artifact_bucket_name(account, session.region_name)
    ensure_artifact_bucket(s3, bucket, session.region_name)
    key = target.key_prefix + os.path.basename(zip_path)
    artifact = publish_artifact(s3, bucket, key, zip_path)
    params = {"CodeBucket": bucket, "CodeKey": key, "CodeVersionId": artifact["VersionId"]}
    if target_name == "ec2":
        params["CodeSha256"] = artifact["Sha256"]
    return params


def deploy(session, target_name, stack_name, secret_name, waf=True, notification_email=None):
    target = TARGETS[target_name]
    ensure_openai_secret(session.client("secretsmanager"), secret_name)
    zip_path = BUILDERS[target_name](os.path.join(tempfile.gettempdir(), "digitaltwin-build"))
    params = publish_code(session, target_name, zip_path)
    params["OpenAIApiKeySecretName"] = secret_name
    params["WebAclArn"] = deploy_waf(session, f"{stack_name}-waf", target.waf_post_rate) if waf else ""
    if target_name == "serverless":
        params["ReservedConcurrency"] = reserved_concurrency(session.client("lambda"))
        if notification_email is not None:  # omitted -> keep the current subscription
            params["NotificationEmail"] = notification_email

    outputs = deploy_stack(session.client("cloudformation"), stack_name, os.path.join(INFRA, target.template), params)
    if target_name == "serverless":
        publish_site(session, stack_name)
    width = max(map(len, outputs), default=0)
    for key, value in sorted(outputs.items()):
        print(f"  {key:<{width}}  {value}")
    if notification_email:
        print(f"Confirm the SNS subscription email sent to {notification_email} to receive contact requests.")

# ----------------------------------------------------
# CLI
# ----------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_region(p):
        p.add_argument("--region", default=DEFAULT_REGION)

    p = sub.add_parser("deploy", help="build, publish and deploy end to end")
    p.add_argument("target", choices=TARGETS)
    p.add_argument("--stack-name")
    p.add_argument("--secret-name", default=DEFAULT_SECRET_NAME)
    p.add_argument("--no-waf", action="store_true", help="skip the WAF (saves ~$9/month, loses per-IP rate limits)")
    p.add_argument("--notification-email", help="serverless only: receives contact requests and alarms")
    add_region(p)

    p = sub.add_parser("build", help="build the package zip and print its path")
    p.add_argument("target", choices=TARGETS)
    p.add_argument("--out-dir", default=os.path.join(ROOT, "dist"))

    p = sub.add_parser("publish", help="upload a built zip and write its stack parameters")
    p.add_argument("target", choices=TARGETS)
    p.add_argument("--zip", required=True)
    p.add_argument("--params-out", required=True, help="CloudFormation template configuration file to write")
    add_region(p)

    p = sub.add_parser("publish-site", help="sync serverless/web and invalidate CloudFront")
    p.add_argument("--stack-name", default=TARGETS["serverless"].stack_name)
    add_region(p)

    p = sub.add_parser("waf", help="deploy the WAF stack in us-east-1 and print its ARN")
    p.add_argument("--stack-name", required=True)
    p.add_argument("--post-rate-limit", type=int, default=60)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(BUILDERS[args.target](args.out_dir))
            return 0
        session = boto3.Session(region_name=getattr(args, "region", DEFAULT_REGION))
        if args.command == "deploy":
            stack_name = args.stack_name or TARGETS[args.target].stack_name
            deploy(session, args.target, stack_name, args.secret_name, not args.no_waf, args.notification_email)
        elif args.command == "publish":
            params = publish_code(session, args.target, args.zip)
            with open(args.params_out, "w", encoding="utf-8") as f:
                json.dump({"Parameters": params}, f, indent=2)
            print(f"Wrote {args.params_out}")
        elif args.command == "publish-site":
            publish_site(session, args.stack_name)
        elif args.command == "waf":
            print(deploy_waf(session, args.stack_name, args.post_rate_limit))
        return 0
    except (BotoCoreError, ClientError, RuntimeError) as e:  # BotoCoreError: credentials, network, waiters
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
