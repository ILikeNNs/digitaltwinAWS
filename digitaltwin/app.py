import os
import gradio as gr
from openai import OpenAI
# Import modular layers
from . import PROJECT_ROOT
from .database import init_sql_db, log_conversation, log_contact_request
from .rag_engine import RAGEngine
from dotenv import load_dotenv
# Persona and model settings live in helpers so the serverless deployment shares them
from .helpers import (
    SYSTEM_BASE_PROMPT, MAX_MESSAGE_LENGTH, MAX_SESSION_TURNS, LOG_RETENTION_DAYS, GENERIC_ERROR_REPLY,
    SESSION_LIMIT_REPLY, sanitize_input, model_history, stream_reply, make_tool_handler,
)

load_dotenv(override=True)

api_key = os.getenv('OPENAI_API_KEY')

PDF_PATH = os.path.join(PROJECT_ROOT, "linkedin.pdf")

# Initialize OpenAI Client (Make sure OPENAI_API_KEY is in your environment)
client = OpenAI()

# ----------------------------------------------------
# 2. Database Initialization
# ----------------------------------------------------
rag = RAGEngine(api_key=api_key)
init_sql_db()
rag.index_pdf(PDF_PATH)

# ----------------------------------------------------
# 3. Chat Logic
# ----------------------------------------------------

# Tool calls go to the Chroma index and the SQLite contact log; the dispatch is shared with serverless/server.py
handle_tool_calls = make_tool_handler(
    rag.retrieve_context, log_contact_request, on_tool_call=lambda name: print(f"tool call: {name}"),
)


def chat_stream(history):
    """Streams the assistant reply into the conversation as it arrives, then logs the exchange.

    Yields the full history after every received text fragment, so Gradio can re-render the reply live.
    """
    user_question = history[-1]["content"] if history else ""
    history = [{"role":h["role"], "content":h["content"]} for h in history]
    reply = ""

    if sum(1 for h in history if h["role"] == "user") > MAX_SESSION_TURNS:
        reply = SESSION_LIMIT_REPLY
    else:
        # Only recent user/assistant turns reach the model, so a long session cannot grow the prompt without bound
        messages = [{"role": "system", "content": SYSTEM_BASE_PROMPT}] + model_history(history)
        try:
            for fragment in stream_reply(client, messages, handle_tool_calls):
                reply += fragment
                yield history + [{"role":"assistant", "content":reply}]
        except Exception as e:
            # Details go to the server log only: the app is public and error text can leak internals.
            # Keep whatever was already streamed so the visitor's screen doesn't jump.
            print(f"chat failed: {e!r}")
            reply = f"{reply}\n\n{GENERIC_ERROR_REPLY}" if reply else GENERIC_ERROR_REPLY

    # Show the reply (or the error) in the chat either way, so the UI never appears to hang
    history += [{"role":"assistant", "content":reply}]
    try:
        log_conversation(user_question, reply)
    except Exception as e:
        # A full disk or locked database must not take the chat down with it
        print(f"logging failed: {e!r}")
    yield history


def chat(history):
    """Non-streaming variant of chat_stream: returns the final history."""
    for history in chat_stream(history):
        pass
    return history

# ----------------------------------------------------
# 4. Gradio Interface Construction
# ----------------------------------------------------
# Callbacks (along with the chat() function above)

def put_message_in_chatbot(message, history):
        return "", history + [{"role":"user", "content":sanitize_input(message, max_length=MAX_MESSAGE_LENGTH)}]

def submit_message(message, history):
    message, history = put_message_in_chatbot(message, history)
    return message, history, history

def answer(history):
    # A generator handler makes Gradio stream every partial reply to the browser
    for updated in chat_stream(history):
        yield updated, updated

# UI definition

with gr.Blocks() as ui:
    # The conversation lives in server-side session state; the Chatbot only displays it.
    # Reading history from the Chatbot would let a client rewrite earlier turns or inject system messages.
    history_state = gr.State([])
    with gr.Row():
        chatbot = gr.Chatbot(height=500)
    with gr.Row():
        message = gr.Textbox(label="Chat with my digital twin:", max_length=MAX_MESSAGE_LENGTH)
    gr.Markdown(
        f"Conversations are logged to improve this assistant and deleted after {LOG_RETENTION_DAYS} days. "
        "Contact details you share are stored only so I can get back to you."
    )

    # Hooking up events to callbacks; "private" keeps them out of the public Gradio API
    message.submit(
        submit_message, inputs=[message, history_state], outputs=[message, history_state, chatbot],
        api_visibility="private"
    ).then(
        answer, inputs=history_state, outputs=[history_state, chatbot], api_visibility="private"
    )

if __name__ == "__main__":
    # Run locally
    ui.launch(inbrowser=True)