"""Builds, publishes and deploys the digital twin, identically on a developer machine and on CodeBuild.

  set-openai-key            store or rotate the OpenAI API key in Secrets Manager (hidden prompt, verified first)
  deploy {ec2,serverless}   everything end to end (what infra/deploy.ps1 / deploy-serverless.ps1 run)
  build {ec2,serverless}    build the package zip into --out-dir and print its path
  publish {ec2,serverless}  upload a built zip and write the stack's code parameters as a CloudFormation
                            template configuration file (for a pipeline's CloudFormation deploy action)
  publish-site              sync serverless/web to the site bucket and invalidate CloudFront
  waf                       deploy the WAF web ACL stack (us-east-1) and print its ARN
  pipeline                  bootstrap or update the CI/CD pipeline stack (infra/pipeline.yaml) for the serverless app

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

PIPELINE_STACK = "digitaltwin-pipeline"
CONNECTION_NAME = "digitaltwin-github"


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


def build_serverless(out_dir, secret_name=DEFAULT_SECRET_NAME, region=DEFAULT_REGION):
    """Embeds the resume if linkedin.pdf changed, then builds the arm64 Lambda zip (serverless/package.py).

    Embedding needs the OpenAI key: OPENAI_API_KEY when set (CodeBuild maps the secret to it), otherwise it is read
    from the Secrets Manager secret, so no local .env file is needed. Unchanged resumes need no key at all.
    """
    sys.path.insert(0, os.path.join(ROOT, "serverless"))
    import package  # serverless/package.py; imported lazily because it pulls in the app's heavier dependencies

    if package.index_is_stale() and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = read_openai_key(boto3.client("secretsmanager", region_name=region), secret_name)
    package.build_index()
    return package.build_zip(out_dir)


def build(target_name, out_dir, secret_name=DEFAULT_SECRET_NAME, region=DEFAULT_REGION):
    if target_name == "ec2":
        return build_ec2(out_dir)  # the instance embeds the resume itself at startup
    return build_serverless(out_dir, secret_name, region)

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


# The Secrets Manager secret is the only place the OpenAI key is stored: both apps read it at runtime and the serverless
# build reads it to embed the resume. set-openai-key creates it and rotates it.

def read_openai_key(secretsmanager, name):
    try:
        return secretsmanager.get_secret_value(SecretId=name)["SecretString"]
    except secretsmanager.exceptions.ResourceNotFoundException:
        raise RuntimeError(f"Secret {name} not found; store the key first: "
                           "uv run --group deploy python infra/deploy.py set-openai-key") from None


def prompt_openai_key():
    """Reads the key without echoing it, so it never lands in shell history, a command line or a file."""
    if sys.stdin.isatty():
        return getpass.getpass("OpenAI API key (input hidden): ")
    return sys.stdin.readline()  # piped input, for automation


def check_key_format(key):
    if not key.startswith("sk-"):
        raise RuntimeError("that doesn't look like an OpenAI API key (they start with 'sk-'); nothing was stored")


def verify_openai_key(key):
    """One tiny chat call and one embeddings call: proves the key works and has the two permissions the app uses."""
    check_key_format(key)
    import openai  # app dependency; imported here so the other commands don't need it
    sys.path.insert(0, ROOT)
    from helpers import MODEL
    sys.path.insert(0, os.path.join(ROOT, "serverless"))
    from retriever import EMBEDDING_MODEL

    client = openai.OpenAI(api_key=key, max_retries=1)
    try:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "ping"}], max_completion_tokens=16)
        client.embeddings.create(model=EMBEDDING_MODEL, input="ping")
    except openai.AuthenticationError:
        raise RuntimeError("OpenAI rejected this key (mistyped, revoked or expired?); nothing was stored") from None
    except openai.PermissionDeniedError as e:
        raise RuntimeError("the key lacks a permission the app needs (Chat completions and Embeddings must be "
                           f"Write): {e.message}; nothing was stored") from None
    except openai.NotFoundError as e:
        raise RuntimeError(f"the key's project cannot use a model the app needs: {e.message}; nothing was stored") from None


def set_openai_key(secretsmanager, name, key):
    """Creates the secret, or stores a new version of it when rotating; the apps pick it up without a redeploy."""
    key = key.strip()
    check_key_format(key)
    try:
        secretsmanager.put_secret_value(SecretId=name, SecretString=key)
        print(f"Stored a new version of {name}")
    except secretsmanager.exceptions.ResourceNotFoundException:
        secretsmanager.create_secret(Name=name, SecretString=key,
                                     Description="OpenAI API key for the digital twin (read at runtime by the app)")
        print(f"Created {name}")


def ensure_openai_secret(secretsmanager, name):
    """On a first deploy without a stored key, asks for it once (same as set-openai-key)."""
    try:
        secretsmanager.describe_secret(SecretId=name)
        return
    except secretsmanager.exceptions.ResourceNotFoundException:
        pass
    print(f"No OpenAI key stored yet in {name}.")
    key = prompt_openai_key()
    verify_openai_key(key.strip())
    set_openai_key(secretsmanager, name, key)

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

def publish_code(session, target_name, zip_path, ensure_bucket=True):
    """Uploads a built package and returns the stack parameters that point at it.

    ensure_bucket=False (CI): the build role may only write packages, not create or reconfigure the bucket.
    """
    target = TARGETS[target_name]
    account = session.client("sts").get_caller_identity()["Account"]
    s3 = session.client("s3")
    bucket = artifact_bucket_name(account, session.region_name)
    if ensure_bucket:
        ensure_artifact_bucket(s3, bucket, session.region_name)
    key = target.key_prefix + os.path.basename(zip_path)
    artifact = publish_artifact(s3, bucket, key, zip_path)
    params = {"CodeBucket": bucket, "CodeKey": key, "CodeVersionId": artifact["VersionId"]}
    if target_name == "ec2":
        params["CodeSha256"] = artifact["Sha256"]
    return params


def find_connection_arn(codeconnections, name=CONNECTION_NAME):
    """The GitHub connection is created once and approved in the console; it must be AVAILABLE to be usable."""
    for connection in codeconnections.list_connections(ProviderTypeFilter="GitHub", MaxResults=50)["Connections"]:
        if connection["ConnectionName"] == name:
            if connection["ConnectionStatus"] != "AVAILABLE":
                raise RuntimeError(f"connection {name} is {connection['ConnectionStatus']}; finish the GitHub "
                                   "authorization in the console (Developer Tools > Settings > Connections)")
            return connection["ConnectionArn"]
    raise RuntimeError(f"no GitHub connection named {name}; create it and approve it in the console first")


def deploy_pipeline(session, secret_name, waf=True, notification_email=None, connection_arn=None):
    """Bootstraps the CI/CD stack. Everything the pipeline relies on but does not own is ensured here first:
    the code bucket, the OpenAI key secret and the WAF web ACL (us-east-1, outside the pipeline's region)."""
    app = TARGETS["serverless"]
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = artifact_bucket_name(account, session.region_name)
    ensure_artifact_bucket(session.client("s3"), bucket, session.region_name)
    ensure_openai_secret(session.client("secretsmanager"), secret_name)
    params = {
        "ConnectionArn": connection_arn or find_connection_arn(session.client("codeconnections")),
        "AppStackName": app.stack_name,
        "CodeBucketName": bucket,
        "OpenAIApiKeySecretName": secret_name,
        "ReservedConcurrency": reserved_concurrency(session.client("lambda")),
        "WebAclArn": deploy_waf(session, f"{app.stack_name}-waf", app.waf_post_rate) if waf else "",
    }
    if notification_email is not None:  # omitted -> keep the current subscription
        params["NotificationEmail"] = notification_email
    return deploy_stack(session.client("cloudformation"), PIPELINE_STACK, os.path.join(INFRA, "pipeline.yaml"), params)


def deploy(session, target_name, stack_name, secret_name, waf=True, notification_email=None):
    target = TARGETS[target_name]
    ensure_openai_secret(session.client("secretsmanager"), secret_name)
    zip_path = build(target_name, os.path.join(tempfile.gettempdir(), "digitaltwin-build"), secret_name, session.region_name)
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
    p.add_argument("--secret-name", default=DEFAULT_SECRET_NAME, help="serverless: key source when re-embedding")
    add_region(p)

    p = sub.add_parser("set-openai-key", help="store or rotate the OpenAI API key in Secrets Manager (prompts, hidden)")
    p.add_argument("--secret-name", default=DEFAULT_SECRET_NAME)
    p.add_argument("--skip-verify", action="store_true", help="store without the test calls to OpenAI")
    add_region(p)

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

    p = sub.add_parser("pipeline", help="bootstrap or update the CI/CD pipeline stack (serverless app)")
    p.add_argument("--secret-name", default=DEFAULT_SECRET_NAME)
    p.add_argument("--no-waf", action="store_true")
    p.add_argument("--notification-email", help="app alarms, contact requests and pipeline failures")
    p.add_argument("--connection-arn", help=f"defaults to the AVAILABLE GitHub connection named {CONNECTION_NAME}")
    add_region(p)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(build(args.target, args.out_dir, args.secret_name, args.region))
            return 0
        session = boto3.Session(region_name=getattr(args, "region", DEFAULT_REGION))
        if args.command == "set-openai-key":
            key = prompt_openai_key().strip()
            check_key_format(key)
            if not args.skip_verify:
                print("Checking the key with OpenAI...", flush=True)
                verify_openai_key(key)
            set_openai_key(session.client("secretsmanager"), args.secret_name, key)
            print("Serverless picks the new key up automatically. EC2: restart the service "
                  "(sudo systemctl restart digitaltwin, via SSM) or it keeps the old key until its next restart.")
        elif args.command == "deploy":
            stack_name = args.stack_name or TARGETS[args.target].stack_name
            deploy(session, args.target, stack_name, args.secret_name, not args.no_waf, args.notification_email)
        elif args.command == "publish":
            params = publish_code(session, args.target, args.zip, ensure_bucket=False)
            with open(args.params_out, "w", encoding="utf-8") as f:
                json.dump({"Parameters": params}, f, indent=2)
            print(f"Wrote {args.params_out}")
        elif args.command == "publish-site":
            publish_site(session, args.stack_name)
        elif args.command == "pipeline":
            outputs = deploy_pipeline(session, args.secret_name, not args.no_waf, args.notification_email,
                                      args.connection_arn)
            for key, value in sorted(outputs.items()):
                print(f"  {key}: {value}")
        elif args.command == "waf":
            print(deploy_waf(session, args.stack_name, args.post_rate_limit))
        return 0
    except (BotoCoreError, ClientError, RuntimeError) as e:  # BotoCoreError: credentials, network, waiters
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
