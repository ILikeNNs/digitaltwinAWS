"""Tests for infra/deploy.py. AWS calls are answered by botocore's Stubber, so these run offline."""
import hashlib
import importlib.util
import json
import os
import zipfile

import pytest

# boto3 is in the "deploy" dependency group: uv run --group deploy pytest
boto3 = pytest.importorskip("boto3")
from botocore.stub import ANY, Stubber  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def deploy():
    spec = importlib.util.spec_from_file_location("deploy", os.path.join(ROOT, "infra", "deploy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(service, region="eu-north-1"):
    return boto3.client(service, region_name=region, aws_access_key_id="test", aws_secret_access_key="test")


def _template(tmp_path, body="Resources: {}"):
    path = tmp_path / "template.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


# ======================= build =======================

def test_ec2_build_is_reproducible(deploy, tmp_path):
    first = deploy.build_ec2(str(tmp_path / "a"))
    second = deploy.build_ec2(str(tmp_path / "b"))

    assert os.path.basename(first) == os.path.basename(second)
    with open(first, "rb") as f1, open(second, "rb") as f2:
        assert f1.read() == f2.read()


def test_ec2_build_contents_and_name(deploy, tmp_path):
    path = deploy.build_ec2(str(tmp_path))

    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    assert os.path.basename(path) == f"app-{digest[:12]}.zip"
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == sorted(deploy.EC2_APP_FILES)
        assert {info.external_attr >> 16 for info in zf.infolist()} == {0o100644}


def test_package_keys_match_template_patterns(deploy):
    # The CodeKey AllowedPattern in each template must accept the keys publish_code produces
    for target in deploy.TARGETS.values():
        with open(os.path.join(ROOT, "infra", target.template), encoding="utf-8") as f:
            template = f.read()
        assert f"AllowedPattern: ^{target.key_prefix}" in template

# ======================= artifacts =======================

def test_publish_artifact_reuses_existing_package(deploy, tmp_path):
    s3 = _client("s3")
    zip_path = tmp_path / "pkg.zip"
    zip_path.write_bytes(b"zip")
    with Stubber(s3) as stub:
        stub.add_response("head_object", {"VersionId": "v1", "Metadata": {"sha256": "abc"}},
                          {"Bucket": "b", "Key": "lambda/pkg.zip"})
        assert deploy.publish_artifact(s3, "b", "lambda/pkg.zip", str(zip_path)) == {"VersionId": "v1", "Sha256": "abc"}
        stub.assert_no_pending_responses()


def test_publish_artifact_uploads_with_checksum(deploy, tmp_path):
    s3 = _client("s3")
    zip_path = tmp_path / "pkg.zip"
    zip_path.write_bytes(b"zip")
    sha = hashlib.sha256(b"zip").hexdigest()
    with Stubber(s3) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_response("put_object", {"VersionId": "v2"},
                          {"Bucket": "b", "Key": "lambda/pkg.zip", "Body": ANY, "Metadata": {"sha256": sha}})
        assert deploy.publish_artifact(s3, "b", "lambda/pkg.zip", str(zip_path)) == {"VersionId": "v2", "Sha256": sha}


def test_publish_artifact_requires_versioning(deploy, tmp_path):
    s3 = _client("s3")
    zip_path = tmp_path / "pkg.zip"
    zip_path.write_bytes(b"zip")
    with Stubber(s3) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_response("put_object", {}, {"Bucket": "b", "Key": "k.zip", "Body": ANY, "Metadata": ANY})
        with pytest.raises(RuntimeError, match="versioning"):
            deploy.publish_artifact(s3, "b", "k.zip", str(zip_path))

# ======================= CloudFormation =======================

def _stack(status, parameters=(), outputs=()):
    return {"Stacks": [{
        "StackName": "s", "CreationTime": "2026-01-01T00:00:00Z", "StackStatus": status,
        "Parameters": [{"ParameterKey": k, "ParameterValue": "old"} for k in parameters],
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs],
    }]}


def _summary(*keys):
    return {"Parameters": [{"ParameterKey": k} for k in keys]}


def test_deploy_stack_no_changes_is_a_noop(deploy, tmp_path):
    cfn = _client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE"))
        stub.add_response("get_template_summary", _summary("A"))
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE", parameters=["A"]))
        stub.add_response("create_change_set", {"Id": "cs"}, {
            "StackName": "s", "ChangeSetName": ANY, "TemplateBody": ANY, "Capabilities": ["CAPABILITY_IAM"],
            "ChangeSetType": "UPDATE", "Parameters": [{"ParameterKey": "A", "ParameterValue": "new"}]})
        failed = {"Status": "FAILED", "StatusReason": "The submitted information didn't contain changes."}
        stub.add_response("describe_change_set", failed)  # polled by the waiter
        stub.add_response("describe_change_set", failed)  # read for the reason
        stub.add_response("delete_change_set", {})
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE", outputs=[("Url", "https://x")]))

        assert deploy.deploy_stack(cfn, "s", _template(tmp_path), {"A": "new"}) == {"Url": "https://x"}
        stub.assert_no_pending_responses()


def test_deploy_stack_keeps_previous_values_for_omitted_parameters(deploy, tmp_path):
    cfn = _client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE"))
        stub.add_response("get_template_summary", _summary("Code", "Email", "NewParam"))
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE", parameters=["Code", "Email"]))
        # Email omitted -> previous value; NewParam is new to the stack -> template default (not sent)
        stub.add_response("create_change_set", {"Id": "cs"}, {
            "StackName": "s", "ChangeSetName": ANY, "TemplateBody": ANY, "Capabilities": ANY, "ChangeSetType": "UPDATE",
            "Parameters": [{"ParameterKey": "Code", "ParameterValue": "v2"},
                           {"ParameterKey": "Email", "UsePreviousValue": True}]})
        stub.add_response("describe_change_set", {"Status": "CREATE_COMPLETE"})
        stub.add_response("execute_change_set", {})
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE"))  # polled by the stack waiter
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE", outputs=[("Url", "u")]))

        assert deploy.deploy_stack(cfn, "s", _template(tmp_path), {"Code": "v2"}) == {"Url": "u"}
        stub.assert_no_pending_responses()


def test_deploy_stack_creates_new_stack(deploy, tmp_path):
    cfn = _client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_client_error("describe_stacks", "ValidationError", "Stack with id s does not exist")
        stub.add_response("get_template_summary", _summary("A"))
        stub.add_response("create_change_set", {"Id": "cs"}, {
            "StackName": "s", "ChangeSetName": ANY, "TemplateBody": ANY, "Capabilities": ANY, "ChangeSetType": "CREATE",
            "Parameters": [{"ParameterKey": "A", "ParameterValue": "1"}]})
        stub.add_response("describe_change_set", {"Status": "CREATE_COMPLETE"})
        stub.add_response("execute_change_set", {})
        stub.add_response("describe_stacks", _stack("CREATE_COMPLETE"))
        stub.add_response("describe_stacks", _stack("CREATE_COMPLETE"))

        assert deploy.deploy_stack(cfn, "s", _template(tmp_path), {"A": 1}) == {}
        stub.assert_no_pending_responses()


def test_deploy_stack_rejects_unknown_parameters(deploy, tmp_path):
    cfn = _client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE"))
        stub.add_response("get_template_summary", _summary("A"))
        with pytest.raises(RuntimeError, match="no parameter.*Typo"):
            deploy.deploy_stack(cfn, "s", _template(tmp_path), {"A": 1, "Typo": 2})


def test_deploy_stack_refuses_rollback_complete(deploy, tmp_path):
    cfn = _client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", _stack("ROLLBACK_COMPLETE"))
        with pytest.raises(RuntimeError, match="delete it"):
            deploy.deploy_stack(cfn, "s", _template(tmp_path), {})


def test_deploy_stack_reports_failed_resources(deploy, tmp_path):
    cfn = _client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE"))
        stub.add_response("get_template_summary", _summary())
        stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE"))
        stub.add_response("create_change_set", {"Id": "cs"}, {
            "StackName": "s", "ChangeSetName": ANY, "TemplateBody": ANY, "Capabilities": ANY,
            "ChangeSetType": "UPDATE", "Parameters": []})
        stub.add_response("describe_change_set", {"Status": "CREATE_COMPLETE"})
        stub.add_response("execute_change_set", {})
        stub.add_response("describe_stacks", _stack("UPDATE_ROLLBACK_COMPLETE"))  # waiter -> failure state
        stub.add_response("describe_stack_events", {"StackEvents": [{
            "StackId": "id", "EventId": "e", "StackName": "s", "Timestamp": "2026-01-01T00:00:00Z",
            "LogicalResourceId": "ChatFunction", "ResourceStatus": "UPDATE_FAILED",
            "ResourceStatusReason": "Layer not found"}]})
        with pytest.raises(RuntimeError, match="ChatFunction: Layer not found"):
            deploy.deploy_stack(cfn, "s", _template(tmp_path), {})


def test_templates_fit_inline_limit(deploy):
    for name in ("digitaltwin.yaml", "serverless.yaml", "waf.yaml", "pipeline.yaml"):
        assert os.path.getsize(os.path.join(ROOT, "infra", name)) < deploy.INLINE_TEMPLATE_LIMIT

# ======================= concurrency =======================

@pytest.mark.parametrize("quota, expected", [(10, 0), (104, 0), (105, 5), (1000, 5)])
def test_reserved_concurrency_respects_unreserved_minimum(deploy, quota, expected):
    lam = _client("lambda")
    with Stubber(lam) as stub:
        stub.add_response("get_account_settings", {"AccountLimit": {"ConcurrentExecutions": quota}})
        assert deploy.reserved_concurrency(lam) == expected

# ======================= static site =======================

class _Session:
    """Hands out pre-stubbed clients by service name."""

    def __init__(self, **clients):
        self.clients = clients

    def client(self, service, **kwargs):
        return self.clients[service]


def _invalidation(status):
    return {"Invalidation": {"Id": "I1", "Status": status, "CreateTime": "2026-01-01T00:00:00Z",
                             "InvalidationBatch": {"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": "r"}}}


def test_publish_site_uploads_changed_deletes_removed_and_invalidates(deploy, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_bytes(b"<html>")
    (web / "app.js").write_bytes(b"new js")
    monkeypatch.setattr(deploy, "WEB_DIR", str(web))
    unchanged_etag = hashlib.md5(b"<html>").hexdigest()

    cfn, s3, cloudfront = _client("cloudformation"), _client("s3"), _client("cloudfront", "us-east-1")
    with Stubber(cfn) as cfn_stub, Stubber(s3) as s3_stub, Stubber(cloudfront) as cf_stub:
        cfn_stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE",
                                                         outputs=[("SiteBucketName", "site"), ("DistributionId", "D1")]))
        s3_stub.add_response("list_objects_v2", {"Contents": [
            {"Key": "index.html", "ETag": f'"{unchanged_etag}"'},
            {"Key": "app.js", "ETag": '"stale"'},
            {"Key": "old.css", "ETag": '"x"'},
        ]}, {"Bucket": "site"})
        s3_stub.add_response("put_object", {}, {
            "Bucket": "site", "Key": "app.js", "Body": b"new js", "ContentType": "text/javascript; charset=utf-8"})
        s3_stub.add_response("delete_objects", {}, {
            "Bucket": "site", "Delete": {"Objects": [{"Key": "old.css"}], "Quiet": True}})
        cf_stub.add_response("create_invalidation", _invalidation("InProgress"), {"DistributionId": "D1", "InvalidationBatch": {
            "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": ANY}})
        cf_stub.add_response("get_invalidation", _invalidation("Completed"), {"DistributionId": "D1", "Id": "I1"})

        deploy.publish_site(_Session(cloudformation=cfn, s3=s3, cloudfront=cloudfront), "stack", wait=True)
        for stub in (cfn_stub, s3_stub, cf_stub):
            stub.assert_no_pending_responses()


def test_publish_site_skips_invalidation_when_nothing_changed(deploy, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_bytes(b"<html>")
    monkeypatch.setattr(deploy, "WEB_DIR", str(web))

    cfn, s3 = _client("cloudformation"), _client("s3")
    with Stubber(cfn) as cfn_stub, Stubber(s3) as s3_stub:
        cfn_stub.add_response("describe_stacks", _stack("UPDATE_COMPLETE",
                                                         outputs=[("SiteBucketName", "site"), ("DistributionId", "D1")]))
        s3_stub.add_response("list_objects_v2", {"Contents": [
            {"Key": "index.html", "ETag": f'"{hashlib.md5(b"<html>").hexdigest()}"'}]})
        # No put/delete/invalidation stubbed: any such call would fail the test
        deploy.publish_site(_Session(cloudformation=cfn, s3=s3, cloudfront=None), "stack")

# ======================= CLI =======================

def test_publish_writes_template_configuration(deploy, tmp_path, monkeypatch):
    def fake_publish_code(session, target, zip_path, ensure_bucket=True):
        assert ensure_bucket is False  # CI's build role may not create or reconfigure the bucket
        return {"CodeBucket": "b", "CodeKey": "k"}

    monkeypatch.setattr(deploy, "publish_code", fake_publish_code)
    out = tmp_path / "params.json"

    assert deploy.main(["publish", "serverless", "--zip", "x.zip", "--params-out", str(out)]) == 0
    assert json.loads(out.read_text()) == {"Parameters": {"CodeBucket": "b", "CodeKey": "k"}}

# ======================= OpenAI key =======================

def test_set_openai_key_rotates_existing_secret(deploy):
    sm = _client("secretsmanager")
    with Stubber(sm) as stub:
        stub.add_response("put_secret_value", {}, {"SecretId": "s", "SecretString": "sk-new"})
        deploy.set_openai_key(sm, "s", "  sk-new\n")
        stub.assert_no_pending_responses()


def test_set_openai_key_creates_missing_secret(deploy):
    sm = _client("secretsmanager")
    with Stubber(sm) as stub:
        stub.add_client_error("put_secret_value", "ResourceNotFoundException")
        stub.add_response("create_secret", {}, {"Name": "s", "SecretString": "sk-1", "Description": ANY})
        deploy.set_openai_key(sm, "s", "sk-1")
        stub.assert_no_pending_responses()


def test_set_openai_key_rejects_non_keys_before_any_call(deploy):
    sm = _client("secretsmanager")
    with Stubber(sm):  # no responses queued: any AWS call would fail the test
        with pytest.raises(RuntimeError, match="doesn't look like"):
            deploy.set_openai_key(sm, "s", "my password")


def test_read_openai_key_explains_missing_secret(deploy):
    sm = _client("secretsmanager")
    with Stubber(sm) as stub:
        stub.add_client_error("get_secret_value", "ResourceNotFoundException")
        with pytest.raises(RuntimeError, match="set-openai-key"):
            deploy.read_openai_key(sm, "s")


@pytest.mark.parametrize("error_name, message", [
    ("AuthenticationError", "rejected this key"),
    ("PermissionDeniedError", "lacks a permission"),
    ("NotFoundError", "cannot use a model"),
])
def test_verify_openai_key_explains_failures(deploy, monkeypatch, error_name, message):
    import httpx
    import openai

    def failing_create(**kwargs):
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/x"))
        raise getattr(openai, error_name)("denied", response=response, body=None)

    fake = type("FakeOpenAI", (), {})()
    fake.chat = type("Chat", (), {"completions": type("C", (), {"create": staticmethod(failing_create)})()})()
    fake.embeddings = type("E", (), {"create": staticmethod(failing_create)})()
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: fake)

    with pytest.raises(RuntimeError, match=message):
        deploy.verify_openai_key("sk-test")


class _FakeS3:
    def __init__(self, cached):
        self.cached, self.calls = cached, []

    def download_file(self, bucket, key, path):
        self.calls.append(("download", key))
        if not self.cached:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        with open(path, "w") as f:
            f.write("{}")

    def upload_file(self, path, bucket, key):
        self.calls.append(("upload", key))


def _fake_serverless_build(deploy, monkeypatch, tmp_path, stale, cached, events):
    """Stands in for package.py, S3, STS and the secret; `events` records secret reads and index builds."""
    import sys
    import types

    fake_package = types.SimpleNamespace(
        INDEX_PATH=str(tmp_path / "embeddings.json"),
        index_is_stale=lambda: stale,
        pdf_sha256=lambda: "abc123",
        build_index=lambda: events.append(f"build_index:{os.environ.get('OPENAI_API_KEY')}"),
        build_zip=lambda out_dir: os.path.join(out_dir, "chat.zip"),
    )
    monkeypatch.setitem(sys.modules, "package", fake_package)
    monkeypatch.setattr(deploy, "read_openai_key", lambda sm, name: events.append("secret") or "sk-secret")
    s3 = _FakeS3(cached)
    session = types.SimpleNamespace(
        region_name="eu-north-1",
        client=lambda service, **kw: s3 if service == "s3" else types.SimpleNamespace(
            get_caller_identity=lambda: {"Account": "123456789012"}),
    )
    return s3, session


@pytest.mark.parametrize("stale, cached, env_key, expected", [
    # resume unchanged: nothing to fetch, no key needed
    (False, False, None, []),
    # resume changed, but embedded before (any machine): reuse the cache, no OpenAI call
    (True, True, None, ["download"]),
    # first build of a new resume: key from Secrets Manager, embed, cache the result
    (True, False, None, ["download", "secret", "build_index:sk-secret", "upload"]),
    # first build of a new resume with OPENAI_API_KEY already set (e.g. from .env): no secret read
    (True, False, "sk-env", ["download", "build_index:sk-env", "upload"]),
])
def test_build_serverless_embeds_only_on_cache_miss(deploy, monkeypatch, tmp_path, stale, cached, env_key, expected):
    events = []
    s3, session = _fake_serverless_build(deploy, monkeypatch, tmp_path, stale, cached, events)
    if env_key:
        monkeypatch.setenv("OPENAI_API_KEY", env_key)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert deploy.build_serverless(str(tmp_path), session) == os.path.join(str(tmp_path), "chat.zip")
    timeline = [c[0] for c in s3.calls] + events
    order = {"download": 0, "secret": 1}
    timeline.sort(key=lambda e: order.get(e, 2 if e.startswith("build_index") else 3))
    assert timeline == expected
    assert all(key == "embeddings/abc123.json" for _, key in s3.calls)


@pytest.mark.parametrize("cached", [True, False])
def test_build_serverless_cache_only_never_embeds(deploy, monkeypatch, tmp_path, cached):
    # CodeBuild holds no OpenAI key: a new resume must have been embedded (and cached) from a developer machine
    events = []
    s3, session = _fake_serverless_build(deploy, monkeypatch, tmp_path, True, cached, events)

    if cached:
        assert deploy.build_serverless(str(tmp_path), session, embed=False) == os.path.join(str(tmp_path), "chat.zip")
    else:
        with pytest.raises(RuntimeError, match="no cached embeddings.*build serverless"):
            deploy.build_serverless(str(tmp_path), session, embed=False)
    assert events == []
    assert s3.calls == [("download", "embeddings/abc123.json")]


# ======================= pipeline bootstrap =======================

def _connections(*items):
    return {"Connections": [{"ConnectionName": n, "ConnectionStatus": st, "ConnectionArn": f"arn:{n}"} for n, st in items]}


def test_find_connection_arn_requires_available_connection(deploy):
    cc = _client("codeconnections")
    with Stubber(cc) as stub:
        stub.add_response("list_connections", _connections(("other", "AVAILABLE"), ("digitaltwin-github", "AVAILABLE")))
        assert deploy.find_connection_arn(cc) == "arn:digitaltwin-github"
        stub.add_response("list_connections", _connections(("digitaltwin-github", "PENDING")))
        with pytest.raises(RuntimeError, match="PENDING.*console"):
            deploy.find_connection_arn(cc)
        stub.add_response("list_connections", _connections())
        with pytest.raises(RuntimeError, match="no GitHub connection"):
            deploy.find_connection_arn(cc)


def test_deploy_pipeline_ensures_dependencies_and_passes_parameters(deploy, monkeypatch):
    calls = []
    sts = _client("sts")
    monkeypatch.setattr(deploy, "ensure_artifact_bucket", lambda s3, bucket, region: calls.append(("bucket", bucket)))
    monkeypatch.setattr(deploy, "ensure_openai_secret", lambda sm, name: calls.append(("secret", name)))
    monkeypatch.setattr(deploy, "reserved_concurrency", lambda lam: 5)
    monkeypatch.setattr(deploy, "deploy_waf", lambda session, stack, rate: calls.append(("waf", stack, rate)) or "arn:waf")
    monkeypatch.setattr(deploy, "deploy_stack", lambda cfn, stack, template, params: calls.append(("stack", stack, params)) or {})

    class Session:
        region_name = "eu-north-1"

        def client(self, service, **kwargs):
            return sts if service == "sts" else None

    with Stubber(sts) as stub:
        stub.add_response("get_caller_identity", {"Account": "123456789012"})
        deploy.deploy_pipeline(Session(), "my/secret", connection_arn="arn:conn")

    assert calls[:3] == [("bucket", "digitaltwin-artifacts-123456789012-eu-north-1"), ("secret", "my/secret"),
                         ("waf", "digitaltwin-serverless-waf", 30)]
    _, stack, params = calls[3]
    assert stack == "digitaltwin-pipeline"
    assert params == {"ConnectionArn": "arn:conn", "AppStackName": "digitaltwin-serverless",
                      "CodeBucketName": "digitaltwin-artifacts-123456789012-eu-north-1",
                      "OpenAIApiKeySecretName": "my/secret", "ReservedConcurrency": 5, "WebAclArn": "arn:waf"}
    assert "NotificationEmail" not in params  # omitted -> the stack keeps its current value

# ======================= templates stay consistent =======================

def _load_template(name):
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    # Keep intrinsic functions as plain data: {"!Sub": [...]} etc.
    Loader.add_multi_constructor("!", lambda loader, suffix, node: {"!" + suffix: (
        loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node, deep=True) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node))})
    with open(os.path.join(ROOT, "infra", name), encoding="utf-8") as f:
        return yaml.load(f, Loader=Loader)


def _actions(statements):
    for stmt in statements:
        if stmt["Effect"] == "Allow":
            yield from (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])


# AWSXRayDaemonWriteAccess, attached to the chat function's role
XRAY_MANAGED_ACTIONS = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
                        "xray:GetSamplingTargets", "xray:GetSamplingStatisticSummaries"]


def test_permissions_boundary_covers_every_app_role_permission():
    import fnmatch

    pipeline, app = _load_template("pipeline.yaml"), _load_template("serverless.yaml")
    boundary = list(_actions(pipeline["Resources"]["WorkloadPermissionsBoundary"]["Properties"]["PolicyDocument"]["Statement"]))
    needed = list(XRAY_MANAGED_ACTIONS)
    for name, resource in app["Resources"].items():
        if resource["Type"] == "AWS::IAM::Role":
            for policy in resource["Properties"].get("Policies", []):
                needed += list(_actions(policy["PolicyDocument"]["Statement"]))
    uncovered = [a for a in needed if not any(fnmatch.fnmatchcase(a.lower(), b.lower()) for b in boundary)]
    assert not uncovered, f"app roles need actions outside WorkloadPermissionsBoundary: {uncovered}"


def test_permissions_boundary_is_scoped_to_the_app():
    # The repository writes the app roles' own policies, so only the boundary limits what a malicious commit can reach.
    # X-Ray has no resource-level permissions; everything else must name this app's resources.
    pipeline = _load_template("pipeline.yaml")
    statements = pipeline["Resources"]["WorkloadPermissionsBoundary"]["Properties"]["PolicyDocument"]["Statement"]
    unscoped = [a for s in statements if s["Resource"] == "*" for a in _actions([s])]
    assert unscoped and all(a.startswith("xray:") for a in unscoped), f"unscoped boundary actions: {unscoped}"


def test_build_role_cannot_read_secrets():
    # The build runs repository code (tests, packaging) and only reads cached embeddings, so it never needs the key
    build_role = _load_template("pipeline.yaml")["Resources"]["BuildRole"]
    actions = [a for p in build_role["Properties"]["Policies"] for a in _actions(p["PolicyDocument"]["Statement"])]
    assert not [a for a in actions if a.startswith("secretsmanager:")]


def test_pipeline_parameter_overrides_match_the_app_template():
    import re

    pipeline, app = _load_template("pipeline.yaml"), _load_template("serverless.yaml")
    deploy_action = next(a for stage in pipeline["Resources"]["Pipeline"]["Properties"]["Stages"]
                         for a in stage["Actions"] if a["ActionTypeId"]["Provider"] == "CloudFormation")
    template_text, _ = deploy_action["Configuration"]["ParameterOverrides"]["!Sub"]
    overrides = json.loads(re.sub(r"\$\{([^}]+)\}", r"\1", template_text))  # ${Param} -> Param; the JSON's own braces stay
    assert set(overrides) <= set(app["Parameters"]), "pipeline overrides parameters serverless.yaml doesn't declare"
    assert "PermissionsBoundaryArn" in overrides  # the escalation guard only works if the pipeline always sets it
    assert deploy_action["Configuration"]["TemplateConfiguration"] == "BuildOutput::params.json"


# ======================= dependency audit =======================

@pytest.mark.parametrize("lambda_vulnerable, others_vulnerable, outcome", [
    (False, False, "pass"),
    (False, True, "warn"),   # Gradio/EC2/dev packages aren't deployed by this pipeline: report, don't block
    (True, False, "fail"),   # what the Lambda ships: block the deploy
])
def test_audit_blocks_only_on_lambda_vulnerabilities(deploy, monkeypatch, tmp_path, capsys,
                                                     lambda_vulnerable, others_vulnerable, outcome):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "package", types.SimpleNamespace(
        LAMBDA_PYTHON="3.13", LAMBDA_PLATFORM="aarch64-manylinux2014", RUNTIME_PACKAGES=["openai"]))

    def fake_run(cmd, **kwargs):
        failed = cmd[0] == "uvx" and (
            (cmd[-1].endswith("requirements-lambda.txt") and lambda_vulnerable)
            or (cmd[-1].endswith("requirements-all.txt") and others_vulnerable))
        return types.SimpleNamespace(returncode=1 if failed else 0)

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    if outcome == "fail":
        with pytest.raises(RuntimeError, match="Lambda's dependencies"):
            deploy.audit_dependencies(str(tmp_path))
    else:
        deploy.audit_dependencies(str(tmp_path))
        assert ("WARNING" in capsys.readouterr().out) == (outcome == "warn")


# ======================= smoke tests =======================

class _FakeSite:
    """Answers deploy._http like the live deployment would, with knobs to break each part."""

    def __init__(self, index_html, api_status=400, direct_status=403, stale_ui=False, api_failures_before_ok=0):
        self.index_html, self.api_status, self.direct_status = index_html, api_status, direct_status
        self.stale_ui, self.api_failures_left = stale_ui, api_failures_before_ok
        self.requests = []

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30):
        self.requests.append((method, url, dict(headers or {})))
        security = {"content-security-policy": "default-src 'none'", "strict-transport-security": "max-age=1",
                    "x-content-type-options": "nosniff"}
        if url == "https://site.example/":
            return 200, security, b"<old ui>" if self.stale_ui else self.index_html
        if url == "https://site.example/app.js":
            return 200, security, b"js"
        if url == "https://site.example/api/chat":
            assert headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
            if self.api_failures_left:
                self.api_failures_left -= 1
                return 503, {}, b"propagating"
            return self.api_status, {}, b'{"error": "message must be non-empty"}'
        if url == "https://fn.example/api/chat":
            assert "x-amz-content-sha256" not in (headers or {})  # deliberately unsigned
            return self.direct_status, {}, b'{"Message": "Forbidden"}'
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture
def live(deploy, monkeypatch):
    with open(os.path.join(ROOT, "serverless", "web", "index.html"), "rb") as f:
        index_html = f.read()
    monkeypatch.setattr(deploy, "stack_outputs", lambda cfn, stack: {
        "SiteUrl": "https://site.example", "ChatFunctionUrl": "https://fn.example/"})
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    session = _Session(cloudformation=None)

    def run(**site_knobs):
        site = _FakeSite(index_html, **site_knobs)
        monkeypatch.setattr(deploy, "_http", site)
        deploy.smoke_test(session, "stack", deadline_seconds=0.05, retry_delay=0)
        return site
    return run


def test_smoke_test_passes_on_a_healthy_deployment(live):
    site = live()
    assert [m for m, _, _ in site.requests] == ["GET", "GET", "POST", "POST"]


def test_smoke_test_waits_for_cloudfront_propagation(live):
    site = live(api_failures_before_ok=2)
    assert sum(1 for _, url, _ in site.requests if url.endswith("site.example/api/chat")) == 3


@pytest.mark.parametrize("knobs, failing_check", [
    ({"direct_status": 200}, "Function URL refuses direct calls"),
    ({"stale_ui": True}, "site serves this commit's UI"),
    ({"api_status": 502}, "API answers through CloudFront"),
])
def test_smoke_test_fails_on_a_broken_deployment(live, knobs, failing_check):
    with pytest.raises(RuntimeError, match=f"smoke tests failed: .*{failing_check}"):
        live(**knobs)


# ======================= buildspecs =======================

def test_buildspecs_only_use_existing_commands_and_files(deploy):
    import re
    import yaml

    for name in ("build.yml", "publish.yml"):
        with open(os.path.join(ROOT, "buildspecs", name), encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        commands = [c for phase in spec["phases"].values() for c in phase["commands"]]
        for command in commands:
            for sub in re.findall(r"infra/deploy\.py (\S+)", command):
                with pytest.raises(SystemExit) as exit_info:
                    deploy.main([sub, "--help"])
                assert exit_info.value.code == 0, f"{name}: deploy.py has no '{sub}' command"
            for path in re.findall(r"(infra/\S+\.(?:yaml|guard)|serverless/\S+\.\w+)", command):
                assert os.path.exists(os.path.join(ROOT, path)), f"{name} references missing {path}"


def test_pipe_name_matches_the_deploy_roles_permissions():
    # CloudFormation names pipes "<LogicalId>-<random>" (no stack prefix), outside the deploy role's
    # pipe/<AppStackName>-* permissions; the first pipeline run failed on exactly this. The name must stay prefixed.
    app, pipeline = _load_template("serverless.yaml"), _load_template("pipeline.yaml")
    pipe = next(r for r in app["Resources"].values() if r["Type"] == "AWS::Pipes::Pipe")
    assert pipe["Properties"]["Name"] == {"!Sub": "${AWS::StackName}-contacts"}
    statements = pipeline["Resources"]["CfnDeployRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    pipes = next(s for s in statements if s["Sid"] == "Pipes")
    assert pipes["Resource"]["!Sub"].endswith(":pipe/${AppStackName}-*")


def test_markdown_only_pushes_do_not_trigger_the_pipeline():
    # CodePipeline's "**/*.md" only matches Markdown inside a directory; a README-only push at the repo root
    # triggered a full run until "*.md" was added.
    pipeline = _load_template("pipeline.yaml")
    push = pipeline["Resources"]["Pipeline"]["Properties"]["Triggers"][0]["GitConfiguration"]["Push"][0]
    assert {"*.md", "**/*.md"} <= set(push["FilePaths"]["Excludes"])
