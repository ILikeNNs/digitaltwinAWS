"""Streaming chat API for the serverless digital twin.

Runs as a small Starlette app inside Lambda via the Lambda Web Adapter (run.sh starts uvicorn; the adapter
turns Function URL invocations into HTTP requests and streams the response back). CloudFront reaches the
Function URL through Origin Access Control, so only signed requests from our distribution get here.

POST /api/chat   {"sessionId": "<optional, from a previous response>", "message": "<user text>"}
  -> application/x-ndjson, one JSON object per line:
     {"sessionId": "..."}                first, always
     {"delta": "..."}                    reply text as it streams
     {"done": true, "sessionEnded": b}   last
The browser only ever sends its newest message; history is kept server-side so it cannot be forged.
"""
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key
from openai import AuthenticationError, OpenAI
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

# Shared with the Gradio app: persona, model settings, tool schemas, limits, sanitizing and the streaming loop
from helpers import (
    SYSTEM_BASE_PROMPT, MAX_MESSAGE_LENGTH, MAX_SESSION_TURNS, MAX_CONTACTS_PER_HOUR,
    GENERIC_ERROR_REPLY, SESSION_LIMIT_REPLY, sanitize_input, is_valid_email, model_history, stream_reply,
)
from retriever import Retriever


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, so CloudWatch Logs Insights can query fields."""

    def format(self, record):
        entry = {"level": record.levelname, "message": record.getMessage(), "logger": record.name}
        for key in ("tool", "session"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logger = logging.getLogger("digitaltwin")

LOG_TTL_DAYS = int(os.environ.get("LOG_TTL_DAYS", "365"))
SESSION_TTL_HOURS = 24
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_secrets = boto3.client("secretsmanager")


def _load_openai_key():
    return _secrets.get_secret_value(SecretId=os.environ["OPENAI_SECRET_ID"])["SecretString"]


# Created once per cold start (during Lambda init, before the adapter's readiness check passes) and reused
client = OpenAI(api_key=_load_openai_key())
rag = Retriever(client)


def refresh_openai_key():
    """Picks up a key rotated in Secrets Manager without a redeploy; True if the stored key differs from ours."""
    global client
    key = _load_openai_key()
    if key == client.api_key:
        return False
    client = OpenAI(api_key=key)
    rag.client = client
    return True


def stream_reply_refreshing_key(build_messages):
    """stream_reply, retried once with a freshly loaded key if OpenAI rejects ours (rotated, revoked or expired).

    `build_messages` returns a fresh message list, since stream_reply extends the one it is given.
    """
    try:
        yield from stream_reply(client, build_messages(), handle_tool_calls)
    except AuthenticationError:
        if not refresh_openai_key():
            raise
        logger.warning("OpenAI rejected the key; reloaded a new one from Secrets Manager and retrying")
        yield from stream_reply(client, build_messages(), handle_tool_calls)

# ----------------------------------------------------
# DynamoDB (single table)
#   pk=CONVERSATION | CONTACT     sk=<iso time>#<rand>   log records, written only
#   pk=SESSION#<id>               sk=SESSION             server-side conversation state (24h TTL)
# ----------------------------------------------------

def _put_record(record_type, ttl_days=None, **attributes):
    now = datetime.now(timezone.utc)
    item = {
        "pk": record_type,
        "sk": f"{now.isoformat()}#{uuid.uuid4().hex[:8]}",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        **attributes,
    }
    if ttl_days:
        item["expiresAt"] = int(now.timestamp()) + ttl_days * 86400
    table.put_item(Item=item)


def log_conversation(question, answer):
    _put_record(
        "CONVERSATION",
        ttl_days=LOG_TTL_DAYS,
        user_question=sanitize_input(question, max_length=1000),
        bot_answer=sanitize_input(answer, max_length=4000),
    )


def _recent_contact_count():
    # The role may only COUNT this partition (IAM dynamodb:Select condition), never read contact details back
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    response = table.query(KeyConditionExpression=Key("pk").eq("CONTACT") & Key("sk").gte(hour_ago), Select="COUNT")
    return response["Count"]


def log_contact_request(name, email, message):
    """Saves a structured contact request; a DynamoDB stream -> EventBridge Pipe forwards it by email."""
    clean_name = sanitize_input(name)
    clean_email = sanitize_input(email, max_length=320)
    if not is_valid_email(clean_email):
        return "Error: that email address doesn't look valid. Please ask the user to double-check it."
    if _recent_contact_count() >= MAX_CONTACTS_PER_HOUR:
        return "Error: too many contact requests right now. Please ask the user to try again later."
    _put_record("CONTACT", name=clean_name, email=clean_email, message=sanitize_input(message))
    return f"Success: Contact request for {clean_name} has been securely logged."


def load_session(session_id):
    if not session_id or not SESSION_ID_PATTERN.match(session_id):
        return uuid.uuid4().hex, []
    item = table.get_item(Key={"pk": f"SESSION#{session_id}", "sk": "SESSION"}).get("Item")
    if not item:
        return uuid.uuid4().hex, []
    return session_id, item["history"]


def save_session(session_id, history):
    table.put_item(Item={
        "pk": f"SESSION#{session_id}",
        "sk": "SESSION",
        "history": history,
        "expiresAt": int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_HOURS * 3600,
    })

# ----------------------------------------------------
# Chat logic (mirrors app.py)
# ----------------------------------------------------

def handle_tool_calls(message):
    messages = []
    for tool_call in message.tool_calls:
        arguments = json.loads(tool_call.function.arguments)
        if tool_call.function.name == "query_resume":
            tool_output = rag.retrieve_context(query=arguments.get("query"))
        elif tool_call.function.name == "save_contact_info":
            tool_output = log_contact_request(
                name=arguments.get("name"),
                email=arguments.get("email"),
                message=arguments.get("message"),
            )
        else:
            tool_output = "Error: Tool not found."
        logger.info("tool call", extra={"tool": tool_call.function.name})
        messages.append({
            "tool_call_id": tool_call.id,
            "role": "tool",
            "name": tool_call.function.name,
            "content": tool_output,
        })
    return messages


def _line(**fields):
    return json.dumps(fields) + "\n"


def reply_stream(session_id, history, message):
    """Generates the NDJSON response body; Starlette iterates it in a worker thread (the SDK calls block)."""
    yield _line(sessionId=session_id)
    if sum(1 for m in history if m["role"] == "user") >= MAX_SESSION_TURNS:
        yield _line(delta=SESSION_LIMIT_REPLY)
        yield _line(done=True, sessionEnded=True)
        return

    history.append({"role": "user", "content": message})
    reply = ""
    try:
        for fragment in stream_reply_refreshing_key(
                lambda: [{"role": "system", "content": SYSTEM_BASE_PROMPT}] + model_history(history)):
            reply += fragment
            yield _line(delta=fragment)
    except AuthenticationError:
        # A distinct message so a CloudWatch metric filter can alarm on it: the stored key itself is bad
        logger.exception("openai authentication failed", extra={"session": session_id})
        reply = GENERIC_ERROR_REPLY
        yield _line(delta=reply)
    except Exception:
        # Details go to CloudWatch, not to the (public) caller; keep what was already streamed
        logger.exception("chat failed", extra={"session": session_id})
        tail = f"\n\n{GENERIC_ERROR_REPLY}" if reply else GENERIC_ERROR_REPLY
        reply += tail
        yield _line(delta=tail)

    history.append({"role": "assistant", "content": sanitize_input(reply, max_length=4000)})
    # Persistence failures are logged but never cost the visitor their answer
    try:
        save_session(session_id, history)
        log_conversation(message, reply)
    except Exception:
        logger.exception("persisting conversation failed", extra={"session": session_id})
    yield _line(done=True, sessionEnded=False)

# ----------------------------------------------------
# HTTP layer
# ----------------------------------------------------

async def chat_endpoint(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        message = sanitize_input(payload.get("message"), max_length=MAX_MESSAGE_LENGTH)
        if not message:
            raise ValueError("'message' must be a non-empty string")
        session_id = payload.get("sessionId") if isinstance(payload.get("sessionId"), str) else None
    except (ValueError, UnicodeDecodeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    session_id, history = await run_in_threadpool(load_session, session_id)
    return StreamingResponse(
        reply_stream(session_id, history, message),
        media_type="application/x-ndjson",
        headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
    )


async def healthz(request: Request):
    """Readiness check for the Lambda Web Adapter (AWS_LWA_READINESS_CHECK_PATH)."""
    return PlainTextResponse("ok")


app = Starlette(routes=[
    Route("/api/chat", chat_endpoint, methods=["POST"]),
    Route("/healthz", healthz, methods=["GET"]),
])
