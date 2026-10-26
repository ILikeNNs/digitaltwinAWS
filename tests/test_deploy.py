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
    for name in ("digitaltwin.yaml", "serverless.yaml", "waf.yaml"):
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
        cf_stub.add_response("create_invalidation", {}, {"DistributionId": "D1", "InvalidationBatch": {
            "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": ANY}})

        deploy.publish_site(_Session(cloudformation=cfn, s3=s3, cloudfront=cloudfront), "stack")
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
    monkeypatch.setattr(deploy, "publish_code", lambda session, target, zip_path: {"CodeBucket": "b", "CodeKey": "k"})
    out = tmp_path / "params.json"

    assert deploy.main(["publish", "serverless", "--zip", "x.zip", "--params-out", str(out)]) == 0
    assert json.loads(out.read_text()) == {"Parameters": {"CodeBucket": "b", "CodeKey": "k"}}
