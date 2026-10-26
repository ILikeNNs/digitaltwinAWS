import re
from types import SimpleNamespace

MODEL = "gpt-6-luna"
# gpt-6-luna rejects function tools in Chat Completions unless reasoning is off; answering from retrieved resume
# context doesn't need it, and it keeps replies fast and cheap
REASONING_EFFORT = "none"
TEMPERATURE = 0.3
# Upper bound on tool-call round trips per user message, so a looping model cannot burn tokens forever
MAX_TOOL_ROUNDS = 5

# Spend guards: the chat is public and every token is billed to our OpenAI key
MAX_COMPLETION_TOKENS = 1000   # per model call
MAX_MESSAGE_LENGTH = 2000      # characters per user message
MAX_HISTORY_MESSAGES = 20      # most recent turns sent to the model
MAX_SESSION_TURNS = 30         # user messages per conversation before it must be restarted
MAX_CONTACTS_PER_HOUR = 10     # global cap, so the contact tool cannot be used to flood storage or email
LOG_RETENTION_DAYS = 365       # conversation logs are deleted after this; contact requests are kept

GENERIC_ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."
SESSION_LIMIT_REPLY = "This conversation has reached its length limit. Please refresh the page to start a new one."

EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")


def sanitize_input(text, max_length=1000):
    """
    Sanitizes and truncates input strings to prevent buffer/bloat attacks.
    Removes raw null bytes which can cause unexpected database behavior.
    """
    if not isinstance(text, str):
        return ""

    # Remove null characters which can trick string terminators
    text = text.replace('\x00', '')

    # Truncate text to prevent long-string Denial of Service (DoS) attacks
    return text[:max_length].strip()


def is_valid_email(email):
    return bool(EMAIL_PATTERN.match(email or ""))


def model_history(history):
    """Returns the part of a conversation sent to the model: user/assistant turns only, capped to the most recent ones."""
    return [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant")
    ][-MAX_HISTORY_MESSAGES:]

# Replace this text with your actual resume and background details
SYSTEM_BASE_PROMPT = """
You are the AI Digital Twin of the professional outlined in the documents.
Answer questions on their behalf using exclusively the retrieved background context provided below.

Guidelines:
- Speak strictly in the first person ("I built...", "In my previous role...").
- Be professional, polite, and clear.
- Base your answers directly on the retrieved context. If the context does not contain the answer, say "I don't have that information explicitly listed in my resume, but I'd be happy to discuss it over an email."
"""

tools = [
    {
        "type": "function",
        "function": {
            "name": "query_resume",
            "description": "Searches the candidate's vector database resume for answers regarding skills, history, and education.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search term or question about the resume."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_contact_info",
            "description": "Logs the name, email, and message of a recruiter or user who wants to get in touch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Full name of the contact person."},
                    "email": {"type": "string", "description": "Email address of the contact person."},
                    "message": {"type": "string", "description": "The specific inquiry or message they left."}
                },
                "required": ["name", "email", "message"]
            }
        }
    }
]


def stream_reply(client, messages, handle_tool_calls):
    """Runs the tool-calling loop with streamed completions, yielding the assistant's reply text as it arrives.

    Tool-call rounds are resolved silently (their deltas are reassembled here); only reply text is yielded.
    `handle_tool_calls(message)` receives an object with `.tool_calls[i].id/.function.name/.function.arguments`
    and returns the tool result messages. `messages` is extended in place, like the non-streaming loop did.
    """
    for tool_round in range(MAX_TOOL_ROUNDS + 1):
        stream = client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools, tool_choice="auto", temperature=TEMPERATURE,
            reasoning_effort=REASONING_EFFORT, max_completion_tokens=MAX_COMPLETION_TOKENS, stream=True,
        )
        calls = {}  # tool-call index -> {"id", "name", "arguments"}, filled from fragments spread over chunks
        finish_reason = None
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                yield delta.content
            for fragment in delta.tool_calls or []:
                call = calls.setdefault(fragment.index, {"id": None, "name": "", "arguments": ""})
                if fragment.id:
                    call["id"] = fragment.id
                if fragment.function and fragment.function.name:
                    call["name"] += fragment.function.name
                if fragment.function and fragment.function.arguments:
                    call["arguments"] += fragment.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        if finish_reason != "tool_calls":
            return
        if tool_round >= MAX_TOOL_ROUNDS:
            raise RuntimeError(f"Exceeded {MAX_TOOL_ROUNDS} tool-call rounds without a final answer.")
        ordered = [calls[i] for i in sorted(calls)]
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in ordered
            ],
        })
        messages.extend(handle_tool_calls(SimpleNamespace(tool_calls=[
            SimpleNamespace(id=c["id"], function=SimpleNamespace(name=c["name"], arguments=c["arguments"]))
            for c in ordered
        ])))
