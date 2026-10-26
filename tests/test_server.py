"""Tests for serverless/server.py's handling of a rotated or rejected OpenAI key. AWS and OpenAI are faked."""
import importlib.util
import logging
import os
import sys
import types

import pytest

boto3 = pytest.importorskip("boto3")  # "deploy" dependency group (the Lambda runtime provides boto3)
import httpx  # noqa: E402
import openai  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeOpenAI:
    def __init__(self, api_key, **kwargs):
        self.api_key = api_key


def _auth_error():
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return openai.AuthenticationError("Incorrect API key provided", response=response, body=None)


@pytest.fixture
def server(monkeypatch):
    """Loads server.py with fake DynamoDB/Secrets Manager/OpenAI; `stored_key` is what Secrets Manager returns."""
    state = {"stored_key": "sk-old"}
    monkeypatch.setenv("TABLE_NAME", "table")
    monkeypatch.setenv("OPENAI_SECRET_ID", "secret")
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: types.SimpleNamespace(Table=lambda name: None))
    monkeypatch.setattr(boto3, "client", lambda *a, **k: types.SimpleNamespace(
        get_secret_value=lambda SecretId: {"SecretString": state["stored_key"]}))
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    retriever = types.ModuleType("retriever")
    retriever.Retriever = lambda client: types.SimpleNamespace(client=client)
    monkeypatch.setitem(sys.modules, "retriever", retriever)
    monkeypatch.syspath_prepend(ROOT)

    spec = importlib.util.spec_from_file_location("server", os.path.join(ROOT, "serverless", "server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "save_session", lambda *a: None)
    monkeypatch.setattr(module, "log_conversation", lambda *a: None)

    def fake_stream_reply(client, messages, handle_tool_calls):
        if client.api_key != "sk-good":
            raise _auth_error()
        yield "Hi there"

    monkeypatch.setattr(module, "stream_reply", fake_stream_reply)
    return module, state


def _deltas(lines):
    import json
    return [json.loads(line).get("delta") for line in lines if "delta" in line]


def test_rotated_key_is_picked_up_without_restart(server):
    module, state = server
    state["stored_key"] = "sk-good"  # rotated with set-openai-key after this Lambda started

    lines = list(module.reply_stream("a" * 32, [], "hello"))

    assert _deltas(lines) == ["Hi there"]
    assert module.client.api_key == "sk-good"
    assert module.rag.client is module.client


def test_rejected_key_without_rotation_fails_cleanly_and_is_logged(server, caplog):
    module, state = server  # the stored key is still the rejected one

    with caplog.at_level(logging.ERROR, logger="digitaltwin"):
        lines = list(module.reply_stream("a" * 32, [], "hello"))

    assert _deltas(lines) == [module.GENERIC_ERROR_REPLY]
    assert '"done": true' in lines[-1]
    assert any(r.getMessage() == "openai authentication failed" for r in caplog.records)


def test_auth_failure_alarm_matches_the_logged_message():
    # The metric filter in infra/serverless.yaml must match server.py's log message, or the alarm silently never fires
    import re

    with open(os.path.join(ROOT, "infra", "serverless.yaml"), encoding="utf-8") as f:
        pattern = re.search(r"""FilterPattern: '"([^"]+)"'""", f.read()).group(1)
    with open(os.path.join(ROOT, "serverless", "server.py"), encoding="utf-8") as f:
        assert f'logger.exception("{pattern}"' in f.read()
